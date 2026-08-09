"""How many times one sequence may be generated, and who lifts the ceiling.

A per-person budget stops somebody spending. It does nothing about one shot
quietly eating a season's worth of tries — the money is theirs to spend and the
system has no opinion about where it goes. This is that opinion: five attempts
per sequence, which is roughly "the obvious things have been tried". Past that
the answer is usually a different prompt or a different reference, not another
roll of the same one, and the person best placed to say which is not the one who
has just rolled five times.

**Derived, not counted.** The tally is the requests already attached to the
sequence's nodes, read when asked. A counter column would be one more thing to
keep in step, and it would drift the first time a request was deleted or a node
moved — silently, and in the direction that lets more through.

**Blocked sequences are a queue, not a notification.** Nothing is sent when the
fifth attempt lands: the sequence simply appears on a list the PM already looks
at, and leaves it when the limit is lifted or the work is done. A notification
would need somewhere to live, would need marking read, and would still be
telling you about a state you can see.
"""
from __future__ import annotations

import uuid
from typing import Optional

from sqlmodel import Session, select

from flowboard.db.models import Node, Request, Scene, Series, Shot

#: Attempts allowed on a sequence before a PM has to look at it.
DEFAULT_LIMIT = 5

#: VIDEO only. The ceiling is about video takes — the expensive, slow thing a
#: sequence exists to produce — and the five is calibrated to that.
#:
#: Everything else the canvas queues carries a node too and is deliberately not
#: counted:
#:   gen_image, edit_image  — building and fixing the references a take is made
#:                            FROM. Charging attempts for them would make the
#:                            artist ration exactly the preparation that stops
#:                            them wasting takes.
#:   gen_storyboard, retry_storyboard_shot — planning, before there is a take.
#:   vision                 — analysing an image they already have.
#:
#: `flow_gen_image` was in this list and should not have been: it is the comic
#: panel path, capped separately by a daily image quota, and it carries no node
#: at all — so it never actually bit. A line that is only harmless because
#: nothing reaches it is one refactor away from being wrong.
_GEN_TYPES = ("gen_video",)


class SequenceLocked(Exception):
    """This sequence has used its attempts. Carries what the caller must say."""

    def __init__(self, shot: Shot, used: int, limit: int) -> None:
        super().__init__(
            f"sequence {shot.code or ''} has used all {limit} of its generations "
            "— a PM has to look at it before it can run again"
        )
        self.shot_id = shot.id
        self.code = shot.code
        self.used = used
        self.limit = limit


def limit_of(shot: Shot) -> int:
    """The ceiling for this sequence: the house default until somebody lifts it."""
    return shot.gen_limit if shot.gen_limit is not None else DEFAULT_LIMIT


def used(session: Session, shot_id: uuid.UUID) -> int:
    """Generations already run against this sequence.

    Counts REQUESTS, not images. A request asking for four variants is one
    attempt: the artist tried one thing four ways, which is one idea, and
    charging four would make variants too expensive to use for what they are
    for.
    """
    node_ids = [
        n for n in session.exec(select(Node.id).where(Node.shot_id == shot_id)).all()
    ]
    if not node_ids:
        return 0
    rows = session.exec(
        select(Request).where(
            Request.node_id.in_(node_ids),  # type: ignore[attr-defined]
            Request.type.in_(_GEN_TYPES),  # type: ignore[attr-defined]
        )
    ).all()
    # A run that errored produced nothing. Charging an attempt for an upstream
    # failure spends the artist's five on the gateway having a bad afternoon.
    return sum(1 for r in rows if r.status != "error")


def state(session: Session, shot: Shot) -> dict:
    n, cap = used(session, shot.id), limit_of(shot)
    return {
        "used": n,
        "limit": cap,
        "remaining": max(0, cap - n),
        "locked": n >= cap,
        "unlocked": shot.gen_limit is not None,
    }


def check(session: Session, node_id: Optional[int]) -> None:
    """Refuse a generation on a sequence that has used its attempts.

    Checked before the request row exists, so a refusal costs nothing and does
    not itself count against the limit on the retry.
    """
    if node_id is None:
        return
    node = session.get(Node, node_id)
    if node is None:
        return
    shot = session.get(Shot, node.shot_id)
    if shot is None:
        return
    n, cap = used(session, shot.id), limit_of(shot)
    if n >= cap:
        raise SequenceLocked(shot, n, cap)


# ── the queue a PM looks at ─────────────────────────────────────────────────


def blocked(session: Session, project_id: Optional[uuid.UUID] = None) -> list[dict]:
    """Sequences that have run out, with enough context to judge them.

    Read fresh every time rather than recorded when the limit was hit: a
    sequence whose limit was lifted, or whose shot was deleted, has to leave
    this list on its own. Anything stored would need taking off it too, and the
    day that is forgotten a PM is looking at work that is already moving.
    """
    shots = session.exec(select(Shot)).all()
    scenes = {sc.id: sc for sc in session.exec(select(Scene)).all()}
    series = {sr.id: sr for sr in session.exec(select(Series)).all()}

    out = []
    for shot in shots:
        scene = scenes.get(shot.scene_id)
        if scene is None:
            continue
        if project_id is not None and scene.project_id != project_id:
            continue
        n, cap = used(session, shot.id), limit_of(shot)
        if n < cap:
            continue
        sr = series.get(scene.series_id)
        out.append({
            "shot_id": str(shot.id),
            "code": shot.code or "",
            "episode_id": str(scene.id),
            "episode": scene.code or scene.name,
            "series": sr.name if sr else "",
            "project_id": str(scene.project_id),
            "used": n,
            "limit": cap,
            "unlocked": shot.gen_limit is not None,
            "assignee_user_id": str(scene.assignee_user_id) if scene.assignee_user_id else None,
        })
    # Worst first: the one furthest past its ceiling has been stuck longest.
    return sorted(out, key=lambda r: (-(r["used"] - r["limit"]), r["code"]))


def unlock(session: Session, shot_id: uuid.UUID, extra: int = DEFAULT_LIMIT) -> dict:
    """Grant this sequence more attempts, counted from where it actually is.

    Set to `used + extra`, not `limit + extra`. Those differ once a sequence is
    past its ceiling — which it can be, since a run in flight is not refused
    retrospectively — and adding to the old limit would hand back fewer
    attempts than the number on the button.
    """
    shot = session.get(Shot, shot_id)
    if shot is None:
        raise SequenceLocked  # type: ignore[misc]
    shot.gen_limit = used(session, shot_id) + max(1, extra)
    session.add(shot)
    session.commit()
    session.refresh(shot)
    return state(session, shot)


def relock(session: Session, shot_id: uuid.UUID) -> dict:
    """Put a sequence back on the house default — undo for a mis-click."""
    shot = session.get(Shot, shot_id)
    if shot is None:
        raise SequenceLocked  # type: ignore[misc]
    shot.gen_limit = None
    session.add(shot)
    session.commit()
    session.refresh(shot)
    return state(session, shot)
