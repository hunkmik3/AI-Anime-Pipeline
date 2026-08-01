"""ORM models for the anime narrative pipeline (Phase 1 refactor).

Hierarchy: Project → Scene → Shot → (Node, Edge). Project/Scene/Shot use
UUID PKs; child tables keep INT PKs for compatibility with existing tests
and frontend.

JSON columns are dialect-aware via ``with_variant``: **JSONB on Postgres**
(dev/prod/tests — indexable, unchanged) and generic **JSON on every other
dialect** (SQLite, for the self-contained desktop build that creates its
schema with ``SQLModel.metadata.create_all``). ``uuid.UUID`` PKs map through
SQLModel's generic ``Uuid`` type, which is native UUID on Postgres and
CHAR(32) on SQLite — no per-dialect handling needed.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import JSON, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Column, Field, SQLModel

# JSONB on Postgres, plain JSON elsewhere (SQLite). One type, both engines.
_JSON = JSON().with_variant(JSONB(), "postgresql")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _uuid_pk() -> uuid.UUID:
    return uuid.uuid4()


def _jsonb_dict() -> Column:
    return Column(_JSON, nullable=False, server_default=text("'{}'"))


def _jsonb_list() -> Column:
    return Column(_JSON, nullable=False, server_default=text("'[]'"))


# ── Hierarchy: Project → Series → Scene(Episode/Chapter) → Shot(Sequence) ──
#
# Phase 10 renamed the production tiers to match how the studio actually talks
# about the work. The table names stay ``scene``/``shot`` (renaming them would
# touch every FK, canvas payload and saved workflow for no functional gain);
# only the user-facing labels move:
#
#     Project  →  Series  →  Episode | Chapter  →  Sequence
#     project     series      scene                shot
#
# ``Series`` is the new tier. A Series decides whether its children are called
# Episodes or Chapters (``unit_label``), so one Project can hold an animated
# series and a webtoon side by side.


class Project(SQLModel, table=True):
    id: uuid.UUID = Field(
        default_factory=_uuid_pk,
        primary_key=True,
        sa_column_kwargs={"server_default": None},
    )
    name: str
    # Multi-user (Phase 9): the user who owns this project. Nullable so rows
    # created before auth (or by admin tooling) survive; all reads scope by it.
    owner_user_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="app_user.id", index=True
    )
    project_bible: dict[str, Any] = Field(default_factory=dict, sa_column=_jsonb_dict())
    settings: dict[str, Any] = Field(default_factory=dict, sa_column=_jsonb_dict())
    created_at: datetime = Field(default_factory=_utcnow)


class ProjectMember(SQLModel, table=True):
    """Additional users a project is assigned to (beyond ``owner_user_id``).

    A project can be shared with several people: a non-admin may open and work
    in a project when they are its owner OR listed here. Admins see everything,
    so they never need a row. One row per (project, user).

    Phase 10: ``role`` is what the member may *do* inside the project —
    producer | lead | artist | viewer (see ``services/permissions.py``). The
    project's ``owner_user_id`` is implicitly a producer and needs no row."""

    __tablename__ = "project_member"
    __table_args__ = (UniqueConstraint("project_id", "user_id", name="uq_project_member"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="project.id", index=True)
    user_id: uuid.UUID = Field(foreign_key="app_user.id", index=True)
    role: str = "artist"
    created_at: datetime = Field(default_factory=_utcnow)


class Series(SQLModel, table=True):
    """A production line inside a Project — "Season 1", "Volume 2".

    Owns the naming convention for its children: ``unit_label`` picks whether
    they read as Episodes (animation) or Chapters (webtoon/manga), and ``code``
    is the short prefix used to build human codes like ``S1-EP007-SQ03``.
    """

    __tablename__ = "series"

    id: uuid.UUID = Field(default_factory=_uuid_pk, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="project.id", index=True)
    name: str
    code: str = ""
    unit_label: str = "Episode"   # "Episode" | "Chapter"
    # Phase 11: the Series Producer — the person who reviews this series'
    # deliverables. First link in the approver chain (see submission_service);
    # nullable, in which case review falls through to the project's PM.
    producer_user_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="app_user.id", index=True
    )
    order_index: int = 0
    settings: dict[str, Any] = Field(default_factory=dict, sa_column=_jsonb_dict())
    # Phase 10 CRM: production-tracking bag mirroring the Series_Master sheet
    # (tier, status, priority, dates, genres, logline, planned episodes…). A
    # JSONB bag rather than ~17 typed columns so the field set can track the
    # sheet without a migration each time; known keys live in series_service.
    production: dict[str, Any] = Field(default_factory=dict, sa_column=_jsonb_dict())
    created_at: datetime = Field(default_factory=_utcnow)


class Scene(SQLModel, table=True):
    """An Episode or Chapter (label comes from its parent Series)."""

    id: uuid.UUID = Field(default_factory=_uuid_pk, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="project.id", index=True)
    # Nullable so rows predating the Series tier survive; the migration
    # backfills every existing scene into its project's "Default" series.
    series_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="series.id", index=True
    )
    name: str
    # Human code within the series — "EP007", "CH012". Free-form, not unique.
    code: str = ""
    order_index: int = 0
    # Phase 11 — deliverable ownership + lifecycle. An episode is the unit that
    # gets submitted: the assignee generates its sequences in-app, edits the cut
    # OUTSIDE the app, then submits a Drive link for review.
    #   assignee_user_id   — the only person who may submit (besides admins)
    #   deliverable_status — draft | submitted | approved | paid
    #     A rejection returns the episode to `draft` (the Submission row keeps
    #     the rejection + reason), matching the deliverable state machine.
    assignee_user_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="app_user.id", index=True
    )
    deliverable_status: str = Field(default="draft", index=True)
    # Phase 10 CRM: per-episode production bag mirroring the Episode_Tracker
    # sheet (pipeline status + the four role assignees + duration/deadline…).
    # Known keys live in scene_service.EPISODE_PROD_FIELDS.
    production: dict[str, Any] = Field(default_factory=dict, sa_column=_jsonb_dict())
    # Phase 8.3: multi-shot SceneCanvas layout.
    # shot_groups[] = [{shot_id, position:{x,y}, collapsed, label, order}].
    # (Scene Bible removed — Manual mode runs no Phase 6 bible injection.)
    canvas_state: dict[str, Any] = Field(default_factory=dict, sa_column=_jsonb_dict())
    # Master establishing asset is set later (after first shot completes).
    # FK is declared at the Postgres level via the migration; we don't
    # model the relationship here because Asset has its own project_id
    # which is the canonical ownership signal.
    master_establishing_asset_id: Optional[int] = Field(default=None, foreign_key="asset.id")
    created_at: datetime = Field(default_factory=_utcnow)


class Shot(SQLModel, table=True):
    """A Sequence — the unit a single artist owns and generates on the canvas."""

    id: uuid.UUID = Field(default_factory=_uuid_pk, primary_key=True)
    scene_id: uuid.UUID = Field(foreign_key="scene.id", index=True)
    # Human code within the episode/chapter — "SQ03". Free-form, not unique.
    code: str = ""
    order_index: int = 0
    script_text: str = ""
    # idle | running | awaiting_approval | done | error
    status: str = "idle"
    current_node_id: Optional[int] = Field(default=None, foreign_key="node.id")
    final_video_asset_id: Optional[int] = Field(default=None, foreign_key="asset.id")
    workflow_metadata: dict[str, Any] = Field(default_factory=dict, sa_column=_jsonb_dict())
    # Phase 11.5: production bag, matching Series and Scene. Holds this
    # sequence's own credit ceiling (``credit_budget_usd``) — the tier the
    # generation gate checks first, since a sequence is what an artist generates
    # into. Kept as a bag so per-sequence tracking can grow without migrations.
    production: dict[str, Any] = Field(default_factory=dict, sa_column=_jsonb_dict())
    created_at: datetime = Field(default_factory=_utcnow)


# ── Per-shot workflow graph ─────────────────────────────────────────────


class Node(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    shot_id: uuid.UUID = Field(foreign_key="shot.id", index=True)
    short_id: str = Field(index=True)
    type: str
    x: float = 0.0
    y: float = 0.0
    w: float = 240.0
    h: float = 160.0
    data: dict = Field(default_factory=dict, sa_column=_jsonb_dict())
    status: str = "idle"
    created_at: datetime = Field(default_factory=_utcnow)


class Edge(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    shot_id: uuid.UUID = Field(foreign_key="shot.id", index=True)
    source_id: int = Field(foreign_key="node.id")
    target_id: int = Field(foreign_key="node.id")
    kind: str = "ref"
    # Per-edge variant pin: when the source node holds multiple variants
    # (`data.mediaIds`), this index selects WHICH variant feeds the
    # downstream as a reference. None = "fall back to the source's
    # active mediaId" (the natural single-variant case).
    source_variant_idx: Optional[int] = None


class Request(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    node_id: Optional[int] = Field(default=None, foreign_key="node.id", index=True)
    type: str
    params: dict = Field(default_factory=dict, sa_column=_jsonb_dict())
    status: str = "queued"
    result: dict = Field(default_factory=dict, sa_column=_jsonb_dict())
    error: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)
    finished_at: Optional[datetime] = None


class Asset(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    # project_id is the canonical ownership signal (Phase 1 addition).
    # Nullable for pre-binding ingest (extension can drop a media row
    # before the user wires it to a node/project).
    project_id: Optional[uuid.UUID] = Field(default=None, foreign_key="project.id", index=True)
    # node_id is optional — assets can arrive from TRPC before any node
    # binding (e.g. the user browses an old Flow project).
    node_id: Optional[int] = Field(default=None, foreign_key="node.id", index=True)
    kind: str  # image | video | thumbnail
    # Media id (the hex uuid from Google Flow). Unique so ingest can upsert.
    uuid_media_id: Optional[str] = Field(default=None, index=True, unique=True)
    # Latest captured signed GCS URL (expires — refreshed when user reopens
    # Flow tab).
    url: Optional[str] = None
    local_path: Optional[str] = None
    mime: Optional[str] = None
    asset_metadata: dict[str, Any] = Field(default_factory=dict, sa_column=_jsonb_dict())
    created_at: datetime = Field(default_factory=_utcnow)


class Reference(SQLModel, table=True):
    """User-curated saved media for cross-project reuse.

    Distinct from Asset (auto-managed cache index). Each Reference
    points at one media_id and snapshots enough metadata to spawn a
    brand-new visual_asset node in any shot without re-vision or
    re-upload. Scoped to a project so cross-project leakage doesn't
    happen.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: Optional[uuid.UUID] = Field(default=None, foreign_key="project.id", index=True)
    media_id: str = Field(index=True, unique=True)
    url: Optional[str] = None
    label: str = ""
    kind: str  # "image" | "character" | "visual_asset" | "storyboard_shot"
    ai_brief: Optional[str] = None
    aspect_ratio: Optional[str] = None
    tags: list = Field(default_factory=list, sa_column=_jsonb_list())
    pinned: bool = False
    position: int = 0
    source_shot_id: Optional[uuid.UUID] = Field(default=None, foreign_key="shot.id", index=True)
    source_node_short_id: Optional[str] = None
    # Flow Studio (/giantflow) scopes its assets by its own board instead of by
    # project, because the studio was brought over standalone and is not yet wired
    # into the Project → Series → Episode hierarchy. Keeping its images in THIS
    # table (rather than a parallel one) is what makes that later wiring a backfill
    # of ``project_id`` rather than a data migration.
    source_board_id: Optional[int] = Field(
        default=None, foreign_key="flow_board.id", index=True
    )
    created_at: datetime = Field(default_factory=_utcnow)


class FlowBoard(SQLModel, table=True):
    """A Flow Studio project — the studio's own grouping for generated images.

    Deliberately NOT ``Project``. The studio came over as a standalone surface at
    ``/giantflow``; giving it its own list keeps it out of the production
    hierarchy (and out of per-project budgets and RBAC) until that integration is
    planned. One nullable column on Reference is the entire coupling.
    """

    __tablename__ = "flow_board"  # type: ignore[assignment]

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    created_at: datetime = Field(default_factory=_utcnow, index=True)


class ChatMessage(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="project.id", index=True)
    role: str  # user | assistant | system
    content: str
    mentions: list = Field(default_factory=list, sa_column=_jsonb_list())
    created_at: datetime = Field(default_factory=_utcnow)


# ── Plan stack (kept transitionally; replaced by Phase 7 approval flow) ──


class Plan(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    shot_id: uuid.UUID = Field(foreign_key="shot.id", index=True)
    spec: dict = Field(default_factory=dict, sa_column=_jsonb_dict())
    status: str = "draft"  # draft | approved | running | done | failed
    created_at: datetime = Field(default_factory=_utcnow)


class PlanRevision(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    plan_id: int = Field(foreign_key="plan.id", index=True)
    rev_no: int
    spec: dict = Field(default_factory=dict, sa_column=_jsonb_dict())
    edits: dict = Field(default_factory=dict, sa_column=_jsonb_dict())
    created_at: datetime = Field(default_factory=_utcnow)


class PipelineRun(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    plan_id: int = Field(foreign_key="plan.id", index=True)
    status: str = "pending"  # pending | running | done | failed
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    error: Optional[str] = None


class ProjectFlowMapping(SQLModel, table=True):
    """1:1 link between a local project and a Google Flow project_id.

    Renamed from BoardFlowProject. Paygate tier is loaded realtime from
    the extension via /api/auth/me, not persisted here — the binding is
    purely about project identity.
    """
    __tablename__ = "project_flow_mapping"  # type: ignore[assignment]

    project_id: uuid.UUID = Field(primary_key=True, foreign_key="project.id")
    flow_project_id: str
    created_at: datetime = Field(default_factory=_utcnow)


# ── Multi-user (Phase 9) ─────────────────────────────────────────────────


class User(SQLModel, table=True):
    """An app account. Provisioned by an admin (no open signup). Owns
    Projects; the Avis API key + usage budgeting live server-side.

    Table is ``app_user`` because ``user`` is a reserved word in Postgres.
    """

    __tablename__ = "app_user"  # type: ignore[assignment]

    id: uuid.UUID = Field(default_factory=_uuid_pk, primary_key=True)
    username: str = Field(index=True, unique=True)
    password_hash: str
    role: str = "user"        # "admin" | "user"
    status: str = "active"    # "active" | "suspended"
    display_name: Optional[str] = None
    # Phase 9.2 budgeting (USD). budget_usd = total allocated by admin;
    # spent_usd = running total of settled actual costs. Outstanding holds
    # (reserved, not yet settled) live in UsageRecord.
    budget_usd: float = Field(default=0.0)
    spent_usd: float = Field(default=0.0)
    # Phase 0 security hardening:
    #  - token_version: bumped on suspend / password-change / "log out
    #    everywhere" → any outstanding token carrying an older tv is rejected
    #    (stateless-token revocation without a server-side session store).
    #  - failed_attempts / locked_until: login brute-force lockout.
    #  - last_login: audit + compromise detection.
    token_version: int = Field(default=0)
    failed_attempts: int = Field(default=0)
    locked_until: Optional[datetime] = None
    last_login: Optional[datetime] = None
    # Phase 1 account lifecycle:
    #  - email: optional identifier (also the link key for Google SSO in P2).
    #  - must_change_password: admin-provisioned temp passwords force a change
    #    on first login; cleared once the user sets their own.
    email: Optional[str] = Field(default=None, index=True)
    must_change_password: bool = Field(default=False)
    created_at: datetime = Field(default_factory=_utcnow)


class UsageRecord(SQLModel, table=True):
    """One metered generation. Reserved (estimate) at dispatch, then settled
    with the real Avis ``usdCost`` (or released on failure)."""

    __tablename__ = "usage_record"  # type: ignore[assignment]

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="app_user.id", index=True)
    request_id: Optional[int] = Field(default=None, index=True)
    kind: str = "video"
    model: Optional[str] = None
    estimated_usd: float = 0.0
    actual_usd: Optional[float] = None
    status: str = "reserved"  # reserved | settled | released
    created_at: datetime = Field(default_factory=_utcnow)
    settled_at: Optional[datetime] = None


class DownloadEvent(SQLModel, table=True):
    """A user actually downloaded an output — the strongest "this was used"
    signal we can get without asking them to click anything extra.

    The media route is also used for previews, so downloads are recorded by an
    explicit ping from the download button. Used by the cost/waste stats to
    confirm which generation on a node was the one that got kept."""

    __tablename__ = "download_event"  # type: ignore[assignment]

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: Optional[uuid.UUID] = Field(default=None, index=True)
    media_id: str = Field(index=True)
    node_id: Optional[int] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=_utcnow, index=True)


class AppSetting(SQLModel, table=True):
    """Tiny key/value store for admin-editable runtime settings.

    Currently holds ``avis_pool_usd`` — how much money the admin has topped up
    on the shared Avis key. Avis exposes no balance API (its docs list only
    model/chat/image/video endpoints), so the pool is entered by the admin and
    drawn down against the REAL per-generation ``usdCost`` we already record in
    UsageRecord. Generic on purpose: future settings need no new migration."""

    __tablename__ = "app_setting"  # type: ignore[assignment]

    key: str = Field(primary_key=True)
    value: str = ""
    updated_at: datetime = Field(default_factory=_utcnow)


class AuditLog(SQLModel, table=True):
    """Phase 3 — security audit trail: logins, SSO, and admin actions.

    Actor/target are denormalized to *_label strings (no FK) so entries stay
    readable and survive account deletion. Never stores secrets."""

    __tablename__ = "audit_log"  # type: ignore[assignment]

    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=_utcnow, index=True)
    action: str = Field(index=True)                # e.g. "login.success", "user.suspend"
    actor_user_id: Optional[uuid.UUID] = Field(default=None, index=True)
    actor_label: Optional[str] = None              # username/email at the time
    target_user_id: Optional[uuid.UUID] = Field(default=None, index=True)
    target_label: Optional[str] = None
    ip: Optional[str] = None
    detail: Optional[str] = None                   # short human-readable context
    # What was changed, when the subject isn't a user account: "project",
    # "series", "scene", "shot". Together with object_id this is what makes
    # "everything that ever happened to Ep03" answerable — the record the studio
    # never had while production ran on Sheets and Discord. Unset for logins.
    object_type: Optional[str] = Field(default=None, index=True)
    object_id: Optional[str] = Field(default=None, index=True)


class Registration(SQLModel, table=True):
    """A self-service signup waiting on admin approval.

    Deliberately NOT an ``app_user`` row: a pending request has no password,
    must not reserve a username, and must not be able to log in. On approval we
    mint a real User (temp password + must_change_password) and email the
    credentials; the row survives as an auditable record of the decision.

    ``email`` is indexed but NOT unique — one *pending* request per email is
    enforced in the service, so a rejected applicant can apply again later.
    """

    __tablename__ = "registration"  # type: ignore[assignment]

    id: uuid.UUID = Field(default_factory=_uuid_pk, primary_key=True)
    email: str = Field(index=True)
    display_name: Optional[str] = None
    note: Optional[str] = None                     # free text: why they want access
    status: str = Field(default="pending", index=True)  # pending | approved | rejected
    created_at: datetime = Field(default_factory=_utcnow, index=True)
    decided_at: Optional[datetime] = None
    decided_by: Optional[str] = None               # admin username at decision time
    # Stamped on approval so the admin can relay the login if the email bounced.
    created_username: Optional[str] = None


# ── Phase 11: deliverable submission + review ────────────────────────────


class Submission(SQLModel, table=True):
    """One attempt at delivering an Episode.

    The finished cut is edited outside the app, so what we store is the link to
    it (Google Drive) plus who submitted, who reviewed, and the verdict. Rows
    are append-only history: a rejected submission stays for the record and the
    next attempt gets ``version + 1``, so the whole back-and-forth is auditable
    (and the reviewer can compare against the previous cut).

    ``status``: submitted | approved | rejected. The parent Scene carries the
    current lifecycle state (draft/submitted/approved/paid) — a rejection sends
    the Scene back to ``draft`` while this row keeps the reason.
    """

    __tablename__ = "submission"  # type: ignore[assignment]

    id: uuid.UUID = Field(default_factory=_uuid_pk, primary_key=True)
    scene_id: uuid.UUID = Field(foreign_key="scene.id", index=True)
    version: int = 1
    # The delivered cut. ``drive_url`` is what the employee pasted;
    # ``drive_file_id`` is parsed out of it so the UI can embed a player.
    drive_url: str = ""
    drive_file_id: Optional[str] = None
    note: Optional[str] = None                 # employee's note to the reviewer

    submitted_by: Optional[uuid.UUID] = Field(
        default=None, foreign_key="app_user.id", index=True
    )
    submitted_at: datetime = Field(default_factory=_utcnow, index=True)

    status: str = Field(default="submitted", index=True)
    # Who the approver chain resolved to at submit time (so the inbox is stable
    # even if roles change later), and the actual decision.
    approver_user_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="app_user.id", index=True
    )
    reviewed_by: Optional[uuid.UUID] = Field(default=None, foreign_key="app_user.id")
    reviewed_at: Optional[datetime] = None
    review_note: Optional[str] = None          # required when rejecting


class CreditGrant(SQLModel, table=True):
    """A **request** for extra credit on a Project or Series, and its verdict.

    The BOD sets a base budget (``project.settings['credit_budget_usd']`` /
    ``series.production['credit_budget_usd']``); when it runs out generation is
    blocked. A PM may ask for more — with a **required reason** — but the money
    only lands once an **admin approves**: overspend is a BOD decision, not a
    PM's, so a pending request changes nothing.

    Effective budget = base + SUM(amount_usd WHERE status == "approved").

    ``status``: pending | approved | rejected.
    """

    __tablename__ = "credit_grant"  # type: ignore[assignment]

    id: uuid.UUID = Field(default_factory=_uuid_pk, primary_key=True)
    scope: str = Field(index=True)          # "project" | "series"
    scope_id: uuid.UUID = Field(index=True)  # project.id or series.id
    amount_usd: float = 0.0
    reason: str = ""                         # required by the service
    # Who asked (the PM). Kept as ``granted_by`` for continuity with the rows
    # written before the approval step existed.
    granted_by: Optional[uuid.UUID] = Field(
        default=None, foreign_key="app_user.id", index=True
    )
    created_at: datetime = Field(default_factory=_utcnow, index=True)

    status: str = Field(default="pending", index=True)
    decided_by: Optional[uuid.UUID] = Field(default=None, foreign_key="app_user.id")
    decided_at: Optional[datetime] = None
    decision_note: Optional[str] = None      # admin's note; required on reject
