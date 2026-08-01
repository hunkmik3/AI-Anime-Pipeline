"""Change history for a production object.

    GET /api/history/{object_type}/{object_id}

The studio's whole reason for moving off Google Sheets + Discord was that
nothing left a trace: edits overwrote each other and decisions lived in chat.
Writing the trail (see ``audit_service.record_change``) only pays off if people
can *read* it, which is what this route is for — "who reassigned Ep03, and when"
answered in the UI next to the thing itself.

Deliberately not admin-only: a PM must be able to see the history of their own
project. Access is the same project-read permission as the object itself, so
this exposes nothing a caller can't already see.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException

from flowboard.db import get_session
from flowboard.db.models import Project, Scene, Series, Shot
from flowboard.routes.deps import get_optional_user
from flowboard.services import audit_service, permissions

router = APIRouter(tags=["history"])

# What can be asked about, and how to find the project that owns it (the gate).
_OBJECT_TYPES = ("project", "series", "scene", "shot")


def _project_id_for(session, object_type: str, object_id: uuid.UUID) -> uuid.UUID:
    """The project an object belongs to — 404 when it doesn't exist."""
    if object_type == "project":
        if session.get(Project, object_id) is None:
            raise HTTPException(404, "project not found")
        return object_id
    if object_type == "series":
        row = session.get(Series, object_id)
        if row is None:
            raise HTTPException(404, "series not found")
        return row.project_id
    if object_type == "scene":
        row = session.get(Scene, object_id)
        if row is None:
            raise HTTPException(404, "episode not found")
        return row.project_id
    if object_type == "shot":
        shot = session.get(Shot, object_id)
        if shot is None:
            raise HTTPException(404, "sequence not found")
        scene = session.get(Scene, shot.scene_id)
        if scene is None:
            raise HTTPException(404, "episode not found")
        return scene.project_id
    raise HTTPException(400, f"unknown object type {object_type!r}")


@router.get("/api/history/{object_type}/{object_id}")
def object_history(
    object_type: str,
    object_id: uuid.UUID,
    limit: int = 200,
    user=Depends(get_optional_user),
):
    """Everything recorded against one object, newest first."""
    if object_type not in _OBJECT_TYPES:
        raise HTTPException(400, f"unknown object type {object_type!r}")
    with get_session() as s:
        project_id = _project_id_for(s, object_type, object_id)
        # Same gate as reading the object itself — 404 for outsiders, so this
        # never confirms that an id exists to someone with no access.
        permissions.require(s, user, project_id, "canvas.read")
    return {
        "object_type": object_type,
        "object_id": str(object_id),
        "entries": audit_service.history_for(
            object_type, object_id, limit=min(max(1, limit), 1000)
        ),
    }
