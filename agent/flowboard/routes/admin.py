"""Admin account management (Phase 9 multi-user). Admin-only.

You (the owner/admin) provision accounts here — there is no open signup.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from flowboard.routes.deps import require_admin
from flowboard.services import budget_service, user_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])


class CreateUserBody(BaseModel):
    username: str
    password: str
    role: str = "user"  # "admin" | "user"
    display_name: Optional[str] = None


class UpdateUserBody(BaseModel):
    status: Optional[str] = None         # "active" | "suspended"
    password: Optional[str] = None       # reset password
    display_name: Optional[str] = None
    budget_usd: Optional[float] = None       # set absolute $ budget
    add_budget_usd: Optional[float] = None   # top-up (+/-) $ budget


def _user_with_budget(u) -> dict:
    d = user_service.public_dict(u)
    summ = budget_service.summary(u.id)
    if summ:
        d["available_usd"] = summ["available_usd"]
        d["reserved_usd"] = summ["reserved_usd"]
    return d


@router.get("/users")
def list_users() -> list[dict]:
    return [_user_with_budget(u) for u in user_service.list_users()]


@router.post("/users")
def create_user(body: CreateUserBody) -> dict:
    try:
        u = user_service.create_user(
            body.username,
            body.password,
            role=body.role,
            display_name=body.display_name,
        )
    except user_service.UsernameTaken:
        raise HTTPException(status_code=409, detail="username already exists")
    except user_service.UserError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return user_service.public_dict(u)


@router.get("/users/{user_id}/activity")
def user_activity(user_id: str, limit: int = 100) -> dict:
    """Per-user generation history for the admin view: what they generated,
    which model, the real $ cost, status, and output media ids."""
    u = user_service.get_by_id(user_id)
    if u is None:
        raise HTTPException(status_code=404, detail="user not found")
    data = budget_service.user_activity(user_id, limit=min(max(1, limit), 500))
    if data is None:
        raise HTTPException(status_code=404, detail="user not found")
    data["user"] = _user_with_budget(u)
    return data


@router.delete("/users/{user_id}")
def delete_user(user_id: str, caller=Depends(require_admin)) -> dict:
    """Delete an account. Guards: can't delete yourself or the last admin.
    Owned projects are orphaned (not destroyed)."""
    if str(caller.id) == str(user_id):
        raise HTTPException(status_code=400, detail="cannot delete your own account")
    u = user_service.get_by_id(user_id)
    if u is None:
        raise HTTPException(status_code=404, detail="user not found")
    if u.role == "admin" and user_service.count_admins() <= 1:
        raise HTTPException(status_code=400, detail="cannot delete the last admin")
    try:
        user_service.delete_user(user_id)
    except user_service.UserNotFound:
        raise HTTPException(status_code=404, detail="user not found")
    return {"ok": True}


@router.patch("/users/{user_id}")
def update_user(user_id: str, body: UpdateUserBody) -> dict:
    try:
        u = user_service.get_by_id(user_id)
        if u is None:
            raise HTTPException(status_code=404, detail="user not found")
        if body.display_name is not None:
            user_service.set_display_name(user_id, body.display_name)
        if body.password:
            user_service.set_password(user_id, body.password)
        if body.status is not None:
            user_service.set_status(user_id, body.status)
        if body.budget_usd is not None:
            user_service.set_budget(user_id, body.budget_usd)
        if body.add_budget_usd is not None:
            user_service.add_budget(user_id, body.add_budget_usd)
        refreshed = user_service.get_by_id(user_id)
        return _user_with_budget(refreshed)
    except user_service.UserNotFound:
        raise HTTPException(status_code=404, detail="user not found")
    except user_service.UserError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
