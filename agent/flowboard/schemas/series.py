"""Series Pydantic schemas (Phase 10).

A Series is the production line inside a Project — "Season 1", "Volume 2" —
and decides whether its children read as Episodes or Chapters.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

UnitLabel = Literal["Episode", "Chapter"]


class SeriesCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    # Short prefix for child codes — "S1" → "S1-EP007".
    code: str = Field(default="", max_length=32)
    unit_label: UnitLabel = "Episode"
    order_index: Optional[int] = Field(default=None, ge=0)
    # Phase 10 CRM: Series_Master production metadata (tier, status, dates,
    # genres, logline…). Unknown keys are dropped in the service.
    production: Optional[dict[str, Any]] = None


class SeriesUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    code: Optional[str] = Field(default=None, max_length=32)
    unit_label: Optional[UnitLabel] = None
    order_index: Optional[int] = Field(default=None, ge=0)
    # Patch merged over the existing bag; "" / null clears a key.
    production: Optional[dict[str, Any]] = None
    # View-only archive lock. True freezes the series (read-only), False lifts it.
    frozen: Optional[bool] = None


class SeriesRead(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    code: str
    unit_label: str
    order_index: int
    frozen: bool = False
    production: dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[datetime] = None


class SeriesReadDetail(SeriesRead):
    episode_count: int
