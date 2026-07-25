"""Phase 10: Series REST surface — the tier between Project and Episode/Chapter.

    ``/api/projects/{project_id}/series``  — collection (list + create)
    ``/api/series/{series_id}``            — item (detail / update / delete)
    ``/api/series/{series_id}/episodes``   — the Episodes/Chapters under it

Unlike the pre-Phase-10 structure endpoints, these are **not** admin-only: a
producer or lead builds their own Series (see ``services/permissions.py``).
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from flowboard.db import get_session
from flowboard.routes.deps import get_optional_user
from flowboard.schemas import SeriesCreate, SeriesUpdate
from flowboard.services import permissions
from flowboard.services import scene_service as ss
from flowboard.services import series_service as ses

router = APIRouter(tags=["series"])


@router.get("/api/production/crew-names")
def crew_names(user=Depends(get_optional_user)):
    """Distinct crew names across every episode — the option pool for the CRM
    crew dropdowns. Admin/console surface; returns just the sorted names."""
    with get_session() as s:
        return {"names": ss.distinct_crew_names(s)}


def _series_dict(row, *, episode_count: int | None = None, stats: dict | None = None) -> dict:
    d = {
        "id": str(row.id),
        "project_id": str(row.project_id),
        "name": row.name,
        "code": row.code or "",
        "unit_label": row.unit_label or "Episode",
        "order_index": row.order_index,
        "production": dict(row.production or {}),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
    if episode_count is not None:
        d["episode_count"] = episode_count
    if stats is not None:
        d["stats"] = stats
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
        return [
            _series_dict(
                r,
                episode_count=ses.series_episode_count(s, r.id),
                stats=ses.series_stats(s, r.id),
            )
            for r in rows
        ]


@router.post("/api/projects/{project_id}/series")
def create_series(
    project_id: uuid.UUID, body: SeriesCreate, user=Depends(get_optional_user)
):
    with get_session() as s:
        permissions.require(s, user, project_id, "series.create")
        try:
            row = ses.create_series(
                s,
                project_id,
                name=body.name,
                code=body.code,
                unit_label=body.unit_label,
                order_index=body.order_index,
                production=body.production,
            )
        except ses.ProjectNotFound:
            raise HTTPException(404, "project not found")
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
        return _series_dict(row, episode_count=ses.series_episode_count(s, series_id))


@router.patch("/api/series/{series_id}")
def update_series(
    series_id: uuid.UUID, body: SeriesUpdate, user=Depends(get_optional_user)
):
    with get_session() as s:
        try:
            row = ses.get_series(s, series_id)
            permissions.require(s, user, row.project_id, "series.update")
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
        return _series_dict(
            row,
            episode_count=ses.series_episode_count(s, series_id),
            stats=ses.series_stats(s, series_id),
        )


@router.delete("/api/series/{series_id}")
def delete_series(series_id: uuid.UUID, user=Depends(get_optional_user)):
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
            ses.delete_series(s, series_id)
        except ses.SeriesNotFound:
            raise HTTPException(404, "series not found")
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
        return [_scene_dict(sc) for sc in scenes]
