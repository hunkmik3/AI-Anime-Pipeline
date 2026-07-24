"""Video model registry.

A *model* is the dispatch unit — not a provider. Two Dreamina models
(``seedance-1-5-pro``, ``seedance-2-0``) live in the same provider class
but advertise different capabilities. The registry binds:

    model_id  →  (provider_name, capabilities, factory)

so the worker can look up a model and get back a fully-built provider
instance ready to ``submit()`` / ``poll()``.

Why per-model and not per-provider:

- Capability declarations differ per-model (Seedance 1.5 is i2v-only;
  2.0 is r2v-capable). Anchoring capabilities at the provider level
  would force the frontend to also know about model-internal nuance.
- Future expansion (Kling, Hailuo, ...) drops in as new entries without
  touching the worker.

The factory is lazy: providers are instantiated on first lookup and
cached, so the dev cost of "discovering" Dreamina (HTTPX client setup,
secret reading) only happens when a Dreamina model is actually used.
"""
from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import Callable, Optional

from .base import VideoProvider, VideoProviderCapability


@dataclass(frozen=True)
class VideoModelEntry:
    """One row in the model dispatch table."""

    model_id: str                           # frontend dropdown key (stable across redeploys)
    provider_name: str                      # "flow" | "dreamina"
    display_name: str                       # human label for UI
    upstream_model_id: Optional[str]        # what gets sent to the provider API; None for Flow (model is implicit in extension config)
    capabilities: VideoProviderCapability
    factory: Callable[["VideoModelEntry"], VideoProvider]


_MODELS: dict[str, VideoModelEntry] = {}
_INSTANCES: dict[str, VideoProvider] = {}
_LOCK = Lock()


def register(entry: VideoModelEntry) -> None:
    """Register a model entry. Idempotent — re-registration overwrites."""
    _MODELS[entry.model_id] = entry
    with _LOCK:
        _INSTANCES.pop(entry.model_id, None)


def get_video_model(model_id: str) -> VideoModelEntry:
    """Look up the entry for ``model_id``.

    Raises ``KeyError`` if not registered — callers (worker, Pydantic
    validators) translate to a 422 / typed error.
    """
    try:
        return _MODELS[model_id]
    except KeyError as exc:
        raise KeyError(f"unknown video model: {model_id!r}") from exc


def get_video_provider(model_id: str) -> VideoProvider:
    """Return a (cached) provider instance configured for ``model_id``."""
    entry = get_video_model(model_id)
    with _LOCK:
        inst = _INSTANCES.get(model_id)
        if inst is None:
            inst = entry.factory(entry)
            _INSTANCES[model_id] = inst
    return inst


def list_video_models() -> list[VideoModelEntry]:
    """All registered entries, in registration order."""
    return list(_MODELS.values())


def get_default_model_id() -> str:
    """Process-wide default video model.

    Resolved from ``FLOWBOARD_DEFAULT_VIDEO_MODEL`` (config.DEFAULT_VIDEO_MODEL),
    falling back to ``"flow-default"`` when unset or pointing at a model that
    isn't registered. Per-project + per-node overrides happen above this layer
    (see worker resolution chain in processor._handle_gen_video).
    """
    from flowboard.config import DEFAULT_VIDEO_MODEL

    if DEFAULT_VIDEO_MODEL and DEFAULT_VIDEO_MODEL in _MODELS:
        return DEFAULT_VIDEO_MODEL
    if DEFAULT_VIDEO_MODEL and DEFAULT_VIDEO_MODEL != "flow-default":
        import logging

        logging.getLogger(__name__).warning(
            "FLOWBOARD_DEFAULT_VIDEO_MODEL=%r is not a registered model; "
            "falling back to flow-default",
            DEFAULT_VIDEO_MODEL,
        )
    return "flow-default"


def is_registered(model_id: str) -> bool:
    return model_id in _MODELS


def reset_for_tests() -> None:
    """Wipe registry + instance cache. Used by test fixtures."""
    _MODELS.clear()
    with _LOCK:
        _INSTANCES.clear()


# ── Default registration ────────────────────────────────────────────────
#
# Registration happens lazily to avoid heavyweight side effects at import
# (Flow SDK module-level state, httpx client construction). The first
# call to register_defaults() — invoked from main.py app startup AND
# from worker module init — wires the registry.

_DEFAULTS_REGISTERED = False


def register_defaults() -> None:
    """Wire up Flow + Dreamina entries. Safe to call multiple times."""
    global _DEFAULTS_REGISTERED
    if _DEFAULTS_REGISTERED:
        return

    # Local imports break the otherwise-circular registry→provider→base→registry chain.
    from .dreamina import (
        DreaminaVideoProvider,
        SEEDANCE_1_5_PRO_CAPABILITY,
        SEEDANCE_2_0_CAPABILITY,
    )
    from .avis import (
        AvisVideoProvider,
        AVIS_SEEDANCE_2_0_CAPABILITY,
        AVIS_SEEDANCE_I2V_CAPABILITY,
    )

    register(
        VideoModelEntry(
            model_id="seedance-1-5-pro",
            provider_name="dreamina",
            display_name="Dreamina Seedance 1.5 Pro (i2v)",
            upstream_model_id="seedance-1-5-pro-251215",
            capabilities=SEEDANCE_1_5_PRO_CAPABILITY,
            factory=lambda entry: DreaminaVideoProvider(entry),
        )
    )
    register(
        VideoModelEntry(
            model_id="seedance-2-0",
            provider_name="avis",
            display_name="Seedance 2.0 (Avis · r2v)",
            upstream_model_id="dreamina-seedance-2-0",
            capabilities=AVIS_SEEDANCE_2_0_CAPABILITY,
            factory=lambda entry: AvisVideoProvider(entry),
        )
    )
    # ── Extra Avis Seedance models (added per request) ─────────────────────
    # All routed through the Avis gateway (the working key). seedance-1-5-pro
    # is re-pointed to Avis here (overwrites the direct-BytePlus entry above).
    # 4k is intentionally omitted from every capability.
    for _mid, _label, _cap in (
        ("seedance-1-5-pro", "Seedance 1.5 Pro (Avis · i2v)", AVIS_SEEDANCE_I2V_CAPABILITY),
        ("dreamina-seedance-2-0-fast", "Seedance 2.0 Fast (Avis · r2v)", AVIS_SEEDANCE_2_0_CAPABILITY),
        ("dreamina-seedance-2-0-mini", "Seedance 2.0 Mini (Avis · r2v)", AVIS_SEEDANCE_2_0_CAPABILITY),
        ("seedance-1-0-pro", "Seedance 1.0 Pro (Avis · i2v)", AVIS_SEEDANCE_I2V_CAPABILITY),
        ("seedance-1-0-pro-fast", "Seedance 1.0 Pro Fast (Avis · i2v)", AVIS_SEEDANCE_I2V_CAPABILITY),
    ):
        register(
            VideoModelEntry(
                model_id=_mid,
                provider_name="avis",
                display_name=_label,
                upstream_model_id=_mid,
                capabilities=_cap,
                factory=lambda entry: AvisVideoProvider(entry),
            )
        )

    _DEFAULTS_REGISTERED = True
