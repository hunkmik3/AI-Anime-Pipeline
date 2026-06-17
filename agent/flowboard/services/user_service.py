"""App account CRUD + admin bootstrap (Phase 9 multi-user).

Accounts are admin-provisioned (no open signup). Passwords are hashed via
``services.auth``. Read helpers return detached ORM objects (attributes are
loaded during the query, safe to read after the session closes).
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Optional

from sqlalchemy import func
from sqlmodel import select

from flowboard.db import get_session
from flowboard.db.models import Project, User
from flowboard.services import auth

logger = logging.getLogger(__name__)


class UserError(RuntimeError):
    pass


class UsernameTaken(UserError):
    pass


class UserNotFound(UserError):
    pass


def _coerce_uuid(value) -> Optional[uuid.UUID]:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


def get_by_username(username: str) -> Optional[User]:
    with get_session() as s:
        return s.exec(select(User).where(User.username == username)).first()


def get_by_id(user_id) -> Optional[User]:
    uid = _coerce_uuid(user_id)
    if uid is None:
        return None
    with get_session() as s:
        return s.get(User, uid)


def list_users() -> list[User]:
    with get_session() as s:
        return list(s.exec(select(User).order_by(User.created_at)).all())


def count_users() -> int:
    with get_session() as s:
        return int(s.exec(select(func.count()).select_from(User)).one())


def create_user(
    username: str,
    password: str,
    *,
    role: str = "user",
    display_name: Optional[str] = None,
) -> User:
    username = (username or "").strip()
    if not username:
        raise UserError("username required")
    if not password:
        raise UserError("password required")
    if role not in ("admin", "user"):
        raise UserError(f"bad role: {role!r}")
    with get_session() as s:
        if s.exec(select(User).where(User.username == username)).first():
            raise UsernameTaken(username)
        u = User(
            username=username,
            password_hash=auth.hash_password(password),
            role=role,
            display_name=(display_name or None),
        )
        s.add(u)
        s.commit()
        s.refresh(u)
        return u


def set_status(user_id, status: str) -> User:
    if status not in ("active", "suspended"):
        raise UserError(f"bad status: {status!r}")
    uid = _coerce_uuid(user_id)
    with get_session() as s:
        u = s.get(User, uid) if uid else None
        if u is None:
            raise UserNotFound(str(user_id))
        u.status = status
        s.add(u)
        s.commit()
        s.refresh(u)
        return u


def set_display_name(user_id, display_name: Optional[str]) -> None:
    uid = _coerce_uuid(user_id)
    with get_session() as s:
        u = s.get(User, uid) if uid else None
        if u is None:
            raise UserNotFound(str(user_id))
        u.display_name = (display_name or None)
        s.add(u)
        s.commit()


def set_password(user_id, password: str) -> None:
    if not password:
        raise UserError("password required")
    uid = _coerce_uuid(user_id)
    with get_session() as s:
        u = s.get(User, uid) if uid else None
        if u is None:
            raise UserNotFound(str(user_id))
        u.password_hash = auth.hash_password(password)
        s.add(u)
        s.commit()


def claim_orphan_projects(owner_user_id) -> int:
    """Assign every owner-less project to this user. Used on first-admin
    bootstrap so an existing single-user DB's projects aren't orphaned."""
    uid = _coerce_uuid(owner_user_id)
    if uid is None:
        return 0
    with get_session() as s:
        rows = list(s.exec(select(Project).where(Project.owner_user_id.is_(None))).all())
        for p in rows:
            p.owner_user_id = uid
            s.add(p)
        s.commit()
        return len(rows)


def ensure_bootstrap_admin() -> None:
    """Create the first admin from FLOWBOARD_ADMIN_USER/PASSWORD when the
    accounts table is empty. No-op once any user exists. Existing owner-less
    projects are claimed by the new admin so they don't vanish."""
    if count_users() > 0:
        return
    username = os.getenv("FLOWBOARD_ADMIN_USER")
    password = os.getenv("FLOWBOARD_ADMIN_PASSWORD")
    if not username or not password:
        logger.warning(
            "no accounts yet and FLOWBOARD_ADMIN_USER/PASSWORD unset — "
            "set them to bootstrap the first admin"
        )
        return
    u = create_user(username, password, role="admin", display_name="Admin")
    claimed = claim_orphan_projects(u.id)
    logger.info("bootstrapped admin account %r (claimed %d existing project(s))", username, claimed)


def public_dict(u: User) -> dict:
    return {
        "id": str(u.id),
        "username": u.username,
        "role": u.role,
        "status": u.status,
        "display_name": u.display_name,
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }
