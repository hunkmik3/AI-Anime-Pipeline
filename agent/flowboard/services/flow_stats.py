"""What the comic side is doing, and what it cost — for the admin console.

`stats_service` reads eight tables and not one of them belongs to Giantflow, so
the console could answer "how is MoguTV going" and had nothing at all to say
about 99 panels across 7 comics. Everything below fills exactly that hole; the
video side is untouched.

Two things are joined that were never joined before:

**Progress.** Giantflow already counts panels by status at every tier — the
batch grid and the chapter cards read it constantly — but only ever for the one
comic on screen. Rolling it up across the slate is a different question and had
no answer.

**Money.** Spend is attributed by walking `request.node_id → shot → scene →
project`. A panel is not on that tree, so a panel generation could not be
attributed at all: the panel id lived in `params["__panel_id"]`, which is true
and unjoinable, and every one of those runs appeared in the ledger as money
belonging to nobody. `Request.flow_panel_id` is the column that fixes it, and
this module is what reads it.
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Optional

from sqlmodel import Session, select

from flowboard.db import get_session
from flowboard.services import flow_quota
from flowboard.db.models import (
    FlowBatch,
    FlowChapter,
    FlowPanel,
    FlowPanelEvent,
    FlowSeries,
    PANEL_STATUSES,
    Request,
    UsageRecord,
    User,
)


def _empty_counts() -> dict[str, int]:
    return {s: 0 for s in PANEL_STATUSES}


def _cost_by_panel(session: Session) -> dict[int, tuple[float, int]]:
    """panel id → (dollars, images produced).

    Priced from the TARIFF, not from a billed amount, because there is no billed
    amount to read. The video side settles against what Avis actually charged
    and stores it on a UsageRecord; panel generation creates no UsageRecord at
    all — it is outside the budget system — so the only figure that exists is
    the fixed per-image price the studio pays.

    That makes this an accurate charge rather than a reconciled one, which is
    worth knowing when it disagrees with an invoice. Atrium images price at zero
    on purpose: they cost quota, not money, and the quota is reported separately.

    Counts finished images, not requests: a run asking for four variants costs
    four, and a run that failed cost nothing.
    """
    reqs = [
        r
        for r in session.exec(
            select(Request).where(Request.flow_panel_id.is_not(None))  # type: ignore[union-attr]
        ).all()
        if r.status == "done"
    ]
    if not reqs:
        return {}
    out: dict[int, list[float]] = defaultdict(list)
    counts: dict[int, int] = defaultdict(int)
    for r in reqs:
        n = flow_quota.images_in(r.result)
        if not n:
            continue
        out[r.flow_panel_id].append(flow_quota.price_usd(r.params, n))  # type: ignore[index]
        counts[r.flow_panel_id] += n  # type: ignore[index]
    return {pid: (round(sum(v), 4), counts[pid]) for pid, v in out.items()}


def _tree(session: Session):
    comics = {c.id: c for c in session.exec(select(FlowSeries)).all()}
    chapters = {c.id: c for c in session.exec(select(FlowChapter)).all()}
    batches = {b.id: b for b in session.exec(select(FlowBatch)).all()}
    panels = list(session.exec(select(FlowPanel)).all())
    return comics, chapters, batches, panels


def _comic_of(panel: FlowPanel, batches, chapters) -> Optional[int]:
    b = batches.get(panel.batch_id)
    if b is None:
        return None
    c = chapters.get(b.chapter_id)
    return c.series_id if c else None


# ── the numbers ─────────────────────────────────────────────────────────────


def overview() -> dict:
    """One line for the top of the console: how much comic work exists, how far
    through it is, and what it has cost."""
    with get_session() as s:
        comics, chapters, batches, panels = _tree(s)
        cost = _cost_by_panel(s)
    counts = _empty_counts()
    for p in panels:
        counts[p.status] = counts.get(p.status, 0) + 1
    spent = round(sum(v[0] for v in cost.values()), 4)
    runs = sum(v[1] for v in cost.values())
    done = counts.get("approved", 0)
    return {
        "comics": len(comics),
        "chapters": len(chapters),
        "batches": len(batches),
        "panels": len(panels),
        "status_counts": counts,
        "approved": done,
        "approved_pct": round(done / len(panels) * 100, 1) if panels else 0.0,
        "spent_usd": spent,
        "runs": runs,
        # What a finished panel actually costs, which is the number that decides
        # whether this pipeline pays for itself. Divided by APPROVED, not by
        # runs: the re-dos are part of the price of the one that shipped.
        "cost_per_approved": round(spent / done, 4) if done else 0.0,
    }


def by_comic() -> list[dict]:
    """One row per comic — the table a producer scans."""
    with get_session() as s:
        comics, chapters, batches, panels = _tree(s)
        cost = _cost_by_panel(s)

    rows: dict[int, dict] = {
        cid: {
            "series_id": cid,
            "name": c.name,
            "chapters": 0,
            "panels": 0,
            "status_counts": _empty_counts(),
            "spent_usd": 0.0,
            "runs": 0,
        }
        for cid, c in comics.items()
    }
    for ch in chapters.values():
        if ch.series_id in rows:
            rows[ch.series_id]["chapters"] += 1
    for p in panels:
        cid = _comic_of(p, batches, chapters)
        if cid not in rows:
            continue
        row = rows[cid]
        row["panels"] += 1
        row["status_counts"][p.status] = row["status_counts"].get(p.status, 0) + 1
        usd, runs = cost.get(p.id, (0.0, 0))
        row["spent_usd"] += usd
        row["runs"] += runs

    out = []
    for row in rows.values():
        row["spent_usd"] = round(row["spent_usd"], 4)
        row["approved"] = row["status_counts"].get("approved", 0)
        row["approved_pct"] = (
            round(row["approved"] / row["panels"] * 100, 1) if row["panels"] else 0.0
        )
        out.append(row)
    return sorted(out, key=lambda r: (-r["panels"], r["name"].lower()))


def by_artist() -> list[dict]:
    """Who did what, and how it landed.

    Attributed through the BATCH assignee, which is where "who is doing this"
    lives on the comic side — a panel has no owner of its own.

    ``sent_back`` counts EVENTS, not panels: a panel returned three times is
    three send-backs, and the whole reason to look at this column is to see how
    often work comes back.
    """
    with get_session() as s:
        comics, chapters, batches, panels = _tree(s)
        cost = _cost_by_panel(s)
        users = {u.id: u for u in s.exec(select(User)).all()}
        # Events for panels, so "sent back twice" is two, not one.
        by_panel_events: dict[int, list[str]] = defaultdict(list)
        for e in s.exec(select(FlowPanelEvent)).all():
            by_panel_events[e.panel_id].append(e.kind)

    rows: dict[Optional[uuid.UUID], dict] = {}
    for p in panels:
        b = batches.get(p.batch_id)
        who = b.assignee_user_id if b else None
        row = rows.setdefault(
            who,
            {
                "user_id": str(who) if who else None,
                "name": (
                    (users[who].display_name or users[who].username)
                    if who in users
                    else "Chưa giao"
                ),
                "panels": 0,
                "status_counts": _empty_counts(),
                "sent_back": 0,
                "spent_usd": 0.0,
                "runs": 0,
            },
        )
        row["panels"] += 1
        row["status_counts"][p.status] = row["status_counts"].get(p.status, 0) + 1
        row["sent_back"] += sum(
            1 for k in by_panel_events.get(p.id, []) if k == "changes_requested"
        )
        usd, runs = cost.get(p.id, (0.0, 0))
        row["spent_usd"] += usd
        row["runs"] += runs

    out = []
    for row in rows.values():
        row["spent_usd"] = round(row["spent_usd"], 4)
        row["approved"] = row["status_counts"].get("approved", 0)
        # Runs per approved panel: the honest read of "how many attempts does
        # this person need". 0 when nothing of theirs is approved yet, rather
        # than a number divided by zero and quietly reported as infinity.
        row["runs_per_approved"] = (
            round(row["runs"] / row["approved"], 2) if row["approved"] else 0.0
        )
        out.append(row)
    return sorted(out, key=lambda r: -r["panels"])


def unattributed() -> dict:
    """Spend the console cannot place, said out loud.

    A ledger that silently drops what it cannot explain is worse than one that
    reports a gap: the total still looks right, so nobody goes looking.
    """
    with get_session() as s:
        reqs = list(s.exec(select(Request)).all())
        paid = list(
            s.exec(select(UsageRecord).where(UsageRecord.status == "settled")).all()
        )
    by_req = {r.id: r for r in reqs}
    usd = 0.0
    runs = 0
    for u in paid:
        req = by_req.get(u.request_id)
        if req is None or (req.node_id is None and req.flow_panel_id is None):
            usd += float(u.actual_usd or 0.0)
            runs += 1
    return {"spent_usd": round(usd, 4), "runs": runs}


def quota_today() -> dict:
    """Today's image cap, for the console — the other half of what a comic run
    costs. An Atrium image spends quota rather than money, so a spend figure
    alone says nothing about whether the studio is about to hit a wall."""
    with get_session() as s:
        used = flow_quota.used_today(s)
    return {
        "quota": flow_quota.DAILY_QUOTA,
        "used": used[flow_quota.GEMINI],
        "remaining": max(0, flow_quota.DAILY_QUOTA - used[flow_quota.GEMINI]),
        "seedream_images": used[flow_quota.SEEDREAM],
        "seconds_until_reset": flow_quota.seconds_until_reset(),
    }
