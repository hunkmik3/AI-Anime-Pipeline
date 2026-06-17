"""Admin account management (Phase 9 multi-user). Admin-only.

You (the owner/admin) provision accounts here — there is no open signup.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from flowboard.routes.deps import require_admin
from flowboard.services import user_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])


class CreateUserBody(BaseModel):
    username: str
    password: str
    role: str = "user"  # "admin" | "user"
    display_name: Optional[str] = None


class UpdateUserBody(BaseModel):
    status: Optional[str] = None        # "active" | "suspended"
    password: Optional[str] = None      # reset password
    display_name: Optional[str] = None


@router.get("/users")
def list_users() -> list[dict]:
    return [user_service.public_dict(u) for u in user_service.list_users()]


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
        refreshed = user_service.get_by_id(user_id)
        return user_service.public_dict(refreshed)
    except user_service.UserNotFound:
        raise HTTPException(status_code=404, detail="user not found")
    except user_service.UserError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
