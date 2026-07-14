"""Phase 3 — security audit trail.

Append-only log of logins, SSO, and admin actions. ``record`` never raises:
auditing must never break the operation it's recording. Actor/target are
stored as denormalized labels so entries stay readable after account deletion.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from sqlalchemy import desc
from sqlmodel import select

from flowboard.db import get_session
from flowboard.db.models import AuditLog, User

logger = logging.getLogger(__name__)


def _resolve(obj, label: Optional[str]):
    """(user_id, label) from a User, a uuid/str id, or None."""
    if obj is None:
        return None, label
    if isinstance(obj, User):
        return obj.id, (label or obj.display_name or obj.username)
    try:
        return uuid.UUID(str(obj)), label
    except (ValueError, TypeError):
        return None, label


def client_ip(request) -> Optional[str]:
    """Best-effort client IP, honoring the Cloudflare tunnel / proxy headers."""
    if request is None:
        return None
    h = request.headers
    cf = h.get("cf-connecting-ip")
    if cf:
        return cf.strip()
    xff = h.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else None


def record(
    action: str,
    *,
    actor=None,
    target=None,
    actor_label: Optional[str] = None,
    target_label: Optional[str] = None,
    ip: Optional[str] = None,
    detail: Optional[str] = None,
) -> None:
    try:
        a_id, a_label = _resolve(actor, actor_label)
        t_id, t_label = _resolve(target, target_label)
        with get_session() as s:
            s.add(
                AuditLog(
                    action=action,
                    actor_user_id=a_id,
                    actor_label=a_label,
                    target_user_id=t_id,
                    target_label=t_label,
                    ip=ip,
                    detail=(detail[:500] if isinstance(detail, str) else detail),
                )
            )
            s.commit()
    except Exception:  # noqa: BLE001 — auditing must never break the caller
        logger.exception("audit record failed for %s", action)


def _public(r: AuditLog) -> dict:
    return {
        "id": r.id,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "action": r.action,
        "actor": r.actor_label,
        "actor_id": str(r.actor_user_id) if r.actor_user_id else None,
        "target": r.target_label,
        "target_id": str(r.target_user_id) if r.target_user_id else None,
        "ip": r.ip,
        "detail": r.detail,
    }


def list_recent(limit: int = 200, action: Optional[str] = None) -> list[dict]:
    with get_session() as s:
        q = select(AuditLog)
        if action:
            q = q.where(AuditLog.action == action)
        q = q.order_by(desc(AuditLog.created_at)).limit(limit)
        return [_public(r) for r in s.exec(q).all()]
