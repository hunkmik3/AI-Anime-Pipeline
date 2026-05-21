"""Shot Pydantic schemas."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

ShotStatus = Literal["idle", "running", "awaiting_approval", "done", "error"]


class ShotCreate(BaseModel):
    order_index: Optional[int] = Field(default=None, ge=0)
    script_text: str = ""


class ShotUpdate(BaseModel):
    order_index: Optional[int] = Field(default=None, ge=0)
    script_text: Optional[str] = None
    status: Optional[ShotStatus] = None
    current_node_id: Optional[int] = None
    final_video_asset_id: Optional[int] = None
    workflow_metadata: Optional[dict[str, Any]] = None


class ShotRead(BaseModel):
    id: uuid.UUID
    scene_id: uuid.UUID
    order_index: int
    script_text: str
    status: str
    current_node_id: Optional[int] = None
    final_video_asset_id: Optional[int] = None
    workflow_metadata: dict[str, Any]
    created_at: Optional[datetime] = None
