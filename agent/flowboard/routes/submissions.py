"""Phase 11 — deliverable submission + review REST surface.

    POST   /api/series/{series_id}/submissions  — employee hands in the finished cut
    GET    /api/series/{series_id}/submissions  — that series' full history
    POST   /api/submissions/{id}/approve        — reviewer accepts
    POST   /api/submissions/{id}/reject         — reviewer sends back (+ reason)
    GET    /api/my/series                       — "My work" (what they hand in)
    GET    /api/review/queue                    — "Awaiting review" inbox
    PATCH  /api/scenes/{scene_id}/assignee      — PM assigns one episode
    PATCH  /api/series/{series_id}/assignee     — PM hands over a whole series
    PATCH  /api/series/{series_id}/producer     — PM sets the Series Producer

The SERIES is the deliverable: one person takes it, hands in one finished cut, and
a PM reviews it once. It used to be the episode, which meant a twelve-episode
series was twelve hand-ins and twelve sign-offs of the same piece of work.

Authorization lives in ``submission_service`` (assignee-only submit, no
self-review, approver chain); this module maps its error vocab to HTTP.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlmodel import select

from flowboard.db import get_session
from flowboard.db.models import (
    EditNote,
    Project,
    Scene,
    SceneCollaborator,
    Series,
    Shot,
    Submission,
    User,
)
from flowboard.routes.deps import get_optional_user
from flowboard.services import (
    audit_service,
    auth,
    drive,
    drive_cache,
    permissions,
    resource_guard,
)
from flowboard.services import editor_service as es
from flowboard.services import submission_service as subs
from flowboard.services import user_service

logger = logging.getLogger(__name__)
router = APIRouter(tags=["submissions"])

_STATUS_BY_CODE = {
    "not_found": 404,
    "forbidden": 403,
    "bad_input": 400,
    "closed": 409,
}


def _fail(exc: subs.SubmissionError) -> HTTPException:
    return HTTPException(_STATUS_BY_CODE.get(exc.code, 400), str(exc))


def _name(user_id) -> Optional[str]:
    if not user_id:
        return None
    u = user_service.get_by_id(user_id)
    return (u.display_name or u.username) if u else None


def _sub_dict(row) -> dict:
    return {
        "id": str(row.id),
        "series_id": str(row.series_id) if row.series_id else None,
        # Only ever set on rows written before the series became the deliverable.
        "scene_id": str(row.scene_id) if row.scene_id else None,
        "version": row.version,
        "drive_url": row.drive_url,
        "drive_file_id": row.drive_file_id,
        # Preferred: our own proxy, so the file can stay Restricted on Drive and
        # the reviewer needs no Google account. `preview_url` (Drive's embed) is
        # kept as the fallback for when the app has no Drive identity configured.
        "stream_url": f"/api/submissions/{row.id}/video" if row.drive_file_id else None,
        "preview_url": (
            subs.drive_preview_url(row.drive_file_id) if row.drive_file_id else None
        ),
        "note": row.note,
        "submitted_by": str(row.submitted_by) if row.submitted_by else None,
        "submitted_by_name": _name(row.submitted_by),
        "submitted_at": row.submitted_at.isoformat() if row.submitted_at else None,
        "kind": getattr(row, "kind", "cut"),
        "status": row.status,
        "approver_user_id": str(row.approver_user_id) if row.approver_user_id else None,
        "approver_name": _name(row.approver_user_id),
        "reviewed_by_name": _name(row.reviewed_by),
        "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
        "review_note": row.review_note,
    }


def _deliverable_dict(series, session) -> dict:
    """A series + its delivery state, for the My-work / review lists.

    The series is what is handed in, so it is what these pages are rows of. The
    episode count comes along because "3 episodes" is how somebody recognises which
    series this is and how much is behind the one link.
    """
    # The WHOLE back-and-forth, newest first — not just the current attempt.
    # Both inboxes need it: an artist reading "sent back" wants to see what was
    # said the last two times, and a reviewer deciding on v3 is really asking what
    # changed since v1. It costs nothing extra — the latest attempt was already a
    # query for this same list.
    history = subs.list_submissions(session, series.id)
    latest = history[0] if history else None
    project = session.get(Project, series.project_id)
    episodes = list(
        session.exec(
            select(Scene)
            .where(Scene.series_id == series.id)
            .order_by(Scene.order_index, Scene.created_at)
        ).all()
    )
    return {
        "id": str(series.id),
        "name": series.name,
        "code": series.code or "",
        "project_id": str(series.project_id),
        "project_name": project.name if project else None,
        "assignee_user_id": (
            str(series.assignee_user_id) if series.assignee_user_id else None
        ),
        "assignee_name": _name(series.assignee_user_id),
        "producer_name": _name(series.producer_user_id),
        "deliverable_status": series.deliverable_status or "draft",
        "episode_count": len(episodes),
        "episodes": [
            {"id": str(e.id), "code": e.code or "", "name": e.name} for e in episodes
        ],
        "latest_submission": _sub_dict(latest) if latest else None,
        "submissions": [_sub_dict(r) for r in history],
    }


# ── Submit + history ──────────────────────────────────────────────────────


class SubmitBody(BaseModel):
    drive_url: str = Field(min_length=5, max_length=1000)
    note: Optional[str] = Field(default=None, max_length=2000)


@router.post("/api/series/{series_id}/submissions")
def create_submission(
    series_id: uuid.UUID, body: SubmitBody, user=Depends(get_optional_user)
):
    """Hand in the finished cut for a SERIES — one link, reviewed once."""
    with get_session() as s:
        try:
            row = subs.submit(
                s, series_id, user=user, drive_url=body.drive_url, note=body.note
            )
        except subs.SubmissionError as exc:
            raise _fail(exc)
        return _sub_dict(row)


@router.get("/api/series/{series_id}/submissions")
def list_series_submissions(series_id: uuid.UUID, user=Depends(get_optional_user)):
    with get_session() as s:
        series = s.get(Series, series_id)
        if series is None:
            raise HTTPException(404, "series not found")
        # Bare project membership is not enough: it would let anyone on the project
        # read another team's delivery history — drive links, submitter notes,
        # rejection reasons. `can_see_series` is the same scope that decides whether
        # the series exists for this caller at all.
        permissions.require(s, user, series.project_id, "canvas.read")
        if not permissions.can_see_series(s, user, series.project_id, series_id):
            raise HTTPException(404, "series not found")
        return {
            "series": _deliverable_dict(series, s),
            "submissions": [_sub_dict(r) for r in subs.list_submissions(s, series_id)],
        }


# ── Video proxy ───────────────────────────────────────────────────────────


@router.get("/api/submissions/{submission_id}/video")
async def stream_submission_video(
    submission_id: uuid.UUID, request: Request, user=Depends(get_optional_user)
):
    """Stream a submitted cut through the app, using the studio's own Drive
    identity.

    This is what lets the file stay **Restricted** on Drive: the reviewer never
    talks to Google, so they need no Google account and no link is ever shared
    outside the company. Access is decided here, by project permission.

    The browser's Range header is forwarded and Drive's 206 passed straight back,
    so seeking works without buffering the file anywhere.
    """
    with get_session() as s:
        # Scoped to the episode, not just the project. The proxy streams with
        # the studio's own Drive identity, so a gap here hands out cuts that are
        # deliberately Restricted on Drive — the one control this route exists
        # to enforce.
        row = resource_guard.authorize_submission(s, user, submission_id)
        file_id = row.drive_file_id

    if not file_id:
        raise HTTPException(404, "this submission has no Drive file")

    try:
        # Goes through the read-ahead cache: the first play pulls the file down
        # in one fast sequential request while serving the player from it, and
        # later plays are entirely local (instant seeking).
        status, headers, body = await drive_cache.serve(
            file_id, range_header=request.headers.get("range")
        )
    except drive.DriveError as exc:
        # 424: the dependency (Drive) refused — distinct from our own 403/404 so
        # the UI can say "ask the submitter to put it in the shared folder".
        code = {"not_configured": 501, "not_authorized": 503, "no_access": 424}.get(
            exc.code, 502
        )
        raise HTTPException(code, str(exc))

    return StreamingResponse(body, status_code=status, headers=headers)


# ── Review ────────────────────────────────────────────────────────────────


class ReviewBody(BaseModel):
    note: Optional[str] = Field(default=None, max_length=2000)


@router.post("/api/submissions/{submission_id}/approve")
def approve_submission(
    submission_id: uuid.UUID, body: ReviewBody, user=Depends(get_optional_user)
):
    with get_session() as s:
        try:
            return _sub_dict(subs.approve(s, submission_id, user=user, note=body.note))
        except subs.SubmissionError as exc:
            raise _fail(exc)


@router.post("/api/submissions/{submission_id}/reject")
def reject_submission(
    submission_id: uuid.UUID, body: ReviewBody, user=Depends(get_optional_user)
):
    with get_session() as s:
        try:
            return _sub_dict(
                subs.reject(s, submission_id, user=user, note=body.note or "")
            )
        except subs.SubmissionError as exc:
            raise _fail(exc)


# ── Listings ──────────────────────────────────────────────────────────────


@router.get("/api/my/series")
def my_series(user=Depends(get_optional_user)):
    """The series this employee has to hand in, with their delivery state."""
    with get_session() as s:
        if user is None:
            return {"series": []}
        rows = subs.series_for_assignee(s, user.id)
        return {"series": [_deliverable_dict(r, s) for r in rows]}


@router.get("/api/review/queue")
def review_inbox(user=Depends(get_optional_user)):
    """Submissions waiting on this reviewer (admins see all open ones)."""
    with get_session() as s:
        rows = subs.review_queue(s, user)
        out = []
        for r in rows:
            series = s.get(Series, r.series_id) if r.series_id else None
            out.append(
                {
                    "submission": _sub_dict(r),
                    "series": _deliverable_dict(series, s) if series else None,
                }
            )
        return {"items": out}


# ── Assignment (PM) ───────────────────────────────────────────────────────


class AssigneeBody(BaseModel):
    user_id: Optional[uuid.UUID] = None  # None clears the assignment


@router.patch("/api/scenes/{scene_id}/assignee")
def set_episode_assignee(
    scene_id: uuid.UUID,
    body: AssigneeBody,
    request: Request,
    user=Depends(get_optional_user),
):
    """Assign the employee who owns (and may submit) this episode. Lead+."""
    with get_session() as s:
        scene = s.get(Scene, scene_id)
        if scene is None:
            raise HTTPException(404, "episode not found")
        permissions.require(s, user, scene.project_id, "episode.update")
        if body.user_id is not None and s.get(User, body.user_id) is None:
            raise HTTPException(404, "user not found")
        previous = scene.assignee_user_id
        scene.assignee_user_id = body.user_id
        s.add(scene)
        s.commit()
        s.refresh(scene)
        # Reassignment is a management decision, so it belongs on the record:
        # "who was on Ep03 last week" is exactly what the Sheet could never say.
        audit_service.record_change(
            "episode.assignee",
            object_type="scene",
            object_id=scene.id,
            object_label=scene.code or scene.name,
            changes={"assignee": (_name(previous), _name(body.user_id))},
            actor=user,
            target=body.user_id,
            ip=audit_service.client_ip(request),
        )
        return {
            "id": str(scene.id),
            "code": scene.code or "",
            "name": scene.name,
            "assignee_user_id": (
                str(scene.assignee_user_id) if scene.assignee_user_id else None
            ),
            "assignee_name": _name(scene.assignee_user_id),
        }


# ── helpers on an episode ───────────────────────────────────────────────────
#
# One owner stays one owner. `assignee_user_id` is still the only person who may
# hand the cut in, because a deliverable two people can submit is one nobody is
# accountable for. These are the people added when that one person is
# overloaded — which a chapter split between three panel artists makes routine,
# since the whole chapter arrives as a single episode.


class HelperBody(BaseModel):
    user_id: uuid.UUID


def _helpers(session, scene_id: uuid.UUID) -> list[dict]:
    rows = session.exec(
        select(SceneCollaborator).where(SceneCollaborator.scene_id == scene_id)
    ).all()
    return [
        {
            "user_id": str(r.user_id),
            "name": _name(r.user_id),
            "added_by_name": _name(r.added_by),
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


@router.get("/api/scenes/{scene_id}/helpers")
def list_episode_helpers(scene_id: uuid.UUID, user=Depends(get_optional_user)):
    with get_session() as s:
        scene = s.get(Scene, scene_id)
        if scene is None:
            raise HTTPException(404, "episode not found")
        # Read gate goes through the SCENE, not the project: a helper must be
        # able to see the list they are on, and they cannot read the project
        # wholesale.
        permissions.require_scene(s, user, scene.project_id, scene_id, "canvas.read")
        return _helpers(s, scene_id)


@router.post("/api/scenes/{scene_id}/helpers")
def add_episode_helper(
    scene_id: uuid.UUID,
    body: HelperBody,
    request: Request,
    user=Depends(get_optional_user),
):
    """Add someone to help on this episode. Lead+, same as reassigning it."""
    with get_session() as s:
        scene = s.get(Scene, scene_id)
        if scene is None:
            raise HTTPException(404, "episode not found")
        permissions.require(s, user, scene.project_id, "episode.update")
        if s.get(User, body.user_id) is None:
            raise HTTPException(404, "user not found")
        if scene.assignee_user_id == body.user_id:
            # Not an error worth failing on — they already have everything a
            # helper row would grant, and more.
            return _helpers(s, scene_id)
        exists = s.exec(
            select(SceneCollaborator).where(
                SceneCollaborator.scene_id == scene_id,
                SceneCollaborator.user_id == body.user_id,
            )
        ).first()
        if exists is None:
            s.add(
                SceneCollaborator(
                    scene_id=scene_id,
                    user_id=body.user_id,
                    added_by=(user.id if user else None),
                )
            )
            s.commit()
            audit_service.record_change(
                "episode.helper_added",
                object_type="scene",
                object_id=scene.id,
                object_label=scene.code or scene.name,
                changes={"helper": (None, _name(body.user_id))},
                actor=user,
                target=body.user_id,
                ip=audit_service.client_ip(request),
            )
        return _helpers(s, scene_id)


@router.delete("/api/scenes/{scene_id}/helpers/{user_id}")
def remove_episode_helper(
    scene_id: uuid.UUID,
    user_id: uuid.UUID,
    request: Request,
    user=Depends(get_optional_user),
):
    with get_session() as s:
        scene = s.get(Scene, scene_id)
        if scene is None:
            raise HTTPException(404, "episode not found")
        permissions.require(s, user, scene.project_id, "episode.update")
        row = s.exec(
            select(SceneCollaborator).where(
                SceneCollaborator.scene_id == scene_id,
                SceneCollaborator.user_id == user_id,
            )
        ).first()
        if row is not None:
            s.delete(row)
            s.commit()
            audit_service.record_change(
                "episode.helper_removed",
                object_type="scene",
                object_id=scene.id,
                object_label=scene.code or scene.name,
                changes={"helper": (_name(user_id), None)},
                actor=user,
                target=user_id,
                ip=audit_service.client_ip(request),
            )
        return _helpers(s, scene_id)


class ProducerBody(BaseModel):
    user_id: Optional[uuid.UUID] = None  # None clears → chain falls back to PM


@router.patch("/api/series/{series_id}/assignee")
def set_series_assignee(
    series_id: uuid.UUID,
    body: ProducerBody,
    request: Request,
    user=Depends(get_optional_user),
):
    """Hand a whole series to one employee — every episode under it is theirs to
    work in and to hand in.

    The ordinary case: one person builds a series, and a PM should not have to
    assign twelve episodes one at a time. A per-episode assignment still overrides
    it, which is how a series is split when one person is overloaded.

    Deliberately NOT `producer_user_id`, which sits beside it: that is the first
    reviewer in the approver chain, and putting the artist there would make them
    the approver of their own submissions.
    """
    with get_session() as s:
        series = s.get(Series, series_id)
        if series is None:
            raise HTTPException(404, "series not found")
        permissions.require(s, user, series.project_id, "member.manage")
        if body.user_id is not None and s.get(User, body.user_id) is None:
            raise HTTPException(404, "user not found")
        previous = series.assignee_user_id
        series.assignee_user_id = body.user_id
        s.add(series)
        s.commit()
        s.refresh(series)
        # This grants access to a whole subtree; who changed it is worth keeping.
        audit_service.record_change(
            "series.assignee",
            object_type="series",
            object_id=series.id,
            object_label=series.code or series.name,
            changes={"assignee": (_name(previous), _name(body.user_id))},
            actor=user,
            target=body.user_id,
            ip=audit_service.client_ip(request),
        )
        return {
            "id": str(series.id),
            "assignee_user_id": (
                str(series.assignee_user_id) if series.assignee_user_id else None
            ),
            "assignee_name": _name(series.assignee_user_id),
        }


@router.patch("/api/series/{series_id}/producer")
def set_series_producer(
    series_id: uuid.UUID,
    body: ProducerBody,
    request: Request,
    user=Depends(get_optional_user),
):
    """Set the Series Producer — the first reviewer in the approver chain.
    Producer-level call (staffing a series), so gated on member.manage."""
    with get_session() as s:
        series = s.get(Series, series_id)
        if series is None:
            raise HTTPException(404, "series not found")
        permissions.require(s, user, series.project_id, "member.manage")
        if body.user_id is not None and s.get(User, body.user_id) is None:
            raise HTTPException(404, "user not found")
        previous = series.producer_user_id
        series.producer_user_id = body.user_id
        s.add(series)
        s.commit()
        s.refresh(series)
        # This one moves who reviews the work — worth knowing who changed it.
        audit_service.record_change(
            "series.producer",
            object_type="series",
            object_id=series.id,
            object_label=series.code or series.name,
            changes={"producer": (_name(previous), _name(body.user_id))},
            actor=user,
            target=body.user_id,
            ip=audit_service.client_ip(request),
        )
        return {
            "id": str(series.id),
            "producer_user_id": (
                str(series.producer_user_id) if series.producer_user_id else None
            ),
            "producer_name": _name(series.producer_user_id),
        }

# ── Raw material, for the editor ────────────────────────────────────────────


@router.post("/api/series/{series_id}/edits")
def submit_edit(series_id: uuid.UUID, body: SubmitBody, user=Depends(get_optional_user)):
    """The editor hands the assembled episode back.

    A second hand-over on the same series, not another attempt at the artist's:
    it has its own version sequence, and it does NOT move `deliverable_status`.
    That field tracks the artist's hand-over to the PM, and an approved series can
    still be re-cut — refusing that would make a fix after sign-off impossible.

    Gated on `cut.submit`, which the editor holds. Not on being the series
    assignee: that is the artist, and requiring it would mean the only person
    allowed to hand the edit back is the one who did not make it.
    """
    with get_session() as s:
        series = s.get(Series, series_id)
        if series is None:
            raise HTTPException(404, "series not found")
        permissions.require(s, user, series.project_id, "cut.submit")
        if not permissions.can_see_series(s, user, series.project_id, series_id):
            raise HTTPException(404, "series not found")
        try:
            row = subs.submit(
                s, series_id, user=user, drive_url=body.drive_url,
                note=body.note, kind="edit",
            )
        except subs.SubmissionError as exc:
            raise _fail(exc)
        return _sub_dict(row)


@router.get("/api/series/{series_id}/edits")
def list_edits(series_id: uuid.UUID, user=Depends(get_optional_user)):
    """Every cut the editor has handed back, newest first."""
    with get_session() as s:
        series = s.get(Series, series_id)
        if series is None:
            raise HTTPException(404, "series not found")
        permissions.require(s, user, series.project_id, "canvas.read")
        if not permissions.can_see_series(s, user, series.project_id, series_id):
            raise HTTPException(404, "series not found")
        return {"edits": [_sub_dict(r) for r in subs.list_submissions(s, series_id, kind="edit")]}


class NoteBody(BaseModel):
    at_seconds: float = Field(ge=0)
    #: The drawing over the frame, as a `data:image/png;base64,…` URL. Sent with
    #: the note rather than uploaded first: a drawing without its note is an
    #: orphan nobody can interpret, and two round trips is two ways to half-fail.
    drawing_data_url: Optional[str] = Field(default=None, max_length=8_000_000)
    body: str = Field(default="", max_length=4000)
    #: Which sequence this is about. Optional so a general note ("the whole thing
    #: is too dark") can be left without pinning it to a shot that is not at fault.
    shot_id: Optional[uuid.UUID] = None
    drawing_media_id: Optional[str] = Field(default=None, max_length=200)


def _note_dict(session, n: EditNote) -> dict:
    shot = session.get(Shot, n.shot_id) if n.shot_id else None
    return {
        "id": n.id,
        "submission_id": str(n.submission_id),
        "shot_id": str(n.shot_id) if n.shot_id else None,
        "shot_code": (shot.code or "") if shot else None,
        "at_seconds": n.at_seconds,
        "body": n.body,
        "drawing_media_id": n.drawing_media_id,
        "resolved": bool(n.resolved),
        "resolved_at": n.resolved_at.isoformat() if n.resolved_at else None,
        "author_name": _name(n.author_user_id),
        "created_at": n.created_at.isoformat() if n.created_at else None,
    }


def _ingest_drawing(data_url: str) -> Optional[str]:
    """Store a canvas export and return its media id.

    A failure here does NOT cost the note. The sentence and the sequence are what
    route the work to the right person; the drawing makes it precise. Losing the
    note because the picture would not save trades the whole message for the
    annotation on it.
    """
    import base64
    import uuid as _uuid

    from flowboard.services import media as media_service

    try:
        header, _, payload = data_url.partition(",")
        if "base64" not in header or not payload:
            return None
        raw = base64.b64decode(payload)
        mid = str(_uuid.uuid4())
        if media_service.ingest_inline_bytes(mid, raw, kind="image", mime="image/png"):
            return mid
    except Exception:  # noqa: BLE001
        logger.exception("could not store an annotation drawing")
    return None


def _note_guard(s, user, submission_id: uuid.UUID, capability: str):
    row = s.get(Submission, submission_id)
    if row is None or row.series_id is None:
        raise HTTPException(404, "submission not found")
    series = s.get(Series, row.series_id)
    if series is None:
        raise HTTPException(404, "submission not found")
    permissions.require(s, user, series.project_id, capability)
    if not permissions.can_see_series(s, user, series.project_id, series.id):
        raise HTTPException(404, "submission not found")
    return row, series


@router.post("/api/submissions/{submission_id}/notes")
def add_note(
    submission_id: uuid.UUID, body: NoteBody, request: Request,
    user=Depends(get_optional_user),
):
    """Leave a note on a frame of the cut, pinned to the sequence it is about."""
    with get_session() as s:
        _note_guard(s, user, submission_id, "cut.annotate")
        media_id = body.drawing_media_id
        if not media_id and body.drawing_data_url:
            media_id = _ingest_drawing(body.drawing_data_url)
        note = EditNote(
            submission_id=submission_id,
            shot_id=body.shot_id,
            at_seconds=float(body.at_seconds),
            body=(body.body or "").strip(),
            drawing_media_id=media_id,
            author_user_id=user.id if user else None,
        )
        s.add(note); s.commit(); s.refresh(note)
        return _note_dict(s, note)


@router.get("/api/submissions/{submission_id}/notes")
def list_notes(submission_id: uuid.UUID, user=Depends(get_optional_user)):
    """Every note on this cut, in play order — the order they are worked through."""
    with get_session() as s:
        _note_guard(s, user, submission_id, "canvas.read")
        rows = s.exec(
            select(EditNote)
            .where(EditNote.submission_id == submission_id)
            .order_by(EditNote.at_seconds)
        ).all()
        row = s.get(Submission, submission_id)
        # The series comes back with the notes because the page needs the strip of
        # sequences to pick from, and one call is one round trip.
        return {
            "series_id": str(row.series_id) if row and row.series_id else None,
            "notes": [_note_dict(s, n) for n in rows],
        }


@router.post("/api/notes/{note_id}/resolve")
def resolve_note(note_id: int, done: bool = True, user=Depends(get_optional_user)):
    """The artist marks a note dealt with — or puts it back.

    `canvas.write`, because the person who fixes it is the one who marks it fixed.
    Reversible on purpose: "fixed" that cannot be undone makes a mis-click a lie
    the editor then has to argue with.
    """
    with get_session() as s:
        note = s.get(EditNote, note_id)
        if note is None:
            raise HTTPException(404, "note not found")
        _note_guard(s, user, note.submission_id, "canvas.write")
        note.resolved = bool(done)
        note.resolved_by = (user.id if user and done else None)
        note.resolved_at = __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc) if done else None
        s.add(note); s.commit(); s.refresh(note)
        return _note_dict(s, note)


@router.get("/api/shots/{shot_id}/notes")
def notes_on_shot(shot_id: uuid.UUID, user=Depends(get_optional_user)):
    """What the editor said about THIS sequence.

    The half the artist sees. Arranged by sequence rather than by cut, because an
    artist opening a sequence is asking "what is wrong with this one" — making
    them open the editor's timeline and find their own shot in it is asking them
    to do the app's job.
    """
    with get_session() as s:
        shot = s.get(Shot, shot_id)
        if shot is None:
            raise HTTPException(404, "sequence not found")
        scene = s.get(Scene, shot.scene_id)
        permissions.require_scene(s, user, scene.project_id, scene.id, "canvas.read")
        rows = s.exec(
            select(EditNote)
            .where(EditNote.shot_id == shot_id)
            .order_by(EditNote.resolved, EditNote.at_seconds)
        ).all()
        return {
            "notes": [_note_dict(s, n) for n in rows],
            "open_count": sum(1 for n in rows if not n.resolved),
        }


@router.get("/api/scenes/{scene_id}/notes")
def notes_on_episode(scene_id: uuid.UUID, user=Depends(get_optional_user)):
    """Every editor note landing on any sequence of this episode.

    One call for the canvas. Per-sequence it would be one request per node on a
    page that already has forty, and the answer is almost always "none".
    """
    with get_session() as s:
        scene = s.get(Scene, scene_id)
        if scene is None:
            raise HTTPException(404, "episode not found")
        permissions.require_scene(s, user, scene.project_id, scene_id, "canvas.read")
        shot_ids = list(s.exec(select(Shot.id).where(Shot.scene_id == scene_id)).all())
        if not shot_ids:
            return {"notes": [], "open_count": 0}
        rows = s.exec(
            select(EditNote)
            .where(EditNote.shot_id.in_(shot_ids))  # type: ignore[attr-defined]
            .order_by(EditNote.resolved, EditNote.at_seconds)
        ).all()
        return {
            "notes": [_note_dict(s, n) for n in rows],
            "open_count": sum(1 for n in rows if not n.resolved),
        }


@router.get("/api/my/materials")
def my_materials(user=Depends(get_optional_user)):
    """Series this account may pull raw material from — the editor's own page.

    Arranged by person rather than by project, for the same reason "My work" is:
    an editor is handed series across several projects and their question is
    "what is waiting for me to cut", which no project page can answer.
    """
    with get_session() as s:
        if user is None:
            return {"series": []}
        out = []
        for sr in s.exec(select(Series).order_by(Series.order_index, Series.created_at)).all():
            role = permissions.project_role(s, user, sr.project_id)
            if not permissions.role_allows(role, "material.pull"):
                continue
            if not permissions.can_see_series(s, user, sr.project_id, sr.id):
                continue
            project = s.get(Project, sr.project_id)
            data = es.materials(s, sr.id)
            # The editor's own latest cut, so the page can offer the way into it.
            # Without this the review screen existed and nothing led to it.
            latest_edit = subs.latest_submission(s, sr.id, kind="edit")
            out.append({
                "latest_edit_id": str(latest_edit.id) if latest_edit else None,
                "latest_edit_version": latest_edit.version if latest_edit else None,
                "id": str(sr.id),
                "name": sr.name,
                "code": sr.code or "",
                "project_name": project.name if project else None,
                "role": role,
                "episode_count": len(data["episodes"]),
                "clip_count": data["clip_count"],
                "deliverable_status": sr.deliverable_status or "draft",
            })
        return {"series": out}


@router.get("/api/series/{series_id}/materials")
def list_materials(
    series_id: uuid.UUID, all_takes: bool = False, user=Depends(get_optional_user)
):
    """Every generated clip in the series, grouped episode → sequence.

    `material.pull`, which the editor holds and a viewer does not — the raw
    material of a series is the whole of somebody's unfinished work, and being
    able to READ a project is not the same as being handed all of it as files.
    """
    with get_session() as s:
        series = s.get(Series, series_id)
        if series is None:
            raise HTTPException(404, "series not found")
        permissions.require(s, user, series.project_id, "material.pull")
        if not permissions.can_see_series(s, user, series.project_id, series_id):
            raise HTTPException(404, "series not found")
        return es.materials(s, series_id, all_takes=all_takes)


@router.get("/api/series/{series_id}/materials-token")
def materials_download_token(
    series_id: uuid.UUID, user=Depends(get_optional_user)
):
    """Mint a short-lived, series-scoped token so the browser's native
    downloader can fetch the (up to 2 GB) zip via a plain link — an ``<a href>``
    can't carry the Bearer header. Gated by the SAME permission as the download
    itself, so this leaks no access the caller doesn't already have.

    No-auth single-user mode has no account to bind a token to; it also has no
    auth gate to satisfy, so the empty token simply falls back to the plain URL.
    """
    with get_session() as s:
        series = s.get(Series, series_id)
        if series is None:
            raise HTTPException(404, "series not found")
        permissions.require(s, user, series.project_id, "material.pull")
        if not permissions.can_see_series(s, user, series.project_id, series_id):
            raise HTTPException(404, "series not found")
        token = (
            auth.make_download_token(
                str(user.id),
                f"series:{series_id}:materials",
                token_version=int(user.token_version or 0),
            )
            if user is not None
            else ""
        )
    return {"token": token}


@router.get("/api/series/{series_id}/materials.zip")
async def download_materials(
    series_id: uuid.UUID, all_takes: bool = False, user=Depends(get_optional_user)
):
    """The whole series as one archive, named the way the editor will refer to it.

    Files that cannot be read are SKIPPED, not fatal, and the count of skips comes
    back in a header: one clip missing from storage must not cost the editor the
    other thirty-nine, and silently shipping thirty-nine as if it were forty is
    how a missing shot reaches the cut.
    """
    import io
    import zipfile

    with get_session() as s:
        series = s.get(Series, series_id)
        if series is None:
            raise HTTPException(404, "series not found")
        permissions.require(s, user, series.project_id, "material.pull")
        if not permissions.can_see_series(s, user, series.project_id, series_id):
            raise HTTPException(404, "series not found")
        data = es.materials(s, series_id, all_takes=all_takes)
        stem = (series.code or series.name or "series").strip().replace(" ", "-")

    clips = es.flat_clips(data)
    if not clips:
        raise HTTPException(404, "this series has no generated clips yet")

    buf = io.BytesIO()
    written = skipped = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        # STORED, not DEFLATED: these are already-compressed video files, so
        # deflate spends minutes of CPU to save nothing.
        for name, media_id in clips:
            got = await _clip_bytes(media_id)
            if got is None:
                skipped += 1
                continue
            payload, ext = got
            if ext and not name.lower().endswith(f".{ext}"):
                name = name.rsplit(".", 1)[0] + f".{ext}"
            zf.writestr(name, payload)
            written += 1

    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="application/zip",
        headers={
            "content-disposition": f'attachment; filename="{stem}_material.zip"',
            "x-clips-written": str(written),
            "x-clips-skipped": str(skipped),
        },
    )


async def _clip_bytes(media_id: str):
    """Cached file if there is one, otherwise fetch it once and cache it."""
    from flowboard.services import media as media_service

    path = media_service.cached_path(media_id)
    if path is not None and path.exists():
        return path.read_bytes(), path.suffix.lstrip(".").lower() or "mp4"
    got = await media_service.fetch_and_cache(media_id)
    if got is None:
        return None
    payload, _mime, cached = got
    return payload, (cached.suffix.lstrip(".").lower() or "mp4")
