"""Self-service signup → admin approval → provisioned account.

Flow:
  1. Anyone submits ``request_access(email, name)`` from the login page. That
     writes a ``Registration`` row with status ``pending`` — NOT a User, so a
     request carries no password and reserves no username.
  2. An admin sees the queue and approves or rejects it.
  3. ``approve()`` mints a real User with a random temporary password and
     ``must_change_password=True``, then emails the credentials. The password
     is also returned so the admin can relay it if the mail bounced.

Signup is open to any email address (admin approval is the only gate), so the
queue is protected against spam: one *pending* row per email, and the public
endpoint always answers identically whether or not the email already exists
(no account enumeration).
"""
from __future__ import annotations

import logging
import re
import secrets
import string
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import select

from flowboard.db import get_session
from flowboard.db.models import Registration, User
from flowboard.services import email_service, user_service

logger = logging.getLogger(__name__)

# Deliberately permissive: real validation is "the approval email arrives".
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_NAME_MAX = 80
_NOTE_MAX = 500


class RegistrationError(RuntimeError):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match((email or "").strip()))


def _temp_password(length: int = 14) -> str:
    """Random temp password. Excludes lookalike glyphs (O/0, l/1, I) because
    this gets read out of an email and retyped by a human."""
    alphabet = (
        "".join(c for c in string.ascii_letters if c not in "lIO")
        + "".join(c for c in string.digits if c not in "01")
        + "!@#$%*?"
    )
    return "".join(secrets.choice(alphabet) for _ in range(length))


# ── public: submit a request ─────────────────────────────────────────────────


def request_access(
    email: str, display_name: Optional[str] = None, note: Optional[str] = None
) -> None:
    """Record a pending signup. Idempotent and silent by design: callers get
    the same answer whether this created a row, hit an existing pending one, or
    matched an existing account — so the endpoint can't be used to probe which
    emails are registered."""
    email = (email or "").strip().lower()
    if not valid_email(email):
        raise RegistrationError("enter a valid email address")

    with get_session() as s:
        # Already has an account → nothing to do (do NOT tell the caller).
        # Checks both fields because the username IS the email for accounts
        # minted here, while admin-provisioned ones only carry it as `email`.
        if s.exec(
            select(User).where((User.email == email) | (User.username == email))
        ).first():
            logger.info("signup ignored: %s already has an account", email)
            return
        # Already queued → don't stack duplicates in the admin's queue.
        existing = s.exec(
            select(Registration)
            .where(Registration.email == email)
            .where(Registration.status == "pending")
        ).first()
        if existing:
            logger.info("signup ignored: %s already pending", email)
            return
        s.add(
            Registration(
                email=email,
                display_name=(display_name or "").strip()[:_NAME_MAX] or None,
                note=(note or "").strip()[:_NOTE_MAX] or None,
            )
        )
        s.commit()
    logger.info("signup queued: %s", email)


# ── admin: read the queue ────────────────────────────────────────────────────


def _public(r: Registration) -> dict:
    return {
        "id": str(r.id),
        "email": r.email,
        "display_name": r.display_name,
        "note": r.note,
        "status": r.status,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "decided_at": r.decided_at.isoformat() if r.decided_at else None,
        "decided_by": r.decided_by,
        "created_username": r.created_username,
    }


def list_registrations(status: Optional[str] = None, limit: int = 200) -> list[dict]:
    with get_session() as s:
        q = select(Registration)
        if status:
            q = q.where(Registration.status == status)
        rows = s.exec(q.order_by(Registration.created_at.desc()).limit(limit)).all()
        return [_public(r) for r in rows]


def pending_count() -> int:
    with get_session() as s:
        return len(
            s.exec(select(Registration).where(Registration.status == "pending")).all()
        )


def _get_pending(s, reg_id) -> Registration:
    try:
        rid = reg_id if isinstance(reg_id, uuid.UUID) else uuid.UUID(str(reg_id))
    except (ValueError, AttributeError, TypeError):
        raise RegistrationError("registration not found")
    r = s.get(Registration, rid)
    if r is None:
        raise RegistrationError("registration not found")
    if r.status != "pending":
        raise RegistrationError(f"already {r.status}")
    return r


# ── admin: decide ────────────────────────────────────────────────────────────


def approve(reg_id, *, admin_label: Optional[str] = None,
            budget_usd: float = 0.0) -> dict:
    """Create the account, email the credentials, close the request.

    Returns the temp password so the admin can relay it by hand when
    ``email_sent`` is False (mail is best-effort and must not fail approval).
    """
    with get_session() as s:
        r = _get_pending(s, reg_id)
        email, display_name = r.email, r.display_name

    # The username IS the email they signed up with — nothing to invent, and it
    # matches what the credentials mail tells them to type. The password is the
    # only generated part.
    username = email
    password = _temp_password()
    try:
        user = user_service.create_user(
            username,
            password,
            role="user",
            display_name=display_name,
            email=email,
            must_change_password=True,
        )
    except user_service.UsernameTaken:
        # request_access screens this, so it only happens if an account was
        # created under that name between the request and the approval.
        raise RegistrationError(f"an account named {username} already exists")
    if budget_usd and budget_usd > 0:
        user_service.set_budget(user.id, budget_usd)

    # Close the request BEFORE emailing: the account exists either way, and a
    # slow/failing SMTP must not leave the row re-approvable.
    with get_session() as s:
        r = s.get(Registration, uuid.UUID(str(reg_id)))
        if r is not None:
            r.status = "approved"
            r.decided_at = _utcnow()
            r.decided_by = admin_label
            r.created_username = username
            s.add(r)
            s.commit()

    sent = email_service.send_credentials(email, username, password, display_name)
    return {
        "user_id": str(user.id),
        "username": username,
        "email": email,
        "temp_password": password,
        "email_sent": sent,
    }


def reject(reg_id, *, admin_label: Optional[str] = None,
           notify: bool = False) -> dict:
    with get_session() as s:
        r = _get_pending(s, reg_id)
        email, display_name = r.email, r.display_name
        r.status = "rejected"
        r.decided_at = _utcnow()
        r.decided_by = admin_label
        s.add(r)
        s.commit()

    sent = email_service.send_rejected(email, display_name) if notify else False
    return {"email": email, "email_sent": sent}
