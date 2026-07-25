"""Scene Pydantic schemas.

Phase 8.3: Scene Bible (``scene_bible_text``) removed — Manual mode runs no
Phase 6 bible injection. ``canvas_state`` (multi-shot SceneCanvas layout) is
read-only here; mutated via the canvas / group / auto-migrate endpoints.
``master_establishing_asset_id`` is unrelated to the bible and kept.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class SceneEstablishing(BaseModel):
    """Scene's master/establishing-shot asset pointer (was bundled with the
    now-removed Scene Bible; kept for the MasterShot reference flow)."""

    model_config = ConfigDict(extra="forbid")

    # Master establishing asset is an Asset.id (int PK in Phase 1). The
    # route validates the FK belongs to the scene's parent project.
    master_establishing_asset_id: Optional[int] = None


class SceneCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    # Phase 10: which Series this Episode/Chapter belongs to. Omitted → the
    # project's first (or an auto-created "Default") series, so the pre-Series
    # flat API keeps working.
    series_id: Optional[uuid.UUID] = None
    code: str = Field(default="", max_length=32)
    order_index: Optional[int] = Field(default=None, ge=0)


class SceneUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    series_id: Optional[uuid.UUID] = None
    code: Optional[str] = Field(default=None, max_length=32)
    order_index: Optional[int] = Field(default=None, ge=0)
    # Phase 10 CRM: Episode_Tracker production metadata (pipeline status, the
    # four role assignees, duration/deadline…). Patch merged over the bag.
    production: Optional[dict[str, Any]] = None


class SceneRead(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    series_id: Optional[uuid.UUID] = None
    name: str
    code: str = ""
    order_index: int
    production: dict[str, Any] = Field(default_factory=dict)
    canvas_state: dict[str, Any] = Field(default_factory=dict)
    master_establishing_asset_id: Optional[int] = None
    created_at: Optional[datetime] = None


class SceneReadDetail(SceneRead):
    shot_count: int
