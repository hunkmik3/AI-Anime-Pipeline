"""Account/session routes — app login (Phase 9 multi-user).

Distinct from the Flow-identity routes under /api/auth (that's the Chrome
extension's Google profile). These are the app's own admin-provisioned logins.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from flowboard.db.models import User
from flowboard.routes.deps import get_current_user
from flowboard.services import auth, user_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/account", tags=["account"])


class LoginBody(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(body: LoginBody) -> dict:
    user = user_service.get_by_username(body.username.strip())
    # Same generic error for all failure modes so we don't leak which usernames
    # exist or whether an account is suspended.
    if (
        user is None
        or user.status != "active"
        or not auth.verify_password(body.password, user.password_hash)
    ):
        raise HTTPException(status_code=401, detail="invalid credentials")
    return {"token": auth.make_token(str(user.id)), "user": user_service.public_dict(user)}


@router.get("/me")
def me(user: User = Depends(get_current_user)) -> dict:
    return user_service.public_dict(user)
