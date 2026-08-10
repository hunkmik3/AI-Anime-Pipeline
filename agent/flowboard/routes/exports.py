"""CSV exports.

    GET /api/export/projects/{id}/episodes    the episode tracker
    GET /api/export/projects/{id}/submissions the delivery + review history
    GET /api/export/projects/{id}/spend       credit spend per episode
    GET /api/export/history/{type}/{id}       one object's change trail

This exists to stop the spreadsheet coming back. When a producer needs an
overview the app can't render, the fallback is to retype it into a Sheet — and
that copy immediately drifts from reality, which is the failure the app was built
to end. An export keeps the app the single source and the Sheet a snapshot.

Producer-level, and the rows respect the caller's visibility scope, so an export
can never become a way around diagram 4.
"""
from __future__ import annotations

import csv
import io
import uuid
from typing import Iterable, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from flowboard.db import get_session
from flowboard.db.models import Project, Scene, Series, Submission, User
from flowboard.routes.deps import get_optional_user
from flowboard.services import audit_service, permissions, scope_budget
from flowboard.services import submission_service as subs
from flowboard.services import user_service

router = APIRouter(prefix="/api/export", tags=["export"])


def _csv_response(filename: str, header: list[str], rows: Iterable[list]) -> StreamingResponse:
    buf = io.StringIO()
    # utf-8-sig: Excel misreads plain UTF-8 CSV, and these files carry Vietnamese
    # names and titles. Without the BOM they open as mojibake.
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow(["" if v is None else v for v in r])
    data = "﻿" + buf.getvalue()
    return StreamingResponse(
        iter([data]),
        media_type="text/csv; charset=utf-8",
        headers={"content-disposition": f'attachment; filename="{filename}"'},
    )


def _gate(session, project_id: uuid.UUID, user) -> None:
    if session.get(Project, project_id) is None:
        raise HTTPException(404, "project not found")
    permissions.require(session, user, project_id, "member.manage")


def _name(user_id) -> Optional[str]:
    if not user_id:
        return None
    u = user_service.get_by_id(user_id)
    return (u.display_name or u.username) if u else None


def _visible_episodes(session, project_id: uuid.UUID, user) -> list[Scene]:
    """Episodes in the project the caller may see, oldest series first."""
    from sqlmodel import select

    rows = list(session.exec(select(Scene).where(Scene.project_id == project_id)).all())
    scope = permissions.visible_scope(session, user, project_id)
    if scope is not None:
        rows = [r for r in rows if r.id in scope["scene_ids"]]
    return sorted(rows, key=lambda r: (str(r.series_id or ""), r.order_index, r.name))


@router.get("/projects/{project_id}/episodes")
def export_episodes(project_id: uuid.UUID, user=Depends(get_optional_user)):
    """The episode tracker: who owns each episode, where it stands, what it cost."""
    with get_session() as s:
        _gate(s, project_id, user)
        project = s.get(Project, project_id)
        series_names = {
            r.id: (r.code or r.name) for r in s.exec(_select_series(project_id)).all()
        }
        eps = _visible_episodes(s, project_id, user)
        delivery = subs.delivery_status_map(s, eps)
        rows = []
        for ep in eps:
            budget = scope_budget.summary(s, "scene", ep.id)
            rows.append(
                [
                    project.name if project else "",
                    series_names.get(ep.series_id, ""),
                    ep.code or "",
                    ep.name,
                    # The series' state: an episode is not handed in on its own.
                    delivery.get(ep.id, "draft"),
                    _name(ep.assignee_user_id),
                    budget["base_usd"] or "",
                    budget["spent_usd"],
                    "" if budget["remaining_usd"] is None else budget["remaining_usd"],
                    ep.created_at.isoformat() if ep.created_at else "",
                ]
            )
    return _csv_response(
        f"episodes-{project_id}.csv",
        [
            "project", "series", "code", "episode", "status", "assignee",
            "budget_usd", "spent_usd", "remaining_usd", "created_at",
        ],
        rows,
    )


def _select_series(project_id: uuid.UUID):
    from sqlmodel import select

    return select(Series).where(Series.project_id == project_id)


@router.get("/projects/{project_id}/submissions")
def export_submissions(project_id: uuid.UUID, user=Depends(get_optional_user)):
    """Every delivery attempt and its verdict — the record Discord never kept."""
    from sqlmodel import select

    with get_session() as s:
        _gate(s, project_id, user)
        # Delivery is per SERIES, so the export is too. Scoped through the
        # episodes the caller may see: a series they can see nothing of must not
        # hand them its drive links and rejection reasons.
        visible_series = {
            e.series_id for e in _visible_episodes(s, project_id, user) if e.series_id
        }
        names = {
            r.id: (r.code or r.name)
            for r in s.exec(_select_series(project_id)).all()
        }
        if not visible_series:
            rows_in: list[Submission] = []
        else:
            rows_in = list(
                s.exec(
                    select(Submission)
                    .where(Submission.series_id.in_(list(visible_series)))  # type: ignore[attr-defined]
                    .order_by(Submission.submitted_at)  # type: ignore[attr-defined]
                ).all()
            )
        rows = [
            [
                names.get(sub.series_id, ""),
                sub.version,
                sub.status,
                _name(sub.submitted_by),
                sub.submitted_at.isoformat() if sub.submitted_at else "",
                _name(sub.approver_user_id),
                _name(sub.reviewed_by),
                sub.reviewed_at.isoformat() if sub.reviewed_at else "",
                sub.review_note or "",
                sub.note or "",
                sub.drive_url or "",
            ]
            for sub in rows_in
        ]
    return _csv_response(
        f"submissions-{project_id}.csv",
        [
            "series", "version", "status", "submitted_by", "submitted_at",
            "assigned_reviewer", "reviewed_by", "reviewed_at", "review_note",
            "submitter_note", "drive_url",
        ],
        rows,
    )


@router.get("/projects/{project_id}/spend")
def export_spend(project_id: uuid.UUID, user=Depends(get_optional_user)):
    """Credit spend per episode against its ceiling — the finance view."""
    with get_session() as s:
        _gate(s, project_id, user)
        series_names = {
            r.id: (r.code or r.name) for r in s.exec(_select_series(project_id)).all()
        }
        rows = []
        for ep in _visible_episodes(s, project_id, user):
            b = scope_budget.summary(s, "scene", ep.id)
            rows.append(
                [
                    series_names.get(ep.series_id, ""),
                    ep.code or ep.name,
                    _name(ep.assignee_user_id),
                    b["base_usd"] or "",
                    b["granted_usd"],
                    b["effective_usd"],
                    b["spent_usd"],
                    b["reserved_usd"],
                    "" if b["remaining_usd"] is None else b["remaining_usd"],
                    "yes" if b["unlimited"] else "no",
                ]
            )
    return _csv_response(
        f"spend-{project_id}.csv",
        [
            "series", "episode", "assignee", "budget_usd", "granted_usd",
            "effective_usd", "spent_usd", "reserved_usd", "remaining_usd",
            "unlimited",
        ],
        rows,
    )


@router.get("/history/{object_type}/{object_id}")
def export_history(
    object_type: str, object_id: uuid.UUID, user=Depends(get_optional_user)
):
    """One object's change trail as CSV — for handing to someone outside the app."""
    from flowboard.routes.history import _OBJECT_TYPES, _project_id_for

    if object_type not in _OBJECT_TYPES:
        raise HTTPException(400, f"unknown object type {object_type!r}")
    with get_session() as s:
        project_id = _project_id_for(s, object_type, object_id)
        permissions.require(s, user, project_id, "member.manage")
    entries = audit_service.history_for(object_type, object_id, limit=1000)
    rows = [
        [e["created_at"], e["action"], e["actor"], e["target"], e["detail"], e["ip"]]
        for e in entries
    ]
    return _csv_response(
        f"history-{object_type}-{object_id}.csv",
        ["when", "action", "who", "target", "change", "ip"],
        rows,
    )
