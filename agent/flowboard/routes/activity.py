"""Activity feed — read-only surface over the Request table.

Existing worker-driven types (gen_image / gen_video / edit_image) are
already logged here by `worker/processor.py`. LLM call sites
(auto_prompt / vision / planner) and upload routes will start wrapping
their calls in `services/activity.record_activity` so they show up in
the same feed — see `.omc/plans/activity-logs.md`.

Endpoints:
  GET  /api/activity?limit=50&before_id=N&type=auto_prompt,vision
       → list-projection (no params/result/error) sorted DESC by id
       → cursor pagination via `next_before_id`
  GET  /api/activity/{id}
       → full row including params, result, error
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlmodel import select

from flowboard.db import get_session
from flowboard.db.models import Node, Request, Scene, Shot
from flowboard.routes.deps import get_optional_user, owner_scope
from flowboard.services import permissions, resource_guard
from flowboard.services import project_service as ps

router = APIRouter(prefix="/api/activity", tags=["activity"])


def _utc_iso(dt: Optional[datetime]) -> Optional[str]:
    """Serialize a UTC datetime as ISO with explicit ``Z`` suffix.

    SQLite's ``DateTime`` column stores the value as an ISO string but
    strips tz info on read-back, so models that wrote ``datetime.now(tz=utc)``
    come back as **naive** datetimes here. Without an explicit suffix
    the frontend's ``new Date(string)`` parses naive ISO as **local**
    time — Vietnam (UTC+7) clients then read every timestamp as 7h in
    the past, and "X minutes ago" computations show 7h+ offsets. We
    annotate the value as UTC (naive → tag, aware → convert) before
    serializing so the wire format unambiguously says "UTC."
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    # Replace the `+00:00` offset with `Z` — both are valid ISO-8601 UTC
    # markers, but `Z` is shorter and matches what most JS code expects.
    return dt.isoformat().replace("+00:00", "Z")


def _duration_ms(req: Request) -> Optional[int]:
    """Return finished_at − created_at in ms, or None if still running."""
    if req.finished_at is None:
        return None
    delta = req.finished_at - req.created_at
    return int(delta.total_seconds() * 1000)


def _visible_scene_ids(session, user) -> list:
    """Every episode this caller may read, across all the projects they're on.

    The feed spans the whole installation, so it can't gate on a single project
    id — it has to be narrowed to what the caller could already open. Episode
    scope applies too: an artist assigned Ep03 must not read Ep01's prompts and
    results here just because both sit in the same project.
    """
    scene_ids: list = []
    for project in ps.list_projects(session, owner_user_id=owner_scope(user)):
        scope = permissions.visible_scope(session, user, project.id)
        if scope is None:  # producer/lead — the whole project is theirs
            scene_ids.extend(
                session.exec(select(Scene.id).where(Scene.project_id == project.id)).all()
            )
        else:
            scene_ids.extend(scope["scene_ids"])
    return scene_ids


@router.get("")
def list_activity(
    limit: int = Query(50, ge=1, le=200),
    before_id: Optional[int] = Query(None, ge=1),
    type: Optional[str] = Query(None, description="Comma-separated type filter"),
    user=Depends(get_optional_user),
) -> dict:
    """Return the most recent N activity rows in DESC order by id.

    `before_id` is a cursor — passing the `next_before_id` from a prior
    response yields the next older page. Avoids the offset-pagination
    consistency hazard for an actively-changing feed.
    """
    type_filter: Optional[set[str]] = None
    if type:
        type_filter = {t.strip() for t in type.split(",") if t.strip()}

    # `LIMIT limit + 1` lets us distinguish "exactly this many rows
    # exist" from "there's at least one more page" without a second
    # round-trip. The naive `len == limit` check incorrectly signals
    # "more pages" when the total happens to be a multiple of `limit`.
    fetch = limit + 1

    with get_session() as s:
        stmt = select(Request).order_by(Request.id.desc()).limit(fetch)
        if before_id is not None:
            stmt = stmt.where(Request.id < before_id)
        if type_filter:
            stmt = stmt.where(Request.type.in_(type_filter))
        if not resource_guard.is_unscoped(s, user):
            # Unfiltered, this returned every generation in the company —
            # prompts, node handles and errors from projects the caller can't
            # open — so narrow it in SQL rather than 403-ing the whole feed.
            scene_ids = _visible_scene_ids(s, user)
            if not scene_ids:
                return {"items": [], "next_before_id": None}
            # A row with no node has nothing to scope it to, and `NULL IN (…)`
            # is never true, so those drop out for scoped callers by design.
            stmt = stmt.where(
                Request.node_id.in_(
                    select(Node.id).where(
                        Node.shot_id.in_(
                            select(Shot.id).where(Shot.scene_id.in_(scene_ids))
                        )
                    )
                )
            )
        rows = s.exec(stmt).all()

        has_more = len(rows) > limit
        rows = rows[:limit]

        # Resolve node_short_id in one round trip via a join keyed by
        # the distinct node_ids in this page.
        node_ids = {r.node_id for r in rows if r.node_id is not None}
        short_ids: dict[int, str] = {}
        if node_ids:
            for n in s.exec(select(Node).where(Node.id.in_(node_ids))).all():
                if n.id is not None:
                    short_ids[n.id] = n.short_id

    items = [
        {
            "id": r.id,
            "type": r.type,
            "status": r.status,
            "node_id": r.node_id,
            "node_short_id": short_ids.get(r.node_id) if r.node_id else None,
            "created_at": _utc_iso(r.created_at),
            "finished_at": _utc_iso(r.finished_at),
            "duration_ms": _duration_ms(r),
        }
        for r in rows
    ]
    next_before_id = rows[-1].id if has_more and rows else None
    return {"items": items, "next_before_id": next_before_id}


@router.get("/{request_id}")
def get_activity_detail(request_id: int, user=Depends(get_optional_user)) -> dict:
    """Return the full row including params (input), result (output),
    error. Used by the UI's detail modal."""
    with get_session() as s:
        # Activity ids are Request ids — sequential integers. This is the
        # verbose projection (full prompt, provider result, error), so an
        # ungated caller could read another team's briefs by counting up.
        req = resource_guard.authorize_request(s, user, request_id)
        node_short_id: Optional[str] = None
        if req.node_id is not None:
            n = s.get(Node, req.node_id)
            if n is not None:
                node_short_id = n.short_id
    return {
        "id": req.id,
        "type": req.type,
        "status": req.status,
        "node_id": req.node_id,
        "node_short_id": node_short_id,
        "params": req.params,
        "result": req.result,
        "error": req.error,
        "created_at": _utc_iso(req.created_at),
        "finished_at": _utc_iso(req.finished_at),
        "duration_ms": _duration_ms(req),
    }
