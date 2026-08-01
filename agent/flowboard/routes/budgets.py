"""Phase 11.1/11.2 — Project/Series credit budgets, with admin-approved top-ups.

    GET   /api/budgets/requests/pending            — admin inbox of credit requests
    POST  /api/budgets/requests/{id}/approve       — admin approves (raises the ceiling)
    POST  /api/budgets/requests/{id}/reject        — admin rejects (note required)
    GET   /api/budgets/{scope}/{scope_id}          — used / remaining / request log
    PUT   /api/budgets/{scope}/{scope_id}          — BOD sets the base budget
    POST  /api/budgets/{scope}/{scope_id}/grants   — PM REQUESTS more (reason required)
    GET   /api/budgets/{scope}/{scope_id}/grants   — that scope's request log

``scope`` is "project" or "series". Setting the base budget and deciding requests
are admin/BOD acts; a PM can only *ask*. Reads are open to anyone who can see the
project, so an artist can tell how much runway is left before they're blocked.

The ``/requests/...`` routes are declared BEFORE ``/{scope}/{scope_id}`` on
purpose — FastAPI matches in registration order and the catch-all would swallow
them (failing the UUID parse on "pending").
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from flowboard.db import get_session
from flowboard.db.models import Project, Scene, Series, Shot
from flowboard.routes.deps import get_optional_user, require_admin
from flowboard.services import audit_service, permissions, scope_budget
from flowboard.services import user_service

router = APIRouter(prefix="/api/budgets", tags=["budgets"])


def _project_id_for(s, scope: str, scope_id: uuid.UUID) -> uuid.UUID:
    """Every permission check happens against the owning project."""
    if scope == "project":
        if s.get(Project, scope_id) is None:
            raise HTTPException(404, "project not found")
        return scope_id
    if scope == "series":
        row = s.get(Series, scope_id)
        if row is None:
            raise HTTPException(404, "series not found")
        return row.project_id
    if scope == "scene":
        row = s.get(Scene, scope_id)
        if row is None:
            raise HTTPException(404, "episode not found")
        return row.project_id
    if scope == "shot":
        shot = s.get(Shot, scope_id)
        if shot is None:
            raise HTTPException(404, "sequence not found")
        scene = s.get(Scene, shot.scene_id)
        if scene is None:
            raise HTTPException(404, "episode not found")
        return scene.project_id
    raise HTTPException(
        400, "scope must be one of: project, series, scene (episode), shot (sequence)"
    )


def _grant_dict(g, session=None) -> dict:
    who = user_service.get_by_id(g.granted_by) if g.granted_by else None
    decider = user_service.get_by_id(g.decided_by) if g.decided_by else None
    d = {
        "id": str(g.id),
        "scope": g.scope,
        "scope_id": str(g.scope_id),
        "amount_usd": g.amount_usd,
        "reason": g.reason,
        "requested_by": str(g.granted_by) if g.granted_by else None,
        "granted_by_name": (who.display_name or who.username) if who else None,
        "requested_by_username": who.username if who else None,
        "requested_by_email": getattr(who, "email", None) if who else None,
        "created_at": g.created_at.isoformat() if g.created_at else None,
        "status": g.status,
        "decided_by_name": (decider.display_name or decider.username) if decider else None,
        "decided_at": g.decided_at.isoformat() if g.decided_at else None,
        "decision_note": g.decision_note,
    }
    # Which project/series the money is for — the admin deciding shouldn't have
    # to go look the id up.
    if session is not None:
        project = None
        if g.scope == "series":
            row = session.get(Series, g.scope_id)
            if row is not None:
                d["series_name"] = row.name
                d["series_code"] = row.code or ""
                project = session.get(Project, row.project_id)
        elif g.scope == "project":
            project = session.get(Project, g.scope_id)
        if project is not None:
            d["project_id"] = str(project.id)
            d["project_name"] = project.name
        # Current state of the budget being topped up, so the decision has the
        # numbers next to it.
        try:
            s = scope_budget.summary(session, g.scope, g.scope_id)
            d["budget"] = {
                "used_usd": s["used_usd"],
                "effective_usd": s["effective_usd"],
                "remaining_usd": s["remaining_usd"],
                "unlimited": s["unlimited"],
            }
        except Exception:  # pragma: no cover - a deleted scope must not 500 the inbox
            pass
    return d


# ── admin verdict on a credit request ─────────────────────────────────────


class DecisionBody(BaseModel):
    note: str | None = Field(default=None, max_length=500)


@router.get("/requests/pending")
def pending_requests(user=Depends(require_admin)):
    """The admin's credit-request inbox."""
    with get_session() as s:
        return {"requests": [_grant_dict(g, s) for g in scope_budget.pending_grants(s)]}


@router.post("/requests/{grant_id}/approve")
def approve_request(
    grant_id: uuid.UUID, body: DecisionBody, user=Depends(require_admin)
):
    """Approving is what actually raises the ceiling."""
    with get_session() as s:
        try:
            row = scope_budget.decide_grant(
                s, grant_id, approve=True, user=user, note=body.note
            )
        except scope_budget.GrantError as exc:
            raise HTTPException(400, str(exc))
        out = scope_budget.summary(s, row.scope, row.scope_id)
        out["grant"] = _grant_dict(row, s)
        return out


@router.post("/requests/{grant_id}/reject")
def reject_request(grant_id: uuid.UUID, body: DecisionBody, user=Depends(require_admin)):
    """Rejecting needs a note so the asker knows why."""
    with get_session() as s:
        try:
            row = scope_budget.decide_grant(
                s, grant_id, approve=False, user=user, note=body.note
            )
        except scope_budget.GrantError as exc:
            raise HTTPException(400, str(exc))
        out = scope_budget.summary(s, row.scope, row.scope_id)
        out["grant"] = _grant_dict(row, s)
        return out


@router.get("/{scope}/{scope_id}")
def get_budget(scope: str, scope_id: uuid.UUID, user=Depends(get_optional_user)):
    with get_session() as s:
        pid = _project_id_for(s, scope, scope_id)
        permissions.require(s, user, pid, "canvas.read")
        out = scope_budget.summary(s, scope, scope_id)
        out["grants"] = [_grant_dict(g, s) for g in scope_budget.list_grants(s, scope, scope_id)]
        return out


class SetBudgetBody(BaseModel):
    amount_usd: float = Field(ge=0)  # 0 clears the ceiling (unlimited)


@router.put("/{scope}/{scope_id}")
def set_budget(
    scope: str,
    scope_id: uuid.UUID,
    body: SetBudgetBody,
    request: Request,
    user=Depends(require_admin),
):
    """BOD/admin sets the base credit budget. 0 = unlimited."""
    with get_session() as s:
        _project_id_for(s, scope, scope_id)  # validates existence
        try:
            return scope_budget.set_budget(
                s,
                scope,
                scope_id,
                body.amount_usd,
                user=user,
                ip=audit_service.client_ip(request),
            )
        except scope_budget.GrantError as exc:
            raise HTTPException(400, str(exc))


class GrantBody(BaseModel):
    amount_usd: float = Field(gt=0)
    reason: str = Field(min_length=1, max_length=500)


@router.post("/{scope}/{scope_id}/grants")
def request_grant(
    scope: str, scope_id: uuid.UUID, body: GrantBody, user=Depends(get_optional_user)
):
    """PM **requests** extra credit. Lands pending and changes nothing until an
    admin approves — the reason is mandatory."""
    with get_session() as s:
        pid = _project_id_for(s, scope, scope_id)
        permissions.require(s, user, pid, "member.manage")  # producer+
        try:
            row = scope_budget.request_grant(
                s, scope, scope_id, amount_usd=body.amount_usd, reason=body.reason, user=user
            )
        except scope_budget.GrantError as exc:
            raise HTTPException(400, str(exc))
        out = scope_budget.summary(s, scope, scope_id)
        out["grant"] = _grant_dict(row, s)
        return out


@router.get("/{scope}/{scope_id}/grants")
def list_grants(scope: str, scope_id: uuid.UUID, user=Depends(get_optional_user)):
    with get_session() as s:
        pid = _project_id_for(s, scope, scope_id)
        permissions.require(s, user, pid, "canvas.read")
        return {
            "grants": [_grant_dict(g, s) for g in scope_budget.list_grants(s, scope, scope_id)]
        }
