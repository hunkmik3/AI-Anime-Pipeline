"""Manga colorizer — utility endpoints for the colorize SEQUENCE.

The heavy jobs (build bible, character sheets, colorize a page, fix a region) run
through the worker queue (POST /api/requests with type "colorize_*"), so this
router only serves the interactive helpers the sequence node + Fix editor need:

  * POST /upload-page    — ingest a page/style image into the local media cache.
  * POST /detect-panels  — VLM panel boxes for the Fix picker.
  * POST /detect-objects — VLM object boxes for the Fix picker.
  * POST /sam-point      — MobileSAM click-to-segment → the object's box.
  * POST /download-zip   — bundle chosen colorized pages into a ZIP.
  * GET  /config         — is the engine / reader / SAM wired?
"""
from __future__ import annotations

import asyncio
import io
import uuid
import zipfile
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from pydantic import BaseModel

from flowboard.db.models import User
from flowboard.routes.deps import get_optional_user
from flowboard.services import media as media_service

router = APIRouter(prefix="/api/colorize", tags=["colorize"])

# Local page ingest — manga scans can be large, so allow more than the Flow cap.
_ALLOWED_MIMES = {"image/png", "image/jpeg", "image/webp"}
_MAX_PAGE_BYTES = 40 * 1024 * 1024


@router.get("/config")
def colorize_config():
    """So the UI can warn up front if the engine/reader/SAM isn't wired."""
    from flowboard.services import sam
    from flowboard.services.colorize import detect, reader
    from flowboard.services.image import seedream

    return {
        "engine_configured": seedream.is_configured(),
        "provider": seedream.provider(),
        "model": seedream.default_model(),
        "reader_configured": reader.is_configured(),
        "detect_configured": detect.is_configured(),
        "sam_available": sam.available(),
    }


@router.post("/upload-page")
async def upload_page(
    file: UploadFile = File(...),
    user: Optional[User] = Depends(get_optional_user),
):
    """Save one page/style image straight to the local media cache → media_id."""
    mime = (file.content_type or "").lower().split(";")[0].strip()
    if mime not in _ALLOWED_MIMES:
        raise HTTPException(415, f"unsupported mime: {mime!r}")
    raw = await file.read(_MAX_PAGE_BYTES + 1)
    if not raw:
        raise HTTPException(400, "empty file")
    if len(raw) > _MAX_PAGE_BYTES:
        raise HTTPException(413, f"file too large: {len(raw)} > {_MAX_PAGE_BYTES}")
    mid = str(uuid.uuid4())
    if not media_service.ingest_inline_bytes(mid, raw, kind="image", mime=mime):
        raise HTTPException(500, "ingest failed")
    return {"media_id": mid, "mime": mime, "size": len(raw)}


class _MediaBody(BaseModel):
    media_id: str


@router.post("/detect-panels")
async def detect_panels_route(
    body: _MediaBody,
    user: Optional[User] = Depends(get_optional_user),
):
    """Panel boxes [{box:[ymin,xmin,ymax,xmax] 0-1000}] in reading order."""
    from flowboard.services.colorize import detect

    if media_service.cached_path(body.media_id) is None:
        raise HTTPException(404, "media not found")
    panels = await detect.detect_panels(body.media_id)
    return {"panels": panels}


@router.post("/detect-objects")
async def detect_objects_route(
    body: _MediaBody,
    user: Optional[User] = Depends(get_optional_user),
):
    """Editable-part boxes [{id,label,box}] for the Fix picker."""
    from flowboard.services.colorize import detect

    if media_service.cached_path(body.media_id) is None:
        raise HTTPException(404, "media not found")
    segs = await detect.detect_objects(body.media_id)
    return {"segments": [{"id": i, "label": s["label"], "box": s["box"]} for i, s in enumerate(segs)]}


class _SamPoint(BaseModel):
    media_id: str
    x: float  # click X, normalized 0-1000
    y: float  # click Y, normalized 0-1000


@router.post("/sam-point")
async def sam_point_route(
    body: _SamPoint,
    user: Optional[User] = Depends(get_optional_user),
):
    """Click-to-segment: MobileSAM segments the object at the click → its box."""
    from flowboard.services import sam

    if not sam.available():
        raise HTTPException(503, "SAM models not available")
    p = media_service.cached_path(body.media_id)
    if p is None:
        raise HTTPException(404, "media not found")
    try:
        raw = p.read_bytes()
    except OSError:
        raise HTTPException(404, "media unreadable")
    box = await asyncio.to_thread(sam.segment_point_box, raw, body.x, body.y, media_id=body.media_id)
    if box is None:
        raise HTTPException(422, "nothing segmented at that point")
    return {"box": box}


class _ZipItem(BaseModel):
    name: str
    media_id: str


class _ZipBody(BaseModel):
    items: list[_ZipItem]
    name: Optional[str] = None


@router.post("/download-zip")
def download_zip(
    body: _ZipBody,
    user: Optional[User] = Depends(get_optional_user),
):
    """ZIP of the chosen colorized pages, numbered by reading order."""
    if not body.items:
        raise HTTPException(400, "no items")
    buf = io.BytesIO()
    n = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i, it in enumerate(body.items, start=1):
            p = media_service.cached_path(it.media_id)
            if p is None:
                continue
            try:
                data = p.read_bytes()
            except OSError:
                continue
            ext = p.suffix or ".png"
            safe = "".join(c if (c.isalnum() or c in "-_ .") else "_" for c in (it.name or "")).strip()
            z.writestr(f"{i:03d}_{safe}{ext}" if safe else f"{i:03d}{ext}", data)
            n += 1
    if n == 0:
        raise HTTPException(404, "no pages available")
    fname = "".join(c if (c.isalnum() or c in "-_ ") else "_" for c in (body.name or "chapter")).strip() or "chapter"
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}_colorized.zip"'},
    )
