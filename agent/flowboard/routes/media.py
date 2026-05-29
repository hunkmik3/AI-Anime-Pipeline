"""Media cache routes.

`GET /media/:media_id` streams bytes (cache hit → immediate; miss → one-shot
fetch from GCS then cache). `GET /api/media/:media_id/status` exposes cache
state for the frontend to poll while it waits for a URL to arrive.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from flowboard.services import media as media_service

logger = logging.getLogger(__name__)

bytes_router = APIRouter(tags=["media"])
api_router = APIRouter(prefix="/api/media", tags=["media"])


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
def debug_assets():
    """Dev-only dump of every Asset row so we can see what URLs the extension
    has actually pushed to the agent. Remove once media flow is stable.
    """
    from sqlmodel import select as _select

    from flowboard.db import get_session
    from flowboard.db.models import Asset

    with get_session() as s:
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
