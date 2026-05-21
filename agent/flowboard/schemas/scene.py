"""Scene + Scene Bible Pydantic schemas."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class SceneBible(BaseModel):
    """Spatial / lighting anchor for a single scene. Injected after the
    project bible in Phase 6 prompt assembly.
    """

    model_config = ConfigDict(extra="forbid")

    scene_bible_text: str = ""
    # Master establishing asset is an Asset.id (int PK in Phase 1). The
    # route validates the FK belongs to the scene's parent project.
    master_establishing_asset_id: Optional[int] = None


class SceneCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    order_index: Optional[int] = Field(default=None, ge=0)
    scene_bible_text: str = ""


class SceneUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    order_index: Optional[int] = Field(default=None, ge=0)
    scene_bible_text: Optional[str] = None


class SceneRead(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    order_index: int
    scene_bible_text: str
    master_establishing_asset_id: Optional[int] = None
    created_at: Optional[datetime] = None


class SceneReadDetail(SceneRead):
    shot_count: int
