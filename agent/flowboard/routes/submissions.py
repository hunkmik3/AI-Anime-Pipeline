"""Phase 11 — deliverable submission + review REST surface.

    POST   /api/scenes/{scene_id}/submissions   — employee submits a Drive link
    GET    /api/scenes/{scene_id}/submissions   — that episode's full history
    POST   /api/submissions/{id}/approve        — reviewer accepts
    POST   /api/submissions/{id}/reject         — reviewer sends back (+ reason)
    GET    /api/my/episodes                     — "My work" (assignee's episodes)
    GET    /api/review/queue                    — "Awaiting review" inbox
    PATCH  /api/scenes/{scene_id}/assignee      — PM assigns the episode owner
    PATCH  /api/series/{series_id}/producer     — PM sets the Series Producer

Authorization lives in ``submission_service`` (assignee-only submit, no
self-review, approver chain); this module maps its error vocab to HTTP.
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlmodel import select

from flowboard.db import get_session
from flowboard.db.models import (
    Project,
    Scene,
    SceneCollaborator,
    Series,
    Submission,
    User,
)
from flowboard.routes.deps import get_optional_user
from flowboard.services import (
    audit_service,
    drive,
    drive_cache,
    permissions,
    resource_guard,
)
from flowboard.services import submission_service as subs
from flowboard.services import user_service

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
        "scene_id": str(row.scene_id),
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
        "status": row.status,
        "approver_user_id": str(row.approver_user_id) if row.approver_user_id else None,
        "approver_name": _name(row.approver_user_id),
        "reviewed_by_name": _name(row.reviewed_by),
        "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
        "review_note": row.review_note,
    }


def _episode_dict(s, session) -> dict:
    """Episode + its delivery state, for the My-work / inbox lists."""
    latest = subs.latest_submission(session, s.id)
    project = session.get(Project, s.project_id)
    series = session.get(Series, s.series_id) if s.series_id else None
    return {
        "id": str(s.id),
        "name": s.name,
        "code": s.code or "",
        "project_id": str(s.project_id),
        "project_name": project.name if project else None,
        "series_id": str(s.series_id) if s.series_id else None,
        "series_name": series.name if series else None,
        # The code is how people say which series it is out loud; without it two
        # series both holding an "Episode 9" are indistinguishable in the inboxes.
        "series_code": (series.code or "") if series else None,
        "assignee_user_id": str(s.assignee_user_id) if s.assignee_user_id else None,
        "assignee_name": _name(s.assignee_user_id),
        "deliverable_status": s.deliverable_status or "draft",
        "latest_submission": _sub_dict(latest) if latest else None,
    }


# ── Submit + history ──────────────────────────────────────────────────────


class SubmitBody(BaseModel):
    drive_url: str = Field(min_length=5, max_length=1000)
    note: Optional[str] = Field(default=None, max_length=2000)


@router.post("/api/scenes/{scene_id}/submissions")
def create_submission(
    scene_id: uuid.UUID, body: SubmitBody, user=Depends(get_optional_user)
):
    with get_session() as s:
        try:
            row = subs.submit(
                s, scene_id, user=user, drive_url=body.drive_url, note=body.note
            )
        except subs.SubmissionError as exc:
            raise _fail(exc)
        return _sub_dict(row)


@router.get("/api/scenes/{scene_id}/submissions")
def list_scene_submissions(scene_id: uuid.UUID, user=Depends(get_optional_user)):
    with get_session() as s:
        # Project membership was not enough: it let an artist assigned one
        # episode read a sibling's delivery history — drive links, submitter
        # notes and rejection reasons — which is exactly what the episode
        # visibility scope exists to prevent.
        scene = resource_guard.authorize_scene(s, user, scene_id)
        return {
            "episode": _episode_dict(scene, s),
            "submissions": [_sub_dict(r) for r in subs.list_submissions(s, scene_id)],
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


@router.get("/api/my/episodes")
def my_episodes(user=Depends(get_optional_user)):
    """The signed-in employee's assigned episodes + their delivery state."""
    with get_session() as s:
        if user is None:
            return {"episodes": []}
        rows = subs.episodes_for_assignee(s, user.id)
        return {"episodes": [_episode_dict(r, s) for r in rows]}


@router.get("/api/review/queue")
def review_inbox(user=Depends(get_optional_user)):
    """Submissions waiting on this reviewer (admins see all open ones)."""
    with get_session() as s:
        rows = subs.review_queue(s, user)
        out = []
        for r in rows:
            scene = s.get(Scene, r.scene_id)
            out.append(
                {
                    "submission": _sub_dict(r),
                    "episode": _episode_dict(scene, s) if scene else None,
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
        return _episode_dict(scene, s)


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
