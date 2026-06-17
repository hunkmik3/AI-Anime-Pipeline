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
from flowboard.db.models import UsageRecord, User

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
