"""Phase 10 — Series CRUD (the tier between Project and Episode/Chapter)."""
from __future__ import annotations

import re
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlmodel import Session, func, select

from flowboard.db.models import Project, Scene, Series, Shot

UNIT_LABELS = ("Episode", "Chapter")

# ── Production-tracking metadata (Series_Master spreadsheet columns) ─────────
# Stored in Series.production (a JSONB bag). Order is the natural form order;
# numeric ones are coerced to int. Unknown keys are dropped.
SERIES_PROD_FIELDS: tuple[str, ...] = (
    "full_code",
    "tier",
    "status",              # Planning | On-going | Completed | Cancelled
    "priority",            # High | Medium | Low
    "producer",            # PM/Producer who assigns the series to staff
    "assignee",            # the staff member producing the series
    # `sec_per_video` used to live here. Its only reader was a per-episode
    # sequence cap; with the cap gone it was a number the form collected and
    # nothing consulted, so the key is no longer accepted. Values already stored
    # are left alone — `merge_production` starts from the existing bag — they
    # simply cannot be written or edited any more.
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


# ── Auto full_code (mirrors the Series_Master sheet formula) ────────────────
# Format:  <BRAND>_<YY><NNN>_<CODE>_<PascalName>
#   BRAND  project.settings["code_prefix"] or the project name, ASCII-uppercased
#   YY     2-digit year of production.start_date (fallback: current year)
#   NNN    running count of series in the same project + same year (zero-pad 3)
#   CODE   series.code
#   Name   series name in PascalCase, diacritics + spaces stripped
# The prefix (brand_YYNNN_code) is uppercased; the name keeps its PascalCase,
# exactly like `UPPER("MOGU_"&yy&order&"_"&scode) & "_" & name` in the sheet.


def _ascii(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c)
    )


def _pascal(name: str) -> str:
    return "".join(w[:1].upper() + w[1:] for w in re.split(r"[^0-9A-Za-z]+", _ascii(name)) if w)


def _year2(start_date: Any, fallback_year: int) -> str:
    if isinstance(start_date, str):
        m = re.search(r"(?:19|20)\d{2}", start_date)
        if m:
            return m.group(0)[-2:]
    return f"{fallback_year % 100:02d}"


def build_full_code(session: Session, project: Project, series: Series) -> str:
    """Compute the deterministic full_code for a series (see block comment)."""
    raw_brand = (project.settings or {}).get("code_prefix") or project.name or "PRJ"
    brand = re.sub(r"[^0-9A-Za-z]+", "", _ascii(raw_brand)).upper() or "PRJ"
    yy = _year2((series.production or {}).get("start_date"), datetime.now(timezone.utc).year)

    # NNN = position among same-project, same-year series by creation order, so
    # a series' number is stable once assigned (new rows only append).
    siblings = session.exec(
        select(Series).where(Series.project_id == project.id)
    ).all()

    def _yy_of(s: Series) -> str:
        return _year2((s.production or {}).get("start_date"), datetime.now(timezone.utc).year)

    earlier = [
        s
        for s in siblings
        if s.id != series.id
        and _yy_of(s) == yy
        and s.created_at
        and series.created_at
        and s.created_at < series.created_at
    ]
    seq = len(earlier) + 1
    prefix = f"{brand}_{yy}{seq:03d}_{(series.code or '').strip()}".upper()
    return f"{prefix}_{_pascal(series.name or '')}"


def episode_code(series: Series, number: int) -> str:
    """Episode id convention, mirroring the Episode_Tracker sheet:
    ``<SERIES_CODE>_EP<NN>`` → ``HUSB_EP01``, ``P1PRE_EP05``. Falls back to a
    bare ``EP07`` when the series has no code yet. Two digits, widening past 99.
    """
    code = (series.code or "").strip().upper()
    return f"{code}_EP{number:02d}" if code else f"EP{number:02d}"


def _apply_full_code(session: Session, series: Series) -> None:
    """Set production['full_code'] on the series in place (does not commit)."""
    project = session.get(Project, series.project_id)
    if project is None:
        return
    prod = dict(series.production or {})
    prod["full_code"] = build_full_code(session, project, series)
    series.production = prod


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
    # full_code is always machine-generated (mirrors the sheet), overriding any
    # value the caller sent.
    _apply_full_code(session, row)
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
    # Keep full_code in sync whenever an input it depends on changes (name,
    # code, or the start_date inside the production patch).
    if (
        name is not None
        or code is not None
        or (production is not None and "start_date" in production)
    ):
        _apply_full_code(session, row)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


class SeriesNotEmpty(Exception):
    """A series still holding episodes cannot be deleted."""


def delete_series(session: Session, series_id: uuid.UUID) -> None:
    """Delete an EMPTY series, refusing while it still holds episodes.

    It used to detach them instead — ``scene.series_id = None`` — so that a
    mis-click could never take a season of generated work with it. That is no
    longer possible and no longer needs to be: every episode belongs to a series
    now (the column is NOT NULL), which is the same protection expressed as an
    invariant rather than as a rescue. Left as it was, this function would raise
    an integrity error from the database instead of a sentence anyone can read.

    The route already refused when a series held episodes; the guard lives here
    too, because "the caller checks first" is not an invariant.
    """
    row = get_series(session, series_id)
    held = session.exec(select(Scene).where(Scene.series_id == series_id)).all()
    if held:
        raise SeriesNotEmpty(
            f"series still has {len(held)} episode(s) — move or delete them first"
        )
    session.delete(row)
    session.commit()


def generate_structure(
    session: Session,
    series_id: uuid.UUID,
    *,
    episodes: int,
    sequences_per_episode: int,
) -> dict:
    """Plan out a series: ensure it has ``episodes`` Episodes, each Episode with
    ``sequences_per_episode`` Sequences.

    Idempotent + non-destructive:
    - only the MISSING episodes (beyond what already exists) are created, so
      re-running tops up rather than duplicating;
    - sequences are only added to episodes that currently have NONE, so
      episodes already holding real work are never touched.

    Bulk-inserts in a single transaction. Returns what was created.
    """
    series = get_series(session, series_id)
    existing = list(
        session.exec(
            select(Scene)
            .where(Scene.series_id == series_id)
            .order_by(Scene.order_index, Scene.created_at, Scene.id)
        ).all()
    )

    made_ep = 0
    new_scenes: list[Scene] = []
    for i in range(len(existing), episodes):
        sc = Scene(
            project_id=series.project_id,
            series_id=series_id,
            name=f"Episode {i + 1}",
            code=episode_code(series, i + 1),
            order_index=i,
        )
        session.add(sc)
        new_scenes.append(sc)
        made_ep += 1
    session.flush()  # assign scene ids

    made_seq = 0
    target_eps = (existing + new_scenes)[:episodes]
    for sc in target_eps:
        has_shot = session.exec(
            select(Shot.id).where(Shot.scene_id == sc.id).limit(1)
        ).first()
        if has_shot is None and sequences_per_episode > 0:
            for j in range(sequences_per_episode):
                session.add(
                    Shot(scene_id=sc.id, order_index=j, code=f"SQ{j + 1:02d}")
                )
                made_seq += 1
    session.commit()
    return {
        "episodes_created": made_ep,
        "sequences_created": made_seq,
        "total_episodes": len(target_eps),
        "sequences_per_episode": sequences_per_episode,
    }


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
