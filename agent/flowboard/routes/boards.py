"""Legacy `/api/boards/*` endpoints — Phase 1 shim.

Each "Board" is now backed by a Project + one Scene + one Shot under the
hood. The board.id returned to callers is the Shot's UUID (as a string),
which is what `/api/nodes` and `/api/edges` accept as `shot_id`.

Phase 2 will introduce real `/api/projects`, `/api/scenes`, `/api/shots`
routes and deprecate this module. Until then, this shim lets the existing
frontend and tests keep working unchanged.
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlmodel import select

from flowboard.db import get_session
from flowboard.db.models import (
    Edge,
    Node,
    PipelineRun,
    Plan,
    PlanRevision,
    Project,
    ProjectFlowMapping,
    Scene,
    Shot,
)

router = APIRouter(prefix="/api/boards", tags=["boards"])


class BoardCreate(BaseModel):
    name: str


class BoardUpdate(BaseModel):
    name: str


def _board_view(project: Project, shot: Shot) -> dict:
    """Project + Shot rendered in the legacy Board response shape."""
    return {
        "id": str(shot.id),
        "name": project.name,
        "created_at": shot.created_at.isoformat() if shot.created_at else None,
        "project_id": str(project.id),
    }


def _parse_uuid(raw: str) -> Optional[uuid.UUID]:
    try:
        return uuid.UUID(str(raw))
    except (ValueError, TypeError, AttributeError):
        return None


def _resolve_shot(session, board_id: str) -> Optional[tuple[Shot, Scene, Project]]:
    sid = _parse_uuid(board_id)
    if sid is None:
        return None
    shot = session.get(Shot, sid)
    if shot is None:
        return None
    scene = session.get(Scene, shot.scene_id)
    if scene is None:
        return None
    project = session.get(Project, scene.project_id)
    if project is None:
        return None
    return shot, scene, project


@router.get("")
def list_boards():
    """List one Board per Shot. Shots are ordered by created_at (oldest first)."""
    with get_session() as s:
        shots = list(s.exec(select(Shot).order_by(Shot.created_at, Shot.id)).all())
        out: list[dict] = []
        for shot in shots:
            scene = s.get(Scene, shot.scene_id)
            if scene is None:
                continue
            project = s.get(Project, scene.project_id)
            if project is None:
                continue
            out.append(_board_view(project, shot))
        return out


@router.post("")
def create_board(body: BoardCreate):
    """Create a Project + one default Scene + one default Shot.

    Returned `id` is the Shot's UUID — this is the value `/api/nodes` and
    `/api/edges` accept as `shot_id`.
    """
    with get_session() as s:
        project = Project(name=body.name)
        s.add(project)
        s.flush()
        scene = Scene(project_id=project.id, name="Scene 1", order_index=0)
        s.add(scene)
        s.flush()
        shot = Shot(scene_id=scene.id, order_index=0)
        s.add(shot)
        s.commit()
        s.refresh(project)
        s.refresh(shot)
        return _board_view(project, shot)


@router.get("/{board_id}")
def get_board(board_id: str):
    with get_session() as s:
        resolved = _resolve_shot(s, board_id)
        if resolved is None:
            raise HTTPException(404, "board not found")
        shot, _scene, project = resolved
        nodes = list(s.exec(select(Node).where(Node.shot_id == shot.id)).all())
        edges = list(s.exec(select(Edge).where(Edge.shot_id == shot.id)).all())
        return {
            "board": _board_view(project, shot),
            "nodes": nodes,
            "edges": edges,
        }


@router.patch("/{board_id}")
def update_board(board_id: str, body: BoardUpdate):
    with get_session() as s:
        resolved = _resolve_shot(s, board_id)
        if resolved is None:
            raise HTTPException(404, "board not found")
        shot, _scene, project = resolved
        project.name = body.name
        s.add(project)
        s.commit()
        s.refresh(project)
        return _board_view(project, shot)


@router.delete("/{board_id}")
def delete_board(board_id: str):
    """Cascade-delete the entire Project tree.

    Each legacy Board is 1:1 with a Project under the shim, so deleting a
    Board removes the project + all its scenes/shots/nodes/edges/chats/
    plans/runs/assets. Postgres FK ``ON DELETE CASCADE`` handles most of
    the cascade; Plan/Pipeline rows hang off Shot which CASCADEs through
    Scene → Project.
    """
    with get_session() as s:
        resolved = _resolve_shot(s, board_id)
        if resolved is None:
            raise HTTPException(404, "board not found")
        _shot, _scene, project = resolved

        # Plan + PlanRevision + PipelineRun still cascade via Shot.id, but
        # we explicitly clear them first so the test_delete_board_cascades
        # assertions can find them gone even if the CASCADE timing is
        # opaque in a transaction.
        shot_ids = [
            row.id
            for row in s.exec(
                select(Shot).join(Scene).where(Scene.project_id == project.id)
            ).all()
        ]
        if shot_ids:
            plan_ids = [
                p.id
                for p in s.exec(select(Plan).where(Plan.shot_id.in_(shot_ids))).all()  # type: ignore[attr-defined]
            ]
            if plan_ids:
                for prv in s.exec(
                    select(PlanRevision).where(PlanRevision.plan_id.in_(plan_ids))  # type: ignore[attr-defined]
                ).all():
                    s.delete(prv)
                for run in s.exec(
                    select(PipelineRun).where(PipelineRun.plan_id.in_(plan_ids))  # type: ignore[attr-defined]
                ).all():
                    s.delete(run)
                for pl in s.exec(
                    select(Plan).where(Plan.id.in_(plan_ids))  # type: ignore[attr-defined]
                ).all():
                    s.delete(pl)

        # Clear ProjectFlowMapping if present (CASCADE would handle it,
        # but explicit delete keeps the assertion simple).
        mapping = s.get(ProjectFlowMapping, project.id)
        if mapping is not None:
            s.delete(mapping)

        s.delete(project)
        s.commit()
        return {"deleted": board_id}
