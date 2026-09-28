"""ffprobe a reference video.

A separate probe from ``frame_extract._probe_video`` because the analyzer needs
two things that one does not read: the frame rate (every timecode and every
frame-tolerance check is derived from it) and whether there is an audio track
at all (no audio means no ASR pass, not a failed one).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from flowboard.services.frame_extract import FFPROBE_BIN, FrameExtractError, _run

_PROBE_TIMEOUT = 30


@dataclass(frozen=True)
class VideoMeta:
    duration: float
    fps: float
    width: int
    height: int
    has_audio: bool

    @property
    def aspect_ratio(self) -> str:
        """The nearest production ratio, not the raw fraction — 1080x1920 is
        "9:16", which is what a person means, not "0.5625"."""
        if not self.width or not self.height:
            return "unknown"
        r = self.width / self.height
        named = {"9:16": 9 / 16, "16:9": 16 / 9, "1:1": 1.0, "4:3": 4 / 3, "3:4": 3 / 4, "21:9": 21 / 9}
        best = min(named, key=lambda k: abs(named[k] - r))
        return best if abs(named[best] - r) < 0.03 else f"{self.width}:{self.height}"

    @property
    def frame(self) -> float:
        """One frame, in seconds. The unit every tolerance check is written in."""
        return 1.0 / self.fps if self.fps else 1 / 30

    def as_dict(self) -> dict:
        return {
            "duration": round(self.duration, 3),
            "fps": round(self.fps, 3),
            "width": self.width,
            "height": self.height,
            "aspect_ratio": self.aspect_ratio,
            "has_audio": self.has_audio,
        }


def _rate(text: str) -> float:
    try:
        return float(Fraction(text)) if text and text != "0/0" else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe(path: Path) -> VideoMeta:
    proc = _run(
        [
            FFPROBE_BIN, "-v", "error",
            "-show_entries", "format=duration:stream=codec_type,width,height,avg_frame_rate,r_frame_rate",
            "-of", "json", str(path),
        ],
        timeout=_PROBE_TIMEOUT,
    )
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError as exc:
        raise FrameExtractError("probe_parse", "ffprobe returned invalid JSON") from exc

    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise FrameExtractError("no_video", "file has no video stream")

    # avg_frame_rate is the real average; r_frame_rate is the container's
    # nominal rate and lies on variable-frame-rate phone footage.
    fps = _rate(video.get("avg_frame_rate", "")) or _rate(video.get("r_frame_rate", ""))
    return VideoMeta(
        duration=float((data.get("format") or {}).get("duration") or 0.0),
        fps=fps,
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
    )


def timecode(seconds: float) -> str:
    """MM:SS.cc — the format the reference shotlists already use."""
    seconds = max(0.0, seconds)
    return f"{int(seconds // 60):02d}:{seconds % 60:05.2f}"
