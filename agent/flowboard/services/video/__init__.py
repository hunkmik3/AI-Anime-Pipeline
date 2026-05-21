"""Video generation provider abstraction.

Public surface:
- ``VideoProvider`` Protocol (base.py)
- ``VideoModelEntry`` registry rows (registry.py)
- ``get_video_model``, ``list_video_models``, ``get_default_model_id``
- ``FlowVideoProvider``, ``DreaminaVideoProvider``

Worker code imports through the registry, not the concrete providers.
"""
from __future__ import annotations

from .base import (
    VideoError,
    VideoErrorCode,
    VideoGenPollResult,
    VideoGenSubmitParams,
    VideoGenSubmitResult,
    VideoProvider,
    VideoProviderCapability,
)
from .registry import (
    VideoModelEntry,
    get_default_model_id,
    get_video_model,
    get_video_provider,
    list_video_models,
)

__all__ = [
    "VideoError",
    "VideoErrorCode",
    "VideoGenPollResult",
    "VideoGenSubmitParams",
    "VideoGenSubmitResult",
    "VideoModelEntry",
    "VideoProvider",
    "VideoProviderCapability",
    "get_default_model_id",
    "get_video_model",
    "get_video_provider",
    "list_video_models",
]
