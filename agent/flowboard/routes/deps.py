"""Shared FastAPI auth dependencies (Phase 9 multi-user).

``get_current_user`` extracts + validates the Bearer token and loads the
account; ``require_admin`` additionally gates on the admin role. Routes opt in
with ``Depends(...)``; a missing/invalid token yields 401, a non-admin 403.
"""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, Header, HTTPException

from flowboard.db.models import User
from flowboard.services import auth, user_service


def get_current_user(authorization: Optional[str] = Header(default=None)) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization[7:].strip()
    user_id = auth.verify_token(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="invalid or expired token")
    user = user_service.get_by_id(user_id)
    if user is None or user.status != "active":
        raise HTTPException(status_code=401, detail="account not found or suspended")
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    return user


def get_optional_user(authorization: Optional[str] = Header(default=None)) -> Optional[User]:
    """Like ``get_current_user`` but returns None instead of raising — used by
    routes that scope by owner: a valid token → scope to that user; no/invalid
    token (single-user/dev with auth off) → None → unscoped."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    user_id = auth.verify_token(authorization[7:].strip())
    if not user_id:
        return None
    user = user_service.get_by_id(user_id)
    if user is None or user.status != "active":
        return None
    return user
