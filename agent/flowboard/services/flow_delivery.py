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
from typing import Optional

from sqlmodel import Session, select

from flowboard.db.models import (
    FlowChapter,
    FlowPanel,
    Scene,
    Series,
    Shot,
)
from flowboard.services import panel_service as ps
from flowboard.services import scene_service, shot_service


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
        code=panel.code or "",
        script_text="",
    )
    _record_source(session, panel, shot)

    panel.studio_shot_id = shot.id
    session.add(panel)
    session.commit()
    return Delivered(shot_id=shot.id, scene_id=scene.id, created=True)


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
    """Say what this sequence is *of*, on the sequence itself.

    A sequence carrying only a code tells the animator the work exists, not what
    it is a picture of. The obvious move — copy the panel into the production
    project's asset library as a Reference — does not work and must not be
    retried: ``Reference.media_id`` is unique across the WHOLE table, not per
    project, and giantflow already holds a row for this image. A second insert
    is the `UniqueViolation` this codebase has hit before, and "fixing" it by
    re-pointing the existing row would move giantflow's own panel reference into
    the production project and out of the panel workspace.

    So the link is recorded here instead: one media id on the sequence, which is
    all the studio needs to show the artwork and all giantflow needs to stay
    intact.
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
