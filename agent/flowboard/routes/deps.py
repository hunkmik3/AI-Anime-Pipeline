"""Shared FastAPI auth dependencies (Phase 9 multi-user).

``get_current_user`` extracts + validates the Bearer token and loads the
account; ``require_admin`` additionally gates on the admin role. Routes opt in
with ``Depends(...)``; a missing/invalid token yields 401, a non-admin 403.
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request

from flowboard.db.models import User
from flowboard.services import user_service


def get_current_user(
    request: Request, authorization: Optional[str] = Header(default=None)
) -> User:
    # When REQUIRE_AUTH is on, the global middleware already resolved + validated
    # the account (DB + status + token_version) and stashed it — reuse it to
    # avoid a second lookup. Otherwise resolve from the header here.
    cached = getattr(request.state, "user", None)
    if cached is not None:
        return cached
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    user = user_service.authenticate_token(authorization[7:].strip())
    if user is None:
        raise HTTPException(status_code=401, detail="invalid, expired, or revoked token")
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    """The owner only. Money, and who else is an owner, stop here."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    return user


def require_staff(user: User = Depends(get_current_user)) -> User:
    """Admin or studio manager — the console as a whole.

    Separate from ``require_admin`` so the two can diverge on the handful of
    endpoints where they must: budgets and promoting someone to admin are the
    owner's alone, and a manager who could do either would be an admin under a
    different name.
    """
    if user.role not in STAFF_ROLES:
        raise HTTPException(status_code=403, detail="admin or manager only")
    return user


def get_optional_user(
    request: Request, authorization: Optional[str] = Header(default=None)
) -> Optional[User]:
    """Like ``get_current_user`` but returns None instead of raising — used by
    owner-scoped routes. With REQUIRE_AUTH on, the middleware has already
    rejected suspended/revoked tokens before the route runs, so None here means
    genuinely no token (single-user/dev with auth off) → unscoped."""
    cached = getattr(request.state, "user", None)
    if cached is not None:
        return cached
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    return user_service.authenticate_token(authorization[7:].strip())


# ── project-structure policy (Phase 9.1) ────────────────────────────────────
# Only an admin may CREATE / RENAME / REORDER / DELETE a Project, Scene or Shot.
# Admins provision the whole structure on behalf of a user; a normal user only
# works *inside* a shot they own (nodes, prompts, generations, downloads).


#: System roles that run the studio rather than work in it.
#:
#: ``admin`` is the owner: everything, including money and who is an admin.
#: ``manager`` is a studio manager: opens projects, provisions accounts, links a
#: comic to the production series it delivers into — the day-to-day of running
#: two branches — but never touches budgets or promotes anyone to admin.
#:
#: The split exists because ``project.manage`` was admin-only, which made one
#: person the sole route to a new project. That is a bottleneck the moment two
#: branches run at once, and the answer is a second staff role rather than
#: handing out the role that also controls the money.
STAFF_ROLES: tuple[str, ...] = ("admin", "manager")


def is_staff(user: Optional[User]) -> bool:
    """Runs the studio. ``None`` is the no-auth dev/test path, which is unscoped
    for the same reason it always was: it must behave like the single-user app."""
    return user is None or user.role in STAFF_ROLES


def owner_scope(user: Optional[User]) -> Optional[uuid.UUID]:
    """The owner id a caller's reads/mutations are confined to.

    ``None`` means *unscoped* — it applies to staff (who see and manage every
    user's projects) and to the no-auth dev/test path (REQUIRE_AUTH off), which
    must keep behaving like the original single-user app. A normal user is
    confined to their own id.
    """
    if is_staff(user):
        return None
    return user.id


def require_structure_admin(
    user: Optional[User] = Depends(get_optional_user),
) -> Optional[User]:
    """Gate structural writes (create/rename/reorder/delete of project·scene·
    shot) to staff.

    When there is no authenticated user (REQUIRE_AUTH off — dev and the whole
    existing test suite) the operation stays permitted, exactly as before; the
    rule is strictly "an authenticated non-staff caller is denied", never "no
    user is denied".
    """
    if not is_staff(user):
        raise HTTPException(
            status_code=403,
            detail="only an admin or studio manager can create or modify project structure",
        )
    return user
