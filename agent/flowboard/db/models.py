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
from datetime import date, datetime, timezone
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
    is the short prefix used to build human codes: ``S1_EP07``, ``S1_EP07_SQ03``.
    Underscores, matching the Episode_Tracker sheet these mirror — a dashed
    variant was written here once and nothing ever produced one.
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
    # The person who BUILDS this series — every episode under it is theirs to work
    # in and to hand in. Separate from `producer_user_id` above on purpose: that
    # one is the reviewer, so putting the artist there would make them the approver
    # of their own submissions. One field cannot be both ends of a handover.
    #
    # Per-episode assignment (`Scene.assignee_user_id`) still stands and is
    # narrower; this is for the ordinary case where one person takes a whole
    # series and a PM does not want to assign twelve episodes one at a time.
    assignee_user_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="app_user.id", index=True
    )
    # draft | submitted | approved | paid. The series is the unit that is handed
    # in: one finished cut for the whole thing, reviewed once. A rejection returns
    # it to `draft` and the Submission row keeps the reason.
    deliverable_status: str = Field(default="draft", index=True)
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
    #: Every episode lives in a series. NOT NULL, not by convention: it was
    #: nullable to let rows predating the Series tier survive their migration,
    #: and although `create_scene` has always fallen back to the project's
    #: "Default" series so nothing in the app produced an orphan, the column
    #: still permitted one. A four-tier hierarchy that the database does not
    #: enforce is a four-tier hierarchy exactly until someone writes a third
    #: creation path.
    series_id: uuid.UUID = Field(foreign_key="series.id", index=True)
    name: str
    # Human code within the series — "EP007", "CH012". Free-form, not unique.
    code: str = ""
    order_index: int = 0
    # Who works in this episode. Narrower than the series assignee and beats it:
    # that is how a series is split when one person is overloaded.
    assignee_user_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="app_user.id", index=True
    )
    # Kept for history and no longer the truth. The unit that gets DELIVERED is
    # the series — one cut handed in for the whole thing, reviewed once — so
    # `Series.deliverable_status` is what the app reads and writes. An episode's
    # delivery state is its series' state; reading it from here would report
    # "draft" for episodes inside an approved series.
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


class SceneCollaborator(SQLModel, table=True):
    """Someone helping on an episode they do not own.

    An episode has one owner — ``Scene.assignee_user_id`` — and that stays true:
    one person is accountable for the cut and is the only one who may hand it in.
    But a chapter split between three panel artists arrives as one episode, and
    "one person animates all of it" stops being realistic the moment that
    episode is large. So: one owner, plus whoever is added when they are
    overloaded.

    A row here grants exactly what the owner has *inside* the episode — see it,
    add and edit its sequences, work its canvas — and nothing outside it. It does
    not grant submitting: a deliverable with two people able to hand it in is a
    deliverable nobody is accountable for.
    """

    __tablename__ = "scene_collaborator"
    __table_args__ = (
        UniqueConstraint("scene_id", "user_id", name="uq_scene_collaborator"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    scene_id: uuid.UUID = Field(foreign_key="scene.id", index=True)
    user_id: uuid.UUID = Field(foreign_key="app_user.id", index=True)
    #: Who added them, for the same reason every other assignment records it.
    added_by: Optional[uuid.UUID] = Field(default=None, foreign_key="app_user.id")
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
    #: How many generations this sequence may run in total. NULL means the house
    #: default; a number is an unlock a PM granted after looking at why the first
    #: attempts did not work.
    #:
    #: A ceiling per SEQUENCE, not per person. A budget stops someone spending
    #: and does nothing about one shot quietly eating a season's worth of tries;
    #: five is roughly "the obvious things have been tried", and past that the
    #: answer is usually a different prompt or a different reference rather than
    #: another roll of the same one.
    gen_limit: Optional[int] = Field(default=None)
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
    #: What this run was for, on the Giant Studio side. A node reaches its
    #: project through shot → scene, which is how every cost rollup attributes
    #: spend.
    node_id: Optional[int] = Field(default=None, foreign_key="node.id", index=True)
    #: …and on the Giantflow side. Panels are not on the node tree, so a panel
    #: generation had no way to be attributed at all: the panel id was written
    #: into ``params["__panel_id"]``, which is true and unjoinable, so every one
    #: of those runs showed up in the ledger as spend belonging to nobody.
    #:
    #: Exactly one of the two is set. Not a generic (subject_type, subject_id)
    #: pair: the two hierarchies are genuinely different tables, and an untyped
    #: pair cannot be joined, cannot be constrained, and would let a row name a
    #: panel that does not exist.
    flow_panel_id: Optional[int] = Field(
        default=None, foreign_key="flow_panel.id", index=True
    )
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
    # Which image model actually produced this (e.g. "gemini-3.1-flash-image",
    # "dola-seedream-5-0-pro") — the RESOLVED value, not necessarily what the
    # caller asked for, since the backend can substitute a fallback. None for
    # uploads and for rows that predate the column.
    model_used: Optional[str] = None
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
    #: Whose board this is. The studio shipped as one shared list with no owner
    #: column, which meant there was nothing to authorise against — any signed-in
    #: account could open, rename and delete anyone's board, and the docstring
    #: above said so plainly rather than pretending otherwise.
    #:
    #: Nullable, and NULL means nobody's: a board from before this column existed
    #: has no owner to name, and inventing one would be a guess written into the
    #: database. Those are visible to admins only, which is the safe reading of
    #: "we do not know whose this is".
    owner_user_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="app_user.id", index=True
    )
    created_at: datetime = Field(default_factory=_utcnow, index=True)


# ── Giantflow: panel production ─────────────────────────────────────────────
#
# The studio adapts comics panel by panel: someone cuts the original pages into a
# folder of panels ("raw material"), an artist restyles each one with AI, a PM
# reviews it and sends it back with notes until it passes. That review used to
# live on a Miro board — one row per panel, one column per stage — and these
# tables are that board, with generation attached instead of alongside.
#
# The PANEL is the unit of work, not the project: it is what gets assigned, what
# carries a status, what a note is about, and what gets exported.


class FlowProject(SQLModel, table=True):
    """The studio's slate — the container every comic hangs off.

    The top of four tiers: Project → Series → Batch → Panel. It holds a name and
    a cover and nothing else; all the work happens below it.

    Separate from ``Project`` on purpose: giantflow is a standalone surface, so a
    role here grants nothing in the production hierarchy and vice versa.
    """

    __tablename__ = "flow_project"  # type: ignore[assignment]

    id: Optional[int] = Field(default=None, primary_key=True)
    #: Highest comic number ever issued on this slate — a high-water mark, not a
    #: count. Reading the highest number IN USE would hand a deleted comic's
    #: number to the next one, and that number is in exported folder names and
    #: in what people say to each other; two comics sharing it is two people
    #: certain they are discussing the same thing.
    last_series_seq: Optional[int] = Field(default=None)
    name: str
    cover_media_id: Optional[str] = None
    #: The production project this slate hands over into. Every comic created here
    #: gets its counterpart under it, so the two sides carry the same shape without
    #: anyone wiring them up comic by comic.
    #:
    #: Stored rather than matched by name: names get edited, and a rename would
    #: otherwise silently start a second project beside the first.
    studio_project_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="project.id", index=True
    )
    order_index: int = Field(default=0, index=True)
    created_at: datetime = Field(default_factory=_utcnow)


class FlowSeries(SQLModel, table=True):
    """One comic being adapted — what the UI used to call a Project.

    Renamed rather than left alone: this row has always been a comic, and a table
    called `project` under a screen labelled "Series" is the drift that costs an
    afternoon later.
    """

    __tablename__ = "flow_series"  # type: ignore[assignment]

    id: Optional[int] = Field(default=None, primary_key=True)
    #: The slate it belongs to.
    project_id: int = Field(foreign_key="flow_project.id", index=True)
    #: The production Series this comic delivers into, once someone links them.
    #:
    #: The whole handover hangs off this one column. giantflow adapts panels;
    #: giantstudio animates them; "one finished panel is one sequence" is the
    #: studio's own rule, and it makes every other pairing follow — a chapter is
    #: an episode, a panel is a sequence — so nothing else has to be paired by
    #: hand. NULL means this comic does not hand over, which is the right answer
    #: for one being adapted for print.
    studio_series_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="series.id", index=True
    )
    name: str
    #: Hand-picked cover. When unset the card falls back to the first panel of the
    #: first batch, so a project looks like itself without anyone uploading
    #: anything — the same rule the episode cards use.
    cover_media_id: Optional[str] = None
    #: Where the tile sits in the grid. Hand-arranged, because "which show is
    #: active right now" is not something a creation date knows.
    order_index: int = Field(default=0, index=True)
    created_by: Optional[uuid.UUID] = Field(
        default=None, foreign_key="app_user.id", index=True
    )
    #: A deadline is a DAY. Storing a timestamp would make "due today" depend on
    #: which timezone the server happens to run in.
    due_date: Optional[date] = None
    created_at: datetime = Field(default_factory=_utcnow, index=True)


class FlowSeriesMember(SQLModel, table=True):
    """Who works on a giantflow SERIES, and as what.

    On the series, not the project above it: one role for the studio's whole
    slate is the opposite of what per-comic roles are for — a PM on X-MEN is not
    automatically a PM on MAGMEL.

    Roles reuse ``services/permissions.py``'s ranking (viewer < artist < lead <
    producer) — a PM is a producer — but membership is stored HERE rather than on
    ``project_member`` so the two systems stay sealed off from each other.
    """

    __tablename__ = "flow_series_member"  # type: ignore[assignment]
    __table_args__ = (
        UniqueConstraint("series_id", "user_id", name="uq_flow_project_member"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    series_id: int = Field(foreign_key="flow_series.id", index=True)
    user_id: uuid.UUID = Field(foreign_key="app_user.id", index=True)
    role: str = "artist"
    created_at: datetime = Field(default_factory=_utcnow)


class FlowChapter(SQLModel, table=True):
    """One instalment of a comic.

    The tier the work is actually divided on: a chapter arrives, gets split among
    artists, and ships. Batches hang off this rather than off the series, because
    "artist X takes panels 1-45" is a statement about a chapter, not about the
    whole comic.
    """

    __tablename__ = "flow_chapter"  # type: ignore[assignment]

    id: Optional[int] = Field(default=None, primary_key=True)
    series_id: int = Field(foreign_key="flow_series.id", index=True)
    #: The Episode this chapter became, created the first time one of its
    #: panels is approved. Derived on demand rather than paired up front: a
    #: chapter nobody has finished a panel in does not need an episode yet, and
    #: making one anyway fills the production board with empty rows.
    studio_scene_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="scene.id", index=True
    )
    name: str
    cover_media_id: Optional[str] = None
    order_index: int = Field(default=0, index=True)
    due_date: Optional[date] = None
    created_by: Optional[uuid.UUID] = Field(
        default=None, foreign_key="app_user.id", index=True
    )
    created_at: datetime = Field(default_factory=_utcnow)


class FlowBatch(SQLModel, table=True):
    """A work package inside a comic — one artist's share of it.

    The PM creates a batch, hands it to an artist, and uploads THAT artist's
    folder of panels into it. So the batch, not the project, owns an import: the
    material arrives already divided by who is doing it, which is how the studio
    actually hands work out. There is no range-splitting step because there is
    never one big pile to split.

    The assignee lives here and nowhere else. Keeping a copy on each panel too
    would be two sources of truth for one fact, and they drift.
    """

    __tablename__ = "flow_batch"  # type: ignore[assignment]

    id: Optional[int] = Field(default=None, primary_key=True)
    chapter_id: int = Field(foreign_key="flow_chapter.id", index=True)
    name: str
    assignee_user_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="app_user.id", index=True
    )
    order_index: int = Field(default=0, index=True)
    created_at: datetime = Field(default_factory=_utcnow, index=True)


#: Panel lifecycle. ``approved`` is terminal for GENERATION — the app refuses new
#: versions for an approved panel — but not irreversible: a PM can reopen it to
#: ``changes_requested``, because one mis-click should not destroy the work.
PANEL_STATUSES = (
    "todo",
    "in_progress",
    "submitted",
    "changes_requested",
    "approved",
)


class FlowPanel(SQLModel, table=True):
    """One panel of the comic — the unit of work.

    ``order_index`` comes from the cutter's filename order and is never
    re-derived: they sorted the folder deliberately, so the app preserves what it
    was given rather than trying to be clever about reading order.
    """

    __tablename__ = "flow_panel"  # type: ignore[assignment]
    __table_args__ = (
        UniqueConstraint("batch_id", "code", name="uq_flow_panel_code"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    #: The batch that owns it. Who works on this panel comes from the batch —
    #: the panel deliberately does not carry its own assignee.
    batch_id: int = Field(foreign_key="flow_batch.id", index=True)
    #: The cutter's own name for it ("PANEL006") — shown as-is so it matches
    #: their sheet and the Miro history it replaces.
    code: str = Field(index=True)
    order_index: int = Field(default=0, index=True)
    status: str = Field(default="todo", index=True)
    #: The version being delivered. Set by submitting, because submitting IS
    #: choosing — an artist who made ten tries and preferred the seventh had no
    #: way to say so before, and every surface fell back to the most recent one.
    #: NULL means nobody has picked yet; readers fall back to the latest version.
    final_media_id: Optional[str] = Field(default=None)
    #: The Sequence this panel became. Recorded so approving twice — which a
    #: reopen-and-re-approve does — hands over once. Without it each extra
    #: verdict would add another sequence to the episode, and nothing would look
    #: wrong enough to notice until someone counted.
    studio_shot_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="shot.id", index=True
    )
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class FlowPanelImage(SQLModel, table=True):
    """A picture belonging to a panel — either its raw material or a result.

    Both live in one table because they are the same kind of thing to the UI (a
    media id to show) and the pairing is the point: every PM note compares the
    result against the original. ``role`` keeps them apart, and a panel legitimately
    has SEVERAL of each — one Miro row carried three raw pieces, and each review
    round adds another generated version.
    """

    __tablename__ = "flow_panel_image"  # type: ignore[assignment]

    id: Optional[int] = Field(default=None, primary_key=True)
    panel_id: int = Field(foreign_key="flow_panel.id", index=True)
    role: str = Field(index=True)  # "raw" | "generated"
    #: Ordinal within its role: raw pieces in import order, generated versions
    #: 1, 2, 3… so "v3 was sent back" is sayable.
    version: int = Field(default=1)
    media_id: str = Field(index=True)
    #: Which model produced it (generated only) — the panel imposes nothing, the
    #: artist picks per generation, so this is a record rather than a setting.
    model_used: Optional[str] = None
    created_by: Optional[uuid.UUID] = Field(
        default=None, foreign_key="app_user.id", index=True
    )
    created_at: datetime = Field(default_factory=_utcnow, index=True)


class FlowPanelEvent(SQLModel, table=True):
    """One thing that happened to a panel, append-only.

    Versions carry timestamps and notes carry authors, but neither records the
    handover: who submitted which version, who ruled on it, when it came back.
    Deriving that afterwards is guesswork — "there are three versions and the
    panel is approved" does not say which version was approved.
    """

    __tablename__ = "flow_panel_event"  # type: ignore[assignment]

    id: Optional[int] = Field(default=None, primary_key=True)
    panel_id: int = Field(foreign_key="flow_panel.id", index=True)
    #: submitted | approved | changes_requested | reopened | version_added
    kind: str
    actor_user_id: Optional[uuid.UUID] = Field(default=None, foreign_key="app_user.id")
    #: The version this is about, when it is about one.
    media_id: Optional[str] = None
    #: The reason, for a send-back.
    body: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow, index=True)


class FlowPanelNote(SQLModel, table=True):
    """One PM remark on a panel, and whether it has been dealt with.

    Attached to the PANEL, not to a version — matching how the remarks actually
    read. "Sai nơ áo" is true of the panel and stays true across re-generations
    until someone fixes it; hanging it off a version would orphan it on the next
    attempt. ``resolved`` is the Miro board's "Fixed".
    """

    __tablename__ = "flow_panel_note"  # type: ignore[assignment]

    id: Optional[int] = Field(default=None, primary_key=True)
    panel_id: int = Field(foreign_key="flow_panel.id", index=True)
    body: str
    author_user_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="app_user.id", index=True
    )
    resolved: bool = Field(default=False, index=True)
    resolved_by: Optional[uuid.UUID] = Field(default=None, foreign_key="app_user.id")
    resolved_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=_utcnow, index=True)


class FlowNoticeRead(SQLModel, table=True):
    """How far one account has read its notifications. One row per user.

    The whole of the notifications feature that has to be stored. Everything on
    that tab — what is waiting, what came back, what was approved — is derived
    from panels and events at read time, because a stored copy would need a write
    at every event site and would go stale the first time someone added a sixth
    one. What genuinely cannot be derived is whether *you* have already looked,
    so that, and only that, is a table.

    A single watermark rather than a row per unread item: the count answers "is
    anything new since I last looked", which is one fact, and per-item read state
    would be a second bookkeeping problem for a feed that already disappears on
    its own when the work is done.
    """

    __tablename__ = "flow_notice_read"  # type: ignore[assignment]

    user_id: uuid.UUID = Field(foreign_key="app_user.id", primary_key=True)
    seen_at: datetime = Field(default_factory=_utcnow)


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


class EditNote(SQLModel, table=True):
    """One note the editor left on a frame of their cut.

    SyncSketch in one row: where in the cut, which sequence it is really about,
    what was said, and a drawing over the frame.

    ``shot_id`` is the point of the whole feature and it is CHOSEN, not computed.
    The cut is assembled outside the app — trimmed, reordered, shots dropped — so
    a timecode cannot be resolved back to a sequence by arithmetic without an EDL
    the app does not have. The editor is already paused on the frame; picking the
    clip they are looking at is one click and is always right, where a guess is
    silently wrong the first time somebody trims a shot.

    ``resolved`` is what stops the second round starting from nothing: without it
    the editor has to remember which of six notes were acted on.
    """

    __tablename__ = "edit_note"

    id: Optional[int] = Field(default=None, primary_key=True)
    submission_id: uuid.UUID = Field(foreign_key="submission.id", index=True)
    #: The sequence this is about, as the editor identified it.
    shot_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="shot.id", index=True
    )
    #: Seconds into the editor's cut. Kept as float — a note lands on a frame, and
    #: 02:47.12 rounded to the second points at the wrong one at 24fps.
    at_seconds: float = 0.0
    body: str = ""
    #: The drawing over the frame, as a media id. Optional: plenty of notes are
    #: just a sentence, and forcing a canvas export for those costs a round trip.
    drawing_media_id: Optional[str] = None
    resolved: bool = Field(default=False, index=True)
    resolved_by: Optional[uuid.UUID] = Field(
        default=None, foreign_key="app_user.id"
    )
    resolved_at: Optional[datetime] = None
    author_user_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="app_user.id", index=True
    )
    created_at: datetime = Field(default_factory=_utcnow, index=True)


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
    """One attempt at delivering a Series.

    The finished cut is edited outside the app, so what we store is the link to
    it (Google Drive) plus who submitted, who reviewed, and the verdict. Rows
    are append-only history: a rejected submission stays for the record and the
    next attempt gets ``version + 1``, so the whole back-and-forth is auditable
    (and the reviewer can compare against the previous cut).

    ``status``: submitted | approved | rejected. The parent Series carries the
    current lifecycle state (draft/submitted/approved/paid) — a rejection sends
    the Series back to ``draft`` while this row keeps the reason.

    Delivery used to be per EPISODE. It is per series because that is how the
    work is handed over: one person takes a series, and a PM reviews the finished
    thing once rather than signing off twelve times. ``scene_id`` stays nullable
    so rows written under the old rule keep pointing at what they described.
    """

    __tablename__ = "submission"  # type: ignore[assignment]

    id: uuid.UUID = Field(default_factory=_uuid_pk, primary_key=True)
    #: Which hand-over this is. ``cut`` is the artist handing the generated work
    #: over; ``edit`` is the editor handing the assembled episode back. Two rows
    #: on the same series that mean different things, so they cannot share a
    #: version sequence or a queue — an artist's v2 and an editor's v2 are not the
    #: same round of anything.
    kind: str = Field(default="cut", index=True)
    series_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="series.id", index=True
    )
    scene_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="scene.id", index=True
    )
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
