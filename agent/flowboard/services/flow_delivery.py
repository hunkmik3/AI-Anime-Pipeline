"""The handover: an approved panel becomes a sequence.

Two products, one piece of work. giantflow adapts a comic panel by panel; a
finished panel is then animated in giantstudio. Until now those were two
databases' worth of rows with nothing between them — someone read a list of
approved panels off one screen and typed sequences into another.

**"One finished panel is one sequence"** is the studio's own rule, and it does
all the work here. Given it, every other pairing follows:

    giantflow                       giantstudio
    ─────────────────────           ───────────────────
    Series   (the comic)      ══►   Series
    Chapter                   ══►   Episode
    Batch    (who does what)   ─    (no counterpart)
    Panel                     ══►   Sequence

So exactly one thing has to be decided by a person — which production Series a
comic delivers into — and it is decided once, on the comic. Everything below it
is derived. A batch has no counterpart because it is not a tier of the work: it
is how a chapter is split between artists, which is a fact about the panel side
only.

**On approval, not on submission.** The studio says a panel is handed over when
it is "finished and submitted", but submission is the request for a verdict, not
the verdict — a submitted panel may still come back. Delivering then would put
sequences on the production board for work that is about to be rejected, and
taking them away again is worse than adding them late.

**Delivering twice is the failure to design against.** A panel can be reopened
and approved again; a route can be retried. Each panel records the sequence it
became, so the second approval finds it and does nothing.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Session, select

from flowboard.db.models import (
    FlowBatch,
    FlowChapter,
    FlowPanel,
    Node,
    Scene,
    Series,
    Shot,
)
from flowboard.services import panel_service as ps
from flowboard.services import scene_service, shot_service
from flowboard.short_id import generate_unique_short_id


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DeliveryError(Exception):
    """``code`` is a short vocab the route maps to a status."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class Delivered:
    """What the handover produced, for the caller to report."""

    shot_id: uuid.UUID
    scene_id: uuid.UUID
    #: False when this panel had already been delivered and we found it again.
    created: bool


# ── the link ────────────────────────────────────────────────────────────────


def link_series(session: Session, flow_series_id: int, studio_series_id: Optional[uuid.UUID]):
    """Point a comic at the production series it delivers into, or unlink it.

    Unlinking leaves everything already delivered alone. Sequences that exist are
    work someone may have started; withdrawing them because a link changed would
    destroy that, and the link is a routing decision, not a claim of ownership.
    """
    comic = ps.get_series(session, flow_series_id)
    if studio_series_id is not None and session.get(Series, studio_series_id) is None:
        raise DeliveryError("not_found", "that production series does not exist")
    comic.studio_series_id = studio_series_id
    session.add(comic)
    session.commit()
    session.refresh(comic)
    return comic


def linked_series(session: Session, flow_series_id: int) -> Optional[Series]:
    comic = ps.get_series(session, flow_series_id)
    if comic.studio_series_id is None:
        return None
    return session.get(Series, comic.studio_series_id)


# ── the handover ────────────────────────────────────────────────────────────


def deliver_panel(session: Session, panel_id: int) -> Optional[Delivered]:
    """Hand one approved panel over as a sequence.

    Returns ``None`` when there is nothing to do — the comic is not linked, or
    the panel is not approved. Silence rather than an error: most comics never
    deliver, and a verdict on one of those is not a failure.
    """
    panel = ps.get_panel(session, panel_id)
    if panel.status != "approved":
        return None

    batch = ps.get_batch(session, panel.batch_id)
    chapter = ps.get_chapter(session, batch.chapter_id)
    comic = ps.get_series(session, chapter.series_id)
    if comic.studio_series_id is None:
        return None
    studio_series = session.get(Series, comic.studio_series_id)
    if studio_series is None:
        # The production series was deleted out from under the link. Refuse
        # loudly: silently dropping the handover would lose finished work.
        raise DeliveryError(
            "not_found",
            f"“{comic.name}” is linked to a production series that no longer exists",
        )

    # Already handed over — a reopen-and-re-approve, or a retried request.
    if panel.studio_shot_id is not None:
        existing = session.get(Shot, panel.studio_shot_id)
        if existing is not None:
            return Delivered(shot_id=existing.id, scene_id=existing.scene_id, created=False)
        # The sequence was deleted downstream. Clear the stale pointer and make
        # a new one: the panel is still approved, so it still owes a sequence.
        panel.studio_shot_id = None

    scene = _episode_for(session, chapter, studio_series)
    shot = shot_service.create_shot(
        session,
        scene.id,
        # The panel's own position, NOT the next free slot. Panels are approved
        # in whatever order the PM gets to them, so arrival order would make
        # panel 8 into Sequence 1 whenever it was reviewed first — and the
        # numbering would then shuffle as the rest caught up. Pinning it here
        # means panel 8 is Sequence 8 from the moment it lands, and the gaps
        # where 6 and 7 will go are visible instead of imaginary.
        order_index=_chapter_position(session, panel, chapter),
        code=panel.code or "",
        script_text="",
    )
    _record_source(session, panel, shot)

    panel.studio_shot_id = shot.id
    session.add(panel)
    session.commit()
    return Delivered(shot_id=shot.id, scene_id=scene.id, created=True)


def _chapter_position(session: Session, panel: FlowPanel, chapter: FlowChapter) -> int:
    """Where this panel sits in its CHAPTER, counting from 0.

    Not `panel.order_index`, which counts within a BATCH. A batch is one
    artist's share of a chapter, so a chapter split three ways holds three
    panels all numbered 0 — and delivering them would put three sequences on
    top of each other in slot 1. The chapter is the tier the episode
    corresponds to, so the chapter is what the position has to be measured in.

    Batches in their own order, panels in theirs, counted through: exactly the
    reading order the person who cut the pages laid down.
    """
    batches = session.exec(
        select(FlowBatch)
        .where(FlowBatch.chapter_id == chapter.id)
        .order_by(FlowBatch.order_index, FlowBatch.id)
    ).all()
    n = 0
    for b in batches:
        panels = session.exec(
            select(FlowPanel)
            .where(FlowPanel.batch_id == b.id)
            .order_by(FlowPanel.order_index, FlowPanel.id)
        ).all()
        for p in panels:
            if p.id == panel.id:
                return n
            n += 1
    # Unreachable while the panel is in this chapter; falling back to appending
    # is better than raising over a position.
    return n


def _episode_for(session: Session, chapter: FlowChapter, studio_series: Series) -> Scene:
    """The episode this chapter became, making it on first use."""
    if chapter.studio_scene_id is not None:
        scene = session.get(Scene, chapter.studio_scene_id)
        if scene is not None:
            return scene
        # Deleted downstream; fall through and make another rather than fail.

    scene = scene_service.create_scene(
        session,
        studio_series.project_id,
        name=chapter.name,
        series_id=studio_series.id,
    )
    chapter.studio_scene_id = scene.id
    session.add(chapter)
    session.commit()
    return scene


def _record_source(session: Session, panel: FlowPanel, shot: Shot) -> None:
    """Put the approved artwork ON the sequence's canvas, as a reference node.

    The first cut wrote the media id into ``shot.production`` and stopped there.
    That was true and useless: the sequence opened as an empty box, and the
    animator had to go and find the panel they had just been handed. A record
    nothing reads is not a handover.

    So the panel arrives as a ``visual_asset`` node — the same node the library's
    click-to-spawn creates, with the same ``data`` shape — because that is what
    the canvas already knows how to draw, wire into a prompt and feed to i2v.
    Anything else would be a second kind of image node to teach every consumer
    about.

    ``shot.production`` still records the pairing. The node is what a person
    sees; that is what code asks when it needs to know which panel this came
    from, and it survives the artist deleting or replacing the node.
    """
    delivered = ps.delivered(session, panel.id)
    if delivered is None:
        return

    shot.production = {
        **(shot.production or {}),
        "source_panel": {
            "panel_id": panel.id,
            "code": panel.code,
            "media_id": delivered.media_id,
        },
    }
    session.add(shot)

    session.add(
        Node(
            shot_id=shot.id,
            short_id=generate_unique_short_id(session, shot.id),
            type="visual_asset",
            # Top-left of an empty canvas. Not centred: the canvas has no size
            # until it is opened, and every sequence starting at the same place
            # is easier to work with than every sequence starting somewhere
            # slightly different.
            x=80,
            y=80,
            w=320,
            h=320,
            data={
                "title": panel.code or "panel",
                "mediaId": delivered.media_id,
                "status": "done",
                "renderedAt": _utcnow().isoformat(),
                # Where it came from, on the node itself — the artist reads this
                # long before anyone queries `shot.production`.
                "sourcePanelId": panel.id,
            },
            status="done",
        )
    )
    session.commit()


# ── reporting ───────────────────────────────────────────────────────────────


def delivery_state(session: Session, flow_series_id: int) -> dict:
    """How much of this comic has crossed over — the number a PM asks for."""
    comic = ps.get_series(session, flow_series_id)
    studio = session.get(Series, comic.studio_series_id) if comic.studio_series_id else None
    chapters = session.exec(
        select(FlowChapter).where(FlowChapter.series_id == flow_series_id)
    ).all()
    panels = ps.list_series_panels(session, flow_series_id)
    approved = [p for p in panels if p.status == "approved"]
    return {
        "linked": studio is not None,
        "studio_series_id": str(studio.id) if studio else None,
        "studio_series_name": studio.name if studio else None,
        "approved": len(approved),
        "delivered": sum(1 for p in approved if p.studio_shot_id),
        "episodes": sum(1 for c in chapters if c.studio_scene_id),
    }
