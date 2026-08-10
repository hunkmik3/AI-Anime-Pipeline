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
from flowboard.services import submission_service as subs_service


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


def _submissions_by_series(session: Session, series_ids: list[uuid.UUID]) -> dict:
    """Delivery attempts, grouped by the series they were attempts at.

    Keyed on the series because that is what gets handed in. Keyed on the episode,
    every one of these numbers silently became zero the day the deliverable moved
    up a tier — the join simply stopped matching, which is the kind of breakage a
    dashboard reports as "a quiet quarter".
    """
    if not series_ids:
        return {}
    out: dict[uuid.UUID, list[Submission]] = {}
    rows = session.exec(
        select(Submission).where(Submission.series_id.in_(series_ids))  # type: ignore[attr-defined]
    ).all()
    for r in rows:
        if r.series_id is not None:
            out.setdefault(r.series_id, []).append(r)
    for v in out.values():
        v.sort(key=lambda r: r.version)
    return out


def _series_in_scope(
    session: Session,
    eps: list[Scene],
    *,
    project_id: Optional[uuid.UUID] = None,
    series_id: Optional[uuid.UUID] = None,
) -> list[Series]:
    """The series the caller's filter covers.

    Taken from the filter rather than from the episodes, so a series that has been
    handed over but has no episodes yet still shows the person carrying it — which
    is exactly when a manager wants to see it.
    """
    q = select(Series)
    if series_id:
        q = q.where(Series.id == series_id)
    elif project_id:
        q = q.where(Series.project_id == project_id)
    return list(session.exec(q).all())


def _credits_by_user(
    session: Session, eps: list[Scene]
) -> dict[Optional[uuid.UUID], float]:
    """Settled spend inside these episodes, per the person who ran it.

    From `UsageRecord`, which is the only place the spender is recorded — the
    request itself carries no user. Anything the scope spent that has no usage
    record behind it lands under ``None``: generations from before metering, or
    from the no-auth path. Dropping that remainder would make the per-person
    column quietly under-report the project total, and a cost table that does not
    add up to itself is worse than no table.
    """
    from flowboard.db.models import Node, Request, Shot, UsageRecord

    scene_ids = [e.id for e in eps]
    if not scene_ids:
        return {}
    shot_ids = list(
        session.exec(select(Shot.id).where(Shot.scene_id.in_(scene_ids))).all()  # type: ignore[attr-defined]
    )
    if not shot_ids:
        return {}
    node_ids = list(
        session.exec(select(Node.id).where(Node.shot_id.in_(shot_ids))).all()  # type: ignore[attr-defined]
    )
    if not node_ids:
        return {}

    total = 0.0
    req_ids: list[int] = []
    for req in session.exec(
        select(Request).where(Request.node_id.in_(node_ids))  # type: ignore[attr-defined]
    ).all():
        if req.id is not None:
            req_ids.append(req.id)
        cost = (req.result or {}).get("cost_usd")
        if isinstance(cost, (int, float)):
            total += float(cost)
    if not req_ids:
        return {}

    out: dict[Optional[uuid.UUID], float] = {}
    attributed = 0.0
    for ur in session.exec(
        select(UsageRecord).where(
            UsageRecord.request_id.in_(req_ids),  # type: ignore[attr-defined]
            UsageRecord.status == "settled",
        )
    ).all():
        amount = float(ur.actual_usd or 0.0)
        out[ur.user_id] = out.get(ur.user_id, 0.0) + amount
        attributed += amount

    remainder = round(total - attributed, 6)
    if remainder > 0.005:
        out[None] = out.get(None, 0.0) + remainder
    return {k: round(v, 6) for k, v in out.items()}


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

    names: dict[Optional[uuid.UUID], Optional[str]] = {}
    rows: dict[Optional[uuid.UUID], dict] = {}

    def _name_of(uid):
        if uid not in names:
            u = session.get(User, uid) if uid else None
            names[uid] = (u.display_name or u.username) if u else None
        return names[uid]

    # TWO units, on purpose, because the studio has two.
    #
    # An episode is what somebody WORKS in — so "assigned" is counted per episode.
    # A SERIES is what gets handed in and reviewed — so delivered, attempts,
    # rejections, first-pass and review turnaround are counted once per series,
    # against the person it was given to. Counting a series' attempts once per
    # episode would report a three-episode series sent back once as three
    # rejections.
    #
    # "Assigned" follows the same fallback the rest of the app uses: the episode's
    # own assignee, else whoever the whole series was handed to. Reading only the
    # episode column reported 0 episodes for the person carrying twelve of them,
    # because handing over a series does not assign its episodes one by one.
    series_by_id = {
        sr.id: sr
        for sr in _series_in_scope(session, eps, project_id=project_id, series_id=series_id)
    }
    for ep in eps:
        uid = ep.assignee_user_id
        if uid is None and ep.series_id:
            sr = series_by_id.get(ep.series_id) or session.get(Series, ep.series_id)
            uid = sr.assignee_user_id if sr else None
        row = rows.setdefault(uid, _blank(uid, _name_of(uid)))
        row["assigned"] += 1

    # Credits go to WHOEVER RAN THE GENERATION, which is recorded on the usage
    # record, not to whoever owns the episode it landed in. They are different
    # people the moment an episode is lent out to clear a backlog, and "who is
    # burning budget" is the question this column exists to answer — charging it
    # to the owner answers a different one, quietly.
    for uid, amount in _credits_by_user(session, eps).items():
        row = rows.setdefault(uid, _blank(uid, _name_of(uid)))
        row["credits_usd"] += amount

    series_rows = _series_in_scope(session, eps, project_id=project_id, series_id=series_id)
    subs = _submissions_by_series(session, [sr.id for sr in series_rows])
    for sr in series_rows:
        uid = sr.assignee_user_id
        row = rows.setdefault(uid, _blank(uid, _name_of(uid)))

        st = sr.deliverable_status or "draft"
        if st in ("approved", "paid"):
            row["delivered"] += 1
        elif st == "submitted":
            row["in_review"] += 1

        attempts = subs.get(sr.id, [])
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
        delivery = subs_service.delivery_status_map(session, eps)
        delivered = sum(
            1 for e in eps if delivery.get(e.id, "draft") in ("approved", "paid")
        )
        per_project.append(
            {
                "project_id": str(p.id),
                "project_name": p.name,
                "episodes": len(eps),
                "delivered": delivered,
                "in_review": sum(
                    1 for e in eps if delivery.get(e.id) == "submitted"
                ),
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
    delivery = subs_service.delivery_status_map(session, eps)
    delivered = sum(
        1 for e in eps if delivery.get(e.id, "draft") in ("approved", "paid")
    )
    return {
        "series_id": str(series_id),
        "series_name": series.name,
        "series_code": series.code or "",
        "episodes": len(eps),
        "delivered": delivered,
        "in_review": sum(1 for e in eps if delivery.get(e.id) == "submitted"),
        "unassigned": sum(1 for e in eps if not e.assignee_user_id),
        "completion_pct": round(delivered / len(eps) * 100, 1) if eps else None,
        "budget": scope_budget.summary(session, "series", series_id),
        "people": per_person,
    }
