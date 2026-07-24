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

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from flowboard.config import BRIDGE_ENABLED
from flowboard.db import get_session
from flowboard.routes.deps import (
    get_optional_user,
    owner_scope,
    require_structure_admin,
)
from flowboard.schemas import ProjectCreate, ProjectUpdate
from flowboard.services import project_service as ps
from flowboard.services import user_service
from flowboard.services.flow_sdk import get_flow_sdk, is_valid_project_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/projects", tags=["projects"])


def _project_dict(project, *, owner_names: dict | None = None, session=None) -> dict:
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
        member_ids = [str(m) for m in ps.get_project_member_ids(session, project.id)]
        assignees = ([oid] if oid else []) + [m for m in member_ids if m != oid]
        d["assignee_ids"] = assignees
        if owner_names:
            d["assignee_names"] = [owner_names.get(a, a) for a in assignees]
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
        return [_project_dict(p, owner_names=names, session=s) for p in projects]


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
        return _project_dict(project, owner_names=names, session=s)


@router.get("/{project_id}")
def get_project(project_id: uuid.UUID, user=Depends(get_optional_user)):
    scope = owner_scope(user)
    with get_session() as s:
        try:
            project = ps.get_project(s, project_id, owner_user_id=scope)
        except ps.ProjectNotFound:
            raise HTTPException(404, "project not found")
        names = _owner_name_map(user_service.list_users()) if scope is None else {}
        base = _project_dict(project, owner_names=names, session=s)
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
        return _project_dict(project, owner_names=names, session=s)


@router.delete("/{project_id}")
def delete_project(project_id: uuid.UUID, user=Depends(require_structure_admin)):
    with get_session() as s:
        try:
            ps.get_project(s, project_id, owner_user_id=owner_scope(user))
            ps.delete_project(s, project_id)
        except ps.ProjectNotFound:
            raise HTTPException(404, "project not found")
        return {"deleted": str(project_id)}


@router.get("/{project_id}/cost")
def get_project_cost(project_id: uuid.UUID):
    with get_session() as s:
        try:
            ps.get_project(s, project_id)
        except ps.ProjectNotFound:
            raise HTTPException(404, "project not found")
        return {"cost_usd": ps.project_cost_usd(s, project_id)}


@router.get("/{project_id}/video-gens")
def get_project_video_gens(project_id: uuid.UUID, user=Depends(get_optional_user)):
    """All generated video clips in a project, grouped episode -> sequence,
    with prompt + settings. Powers the project 'Generated videos' gallery.
    Owner-scoped (admins unscoped)."""
    from flowboard.services import stats_service

    with get_session() as s:
        try:
            ps.get_project(s, project_id, owner_user_id=owner_scope(user))
        except ps.ProjectNotFound:
            raise HTTPException(404, "project not found")
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
    """Set/clear a project's cover thumbnail. Cosmetic → owner-scoped (owner +
    admin), unlike the structural PATCH."""
    with get_session() as s:
        try:
            ps.get_project(s, project_id, owner_user_id=owner_scope(user))
            project = ps.set_project_cover(s, project_id, body.media_id)
        except ps.ProjectNotFound:
            raise HTTPException(404, "project not found")
        names = _owner_name_map(user_service.list_users()) if owner_scope(user) is None else {}
        return _project_dict(project, owner_names=names, session=s)


@router.get("/{project_id}/chat")
def list_project_chat(
    project_id: uuid.UUID,
    limit: int = Query(default=500, ge=1, le=2000),
):
    with get_session() as s:
        try:
            return ps.list_project_chat(s, project_id, limit=limit)
        except ps.ProjectNotFound:
            raise HTTPException(404, "project not found")


# ── Flow project binding ──────────────────────────────────────────────────


@router.get("/{project_id}/flow-project")
def get_flow_project(project_id: uuid.UUID):
    with get_session() as s:
        # Bridge off (Avis/Seedance mode): there's no Google Flow binding —
        # the DB project id doubles as the project handle that uploads + the
        # worker's R2 namespace key need.
        if not BRIDGE_ENABLED:
            try:
                ps.get_project(s, project_id)
            except ps.ProjectNotFound:
                raise HTTPException(404, "project not found")
            return {"flow_project_id": str(project_id), "created": False}
        try:
            row = ps.get_flow_project(s, project_id)
        except ps.ProjectNotFound:
            raise HTTPException(404, "project not found")
        except ps.FlowProjectNotBound:
            raise HTTPException(404, "no flow project bound to this project")
        return {"flow_project_id": row.flow_project_id, "created": False}


@router.post("/{project_id}/flow-project")
async def ensure_flow_project(project_id: uuid.UUID):
    # Cheap path: existing binding short-circuits before the extension hop.
    with get_session() as s:
        try:
            project = ps.get_project(s, project_id)
        except ps.ProjectNotFound:
            raise HTTPException(404, "project not found")
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
