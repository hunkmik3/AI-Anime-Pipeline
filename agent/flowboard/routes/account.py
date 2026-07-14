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
from flowboard.services import auth, budget_service, user_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/account", tags=["account"])


class LoginBody(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(body: LoginBody) -> dict:
    user = user_service.get_by_username(body.username.strip())
    # Same generic error for unknown user / wrong password / suspended, so we
    # don't leak which usernames exist. Lockout is the one distinct signal.
    if user is None:
        raise HTTPException(status_code=401, detail="invalid credentials")
    if user_service.is_locked(user):
        raise HTTPException(
            status_code=429, detail="too many failed attempts — try again later"
        )
    if user.status != "active" or not auth.verify_password(body.password, user.password_hash):
        user_service.register_failed_login(user.id)
        raise HTTPException(status_code=401, detail="invalid credentials")
    # Success: clear lockout, stamp last_login, upgrade the hash if it's below
    # the current cost, then issue a token stamped with the account's tv.
    user_service.register_successful_login(user.id, rehash_password=body.password)
    fresh = user_service.get_by_id(user.id) or user
    return {
        "token": auth.make_token(str(fresh.id), fresh.token_version),
        "user": user_service.public_dict(fresh),
    }


@router.get("/me")
def me(user: User = Depends(get_current_user)) -> dict:
    d = user_service.public_dict(user)
    summ = budget_service.summary(user.id)
    if summ:
        d["available_usd"] = summ["available_usd"]
        d["reserved_usd"] = summ["reserved_usd"]
    return d
