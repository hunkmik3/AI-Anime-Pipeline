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
import re
import uuid
from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from pydantic import BaseModel, Field
from sqlmodel import select

from flowboard.db import get_session
from flowboard.routes.deps import get_optional_user
from flowboard.services import media as media_service
from flowboard.db.models import PANEL_STATUSES
from flowboard.services import flow_permissions as fp
from flowboard.services import panel_service as ps
from flowboard.services import resource_guard
from flowboard.services import user_service

logger = logging.getLogger(__name__)

async def _apply_view_as(request: Request) -> None:
    """Honour ``X-Giantflow-View-As`` for the whole request.

    A router-wide dependency rather than a parameter on 34 handlers: the header
    has to reach `flow_permissions.role_for`, which is called from deep inside
    the service layer and has no access to the request.

    **async on purpose.** FastAPI runs a *sync* dependency in a worker thread,
    which gets a COPY of the context — a `ContextVar.set` there is discarded the
    moment the thread returns, so the header silently did nothing. Declared
    async, this runs in the request's own coroutine, where the set sticks and is
    then copied into the handler's threadpool call.

    Safe to trust because it can only ever LOWER the caller's role — see
    `cap_to_preview`. A forged header takes rights away from whoever sends it.
    """
    fp.set_preview(request.headers.get("x-giantflow-view-as"))


router = APIRouter(
    prefix="/api/flowstudio",
    tags=["flow-panels"],
    dependencies=[Depends(_apply_view_as)],
)

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


def _series_dict(session, row) -> dict:
    # Batches hang off CHAPTERS now. `list_batches(session, row.id)` still ran
    # and still returned rows — it matched batches whose chapter id happened to
    # equal this series id — which is the shape of wrong answer that never
    # raises. Count them through the chapters instead.
    chapters = ps.list_chapters(session, row.id)
    batches = [b for c in chapters for b in ps.list_batches(session, c.id)]
    panels = ps.list_series_panels(session, row.id)
    return {
        "id": row.id,
        #: The slate above it. Without this the breadcrumb built
        #: `/giantflow/p/undefined` and the series page asked for project NaN.
        "project_id": row.project_id,
        "name": row.name,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        # Hand-picked cover, else the comic's opening panel.
        "thumb_media_id": ps.series_cover_media_id(session, row),
        "has_cover": bool(row.cover_media_id),
        # The numbers a PM opens this list for.
        "chapter_count": len(chapters),
        "batch_count": len(batches),
        "panel_count": len(panels),
        "approved_count": len([p for p in panels if p.status == "approved"]),
    }


def _batch_dict(session, row) -> dict:
    panels = ps.list_panels(session, row.id)
    # The whole spread, not just "approved": a batch that is 40 untouched and 5
    # approved and one that is 40 in review and 5 approved read identically
    # otherwise, and they are nothing alike to a PM.
    counts = {s: 0 for s in PANEL_STATUSES}
    for p in panels:
        counts[p.status] = counts.get(p.status, 0) + 1
    first_raw = None
    for p in panels:
        raws = ps.panel_images(session, p.id, role="raw")
        if raws:
            first_raw = raws[0].media_id
            break
    return {
        "id": row.id,
        "chapter_id": row.chapter_id,
        "name": row.name,
        "assignee_user_id": str(row.assignee_user_id) if row.assignee_user_id else None,
        "assignee_name": _user_name(row.assignee_user_id),
        "panel_count": len(panels),
        "approved_count": counts.get("approved", 0),
        "status_counts": counts,
        "open_notes": sum(ps.unresolved_count(session, p.id) for p in panels),
        # Its first panel, so a row is recognisable without opening it.
        "thumb_media_id": first_raw,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


class ReorderBody(BaseModel):
    #: Ids in their new order. Omitted ids keep their relative order, after these.
    ids: list[int]

# ── Projects — the slate ────────────────────────────────────────────────────
#
# The top of four tiers: Project → Series → Batch → Panel. A project holds a name
# and a cover; every capability check still happens against the SERIES, because
# authority is per comic — a PM on X-MEN is not a PM on MAGMEL.


def _project_dict(session, row) -> dict:
    series = ps.list_series(session, row.id)
    panels = [p for s in series for p in ps.list_series_panels(session, s.id)]
    return {
        "id": row.id,
        "name": row.name,
        "thumb_media_id": ps.project_cover_media_id(session, row.id),
        "has_cover": bool(row.cover_media_id),
        "series_count": len(series),
        "panel_count": len(panels),
        "approved_count": len([p for p in panels if p.status == "approved"]),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("/projects")
def list_projects(user=Depends(get_optional_user)):
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        return [_project_dict(s, r) for r in ps.list_projects(s)]


class ProjectBody(BaseModel):
    name: str = Field(min_length=1)


class SeriesBody(BaseModel):
    project_id: int
    name: str = Field(min_length=1)


@router.post("/projects")
def create_project(body: ProjectBody, user=Depends(get_optional_user)):
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        _guard(s, user, None, "project.manage")
        try:
            return _project_dict(s, ps.create_project(s, body.name))
        except ps.PanelError as exc:
            raise _fail(exc)


class ProjectPatch(BaseModel):
    name: Optional[str] = None
    cover_media_id: Optional[str] = None
    set_cover: bool = False


@router.patch("/projects/{project_id}")
def update_project(project_id: int, body: ProjectPatch, user=Depends(get_optional_user)):
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        _guard(s, user, None, "project.manage")
        try:
            row = ps.update_project(
                s, project_id, name=body.name,
                cover_media_id=body.cover_media_id, set_cover=body.set_cover,
            )
            return _project_dict(s, row)
        except ps.PanelError as exc:
            raise _fail(exc)


@router.delete("/projects/{project_id}")
def delete_project(project_id: int, user=Depends(get_optional_user)):
    """Takes its series with it, and through them every batch and panel."""
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        _guard(s, user, None, "project.manage")
        try:
            ps.delete_project(s, project_id)
        except ps.PanelError as exc:
            raise _fail(exc)
        return {"ok": True}


@router.post("/projects/reorder")
def reorder_projects(body: ReorderBody, user=Depends(get_optional_user)):
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        _guard(s, user, None, "project.manage")
        ps.reorder_projects(s, body.ids)
        return {"ok": True}


@router.get("/series")
def list_series(project_id: Optional[int] = None, user=Depends(get_optional_user)):
    """The comics on one slate, or every comic when no project is named."""
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        return [_series_dict(s, r) for r in ps.list_series(s, project_id)]


@router.post("/series")
def create_series(body: SeriesBody, user=Depends(get_optional_user)):
    """A comic belongs to a slate, so the project it goes on is required."""
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        _guard(s, user, None, "project.manage")
        try:
            row = ps.create_series(
                s, body.project_id, body.name, created_by=(user.id if user else None)
            )
        except ps.PanelError as exc:
            raise _fail(exc)
        return _series_dict(s, row)


@router.patch("/series/{series_id}")
def rename_project(series_id: int, body: ProjectBody, user=Depends(get_optional_user)):
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        _guard(s, user, series_id, "project.manage")
        try:
            return _series_dict(s, ps.rename_series(s, series_id, body.name))
        except ps.PanelError as exc:
            raise _fail(exc)


@router.delete("/series/{series_id}")
def delete_series(series_id: int, user=Depends(get_optional_user)):
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        _guard(s, user, series_id, "project.manage")
        try:
            ps.delete_series(s, series_id)
        except ps.PanelError as exc:
            raise _fail(exc)
        return {"deleted": series_id}




@router.post("/series/reorder")
def reorder_series(body: ReorderBody, user=Depends(get_optional_user)):
    """Persist a hand-arranged project grid."""
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        _guard(s, user, None, "project.manage")
        n = ps.reorder_series(s, body.ids)
        return {"reordered": n}


@router.post("/chapters/{chapter_id}/batches/reorder")
def reorder_batches(chapter_id: int, body: ReorderBody, user=Depends(get_optional_user)):
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        _guard(s, user, _series_of_chapter(s, chapter_id), "batch.manage")
        try:
            n = ps.reorder_batches(s, chapter_id, body.ids)
        except ps.PanelError as exc:
            raise _fail(exc)
        return {"reordered": n}


class CoverBody(BaseModel):
    #: None clears it, falling back to the first panel.
    media_id: Optional[str] = None


@router.post("/series/{series_id}/cover")
def set_series_cover(series_id: int, body: CoverBody, user=Depends(get_optional_user)):
    """Point the project card at an image. Cosmetic, so any account may do it."""
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        _guard(s, user, series_id, "batch.manage")
        try:
            return _series_dict(s, ps.set_series_cover(s, series_id, body.media_id))
        except ps.PanelError as exc:
            raise _fail(exc)


# ── Import ──────────────────────────────────────────────────────────────────

_ALLOWED_MIME = {"image/png", "image/jpeg", "image/webp", "image/bmp"}
#: Per file. Raw panels are scans, not 4K renders — generous but not unbounded.
_MAX_FILE_BYTES = 30 * 1024 * 1024
#: A chapter is a few hundred panels; well past that means someone picked the
#: wrong folder, and finding out after a 10-minute upload is the worst version.
_MAX_FILES = 2000


@router.post("/batches/{batch_id}/import")
async def import_folder(
    batch_id: int,
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
        _guard(s, user, _series_of_batch(s, batch_id), "batch.import")

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
            panels = ps.import_panels(s, batch_id, entries=entries)
        except ps.PanelError as exc:
            raise _fail(exc)
        logger.info(
            "flowpanels: imported %d file(s) into %d panel(s) on batch %s (%d skipped)",
            len(entries), len(panels), batch_id, len(skipped),
        )
        return {
            "batch_id": batch_id,
            "panels": [_panel_dict(s, p) for p in panels],
            "imported_files": len(entries),
            "skipped": skipped[:20],
            "skipped_count": len(skipped),
        }


# ── Panels ──────────────────────────────────────────────────────────────────


def _series_of_chapter(session, chapter_id: int) -> int:
    return ps.get_chapter(session, chapter_id).series_id


def _series_of_batch(session, batch_id: int) -> int:
    """A batch reaches its comic through its chapter now."""
    return _series_of_chapter(session, ps.get_batch(session, batch_id).chapter_id)


def _series_of_panel(session, panel_id: int) -> int:
    return _series_of_batch(session, ps.get_panel(session, panel_id).batch_id)


def _guard(session, user, series_id, capability: str) -> str:
    """Server-side capability check. The UI's role switch is a drawing hint; this
    is the rule. A button the frontend declines to render is still a reachable
    endpoint, so every one of them is checked here too."""
    return fp.require(session, user, series_id, capability)


def _panel_dict(session, panel, *, with_images: bool = False) -> dict:
    raws = ps.panel_images(session, panel.id, role="raw")
    latest = ps.latest_generated(session, panel.id)
    shown = ps.delivered(session, panel.id)
    batch = ps.get_batch(session, panel.batch_id)
    chapter = ps.get_chapter(session, batch.chapter_id)
    d = {
        "id": panel.id,
        "batch_id": panel.batch_id,
        "batch_name": batch.name,
        "chapter_id": chapter.id,
        "chapter_name": chapter.name,
        "series_id": chapter.series_id,
        "code": panel.code,
        "order_index": panel.order_index,
        "status": panel.status,
        # Who works on this comes from the BATCH — the panel has no assignee of
        # its own, so there is one place this fact lives.
        "assignee_user_id": str(batch.assignee_user_id) if batch.assignee_user_id else None,
        "assignee_name": _user_name(batch.assignee_user_id),
        # The grid shows original and result side by side — that pairing is the
        # whole point of the board this replaces.
        "raw_media_id": raws[0].media_id if raws else None,
        "raw_count": len(raws),
        "latest_media_id": latest.media_id if latest else None,
        # What this panel is DELIVERING — the submitted pick, else the latest.
        # Grids and covers must read this, never `latest_media_id`, or a PM
        # reviews one image and is shown another.
        "delivered_media_id": shown.media_id if shown else None,
        "delivered_version": shown.version if shown else 0,
        #: Null until someone submits. Distinct from `delivered_media_id`, which
        #: always has a value once anything has been generated.
        "final_media_id": panel.final_media_id,
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


@router.get("/batches/{batch_id}/panels")
def list_panels(batch_id: int, user=Depends(get_optional_user)):
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        _guard(s, user, _series_of_batch(s, batch_id), "panel.read")
        try:
            ps.get_batch(s, batch_id)
        except ps.PanelError as exc:
            raise _fail(exc)
        return [_panel_dict(s, p) for p in ps.list_panels(s, batch_id)]


# ── Batches ─────────────────────────────────────────────────────────────────


class BatchCreate(BaseModel):
    #: Optional: omitted, the server names it by the studio's convention
    #: (``Project_Series_Chapter_batchNN``). Typed names drifted — "Quân",
    #: "quan" and "26004_UL-X-MEN_Quân" all appeared in one comic, and the
    #: export folders inherit whatever was typed.
    name: Optional[str] = Field(default=None, max_length=200)
    assignee_user_id: Optional[uuid.UUID] = None


class BatchUpdate(BaseModel):
    name: Optional[str] = None
    assignee_user_id: Optional[uuid.UUID] = None
    #: None is a real value — "take this off whoever had it" must be sayable
    #: separately from "leave the assignee alone".
    set_assignee: bool = False


@router.get("/chapters/{chapter_id}/batches")
def list_batches(chapter_id: int, user=Depends(get_optional_user)):
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        _guard(s, user, _series_of_chapter(s, chapter_id), "panel.read")
        try:
            # The CHAPTER, not the series. Both are plain ints, so passing one
            # where the other belongs type-checks and runs — it just 404s on an
            # id that is perfectly valid for the other table.
            ps.get_chapter(s, chapter_id)
        except ps.PanelError as exc:
            raise _fail(exc)
        return [_batch_dict(s, b) for b in ps.list_batches(s, chapter_id)]


@router.post("/chapters/{chapter_id}/batches")
def create_batch(chapter_id: int, body: BatchCreate, user=Depends(get_optional_user)):
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        _guard(s, user, _series_of_chapter(s, chapter_id), "batch.manage")
        try:
            row = ps.create_batch(
                s, chapter_id, body.name, assignee_user_id=body.assignee_user_id
            )
            return _batch_dict(s, row)
        except ps.PanelError as exc:
            raise _fail(exc)


class BatchesCreate(BaseModel):
    #: One entry per batch. Blank names are skipped, so a form with spare rows
    #: does not have to police itself.
    batches: list[BatchCreate]


@router.post("/chapters/{chapter_id}/batches/bulk")
def create_batches(chapter_id: int, body: BatchesCreate, user=Depends(get_optional_user)):
    """Create several batches in one commit — dividing a chapter is one decision."""
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        _guard(s, user, _series_of_chapter(s, chapter_id), "batch.manage")
        try:
            rows = ps.create_batches(
                s, chapter_id, [(b.name, b.assignee_user_id) for b in body.batches]
            )
        except ps.PanelError as exc:
            raise _fail(exc)
        return [_batch_dict(s, r) for r in rows]


@router.get("/batches/{batch_id}")
def get_batch(batch_id: int, user=Depends(get_optional_user)):
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        _guard(s, user, _series_of_batch(s, batch_id), "panel.read")
        try:
            return _batch_dict(s, ps.get_batch(s, batch_id))
        except ps.PanelError as exc:
            raise _fail(exc)


@router.patch("/batches/{batch_id}")
def update_batch(batch_id: int, body: BatchUpdate, user=Depends(get_optional_user)):
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        _guard(s, user, _series_of_batch(s, batch_id), "batch.manage")
        try:
            row = ps.update_batch(
                s, batch_id, name=body.name,
                assignee_user_id=body.assignee_user_id,
                set_assignee=body.set_assignee,
            )
            return _batch_dict(s, row)
        except ps.PanelError as exc:
            raise _fail(exc)


@router.delete("/batches/{batch_id}")
def delete_batch(batch_id: int, user=Depends(get_optional_user)):
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        _guard(s, user, _series_of_batch(s, batch_id), "batch.manage")
        try:
            ps.delete_batch(s, batch_id)
        except ps.PanelError as exc:
            raise _fail(exc)
        return {"deleted": batch_id}


@router.get("/panels/{panel_id}")
def get_panel(panel_id: int, user=Depends(get_optional_user)):
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        _guard(s, user, _series_of_panel(s, panel_id), "panel.read")
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


# ── Export ──────────────────────────────────────────────────────────────────
#
# Two shapes, because the studio needs both: one panel now, or the whole
# signed-off batch when it ships. Both read ``final_media_id`` through
# ``ps.delivered`` — the version the artist chose and the PM approved, never the
# most recent attempt, which is what made "download the approved work"
# undefinable until that column existed.
#
# Files are named with the cutter's own code, so what comes out matches the
# sheet the studio already works from.


async def _image_bytes(media_id: str) -> Optional[tuple[bytes, str]]:
    """Cached file if there is one, otherwise fetch it once and cache it."""
    path = media_service.cached_path(media_id)
    if path is not None and path.exists():
        return path.read_bytes(), path.suffix.lstrip(".").lower() or "png"
    got = await media_service.fetch_and_cache(media_id)
    if got is None:
        return None
    data, _mime, cached = got
    return data, (cached.suffix.lstrip(".").lower() or "png")


@router.get("/panels/{panel_id}/download")
async def download_panel(panel_id: int, user=Depends(get_optional_user)):
    """The one image this panel delivers, named after the panel."""
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        _guard(s, user, _series_of_panel(s, panel_id), "panel.read")
        try:
            panel = ps.get_panel(s, panel_id)
        except ps.PanelError as exc:
            raise _fail(exc)
        img = ps.delivered(s, panel_id)
        code = panel.code
    if img is None:
        raise HTTPException(404, "this panel has no generated version yet")
    got = await _image_bytes(img.media_id)
    if got is None:
        raise HTTPException(502, "the image could not be read back from storage")
    data, ext = got
    return Response(
        content=data,
        media_type=media_service._mime_from_ext(ext),
        headers={"Content-Disposition": f'attachment; filename="{code}.{ext}"'},
    )


async def _zip_panels(session, panels: list, *, folders: bool) -> tuple[bytes, int, int]:
    """Zip each panel's delivered image. Returns (bytes, written, skipped).

    Skips rather than fails on an unreadable image: one broken file must not cost
    a producer the other two hundred.
    """
    import io
    import zipfile

    picked = []
    for panel in panels:
        img = ps.delivered(session, panel.id)
        if img is None:
            continue
        batch = ps.get_batch(session, panel.batch_id)
        picked.append((panel.code, batch.name, img.media_id))

    buf = io.BytesIO()
    written = skipped = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for code, batch_name, media_id in picked:
            got = await _image_bytes(media_id)
            if got is None:
                skipped += 1
                continue
            data, ext = got
            name = f"{batch_name}/{code}.{ext}" if folders else f"{code}.{ext}"
            zf.writestr(name, data)
            written += 1
    return buf.getvalue(), written, skipped


@router.get("/batches/{batch_id}/export")
async def export_batch(batch_id: int, user=Depends(get_optional_user)):
    """Every approved panel in one artist's batch, as a zip."""
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        _guard(s, user, _series_of_batch(s, batch_id), "panel.read")
        try:
            batch = ps.get_batch(s, batch_id)
        except ps.PanelError as exc:
            raise _fail(exc)
        panels = [p for p in ps.list_panels(s, batch_id) if p.status == "approved"]
        if not panels:
            raise HTTPException(404, "no approved panels in this batch yet")
        data, written, skipped = await _zip_panels(s, panels, folders=False)
        name = _safe_filename(batch.name)
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{name}_approved.zip"',
            # Surfaced as a header so the client can say "3 files could not be
            # read" instead of silently handing over a short zip.
            "X-Export-Written": str(written),
            "X-Export-Skipped": str(skipped),
        },
    )


@router.get("/series/{series_id}/export")
async def export_project(series_id: int, user=Depends(get_optional_user)):
    """Every approved panel in the comic, foldered by batch."""
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        _guard(s, user, series_id, "panel.read")
        try:
            project = ps.get_series(s, series_id)
        except ps.PanelError as exc:
            raise _fail(exc)
        panels = [
            p for p in ps.list_series_panels(s, series_id) if p.status == "approved"
        ]
        if not panels:
            raise HTTPException(404, "no approved panels in this project yet")
        data, written, skipped = await _zip_panels(s, panels, folders=True)
        name = _safe_filename(project.name)
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{name}_approved.zip"',
            "X-Export-Written": str(written),
            "X-Export-Skipped": str(skipped),
        },
    )


def _safe_filename(name: str) -> str:
    """A comic's name as typed is not a filename — slashes and quotes in a
    Content-Disposition header are how you get a mangled download."""
    cleaned = re.sub(r'[^A-Za-z0-9._-]+', "_", (name or "export").strip())
    return cleaned.strip("._-") or "export"


# ── Who am I here ───────────────────────────────────────────────────────────


@router.get("/me")
def whoami(user=Depends(get_optional_user)):
    """What this account may do, so the UI draws the right thing.

    Advisory only — every capability is enforced per request as well. This exists
    so the interface stops offering buttons that would 403, not to decide
    anything.
    """
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        best = fp.best_role(s, user)
        return {
            "user_id": str(user.id) if user else None,
            "system_role": getattr(user, "role", None) if user else "admin",
            "best_role": best,
            "capabilities": fp.capability_map(best),
            # Per comic, because authority is per comic — the global answer above
            # is only for deciding whether to show the Review tab at all.
            "projects": {
                str(proj.id): fp.role_for(s, user, proj.id)
                for proj in ps.list_series(s)
            },
        }


class MemberBody(BaseModel):
    user_id: uuid.UUID
    role: str = fp.ARTIST


@router.get("/series/{series_id}/members")
def list_members(series_id: int, user=Depends(get_optional_user)):
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        _guard(s, user, series_id, "panel.read")
        return [
            {
                "user_id": str(m.user_id),
                "name": _user_name(m.user_id),
                "role": m.role,
            }
            for m in ps.list_members(s, series_id)
        ]


@router.put("/series/{series_id}/members")
def put_member(series_id: int, body: MemberBody, user=Depends(get_optional_user)):
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        _guard(s, user, series_id, "member.manage")
        if body.role not in fp.FLOW_ROLES:
            raise HTTPException(400, f"role must be one of {list(fp.FLOW_ROLES)}")
        m = ps.set_member(s, series_id, body.user_id, body.role)
        return {"user_id": str(m.user_id), "name": _user_name(m.user_id), "role": m.role}


@router.delete("/series/{series_id}/members/{user_id}")
def delete_member(series_id: int, user_id: uuid.UUID, user=Depends(get_optional_user)):
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        _guard(s, user, series_id, "member.manage")
        ps.remove_member(s, series_id, user_id)
        return {"ok": True}


# ── Queues: the PM's review pile, and the artist's results ───────────────────
#
# Two pages, deliberately not one. A reviewer's question is "what is waiting on
# me, across everyone" and an artist's is "what came back to me, and why" —
# opposite ends of the same handover, and neither is answered by browsing the
# project tree. The tree is for organising work; these are for doing it.


def _queue_dict(session, panel) -> dict:
    """A panel as it appears in a queue: the pairing, who, and the verdict."""
    d = _panel_dict(session, panel)
    # Unresolved notes only. A queue card is answering "what is wrong with this
    # now", and remarks already ticked off would be re-litigating settled work.
    d["notes"] = [
        {
            "id": n.id,
            "body": n.body,
            "author_name": _user_name(n.author_user_id),
            "created_at": n.created_at.isoformat() if n.created_at else None,
        }
        for n in ps.list_notes(session, panel.id)
        if not n.resolved
    ]
    d["series_name"] = ps.series_of_batch(session, panel.batch_id).name
    return d


@router.get("/panels")
def all_panels(
    status: Optional[str] = None,
    series_id: Optional[int] = None,
    assignee: Optional[uuid.UUID] = None,
    q: Optional[str] = None,
    user=Depends(get_optional_user),
):
    """Every panel, with its state — the management view.

    ``status`` takes a comma-separated list so one request answers "sent back or
    in review" without the client making two.
    """
    statuses = [s.strip() for s in (status or "").split(",") if s.strip()]
    bad = [s for s in statuses if s not in PANEL_STATUSES]
    if bad:
        raise HTTPException(400, f"unknown status {bad[0]!r}")
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        rows = ps.search_panels(
            s,
            statuses=statuses or None,
            series_id=series_id,
            assignee_user_id=assignee,
            q=q,
        )
        # Filtered, not refused: someone who can read one comic and not another
        # gets the first rather than a 403 for the whole page.
        return [
            _queue_dict(s, row)
            for row in rows
            if fp.allows(
                fp.role_for(s, user, _series_of_batch(s, row.batch_id)), "panel.read"
            )
        ]


@router.get("/review-queue")
def review_queue(series_id: Optional[int] = None, user=Depends(get_optional_user)):
    """Everything handed in and waiting on a verdict, across every artist."""
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        rows = ps.panels_by_status(s, ["submitted"], series_id=series_id)
        # Filtered, not refused: a lead who reviews one comic and merely watches
        # another should see the first without the page erroring on the second.
        out = []
        for row in rows:
            pid = _series_of_batch(s, row.batch_id)
            if fp.allows(fp.role_for(s, user, pid), "panel.review"):
                out.append(_queue_dict(s, row))
        return out


@router.get("/my-work")
def my_work(user=Depends(get_optional_user)):
    """The signed-in artist's own panels and what the PM said about them.

    Scoped by the BATCH's assignee, since that is where "who is doing this"
    lives. Returns the three states an artist acts on; ``todo`` is excluded —
    untouched panels belong in the batch grid, not in a results page.
    """
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        if user is None:
            return {"changes_requested": [], "submitted": [], "approved": []}
        out: dict[str, list] = {}
        for status in ("changes_requested", "submitted", "approved"):
            rows = ps.panels_by_status(s, [status], assignee_user_id=user.id)
            out[status] = [_queue_dict(s, p) for p in rows]
        return out


# ── Chapters ────────────────────────────────────────────────────────────────


def _chapter_dict(session, row) -> dict:
    panels = ps.list_chapter_panels(session, row.id)
    counts = {s: 0 for s in PANEL_STATUSES}
    for p in panels:
        counts[p.status] = counts.get(p.status, 0) + 1
    return {
        "id": row.id,
        "series_id": row.series_id,
        "name": row.name,
        "thumb_media_id": ps.chapter_cover_media_id(session, row),
        "has_cover": bool(row.cover_media_id),
        "batch_count": len(ps.list_batches(session, row.id)),
        #: What a new batch here will be called, minus the number. Sent so the
        #: form shows the exact name rather than re-deriving the convention.
        "batch_name_prefix": ps.batch_name_prefix(session, row.id),
        "panel_count": len(panels),
        "approved_count": counts.get("approved", 0),
        "status_counts": counts,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("/series/{series_id}/chapters")
def list_chapters(series_id: int, user=Depends(get_optional_user)):
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        _guard(s, user, series_id, "panel.read")
        return [_chapter_dict(s, c) for c in ps.list_chapters(s, series_id)]


class ChapterBody(BaseModel):
    name: str = Field(min_length=1)


@router.post("/series/{series_id}/chapters")
def create_chapter(series_id: int, body: ChapterBody, user=Depends(get_optional_user)):
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        _guard(s, user, series_id, "batch.manage")
        try:
            return _chapter_dict(s, ps.create_chapter(s, series_id, body.name))
        except ps.PanelError as exc:
            raise _fail(exc)


@router.get("/chapters/{chapter_id}")
def get_chapter(chapter_id: int, user=Depends(get_optional_user)):
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        _guard(s, user, _series_of_chapter(s, chapter_id), "panel.read")
        try:
            return _chapter_dict(s, ps.get_chapter(s, chapter_id))
        except ps.PanelError as exc:
            raise _fail(exc)


class ChapterPatch(BaseModel):
    name: Optional[str] = None
    cover_media_id: Optional[str] = None
    set_cover: bool = False


@router.patch("/chapters/{chapter_id}")
def update_chapter(chapter_id: int, body: ChapterPatch, user=Depends(get_optional_user)):
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        _guard(s, user, _series_of_chapter(s, chapter_id), "batch.manage")
        try:
            row = ps.update_chapter(
                s, chapter_id, name=body.name,
                cover_media_id=body.cover_media_id, set_cover=body.set_cover,
            )
            return _chapter_dict(s, row)
        except ps.PanelError as exc:
            raise _fail(exc)


@router.delete("/chapters/{chapter_id}")
def delete_chapter(chapter_id: int, user=Depends(get_optional_user)):
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        _guard(s, user, _series_of_chapter(s, chapter_id), "batch.manage")
        try:
            ps.delete_chapter(s, chapter_id)
        except ps.PanelError as exc:
            raise _fail(exc)
        return {"ok": True}


@router.post("/series/{series_id}/chapters/reorder")
def reorder_chapters(series_id: int, body: ReorderBody, user=Depends(get_optional_user)):
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        _guard(s, user, series_id, "batch.manage")
        ps.reorder_chapters(s, body.ids)
        return {"ok": True}


@router.get("/chapters/{chapter_id}/export")
async def export_chapter(chapter_id: int, user=Depends(get_optional_user)):
    """Every approved panel in one chapter, foldered by batch."""
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        _guard(s, user, _series_of_chapter(s, chapter_id), "panel.read")
        try:
            chapter = ps.get_chapter(s, chapter_id)
        except ps.PanelError as exc:
            raise _fail(exc)
        panels = [p for p in ps.list_chapter_panels(s, chapter_id) if p.status == "approved"]
        if not panels:
            raise HTTPException(404, "no approved panels in this chapter yet")
        data, written, skipped = await _zip_panels(s, panels, folders=True)
        name = _safe_filename(chapter.name)
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{name}_approved.zip"',
            "X-Export-Written": str(written),
            "X-Export-Skipped": str(skipped),
        },
    )


# ── The panel's own asset library ────────────────────────────────────────────
#
# The workspace mounts the studio's real components — the composer, the viewer —
# and both of them speak `Reference` rows: tagging an image as a character,
# pinning it, refining it and attaching it to a prompt are all operations on a
# reference. Panel pictures live in `flow_panel_image`, which those components
# have never heard of.
#
# So the two are mirrored. Every raw panel and every generated version gets a
# reference row, created on demand and idempotent on media_id, tagged
# `panel:<id>` so the panel can ask for exactly its own. Nothing is duplicated:
# `flow_panel_image` stays the record of what belongs to the panel and in what
# order, the reference row is the handle the studio UI needs to act on it.


PANEL_TAG_PREFIX = "panel:"


def _ensure_ref(session, media_id: str, *, label: str, model_used, tag: str):
    """Get or create the Reference for one image, tagged for this panel.

    Written to survive a race. Two requests for the same panel's assets can be in
    flight at once — the workspace loads them on mount and again when a
    generation lands — and both would see the row missing and both insert it.
    The unique constraint on ``media_id`` then 500s the second one, which is what
    the "500 Internal Server Error" on the panel page was. The constraint is
    right; checking-then-inserting without handling the loser was not.
    """
    from sqlalchemy.exc import IntegrityError

    from flowboard.db.models import Reference

    def _tagged(row):
        if row is not None and tag not in (row.tags or []):
            # Adopt an image already saved elsewhere rather than making a second
            # row for the same bytes.
            row.tags = [*(row.tags or []), tag]
            session.add(row)
            session.commit()
            session.refresh(row)
        return row

    found = session.exec(select(Reference).where(Reference.media_id == media_id)).first()
    if found is not None:
        return _tagged(found)

    row = Reference(
        media_id=media_id, kind="image", label=label, model_used=model_used,
        tags=["flow", tag], pinned=False,
    )
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        # Someone else won the insert between our SELECT and our INSERT. Take
        # theirs; the row is the same image either way.
        session.rollback()
        return _tagged(
            session.exec(select(Reference).where(Reference.media_id == media_id)).first()
        )
    session.refresh(row)
    return row


def _panel_refs(session, panel_id: int) -> list:
    """Reference rows for this panel's pictures, creating any that are missing."""
    from flowboard.db.models import Reference

    tag = f"{PANEL_TAG_PREFIX}{panel_id}"
    out = []
    seen: set[str] = set()
    for img in ps.panel_images(session, panel_id):
        # The same media twice in one panel would otherwise be inserted twice in
        # a single pass, before either is visible to the other.
        if img.media_id in seen:
            continue
        seen.add(img.media_id)
        row = _ensure_ref(
            session,
            img.media_id,
            label=(f"v{img.version}" if img.role == "generated" else "raw material"),
            model_used=img.model_used,
            tag=tag,
        )
        if row is not None:
            out.append(row)

    # Images the artist brought along have no `flow_panel_image` row — they are
    # input, not something the panel produced — so they are found by the tag
    # instead. Without this they vanished on refresh: present in the grid until
    # you reloaded, then gone.
    extras = [
        r
        for r in session.exec(select(Reference).order_by(Reference.id)).all()
        if tag in (r.tags or []) and r.media_id not in seen
    ]
    return out + extras


@router.get("/panels/{panel_id}/assets")
def panel_assets(panel_id: int, user=Depends(get_optional_user)):
    """This panel's pictures, in the shape the studio grid and viewer expect."""
    from flowboard.routes.references import _row_dict

    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        _guard(s, user, _series_of_panel(s, panel_id), "panel.read")
        try:
            ps.get_panel(s, panel_id)
        except ps.PanelError as exc:
            raise _fail(exc)
        return [_row_dict(r) for r in _panel_refs(s, panel_id)]


class PanelAssetBody(BaseModel):
    media_id: str
    label: Optional[str] = None


@router.post("/panels/{panel_id}/assets")
def add_panel_asset(panel_id: int, body: PanelAssetBody, user=Depends(get_optional_user)):
    """Attach an uploaded image to this panel's library as reference material.

    Separate from ``/versions``: a version is a RESULT and counts towards the
    panel's history; this is input the artist brought along — a character sheet,
    an environment plate — and must not be mistaken for work delivered.
    """
    from flowboard.db.models import Reference
    from flowboard.routes.references import _row_dict

    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        _guard(s, user, _series_of_panel(s, panel_id), "panel.generate")
        try:
            ps.get_panel(s, panel_id)
        except ps.PanelError as exc:
            raise _fail(exc)
        row = _ensure_ref(
            s,
            body.media_id,
            label=body.label or "reference",
            model_used=None,
            tag=f"{PANEL_TAG_PREFIX}{panel_id}",
        )
        if row is None:
            raise HTTPException(500, "could not record that image")
        return _row_dict(row)


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

    The panel's raw material is always prepended to the references. Matching the
    original is what every one of these generations is for, so it is a property
    of the panel rather than something the artist re-attaches each time — and
    there is no control claiming otherwise: the workspace shows the raw as a card
    in the grid, not as a removable chip in the composer.
    """
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        _guard(s, user, _series_of_panel(s, panel_id), "panel.generate")
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
        _guard(s, user, _series_of_panel(s, panel_id), "panel.generate")
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


class SubmitBody(BaseModel):
    """Which version is being handed over.

    Optional so a caller with one version need not name it, but the UI always
    sends it: the submit button lives ON a version card, because a submit button
    somewhere else is what creates the question "which one did they mean".
    """

    media_id: Optional[str] = None


@router.post("/panels/{panel_id}/submit")
def submit(panel_id: int, body: SubmitBody | None = None, user=Depends(get_optional_user)):
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        _guard(s, user, _series_of_panel(s, panel_id), "panel.submit")
        try:
            panel = ps.submit_panel(s, panel_id, media_id=(body.media_id if body else None))
            return _panel_dict(s, panel, with_images=True)
        except ps.PanelError as exc:
            raise _fail(exc)


@router.post("/panels/{panel_id}/review")
def review(panel_id: int, body: ReviewBody, user=Depends(get_optional_user)):
    with get_session() as s:
        resource_guard.require_signed_in(s, user)
        _guard(s, user, _series_of_panel(s, panel_id), "panel.review")
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
        _guard(s, user, _series_of_panel(s, panel_id), "panel.review")
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
        _guard(s, user, _series_of_panel(s, panel_id), "panel.generate")
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
        from flowboard.db.models import FlowPanelNote

        note = s.get(FlowPanelNote, note_id)
        if note is None:
            raise HTTPException(404, "note not found")
        _guard(s, user, _series_of_panel(s, note.panel_id), "panel.generate")
        try:
            note = ps.set_note_resolved(
                s, note_id, body.resolved, user_id=(user.id if user else None)
            )
            return _panel_dict(s, ps.get_panel(s, note.panel_id), with_images=True)
        except ps.PanelError as exc:
            raise _fail(exc)


