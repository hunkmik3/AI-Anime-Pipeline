"""Still-image generation provider abstraction.

Mirror of ``services/video`` — same Protocol shape, model-keyed
registry, capability declarations. The existing Flow gen_image /
edit_image worker code is wrapped behind a thin provider so future
backends (Flux, MidJourney via API, etc.) can drop in without
rewriting the worker.

Phase 5 ships:
- ``FlowImageProvider`` — production-ready, wraps the existing
  ``_handle_gen_image`` / ``_handle_edit_image`` flow
- ``FluxImageProvider`` — stub that raises NotImplementedError on
  ``submit``; included so the registry already has the slot and
  later activation is a single-class change

Worker integration is deferred (Phase 5 scope is the *video* refactor;
image is a skeleton). When image worker code is migrated in a later
phase, it follows the same dispatcher pattern as the video one.
"""
from __future__ import annotations

from .base import (
    ImageError,
    ImageErrorCode,
    ImageGenParams,
    ImageGenResult,
    ImageProvider,
    ImageProviderCapability,
)
from .registry import (
    ImageModelEntry,
    get_image_model,
    get_image_provider,
    list_image_models,
    register_defaults as register_image_defaults,
)

__all__ = [
    "ImageError",
    "ImageErrorCode",
    "ImageGenParams",
    "ImageGenResult",
    "ImageModelEntry",
    "ImageProvider",
    "ImageProviderCapability",
    "get_image_model",
    "get_image_provider",
    "list_image_models",
    "register_image_defaults",
]
