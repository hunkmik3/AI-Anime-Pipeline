"""Phase 2: top-level Project REST surface.

Replaces the legacy ``/api/boards/*`` shim (see ``routes/boards.py``) and
the Flow-project binding sub-resource that lived under
``/api/boards/{id}/project`` (see ``routes/flow_binding_legacy.py``).

Sub-resources:
- ``/bible``     — strict-validated ProjectBible JSONB (see ``routes/bibles.py``)
- ``/flow-project`` — 1:1 Google Flow project_id binding
- ``/chat``      — chat messages now naturally scope by project
- ``/cost``      — cost rollup across all shots' Request rows
"""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from flowboard.config import BRIDGE_ENABLED
from flowboard.db import get_session
from flowboard.routes.deps import (
    get_optional_user,
    owner_scope,
    require_structure_admin,
)
from flowboard.schemas import ProjectCreate, ProjectUpdate
from sqlmodel import select

from flowboard.services import audit_service, permissions, resource_guard
from flowboard.services import project_service as ps
from flowboard.services import user_service
from flowboard.services.flow_sdk import get_flow_sdk, is_valid_project_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/projects", tags=["projects"])


def _project_dict(
    project,
    *,
    owner_names: dict | None = None,
    session=None,
    user=None,
) -> dict:
    oid = str(project.owner_user_id) if project.owner_user_id else None
    d = {
        "id": str(project.id),
        "name": project.name,
        "project_bible": dict(project.project_bible or {}),
        "settings": dict(project.settings or {}),
        "owner_user_id": oid,
        # Resolved display label for the admin console; None for unowned rows.
        "owner_name": (owner_names or {}).get(oid) if oid else None,
        "created_at": project.created_at.isoformat() if project.created_at else None,
    }
    # Cover thumbnail + the full assigned set (owner first, then members) so the
    # admin console can show/edit who a project is shared with.
    if session is not None:
        d["thumb_media_id"] = ps.project_thumb_media_id(session, project)
        members = ps.get_project_members(session, project.id)
        by_id = {str(m.user_id): permissions.normalize_role(m.role) for m in members}
        assignees = ([oid] if oid else []) + [m for m in by_id if m != oid]
        d["assignee_ids"] = assignees
        # The owner is a producer implicitly (no member row) — surface that so
        # the console shows one consistent role column.
        d["assignee_roles"] = {
            a: (permissions.PRODUCER if a == oid else by_id.get(a, permissions.ARTIST))
            for a in assignees
        }
        if owner_names:
            d["assignee_names"] = [owner_names.get(a, a) for a in assignees]
        # What *this* caller may do here — the UI hides what it can't do rather
        # than letting the user click into a 403.
        role = permissions.project_role(session, user, project.id)
        d["my_role"] = role
        d["can"] = permissions.capability_map(role)
        # Phase 11.1: credit budget rollup, so the console can show a Budget
        # column without a second round-trip per row.
        from flowboard.services import scope_budget

        d["budget"] = scope_budget.summary(session, "project", project.id)
    return d


def _owner_name_map(users) -> dict:
    return {str(u.id): (u.display_name or u.username) for u in users}


def _apply_assignment(s, project_id, member_user_ids, *, current_owner=None):
    """Persist a project's full assigned set. The first id (or the current owner
    if still assigned) stays the primary owner; the rest become members.
    Returns the owner id to store on the project. Validates every user exists."""
    ids = list(dict.fromkeys(member_user_ids))  # dedupe, preserve order
    for uid in ids:
        if user_service.get_by_id(uid) is None:
            raise HTTPException(404, f"assigned user not found: {uid}")
    owner = current_owner if (current_owner in ids) else (ids[0] if ids else None)
    ps.set_project_members(s, project_id, [u for u in ids if u != owner])
    return owner


@router.get("")
def list_projects(user=Depends(get_optional_user)):
    # Admins (owner_scope None) see every user's projects — they provision and
    # manage the whole structure; a normal user sees only their own.
    scope = owner_scope(user)
    with get_session() as s:
        projects = ps.list_projects(s, owner_user_id=scope)
        names = _owner_name_map(user_service.list_users()) if scope is None else {}
        return [_project_dict(p, owner_names=names, session=s, user=user) for p in projects]


@router.post("")
def create_project(body: ProjectCreate, user=Depends(require_structure_admin)):
    # Admin-only (require_structure_admin). An admin assigns the project to a
    # user via owner_user_id; the no-auth dev/test path leaves it NULL.
    with get_session() as s:
        owner_id = body.owner_user_id
        assigned = body.member_user_ids
        if user is not None and user.role == "admin":
            # Full assigned set (multi-user) takes precedence over the legacy
            # single owner_user_id; the first assignee becomes the primary owner.
            if assigned:
                for uid in assigned:
                    if user_service.get_by_id(uid) is None:
                        raise HTTPException(404, "assigned user not found")
                owner_id = list(dict.fromkeys(assigned))[0]
            elif owner_id is not None and user_service.get_by_id(owner_id) is None:
                raise HTTPException(404, "owner user not found")
        elif user is not None:
            # A non-admin can never reach here (gate raises 403), but be strict.
            owner_id = user.id
            assigned = None
        project = ps.create_project(
            s,
            name=body.name,
            project_bible=body.project_bible.model_dump() if body.project_bible else None,
            settings=body.settings,
            owner_user_id=owner_id,
        )
        if assigned:
            ps.set_project_members(
                s, project.id, [u for u in dict.fromkeys(assigned) if u != owner_id]
            )
        names = _owner_name_map(user_service.list_users())
        return _project_dict(project, owner_names=names, session=s, user=user)


@router.get("/{project_id}")
def get_project(project_id: uuid.UUID, user=Depends(get_optional_user)):
    scope = owner_scope(user)
    with get_session() as s:
        try:
            project = ps.get_project(s, project_id, owner_user_id=scope)
        except ps.ProjectNotFound:
            raise HTTPException(404, "project not found")
        names = _owner_name_map(user_service.list_users()) if scope is None else {}
        base = _project_dict(project, owner_names=names, session=s, user=user)
        base["scene_count"] = ps.project_scene_count(s, project_id)
        base["asset_count"] = ps.project_asset_count(s, project_id)
        return base


@router.patch("/{project_id}")
def update_project(
    project_id: uuid.UUID, body: ProjectUpdate, user=Depends(require_structure_admin)
):
    # Rename / settings — structural, so admin-only. Admin is unscoped and may
    # edit any user's project.
    with get_session() as s:
        try:
            existing = ps.get_project(s, project_id, owner_user_id=owner_scope(user))
            # Reassignment is admin-only. `member_user_ids` (the full assigned
            # set) takes precedence; `owner_user_id` remains for legacy single
            # reassignment.
            reassign = {}
            if body.member_user_ids is not None:
                reassign["owner_user_id"] = _apply_assignment(
                    s,
                    project_id,
                    body.member_user_ids,
                    current_owner=existing.owner_user_id,
                )
            elif body.owner_user_id is not None:
                if user_service.get_by_id(body.owner_user_id) is None:
                    raise HTTPException(404, "owner user not found")
                reassign["owner_user_id"] = body.owner_user_id
            project = ps.update_project(
                s,
                project_id,
                name=body.name,
                settings=body.settings,
                **reassign,
            )
        except ps.ProjectNotFound:
            raise HTTPException(404, "project not found")
        names = _owner_name_map(user_service.list_users())
        return _project_dict(project, owner_names=names, session=s, user=user)


@router.delete("/{project_id}")
def delete_project(project_id: uuid.UUID, user=Depends(require_structure_admin)):
    with get_session() as s:
        try:
            ps.get_project(s, project_id, owner_user_id=owner_scope(user))
            ps.delete_project(s, project_id)
        except ps.ProjectNotFound:
            raise HTTPException(404, "project not found")
        return {"deleted": str(project_id)}


# ── Members + roles (Phase 10) ────────────────────────────────────────────


class MemberAssignment(BaseModel):
    user_id: uuid.UUID
    role: str = permissions.ARTIST


class MembersBody(BaseModel):
    """The project's full assigned set. The first entry (or the current owner
    if still listed) stays the primary owner and is forced to ``producer``."""

    members: list[MemberAssignment]


def _members_payload(s, project) -> dict:
    names = {str(u.id): (u.display_name or u.username) for u in user_service.list_users()}
    oid = str(project.owner_user_id) if project.owner_user_id else None
    rows = []
    if oid:
        rows.append(
            {
                "user_id": oid,
                "name": names.get(oid, oid),
                "role": permissions.PRODUCER,
                "is_owner": True,
            }
        )
    for m in ps.get_project_members(s, project.id):
        uid = str(m.user_id)
        if uid == oid:
            continue
        rows.append(
            {
                "user_id": uid,
                "name": names.get(uid, uid),
                "role": permissions.normalize_role(m.role),
                "is_owner": False,
            }
        )
    return {"members": rows, "roles": list(permissions.PROJECT_ROLES)}


@router.get("/{project_id}/members")
def list_project_members(project_id: uuid.UUID, user=Depends(get_optional_user)):
    with get_session() as s:
        permissions.require(s, user, project_id, "canvas.read")
        project = ps.get_project(s, project_id)
        return _members_payload(s, project)


@router.get("/{project_id}/assignable-users")
def list_assignable_users(project_id: uuid.UUID, user=Depends(get_optional_user)):
    """The people a producer can add to *this* project — id + display name only.

    Gated on ``member.manage`` (producer+/admin) and deliberately leaner than
    the admin ``/api/admin/users`` console (no budget, role, or audit data), so
    a producer can staff their project without seeing company-wide account info.
    """
    with get_session() as s:
        permissions.require(s, user, project_id, "member.manage")
        return [
            {"user_id": str(u.id), "name": (u.display_name or u.username)}
            for u in user_service.list_users()
            if getattr(u, "status", "active") == "active"
        ]


@router.put("/{project_id}/members")
def set_project_members_route(
    project_id: uuid.UUID,
    body: MembersBody,
    request: Request,
    user=Depends(get_optional_user),
):
    """Replace who is on the project and what each of them may do.

    Producer-and-up (admins included) — this is how a producer staffs their own
    project without an admin round-trip.
    """
    with get_session() as s:
        permissions.require(s, user, project_id, "member.manage")
        try:
            existing = ps.get_project(s, project_id)
        except ps.ProjectNotFound:
            raise HTTPException(404, "project not found")

        seen: dict[uuid.UUID, str] = {}
        for m in body.members:
            if user_service.get_by_id(m.user_id) is None:
                raise HTTPException(404, f"assigned user not found: {m.user_id}")
            seen.setdefault(m.user_id, permissions.normalize_role(m.role))
        ids = list(seen)
        # Snapshot the roster so the trail shows who joined, left, or changed
        # role — a bare "roster replaced" tells nobody anything.
        before_roster = _roster_snapshot(s, project_id, existing.owner_user_id)
        # Editing the roster must never move ownership. Falling back to "the
        # first person in the list" made dropping the owner from the roster
        # silently promote whoever happened to be first — an artist would come
        # out of it holding producer rights (member.manage, series.delete).
        # Ownership changes go through PATCH /api/projects/{id}, which is
        # admin-only and explicit. Only a project that has no owner at all
        # (legacy / no-auth rows) adopts one from the roster.
        owner = existing.owner_user_id or (ids[0] if ids else None)
        ps.set_project_members(
            s,
            project_id,
            [u for u in ids if u != owner],
            roles={u: r for u, r in seen.items() if u != owner},
        )
        if owner != existing.owner_user_id:
            ps.update_project(s, project_id, owner_user_id=owner)
        project = ps.get_project(s, project_id)

        after_roster = _roster_snapshot(s, project_id, project.owner_user_id)
        changes = {}
        for uid in set(before_roster) | set(after_roster):
            changes[_user_label(uid)] = (before_roster.get(uid), after_roster.get(uid))
        audit_service.record_change(
            "project.members",
            object_type="project",
            object_id=project_id,
            object_label=project.name,
            changes=changes,
            actor=user,
            ip=audit_service.client_ip(request),
        )
        return _members_payload(s, project)


def _user_label(user_id) -> str:
    u = user_service.get_by_id(user_id)
    return (u.display_name or u.username) if u else str(user_id)[:8]


def _roster_snapshot(session, project_id, owner_user_id) -> dict:
    """{user_id: role} for a project, owner included as the implicit producer."""
    from flowboard.db.models import ProjectMember

    out: dict = {}
    if owner_user_id:
        out[owner_user_id] = "producer (owner)"
    for m in session.exec(
        select(ProjectMember).where(ProjectMember.project_id == project_id)
    ).all():
        out.setdefault(m.user_id, m.role)
    return out


@router.get("/{project_id}/cost")
def get_project_cost(project_id: uuid.UUID, user=Depends(get_optional_user)):
    with get_session() as s:
        # Spend rolled up across the whole project is management information:
        # ungated, any caller could read what another team's project costs.
        resource_guard.authorize_project(s, user, project_id, "member.manage")
        return {"cost_usd": ps.project_cost_usd(s, project_id)}


@router.get("/{project_id}/video-gens")
def get_project_video_gens(project_id: uuid.UUID, user=Depends(get_optional_user)):
    """All generated video clips in a project, grouped episode -> sequence,
    with prompt + settings. Powers the project 'Generated videos' gallery.
    Producer+ (admins unscoped)."""
    from flowboard.services import stats_service

    with get_session() as s:
        # A cross-episode rollup of everything the project has produced, so it
        # ignores episode scope — bare project membership was too weak a gate.
        resource_guard.authorize_project(s, user, project_id, "member.manage")
    return stats_service.project_video_gens(project_id)


@router.get("/{project_id}/images")
def list_project_images(project_id: uuid.UUID, user=Depends(get_optional_user)):
    """Recent image assets in a project — the pool for picking a cover.
    Owner-scoped (admins unscoped)."""
    with get_session() as s:
        try:
            ps.get_project(s, project_id, owner_user_id=owner_scope(user))
        except ps.ProjectNotFound:
            raise HTTPException(404, "project not found")
        return {"images": ps.project_images(s, project_id)}


class ProjectCoverBody(BaseModel):
    media_id: str | None = None   # None clears (back to auto/monogram)


@router.post("/{project_id}/cover")
def set_project_cover(
    project_id: uuid.UUID, body: ProjectCoverBody, user=Depends(get_optional_user)
):
    """Set/clear a project's cover thumbnail. Cosmetic → project.decorate
    (artist+), unlike the structural PATCH."""
    with get_session() as s:
        resource_guard.authorize_project(s, user, project_id, "project.decorate")
        try:
            project = ps.set_project_cover(s, project_id, body.media_id)
        except ps.ProjectNotFound:
            raise HTTPException(404, "project not found")
        names = _owner_name_map(user_service.list_users()) if owner_scope(user) is None else {}
        return _project_dict(project, owner_names=names, session=s, user=user)


@router.get("/{project_id}/chat")
def list_project_chat(
    project_id: uuid.UUID,
    limit: int = Query(default=500, ge=1, le=2000),
    user=Depends(get_optional_user),
):
    with get_session() as s:
        resource_guard.authorize_project(s, user, project_id, "canvas.read")
        try:
            return ps.list_project_chat(s, project_id, limit=limit)
        except ps.ProjectNotFound:
            raise HTTPException(404, "project not found")


# ── Flow project binding ──────────────────────────────────────────────────


@router.get("/{project_id}/flow-project")
def get_flow_project(project_id: uuid.UUID, user=Depends(get_optional_user)):
    with get_session() as s:
        resource_guard.authorize_project(s, user, project_id, "canvas.read")
        # Bridge off (Avis/Seedance mode): there's no Google Flow binding —
        # the DB project id doubles as the project handle that uploads + the
        # worker's R2 namespace key need.
        if not BRIDGE_ENABLED:
            return {"flow_project_id": str(project_id), "created": False}
        try:
            row = ps.get_flow_project(s, project_id)
        except ps.ProjectNotFound:
            raise HTTPException(404, "project not found")
        except ps.FlowProjectNotBound:
            raise HTTPException(404, "no flow project bound to this project")
        return {"flow_project_id": row.flow_project_id, "created": False}


@router.post("/{project_id}/flow-project")
async def ensure_flow_project(project_id: uuid.UUID, user=Depends(get_optional_user)):
    # Cheap path: existing binding short-circuits before the extension hop.
    with get_session() as s:
        # Binds the project to a Google Flow project on the studio's Flow
        # account, so an ungated caller could create Flow projects off another
        # team's id — a write, not a read.
        project = resource_guard.authorize_project(s, user, project_id, "canvas.write")
        # Bridge off: skip the Flow extension round-trip entirely and hand back
        # the DB project id as the handle (see get_flow_project above).
        if not BRIDGE_ENABLED:
            return {"flow_project_id": str(project_id), "created": False}
        try:
            existing = ps.get_flow_project(s, project_id)
            return {"flow_project_id": existing.flow_project_id, "created": False}
        except ps.FlowProjectNotBound:
            pass
        project_name = project.name

    # Round-trip to the extension is OUTSIDE the DB session.
    resp = await get_flow_sdk().create_project(title=project_name or "Untitled")
    if resp.get("error"):
        raise HTTPException(
            status_code=502,
            detail={"message": resp["error"], "raw": resp.get("raw")},
        )
    flow_project_id = resp.get("project_id")
    if not isinstance(flow_project_id, str) or not flow_project_id:
        raise HTTPException(
            status_code=502,
            detail={"message": "no project_id in Flow response", "raw": resp.get("raw")},
        )
    if not is_valid_project_id(flow_project_id):
        raise HTTPException(
            status_code=502,
            detail={
                "message": "invalid project_id shape from Flow",
                "raw": resp.get("raw"),
            },
        )

    with get_session() as s:
        # Race: another caller could have bound it during the hop.
        try:
            existing = ps.get_flow_project(s, project_id)
            return {"flow_project_id": existing.flow_project_id, "created": False}
        except ps.FlowProjectNotBound:
            pass
        row = ps.bind_flow_project(s, project_id, flow_project_id)
        logger.info("bound project %s → flow_project %s", project_id, flow_project_id)
        return {"flow_project_id": row.flow_project_id, "created": True}
