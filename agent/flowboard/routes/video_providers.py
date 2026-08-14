"""Exposes the video model registry to the frontend.

The frontend reads ``GET /api/video/models`` once at app boot and caches
the capability matrix. The VideoNode settings panel uses the matrix to:

- Populate the model dropdown
- Disable / enable controls (multi-ref, last_frame, audio toggle) based
  on the selected model's capabilities
- Show a persistent banner when a user picks an i2v-only model but
  already has reference images attached

Read-only, and deliberately open to any caller the global auth gate lets
through: the response is the static model registry compiled into the build —
no project, user or credential data — and every user's canvas needs it at boot
to render the model dropdown. Returns a stable JSON shape; new capability
fields can be added without a version bump as long as they're additive.
"""
from __future__ import annotations

import os
from dataclasses import asdict

from fastapi import APIRouter

from flowboard.services.video import registry as _video_registry

router = APIRouter(prefix="/api/video", tags=["video"])


def _b2b_ui_enabled() -> bool:
    """Mirror of avis.b2b_feature_enabled — kept local so this route
    never imports the Avis adapter (that import is lazy on purpose)."""
    raw = os.getenv("FLOWBOARD_AVIS_B2B_ENABLED", "1")
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _entry_dict(entry) -> dict:
    caps = asdict(entry.capabilities)
    # Hide the B2B toggle when the process-wide kill switch is off so the UI
    # doesn't offer a path the worker will reject.
    if not _b2b_ui_enabled():
        caps["supports_b2b_unmoderated"] = False
    return {
        "model_id": entry.model_id,
        "provider": entry.provider_name,
        "display_name": entry.display_name,
        "upstream_model_id": entry.upstream_model_id,
        "capabilities": caps,
    }


@router.get("/models")
def list_models() -> dict:
    _video_registry.register_defaults()
    return {
        "default_model_id": _video_registry.get_default_model_id(),
        "models": [_entry_dict(e) for e in _video_registry.list_video_models()],
    }
