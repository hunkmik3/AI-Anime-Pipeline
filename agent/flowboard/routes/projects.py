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

from fastapi import APIRouter, HTTPException, Query

from flowboard.config import BRIDGE_ENABLED
from flowboard.db import get_session
from flowboard.schemas import ProjectCreate, ProjectUpdate
from flowboard.services import project_service as ps
from flowboard.services.flow_sdk import get_flow_sdk, is_valid_project_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/projects", tags=["projects"])


def _project_dict(project) -> dict:
    return {
        "id": str(project.id),
        "name": project.name,
        "project_bible": dict(project.project_bible or {}),
        "settings": dict(project.settings or {}),
        "created_at": project.created_at.isoformat() if project.created_at else None,
    }


@router.get("")
def list_projects():
    with get_session() as s:
        return [_project_dict(p) for p in ps.list_projects(s)]


@router.post("")
def create_project(body: ProjectCreate):
    with get_session() as s:
        project = ps.create_project(
            s,
            name=body.name,
            project_bible=body.project_bible.model_dump() if body.project_bible else None,
            settings=body.settings,
        )
        return _project_dict(project)


@router.get("/{project_id}")
def get_project(project_id: uuid.UUID):
    with get_session() as s:
        try:
            project = ps.get_project(s, project_id)
        except ps.ProjectNotFound:
            raise HTTPException(404, "project not found")
        base = _project_dict(project)
        base["scene_count"] = ps.project_scene_count(s, project_id)
        base["asset_count"] = ps.project_asset_count(s, project_id)
        return base


@router.patch("/{project_id}")
def update_project(project_id: uuid.UUID, body: ProjectUpdate):
    with get_session() as s:
        try:
            project = ps.update_project(
                s,
                project_id,
                name=body.name,
                settings=body.settings,
            )
        except ps.ProjectNotFound:
            raise HTTPException(404, "project not found")
        return _project_dict(project)


@router.delete("/{project_id}")
def delete_project(project_id: uuid.UUID):
    with get_session() as s:
        try:
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
