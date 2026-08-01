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

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from flowboard.db import get_session
from flowboard.routes.deps import get_optional_user
from flowboard.schemas import SceneCreate, SceneUpdate
from flowboard.services import audit_service, permissions
from flowboard.services import user_service
from flowboard.services import project_service as ps
from flowboard.services import scene_service as ss

router = APIRouter(tags=["scenes"])


def _gate_project(s, project_id, user, capability: str = "canvas.read") -> str:
    """Confirm the caller may do ``capability`` on this project tree.

    Phase 10: structure is no longer admin-only — a producer/lead builds their
    own Episodes. 404 when the caller can't see the project at all, 403 when
    their role is too low. Admins and the no-auth path pass everything.
    """
    return permissions.require(s, user, project_id, capability)


def _gate_scene(s, scene, user, capability: str = "canvas.read") -> str:
    """``_gate_project`` plus the episode visibility scope — an artist may only
    reach the episodes they're assigned to (see permissions.visible_scope)."""
    return permissions.require_scene(s, user, scene.project_id, scene.id, capability)


def _user_name(user_id) -> str | None:
    if not user_id:
        return None
    u = user_service.get_by_id(user_id)
    return (u.display_name or u.username) if u else None


def _scene_dict(scene) -> dict:
    cs = scene.canvas_state or {}
    return {
        "id": str(scene.id),
        "project_id": str(scene.project_id),
        "series_id": str(scene.series_id) if scene.series_id else None,
        "name": scene.name,
        "code": scene.code or "",
        "order_index": scene.order_index,
        "production": dict(scene.production or {}),
        # Phase 11: who owns this episode (the only person who may submit it)
        # and where its deliverable stands. Exposed so the structure UI can
        # assign it — without this the whole submit flow has no entry point.
        "assignee_user_id": str(scene.assignee_user_id) if scene.assignee_user_id else None,
        "assignee_name": _user_name(scene.assignee_user_id),
        "deliverable_status": scene.deliverable_status or "draft",
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
def list_scenes(
    project_id: uuid.UUID,
    series_id: uuid.UUID | None = None,
    user=Depends(get_optional_user),
):
    with get_session() as s:
        try:
            _gate_project(s, project_id, user)  # access gate (404 if not yours)
            scenes = ss.list_scenes(s, project_id, series_id=series_id)
        except (ss.ProjectNotFound, ps.ProjectNotFound):
            raise HTTPException(404, "project not found")
        # Diagram 4: an artist sees the episodes they're assigned, not their
        # colleagues'. None = unrestricted (admin / producer / lead).
        scope = permissions.visible_scope(s, user, project_id)
        if scope is not None:
            scenes = [sc for sc in scenes if sc.id in scope["scene_ids"]]
        return [_scene_dict(sc) for sc in scenes]


@router.post("/api/projects/{project_id}/scenes")
def create_scene(
    project_id: uuid.UUID,
    body: SceneCreate,
    request: Request,
    user=Depends(get_optional_user),
):
    # Phase 10: lead+ on the project (not admin-only). `series_id` omitted →
    # the project's first / auto-created "Default" series.
    with get_session() as s:
        try:
            _gate_project(s, project_id, user, "episode.create")
            scene = ss.create_scene(
                s,
                project_id,
                name=body.name,
                series_id=body.series_id,
                code=body.code,
                order_index=body.order_index,
            )
        except (ss.ProjectNotFound, ps.ProjectNotFound):
            raise HTTPException(404, "project not found")
        audit_service.record_change(
            "episode.created",
            object_type="scene",
            object_id=scene.id,
            object_label=scene.code or scene.name,
            actor=user,
            ip=audit_service.client_ip(request),
            note=f"in project {project_id}",
        )
        return _scene_dict(scene)


# ── Item ──────────────────────────────────────────────────────────────────


@router.get("/api/scenes/{scene_id}")
def get_scene(scene_id: uuid.UUID, user=Depends(get_optional_user)):
    with get_session() as s:
        try:
            scene = ss.get_scene(s, scene_id)
            _gate_scene(s, scene, user)
        except (ss.SceneNotFound, ps.ProjectNotFound):
            raise HTTPException(404, "scene not found")
        base = _scene_dict(scene)
        base["shot_count"] = ss.scene_shot_count(s, scene_id)
        return base


@router.patch("/api/scenes/{scene_id}")
def update_scene(
    scene_id: uuid.UUID,
    body: SceneUpdate,
    request: Request,
    user=Depends(get_optional_user),
):
    # Rename / recode / move between series / reorder — lead+ on the project.
    with get_session() as s:
        try:
            scene = ss.get_scene(s, scene_id)
            _gate_scene(s, scene, user, "episode.update")
            before = {
                "name": scene.name,
                "code": scene.code,
                "series_id": scene.series_id,
                "order_index": scene.order_index,
            }
            scene = ss.update_scene(
                s,
                scene_id,
                name=body.name,
                series_id=body.series_id,
                code=body.code,
                order_index=body.order_index,
                production=body.production,
            )
        except (ss.SceneNotFound, ps.ProjectNotFound):
            raise HTTPException(404, "scene not found")
        audit_service.record_change(
            "episode.updated",
            object_type="scene",
            object_id=scene_id,
            object_label=scene.code or scene.name,
            changes={k: (v, getattr(scene, k)) for k, v in before.items()},
            actor=user,
            ip=audit_service.client_ip(request),
        )
        return _scene_dict(scene)


@router.post("/api/scenes/{scene_id}/cover")
def set_scene_cover(
    scene_id: uuid.UUID, body: SceneCoverBody, user=Depends(get_optional_user)
):
    # Setting a scene's cover thumbnail is cosmetic → anyone who works in the
    # project, not a structural change.
    with get_session() as s:
        try:
            scene = ss.get_scene(s, scene_id)
            _gate_scene(s, scene, user, "project.decorate")
            scene = ss.set_scene_cover(s, scene_id, body.media_id)
        except (ss.SceneNotFound, ps.ProjectNotFound):
            raise HTTPException(404, "scene not found")
        return _scene_dict(scene)


@router.delete("/api/scenes/{scene_id}")
def delete_scene(
    scene_id: uuid.UUID, request: Request, user=Depends(get_optional_user)
):
    with get_session() as s:
        try:
            scene = ss.get_scene(s, scene_id)
            _gate_scene(s, scene, user, "episode.delete")
            label = scene.code or scene.name
            ss.delete_scene(s, scene_id)
        except (ss.SceneNotFound, ps.ProjectNotFound):
            raise HTTPException(404, "scene not found")
        # Deleting an episode takes its sequences and generated work with it —
        # the single most destructive action in the app, so it must leave a mark.
        audit_service.record_change(
            "episode.deleted",
            object_type="scene",
            object_id=scene_id,
            object_label=label,
            actor=user,
            ip=audit_service.client_ip(request),
            note="deleted",
        )
        return {"deleted": str(scene_id)}


@router.get("/api/scenes/{scene_id}/canvas")
def get_scene_canvas(scene_id: uuid.UUID, user=Depends(get_optional_user)):
    """Phase 8.3: multi-shot SceneCanvas payload — shots + all nodes + all
    edges across the scene's shots + the persisted shot_groups layout."""
    with get_session() as s:
        try:
            scene = ss.get_scene(s, scene_id)
            _gate_scene(s, scene, user)
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
            _gate_scene(s, scene, user)
            return ss.auto_migrate_canvas(s, scene_id)
        except (ss.SceneNotFound, ps.ProjectNotFound):
            raise HTTPException(404, "scene not found")


@router.post("/api/scenes/{scene_id}/reorder")
def reorder_shots(
    scene_id: uuid.UUID, body: ShotReorderBody, user=Depends(get_optional_user)
):
    # Reordering sequences within an episode → lead+ (an artist owns a single
    # sequence, not the running order).
    with get_session() as s:
        try:
            scene = ss.get_scene(s, scene_id)
            _gate_scene(s, scene, user, "episode.update")
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
            _gate_scene(s, scene, user)
        except (ss.SceneNotFound, ps.ProjectNotFound):
            raise HTTPException(404, "scene not found")
    raise HTTPException(
        status_code=501,
        detail="scene composition not implemented yet (Phase 7)",
    )
