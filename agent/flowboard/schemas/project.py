"""Project + Project Bible Pydantic schemas.

The Bible is strict-validated (``extra="forbid"``) so callers can't
silently smuggle un-modelled keys into the JSONB column. Loosen to
``allow`` only if a real forward-compat need shows up.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


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


def _validate_project_settings(settings: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Reject unknown ``default_video_model`` values.

    Per Phase 5 decision (B): if the user pins a project to a model that
    isn't registered (typo, removed in a downgrade), surface a 422 with
    the available models instead of silently letting the worker fall
    back. Validator imports the registry lazily so model files don't
    pull in HTTPX/boto3 at schema-module load.
    """
    if not settings:
        return settings
    model_id = settings.get("default_video_model")
    if model_id is None:
        return settings
    if not isinstance(model_id, str) or not model_id:
        raise ValueError("settings.default_video_model must be a non-empty string")
    # Lazy import + register_defaults to avoid cyclic init at app boot.
    from flowboard.services.video import registry as _video_registry
    _video_registry.register_defaults()
    if not _video_registry.is_registered(model_id):
        known = ", ".join(e.model_id for e in _video_registry.list_video_models())
        raise ValueError(
            f"settings.default_video_model={model_id!r} is not a registered "
            f"video model. Known: {known}"
        )
    return settings


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    project_bible: Optional[ProjectBible] = None
    settings: Optional[dict[str, Any]] = None

    @field_validator("settings")
    @classmethod
    def _check_settings(cls, v: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        return _validate_project_settings(v)


class ProjectUpdate(BaseModel):
    """Name + settings only. Bible goes through the dedicated bible endpoint."""

    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    settings: Optional[dict[str, Any]] = None

    @field_validator("settings")
    @classmethod
    def _check_settings(cls, v: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        return _validate_project_settings(v)


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
