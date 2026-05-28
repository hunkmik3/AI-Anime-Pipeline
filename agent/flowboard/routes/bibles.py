"""Project Bible CRUD + Scene establishing-asset pointer.

Phase 8.3: Scene Bible (text) was removed; the ``/scenes/{id}/bible``
endpoint now manages only ``master_establishing_asset_id`` (the MasterShot
reference), kept under the same path for frontend compatibility.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException

from flowboard.db import get_session
from flowboard.schemas import ProjectBible, SceneEstablishing
from flowboard.services import project_service as ps
from flowboard.services import scene_service as ss

router = APIRouter(tags=["bibles"])


# ── Project Bible ────────────────────────────────────────────────────────


@router.get("/api/projects/{project_id}/bible")
def get_project_bible(project_id: uuid.UUID):
    with get_session() as s:
        try:
            return ps.get_project_bible(s, project_id)
        except ps.ProjectNotFound:
            raise HTTPException(404, "project not found")


@router.put("/api/projects/{project_id}/bible")
def put_project_bible(project_id: uuid.UUID, body: ProjectBible):
    with get_session() as s:
        try:
            return ps.put_project_bible(s, project_id, body.model_dump())
        except ps.ProjectNotFound:
            raise HTTPException(404, "project not found")


# ── Scene establishing asset (MasterShot reference) ───────────────────────


@router.get("/api/scenes/{scene_id}/bible")
def get_scene_establishing(scene_id: uuid.UUID):
    with get_session() as s:
        try:
            return ss.get_scene_establishing(s, scene_id)
        except ss.SceneNotFound:
            raise HTTPException(404, "scene not found")


@router.put("/api/scenes/{scene_id}/bible")
def put_scene_establishing(scene_id: uuid.UUID, body: SceneEstablishing):
    with get_session() as s:
        try:
            return ss.put_scene_establishing(
                s,
                scene_id,
                master_establishing_asset_id=body.master_establishing_asset_id,
            )
        except ss.SceneNotFound:
            raise HTTPException(404, "scene not found")
        except ss.InvalidBibleAsset as exc:
            raise HTTPException(400, str(exc))
