"""Giantflow notifications — what this account has to do, and what just changed.

**Derived, never stored.** There is no notification table and nothing fans out on
write. A feed row is read back out of `flow_panel_event`, and a to-do is read out
of the panel's own `status`. Three reasons that is the right trade here:

1. A stored copy needs a write at every event site — submit, review, reopen,
   note, assign — and the day someone adds a sixth, the feed silently stops
   mentioning it. Deriving cannot fall behind the truth because it *is* the
   truth.
2. It works for history that already happened. This studio has hundreds of
   panels and thousands of events on the board today; a stored feed would start
   empty and need a backfill that guesses at who should have been told what.
3. A to-do is a *state*, not an event. "Waiting on your review" must vanish the
   moment the verdict lands, from every device, with nothing to mark read. Rows
   of stored notifications go stale exactly there — you clear the same panel
   twice because two events queued up behind one piece of work.

The one thing that genuinely cannot be derived is **how far this user has read**,
and that is all `FlowNoticeRead` stores: one timestamp per account.

Scope follows the same rule as the rest of giantflow, and for the same reason —
an artist is shown their own share so the 45 panels that are theirs are not
buried in 320 that are not.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlmodel import Session, select

from flowboard.db.models import (
    FlowBatch,
    FlowChapter,
    FlowNoticeRead,
    FlowPanel,
    FlowPanelEvent,
    FlowPanelNote,
    FlowSeries,
    User,
)
from flowboard.services import flow_permissions as fp

#: How far back the feed reads. Older than this and it is history, which the
#: panel's own page tells better than a global list can.
FEED_WINDOW = timedelta(days=30)
FEED_LIMIT = 120

#: A deadline inside this many days is worth saying out loud.
DUE_SOON_DAYS = 3


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _s(n: int) -> str:
    """Plural 's'. Inline everywhere it was unreadable and it is used nine times."""
    return "" if n == 1 else "s"


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Postgres hands back aware datetimes; SQLite (the desktop build) does not.
    Comparing the two raises, so normalise on the way in rather than at each
    comparison site."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class Notice:
    """One line in the feed, or one job on the list.

    ``id`` is stable across requests — derived from the row it came from — so the
    client can key a list on it without the entries jumping about on refresh.
    """

    id: str
    kind: str
    title: str
    body: Optional[str] = None
    at: Optional[datetime] = None
    actor_name: Optional[str] = None
    href: Optional[str] = None
    panel_id: Optional[int] = None
    code: Optional[str] = None
    where: Optional[str] = None
    count: int = 1
    #: True when this row is the reader's own doing. Kept in the feed — a history
    #: with your own actions cut out reads as if they never happened — but never
    #: counted as unread, because nothing you just did is news to you.
    mine: bool = False

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "body": self.body,
            "at": self.at.isoformat() if self.at else None,
            "actor_name": self.actor_name,
            "href": self.href,
            "panel_id": self.panel_id,
            "code": self.code,
            "where": self.where,
            "count": self.count,
            "mine": self.mine,
        }


# ── where each panel lives, in one pass ─────────────────────────────────────


@dataclass
class _Tree:
    """batch/chapter/series for every batch, fetched once.

    Walking `panel → batch → chapter → series` per row is four queries a panel;
    on a feed of 120 events across 300 panels that is the difference between one
    page load and a slideshow.
    """

    batches: dict[int, FlowBatch] = field(default_factory=dict)
    chapters: dict[int, FlowChapter] = field(default_factory=dict)
    series: dict[int, FlowSeries] = field(default_factory=dict)

    def series_id_of_batch(self, batch_id: int) -> Optional[int]:
        b = self.batches.get(batch_id)
        if b is None:
            return None
        c = self.chapters.get(b.chapter_id)
        return c.series_id if c else None

    def where(self, batch_id: int) -> str:
        b = self.batches.get(batch_id)
        if b is None:
            return ""
        c = self.chapters.get(b.chapter_id)
        s = self.series.get(c.series_id) if c else None
        return " · ".join(x for x in [s.name if s else None, c.name if c else None, b.name] if x)


def _load_tree(session: Session) -> _Tree:
    t = _Tree()
    for b in session.exec(select(FlowBatch)).all():
        t.batches[b.id] = b
    for c in session.exec(select(FlowChapter)).all():
        t.chapters[c.id] = c
    for s in session.exec(select(FlowSeries)).all():
        t.series[s.id] = s
    return t


def _readable_series(session: Session, user: Optional[User], tree: _Tree) -> set[int]:
    """Comics this account may read at all."""
    return {
        sid
        for sid in tree.series
        if fp.allows(fp.role_for(session, user, sid), "panel.read")
    }


def _reviewable_series(session: Session, user: Optional[User], tree: _Tree) -> set[int]:
    """Comics this account rules on. The PM's half of the feed hangs off this."""
    return {
        sid
        for sid in tree.series
        if fp.allows(fp.role_for(session, user, sid), "panel.review")
    }


def _visible_batches(
    session: Session, user: Optional[User], tree: _Tree
) -> Optional[set[int]]:
    """Batches in scope, or ``None`` for "everything readable".

    Mirrors `_scoped_to_own_work` in the router deliberately: a notification
    naming a panel the reader cannot open would be a leak dressed as a courtesy.
    """
    role = fp.best_role(session, user)
    if fp.sees_everything(role):
        return None
    return fp.assigned_batch_ids(session, user)


# ── the read watermark ──────────────────────────────────────────────────────


def seen_at(session: Session, user: Optional[User]) -> Optional[datetime]:
    if user is None:
        return None
    row = session.get(FlowNoticeRead, user.id)
    return _aware(row.seen_at) if row else None


def mark_seen(session: Session, user: Optional[User], when: Optional[datetime] = None) -> None:
    if user is None:
        return
    row = session.get(FlowNoticeRead, user.id)
    stamp = when or _utcnow()
    if row is None:
        session.add(FlowNoticeRead(user_id=user.id, seen_at=stamp))
    else:
        row.seen_at = stamp
        session.add(row)
    session.commit()


# ── the work list ───────────────────────────────────────────────────────────

_STATUS_HREF = "/giantflow/panel/{id}"


def _due_notices(
    session: Session,
    tree: _Tree,
    series_ids: set[int],
    chapter_ids: Optional[set[int]],
) -> list[Notice]:
    """Deadlines that have passed or are about to.

    A date is not an event, so these are jobs rather than feed lines — they
    appear while they are true and disappear when the date is moved or met.
    """
    today = date.today()
    out: list[Notice] = []
    for c in tree.chapters.values():
        if c.series_id not in series_ids:
            continue
        if chapter_ids is not None and c.id not in chapter_ids:
            continue
        if not c.due_date:
            continue
        days = (c.due_date - today).days
        if days > DUE_SOON_DAYS:
            continue
        s = tree.series.get(c.series_id)
        if days < 0:
            when = f"{-days} day{_s(-days)} overdue"
        elif days == 0:
            when = "due today"
        else:
            when = f"due in {days} day{_s(days)}"
        out.append(
            Notice(
                id=f"due:chapter:{c.id}",
                kind="overdue" if days < 0 else "due_soon",
                title=f"{c.name} is {when}",
                body=None,
                where=s.name if s else None,
                href=f"/giantflow/c/{c.id}",
                at=None,
            )
        )
    return out


def todo(session: Session, user: Optional[User]) -> list[Notice]:
    """What this account has to act on, right now, derived from live state.

    Ordered by who is blocked: work sent back is blocking an artist, work waiting
    on a verdict is blocking a PM, and a batch with nobody on it is blocking
    everyone. Deadlines come last because they are a warning, not a queue.
    """
    tree = _load_tree(session)
    mine = _visible_batches(session, user, tree)
    readable = _readable_series(session, user, tree)
    reviewable = _reviewable_series(session, user, tree)
    role = fp.best_role(session, user)

    def in_scope(batch_id: int) -> bool:
        sid = tree.series_id_of_batch(batch_id)
        if sid is None or sid not in readable:
            return False
        return mine is None or batch_id in mine

    panels = [p for p in session.exec(select(FlowPanel)).all() if in_scope(p.batch_id)]
    out: list[Notice] = []

    # ── the artist's side ────────────────────────────────────────────────
    # Anyone can hold panels: a PM who took a batch is an artist on that batch.
    # Keyed off the assignment, not off the role name.
    if user is not None:
        held = fp.assigned_batch_ids(session, user)
        back = [p for p in panels if p.batch_id in held and p.status == "changes_requested"]
        started = [p for p in panels if p.batch_id in held and p.status == "in_progress"]
        untouched = [p for p in panels if p.batch_id in held and p.status == "todo"]

        notes_by_panel = _open_notes(session, [p.id for p in back])
        for p in sorted(back, key=lambda x: x.code or ""):
            reasons = notes_by_panel.get(p.id, [])
            out.append(
                Notice(
                    id=f"todo:back:{p.id}",
                    kind="sent_back",
                    title=f"{p.code} came back — fix and submit again",
                    body=reasons[0] if reasons else None,
                    where=tree.where(p.batch_id),
                    href=_STATUS_HREF.format(id=p.id),
                    panel_id=p.id,
                    code=p.code,
                )
            )
        if started:
            out.append(
                Notice(
                    id="todo:in_progress",
                    kind="in_progress",
                    title=f"{len(started)} panel{_s(len(started))} started but not submitted",
                    body="Pick a version and hand it in.",
                    href="/giantflow/my-work",
                    count=len(started),
                )
            )
        if untouched:
            out.append(
                Notice(
                    id="todo:not_started",
                    kind="not_started",
                    title=f"{len(untouched)} panel{_s(len(untouched))} not started",
                    body="They are in your batch and nobody has generated anything yet.",
                    href="/giantflow/panels",
                    count=len(untouched),
                )
            )

    # ── the PM's side ────────────────────────────────────────────────────
    if reviewable:
        waiting = [
            p
            for p in panels
            if p.status == "submitted"
            and (tree.series_id_of_batch(p.batch_id) in reviewable)
        ]
        if waiting:
            # Who is waiting, not where the files are. The full series · chapter ·
            # batch path is 70 characters of mostly-repeated prefix, and the one
            # thing a PM wants off this line is which artist is blocked.
            holders = {
                tree.batches[p.batch_id].assignee_user_id
                for p in waiting
                if p.batch_id in tree.batches
            }
            found = _names(session, {h for h in holders if h})
            blocked = sorted({found.get(h, "unassigned") for h in holders})
            out.append(
                Notice(
                    id="todo:review",
                    kind="to_review",
                    title=f"{len(waiting)} panel{_s(len(waiting))} waiting on your verdict",
                    body="from " + ", ".join(blocked[:4]) if blocked else None,
                    href="/giantflow/review",
                    count=len(waiting),
                )
            )
        empty = [
            b
            for bid, b in tree.batches.items()
            if b.assignee_user_id is None
            and tree.series_id_of_batch(bid) in reviewable
            and (mine is None or bid in mine)
        ]
        if empty:
            out.append(
                Notice(
                    id="todo:unassigned",
                    kind="unassigned",
                    title=f"{len(empty)} batch{'es' if len(empty) != 1 else ''} {'have' if len(empty) != 1 else 'has'} no artist",
                    body=", ".join(b.name for b in sorted(empty, key=lambda x: x.name)[:4]),
                    href="/giantflow",
                    count=len(empty),
                )
            )

    # ── deadlines ────────────────────────────────────────────────────────
    # A PM is warned about every chapter they run; an artist only about the ones
    # they actually hold panels in.
    if fp.sees_everything(role) and reviewable:
        out += _due_notices(session, tree, reviewable, None)
    elif mine is not None and mine:
        held_chapters = {tree.batches[b].chapter_id for b in mine if b in tree.batches}
        out += _due_notices(session, tree, readable, held_chapters)

    return out


def _open_notes(session: Session, panel_ids: list[int]) -> dict[int, list[str]]:
    """Unresolved remarks, newest first, for a set of panels in one query."""
    if not panel_ids:
        return {}
    rows = session.exec(
        select(FlowPanelNote)
        .where(
            FlowPanelNote.panel_id.in_(panel_ids),  # type: ignore[attr-defined]
            FlowPanelNote.resolved == False,  # noqa: E712
        )
        .order_by(FlowPanelNote.created_at.desc())  # type: ignore[attr-defined]
    ).all()
    out: dict[int, list[str]] = {}
    for n in rows:
        out.setdefault(n.panel_id, []).append(n.body)
    return out


# ── the feed ────────────────────────────────────────────────────────────────

_FEED_TITLE = {
    "submitted": "{code} submitted",
    "approved": "{code} approved",
    "changes_requested": "{code} sent back",
    "reopened": "{code} reopened",
}


def feed(
    session: Session, user: Optional[User], *, limit: int = FEED_LIMIT
) -> list[Notice]:
    """What has happened lately, in scope, newest first.

    ``version_added`` is left out. It fires on every generation — a hundred a day
    from one artist — and drowns the handovers, which are the only events anyone
    else needs to know about.
    """
    tree = _load_tree(session)
    mine_batches = _visible_batches(session, user, tree)
    readable = _readable_series(session, user, tree)
    since = _utcnow() - FEED_WINDOW

    events = session.exec(
        select(FlowPanelEvent)
        .where(
            FlowPanelEvent.kind.in_(list(_FEED_TITLE)),  # type: ignore[attr-defined]
            FlowPanelEvent.created_at >= since,
        )
        .order_by(FlowPanelEvent.created_at.desc(), FlowPanelEvent.id.desc())
        .limit(limit * 4)
    ).all()
    if not events:
        return []

    panels = {
        p.id: p
        for p in session.exec(
            select(FlowPanel).where(
                FlowPanel.id.in_({e.panel_id for e in events})  # type: ignore[attr-defined]
            )
        ).all()
    }
    names = _names(session, {e.actor_user_id for e in events if e.actor_user_id})

    out: list[Notice] = []
    for e in events:
        p = panels.get(e.panel_id)
        if p is None:
            continue  # panel deleted; the event outlived it
        sid = tree.series_id_of_batch(p.batch_id)
        if sid is None or sid not in readable:
            continue
        if mine_batches is not None and p.batch_id not in mine_batches:
            continue
        out.append(
            Notice(
                id=f"event:{e.id}",
                kind=e.kind,
                title=_FEED_TITLE[e.kind].format(code=p.code),
                body=e.body,
                at=_aware(e.created_at),
                actor_name=names.get(e.actor_user_id),
                where=tree.where(p.batch_id),
                href=_STATUS_HREF.format(id=p.id),
                panel_id=p.id,
                code=p.code,
                mine=bool(user is not None and e.actor_user_id == user.id),
            )
        )
        if len(out) >= limit:
            break
    return out


def _names(session: Session, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = session.exec(select(User).where(User.id.in_(ids))).all()  # type: ignore[attr-defined]
    return {u.id: (u.display_name or u.username) for u in rows}


def unread_count(
    session: Session, user: Optional[User], rows: Optional[list[Notice]] = None
) -> int:
    """Feed lines since this account last looked, not counting its own doing."""
    if user is None:
        return 0
    rows = feed(session, user) if rows is None else rows
    mark = seen_at(session, user)
    return sum(
        1 for n in rows if not n.mine and (mark is None or (n.at and n.at > mark))
    )


def summary(session: Session, user: Optional[User]) -> dict:
    """Everything the notifications tab draws, in one round trip."""
    jobs = todo(session, user)
    lines = feed(session, user)
    return {
        "todo": [n.as_dict() for n in jobs],
        "feed": [n.as_dict() for n in lines],
        "unread": unread_count(session, user, lines),
        "seen_at": (lambda m: m.isoformat() if m else None)(seen_at(session, user)),
        "role": fp.best_role(session, user),
    }
