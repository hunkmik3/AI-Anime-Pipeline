"""Phase 11 — deliverable submission + review.

An **Episode** is the deliverable. Its sequences are generated in-app, the cut is
assembled and edited *outside* the app, then the finished video is uploaded to
Google Drive and its link submitted here for review.

Three rules from the workflow diagrams drive this module:

1. **Only the assignee submits.** ``scene.assignee_user_id`` owns the episode;
   admins may act on anyone's behalf (support/backfill), nobody else can.
2. **Nobody reviews their own work.** The approver chain resolves
   Series Producer → project PM → escalation owner, skipping whoever submitted.
3. **History is append-only.** Each attempt is a Submission row (v1, v2, …); a
   rejection keeps its reason and sends the Episode back to ``draft`` — it never
   rewrites the previous attempt, so the back-and-forth stays auditable.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Session, select

from flowboard.db.models import Project, Scene, Series, Submission, User

logger = logging.getLogger(__name__)


class SubmissionError(Exception):
    """Domain error; ``code`` is a short vocab the route maps to a status."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


DELIVERABLE_STATES = ("draft", "submitted", "approved", "paid")

# ── Google Drive links ──────────────────────────────────────────────────────
# Accepted shapes (all yield the same file id):
#   https://drive.google.com/file/d/<ID>/view?usp=sharing
#   https://drive.google.com/open?id=<ID>
#   https://drive.google.com/uc?id=<ID>&export=download
#   https://docs.google.com/file/d/<ID>/edit
_DRIVE_PATTERNS = (
    re.compile(r"/file/d/([A-Za-z0-9_-]{10,})"),
    re.compile(r"[?&]id=([A-Za-z0-9_-]{10,})"),
    re.compile(r"/d/([A-Za-z0-9_-]{10,})"),
)


def parse_drive_file_id(url: str) -> Optional[str]:
    """Pull the file id out of a Drive URL, or None if it isn't one."""
    u = (url or "").strip()
    if not u:
        return None
    host_ok = "drive.google.com" in u or "docs.google.com" in u
    if not host_ok:
        return None
    for pat in _DRIVE_PATTERNS:
        m = pat.search(u)
        if m:
            return m.group(1)
    return None


def drive_preview_url(file_id: str) -> str:
    """Embeddable player URL — what the reviewer's <iframe> points at, so the
    video plays in-app instead of bouncing to Drive. Requires the file to be
    link-shared; Drive itself enforces that, we only build the URL."""
    return f"https://drive.google.com/file/d/{file_id}/preview"


# ── Approver chain ──────────────────────────────────────────────────────────


def _first_admin(session: Session) -> Optional[User]:
    return session.exec(select(User).where(User.role == "admin")).first()


def resolve_approver(
    session: Session, scene: Scene, submitter_id: Optional[uuid.UUID]
) -> Optional[User]:
    """Who reviews this episode, per the approver-chain diagram:

        Series Producer (if set and not the submitter)
          → project PM / owner (if set and not the submitter)
            → escalation owner (project.settings["escalation_owner_id"], else an admin)

    Returns None only when nothing resolves (no producer, no owner, no admin) —
    the caller treats that as a configuration error rather than silently
    letting work sit unreviewable.
    """
    candidates: list[Optional[uuid.UUID]] = []

    if scene.series_id:
        series = session.get(Series, scene.series_id)
        if series is not None:
            candidates.append(series.producer_user_id)

    project = session.get(Project, scene.project_id)
    if project is not None:
        candidates.append(project.owner_user_id)
        esc = (project.settings or {}).get("escalation_owner_id")
        if esc:
            try:
                candidates.append(uuid.UUID(str(esc)))
            except (ValueError, TypeError):
                pass

    for cid in candidates:
        if cid and cid != submitter_id:
            u = session.get(User, cid)
            if u is not None and getattr(u, "status", "active") == "active":
                return u

    # Last resort: an admin who isn't the submitter (the escalation owner).
    admin = _first_admin(session)
    if admin is not None and admin.id != submitter_id:
        return admin
    return None


# ── Queries ─────────────────────────────────────────────────────────────────


def list_submissions(session: Session, scene_id: uuid.UUID) -> list[Submission]:
    """Full history for an episode, newest attempt first."""
    return list(
        session.exec(
            select(Submission)
            .where(Submission.scene_id == scene_id)
            .order_by(Submission.version.desc())  # type: ignore[attr-defined]
        ).all()
    )


def latest_submission(session: Session, scene_id: uuid.UUID) -> Optional[Submission]:
    rows = list_submissions(session, scene_id)
    return rows[0] if rows else None


# ── Commands ────────────────────────────────────────────────────────────────


def submit(
    session: Session,
    scene_id: uuid.UUID,
    *,
    user: Optional[User],
    drive_url: str,
    note: Optional[str] = None,
) -> Submission:
    """Record a delivery attempt for an episode.

    Guards: only the assignee (or an admin) may submit; the URL must be a Drive
    link we can embed; an episode already approved/paid is closed.
    """
    scene = session.get(Scene, scene_id)
    if scene is None:
        raise SubmissionError("not_found", "episode not found")

    is_admin = user is not None and user.role == "admin"
    if user is not None and not is_admin:
        # Either the episode is theirs, or the whole series is. A PM who hands over
        # a twelve-episode series does not then assign twelve episodes, and without
        # this the person they gave it to could open every episode and hand none of
        # them in.
        owner = _work_owner(session, scene)
        if owner is None:
            raise SubmissionError(
                "forbidden", "this episode has no assignee — ask your PM to assign it"
            )
        if owner != user.id and scene.assignee_user_id != user.id:
            raise SubmissionError(
                "forbidden", "only the assigned employee can submit this episode"
            )

    if scene.deliverable_status in ("approved", "paid"):
        raise SubmissionError(
            "closed", f"this episode is already {scene.deliverable_status}"
        )

    # One open attempt at a time. In the lifecycle, `submitted` is left only by the
    # approver — accepting or sending back — so the episode is out of the
    # assignee's hands until then. Without this an artist could stack v2, v3, v4
    # on top of a pending v1, and the reviewer's queue filled with several rows for
    # the same episode with no way to tell which one counted.
    open_attempt = latest_submission(session, scene_id)
    if open_attempt is not None and open_attempt.status == "submitted":
        raise SubmissionError(
            "closed",
            f"v{open_attempt.version} of this episode is already waiting for "
            "review — ask the reviewer to send it back if you need to replace it",
        )

    file_id = parse_drive_file_id(drive_url)
    if not file_id:
        # A folder link is the natural mistake — people paste where they *put* the
        # file rather than the file — so say that instead of repeating the format.
        if "/drive/folders/" in (drive_url or "") or "/folders/" in (drive_url or ""):
            raise SubmissionError(
                "bad_input",
                "that's a link to a folder, not to the video. Open the cut in "
                "Drive, then use Share → Copy link on the file itself — it looks "
                "like https://drive.google.com/file/d/<id>/view",
            )
        raise SubmissionError(
            "bad_input",
            "paste a Google Drive video link, e.g. "
            "https://drive.google.com/file/d/<id>/view",
        )

    # Check the app can actually open it BEFORE accepting the submission. Without
    # this the mistake (file outside the shared folder) only surfaces when a
    # reviewer tries to watch — after the assignee has moved on.
    from flowboard.services import drive as drive_service

    if drive_service.is_configured():
        try:
            drive_service.get_metadata(file_id)
        except drive_service.DriveError as exc:
            if exc.code == "no_access":
                raise SubmissionError(
                    "bad_input",
                    "the app can't open that file — move it into the shared "
                    "submissions folder on Drive, then submit again",
                )
            # Drive being down must not block a hand-in; the reviewer's player
            # will surface it instead.
            logger.warning("drive precheck skipped: %s", exc)

    prev = latest_submission(session, scene_id)
    submitter_id = user.id if user is not None else None
    approver = resolve_approver(session, scene, submitter_id)

    row = Submission(
        scene_id=scene_id,
        version=(prev.version + 1) if prev else 1,
        drive_url=drive_url.strip(),
        drive_file_id=file_id,
        note=(note or "").strip() or None,
        submitted_by=submitter_id,
        status="submitted",
        approver_user_id=approver.id if approver else None,
    )
    session.add(row)
    scene.deliverable_status = "submitted"
    session.add(scene)
    session.commit()
    session.refresh(row)
    return row


def _review(
    session: Session,
    submission_id: uuid.UUID,
    *,
    user: Optional[User],
    approve: bool,
    note: Optional[str],
) -> Submission:
    row = session.get(Submission, submission_id)
    if row is None:
        raise SubmissionError("not_found", "submission not found")
    if row.status != "submitted":
        raise SubmissionError("closed", f"this submission is already {row.status}")

    scene = session.get(Scene, row.scene_id)
    if scene is None:
        raise SubmissionError("not_found", "episode not found")

    is_admin = user is not None and user.role == "admin"
    if user is not None:
        # Never review your own submission — that's the whole point of the chain.
        if row.submitted_by is not None and row.submitted_by == user.id:
            raise SubmissionError("forbidden", "you cannot review your own submission")
        if not is_admin:
            # The resolved approver reviews; PM/producer of the project may also
            # step in (the chain picked one, but the tier above stays able to act).
            allowed = {row.approver_user_id}
            project = session.get(Project, scene.project_id)
            if project is not None and project.owner_user_id:
                allowed.add(project.owner_user_id)
            if scene.series_id:
                series = session.get(Series, scene.series_id)
                if series is not None and series.producer_user_id:
                    allowed.add(series.producer_user_id)
            if user.id not in {a for a in allowed if a}:
                raise SubmissionError("forbidden", "you are not the reviewer for this episode")

    clean_note = (note or "").strip() or None
    if not approve and not clean_note:
        raise SubmissionError("bad_input", "a reason is required when sending work back")

    row.status = "approved" if approve else "rejected"
    row.reviewed_by = user.id if user is not None else None
    row.reviewed_at = datetime.now(timezone.utc)
    row.review_note = clean_note
    # Approved locks production; a rejection returns the episode to draft so the
    # assignee can resubmit — their generation quota is NOT topped up.
    scene.deliverable_status = "approved" if approve else "draft"
    session.add(row)
    session.add(scene)
    session.commit()
    session.refresh(row)
    return row


def approve(
    session: Session,
    submission_id: uuid.UUID,
    *,
    user: Optional[User],
    note: Optional[str] = None,
) -> Submission:
    return _review(session, submission_id, user=user, approve=True, note=note)


def reject(
    session: Session,
    submission_id: uuid.UUID,
    *,
    user: Optional[User],
    note: str,
) -> Submission:
    return _review(session, submission_id, user=user, approve=False, note=note)


# ── Inbox / my-work listings ────────────────────────────────────────────────


def _work_owner(session: Session, scene: Scene) -> Optional[uuid.UUID]:
    """Who is on the hook for this episode: its own assignee, else whoever the
    whole series was handed to. ``None`` when nobody has been given it."""
    if scene.assignee_user_id is not None:
        return scene.assignee_user_id
    if scene.series_id:
        series = session.get(Series, scene.series_id)
        if series is not None:
            return series.assignee_user_id
    return None


def episodes_for_assignee(session: Session, user_id: uuid.UUID) -> list[Scene]:
    """Episodes this employee is on the hook for (their "My work" page).

    Episodes assigned to them directly, plus every episode of a series handed to
    them as a whole. Without the second, a PM assigning a twelve-episode series
    left that person's "My work" page empty — and it is the page they work from.

    An episode inside their series that was then assigned to somebody ELSE is not
    theirs: the narrower assignment is a deliberate act and overrides the sweep.
    """
    mine = list(
        session.exec(
            select(Scene)
            .where(Scene.assignee_user_id == user_id)
            .order_by(Scene.order_index, Scene.created_at)
        ).all()
    )
    series_ids = [
        sid
        for sid in session.exec(
            select(Series.id).where(Series.assignee_user_id == user_id)
        ).all()
    ]
    if series_ids:
        mine += list(
            session.exec(
                select(Scene)
                .where(
                    Scene.series_id.in_(series_ids),  # type: ignore[attr-defined]
                    Scene.assignee_user_id.is_(None),  # type: ignore[union-attr]
                )
                .order_by(Scene.order_index, Scene.created_at)
            ).all()
        )
    seen: set[uuid.UUID] = set()
    out = []
    for sc in mine:
        if sc.id not in seen:
            seen.add(sc.id)
            out.append(sc)
    return out


def review_queue(session: Session, user: Optional[User]) -> list[Submission]:
    """Submissions waiting on this reviewer, newest first.

    An admin sees every open submission; anyone else sees the ones the chain
    routed to them, plus those on projects/series they run (so a PM can clear a
    backlog even when the chain named the Series Producer).
    """
    stmt = select(Submission).where(Submission.status == "submitted")
    rows = list(session.exec(stmt.order_by(Submission.submitted_at.desc())).all())  # type: ignore[attr-defined]
    if user is None or user.role == "admin":
        return rows

    mine: list[Submission] = []
    for r in rows:
        if r.submitted_by == user.id:
            continue  # never queue your own work for yourself
        if r.approver_user_id == user.id:
            mine.append(r)
            continue
        scene = session.get(Scene, r.scene_id)
        if scene is None:
            continue
        project = session.get(Project, scene.project_id)
        if project is not None and project.owner_user_id == user.id:
            mine.append(r)
            continue
        if scene.series_id:
            series = session.get(Series, scene.series_id)
            if series is not None and series.producer_user_id == user.id:
                mine.append(r)
    return mine
