"""KPI rollups computed from what the system already recorded.

The workflow diagram says "Create KPI" when the PM assigns an employee, but not
what a KPI measures — that's a management decision nobody has stated yet. So this
module does not invent targets or scores. It computes the measures the recorded
data actually supports, and leaves "what counts as good" to whoever sets policy:

    delivered            episodes approved (throughput)
    attempts             total submissions, i.e. delivered + rework
    rejections           times work came back
    first_pass_rate      share of approvals that landed on the first attempt
    avg_attempts         attempts per approved episode
    credits_usd          generation spend on the episodes they own
    avg_review_days      submit → verdict latency (measures the *reviewer*, not
                         the artist, so it isn't mixed into their quality score)

Every number is derived — there is no KPI table to keep in sync, and adding a
measure later is a query, not a migration. That also means a KPI can't disagree
with the underlying record, which was the whole point of leaving spreadsheets.

Deliberately NOT included: anything about hours worked or speed of the artist.
The data can't support it (nothing records when work actually started, only when
it was assigned and submitted), and a made-up productivity number is worse than
none.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlmodel import Session, select

from flowboard.db.models import Project, Scene, Series, Submission, User
from flowboard.services import scope_budget


def _episodes(
    session: Session,
    *,
    project_id: Optional[uuid.UUID] = None,
    series_id: Optional[uuid.UUID] = None,
) -> list[Scene]:
    q = select(Scene)
    if series_id:
        q = q.where(Scene.series_id == series_id)
    elif project_id:
        q = q.where(Scene.project_id == project_id)
    return list(session.exec(q).all())


def _submissions_by_scene(session: Session, scene_ids: list[uuid.UUID]) -> dict:
    if not scene_ids:
        return {}
    out: dict[uuid.UUID, list[Submission]] = {}
    rows = session.exec(
        select(Submission).where(Submission.scene_id.in_(scene_ids))  # type: ignore[attr-defined]
    ).all()
    for r in rows:
        out.setdefault(r.scene_id, []).append(r)
    for v in out.values():
        v.sort(key=lambda r: r.version)
    return out


def _days(a: Optional[datetime], b: Optional[datetime]) -> Optional[float]:
    if a is None or b is None:
        return None
    # Both are stored UTC-aware, but a legacy naive row must not raise here and
    # take a whole KPI page down with it.
    if (a.tzinfo is None) != (b.tzinfo is None):
        return None
    return round((b - a).total_seconds() / 86400, 2)


def _blank(user_id: Optional[uuid.UUID], name: Optional[str]) -> dict:
    return {
        "user_id": str(user_id) if user_id else None,
        "name": name,
        "assigned": 0,
        "delivered": 0,
        "in_review": 0,
        "attempts": 0,
        "rejections": 0,
        "first_pass": 0,
        "credits_usd": 0.0,
        "_review_days": [],
    }


def _finalise(row: dict) -> dict:
    review_days = row.pop("_review_days")
    delivered = row["delivered"]
    row["first_pass_rate"] = round(row["first_pass"] / delivered, 3) if delivered else None
    row["avg_attempts"] = round(row["attempts"] / delivered, 2) if delivered else None
    row["credits_per_delivered_usd"] = (
        round(row["credits_usd"] / delivered, 4) if delivered else None
    )
    row["avg_review_days"] = (
        round(sum(review_days) / len(review_days), 2) if review_days else None
    )
    row["credits_usd"] = round(row["credits_usd"], 4)
    return row


def people(
    session: Session,
    *,
    project_id: Optional[uuid.UUID] = None,
    series_id: Optional[uuid.UUID] = None,
) -> list[dict]:
    """Per-assignee rollup over the episodes in scope.

    Episodes with no assignee are grouped under a ``None`` user so unassigned
    work is visible rather than quietly dropped — an unstaffed episode is exactly
    the gap a tracker exists to surface.
    """
    eps = _episodes(session, project_id=project_id, series_id=series_id)
    subs = _submissions_by_scene(session, [e.id for e in eps])

    names: dict[Optional[uuid.UUID], Optional[str]] = {}
    rows: dict[Optional[uuid.UUID], dict] = {}

    for ep in eps:
        uid = ep.assignee_user_id
        if uid not in names:
            u = session.get(User, uid) if uid else None
            names[uid] = (u.display_name or u.username) if u else None
        row = rows.setdefault(uid, _blank(uid, names[uid]))

        row["assigned"] += 1
        if (ep.deliverable_status or "draft") in ("approved", "paid"):
            row["delivered"] += 1
        elif ep.deliverable_status == "submitted":
            row["in_review"] += 1

        attempts = subs.get(ep.id, [])
        row["attempts"] += len(attempts)
        row["rejections"] += sum(1 for a in attempts if a.status == "rejected")
        # "First pass" means approved without ever coming back, which is the
        # measure that distinguishes quality from mere throughput.
        approved = [a for a in attempts if a.status == "approved"]
        if approved and approved[0].version == 1:
            row["first_pass"] += 1
        for a in attempts:
            d = _days(a.submitted_at, a.reviewed_at)
            if d is not None:
                row["_review_days"].append(d)

        spend = scope_budget.spend_usd(session, "scene", ep.id)
        row["credits_usd"] += spend["spent_usd"]

    out = [_finalise(r) for r in rows.values()]
    # Most delivered first; unassigned work last so it reads as a residue.
    out.sort(key=lambda r: (r["user_id"] is None, -r["delivered"], -(r["assigned"])))
    return out


def overview(session: Session) -> dict:
    """The whole installation: totals, per-project rows, and per-person across
    everything.

    The studio reads this as a board tracker, so people are aggregated across
    *all* projects rather than per project — someone working on three shows
    appears once, with their real total, instead of three partial rows that have
    to be added up by eye.
    """
    projects = list(session.exec(select(Project)).all())
    per_project = []
    for p in projects:
        eps = _episodes(session, project_id=p.id)
        delivered = sum(
            1 for e in eps if (e.deliverable_status or "draft") in ("approved", "paid")
        )
        per_project.append(
            {
                "project_id": str(p.id),
                "project_name": p.name,
                "episodes": len(eps),
                "delivered": delivered,
                "in_review": sum(1 for e in eps if e.deliverable_status == "submitted"),
                "unassigned": sum(1 for e in eps if not e.assignee_user_id),
                "completion_pct": (
                    round(delivered / len(eps) * 100, 1) if eps else None
                ),
                "budget": scope_budget.summary(session, "project", p.id),
            }
        )

    people_all = people(session)  # no filter → every episode in the app
    totals = {
        "projects": len(projects),
        "episodes": sum(r["episodes"] for r in per_project),
        "delivered": sum(r["delivered"] for r in per_project),
        "in_review": sum(r["in_review"] for r in per_project),
        "unassigned": sum(r["unassigned"] for r in per_project),
        "credits_usd": round(sum(r["credits_usd"] for r in people_all), 4),
    }
    totals["completion_pct"] = (
        round(totals["delivered"] / totals["episodes"] * 100, 1)
        if totals["episodes"]
        else None
    )
    per_project.sort(key=lambda r: (-(r["episodes"]), r["project_name"] or ""))
    return {"totals": totals, "projects": per_project, "people": people_all}


def series_rollup(session: Session, series_id: uuid.UUID) -> dict:
    """One series' delivery picture, plus its per-person breakdown."""
    series = session.get(Series, series_id)
    if series is None:
        return {}
    eps = _episodes(session, series_id=series_id)
    per_person = people(session, series_id=series_id)
    delivered = sum(1 for e in eps if (e.deliverable_status or "draft") in ("approved", "paid"))
    return {
        "series_id": str(series_id),
        "series_name": series.name,
        "series_code": series.code or "",
        "episodes": len(eps),
        "delivered": delivered,
        "in_review": sum(1 for e in eps if e.deliverable_status == "submitted"),
        "unassigned": sum(1 for e in eps if not e.assignee_user_id),
        "completion_pct": round(delivered / len(eps) * 100, 1) if eps else None,
        "budget": scope_budget.summary(session, "series", series_id),
        "people": per_person,
    }
