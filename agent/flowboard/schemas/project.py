"""Project + Project Bible Pydantic schemas.

The Bible is strict-validated (``extra="forbid"``) so callers can't
silently smuggle un-modelled keys into the JSONB column. Loosen to
``allow`` only if a real forward-compat need shows up.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class ProjectBible(BaseModel):
    """Style anchor for the entire project. Injected into every prompt
    synthesis call in Phase 6.
    """

    model_config = ConfigDict(extra="forbid")

    art_style: str = ""
    color_palette: list[str] = Field(default_factory=list)
    line_style: str = ""
    lighting_conventions: str = ""
    negative_prompts: list[str] = Field(default_factory=list)
    style_anchor_asset_ids: list[int] = Field(default_factory=list)


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    project_bible: Optional[ProjectBible] = None
    settings: Optional[dict[str, Any]] = None


class ProjectUpdate(BaseModel):
    """Name + settings only. Bible goes through the dedicated bible endpoint."""

    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    settings: Optional[dict[str, Any]] = None


class ProjectRead(BaseModel):
    id: uuid.UUID
    name: str
    project_bible: dict[str, Any]
    settings: dict[str, Any]
    created_at: Optional[datetime] = None


class ProjectReadDetail(ProjectRead):
    """Detail view adds counts. Lazy-loaded by ``GET /api/projects/{id}``."""

    scene_count: int
    asset_count: int
