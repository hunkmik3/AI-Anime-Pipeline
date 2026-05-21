"""Bootstrap a Google Flow project for a local Board (= Shot under the shim).

One-to-one: each Board's parent Project gets exactly one
``flow_project_id``. The bootstrap is idempotent — calling POST multiple
times returns the same project id without creating a new one on
labs.google.

Phase 1: URL still says "board" but ``board_id`` is now a Shot UUID; the
mapping lives keyed by the parent Project's UUID.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException

from flowboard.db import get_session
from flowboard.db.models import Project, ProjectFlowMapping, Scene, Shot
from flowboard.services.flow_sdk import get_flow_sdk, is_valid_project_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/boards", tags=["board-projects"])


def _project_for_board(session, board_id: str) -> Optional[Project]:
    try:
        shot_uuid = uuid.UUID(board_id)
    except ValueError:
        return None
    shot = session.get(Shot, shot_uuid)
    if shot is None:
        return None
    scene = session.get(Scene, shot.scene_id)
    if scene is None:
        return None
    return session.get(Project, scene.project_id)


@router.get("/{board_id}/project")
def get_board_project(board_id: str):
    with get_session() as s:
        project = _project_for_board(s, board_id)
        if project is None:
            raise HTTPException(404, "board not found")
        row = s.get(ProjectFlowMapping, project.id)
        if row is None:
            raise HTTPException(404, "no project bound to this board")
        return {"flow_project_id": row.flow_project_id, "created": False}


@router.post("/{board_id}/project")
async def ensure_board_project(board_id: str):
    # Cheap path: DB hit only.
    with get_session() as s:
        project = _project_for_board(s, board_id)
        if project is None:
            raise HTTPException(404, "board not found")
        row = s.get(ProjectFlowMapping, project.id)
        if row is not None:
            return {"flow_project_id": row.flow_project_id, "created": False}
        project_name = project.name
        project_id = project.id

    # Release the session before the extension round-trip.
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
        existing = s.get(ProjectFlowMapping, project_id)
        if existing is not None:
            return {"flow_project_id": existing.flow_project_id, "created": False}
        row = ProjectFlowMapping(project_id=project_id, flow_project_id=flow_project_id)
        s.add(row)
        s.commit()
        s.refresh(row)
        logger.info("bound project %s → flow_project %s", project_id, flow_project_id)
        return {"flow_project_id": row.flow_project_id, "created": True}
