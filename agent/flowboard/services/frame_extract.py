"""Extract a still frame from a cached video at a timestamp via ffmpeg.

Phase 8.4 — "new shot from frame" continuity workflow. The user scrubs a
generated Video node's output and extracts the frame at a chosen second; the
frame becomes a brand-new visual_asset (kind=image) that can be wired into the
next shot's video node as its first_frame (i2v) for native Seedance continuity.

ffmpeg/ffprobe are system binaries (brew install ffmpeg). The two subprocess
boundaries (`_probe_video`, `_run_ffmpeg_extract`) are deliberately thin and
named so tests can monkeypatch them without spawning real ffmpeg.
"""
from __future__ import annotations

import json
import logging
import subprocess
import uuid
from pathlib import Path
from typing import Optional

from flowboard.db import get_session
from flowboard.db.models import Asset
from flowboard.services import media as media_service

logger = logging.getLogger(__name__)

FFMPEG_BIN = "ffmpeg"
FFPROBE_BIN = "ffprobe"

_PROBE_TIMEOUT = 30
_EXTRACT_TIMEOUT = 60


class FrameExtractError(Exception):
    """Carries a stable ``code`` the route maps onto an HTTP status."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _run(cmd: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=True
        )
    except FileNotFoundError as exc:  # binary not on PATH
        raise FrameExtractError("ffmpeg_missing", f"{cmd[0]} not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise FrameExtractError("timeout", f"{cmd[0]} timed out") from exc
    except subprocess.CalledProcessError as exc:
        msg = (exc.stderr or "").strip().splitlines()[-1:] or [f"{cmd[0]} failed"]
        raise FrameExtractError("ffmpeg_error", msg[0][:200]) from exc


def _probe_video(path: Path) -> dict:
    """ffprobe the source → {duration: float, width: int, height: int}."""
    proc = _run(
        [
            FFPROBE_BIN,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "format=duration:stream=width,height",
            "-of",
            "json",
            str(path),
        ],
        timeout=_PROBE_TIMEOUT,
    )
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError as exc:
        raise FrameExtractError("probe_parse", "ffprobe returned invalid JSON") from exc
    fmt = data.get("format") or {}
    streams = data.get("streams") or [{}]
    first = streams[0] if streams else {}
    return {
        "duration": float(fmt.get("duration") or 0.0),
        "width": int(first.get("width") or 0),
        "height": int(first.get("height") or 0),
    }


def _run_ffmpeg_extract(src: Path, time: float, out: Path) -> None:
    # -ss before -i = fast input seek; one frame; q:v 2 = high-quality JPEG.
    _run(
        [
            FFMPEG_BIN,
            "-v",
            "error",
            "-y",
            "-ss",
            f"{time:.3f}",
            "-i",
            str(src),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(out),
        ],
        timeout=_EXTRACT_TIMEOUT,
    )


def extract_frame(
    source_media_id: str,
    time: float,
    *,
    source_shot_id: Optional[str] = None,
    source_request_id: Optional[int] = None,
) -> dict:
    """Extract the frame at ``time`` seconds from the cached source video.

    Returns a DTO ``{media_id, asset_id, time, duration, width, height, mime}``.
    Raises ``FrameExtractError`` (mapped to HTTP by the route) on a missing
    cache file, out-of-range timestamp, or ffmpeg failure.
    """
    src = media_service.cached_path(source_media_id)
    if src is None:
        raise FrameExtractError(
            "not_cached", f"source media {source_media_id!r} has no local cache file"
        )

    info = _probe_video(src)
    duration = info["duration"]
    if time < 0 or (duration > 0 and time > duration):
        raise FrameExtractError(
            "time_out_of_range",
            f"time {time:.3f}s is outside the video [0, {duration:.3f}]s",
        )

    new_id = str(uuid.uuid4())
    out_path = media_service.MEDIA_CACHE_DIR / f"{new_id}.jpg"
    _run_ffmpeg_extract(src, time, out_path)
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise FrameExtractError("extract_failed", "ffmpeg produced no frame")

    rounded = round(float(time), 3)
    metadata: dict = {
        "source_type": "extracted_frame",
        "source_media_id": source_media_id,
        "source_time": rounded,
    }
    if source_shot_id:
        metadata["source_shot_id"] = str(source_shot_id)
    if source_request_id is not None:
        metadata["source_request_id"] = source_request_id

    with get_session() as s:
        row = Asset(
            uuid_media_id=new_id,
            kind="image",
            local_path=str(out_path),
            mime="image/jpeg",
            asset_metadata=metadata,
        )
        s.add(row)
        s.commit()
        s.refresh(row)
        asset_id = row.id

    logger.info(
        "extract-frame: src=%s time=%.3f -> media_id=%s asset_id=%s",
        source_media_id, rounded, new_id, asset_id,
    )
    return {
        "media_id": new_id,
        "asset_id": asset_id,
        "time": rounded,
        "duration": round(duration, 3),
        "width": info["width"],
        "height": info["height"],
        "mime": "image/jpeg",
    }
