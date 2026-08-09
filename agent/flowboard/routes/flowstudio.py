"""Flow Studio (`/giantflow`) — its own board list, image upload and usage meter.

Brought over from the manga_extract repo as a standalone surface. It is
deliberately NOT wired into Project → Series → Episode, per-project RBAC, or the
credit budgets yet — that integration is planned separately (see
``docs/INTEGRATION_PLAN.md``).

**A board belongs to whoever made it.** The studio arrived as one shared list
with no owner column, so there was nothing to authorise against and any
signed-in account could rename or delete anyone's board. That held while it was
one team sharing one key; it stopped holding the moment two branches with
different people needed it.

You see your own boards. An admin sees everything, including the ownerless rows
that predate the column — NULL means "we do not know whose this is", which is a
reason to show fewer people, not more.

Someone else's board answers **404, not 403**. 403 admits the row exists, which
is enough to enumerate what colleagues are working on; a board you may not see
should be indistinguishable from one that was never created.

Still shared, and stated plainly so it is not a surprise: **one API key, one
usage meter.** ``/usage`` counts what this install generated, not what you
generated — ``Request`` records no user, so past rows cannot be attributed at
all. Generation is not budget-capped here.

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
from sqlmodel import select, update as sql_update

from flowboard.db import get_session
from flowboard.db.models import FlowBoard, Reference, Request
from flowboard.routes.deps import get_optional_user
from flowboard.services import media as media_service
from flowboard.services import flow_quota
from flowboard.services import resource_guard

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/flowstudio", tags=["flow-studio"])


# ── Boards (the studio's own project list) ───────────────────────────────────


class BoardCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class BoardUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


def _is_admin(user) -> bool:
    """No auth configured (dev, and the legacy test suite) behaves as before —
    one open studio — rather than locking everyone out of their own boards."""
    return user is None or getattr(user, "role", None) == "admin"


def _own_board(session, user, board_id: int) -> FlowBoard:
    """This caller's board, or 404.

    404 rather than 403 on purpose: the two are the same to someone who owns the
    board, and different only to someone who does not — and what 403 tells that
    person is that the board exists.
    """
    row = session.get(FlowBoard, board_id)
    if row is None:
        raise HTTPException(404, "board not found")
    if not _is_admin(user) and row.owner_user_id != user.id:
        raise HTTPException(404, "board not found")
    return row


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
        q = select(FlowBoard).order_by(FlowBoard.created_at)
        if not _is_admin(user):
            q = q.where(FlowBoard.owner_user_id == user.id)
        return [_board_dict(r) for r in s.exec(q).all()]


@router.post("/boards")
def create_board(body: BoardCreate, user=Depends(get_optional_user)):
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        row = FlowBoard(
            name=body.name.strip(),
            owner_user_id=getattr(user, "id", None),
        )
        s.add(row)
        s.commit()
        s.refresh(row)
        return _board_dict(row)


@router.patch("/boards/{board_id}")
def update_board(board_id: int, body: BoardUpdate, user=Depends(get_optional_user)):
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        row = _own_board(s, user, board_id)
        row.name = body.name.strip()
        s.add(row)
        s.commit()
        s.refresh(row)
        return _board_dict(row)


@router.delete("/boards/{board_id}")
def delete_board(board_id: int, user=Depends(get_optional_user)):
    """Delete a studio board, DETACHING its images rather than deleting them.

    The references are the user's library — they outlive the board that produced
    them; only the ``source_board_id`` provenance is cleared. Deleting them (an
    earlier version of this route did) threw away catalogue entries for images the
    user still had, which is not what "delete this project" should mean. Detached
    rows do not leak into other boards either: every studio listing filters on
    ``source_board_id``, so a NULL row appears only in the unscoped library.

    Clearing the column is also what lets the board row go at all — the FK would
    otherwise block the delete and surface as a 500 on the button.
    """
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        row = _own_board(s, user, board_id)
        n = len(s.exec(select(Reference).where(Reference.source_board_id == board_id)).all())
        if n:
            s.exec(
                sql_update(Reference)
                .where(Reference.source_board_id == board_id)
                .values(source_board_id=None)
            )
        s.delete(row)
        s.commit()
        logger.info("flowstudio: deleted board %s, detached %d reference(s)", board_id, n)
        return {"deleted": board_id, "references_detached": n}


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
#   Seedream (Avis) — pay per image.
#
# The Seedream rates are MEASURED, not from a price list: a credit-balance diff
# around a real generation, cross-checked against nine per-generation entries in
# Avis's own VND cost history on 2026-07-30. Every one landed on exactly one of
# two values — 1K $0.059125, 2K $0.11825, exactly 2x. One of those nine was a 2K
# job with no reference image, which is how we know **reference images are free**
# and price tracks output resolution only. The earlier per-input charge modelled
# here was wrong.
# The cap and the tariff live in `services/flow_quota` now, so the meter below
# and the two generation paths that ENFORCE it cannot disagree about the number.
# They did before: this file held the only copy, and nothing outside it could
# see the figure, which is how a cap stayed a display for as long as it did.
DAILY_QUOTA = flow_quota.DAILY_QUOTA
SEEDREAM_USD_PER_IMAGE_1K = flow_quota.SEEDREAM_USD_PER_IMAGE_1K
SEEDREAM_USD_PER_IMAGE_2K = flow_quota.SEEDREAM_USD_PER_IMAGE_2K


def _images_in(x: object):
    return flow_quota.images_in(x)



def _resolution_of(x: object):
    return flow_quota.resolution_of(x)



def _engine_of(x: object):
    return flow_quota.engine_of(x)



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
        eng = _engine_of(r.params)
        if eng is None:
            continue  # decommissioned "ark" provider — not counted anywhere
        imgs = _images_in(r.result)
        is_today = bool(r.created_at and _aware(r.created_at) >= start)
        total[eng] += imgs
        if is_today:
            today[eng] += imgs
        if eng == "seedream" and imgs:
            per_image = (
                SEEDREAM_USD_PER_IMAGE_2K
                if _resolution_of(r.params) == "2K"
                else SEEDREAM_USD_PER_IMAGE_1K
            )
            cost = imgs * per_image
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
                # The blended average over everything generated so far (the 1K/2K
                # mix), not a flat rate — a single number would misreport either
                # tier.
                "usd_per_image": round(
                    sd_cost["total"] / total["seedream"]
                    if total["seedream"]
                    else SEEDREAM_USD_PER_IMAGE_1K,
                    6,
                ),
                "cost_today": round(sd_cost["today"], 4),
                "cost_total": round(sd_cost["total"], 4),
            },
        },
    }
