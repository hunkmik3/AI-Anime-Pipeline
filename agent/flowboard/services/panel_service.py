"""Giantflow panel production — projects, panels, images, notes.

The studio adapts comics panel by panel. Someone cuts the original pages into a
folder of panels ("raw material"), an artist restyles each one with AI, a PM
reviews it and sends it back with notes until it passes. That review used to run
on a Miro board; this module is that board, with generation attached rather than
alongside.

Three rules shape everything here:

1. **The panel is the unit of work** — assigned, statused, noted, exported. The
   project is just the comic it belongs to.
2. **Order comes from the cutter**, via filename. The app preserves what it was
   given and never re-derives reading order.
3. **Approved locks generation.** The lock is enforced here, not in the UI, or it
   is decoration.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Session, select

from flowboard.db.models import (
    FlowBatch,
    FlowPanel,
    FlowPanelImage,
    FlowPanelNote,
    FlowProject,
    FlowProjectMember,
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


def create_project(
    session: Session, name: str, *, created_by: Optional[uuid.UUID] = None
) -> FlowProject:
    clean = (name or "").strip()
    if not clean:
        raise PanelError("bad_input", "a project name is required")
    row = FlowProject(name=clean, created_by=created_by)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def get_project(session: Session, project_id: int) -> FlowProject:
    row = session.get(FlowProject, project_id)
    if row is None:
        raise PanelError("not_found", "project not found")
    return row


def list_projects(session: Session) -> list[FlowProject]:
    return list(
        session.exec(
            select(FlowProject).order_by(FlowProject.order_index, FlowProject.id)
        ).all()
    )


def reorder(session: Session, model, ids: list[int], *, scope=None) -> int:
    """Write a hand-arranged order.

    Takes the ids in their new order and numbers them 0..n. Ids that don't belong
    (deleted meanwhile, or from another project) are skipped rather than
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


def reorder_projects(session: Session, ids: list[int]) -> int:
    return reorder(session, FlowProject, ids)


def reorder_batches(session: Session, project_id: int, ids: list[int]) -> int:
    get_project(session, project_id)
    return reorder(
        session, FlowBatch, ids, scope=(FlowBatch.project_id == project_id)
    )


def project_cover_media_id(session: Session, project: FlowProject) -> Optional[str]:
    """What to show on a project's card.

    A hand-picked cover always wins — someone chose it. Otherwise fall back to the
    **first panel of the first batch**, which is the comic's opening image and
    needs no upload step. Same rule as the episode cards.
    """
    if project.cover_media_id:
        return project.cover_media_id
    for batch in list_batches(session, project.id):
        for panel in list_panels(session, batch.id):
            raws = panel_images(session, panel.id, role="raw")
            if raws:
                return raws[0].media_id
    return None


def set_project_cover(
    session: Session, project_id: int, media_id: Optional[str]
) -> FlowProject:
    """Set, or clear with None (back to the first-panel fallback)."""
    row = get_project(session, project_id)
    row.cover_media_id = (media_id or "").strip() or None
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def rename_project(session: Session, project_id: int, name: str) -> FlowProject:
    clean = (name or "").strip()
    if not clean:
        raise PanelError("bad_input", "a project name is required")
    row = get_project(session, project_id)
    row.name = clean
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def delete_project(session: Session, project_id: int) -> None:
    """Delete a project and everything under it.

    Panels, their images and notes go with it (FK CASCADE) — unlike the old
    board delete, which detached a shared library. Here the panels ARE the
    project: keeping them without it would leave rows nothing can reach. The
    cached media files are untouched, so the pixels survive a mistaken delete
    even though the catalogue does not.
    """
    row = get_project(session, project_id)
    session.delete(row)
    session.commit()


# ── Batches (one artist's share of a comic) ─────────────────────────────────


def create_batch(
    session: Session,
    project_id: int,
    name: str,
    *,
    assignee_user_id: Optional[uuid.UUID] = None,
) -> FlowBatch:
    clean = (name or "").strip()
    if not clean:
        raise PanelError("bad_input", "a batch name is required")
    get_project(session, project_id)
    n = len(list_batches(session, project_id))
    row = FlowBatch(
        project_id=project_id,
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
    project_id: int,
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
    get_project(session, project_id)
    clean = [(n.strip(), a) for n, a in rows if n and n.strip()]
    if not clean:
        raise PanelError("bad_input", "give at least one batch a name")

    start = len(list_batches(session, project_id))
    made: list[FlowBatch] = []
    for i, (name, assignee) in enumerate(clean):
        row = FlowBatch(
            project_id=project_id,
            name=name,
            assignee_user_id=assignee,
            order_index=start + i,
        )
        session.add(row)
        made.append(row)
    session.commit()
    for row in made:
        session.refresh(row)
    return made


def list_batches(session: Session, project_id: int) -> list[FlowBatch]:
    return list(
        session.exec(
            select(FlowBatch)
            .where(FlowBatch.project_id == project_id)
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


def project_of_batch(session: Session, batch_id: int) -> FlowProject:
    return get_project(session, get_batch(session, batch_id).project_id)


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

    Re-importing into a project that already has panels is refused rather than
    merged: a second folder almost always means "I meant a new project", and
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

    Repairs a project imported before the sort was applied, so an existing board
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


def list_project_panels(session: Session, project_id: int) -> list[FlowPanel]:
    """Every panel in a comic, batch by batch, each batch in its own order."""
    out: list[FlowPanel] = []
    for b in list_batches(session, project_id):
        out.extend(list_panels(session, b.id))
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
    return out


# ── Review ──────────────────────────────────────────────────────────────────


def submit_panel(session: Session, panel_id: int) -> FlowPanel:
    panel = get_panel(session, panel_id)
    if panel.status == "approved":
        raise PanelError("closed", "this panel is already approved")
    if not panel_images(session, panel_id, role="generated"):
        raise PanelError("bad_input", "nothing to review — generate a version first")
    panel.status = "submitted"
    panel.updated_at = _utcnow()
    session.add(panel)
    session.commit()
    session.refresh(panel)
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

    Approving locks generation. Sending back requires at least one note — a
    rejection with no reason is the thing the Miro board never did, and it is
    what makes an artist guess.
    """
    panel = get_panel(session, panel_id)
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
    return panel


def reopen_panel(session: Session, panel_id: int) -> FlowPanel:
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


def list_members(session: Session, project_id: int) -> list[FlowProjectMember]:
    return list(
        session.exec(
            select(FlowProjectMember).where(FlowProjectMember.project_id == project_id)
        ).all()
    )


def set_member(
    session: Session, project_id: int, user_id: uuid.UUID, role: str
) -> FlowProjectMember:
    from flowboard.services import permissions

    row = session.exec(
        select(FlowProjectMember).where(
            FlowProjectMember.project_id == project_id,
            FlowProjectMember.user_id == user_id,
        )
    ).first()
    if row is None:
        row = FlowProjectMember(project_id=project_id, user_id=user_id)
    row.role = permissions.normalize_role(role)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def remove_member(session: Session, project_id: int, user_id: uuid.UUID) -> None:
    row = session.exec(
        select(FlowProjectMember).where(
            FlowProjectMember.project_id == project_id,
            FlowProjectMember.user_id == user_id,
        )
    ).first()
    if row is not None:
        session.delete(row)
        session.commit()


__all__ = ["PanelError", "PANEL_STATUSES"]
