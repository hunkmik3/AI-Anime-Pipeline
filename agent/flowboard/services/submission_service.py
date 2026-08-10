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
    session: Session, series: Series, submitter_id: Optional[uuid.UUID]
) -> Optional[User]:
    """Who reviews this series, per the approver-chain diagram:

        Series Producer (if set and not the submitter)
          → project PM / owner (if set and not the submitter)
            → escalation owner (project.settings["escalation_owner_id"], else an admin)

    Returns None only when nothing resolves (no producer, no owner, no admin) —
    the caller treats that as a configuration error rather than silently
    letting work sit unreviewable.
    """
    candidates: list[Optional[uuid.UUID]] = [series.producer_user_id]

    project = session.get(Project, series.project_id)
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


def list_submissions(session: Session, series_id: uuid.UUID) -> list[Submission]:
    """Full history for a series, newest attempt first."""
    return list(
        session.exec(
            select(Submission)
            .where(Submission.series_id == series_id)
            .order_by(Submission.version.desc())  # type: ignore[attr-defined]
        ).all()
    )


def latest_submission(session: Session, series_id: uuid.UUID) -> Optional[Submission]:
    rows = list_submissions(session, series_id)
    return rows[0] if rows else None


# ── Commands ────────────────────────────────────────────────────────────────


def submit(
    session: Session,
    series_id: uuid.UUID,
    *,
    user: Optional[User],
    drive_url: str,
    note: Optional[str] = None,
) -> Submission:
    """Record a delivery attempt for a SERIES.

    The series is what gets handed over: one person takes it, and a PM reviews the
    finished thing once rather than signing off on each episode. Guards: only the
    person the series was given to (or an admin) may submit; the URL must be a
    Drive link we can embed; a series already approved/paid is closed.
    """
    series = session.get(Series, series_id)
    if series is None:
        raise SubmissionError("not_found", "series not found")

    is_admin = user is not None and user.role == "admin"
    if user is not None and not is_admin:
        owner = series.assignee_user_id
        if owner is None:
            raise SubmissionError(
                "forbidden", "this series has no assignee — ask your PM to assign it"
            )
        if owner != user.id:
            raise SubmissionError(
                "forbidden", "only the assigned employee can submit this series"
            )

    if series.deliverable_status in ("approved", "paid"):
        raise SubmissionError(
            "closed", f"this series is already {series.deliverable_status}"
        )

    # One open attempt at a time. In the lifecycle, `submitted` is left only by the
    # approver — accepting or sending back — so the series is out of the
    # assignee's hands until then. Without this an artist could stack v2, v3, v4
    # on top of a pending v1, and the reviewer's queue filled with several rows for
    # the same series with no way to tell which one counted.
    open_attempt = latest_submission(session, series_id)
    if open_attempt is not None and open_attempt.status == "submitted":
        raise SubmissionError(
            "closed",
            f"v{open_attempt.version} of this series is already waiting for "
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

    prev = latest_submission(session, series_id)
    submitter_id = user.id if user is not None else None
    approver = resolve_approver(session, series, submitter_id)

    row = Submission(
        series_id=series_id,
        version=(prev.version + 1) if prev else 1,
        drive_url=drive_url.strip(),
        drive_file_id=file_id,
        note=(note or "").strip() or None,
        submitted_by=submitter_id,
        status="submitted",
        approver_user_id=approver.id if approver else None,
    )
    session.add(row)
    series.deliverable_status = "submitted"
    session.add(series)
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

    series = session.get(Series, row.series_id) if row.series_id else None
    if series is None:
        raise SubmissionError("not_found", "series not found")

    is_admin = user is not None and user.role == "admin"
    if user is not None:
        # Never review your own submission — that's the whole point of the chain.
        if row.submitted_by is not None and row.submitted_by == user.id:
            raise SubmissionError("forbidden", "you cannot review your own submission")
        if not is_admin:
            # The resolved approver reviews; PM/producer of the project may also
            # step in (the chain picked one, but the tier above stays able to act).
            allowed = {row.approver_user_id, series.producer_user_id}
            project = session.get(Project, series.project_id)
            if project is not None and project.owner_user_id:
                allowed.add(project.owner_user_id)
            if user.id not in {a for a in allowed if a}:
                raise SubmissionError("forbidden", "you are not the reviewer for this series")

    clean_note = (note or "").strip() or None
    if not approve and not clean_note:
        raise SubmissionError("bad_input", "a reason is required when sending work back")

    row.status = "approved" if approve else "rejected"
    row.reviewed_by = user.id if user is not None else None
    row.reviewed_at = datetime.now(timezone.utc)
    row.review_note = clean_note
    # Approved locks production; a rejection returns the series to draft so the
    # assignee can resubmit — their generation quota is NOT topped up.
    series.deliverable_status = "approved" if approve else "draft"
    session.add(row)
    session.add(series)
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
    """Who works in this episode: its own assignee, else whoever the whole series
    was handed to.

    Still about WORK, not delivery — the episode's canvas and its access. What
    gets handed in is the series, and only its assignee submits that.
    """
    if scene.assignee_user_id is not None:
        return scene.assignee_user_id
    if scene.series_id:
        series = session.get(Series, scene.series_id)
        if series is not None:
            return series.assignee_user_id
    return None


def delivery_status_map(
    session: Session, scenes: "list[Scene]"
) -> dict[uuid.UUID, str]:
    """Each episode's delivery state, which is its SERIES' state.

    An episode is not handed in on its own any more, so it has no state of its
    own to report. `Scene.deliverable_status` is still a column — it holds what
    rows written under the old rule said — and reading it now would report "draft"
    for every episode inside an approved series.

    One query for the whole list: the callers are per-project and per-series
    rollups that would otherwise ask once per episode.
    """
    sids = {sc.series_id for sc in scenes if sc.series_id}
    if not sids:
        return {sc.id: "draft" for sc in scenes}
    by_series = {
        sr.id: (sr.deliverable_status or "draft")
        for sr in session.exec(
            select(Series).where(Series.id.in_(sids))  # type: ignore[attr-defined]
        ).all()
    }
    return {sc.id: by_series.get(sc.series_id, "draft") for sc in scenes}


def episode_delivery_status(session: Session, scene: Scene) -> str:
    return delivery_status_map(session, [scene]).get(scene.id, "draft")


def series_for_assignee(session: Session, user_id: uuid.UUID) -> list[Series]:
    """Series this employee has to hand in — their "My work" page.

    The page lists what is DELIVERABLE, and a series is delivered as one thing.
    It used to list episodes, one card each, every one offering a "Hand in" that
    delivered a twelfth of the job.

    Episode-level assignment is not swept in here on purpose: being lent one
    episode of somebody else's series is work, and the person who hands the series
    in is still the person it was given to.
    """
    return list(
        session.exec(
            select(Series)
            .where(Series.assignee_user_id == user_id)
            .order_by(Series.order_index, Series.created_at)
        ).all()
    )


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
        series = session.get(Series, r.series_id) if r.series_id else None
        if series is None:
            continue
        if series.producer_user_id == user.id:
            mine.append(r)
            continue
        project = session.get(Project, series.project_id)
        if project is not None and project.owner_user_id == user.id:
            mine.append(r)
    return mine
