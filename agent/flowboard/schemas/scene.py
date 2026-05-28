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
    order_index: Optional[int] = Field(default=None, ge=0)


class SceneUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    order_index: Optional[int] = Field(default=None, ge=0)


class SceneRead(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    order_index: int
    canvas_state: dict[str, Any] = Field(default_factory=dict)
    master_establishing_asset_id: Optional[int] = None
    created_at: Optional[datetime] = None


class SceneReadDetail(SceneRead):
    shot_count: int
