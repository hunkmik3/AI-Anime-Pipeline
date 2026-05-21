"""Exposes the video model registry to the frontend.

The frontend reads ``GET /api/video/models`` once at app boot and caches
the capability matrix. The VideoNode settings panel uses the matrix to:

- Populate the model dropdown
- Disable / enable controls (multi-ref, last_frame, audio toggle) based
  on the selected model's capabilities
- Show a persistent banner when a user picks an i2v-only model but
  already has reference images attached

Read-only. No auth — Flowboard is single-user / localhost-only at this
phase. Returns a stable JSON shape; new capability fields can be added
without a version bump as long as they're additive.
"""
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter

from flowboard.services.video import registry as _video_registry

router = APIRouter(prefix="/api/video", tags=["video"])


def _entry_dict(entry) -> dict:
    return {
        "model_id": entry.model_id,
        "provider": entry.provider_name,
        "display_name": entry.display_name,
        "upstream_model_id": entry.upstream_model_id,
        "capabilities": asdict(entry.capabilities),
    }


@router.get("/models")
def list_models() -> dict:
    _video_registry.register_defaults()
    return {
        "default_model_id": _video_registry.get_default_model_id(),
        "models": [_entry_dict(e) for e in _video_registry.list_video_models()],
    }
