"""Phase 2: Project Bible + Scene Bible CRUD.

Lives in its own router so the strict Pydantic validation (extra=forbid)
is grouped — both bibles are JSONB columns and easy to drift if their
shape is enforced ad-hoc inside the parent routes.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException

from flowboard.db import get_session
from flowboard.schemas import ProjectBible, SceneBible
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


# ── Scene Bible ──────────────────────────────────────────────────────────


@router.get("/api/scenes/{scene_id}/bible")
def get_scene_bible(scene_id: uuid.UUID):
    with get_session() as s:
        try:
            return ss.get_scene_bible(s, scene_id)
        except ss.SceneNotFound:
            raise HTTPException(404, "scene not found")


@router.put("/api/scenes/{scene_id}/bible")
def put_scene_bible(scene_id: uuid.UUID, body: SceneBible):
    with get_session() as s:
        try:
            return ss.put_scene_bible(
                s,
                scene_id,
                scene_bible_text=body.scene_bible_text,
                master_establishing_asset_id=body.master_establishing_asset_id,
            )
        except ss.SceneNotFound:
            raise HTTPException(404, "scene not found")
        except ss.InvalidBibleAsset as exc:
            raise HTTPException(400, str(exc))
