"""KPI rollups — delivery and rework measured from the recorded history.

    GET /api/kpi/overview                whole-app rollup, every project
    GET /api/kpi/projects/{project_id}   per-person rollup across one project
    GET /api/kpi/series/{series_id}      one series: completion + per-person

**Admin only.** The studio treats this as a tracker for the board: it reports
across the whole installation and only admins and BOD members see it. A PM
running one project deliberately does NOT get it — comparing colleagues' output
is not information the app hands to a peer.

There is no separate BOD system role (accounts are ``admin`` or ``user``), so a
board member is given an admin account. If BOD ever needs to be distinguishable
from IT-admin, that is a new role rather than a change here.

See ``kpi_service`` for what each measure means and, importantly, what is
deliberately not measured.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException

from flowboard.db import get_session
from flowboard.db.models import Project, Series
from flowboard.routes.deps import get_optional_user
from flowboard.services import kpi_service, resource_guard

router = APIRouter(prefix="/api/kpi", tags=["kpi"])


@router.get("/overview")
def overview_kpi(user=Depends(get_optional_user)):
    """Every project at once — the board's view of the whole studio."""
    with get_session() as s:
        resource_guard.require_unscoped(s, user)
        return kpi_service.overview(s)


@router.get("/projects/{project_id}")
def project_kpi(project_id: uuid.UUID, user=Depends(get_optional_user)):
    with get_session() as s:
        resource_guard.require_unscoped(s, user)
        if s.get(Project, project_id) is None:
            raise HTTPException(404, "project not found")
        return {
            "project_id": str(project_id),
            "people": kpi_service.people(s, project_id=project_id),
        }


@router.get("/series/{series_id}")
def series_kpi(series_id: uuid.UUID, user=Depends(get_optional_user)):
    with get_session() as s:
        resource_guard.require_unscoped(s, user)
        row = s.get(Series, series_id)
        if row is None:
            raise HTTPException(404, "series not found")
        return kpi_service.series_rollup(s, series_id)
