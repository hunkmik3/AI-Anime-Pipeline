"""Phase 10: Series REST surface — the tier between Project and Episode/Chapter.

    ``/api/projects/{project_id}/series``  — collection (list + create)
    ``/api/series/{series_id}``            — item (detail / update / delete)
    ``/api/series/{series_id}/episodes``   — the Episodes/Chapters under it

Unlike the pre-Phase-10 structure endpoints, these are **not** admin-only: a
producer or lead builds their own Series (see ``services/permissions.py``).
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from flowboard.db import get_session
from flowboard.routes.deps import get_optional_user
from flowboard.schemas import SeriesCreate, SeriesUpdate
from flowboard.services import audit_service, permissions, resource_guard
from flowboard.services import user_service
from flowboard.services import scene_service as ss
from flowboard.services import scope_budget
from flowboard.services import series_service as ses

router = APIRouter(tags=["series"])


@router.get("/api/production/crew-names")
def crew_names(user=Depends(get_optional_user)):
    """Distinct crew names across every episode — the option pool for the CRM
    crew dropdowns. Admin/console surface; returns just the sorted names."""
    with get_session() as s:
        # The pool is installation-wide (the staff roster plus every project's
        # crew), so there is no project to authorize against — and unguarded it
        # handed any caller a roster of everyone the studio employs.
        resource_guard.require_unscoped(s, user)
        return {"names": ss.distinct_crew_names(s)}


def _user_name(user_id) -> str | None:
    if not user_id:
        return None
    u = user_service.get_by_id(user_id)
    return (u.display_name or u.username) if u else None


def _series_dict(
    row,
    *,
    episode_count: int | None = None,
    stats: dict | None = None,
    budget: dict | None = None,
) -> dict:
    d = {
        "id": str(row.id),
        "project_id": str(row.project_id),
        "name": row.name,
        "code": row.code or "",
        "unit_label": row.unit_label or "Episode",
        "order_index": row.order_index,
        "production": dict(row.production or {}),
        # Phase 11: the Series Producer — first reviewer in the approver chain.
        # Exposed here so the structure UI can show and change who nominates.
        "producer_user_id": str(row.producer_user_id) if row.producer_user_id else None,
        "producer_name": _user_name(row.producer_user_id),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
    if episode_count is not None:
        d["episode_count"] = episode_count
    if stats is not None:
        d["stats"] = stats
    if budget is not None:
        d["budget"] = budget
    return d


# ── Collection under project ──────────────────────────────────────────────


@router.get("/api/projects/{project_id}/series")
def list_series(project_id: uuid.UUID, user=Depends(get_optional_user)):
    with get_session() as s:
        permissions.require(s, user, project_id, "canvas.read")
        try:
            rows = ses.list_series(s, project_id)
        except ses.ProjectNotFound:
            raise HTTPException(404, "project not found")
        # Diagram 4: ancestors of an assignment are readable, siblings are not —
        # a series holding none of the caller's work doesn't appear at all.
        scope = permissions.visible_scope(s, user, project_id)
        if scope is not None:
            rows = [r for r in rows if r.id in scope["series_ids"]]
        return [
            _series_dict(
                r,
                episode_count=ses.series_episode_count(s, r.id),
                stats=ses.series_stats(s, r.id),
                budget=scope_budget.summary(s, "series", r.id),
            )
            for r in rows
        ]


@router.post("/api/projects/{project_id}/series")
def create_series(
    project_id: uuid.UUID,
    body: SeriesCreate,
    request: Request,
    user=Depends(get_optional_user),
):
    with get_session() as s:
        permissions.require(s, user, project_id, "series.create")
        # `producer` isn't asked for in the form — whoever creates the series is
        # its producer (they already hold that role on the project). Recorded
        # here so the CRM column is populated without a second declaration.
        production = dict(body.production or {})
        if not production.get("producer") and user is not None:
            production["producer"] = getattr(user, "display_name", None) or user.username
        try:
            row = ses.create_series(
                s,
                project_id,
                name=body.name,
                code=body.code,
                unit_label=body.unit_label,
                order_index=body.order_index,
                production=production,
            )
        except ses.ProjectNotFound:
            raise HTTPException(404, "project not found")
        audit_service.record_change(
            "series.created",
            object_type="series",
            object_id=row.id,
            object_label=row.code or row.name,
            actor=user,
            ip=audit_service.client_ip(request),
            note=f"in project {project_id}",
        )
        return _series_dict(row, episode_count=0, stats=ses.series_stats(s, row.id))


# ── Item ──────────────────────────────────────────────────────────────────


@router.get("/api/series/{series_id}")
def get_series(series_id: uuid.UUID, user=Depends(get_optional_user)):
    with get_session() as s:
        try:
            row = ses.get_series(s, series_id)
        except ses.SeriesNotFound:
            raise HTTPException(404, "series not found")
        permissions.require(s, user, row.project_id, "canvas.read")
        if not permissions.can_see_series(s, user, row.project_id, series_id):
            raise HTTPException(404, "series not found")
        return _series_dict(row, episode_count=ses.series_episode_count(s, series_id))


@router.patch("/api/series/{series_id}")
def update_series(
    series_id: uuid.UUID,
    body: SeriesUpdate,
    request: Request,
    user=Depends(get_optional_user),
):
    with get_session() as s:
        try:
            row = ses.get_series(s, series_id)
            permissions.require(s, user, row.project_id, "series.update")
            before = {
                "name": row.name,
                "code": row.code,
                "unit_label": row.unit_label,
                "order_index": row.order_index,
            }
            before_prod = dict(row.production or {})
            row = ses.update_series(
                s,
                series_id,
                name=body.name,
                code=body.code,
                unit_label=body.unit_label,
                order_index=body.order_index,
                production=body.production,
            )
        except ses.SeriesNotFound:
            raise HTTPException(404, "series not found")
        # The ~27 CRM fields live in a JSONB bag, so without this they would be
        # overwritten silently — the exact Google-Sheet failure this replaces.
        changes = {k: (v, getattr(row, k)) for k, v in before.items()}
        after_prod = dict(row.production or {})
        for key in set(before_prod) | set(after_prod):
            changes[f"production.{key}"] = (before_prod.get(key), after_prod.get(key))
        audit_service.record_change(
            "series.updated",
            object_type="series",
            object_id=series_id,
            object_label=row.code or row.name,
            changes=changes,
            actor=user,
            ip=audit_service.client_ip(request),
        )
        return _series_dict(
            row,
            episode_count=ses.series_episode_count(s, series_id),
            stats=ses.series_stats(s, series_id),
        )


@router.delete("/api/series/{series_id}")
def delete_series(
    series_id: uuid.UUID, request: Request, user=Depends(get_optional_user)
):
    with get_session() as s:
        try:
            row = ses.get_series(s, series_id)
            permissions.require(s, user, row.project_id, "series.delete")
            # A series holding episodes is never deleted implicitly — that would
            # take a season of generated work with it. Caller must move or delete
            # the episodes first.
            count = ses.series_episode_count(s, series_id)
            if count:
                raise HTTPException(
                    409,
                    f"series still has {count} episode(s) — move or delete them first",
                )
            label = row.code or row.name
            ses.delete_series(s, series_id)
        except ses.SeriesNotFound:
            raise HTTPException(404, "series not found")
        audit_service.record_change(
            "series.deleted",
            object_type="series",
            object_id=series_id,
            object_label=label,
            actor=user,
            ip=audit_service.client_ip(request),
            note="deleted",
        )
        return {"deleted": str(series_id)}


class GenerateStructureBody(BaseModel):
    # Bounded so a stray value can't create a runaway number of rows.
    episodes: int = Field(ge=0, le=2000)
    sequences_per_episode: int = Field(ge=0, le=200)


@router.post("/api/series/{series_id}/generate-structure")
def generate_structure(
    series_id: uuid.UUID, body: GenerateStructureBody, user=Depends(get_optional_user)
):
    """Plan out a series' episodes + sequences in one go (idempotent; only
    creates what's missing). Needs episode.create (lead+) since it builds
    structure. The created rows are ordinary Episodes/Sequences, so they show
    up on the project home immediately and edit both ways."""
    with get_session() as s:
        try:
            row = ses.get_series(s, series_id)
        except ses.SeriesNotFound:
            raise HTTPException(404, "series not found")
        permissions.require(s, user, row.project_id, "episode.create")
        return ses.generate_structure(
            s,
            series_id,
            episodes=body.episodes,
            sequences_per_episode=body.sequences_per_episode,
        )


@router.get("/api/series/{series_id}/episodes")
def list_series_episodes(series_id: uuid.UUID, user=Depends(get_optional_user)):
    """The Episodes/Chapters under a series, ordered. Same payload shape as
    ``GET /api/projects/{id}/scenes`` so the frontend reuses one type."""
    from flowboard.routes.scenes import _scene_dict

    with get_session() as s:
        try:
            row = ses.get_series(s, series_id)
        except ses.SeriesNotFound:
            raise HTTPException(404, "series not found")
        permissions.require(s, user, row.project_id, "canvas.read")
        scenes = ss.list_scenes(s, row.project_id, series_id=series_id)
        scope = permissions.visible_scope(s, user, row.project_id)
        if scope is not None:
            scenes = [sc for sc in scenes if sc.id in scope["scene_ids"]]
        return [_scene_dict(s, sc) for sc in scenes]
