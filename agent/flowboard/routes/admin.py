"""Admin account management (Phase 9 multi-user). Admin-only.

You (the owner/admin) provision accounts here — there is no open signup.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from flowboard.routes.deps import require_admin
from flowboard.services import audit_service, budget_service, user_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])


class CreateUserBody(BaseModel):
    username: str
    password: str
    role: str = "user"  # "admin" | "user"
    display_name: Optional[str] = None
    email: Optional[str] = None


class UpdateUserBody(BaseModel):
    status: Optional[str] = None         # "active" | "suspended"
    password: Optional[str] = None       # reset password (forces change on next login)
    display_name: Optional[str] = None
    email: Optional[str] = None
    role: Optional[str] = None           # "admin" | "user" (last-admin guarded)
    must_change_password: Optional[bool] = None
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
def create_user(body: CreateUserBody, request: Request, caller=Depends(require_admin)) -> dict:
    try:
        u = user_service.create_user(
            body.username,
            body.password,
            role=body.role,
            display_name=body.display_name,
            email=body.email,
            # Admin-provisioned password is a temp — force a change on first login.
            must_change_password=True,
        )
    except user_service.UsernameTaken:
        raise HTTPException(status_code=409, detail="username already exists")
    except user_service.UserError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    audit_service.record(
        "user.create", actor=caller, target=u,
        ip=audit_service.client_ip(request), detail=f"role={body.role}",
    )
    return user_service.public_dict(u)


@router.get("/audit")
def audit(limit: int = 200, action: str | None = None) -> list[dict]:
    """Recent security-audit entries (logins, SSO, admin actions), newest first."""
    return audit_service.list_recent(limit=min(max(1, limit), 1000), action=action)


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
def delete_user(user_id: str, request: Request, caller=Depends(require_admin)) -> dict:
    """Delete an account. Guards: can't delete yourself or the last admin.
    Owned projects are orphaned (not destroyed)."""
    if str(caller.id) == str(user_id):
        raise HTTPException(status_code=400, detail="cannot delete your own account")
    u = user_service.get_by_id(user_id)
    if u is None:
        raise HTTPException(status_code=404, detail="user not found")
    if u.role == "admin" and user_service.count_admins() <= 1:
        raise HTTPException(status_code=400, detail="cannot delete the last admin")
    label = u.username
    try:
        user_service.delete_user(user_id)
    except user_service.UserNotFound:
        raise HTTPException(status_code=404, detail="user not found")
    audit_service.record(
        "user.delete", actor=caller, target=user_id, target_label=label,
        ip=audit_service.client_ip(request),
    )
    return {"ok": True}


@router.patch("/users/{user_id}")
def update_user(user_id: str, body: UpdateUserBody, request: Request, caller=Depends(require_admin)) -> dict:
    ip = audit_service.client_ip(request)

    def _log(action: str, detail: str | None = None):
        audit_service.record(action, actor=caller, target=u, ip=ip, detail=detail)

    try:
        u = user_service.get_by_id(user_id)
        if u is None:
            raise HTTPException(status_code=404, detail="user not found")
        if body.display_name is not None:
            user_service.set_display_name(user_id, body.display_name)
        if body.email is not None:
            user_service.set_email(user_id, body.email)
            _log("user.email")
        if body.role is not None:
            user_service.set_role(user_id, body.role)
            _log("user.role", f"role={body.role}")
        if body.password:
            user_service.set_password(user_id, body.password)
            _log("password.reset")
        if body.status is not None:
            user_service.set_status(user_id, body.status)
            _log("user.suspend" if body.status == "suspended" else "user.activate")
        if body.must_change_password is not None:
            user_service.set_must_change_password(user_id, body.must_change_password)
        if body.budget_usd is not None:
            user_service.set_budget(user_id, body.budget_usd)
            _log("user.budget", f"set=${body.budget_usd}")
        if body.add_budget_usd is not None:
            user_service.add_budget(user_id, body.add_budget_usd)
            _log("user.budget", f"add=${body.add_budget_usd}")
        refreshed = user_service.get_by_id(user_id)
        return _user_with_budget(refreshed)
    except user_service.UserNotFound:
        raise HTTPException(status_code=404, detail="user not found")
    except user_service.UserError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
