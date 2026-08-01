"""Media cache routes.

`GET /media/:media_id` streams bytes (cache hit → immediate; miss → one-shot
fetch from GCS then cache). `GET /api/media/:media_id/status` exposes cache
state for the frontend to poll while it waits for a URL to arrive.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from flowboard.db import get_session
from flowboard.db.models import DownloadEvent
from flowboard.routes.deps import get_optional_user
from flowboard.services import resource_guard
from flowboard.services import media as media_service

logger = logging.getLogger(__name__)

bytes_router = APIRouter(tags=["media"])
api_router = APIRouter(prefix="/api/media", tags=["media"])


class DownloadedBody(BaseModel):
    node_id: Optional[int] = None


@api_router.post("/{media_id}/downloaded")
def mark_downloaded(
    media_id: str, body: DownloadedBody, user=Depends(get_optional_user)
) -> dict:
    """Record that the user actually downloaded this output.

    GET /media/:id doubles as the preview route, so we can't infer a download
    from it — the download button pings this explicitly. This is the strongest
    "the clip was kept/used" signal we get without asking for an extra click,
    and it drives the cost/waste stats."""
    if not media_service.is_valid_media_id(media_id):
        raise HTTPException(status_code=400, detail="invalid media_id")
    with get_session() as s:
        s.add(
            DownloadEvent(
                user_id=(user.id if user is not None else None),
                media_id=media_id,
                node_id=body.node_id,
            )
        )
        s.commit()
    return {"ok": True}


@bytes_router.get("/media/{media_id:path}")
async def get_media_bytes(media_id: str):
    media_id = media_service.normalize_media_id(media_id)
    if not media_service.is_valid_media_id(media_id):
        raise HTTPException(status_code=400, detail="invalid media_id")

    cached = media_service.cached_path(media_id)
    if cached is not None:
        return FileResponse(
            path=str(cached),
            media_type=media_service._mime_from_ext(cached.suffix),
        )

    # Cache miss — try one fetch through the stored URL.
    result = await media_service.fetch_and_cache(media_id)
    if result is None:
        status = media_service.status(media_id)
        return JSONResponse(status_code=404, content=status)
    _bytes, mime, path = result
    return FileResponse(path=str(path), media_type=mime)


@api_router.get("/{media_id}/status")
def get_media_status(media_id: str):
    media_id = media_service.normalize_media_id(media_id)
    if not media_service.is_valid_media_id(media_id):
        return JSONResponse(
            status_code=400,
            content={"available": False, "has_url": False, "reason": "invalid_id"},
        )
    return media_service.status(media_id)


# Extensions we treat as video → the thumbnail is the first frame (ffmpeg),
# not a direct image decode. Everything else goes through PIL.
_VIDEO_THUMB_EXTS = {".mp4", ".webm", ".mov", ".m4v", ".avi", ".mkv"}


def _build_thumb(src, thumb_path, w: int) -> bool:
    """Write a downscaled WEBP thumbnail of ``src`` to ``thumb_path``.

    Blocking (PIL + possibly ffmpeg) — call via ``run_in_threadpool`` so a big
    canvas firing dozens of thumb requests never stalls the event loop. For a
    video, a still first frame is extracted with ffmpeg first, then thumbnailed.
    Returns True on success, False if ``src`` couldn't be decoded as an image
    (caller then serves the original bytes)."""
    from PIL import Image

    frame_tmp = None
    try:
        img_src = src
        if src.suffix.lower() in _VIDEO_THUMB_EXTS:
            from flowboard.services import frame_extract

            frame_tmp = media_service.MEDIA_CACHE_DIR / f"thumbframe_{thumb_path.stem}.jpg"
            # First frame (t=0) — cheap and representative enough for a tile.
            frame_extract._run_ffmpeg_extract(src, 0.0, frame_tmp)
            if not frame_tmp.exists() or frame_tmp.stat().st_size == 0:
                return False
            img_src = frame_tmp

        with Image.open(img_src) as im:
            im = im.convert("RGB")
            im.thumbnail((w, w * 4))  # cap width; allow tall portraits
            im.save(thumb_path, "WEBP", quality=80, method=4)
        return True
    except Exception:  # noqa: BLE001 — non-image / ffmpeg error → serve original
        return False
    finally:
        if frame_tmp is not None:
            try:
                frame_tmp.unlink(missing_ok=True)
            except OSError:
                pass


@api_router.get("/{media_id}/thumb")
async def get_media_thumb(media_id: str, w: int = 256):
    """Downscaled WEBP thumbnail for grids/pickers — avoids shipping full-res
    (multi-MB) images, or a full <video> element, for tiny tiles. Cached on
    disk after the first request. For video media the thumbnail is the first
    frame (ffmpeg); everything else decodes directly. Falls back to the
    original bytes for non-images / resize failures."""
    media_id = media_service.normalize_media_id(media_id)
    if not media_service.is_valid_media_id(media_id):
        raise HTTPException(status_code=400, detail="invalid media_id")
    w = max(48, min(int(w), 640))

    thumb_path = media_service.MEDIA_CACHE_DIR / f"thumb_{w}_{media_id}.webp"
    if thumb_path.exists():
        return FileResponse(str(thumb_path), media_type="image/webp")

    src = media_service.cached_path(media_id)
    if src is None:
        result = await media_service.fetch_and_cache(media_id)
        if result is None:
            return JSONResponse(status_code=404, content=media_service.status(media_id))
        src = result[2]

    from starlette.concurrency import run_in_threadpool

    ok = await run_in_threadpool(_build_thumb, src, thumb_path, w)
    if ok:
        return FileResponse(str(thumb_path), media_type="image/webp")
    return FileResponse(str(src), media_type=media_service._mime_from_ext(src.suffix))


class ExtractFrameBody(BaseModel):
    # Lower bound here; the dynamic upper bound (video duration) is checked in
    # the service so it can 422 with the real range in the message.
    time: float = Field(ge=0)
    shot_id: Optional[str] = None
    request_id: Optional[int] = None


@api_router.post("/{media_id}/extract-frame")
def extract_frame_endpoint(media_id: str, body: ExtractFrameBody):
    """Phase 8.4 — extract a still frame from a cached video at ``time``
    seconds → a new visual_asset (kind=image). The frame is cached locally
    (like an upload) and hoisted to R2 on demand at the next gen via
    media_id_to_public_url, so no pre-upload here."""
    from flowboard.services import frame_extract

    media_id = media_service.normalize_media_id(media_id)
    if not media_service.is_valid_media_id(media_id):
        raise HTTPException(status_code=400, detail="invalid media_id")
    if media_service.cached_path(media_id) is None:
        raise HTTPException(status_code=404, detail="source video not cached")

    try:
        return frame_extract.extract_frame(
            media_id,
            body.time,
            source_shot_id=body.shot_id,
            source_request_id=body.request_id,
        )
    except frame_extract.FrameExtractError as exc:
        if exc.code == "time_out_of_range":
            raise HTTPException(status_code=422, detail=str(exc))
        if exc.code in ("ffmpeg_missing",):
            raise HTTPException(status_code=503, detail=str(exc))
        if exc.code == "not_cached":
            raise HTTPException(status_code=404, detail=str(exc))
        logger.error("extract-frame failed (%s): %s", exc.code, exc)
        raise HTTPException(status_code=500, detail=str(exc))


@api_router.get("/_debug/assets")
def debug_assets(user=Depends(get_optional_user)):
    """Dev-only dump of every Asset row so we can see what URLs the extension
    has actually pushed to the agent. Remove once media flow is stable.

    Admin-only. Every other media route is protected by media ids being opaque
    hashes rather than guessable numbers — and a route that lists them all
    dissolves exactly that protection, handing over the id of every asset in the
    company in one call.
    """
    from sqlmodel import select as _select

    from flowboard.db import get_session
    from flowboard.db.models import Asset

    with get_session() as s:
        resource_guard.require_unscoped(s, user)
        rows = s.exec(_select(Asset)).all()
        return {
            "count": len(rows),
            "rows": [
                {
                    "id": r.id,
                    "media_id": r.uuid_media_id,
                    "has_url": bool(r.url),
                    "url_head": (r.url or "")[:80] if r.url else None,
                    "mime": r.mime,
                    "cached": bool(r.local_path),
                    "node_id": r.node_id,
                }
                for r in rows
            ],
        }
