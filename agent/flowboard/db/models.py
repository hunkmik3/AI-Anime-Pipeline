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

from sqlalchemy import JSON, text
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


# ── Hierarchy: Project → Scene → Shot ────────────────────────────────────


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


class Scene(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=_uuid_pk, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="project.id", index=True)
    name: str
    order_index: int = 0
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
    id: uuid.UUID = Field(default_factory=_uuid_pk, primary_key=True)
    scene_id: uuid.UUID = Field(foreign_key="scene.id", index=True)
    order_index: int = 0
    script_text: str = ""
    # idle | running | awaiting_approval | done | error
    status: str = "idle"
    current_node_id: Optional[int] = Field(default=None, foreign_key="node.id")
    final_video_asset_id: Optional[int] = Field(default=None, foreign_key="asset.id")
    workflow_metadata: dict[str, Any] = Field(default_factory=dict, sa_column=_jsonb_dict())
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
    created_at: datetime = Field(default_factory=_utcnow)


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
