"""Giantflow panel production — HTTP surface.

A separate router from ``routes/flowstudio.py`` on purpose: that one still serves
the current board UI, and keeping the new surface beside it means /giantflow keeps
working while this is built. The old board routes come out in the phase that
replaces the UI.

Gating is ``resource_guard.require_signed_in`` for now — the same posture the rest
of giantflow has today. The per-project role gate (a PM assigns and reviews, an
artist generates only on panels assigned to them) lands in its own phase, together
with the membership table it needs; putting a half-enforced version in now would
read as protection that isn't there.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from flowboard.db import get_session
from flowboard.routes.deps import get_optional_user
from flowboard.services import media as media_service
from flowboard.services import panel_service as ps
from flowboard.services import resource_guard
from flowboard.services import user_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/flowstudio", tags=["flow-panels"])

_ERROR_STATUS = {
    "not_found": 404,
    "bad_input": 400,
    "closed": 409,
    "forbidden": 403,
}


def _fail(exc: ps.PanelError) -> HTTPException:
    return HTTPException(_ERROR_STATUS.get(exc.code, 400), str(exc))


def _user_name(uid: Optional[uuid.UUID]) -> Optional[str]:
    if uid is None:
        return None
    u = user_service.get_by_id(uid)
    return (u.display_name or u.username) if u else None


# ── Projects ────────────────────────────────────────────────────────────────


class ProjectBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)


def _project_dict(session, row) -> dict:
    panels = ps.list_panels(session, row.id)
    done = len([p for p in panels if p.status == "approved"])
    return {
        "id": row.id,
        "name": row.name,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        # The two numbers a PM actually opens this list for.
        "panel_count": len(panels),
        "approved_count": done,
    }


@router.get("/projects")
def list_projects(user=Depends(get_optional_user)):
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        return [_project_dict(s, r) for r in ps.list_projects(s)]


@router.post("/projects")
def create_project(body: ProjectBody, user=Depends(get_optional_user)):
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        try:
            row = ps.create_project(
                s, body.name, created_by=(user.id if user else None)
            )
        except ps.PanelError as exc:
            raise _fail(exc)
        return _project_dict(s, row)


@router.patch("/projects/{project_id}")
def rename_project(project_id: int, body: ProjectBody, user=Depends(get_optional_user)):
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        try:
            return _project_dict(s, ps.rename_project(s, project_id, body.name))
        except ps.PanelError as exc:
            raise _fail(exc)


@router.delete("/projects/{project_id}")
def delete_project(project_id: int, user=Depends(get_optional_user)):
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        try:
            ps.delete_project(s, project_id)
        except ps.PanelError as exc:
            raise _fail(exc)
        return {"deleted": project_id}


# ── Import ──────────────────────────────────────────────────────────────────

_ALLOWED_MIME = {"image/png", "image/jpeg", "image/webp", "image/bmp"}
#: Per file. Raw panels are scans, not 4K renders — generous but not unbounded.
_MAX_FILE_BYTES = 30 * 1024 * 1024
#: A chapter is a few hundred panels; well past that means someone picked the
#: wrong folder, and finding out after a 10-minute upload is the worst version.
_MAX_FILES = 2000


@router.post("/projects/{project_id}/import")
async def import_folder(
    project_id: int,
    files: list[UploadFile] = File(...),
    paths: list[str] = Form(...),
    user=Depends(get_optional_user),
):
    """Import a raw-material folder: one panel per file, or per subfolder.

    ``paths`` carries each file's path *inside the chosen folder*
    (``webkitRelativePath``), because that is what says which panel a file belongs
    to — a browser upload otherwise arrives as a flat list of basenames and the
    ``PANEL008/a.png`` grouping is lost.

    Order is the order given. The cutter sorted the folder deliberately; the app
    records that and never re-derives reading order.
    """
    with get_session() as s:
        resource_guard.require_signed_in(s, user)

    if len(files) != len(paths):
        raise HTTPException(400, "files and paths must line up one-to-one")
    if len(files) > _MAX_FILES:
        raise HTTPException(400, f"{len(files)} files is too many — limit is {_MAX_FILES}")

    entries: list[tuple[str, str]] = []
    skipped: list[str] = []
    for upload, rel in zip(files, paths):
        mime = (upload.content_type or "").lower().split(";")[0].strip()
        if mime not in _ALLOWED_MIME:
            # Folders carry .DS_Store, thumbs.db, stray PSDs. Skipping quietly
            # would hide a wrong-folder mistake, so they are reported back.
            skipped.append(rel)
            continue
        raw = await upload.read()
        if not raw or len(raw) > _MAX_FILE_BYTES:
            skipped.append(rel)
            continue
        mid = str(uuid.uuid4())
        if not media_service.ingest_inline_bytes(mid, raw, kind="image", mime=mime):
            skipped.append(rel)
            continue
        entries.append((rel, mid))

    with get_session() as s:
        try:
            panels = ps.import_panels(s, project_id, entries=entries)
        except ps.PanelError as exc:
            raise _fail(exc)
        logger.info(
            "flowpanels: imported %d file(s) into %d panel(s) on project %s (%d skipped)",
            len(entries), len(panels), project_id, len(skipped),
        )
        return {
            "project_id": project_id,
            "panels": [_panel_dict(s, p) for p in panels],
            "imported_files": len(entries),
            "skipped": skipped[:20],
            "skipped_count": len(skipped),
        }


# ── Panels ──────────────────────────────────────────────────────────────────


def _panel_dict(session, panel, *, with_images: bool = False) -> dict:
    raws = ps.panel_images(session, panel.id, role="raw")
    latest = ps.latest_generated(session, panel.id)
    d = {
        "id": panel.id,
        "project_id": panel.project_id,
        "code": panel.code,
        "order_index": panel.order_index,
        "status": panel.status,
        "assignee_user_id": str(panel.assignee_user_id) if panel.assignee_user_id else None,
        "assignee_name": _user_name(panel.assignee_user_id),
        # The grid shows original and result side by side — that pairing is the
        # whole point of the board this replaces.
        "raw_media_id": raws[0].media_id if raws else None,
        "raw_count": len(raws),
        "latest_media_id": latest.media_id if latest else None,
        "version_count": latest.version if latest else 0,
        "unresolved_notes": ps.unresolved_count(session, panel.id),
        "updated_at": panel.updated_at.isoformat() if panel.updated_at else None,
    }
    if with_images:
        d["raw"] = [
            {"media_id": i.media_id, "version": i.version} for i in raws
        ]
        d["versions"] = [
            {
                "media_id": i.media_id,
                "version": i.version,
                "model_used": i.model_used,
                "created_at": i.created_at.isoformat() if i.created_at else None,
            }
            for i in ps.panel_images(session, panel.id, role="generated")
        ]
        d["notes"] = [
            {
                "id": n.id,
                "body": n.body,
                "resolved": n.resolved,
                "author_name": _user_name(n.author_user_id),
                "created_at": n.created_at.isoformat() if n.created_at else None,
            }
            for n in ps.list_notes(session, panel.id)
        ]
    return d


@router.get("/projects/{project_id}/panels")
def list_panels(project_id: int, user=Depends(get_optional_user)):
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        try:
            ps.get_project(s, project_id)
        except ps.PanelError as exc:
            raise _fail(exc)
        return [_panel_dict(s, p) for p in ps.list_panels(s, project_id)]


@router.get("/panels/{panel_id}")
def get_panel(panel_id: int, user=Depends(get_optional_user)):
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        try:
            return _panel_dict(s, ps.get_panel(s, panel_id), with_images=True)
        except ps.PanelError as exc:
            raise _fail(exc)


@router.get("/assignable-users")
def assignable_users(user=Depends(get_optional_user)):
    """Who a panel can be handed to.

    Every active account for now. When giantflow grows its own membership
    (``flow_project_member``), this narrows to the people actually on the
    project — assigning work to someone who cannot open it is a dead end, and
    the picker is where that should be prevented.
    """
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
    return [
        {"user_id": str(u.id), "name": (u.display_name or u.username)}
        for u in user_service.list_users()
        if getattr(u, "status", "active") == "active"
    ]


# ── Generation ──────────────────────────────────────────────────────────────


class GenerateBody(BaseModel):
    """Engine settings, passed through untouched.

    The panel imposes nothing — no aspect, model or size is stored on it and none
    is forced. The artist has the same freedom the studio composer gives them; the
    panel only supplies context (its raw material as the reference) and takes
    custody of the results.
    """

    prompt: str = Field(min_length=1)
    provider: Optional[str] = None
    image_model: Optional[str] = None
    aspect_ratio: Optional[str] = None
    image_size: Optional[str] = None
    variant_count: int = 1
    preserve_colors: bool = False
    #: Extra references beyond the panel's own raw material.
    ref_media_ids: list[str] = []
    #: Edit an existing version rather than generating fresh.
    source_media_id: Optional[str] = None


@router.post("/panels/{panel_id}/generate")
def generate(panel_id: int, body: GenerateBody, user=Depends(get_optional_user)):
    """Queue a generation for this panel and return the request to poll.

    The request is created HERE rather than by the client calling /api/requests
    directly, for one reason: the approved-lock has to be checked **before any
    money is spent**. Attaching results afterwards would find out too late.

    The panel's raw material is prepended to the references automatically —
    matching the original is what every one of these generations is for, so it is
    the default rather than a step the artist repeats by hand.
    """
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        try:
            panel = ps.get_panel(s, panel_id)
        except ps.PanelError as exc:
            raise _fail(exc)
        if panel.status == "approved":
            raise HTTPException(
                409,
                "this panel is approved — ask the PM to reopen it before generating again",
            )
        raw = [i.media_id for i in ps.panel_images(s, panel_id, role="raw")]

    refs = raw + [m for m in body.ref_media_ids if m and m not in raw]
    params: dict = {
        "prompt": body.prompt.strip(),
        "provider": body.provider or "atrium",
        "variant_count": max(1, min(int(body.variant_count or 1), 4)),
        "__panel_id": panel_id,
    }
    if body.image_model:
        params["image_model"] = body.image_model
    if body.aspect_ratio:
        params["aspect_ratio"] = body.aspect_ratio
    if body.image_size and body.image_size != "1K":
        params["image_size"] = body.image_size
    if refs:
        params["ref_media_ids"] = refs
    if body.source_media_id:
        params["source_media_id"] = body.source_media_id
    # Colour preservation only means something with something to match against.
    if body.preserve_colors and (refs or body.source_media_id):
        params["preserve_colors"] = True

    from flowboard.db.models import Request as RequestRow
    from flowboard.worker.processor import get_worker

    with get_session() as s:
        req = RequestRow(type="flow_gen_image", params=params, status="queued")
        s.add(req)
        s.commit()
        s.refresh(req)
        rid = req.id
    get_worker().enqueue(rid)
    return {"request_id": rid, "panel_id": panel_id, "references": len(refs)}


class VersionsBody(BaseModel):
    media_ids: list[str]
    model_used: Optional[str] = None


@router.post("/panels/{panel_id}/versions")
def add_versions(panel_id: int, body: VersionsBody, user=Depends(get_optional_user)):
    """File finished images against the panel as its next version(s)."""
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        try:
            ps.add_generated(
                s,
                panel_id,
                body.media_ids,
                model_used=body.model_used,
                created_by=(user.id if user else None),
            )
            return _panel_dict(s, ps.get_panel(s, panel_id), with_images=True)
        except ps.PanelError as exc:
            raise _fail(exc)


# ── Review + notes ──────────────────────────────────────────────────────────


class ReviewBody(BaseModel):
    approve: bool
    notes: list[str] = []


@router.post("/panels/{panel_id}/submit")
def submit(panel_id: int, user=Depends(get_optional_user)):
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        try:
            return _panel_dict(s, ps.submit_panel(s, panel_id), with_images=True)
        except ps.PanelError as exc:
            raise _fail(exc)


@router.post("/panels/{panel_id}/review")
def review(panel_id: int, body: ReviewBody, user=Depends(get_optional_user)):
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        try:
            panel = ps.review_panel(
                s, panel_id, approve=body.approve, notes=body.notes,
                author_user_id=(user.id if user else None),
            )
            return _panel_dict(s, panel, with_images=True)
        except ps.PanelError as exc:
            raise _fail(exc)


@router.post("/panels/{panel_id}/reopen")
def reopen(panel_id: int, user=Depends(get_optional_user)):
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        try:
            return _panel_dict(s, ps.reopen_panel(s, panel_id), with_images=True)
        except ps.PanelError as exc:
            raise _fail(exc)


class NoteBody(BaseModel):
    body: str = Field(min_length=1)


@router.post("/panels/{panel_id}/notes")
def add_note(panel_id: int, body: NoteBody, user=Depends(get_optional_user)):
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        try:
            ps.add_note(s, panel_id, body.body, author_user_id=(user.id if user else None))
            return _panel_dict(s, ps.get_panel(s, panel_id), with_images=True)
        except ps.PanelError as exc:
            raise _fail(exc)


class NoteResolveBody(BaseModel):
    resolved: bool


@router.patch("/notes/{note_id}")
def resolve_note(note_id: int, body: NoteResolveBody, user=Depends(get_optional_user)):
    """Tick or untick a remark — the Miro board's "Fixed"."""
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        try:
            note = ps.set_note_resolved(
                s, note_id, body.resolved, user_id=(user.id if user else None)
            )
            return _panel_dict(s, ps.get_panel(s, note.panel_id), with_images=True)
        except ps.PanelError as exc:
            raise _fail(exc)


class AssignBody(BaseModel):
    panel_ids: list[int]
    #: None unassigns — "take these back off Quân" needs to be sayable.
    user_id: Optional[uuid.UUID] = None


@router.post("/projects/{project_id}/assign")
def assign(project_id: int, body: AssignBody, user=Depends(get_optional_user)):
    """Assign a batch of panels to one person.

    Batched because the real decision is "artist 1 takes panels 1-30" — one row at
    a time would be thirty round trips for one decision.
    """
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        try:
            ps.get_project(s, project_id)
        except ps.PanelError as exc:
            raise _fail(exc)
        n = ps.assign_panels(s, project_id, body.panel_ids, body.user_id)
        return {"assigned": n, "user_id": str(body.user_id) if body.user_id else None}
