"""Phase 10 — Series CRUD (the tier between Project and Episode/Chapter)."""
from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlmodel import Session, func, select

from flowboard.db.models import Project, Scene, Series

UNIT_LABELS = ("Episode", "Chapter")

# ── Production-tracking metadata (Series_Master spreadsheet columns) ─────────
# Stored in Series.production (a JSONB bag). Order is the natural form order;
# numeric ones are coerced to int. Unknown keys are dropped.
SERIES_PROD_FIELDS: tuple[str, ...] = (
    "full_code",
    "tier",
    "status",              # Planning | On-going | Completed | Cancelled
    "priority",            # High | Medium | Low
    "start_date",
    "end_date",
    "folder_link",
    "genres",
    "tropes",
    "target_market",
    "secondary_markets",
    "target_audience",
    "language_original",
    "logline",
    "total_episodes_planned",
    "episode_duration_sec",
)
_SERIES_INT_FIELDS = {"total_episodes_planned", "episode_duration_sec"}


def clean_production(patch: dict, allowed: tuple[str, ...], int_fields: set) -> dict:
    """Keep only known keys; trim strings, coerce ints, mark blanks for clear.
    Returns the fields to WRITE — callers merge this over the existing bag so
    unset keys are left alone (a key sent as "" or null clears it)."""
    out: dict = {}
    for k, v in (patch or {}).items():
        if k not in allowed:
            continue
        if v is None or (isinstance(v, str) and not v.strip()):
            out[k] = None  # explicit clear
        elif k in int_fields:
            try:
                out[k] = int(v)
            except (TypeError, ValueError):
                out[k] = None
        else:
            out[k] = v.strip() if isinstance(v, str) else v
    return out


def merge_production(existing: dict, patch: dict, allowed, int_fields) -> dict:
    merged = dict(existing or {})
    for k, v in clean_production(patch, allowed, int_fields).items():
        if v is None:
            merged.pop(k, None)
        else:
            merged[k] = v
    return merged


class SeriesNotFound(Exception):
    pass


class ProjectNotFound(Exception):
    pass


def normalize_unit_label(label: Optional[str]) -> str:
    if not label:
        return "Episode"
    want = label.strip().lower()
    for known in UNIT_LABELS:
        if known.lower() == want:
            return known
    return "Episode"


def list_series(session: Session, project_id: uuid.UUID) -> list[Series]:
    if session.get(Project, project_id) is None:
        raise ProjectNotFound(str(project_id))
    return list(
        session.exec(
            select(Series)
            .where(Series.project_id == project_id)
            .order_by(Series.order_index, Series.created_at, Series.id)
        ).all()
    )


def _next_order_index(session: Session, project_id: uuid.UUID) -> int:
    last = session.exec(
        select(func.max(Series.order_index)).where(Series.project_id == project_id)
    ).one()
    if isinstance(last, tuple):
        last = last[0]
    return int(last) + 1 if last is not None else 0


def create_series(
    session: Session,
    project_id: uuid.UUID,
    *,
    name: str,
    code: str = "",
    unit_label: Optional[str] = None,
    order_index: Optional[int] = None,
    settings: Optional[dict[str, Any]] = None,
    production: Optional[dict[str, Any]] = None,
) -> Series:
    if session.get(Project, project_id) is None:
        raise ProjectNotFound(str(project_id))
    row = Series(
        project_id=project_id,
        name=name,
        code=code or "",
        unit_label=normalize_unit_label(unit_label),
        order_index=(
            _next_order_index(session, project_id) if order_index is None else order_index
        ),
        settings=settings or {},
        production=clean_production(
            production or {}, SERIES_PROD_FIELDS, _SERIES_INT_FIELDS
        ),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def get_series(session: Session, series_id: uuid.UUID) -> Series:
    row = session.get(Series, series_id)
    if row is None:
        raise SeriesNotFound(str(series_id))
    return row


def update_series(
    session: Session,
    series_id: uuid.UUID,
    *,
    name: Optional[str] = None,
    code: Optional[str] = None,
    unit_label: Optional[str] = None,
    order_index: Optional[int] = None,
    production: Optional[dict[str, Any]] = None,
) -> Series:
    row = get_series(session, series_id)
    if name is not None:
        row.name = name
    if code is not None:
        row.code = code
    if unit_label is not None:
        row.unit_label = normalize_unit_label(unit_label)
    if order_index is not None:
        row.order_index = order_index
    if production is not None:
        # patch merged over the existing bag (unset keys untouched)
        row.production = merge_production(
            row.production, production, SERIES_PROD_FIELDS, _SERIES_INT_FIELDS
        )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def delete_series(session: Session, series_id: uuid.UUID) -> None:
    """Delete the series. Its episodes are NOT deleted — they're detached
    (``series_id`` → NULL) so a mis-click can never take a season's worth of
    generated work with it. The route refuses outright when it still has
    episodes; this is the last-resort path."""
    row = get_series(session, series_id)
    for scene in session.exec(select(Scene).where(Scene.series_id == series_id)).all():
        scene.series_id = None
        session.add(scene)
    session.delete(row)
    session.commit()


def series_episode_count(session: Session, series_id: uuid.UUID) -> int:
    n = session.exec(
        select(func.count()).select_from(Scene).where(Scene.series_id == series_id)
    ).one()
    return int(n[0] if isinstance(n, tuple) else n)


def series_stats(session: Session, series_id: uuid.UUID) -> dict:
    """Live production stats for a series, computed from its episodes: episode
    count, a per-pipeline-status breakdown and a completion %. Powers the
    Production CRM overview so the numbers always match the episodes."""
    scenes = session.exec(select(Scene).where(Scene.series_id == series_id)).all()
    by_status: dict[str, int] = {}
    for sc in scenes:
        st = ((sc.production or {}).get("status") or "").strip() or "NotStarted"
        by_status[st] = by_status.get(st, 0) + 1
    total = len(scenes)
    done = by_status.get("Completed", 0)
    return {
        "episodes": total,
        "by_status": by_status,
        "completion_pct": round(done / total * 100) if total else 0,
    }


def ensure_default_series(session: Session, project_id: uuid.UUID) -> Series:
    """First Series of a project, creating a ``Default`` one if there is none.

    Keeps the old flat API (create an episode without naming a series) working
    and gives a fresh project something to hang episodes off immediately.
    """
    existing = list_series(session, project_id)
    if existing:
        return existing[0]
    return create_series(session, project_id, name="Default")
