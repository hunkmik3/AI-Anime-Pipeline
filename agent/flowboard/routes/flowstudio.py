"""Flow Studio (`/giantflow`) — its own board list, image upload and usage meter.

Brought over from the manga_extract repo as a standalone surface. It is
deliberately NOT wired into Project → Series → Episode, per-project RBAC, or the
credit budgets yet — that integration is planned separately (see
``docs/INTEGRATION_PLAN.md``).

**Everything here is open to any signed-in user**, via
``resource_guard.require_signed_in`` — the studio is a shared space by decision.
There is no owner to authorize against: one board list the whole team works in,
one API key the whole team spends.

The cost of that, stated plainly so it is not a surprise: **any signed-in user can
rename or delete any board, and deleting a board deletes its images.** There is no
per-user or per-project separation inside the studio, and generation is not
budget-capped. Scoping it to a project is what fixes both (see
``docs/INTEGRATION_PLAN.md``).

Three groups of endpoints, replacing three things from the source repo:

- ``/api/flowstudio/boards`` — was ``/api/boards`` over the shared ``Board``
  table, which also parented the comic node canvas. Here it is a small table of
  its own (``flow_board``); the studio's images hang off it via
  ``reference.source_board_id``.
- ``/api/flowstudio/upload`` — was ``/api/comic/upload-sheet``. Same job (cache
  bytes, return a media id), without dragging the comic router along.
- ``/api/flowstudio/usage`` — was ``/api/flow/usage``. Counts what the studio has
  generated and prices the pay-per-image engine.
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlmodel import delete as sql_delete, select

from flowboard.db import get_session
from flowboard.db.models import FlowBoard, Reference, Request
from flowboard.routes.deps import get_optional_user
from flowboard.services import media as media_service
from flowboard.services import resource_guard

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/flowstudio", tags=["flow-studio"])


# ── Boards (the studio's own project list) ───────────────────────────────────


class BoardCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class BoardUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


def _board_dict(row: FlowBoard) -> dict:
    # ``kind`` is echoed for wire-compatibility with the studio's frontend, which
    # came from a repo where one Board table served two surfaces. Here there is
    # only one, so it is a constant rather than a column.
    return {
        "id": row.id,
        "name": row.name,
        "kind": "flow",
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("/boards")
def list_boards(user=Depends(get_optional_user)):
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        rows = s.exec(select(FlowBoard).order_by(FlowBoard.created_at)).all()
        return [_board_dict(r) for r in rows]


@router.post("/boards")
def create_board(body: BoardCreate, user=Depends(get_optional_user)):
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        row = FlowBoard(name=body.name.strip())
        s.add(row)
        s.commit()
        s.refresh(row)
        return _board_dict(row)


@router.patch("/boards/{board_id}")
def update_board(board_id: int, body: BoardUpdate, user=Depends(get_optional_user)):
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        row = s.get(FlowBoard, board_id)
        if row is None:
            raise HTTPException(404, "board not found")
        row.name = body.name.strip()
        s.add(row)
        s.commit()
        s.refresh(row)
        return _board_dict(row)


@router.delete("/boards/{board_id}")
def delete_board(board_id: int, user=Depends(get_optional_user)):
    """Delete a studio board and the image records filed under it.

    The References ARE the board's contents — leaving them behind would drop them
    into the unscoped library with no board and no project, i.e. visible in every
    other board's grid. The cached media files are NOT deleted, so a mistaken
    delete loses the catalogue entry, not the pixels.
    """
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        row = s.get(FlowBoard, board_id)
        if row is None:
            raise HTTPException(404, "board not found")
        refs = s.exec(
            select(Reference).where(Reference.source_board_id == board_id)
        ).all()
        n = len(refs)
        if n:
            s.exec(sql_delete(Reference).where(Reference.source_board_id == board_id))
        s.delete(row)
        s.commit()
        logger.info("flowstudio: deleted board %s and %d reference(s)", board_id, n)
        return {"deleted": board_id, "references_deleted": n}


# ── Upload ───────────────────────────────────────────────────────────────────

_ALLOWED_IMAGE_MIME = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/bmp",
}
#: Refuse absurd uploads outright. 4K PNGs from the studio run ~10-20 MB, so 40
#: leaves headroom without letting a stray video through.
_MAX_UPLOAD_BYTES = 40 * 1024 * 1024


@router.post("/upload")
async def upload_image(
    file: UploadFile = File(...),
    user=Depends(get_optional_user),
):
    """Cache an image locally and return its media id.

    Used for reference/source images dropped into the studio. Local-only: the
    engines read the bytes off disk (Gemini and Ark inline them; Atrium uploads to
    R2 at dispatch time), so nothing needs to be pushed anywhere on upload.
    """
    with get_session() as s:
        resource_guard.require_signed_in(s, user)

    raw = await file.read()
    if not raw:
        raise HTTPException(400, "empty file")
    if len(raw) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            413, f"file too large ({len(raw) // 1024 // 1024} MB); limit is 40 MB"
        )
    mime = (file.content_type or "").lower().split(";")[0].strip()
    if mime not in _ALLOWED_IMAGE_MIME:
        raise HTTPException(
            400, f"unsupported image type {mime or 'unknown'} — use PNG, JPEG, WebP or BMP"
        )

    mid = str(uuid.uuid4())
    if not media_service.ingest_inline_bytes(mid, raw, kind="image", mime=mime):
        raise HTTPException(500, "failed to cache image")
    return {"media_id": mid, "mime": mime, "size": len(raw)}


# ── Usage meter ──────────────────────────────────────────────────────────────
#
# Two engine families, priced differently, so one number would be a lie:
#   Gemini / Atrium — daily QUOTA, no per-image charge.
#   Seedream (Ark)  — pay per image: output billed in full, reference images
#                     billed after the first (which is free).
# Rates are env-overridable because they follow the account tier.

DAILY_QUOTA = int(os.getenv("FLOWBOARD_DAILY_QUOTA", "1000"))
SEEDREAM_USD_PER_IMAGE = float(os.getenv("FLOWBOARD_SEEDREAM_USD_PER_IMAGE", "0.045"))
SEEDREAM_USD_PER_INPUT = float(os.getenv("FLOWBOARD_SEEDREAM_USD_PER_INPUT", "0.003"))


def _images_in(result: object) -> int:
    if not isinstance(result, dict):
        return 0
    mids = result.get("media_ids")
    if not isinstance(mids, list):
        return 0
    return sum(1 for m in mids if isinstance(m, str) and m)


def _input_count(params: object) -> int:
    """Input images sent with a gen (source + refs) — the billable-input basis."""
    if not isinstance(params, dict):
        return 0
    n = 1 if params.get("source_media_id") else 0
    refs = params.get("ref_media_ids")
    if isinstance(refs, list):
        n += sum(1 for r in refs if isinstance(r, str) and r)
    return n


def _engine_of(params: object) -> str:
    """Bucket a request by provider: ark/avis → seedream, everything else → gemini."""
    p = ""
    if isinstance(params, dict):
        p = str(params.get("provider") or "").lower()
    return "seedream" if p in ("avis", "ark") else "gemini"


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@router.get("/usage")
def flow_usage(user=Depends(get_optional_user)) -> dict:
    """Today's and lifetime image counts, plus Seedream dollars.

    The quota figure is an *estimate*: the upstream gateway does not expose its
    own counter, so this counts what this install generated since local midnight.
    That equals the real usage only while this install is the sole consumer of
    the key — which is why it is labelled an estimate in the UI.
    """
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        rows = s.exec(
            select(Request).where(
                Request.type == "flow_gen_image", Request.status == "done"
            )
        ).all()

    now = datetime.now().astimezone()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    resets_at = start + timedelta(days=1)

    today = {"gemini": 0, "seedream": 0}
    total = {"gemini": 0, "seedream": 0}
    sd_cost = {"today": 0.0, "total": 0.0}

    for r in rows:
        imgs = _images_in(r.result)
        eng = _engine_of(r.params)
        is_today = bool(r.created_at and _aware(r.created_at) >= start)
        total[eng] += imgs
        if is_today:
            today[eng] += imgs
        if eng == "seedream" and imgs:
            billable_inputs = max(0, _input_count(r.params) - 1)
            cost = (
                imgs * SEEDREAM_USD_PER_IMAGE
                + billable_inputs * SEEDREAM_USD_PER_INPUT
            )
            sd_cost["total"] += cost
            if is_today:
                sd_cost["today"] += cost

    return {
        "today": today["gemini"] + today["seedream"],
        "total": total["gemini"] + total["seedream"],
        "daily_quota": DAILY_QUOTA,
        "remaining_est": max(0, DAILY_QUOTA - today["gemini"]),
        "resets_at": resets_at.isoformat(),
        "seconds_until_reset": max(0, int((resets_at - now).total_seconds())),
        "engines": {
            "gemini": {
                "today": today["gemini"],
                "total": total["gemini"],
                "daily_quota": DAILY_QUOTA,
                "remaining_est": max(0, DAILY_QUOTA - today["gemini"]),
            },
            "seedream": {
                "today": today["seedream"],
                "total": total["seedream"],
                "usd_per_image": round(SEEDREAM_USD_PER_IMAGE, 6),
                "cost_today": round(sd_cost["today"], 4),
                "cost_total": round(sd_cost["total"], 4),
            },
        },
    }
