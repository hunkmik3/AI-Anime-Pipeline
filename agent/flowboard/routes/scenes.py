"""Phase 2: Scene REST surface.

Scopes:
- ``/api/projects/{project_id}/scenes`` — collection (list + create)
- ``/api/scenes/{scene_id}``           — item (detail / update / delete)
- ``/api/scenes/{scene_id}/reorder``   — bulk reorder shots within scene
- ``/api/scenes/{scene_id}/compose``   — stubbed 501 until Phase 7

``/bible`` lives in ``routes/bibles.py`` to keep validation rules grouped.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from flowboard.db import get_session
from flowboard.routes.deps import (
    get_optional_user,
    owner_scope,
    require_structure_admin,
)
from flowboard.schemas import SceneCreate, SceneUpdate
from flowboard.services import project_service as ps
from flowboard.services import scene_service as ss

router = APIRouter(tags=["scenes"])


def _gate_project(s, project_id, user) -> None:
    """Confirm the caller may access this project tree — raises ProjectNotFound
    (→ 404) for a non-owner. Admins and the no-auth path are unscoped, so this
    is a no-op for them."""
    ps.get_project(s, project_id, owner_user_id=owner_scope(user))


def _scene_dict(scene) -> dict:
    cs = scene.canvas_state or {}
    return {
        "id": str(scene.id),
        "project_id": str(scene.project_id),
        "name": scene.name,
        "order_index": scene.order_index,
        "canvas_state": cs,
        "master_establishing_asset_id": scene.master_establishing_asset_id,
        # Cover thumbnail (user/admin-set, cosmetic); None → gradient placeholder.
        "thumb_media_id": cs.get("cover_media_id"),
        "created_at": scene.created_at.isoformat() if scene.created_at else None,
    }


class ShotReorderBody(BaseModel):
    shot_ids: list[uuid.UUID]


class SceneCoverBody(BaseModel):
    # None clears the cover (back to placeholder).
    media_id: str | None = None


# ── Collection under project ──────────────────────────────────────────────


@router.get("/api/projects/{project_id}/scenes")
def list_scenes(project_id: uuid.UUID, user=Depends(get_optional_user)):
    with get_session() as s:
        try:
            _gate_project(s, project_id, user)  # owner gate (404 if not yours)
            scenes = ss.list_scenes(s, project_id)
        except (ss.ProjectNotFound, ps.ProjectNotFound):
            raise HTTPException(404, "project not found")
        return [_scene_dict(sc) for sc in scenes]


@router.post("/api/projects/{project_id}/scenes")
def create_scene(
    project_id: uuid.UUID, body: SceneCreate, user=Depends(require_structure_admin)
):
    # Admin-only. Admin is unscoped, so they may add a scene to any user's
    # project (the project they're building out on that user's behalf).
    with get_session() as s:
        try:
            _gate_project(s, project_id, user)
            scene = ss.create_scene(
                s,
                project_id,
                name=body.name,
                order_index=body.order_index,
            )
        except (ss.ProjectNotFound, ps.ProjectNotFound):
            raise HTTPException(404, "project not found")
        return _scene_dict(scene)


# ── Item ──────────────────────────────────────────────────────────────────


@router.get("/api/scenes/{scene_id}")
def get_scene(scene_id: uuid.UUID, user=Depends(get_optional_user)):
    with get_session() as s:
        try:
            scene = ss.get_scene(s, scene_id)
            _gate_project(s, scene.project_id, user)
        except (ss.SceneNotFound, ps.ProjectNotFound):
            raise HTTPException(404, "scene not found")
        base = _scene_dict(scene)
        base["shot_count"] = ss.scene_shot_count(s, scene_id)
        return base


@router.patch("/api/scenes/{scene_id}")
def update_scene(
    scene_id: uuid.UUID, body: SceneUpdate, user=Depends(require_structure_admin)
):
    # Rename / reorder — structural, admin-only.
    with get_session() as s:
        try:
            scene = ss.get_scene(s, scene_id)
            _gate_project(s, scene.project_id, user)
            scene = ss.update_scene(
                s,
                scene_id,
                name=body.name,
                order_index=body.order_index,
            )
        except (ss.SceneNotFound, ps.ProjectNotFound):
            raise HTTPException(404, "scene not found")
        return _scene_dict(scene)


@router.post("/api/scenes/{scene_id}/cover")
def set_scene_cover(
    scene_id: uuid.UUID, body: SceneCoverBody, user=Depends(get_optional_user)
):
    # Setting a scene's cover thumbnail is cosmetic → owner-scoped (owner + admin),
    # not a structural change.
    with get_session() as s:
        try:
            scene = ss.get_scene(s, scene_id)
            _gate_project(s, scene.project_id, user)
            scene = ss.set_scene_cover(s, scene_id, body.media_id)
        except (ss.SceneNotFound, ps.ProjectNotFound):
            raise HTTPException(404, "scene not found")
        return _scene_dict(scene)


@router.delete("/api/scenes/{scene_id}")
def delete_scene(scene_id: uuid.UUID, user=Depends(require_structure_admin)):
    with get_session() as s:
        try:
            scene = ss.get_scene(s, scene_id)
            _gate_project(s, scene.project_id, user)
            ss.delete_scene(s, scene_id)
        except (ss.SceneNotFound, ps.ProjectNotFound):
            raise HTTPException(404, "scene not found")
        return {"deleted": str(scene_id)}


@router.get("/api/scenes/{scene_id}/canvas")
def get_scene_canvas(scene_id: uuid.UUID, user=Depends(get_optional_user)):
    """Phase 8.3: multi-shot SceneCanvas payload — shots + all nodes + all
    edges across the scene's shots + the persisted shot_groups layout."""
    with get_session() as s:
        try:
            scene = ss.get_scene(s, scene_id)
            _gate_project(s, scene.project_id, user)
            return ss.get_scene_canvas(s, scene_id)
        except (ss.SceneNotFound, ps.ProjectNotFound):
            raise HTTPException(404, "scene not found")


@router.post("/api/scenes/{scene_id}/auto-migrate")
def auto_migrate_canvas(scene_id: uuid.UUID, user=Depends(get_optional_user)):
    """Phase 8.3: one-time, idempotent — build canvas_state.shot_groups for a
    scene (one group per shot, default vertical-stack layout). Safe to re-run;
    never clobbers existing (user-moved) groups. Owner-scoped: it's part of
    viewing your own canvas, not a structural change."""
    with get_session() as s:
        try:
            scene = ss.get_scene(s, scene_id)
            _gate_project(s, scene.project_id, user)
            return ss.auto_migrate_canvas(s, scene_id)
        except (ss.SceneNotFound, ps.ProjectNotFound):
            raise HTTPException(404, "scene not found")


@router.post("/api/scenes/{scene_id}/reorder")
def reorder_shots(
    scene_id: uuid.UUID, body: ShotReorderBody, user=Depends(require_structure_admin)
):
    # Reordering shots is structural → admin-only.
    with get_session() as s:
        try:
            scene = ss.get_scene(s, scene_id)
            _gate_project(s, scene.project_id, user)
            shots = ss.reorder_shots(s, scene_id, body.shot_ids)
        except (ss.SceneNotFound, ps.ProjectNotFound):
            raise HTTPException(404, "scene not found")
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return [
            {
                "id": str(sh.id),
                "scene_id": str(sh.scene_id),
                "order_index": sh.order_index,
            }
            for sh in shots
        ]


@router.post("/api/scenes/{scene_id}/compose")
def compose_scene(scene_id: uuid.UUID, user=Depends(get_optional_user)):
    """Phase 7 stub.

    Returns 501 today; will trigger an ffmpeg concat of the scene's
    approved shot videos once the composition pipeline + approval flow
    land. Route shape is fixed so the frontend can be wired in Phase 3
    without churn later.
    """
    with get_session() as s:
        try:
            scene = ss.get_scene(s, scene_id)
            _gate_project(s, scene.project_id, user)
        except (ss.SceneNotFound, ps.ProjectNotFound):
            raise HTTPException(404, "scene not found")
    raise HTTPException(
        status_code=501,
        detail="scene composition not implemented yet (Phase 7)",
    )
