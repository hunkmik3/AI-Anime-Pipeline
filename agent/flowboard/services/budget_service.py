"""Per-user $ budget metering (Phase 9.2).

Hard-cap flow: estimate a video gen's cost, ``reserve`` it against the user's
available budget (refuse when insufficient), then ``settle`` with the real Avis
``usdCost`` once the gen finishes (or ``release`` on failure). Available =
budget_usd − spent_usd − sum(outstanding reserved estimates).
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func
from sqlmodel import select

from flowboard.db import get_session
from flowboard.db.models import (
    Node,
    Project,
    Request,
    Scene,
    Shot,
    UsageRecord,
    User,
)

logger = logging.getLogger(__name__)


# USD-per-output-second by resolution, calibrated to observed Avis usdCost
# (15s/1080p ≈ $6.19 → ~$0.41/s; 5s/720p ≈ $0.83 → ~$0.17/s) with a ~2-3% pad.
# Kept close to actual so the budget reflects reality (a generous pad would
# block gens the user can actually afford). Settlement reconciles to the real
# usdCost afterwards. Env-tunable as Avis pricing changes.
_RATE_USD_PER_SEC = {
    "720p": float(os.getenv("FLOWBOARD_USD_PER_SEC_720P", "0.18")),
    "1080p": float(os.getenv("FLOWBOARD_USD_PER_SEC_1080P", "0.42")),
}
_DEFAULT_RATE = float(os.getenv("FLOWBOARD_USD_PER_SEC_1080P", "0.42"))


def estimate_video_usd(duration_seconds, resolution) -> float:
    dur = max(1, int(duration_seconds or 5))
    rate = _RATE_USD_PER_SEC.get(str(resolution or "").lower(), _DEFAULT_RATE)
    return round(dur * rate, 4)


def _uuid(value) -> Optional[uuid.UUID]:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _reserved_sum(session, uid: uuid.UUID) -> float:
    val = session.exec(
        select(func.coalesce(func.sum(UsageRecord.estimated_usd), 0.0)).where(
            UsageRecord.user_id == uid, UsageRecord.status == "reserved"
        )
    ).one()
    return float(val or 0.0)


def available_usd(user_id) -> float:
    uid = _uuid(user_id)
    if uid is None:
        return 0.0
    with get_session() as s:
        u = s.get(User, uid)
        if u is None:
            return 0.0
        return float(u.budget_usd) - float(u.spent_usd) - _reserved_sum(s, uid)


def summary(user_id) -> Optional[dict]:
    uid = _uuid(user_id)
    if uid is None:
        return None
    with get_session() as s:
        u = s.get(User, uid)
        if u is None:
            return None
        reserved = _reserved_sum(s, uid)
        return {
            "budget_usd": round(float(u.budget_usd), 4),
            "spent_usd": round(float(u.spent_usd), 4),
            "reserved_usd": round(reserved, 4),
            "available_usd": round(float(u.budget_usd) - float(u.spent_usd) - reserved, 4),
        }


def reserve(user_id, *, request_id: Optional[int], estimated_usd: float, model: Optional[str], kind: str = "video") -> bool:
    """Hold ``estimated_usd`` if the user can afford it. Returns False (no hold)
    when over budget."""
    uid = _uuid(user_id)
    if uid is None:
        return False
    with get_session() as s:
        u = s.get(User, uid)
        if u is None:
            return False
        avail = float(u.budget_usd) - float(u.spent_usd) - _reserved_sum(s, uid)
        if avail + 1e-9 < estimated_usd:
            return False
        s.add(
            UsageRecord(
                user_id=uid,
                request_id=request_id,
                kind=kind,
                model=model,
                estimated_usd=float(estimated_usd),
                status="reserved",
            )
        )
        s.commit()
        return True


def settle(request_id: int, actual_usd: float) -> None:
    """Convert a reservation to a settled charge with the real cost."""
    with get_session() as s:
        rec = s.exec(
            select(UsageRecord).where(
                UsageRecord.request_id == request_id, UsageRecord.status == "reserved"
            )
        ).first()
        if rec is None:
            return
        rec.status = "settled"
        rec.actual_usd = float(actual_usd or 0.0)
        rec.settled_at = _utcnow()
        u = s.get(User, rec.user_id)
        if u is not None:
            u.spent_usd = round(float(u.spent_usd) + float(actual_usd or 0.0), 6)
            s.add(u)
        s.add(rec)
        s.commit()


def release(request_id: int) -> None:
    """Drop a reservation without charging (gen failed/cancelled)."""
    with get_session() as s:
        rec = s.exec(
            select(UsageRecord).where(
                UsageRecord.request_id == request_id, UsageRecord.status == "reserved"
            )
        ).first()
        if rec is None:
            return
        rec.status = "released"
        rec.settled_at = _utcnow()
        s.add(rec)
        s.commit()


# Request types that count as a "generation" in the admin activity view.
_GEN_TYPES = ("gen_video", "gen_image", "gen_storyboard", "edit_image")

# Hard cap on how many requests we materialise per user (pathological safety).
_ACTIVITY_FETCH_CAP = 1000


# Params surfaced structurally elsewhere — excluded from the generic dump.
_PARAMS_STRUCTURED = {"prompt", "reference_images", "reference_labels"}


def _activity_item(req: Request, urec: Optional[UsageRecord]) -> dict:
    """Shape one generation row, merging the Request (output/params/status)
    with its budget ledger record (cost), when one exists. The expand view
    needs the *full* picture, so this returns the complete prompt, every input
    reference image, all remaining params, and the output media."""
    params = (req.params or {})
    result = (req.result or {})
    media_ids = [m for m in (result.get("media_ids") or []) if m]
    model = (urec.model if urec and urec.model else None) or params.get("model_id") or params.get("model")
    actual = float(urec.actual_usd) if (urec and urec.actual_usd is not None) else None
    est = float(urec.estimated_usd) if (urec and urec.estimated_usd is not None) else None
    cost = actual if actual is not None else est  # None => not metered (free / pre-budget)

    # Input reference images: media ids + their @labels (parallel arrays).
    ref_ids = params.get("reference_images") or []
    ref_labels = params.get("reference_labels") or []
    inputs = [
        {"id": str(rid), "label": (ref_labels[i] if i < len(ref_labels) else f"@image{i + 1}")}
        for i, rid in enumerate(ref_ids)
        if rid
    ]
    # Everything else in params, for a generic key/value dump in the detail row.
    extra = {k: v for k, v in params.items() if k not in _PARAMS_STRUCTURED}

    return {
        "request_id": req.id,
        "created_at": req.created_at,
        "finished_at": req.finished_at,
        "kind": (urec.kind if urec else None)
        or ("video" if req.type == "gen_video" else "image"),
        "model": model,
        "ledger_status": urec.status if urec else None,  # None = not metered
        "estimated_usd": round(est, 4) if est is not None else None,
        "actual_usd": round(actual, 4) if actual is not None else None,
        "cost_usd": round(cost, 4) if cost is not None else None,
        "request_type": req.type,
        "request_status": req.status,
        "error": req.error,
        "duration_seconds": params.get("duration_seconds"),
        "resolution": params.get("resolution"),
        "prompt": (str(params.get("prompt") or "") or None),  # full prompt, untruncated
        "inputs": inputs,
        "params": extra,
        "media_ids": media_ids,
        "video_url": result.get("videoUrl"),
    }


def user_activity(user_id, *, limit: int = 200) -> Optional[dict]:
    """Per-user generation history for the admin view.

    Attributes generations to the user via **project ownership** (Request →
    Node → Shot → Scene → Project.owner_user_id), not just the budget ledger —
    so historical / pre-budget gens show up too. The metered ledger
    (``UsageRecord``) is merged in for the real $ cost, and any metered request
    not reachable via the project join is still included. Newest first.

    Returns None when the user doesn't exist. Outputs are exposed as
    ``media_ids`` — the frontend streams each via ``GET /media/{id}``.
    """
    uid = _uuid(user_id)
    if uid is None:
        return None
    with get_session() as s:
        u = s.get(User, uid)
        if u is None:
            return None

        # Budget ledger → cost-per-request lookup.
        cost_by_rid: dict[int, UsageRecord] = {}
        for r in s.exec(select(UsageRecord).where(UsageRecord.user_id == uid)).all():
            if r.request_id is not None:
                cost_by_rid[r.request_id] = r

        # Generations in projects this user owns.
        owned = list(s.exec(select(Project.id).where(Project.owner_user_id == uid)).all())
        reqs: list[Request] = []
        seen: set[int] = set()
        if owned:
            rows = s.exec(
                select(Request)
                .join(Node, Node.id == Request.node_id)
                .join(Shot, Shot.id == Node.shot_id)
                .join(Scene, Scene.id == Shot.scene_id)
                .where(Scene.project_id.in_(owned), Request.type.in_(_GEN_TYPES))
                .order_by(Request.created_at.desc())
                .limit(_ACTIVITY_FETCH_CAP)
            ).all()
            for req in rows:
                if req.id is not None and req.id not in seen:
                    reqs.append(req)
                    seen.add(req.id)

        # Safety net: metered requests not reachable via the project join
        # (e.g. node/shot deleted) must still appear.
        missing = [rid for rid in cost_by_rid if rid not in seen]
        if missing:
            for req in s.exec(select(Request).where(Request.id.in_(missing))).all():
                if req.id is not None and req.id not in seen:
                    reqs.append(req)
                    seen.add(req.id)

        reqs.sort(key=lambda r: r.created_at, reverse=True)
        total = len(reqs)
        page = reqs[: max(1, int(limit))]
        items = [_activity_item(req, cost_by_rid.get(req.id)) for req in page]

        reserved = _reserved_sum(s, uid)
        summary = {
            "budget_usd": round(float(u.budget_usd), 4),
            "spent_usd": round(float(u.spent_usd), 4),
            "reserved_usd": round(reserved, 4),
            "available_usd": round(float(u.budget_usd) - float(u.spent_usd) - reserved, 4),
            "gen_count": total,
            "shown": len(items),
        }
        return {"summary": summary, "items": items}


def has_reservation(request_id: int) -> bool:
    with get_session() as s:
        return (
            s.exec(
                select(UsageRecord.id).where(
                    UsageRecord.request_id == request_id, UsageRecord.status == "reserved"
                )
            ).first()
            is not None
        )
