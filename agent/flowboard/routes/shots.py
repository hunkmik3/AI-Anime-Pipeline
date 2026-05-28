"""Phase 2: Shot REST surface.

Scopes:
- ``/api/scenes/{scene_id}/shots`` — collection (list + create)
- ``/api/shots/{shot_id}``         — item CRUD
- ``/api/shots/{shot_id}/workflow``— snapshot-replace nodes+edges
- ``/api/shots/{shot_id}/run``     — Phase 2: status-only; Phase 7 dispatch
- ``/api/shots/{shot_id}/cancel``  — mark idle + cancel in-flight Requests
- ``/api/shots/{shot_id}/jobs``    — Request rows tied to this shot's nodes
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from flowboard.db import get_session
from flowboard.schemas import ShotCreate, ShotUpdate
from flowboard.services import scene_service as scenes
from flowboard.services import shot_service as ss

router = APIRouter(tags=["shots"])


class ShotGroupPatch(BaseModel):
    """Phase 8.3: a shot's SceneCanvas group metadata (lives in
    scene.canvas_state.shot_groups). Only the provided fields are updated."""

    position: dict[str, float] | None = None
    collapsed: bool | None = None
    label: str | None = None
    order: int | None = None


def _shot_dict(shot) -> dict:
    return {
        "id": str(shot.id),
        "scene_id": str(shot.scene_id),
        "order_index": shot.order_index,
        "script_text": shot.script_text,
        "status": shot.status,
        "current_node_id": shot.current_node_id,
        "final_video_asset_id": shot.final_video_asset_id,
        "workflow_metadata": dict(shot.workflow_metadata or {}),
        "created_at": shot.created_at.isoformat() if shot.created_at else None,
    }


class WorkflowSnapshot(BaseModel):
    # ``nodes`` and ``edges`` are intentionally loose dicts — the route
    # accepts the same shape ``GET /workflow`` emits so frontend can
    # round-trip without translation. Server-side validation lives in the
    # service.
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []


# ── Collection under scene ────────────────────────────────────────────────


@router.get("/api/scenes/{scene_id}/shots")
def list_shots(scene_id: uuid.UUID):
    with get_session() as s:
        try:
            shots = ss.list_shots(s, scene_id)
        except ss.SceneNotFound:
            raise HTTPException(404, "scene not found")
        return [_shot_dict(sh) for sh in shots]


@router.post("/api/scenes/{scene_id}/shots")
def create_shot(scene_id: uuid.UUID, body: ShotCreate):
    with get_session() as s:
        try:
            shot = ss.create_shot(
                s,
                scene_id,
                order_index=body.order_index,
                script_text=body.script_text,
            )
        except ss.SceneNotFound:
            raise HTTPException(404, "scene not found")
        return _shot_dict(shot)


# ── Item ──────────────────────────────────────────────────────────────────


@router.get("/api/shots/{shot_id}")
def get_shot(shot_id: uuid.UUID):
    with get_session() as s:
        try:
            shot = ss.get_shot(s, shot_id)
        except ss.ShotNotFound:
            raise HTTPException(404, "shot not found")
        return _shot_dict(shot)


@router.patch("/api/shots/{shot_id}")
def update_shot(shot_id: uuid.UUID, body: ShotUpdate):
    patch = body.model_dump(exclude_unset=True)
    if not patch:
        # Nothing to update — return current state without a write.
        with get_session() as s:
            try:
                shot = ss.get_shot(s, shot_id)
            except ss.ShotNotFound:
                raise HTTPException(404, "shot not found")
            return _shot_dict(shot)
    with get_session() as s:
        try:
            shot = ss.update_shot(s, shot_id, patch=patch)
        except ss.ShotNotFound:
            raise HTTPException(404, "shot not found")
        return _shot_dict(shot)


@router.patch("/api/shots/{shot_id}/group")
def update_shot_group(shot_id: uuid.UUID, body: ShotGroupPatch):
    """Phase 8.3: update a shot's SceneCanvas group metadata (position,
    collapsed, label, order) inside its parent scene's canvas_state."""
    patch = body.model_dump(exclude_unset=True)
    with get_session() as s:
        try:
            shot = ss.get_shot(s, shot_id)
        except ss.ShotNotFound:
            raise HTTPException(404, "shot not found")
        return scenes.update_shot_group(
            s,
            shot.scene_id,
            shot_id,
            position=patch.get("position"),
            collapsed=patch.get("collapsed"),
            label=patch.get("label"),
            order=patch.get("order"),
        )


@router.delete("/api/shots/{shot_id}")
def delete_shot(shot_id: uuid.UUID):
    with get_session() as s:
        try:
            ss.delete_shot(s, shot_id)
        except ss.ShotNotFound:
            raise HTTPException(404, "shot not found")
        return {"deleted": str(shot_id)}


# ── Workflow ─────────────────────────────────────────────────────────────


@router.get("/api/shots/{shot_id}/workflow")
def get_workflow(shot_id: uuid.UUID):
    with get_session() as s:
        try:
            graph = ss.get_workflow(s, shot_id)
        except ss.ShotNotFound:
            raise HTTPException(404, "shot not found")
        return graph


@router.put("/api/shots/{shot_id}/workflow")
def put_workflow(shot_id: uuid.UUID, body: WorkflowSnapshot):
    with get_session() as s:
        try:
            graph = ss.put_workflow(s, shot_id, nodes=body.nodes, edges=body.edges)
        except ss.ShotNotFound:
            raise HTTPException(404, "shot not found")
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return graph


# ── Run / cancel / jobs ───────────────────────────────────────────────────


@router.post("/api/shots/{shot_id}/run")
def run_shot(shot_id: uuid.UUID):
    """Phase 2: flips status to ``running`` and returns the shot.

    The workflow engine (DAG walk + approval-gate pause/resume) lands in
    Phase 7 behind this same route signature. Callers can already use the
    response shape today.
    """
    with get_session() as s:
        try:
            shot = ss.run_shot(s, shot_id)
        except ss.ShotNotFound:
            raise HTTPException(404, "shot not found")
        return _shot_dict(shot)


@router.post("/api/shots/{shot_id}/cancel")
def cancel_shot(shot_id: uuid.UUID):
    with get_session() as s:
        try:
            shot = ss.cancel_shot(s, shot_id)
        except ss.ShotNotFound:
            raise HTTPException(404, "shot not found")
        return _shot_dict(shot)


@router.get("/api/shots/{shot_id}/jobs")
def list_jobs(shot_id: uuid.UUID):
    with get_session() as s:
        try:
            return ss.list_shot_jobs(s, shot_id)
        except ss.ShotNotFound:
            raise HTTPException(404, "shot not found")
