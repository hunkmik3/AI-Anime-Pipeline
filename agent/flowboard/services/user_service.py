"""App account CRUD + admin bootstrap (Phase 9 multi-user).

Accounts are admin-provisioned (no open signup). Passwords are hashed via
``services.auth``. Read helpers return detached ORM objects (attributes are
loaded during the query, safe to read after the session closes).
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func
from sqlmodel import select

from flowboard.db import get_session
from flowboard.db.models import Project, UsageRecord, User
from flowboard.services import auth

logger = logging.getLogger(__name__)

# Login brute-force lockout (Phase 0). Env-tunable.
_LOGIN_MAX_FAILED = int(os.getenv("FLOWBOARD_LOGIN_MAX_FAILED", "5"))
_LOGIN_LOCKOUT_MIN = int(os.getenv("FLOWBOARD_LOGIN_LOCKOUT_MIN", "15"))
_MIN_PASSWORD_LEN = int(os.getenv("FLOWBOARD_MIN_PASSWORD_LEN", "8"))


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


def _check_password_policy(password: str) -> None:
    """Minimum password strength (length). Tunable via FLOWBOARD_MIN_PASSWORD_LEN."""
    if not password or len(password) < _MIN_PASSWORD_LEN:
        raise UserError(f"password must be at least {_MIN_PASSWORD_LEN} characters")


def create_user(
    username: str,
    password: str,
    *,
    role: str = "user",
    display_name: Optional[str] = None,
    email: Optional[str] = None,
    must_change_password: bool = False,
) -> User:
    username = (username or "").strip()
    if not username:
        raise UserError("username required")
    _check_password_policy(password)
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
            email=(email or None),
            must_change_password=bool(must_change_password),
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
        if status == "suspended":
            # Revoke any outstanding token immediately (offboarding).
            u.token_version = int(u.token_version or 0) + 1
        else:  # re-activated → clear any lockout so they can log back in
            u.failed_attempts = 0
            u.locked_until = None
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
    """Admin password reset. Sets a temp password the user must change on next
    login (must_change_password) and revokes their existing sessions."""
    _check_password_policy(password)
    uid = _coerce_uuid(user_id)
    with get_session() as s:
        u = s.get(User, uid) if uid else None
        if u is None:
            raise UserNotFound(str(user_id))
        u.password_hash = auth.hash_password(password)
        u.must_change_password = True
        # A password reset revokes every existing session (force re-login).
        u.token_version = int(u.token_version or 0) + 1
        s.add(u)
        s.commit()


def set_role(user_id, role: str) -> User:
    """Change a user's role. Guards against demoting the last remaining admin."""
    if role not in ("admin", "user"):
        raise UserError(f"bad role: {role!r}")
    uid = _coerce_uuid(user_id)
    with get_session() as s:
        u = s.get(User, uid) if uid else None
        if u is None:
            raise UserNotFound(str(user_id))
        if u.role == "admin" and role != "admin":
            admins = int(
                s.exec(select(func.count()).select_from(User).where(User.role == "admin")).one()
            )
            if admins <= 1:
                raise UserError("cannot demote the last admin")
        u.role = role
        s.add(u)
        s.commit()
        s.refresh(u)
        return u


def set_email(user_id, email: Optional[str]) -> None:
    uid = _coerce_uuid(user_id)
    with get_session() as s:
        u = s.get(User, uid) if uid else None
        if u is None:
            raise UserNotFound(str(user_id))
        u.email = (email or None)
        s.add(u)
        s.commit()


def set_must_change_password(user_id, value: bool) -> None:
    uid = _coerce_uuid(user_id)
    with get_session() as s:
        u = s.get(User, uid) if uid else None
        if u is None:
            raise UserNotFound(str(user_id))
        u.must_change_password = bool(value)
        s.add(u)
        s.commit()


def change_own_password(user_id, current_password: str, new_password: str) -> None:
    """Self-service password change: verify current password, enforce policy on
    the new one, clear must_change_password, and bump token_version (logs out
    other sessions — the caller issues a fresh token for the current one)."""
    _check_password_policy(new_password)
    uid = _coerce_uuid(user_id)
    with get_session() as s:
        u = s.get(User, uid) if uid else None
        if u is None:
            raise UserNotFound(str(user_id))
        if not auth.verify_password(current_password, u.password_hash):
            raise UserError("current password is incorrect")
        u.password_hash = auth.hash_password(new_password)
        u.must_change_password = False
        u.token_version = int(u.token_version or 0) + 1
        s.add(u)
        s.commit()


def authenticate_token(token: str) -> Optional[User]:
    """Resolve a bearer token to an ACTIVE account, enforcing token_version.

    Returns None if the token is invalid/expired/tampered, the account is
    missing or suspended, or the token's ``tv`` is stale (revoked). This is the
    single source of truth used by both the global auth middleware and the
    per-route dependencies, so suspend/delete/password-change take effect on
    the very next request across every route."""
    data = auth.decode_token(token)
    if not data:
        return None
    user = get_by_id(data["uid"])
    if user is None or user.status != "active":
        return None
    if int(data.get("tv", 0)) != int(user.token_version or 0):
        return None
    return user


def bump_token_version(user_id) -> None:
    """Invalidate all of a user's outstanding tokens ("log out everywhere")."""
    uid = _coerce_uuid(user_id)
    with get_session() as s:
        u = s.get(User, uid) if uid else None
        if u is None:
            raise UserNotFound(str(user_id))
        u.token_version = int(u.token_version or 0) + 1
        s.add(u)
        s.commit()


def is_locked(user: User) -> bool:
    """True if the account is currently in a brute-force lockout window."""
    lu = getattr(user, "locked_until", None)
    if lu is None:
        return False
    if lu.tzinfo is None:  # SQLite may return naive datetimes
        lu = lu.replace(tzinfo=timezone.utc)
    return lu > datetime.now(timezone.utc)


def register_failed_login(user_id) -> None:
    """Count a failed password attempt; lock the account past the threshold."""
    uid = _coerce_uuid(user_id)
    with get_session() as s:
        u = s.get(User, uid) if uid else None
        if u is None:
            return
        u.failed_attempts = int(u.failed_attempts or 0) + 1
        if u.failed_attempts >= _LOGIN_MAX_FAILED:
            u.locked_until = datetime.now(timezone.utc) + timedelta(minutes=_LOGIN_LOCKOUT_MIN)
            u.failed_attempts = 0  # reset the counter; the lock is the penalty
        s.add(u)
        s.commit()


def register_successful_login(user_id, *, rehash_password: Optional[str] = None) -> None:
    """Clear lockout counters, stamp last_login, and transparently upgrade a
    legacy/low-cost password hash when ``rehash_password`` (the just-verified
    plaintext) is supplied."""
    uid = _coerce_uuid(user_id)
    with get_session() as s:
        u = s.get(User, uid) if uid else None
        if u is None:
            return
        u.failed_attempts = 0
        u.locked_until = None
        u.last_login = datetime.now(timezone.utc)
        if rehash_password is not None and auth.needs_rehash(u.password_hash):
            u.password_hash = auth.hash_password(rehash_password)
        s.add(u)
        s.commit()


def get_or_create_sso_user(email: str, display_name: Optional[str] = None) -> User:
    """Find (by email, then username) or auto-provision an account for a
    verified SSO identity. SSO users have no usable password (a sentinel hash
    that never verifies) — they sign in via Google only, unless an admin later
    sets a password. Suspended accounts are refused."""
    email = (email or "").strip().lower()
    if not email:
        raise UserError("email required")
    with get_session() as s:
        u = s.exec(select(User).where(User.email == email)).first()
        if u is None:
            u = s.exec(select(User).where(User.username == email)).first()
        if u is not None:
            if u.status != "active":
                raise UserError("account suspended")
            u.email = email
            if display_name and not u.display_name:
                u.display_name = display_name
            u.last_login = datetime.now(timezone.utc)
            s.add(u)
            s.commit()
            s.refresh(u)
            return u
        u = User(
            username=email,
            email=email,
            password_hash="!sso",  # sentinel — never matches verify_password
            role="user",
            status="active",
            display_name=(display_name or None),
            last_login=datetime.now(timezone.utc),
        )
        s.add(u)
        s.commit()
        s.refresh(u)
        return u


def count_admins() -> int:
    with get_session() as s:
        return int(
            s.exec(
                select(func.count()).select_from(User).where(User.role == "admin")
            ).one()
        )


def delete_user(user_id) -> None:
    """Delete an account. Non-destructive to their content: owned projects are
    orphaned (owner_user_id → NULL), not deleted, so generations survive. The
    user's budget ledger (usage_record, FK to app_user) is removed."""
    uid = _coerce_uuid(user_id)
    with get_session() as s:
        u = s.get(User, uid) if uid else None
        if u is None:
            raise UserNotFound(str(user_id))
        for p in s.exec(select(Project).where(Project.owner_user_id == uid)).all():
            p.owner_user_id = None
            s.add(p)
        for rec in s.exec(select(UsageRecord).where(UsageRecord.user_id == uid)).all():
            s.delete(rec)
        s.delete(u)
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
    try:
        u = create_user(username, password, role="admin", display_name="Admin")
    except UserError as exc:
        logger.warning("could not bootstrap admin (%s) — fix FLOWBOARD_ADMIN_PASSWORD", exc)
        return
    claimed = claim_orphan_projects(u.id)
    logger.info("bootstrapped admin account %r (claimed %d existing project(s))", username, claimed)


def set_budget(user_id, budget_usd: float) -> None:
    uid = _coerce_uuid(user_id)
    with get_session() as s:
        u = s.get(User, uid) if uid else None
        if u is None:
            raise UserNotFound(str(user_id))
        u.budget_usd = max(0.0, float(budget_usd))
        s.add(u)
        s.commit()


def add_budget(user_id, delta_usd: float) -> None:
    uid = _coerce_uuid(user_id)
    with get_session() as s:
        u = s.get(User, uid) if uid else None
        if u is None:
            raise UserNotFound(str(user_id))
        u.budget_usd = max(0.0, round(float(u.budget_usd) + float(delta_usd), 6))
        s.add(u)
        s.commit()


def public_dict(u: User) -> dict:
    return {
        "id": str(u.id),
        "username": u.username,
        "role": u.role,
        "status": u.status,
        "display_name": u.display_name,
        "budget_usd": round(float(u.budget_usd), 4),
        "spent_usd": round(float(u.spent_usd), 4),
        "created_at": u.created_at.isoformat() if u.created_at else None,
        "last_login": u.last_login.isoformat() if getattr(u, "last_login", None) else None,
        "email": getattr(u, "email", None),
        "must_change_password": bool(getattr(u, "must_change_password", False)),
    }
