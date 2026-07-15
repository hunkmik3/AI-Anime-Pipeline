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
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="admin only")
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


def owner_scope(user: Optional[User]) -> Optional[uuid.UUID]:
    """The owner id a caller's reads/mutations are confined to.

    ``None`` means *unscoped* — it applies to admins (who see and manage every
    user's projects) and to the no-auth dev/test path (REQUIRE_AUTH off), which
    must keep behaving like the original single-user app. A normal user is
    confined to their own id.
    """
    if user is None or user.role == "admin":
        return None
    return user.id


def require_structure_admin(
    user: Optional[User] = Depends(get_optional_user),
) -> Optional[User]:
    """Gate structural writes (create/rename/reorder/delete of project·scene·
    shot) to admins.

    When there is no authenticated user (REQUIRE_AUTH off — dev and the whole
    existing test suite) the operation stays permitted, exactly as before; the
    rule is strictly "an authenticated *non-admin* is denied", never "no user
    is denied".
    """
    if user is not None and user.role != "admin":
        raise HTTPException(
            status_code=403,
            detail="only an admin can create or modify project structure",
        )
    return user
