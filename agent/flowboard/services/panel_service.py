"""Giantflow panel production — projects, panels, images, notes.

The studio adapts comics panel by panel. Someone cuts the original pages into a
folder of panels ("raw material"), an artist restyles each one with AI, a PM
reviews it and sends it back with notes until it passes. That review used to run
on a Miro board; this module is that board, with generation attached rather than
alongside.

Three rules shape everything here:

1. **The panel is the unit of work** — assigned, statused, noted, exported. The
   series is just the comic it belongs to.
2. **Order comes from the cutter**, via filename. The app preserves what it was
   given and never re-derives reading order.
3. **Approved locks generation.** The lock is enforced here, not in the UI, or it
   is decoration.
"""
from __future__ import annotations

import re
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Session, select

from flowboard.db.models import (
    FlowBatch,
    FlowChapter,
    FlowPanel,
    FlowPanelImage,
    FlowPanelEvent,
    FlowPanelNote,
    FlowProject,
    FlowSeries,
    FlowSeriesMember,
    PANEL_STATUSES,
)


class PanelError(Exception):
    """Domain error; ``code`` is a short vocab the route maps to a status."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Projects ────────────────────────────────────────────────────────────────


# ── Projects — the slate every series hangs off ─────────────────────────────


def list_projects(session: Session) -> list[FlowProject]:
    return list(
        session.exec(
            select(FlowProject).order_by(FlowProject.order_index, FlowProject.id)
        ).all()
    )


def get_project(session: Session, project_id: int) -> FlowProject:
    row = session.get(FlowProject, project_id)
    if row is None:
        raise PanelError("not_found", "project not found")
    return row


def create_project(session: Session, name: str) -> FlowProject:
    clean = (name or "").strip()
    if not clean:
        raise PanelError("bad_input", "a project name is required")
    nxt = len(list_projects(session))
    row = FlowProject(name=clean, order_index=nxt)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def update_project(
    session: Session,
    project_id: int,
    *,
    name: Optional[str] = None,
    cover_media_id: Optional[str] = None,
    set_cover: bool = False,
) -> FlowProject:
    row = get_project(session, project_id)
    if name is not None:
        clean = name.strip()
        if not clean:
            raise PanelError("bad_input", "a project name is required")
        row.name = clean
    #: `set_cover` distinguishes "clear it" from "leave it alone" — the same
    #: reason `update_batch` carries `set_assignee`.
    if set_cover:
        row.cover_media_id = cover_media_id
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def delete_project(session: Session, project_id: int) -> None:
    """Deletes its series, and through them their batches and panels."""
    row = get_project(session, project_id)
    for s in list_series(session, project_id):
        delete_series(session, s.id)
    session.delete(row)
    session.commit()


def reorder_projects(session: Session, ordered_ids: list[int]) -> None:
    for i, pid in enumerate(ordered_ids):
        row = session.get(FlowProject, pid)
        if row is not None:
            row.order_index = i
            session.add(row)
    session.commit()


def project_cover_media_id(session: Session, project_id: int) -> Optional[str]:
    """Hand-set cover, else the first cover found among its series."""
    row = get_project(session, project_id)
    if row.cover_media_id:
        return row.cover_media_id
    for s in list_series(session, project_id):
        # Takes the row, not its id — it reads `cover_media_id` off it.
        got = series_cover_media_id(session, s)
        if got:
            return got
    return None


#: Fixed prefix on every comic. The studio's own convention, and the reason it
#: is in code rather than in people's fingers: six comics had been named by hand
#: and four of them were missing it, which is the same drift the batch names were
#: given a generator to stop.
SERIES_PREFIX = "GCSA"

#: ``GCSA_26001_MAGMEL`` — prefix, a five-digit slate number, then the title.
_SERIES_NAME = re.compile(
    rf"^{SERIES_PREFIX}_(\d{{5}})_(.+)$", re.I
)


def _series_year() -> str:
    """The two digits the slate number opens with. 2026 → "26"."""
    return f"{datetime.now(timezone.utc).year % 100:02d}"


def next_series_number(session: Session, project_id: int) -> str:
    """The next slate number, and it is never one that has been issued before.

    Read from a high-water mark on the slate, not from the numbers currently in
    use. Deriving it from what exists hands a deleted comic's number to the next
    one — and that number is in exported folder names and in what people say to
    each other, so two comics sharing it is two people certain they are
    discussing the same thing.

    The mark is bumped by `claim_series_number`, which is what actually issues
    one; this only answers what is next.
    """
    project = get_project(session, project_id)
    if project.last_series_seq:
        return f"{project.last_series_seq + 1:05d}"
    # Slates that predate the mark: carry on from what is there rather than
    # restarting at 001 on top of an existing comic.
    used = [
        int(m.group(1))
        for row in list_series(session, project_id)
        if (m := _SERIES_NAME.match(row.name or ""))
    ]
    return f"{max(used) + 1:05d}" if used else f"{_series_year()}001"


def claim_series_number(session: Session, project_id: int) -> str:
    """Take the next number and record that it is gone."""
    nxt = next_series_number(session, project_id)
    project = get_project(session, project_id)
    project.last_series_seq = int(nxt)
    session.add(project)
    session.flush()
    return nxt


def series_title_of(name: str) -> str:
    """The part a person actually chose. Given a name already in the
    convention, this is what they typed; given anything else, it is the whole
    thing."""
    m = _SERIES_NAME.match((name or "").strip())
    return m.group(2) if m else (name or "").strip()


def series_name(session: Session, project_id: int, title: str) -> str:
    """Build the full name from a title.

    A name already in the convention is kept as it is — re-saving a comic must
    not renumber it, and typing its full name must not produce
    ``GCSA_26009_GCSA_26001_MAGMEL``.
    """
    clean = (title or "").strip()
    m = _SERIES_NAME.match(clean)
    if m:
        # A name brought in from outside still spends its number. Without this
        # the mark stays where it was and the next generated comic is numbered
        # BELOW one already on the slate — not a collision, but a numbering that
        # runs backwards, which is worse to read than a gap.
        project = get_project(session, project_id)
        given = int(m.group(1))
        if given > (project.last_series_seq or 0):
            project.last_series_seq = given
            session.add(project)
            session.flush()
        return clean
    tail = _token(clean).upper()
    return f"{SERIES_PREFIX}_{claim_series_number(session, project_id)}_{tail}"


def create_series(
    session: Session,
    project_id: int,
    name: str,
    *,
    created_by: Optional[uuid.UUID] = None,
) -> FlowSeries:
    clean = (name or "").strip()
    if not clean:
        raise PanelError("bad_input", "a series name is required")
    get_project(session, project_id)  # 404 rather than a dangling foreign key
    # The caller types a TITLE; the number and prefix are the studio's, not
    # theirs. Typing a full conventional name is recognised and left alone.
    clean = series_name(session, project_id, clean)
    nxt = len(list_series(session, project_id))
    row = FlowSeries(
        project_id=project_id, name=clean, created_by=created_by, order_index=nxt
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def get_series(session: Session, series_id: int) -> FlowSeries:
    row = session.get(FlowSeries, series_id)
    if row is None:
        raise PanelError("not_found", "series not found")
    return row


def list_series(session: Session, project_id: Optional[int] = None) -> list[FlowSeries]:
    """The comics on a slate, or every comic when no project is named."""
    stmt = select(FlowSeries)
    if project_id is not None:
        stmt = stmt.where(FlowSeries.project_id == project_id)
    return list(session.exec(stmt.order_by(FlowSeries.order_index, FlowSeries.id)).all())


def reorder(session: Session, model, ids: list[int], *, scope=None) -> int:
    """Write a hand-arranged order.

    Takes the ids in their new order and numbers them 0..n. Ids that don't belong
    (deleted meanwhile, or from another series) are skipped rather than
    rejected: a stale tab should not make the whole drag fail, and the rows it
    does know about still end up in the right sequence.

    Anything the caller omitted keeps a number past the end, so a partial list
    reorders what it names without scattering the rest.
    """
    rows = {r.id: r for r in session.exec(
        select(model) if scope is None else select(model).where(scope)
    ).all()}
    seen: set[int] = set()
    i = 0
    for rid in ids:
        row = rows.get(rid)
        if row is None or rid in seen:
            continue
        seen.add(rid)
        row.order_index = i
        session.add(row)
        i += 1
    # Everything not named keeps its relative order, after the named ones.
    for rid, row in sorted(rows.items(), key=lambda kv: (kv[1].order_index, kv[0])):
        if rid in seen:
            continue
        row.order_index = i
        session.add(row)
        i += 1
    session.commit()
    return len(seen)


def reorder_series(session: Session, project_id: int, ids: list[int]) -> int:
    """Scoped to the slate, for the same reason chapters are scoped to a comic."""
    get_project(session, project_id)
    return reorder(
        session, FlowSeries, ids, scope=(FlowSeries.project_id == project_id)
    )


def reorder_batches(session: Session, chapter_id: int, ids: list[int]) -> int:
    get_chapter(session, chapter_id)
    return reorder(
        session, FlowBatch, ids, scope=(FlowBatch.chapter_id == chapter_id)
    )


def series_cover_media_id(session: Session, series: FlowSeries) -> Optional[str]:
    """What to show on a series's card.

    A hand-picked cover always wins — someone chose it. Otherwise fall back to the
    **first panel of the first batch**, which is the comic's opening image and
    needs no upload step. Same rule as the episode cards.
    """
    if series.cover_media_id:
        return series.cover_media_id
    # Through the chapters: batches no longer hang off the series, and passing a
    # series id to `list_batches` returns whatever batch happens to sit under the
    # chapter with that id — a wrong answer that never raises.
    for chapter in list_chapters(session, series.id):
        got = chapter_cover_media_id(session, chapter)
        if got:
            return got
    return None


def set_series_cover(
    session: Session, series_id: int, media_id: Optional[str]
) -> FlowSeries:
    """Set, or clear with None (back to the first-panel fallback)."""
    row = get_series(session, series_id)
    row.cover_media_id = (media_id or "").strip() or None
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def update_series(
    session: Session,
    series_id: int,
    *,
    name: Optional[str] = None,
    due_date=None,
    set_due: bool = False,
) -> FlowSeries:
    """Rename and/or set a deadline. ``set_due`` distinguishes "clear it" from
    "leave it alone" — the same reason `update_batch` carries `set_assignee`."""
    row = get_series(session, series_id)
    if name is not None:
        clean = name.strip()
        if not clean:
            raise PanelError("bad_input", "a series name is required")
        row.name = clean
    if set_due:
        row.due_date = due_date
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def rename_series(session: Session, series_id: int, name: str) -> FlowSeries:
    clean = (name or "").strip()
    if not clean:
        raise PanelError("bad_input", "a series name is required")
    row = get_series(session, series_id)
    row.name = clean
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def delete_series(session: Session, series_id: int) -> None:
    """Delete a series and everything under it.

    Panels, their images and notes go with it (FK CASCADE) — unlike the old
    board delete, which detached a shared library. Here the panels ARE the
    series: keeping them without it would leave rows nothing can reach. The
    cached media files are untouched, so the pixels survive a mistaken delete
    even though the catalogue does not.
    """
    row = get_series(session, series_id)
    session.delete(row)
    session.commit()


# ── Batches (one artist's share of a comic) ─────────────────────────────────


# ── Chapters — the tier work is divided on ─────────────────────────────────


def list_chapters(session: Session, series_id: int) -> list[FlowChapter]:
    return list(
        session.exec(
            select(FlowChapter)
            .where(FlowChapter.series_id == series_id)
            .order_by(FlowChapter.order_index, FlowChapter.id)
        ).all()
    )


def get_chapter(session: Session, chapter_id: int) -> FlowChapter:
    row = session.get(FlowChapter, chapter_id)
    if row is None:
        raise PanelError("not_found", "chapter not found")
    return row


def create_chapter(
    session: Session,
    series_id: int,
    name: str,
    *,
    created_by: Optional[uuid.UUID] = None,
    due_date=None,
) -> FlowChapter:
    clean = (name or "").strip()
    if not clean:
        raise PanelError("bad_input", "a chapter name is required")
    get_series(session, series_id)
    nxt = len(list_chapters(session, series_id))
    row = FlowChapter(
        series_id=series_id, name=clean, order_index=nxt,
        created_by=created_by, due_date=due_date,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def update_chapter(
    session: Session,
    chapter_id: int,
    *,
    name: Optional[str] = None,
    cover_media_id: Optional[str] = None,
    set_cover: bool = False,
    due_date=None,
    set_due: bool = False,
) -> FlowChapter:
    row = get_chapter(session, chapter_id)
    if set_due:
        row.due_date = due_date
    if name is not None:
        clean = name.strip()
        if not clean:
            raise PanelError("bad_input", "a chapter name is required")
        row.name = clean
    if set_cover:
        row.cover_media_id = cover_media_id
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def delete_chapter(session: Session, chapter_id: int) -> None:
    """Takes its batches, and through them their panels."""
    row = get_chapter(session, chapter_id)
    for b in list_batches(session, chapter_id):
        delete_batch(session, b.id)
    session.delete(row)
    session.commit()


def reorder_chapters(session: Session, series_id: int, ids: list[int]) -> int:
    """Scoped to the comic. An unscoped version accepted any chapter id, so a
    drag on one comic renumbered another's — the ids carry no ownership and
    nothing was checking."""
    get_series(session, series_id)
    return reorder(
        session, FlowChapter, ids, scope=(FlowChapter.series_id == series_id)
    )


def chapter_cover_media_id(session: Session, chapter: FlowChapter) -> Optional[str]:
    """Hand-picked cover, else the first panel of its first batch."""
    if chapter.cover_media_id:
        return chapter.cover_media_id
    for batch in list_batches(session, chapter.id):
        for panel in list_panels(session, batch.id):
            raws = panel_images(session, panel.id, role="raw")
            if raws:
                return raws[0].media_id
    return None


def series_of_chapter(session: Session, chapter_id: int) -> FlowSeries:
    return get_series(session, get_chapter(session, chapter_id).series_id)


def list_chapter_panels(session: Session, chapter_id: int) -> list[FlowPanel]:
    return [p for b in list_batches(session, chapter_id) for p in list_panels(session, b.id)]


_NAME_TOKEN = re.compile(r"[^A-Za-z0-9]+")


def _token(text: str) -> str:
    """One path segment of a batch name: letters and digits, joined by dashes.

    Accented letters are FOLDED to their base, not dropped. The pattern only
    keeps ASCII — because these end up in exported folder names — and applied
    directly to Vietnamese it ate the letters instead of transliterating them:
    "Đường Về Nhà" came out "NG-V-NH", which nobody can read and which the
    production side then inherited as the name of a series.
    """
    seed = (text or "").strip().replace("Đ", "D").replace("đ", "d")
    folded = unicodedata.normalize("NFD", seed)
    ascii_only = "".join(c for c in folded if unicodedata.category(c) != "Mn")
    return _NAME_TOKEN.sub("-", ascii_only).strip("-") or "x"


def batch_name_prefix(session: Session, chapter_id: int) -> str:
    """``Project_Series_Chapter`` for this chapter, as a batch-name stem.

    Built server-side because only the server knows all four tiers and how many
    batches already exist. Typed names were the alternative and they drift:
    "Quân", "quan", "26004_UL-X-MEN_Quân" all appeared in the same comic, and
    exported folders inherit whatever was typed.
    """
    chapter = get_chapter(session, chapter_id)
    series = get_series(session, chapter.series_id)
    project = get_project(session, series.project_id)
    return f"{_token(project.name)}_{_token(series.name)}_{_token(chapter.name)}"


def default_batch_name(session: Session, chapter_id: int, seq: int) -> str:
    """``Project_Series_Chapter_batchNN``. ``seq`` is 1-based."""
    return f"{batch_name_prefix(session, chapter_id)}_batch{seq:02d}"


def create_batch(
    session: Session,
    chapter_id: int,
    name: str,
    *,
    assignee_user_id: Optional[uuid.UUID] = None,
) -> FlowBatch:
    get_chapter(session, chapter_id)
    n = len(list_batches(session, chapter_id))
    # A blank name is the normal case now: the studio wanted one convention, not
    # whatever each PM typed.
    clean = (name or "").strip() or default_batch_name(session, chapter_id, n + 1)
    row = FlowBatch(
        chapter_id=chapter_id,
        name=clean,
        assignee_user_id=assignee_user_id,
        order_index=n,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def create_batches(
    session: Session,
    chapter_id: int,
    rows: list[tuple[str, Optional[uuid.UUID]]],
) -> list[FlowBatch]:
    """Create several batches at once — the way work is actually handed out.

    A comic is divided among its artists in one sitting, so making that six
    separate actions is six chances to lose track of who already has something.
    Rows with a blank name are dropped rather than rejected: a form that starts
    with spare rows should not punish leaving them empty.

    All or nothing: one commit, so a failure halfway does not leave half a
    division in place.
    """
    get_chapter(session, chapter_id)
    if not rows:
        raise PanelError("bad_input", "add at least one batch")

    start = len(list_batches(session, chapter_id))
    made: list[FlowBatch] = []
    for i, (name, assignee) in enumerate(rows):
        # Blank means "name it by the convention" rather than "skip me": with
        # names generated, the only thing a row carries is who it is for.
        given = (name or "").strip()
        row = FlowBatch(
            chapter_id=chapter_id,
            name=given or default_batch_name(session, chapter_id, start + i + 1),
            assignee_user_id=assignee,
            order_index=start + i,
        )
        session.add(row)
        made.append(row)
    session.commit()
    for row in made:
        session.refresh(row)
    return made


def list_batches(session: Session, chapter_id: int) -> list[FlowBatch]:
    return list(
        session.exec(
            select(FlowBatch)
            .where(FlowBatch.chapter_id == chapter_id)
            .order_by(FlowBatch.order_index, FlowBatch.id)
        ).all()
    )


def get_batch(session: Session, batch_id: int) -> FlowBatch:
    row = session.get(FlowBatch, batch_id)
    if row is None:
        raise PanelError("not_found", "batch not found")
    return row


def update_batch(
    session: Session,
    batch_id: int,
    *,
    name: Optional[str] = None,
    assignee_user_id: Optional[uuid.UUID] = None,
    set_assignee: bool = False,
) -> FlowBatch:
    """Rename and/or reassign.

    ``set_assignee`` exists because None is a real value here — "take this off
    whoever had it" has to be distinguishable from "leave the assignee alone".
    """
    row = get_batch(session, batch_id)
    if name is not None:
        clean = name.strip()
        if not clean:
            raise PanelError("bad_input", "a batch name is required")
        row.name = clean
    if set_assignee:
        row.assignee_user_id = assignee_user_id
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def delete_batch(session: Session, batch_id: int) -> None:
    """Delete a batch and its panels (FK CASCADE).

    The panels ARE the batch's contents — its imported folder — so they go with
    it. Cached media files are untouched, so a mistaken delete loses the
    catalogue, not the pixels.
    """
    session.delete(get_batch(session, batch_id))
    session.commit()


def series_of_batch(session: Session, batch_id: int) -> FlowSeries:
    """The comic a batch belongs to, reached through its chapter."""
    return series_of_chapter(session, get_batch(session, batch_id).chapter_id)


# ── Import ──────────────────────────────────────────────────────────────────

#: Panels arrive as a folder. A SUBFOLDER is one panel and every file in it is one
#: of that panel's raw pieces; a loose file is a panel with a single piece. That is
#: the whole rule — it handles the multi-piece case (one Miro row carried three)
#: without a naming convention anyone has to memorise.
_PIECE_SEP = "/"

_SAFE_CODE = re.compile(r"[^\w.\- ]+")


def panel_code_from_path(rel_path: str) -> str:
    """The panel a file belongs to, from its path inside the imported folder.

    ``PANEL006/a.png`` → ``PANEL006`` (a multi-piece panel)
    ``PANEL007.png``   → ``PANEL007`` (a single-piece panel)

    Kept as the cutter wrote it, minus characters that would be awkward in a
    filename on export, so codes still match their sheet and the Miro history.
    """
    clean = (rel_path or "").replace("\\", "/").strip("/")
    if not clean:
        return ""
    head, sep, _tail = clean.partition(_PIECE_SEP)
    stem = head if sep else head.rsplit(".", 1)[0]
    # strip the replacement char too: "weird:name*.png" would otherwise carry a
    # trailing underscore into every exported filename.
    return _SAFE_CODE.sub("_", stem).strip(" _") or "panel"


def natural_key(path: str) -> list:
    """Sort key that reads digit runs as numbers: PANEL9 before PANEL10.

    The cutter's numbering is usually zero-padded, where plain text sorting would
    do — but it costs nothing to be right when it isn't, and a folder that sorts
    correctly in Finder and wrongly in the app is a bug nobody can explain.
    """
    parts = re.split(r"(\d+)", (path or "").lower())
    return [int(x) if x.isdigit() else x for x in parts]


def import_panels(
    session: Session,
    batch_id: int,
    *,
    entries: list[tuple[str, str]],
) -> list[FlowPanel]:
    """Create panels from an imported raw-material folder.

    ``entries`` is ``[(relative_path, media_id), …]`` — the caller has already
    cached the bytes; this only records what belongs to whom.

    **Order comes from the FILENAME, sorted here.** The obvious design was to
    trust the caller's order, since the cutter sorted the folder deliberately —
    but a browser's folder picker hands over a ``FileList`` in filesystem order,
    not the sorted order the human sees in Finder. Importing 136 panels that way
    produced PANEL111, PANEL105, PANEL065… The filename IS how the cutter
    expressed the order, so sorting by it honours their intent rather than
    overriding it; the arrival order was never carrying that information.

    Re-importing into a series that already has panels is refused rather than
    merged: a second folder almost always means "I meant a new series", and
    silently interleaving two numbering schemes is not recoverable by hand.
    """
    get_batch(session, batch_id)
    existing = session.exec(
        select(FlowPanel).where(FlowPanel.batch_id == batch_id).limit(1)
    ).first()
    if existing is not None:
        raise PanelError(
            "closed",
            "this batch already has panels — import into a new batch instead, "
            "so two numbering schemes don't interleave",
        )
    if not entries:
        raise PanelError("bad_input", "no image files found in that folder")

    panels: dict[str, FlowPanel] = {}
    order = 0
    for rel_path, media_id in sorted(entries, key=lambda e: natural_key(e[0])):
        code = panel_code_from_path(rel_path)
        if not code or not media_id:
            continue
        panel = panels.get(code)
        if panel is None:
            panel = FlowPanel(batch_id=batch_id, code=code, order_index=order)
            session.add(panel)
            session.flush()  # need the id for its images
            panels[code] = panel
            order += 1
        n = len(
            session.exec(
                select(FlowPanelImage).where(
                    FlowPanelImage.panel_id == panel.id,
                    FlowPanelImage.role == "raw",
                )
            ).all()
        )
        session.add(
            FlowPanelImage(
                panel_id=panel.id,
                role="raw",
                version=n + 1,
                media_id=media_id,
            )
        )
    if not panels:
        raise PanelError("bad_input", "no usable image files in that folder")
    session.commit()
    return list_panels(session, batch_id)


def renumber_panels(session: Session, batch_id: int) -> int:
    """Re-derive ``order_index`` from the panel codes, natural-sorted.

    Repairs a series imported before the sort was applied, so an existing board
    does not have to be deleted and re-uploaded to come out in reading order.
    """
    panels = sorted(
        session.exec(select(FlowPanel).where(FlowPanel.batch_id == batch_id)).all(),
        key=lambda p: natural_key(p.code),
    )
    for i, panel in enumerate(panels):
        if panel.order_index != i:
            panel.order_index = i
            session.add(panel)
    session.commit()
    return len(panels)


# ── Panels ──────────────────────────────────────────────────────────────────


def list_panels(session: Session, batch_id: int) -> list[FlowPanel]:
    return list(
        session.exec(
            select(FlowPanel)
            .where(FlowPanel.batch_id == batch_id)
            .order_by(FlowPanel.order_index, FlowPanel.id)
        ).all()
    )


def search_panels(
    session: Session,
    *,
    statuses: Optional[list[str]] = None,
    series_id: Optional[int] = None,
    batch_id: Optional[int] = None,
    assignee_user_id: Optional[uuid.UUID] = None,
    q: Optional[str] = None,
    limit: int = 1000,
) -> list[FlowPanel]:
    """Every panel matching the filters, across batches and comics.

    The per-batch grid answers "how is this share going"; this answers "where is
    anything, right now" — which panel of Quân's came back, how many of the whole
    comic nobody has started. Neither the batch grid nor the two queues could say
    that: one is nailed to a single batch, the others to a single status.

    Ordered by the studio's own numbering (``order_index``) rather than by
    recency, because a manager reading a comic reads it in page order.
    """
    stmt = select(FlowPanel).join(FlowBatch, FlowBatch.id == FlowPanel.batch_id)
    if statuses:
        stmt = stmt.where(FlowPanel.status.in_(statuses))
    if series_id is not None:
        # A batch reaches its comic through its chapter now.
        stmt = stmt.join(FlowChapter, FlowChapter.id == FlowBatch.chapter_id).where(
            FlowChapter.series_id == series_id
        )
    if batch_id is not None:
        stmt = stmt.where(FlowPanel.batch_id == batch_id)
    if assignee_user_id is not None:
        stmt = stmt.where(FlowBatch.assignee_user_id == assignee_user_id)
    if q and q.strip():
        stmt = stmt.where(FlowPanel.code.ilike(f"%{q.strip()}%"))
    stmt = stmt.order_by(
        FlowBatch.chapter_id, FlowBatch.order_index, FlowPanel.order_index, FlowPanel.id
    ).limit(limit)
    return list(session.exec(stmt).all())


def panels_by_status(
    session: Session,
    statuses: list[str],
    *,
    assignee_user_id: Optional[uuid.UUID] = None,
    series_id: Optional[int] = None,
    limit: int = 500,
) -> list[FlowPanel]:
    """Panels in any of ``statuses``, across every batch, newest activity first.

    A queue, not a tree. Review at this studio's scale means 300 panels handed in
    by several artists, and walking series → batch → panel to find the ones
    waiting is the shape of the Miro board this replaces, not an improvement on
    it. Ordered by ``updated_at`` because the thing a reviewer wants is what
    changed, and an artist wants the verdict that just landed.

    ``assignee_user_id`` filters to one artist's own work — the batch carries the
    assignee, so the join goes through it.
    """
    stmt = (
        select(FlowPanel)
        .join(FlowBatch, FlowBatch.id == FlowPanel.batch_id)
        .where(FlowPanel.status.in_(statuses))
    )
    if assignee_user_id is not None:
        stmt = stmt.where(FlowBatch.assignee_user_id == assignee_user_id)
    if series_id is not None:
        stmt = stmt.join(FlowChapter, FlowChapter.id == FlowBatch.chapter_id).where(
            FlowChapter.series_id == series_id
        )
    stmt = stmt.order_by(FlowPanel.updated_at.desc(), FlowPanel.id.desc()).limit(limit)
    return list(session.exec(stmt).all())


def list_series_panels(session: Session, series_id: int) -> list[FlowPanel]:
    """Every panel in a comic — chapter by chapter, batch by batch, in order."""
    out: list[FlowPanel] = []
    for c in list_chapters(session, series_id):
        out.extend(list_chapter_panels(session, c.id))
    return out


def get_panel(session: Session, panel_id: int) -> FlowPanel:
    row = session.get(FlowPanel, panel_id)
    if row is None:
        raise PanelError("not_found", "panel not found")
    return row


def panel_images(
    session: Session, panel_id: int, *, role: Optional[str] = None
) -> list[FlowPanelImage]:
    stmt = select(FlowPanelImage).where(FlowPanelImage.panel_id == panel_id)
    if role:
        stmt = stmt.where(FlowPanelImage.role == role)
    return list(session.exec(stmt.order_by(FlowPanelImage.version, FlowPanelImage.id)).all())


def latest_generated(session: Session, panel_id: int) -> Optional[FlowPanelImage]:
    rows = panel_images(session, panel_id, role="generated")
    return rows[-1] if rows else None


def delivered(session: Session, panel_id: int) -> Optional[FlowPanelImage]:
    """The version this panel is actually delivering.

    The one the artist submitted, when they have picked one — otherwise the most
    recent. The fallback is not a default so much as a description of what a
    panel submitted before ``final_media_id`` existed was submitted under, and it
    is also the honest answer for a panel still being worked on.

    Every surface that shows "the result" — the batch grid, the batch card, the
    series cover, export — must go through this rather than
    ``latest_generated``, or a PM reviews one image and sees another.
    """
    panel = get_panel(session, panel_id)
    rows = panel_images(session, panel_id, role="generated")
    if panel.final_media_id:
        for r in rows:
            if r.media_id == panel.final_media_id:
                return r
    return rows[-1] if rows else None


def add_generated(
    session: Session,
    panel_id: int,
    media_ids: list[str],
    *,
    model_used: Optional[str] = None,
    created_by: Optional[uuid.UUID] = None,
) -> list[FlowPanelImage]:
    """Record generated images against a panel.

    Refuses when the panel is ``approved``: the work is signed off, so the app
    stops accepting new versions. Enforced here rather than by hiding a button,
    because a lock only the UI knows about is not a lock.
    """
    panel = get_panel(session, panel_id)
    if panel.status == "approved":
        raise PanelError(
            "closed",
            "this panel is approved — ask the PM to reopen it before generating again",
        )
    start = len(panel_images(session, panel_id, role="generated"))
    out: list[FlowPanelImage] = []
    for i, mid in enumerate([m for m in media_ids if m], start=1):
        row = FlowPanelImage(
            panel_id=panel_id,
            role="generated",
            version=start + i,
            media_id=mid,
            model_used=model_used,
            created_by=created_by,
        )
        session.add(row)
        out.append(row)
    if out and panel.status == "todo":
        # First result on an untouched panel — move it off the backlog so the
        # grid stops showing it as not started.
        panel.status = "in_progress"
    panel.updated_at = _utcnow()
    session.add(panel)
    session.commit()
    for row in out:
        session.refresh(row)
    for row in out:
        record_event(
            session, panel_id, "version_added",
            actor_user_id=created_by, media_id=row.media_id,
            body=(model_used or "uploaded file"),
        )

    return out


# ── Review ──────────────────────────────────────────────────────────────────


# ── History ─────────────────────────────────────────────────────────────────


def record_event(
    session: Session,
    panel_id: int,
    kind: str,
    *,
    actor_user_id: Optional[uuid.UUID] = None,
    media_id: Optional[str] = None,
    body: Optional[str] = None,
) -> FlowPanelEvent:
    row = FlowPanelEvent(
        panel_id=panel_id, kind=kind, actor_user_id=actor_user_id,
        media_id=media_id, body=body,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def list_events(session: Session, panel_id: int) -> list[FlowPanelEvent]:
    """Oldest first — a history reads forwards."""
    return list(
        session.exec(
            select(FlowPanelEvent)
            .where(FlowPanelEvent.panel_id == panel_id)
            .order_by(FlowPanelEvent.created_at, FlowPanelEvent.id)
        ).all()
    )


def submit_panel(
    session: Session,
    panel_id: int,
    *,
    media_id: Optional[str] = None,
    actor_user_id: Optional[uuid.UUID] = None,
) -> FlowPanel:
    """Hand a panel to the PM, naming the version being handed over.

    ``media_id`` is the whole point: submitting IS choosing. Ten generations and
    a submit used to say nothing about which of the ten was meant, so every
    reader fell back to the newest — the artist's seventh try was invisible.

    Omitting it keeps the existing pick, or takes the latest when there is none,
    so a caller with a single version does not have to name it.
    """
    panel = get_panel(session, panel_id)
    if panel.status == "approved":
        raise PanelError("closed", "this panel is already approved")
    rows = panel_images(session, panel_id, role="generated")
    if not rows:
        raise PanelError("bad_input", "nothing to review — generate a version first")
    if media_id:
        # Must be one of THIS panel's versions: a submission pointing at another
        # panel's image, or at raw material, would sail through review and be
        # exported as the deliverable.
        if media_id not in {r.media_id for r in rows}:
            raise PanelError("bad_input", "that image is not a version of this panel")
        panel.final_media_id = media_id
    elif not panel.final_media_id:
        panel.final_media_id = rows[-1].media_id
    panel.status = "submitted"
    panel.updated_at = _utcnow()
    session.add(panel)
    session.commit()
    session.refresh(panel)
    record_event(
        session, panel_id, "submitted",
        actor_user_id=actor_user_id, media_id=panel.final_media_id,
    )
    return panel


def review_panel(
    session: Session,
    panel_id: int,
    *,
    approve: bool,
    notes: Optional[list[str]] = None,
    author_user_id: Optional[uuid.UUID] = None,
) -> FlowPanel:
    """A PM's verdict.

    Only on a panel that has been HANDED OVER. A verdict is a reply, and without
    this check two things went wrong: an untouched panel could be approved —
    locking generation on work nobody had made, and putting a row in the export
    with no image behind it — and work still in progress could be ruled on and
    taken away from the artist mid-edit.

    Approving locks generation. Sending back requires at least one note — a
    rejection with no reason is the thing the Miro board never did, and it is
    what makes an artist guess.
    """
    panel = get_panel(session, panel_id)
    if panel.status != "submitted":
        raise PanelError(
            "bad_input",
            "this panel has not been submitted — there is nothing to rule on yet",
        )
    clean = [n.strip() for n in (notes or []) if n and n.strip()]
    if approve:
        panel.status = "approved"
    else:
        if not clean:
            raise PanelError("bad_input", "say what needs changing when sending a panel back")
        panel.status = "changes_requested"
    for body in clean:
        session.add(
            FlowPanelNote(panel_id=panel_id, body=body, author_user_id=author_user_id)
        )
    panel.updated_at = _utcnow()
    session.add(panel)
    session.commit()
    session.refresh(panel)
    record_event(
        session, panel_id,
        "approved" if approve else "changes_requested",
        actor_user_id=author_user_id,
        media_id=panel.final_media_id,
        body=" · ".join(clean) or None,
    )
    return panel


def reopen_panel(
    session: Session, panel_id: int, *, actor_user_id: Optional[uuid.UUID] = None
) -> FlowPanel:
    """Un-approve, so an approved panel can be worked on again.

    Exists because approval would otherwise be irreversible and one mis-click
    would destroy the work.
    """
    panel = get_panel(session, panel_id)
    if panel.status != "approved":
        raise PanelError("bad_input", "only an approved panel can be reopened")
    panel.status = "changes_requested"
    panel.updated_at = _utcnow()
    session.add(panel)
    session.commit()
    session.refresh(panel)
    record_event(session, panel_id, "reopened", actor_user_id=actor_user_id)
    return panel


# ── Notes ───────────────────────────────────────────────────────────────────


def list_notes(session: Session, panel_id: int) -> list[FlowPanelNote]:
    return list(
        session.exec(
            select(FlowPanelNote)
            .where(FlowPanelNote.panel_id == panel_id)
            .order_by(FlowPanelNote.created_at, FlowPanelNote.id)
        ).all()
    )


def add_note(
    session: Session,
    panel_id: int,
    body: str,
    *,
    author_user_id: Optional[uuid.UUID] = None,
) -> FlowPanelNote:
    clean = (body or "").strip()
    if not clean:
        raise PanelError("bad_input", "an empty note says nothing")
    get_panel(session, panel_id)
    row = FlowPanelNote(panel_id=panel_id, body=clean, author_user_id=author_user_id)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def set_note_resolved(
    session: Session,
    note_id: int,
    resolved: bool,
    *,
    user_id: Optional[uuid.UUID] = None,
) -> FlowPanelNote:
    """Tick or untick a remark — the Miro board's "Fixed"."""
    row = session.get(FlowPanelNote, note_id)
    if row is None:
        raise PanelError("not_found", "note not found")
    row.resolved = bool(resolved)
    row.resolved_by = user_id if resolved else None
    row.resolved_at = _utcnow() if resolved else None
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def unresolved_count(session: Session, panel_id: int) -> int:
    return len([n for n in list_notes(session, panel_id) if not n.resolved])


# ── Members ─────────────────────────────────────────────────────────────────


def list_members(session: Session, series_id: int) -> list[FlowSeriesMember]:
    return list(
        session.exec(
            select(FlowSeriesMember).where(FlowSeriesMember.series_id == series_id)
        ).all()
    )


def set_member(
    session: Session, series_id: int, user_id: uuid.UUID, role: str
) -> FlowSeriesMember:
    from flowboard.services import flow_permissions as fp

    row = session.exec(
        select(FlowSeriesMember).where(
            FlowSeriesMember.series_id == series_id,
            FlowSeriesMember.user_id == user_id,
        )
    ).first()
    if row is None:
        row = FlowSeriesMember(series_id=series_id, user_id=user_id)
    # flow_permissions, not permissions: this is a giantflow member row, and the
    # two modules deliberately govern different tables.
    row.role = fp.normalize_member_role(role)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def series_for_user(session: Session, user_id: uuid.UUID) -> list[tuple[FlowSeries, str]]:
    """Every comic this person has a standing in, as (comic, role).

    The giantflow half of the same question the admin console asks: what does
    this person have, across everything. Nothing could answer it before —
    membership was only queryable per comic.
    """
    out: list[tuple[FlowSeries, str]] = []
    for m in session.exec(
        select(FlowSeriesMember).where(FlowSeriesMember.user_id == user_id)
    ).all():
        comic = session.get(FlowSeries, m.series_id)
        if comic is not None:
            out.append((comic, m.role))
    return sorted(out, key=lambda x: (x[0].name or "").lower())


def remove_member(session: Session, series_id: int, user_id: uuid.UUID) -> None:
    row = session.exec(
        select(FlowSeriesMember).where(
            FlowSeriesMember.series_id == series_id,
            FlowSeriesMember.user_id == user_id,
        )
    ).first()
    if row is not None:
        session.delete(row)
        session.commit()


__all__ = ["PanelError", "PANEL_STATUSES"]
