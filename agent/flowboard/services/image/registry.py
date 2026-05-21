"""Image model registry. Same shape as ``services/video/registry``."""
from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import Callable, Optional

from .base import ImageProvider, ImageProviderCapability


@dataclass(frozen=True)
class ImageModelEntry:
    model_id: str
    provider_name: str
    display_name: str
    upstream_model_id: Optional[str]
    capabilities: ImageProviderCapability
    factory: Callable[["ImageModelEntry"], ImageProvider]


_MODELS: dict[str, ImageModelEntry] = {}
_INSTANCES: dict[str, ImageProvider] = {}
_LOCK = Lock()


def register(entry: ImageModelEntry) -> None:
    _MODELS[entry.model_id] = entry
    with _LOCK:
        _INSTANCES.pop(entry.model_id, None)


def get_image_model(model_id: str) -> ImageModelEntry:
    try:
        return _MODELS[model_id]
    except KeyError as exc:
        raise KeyError(f"unknown image model: {model_id!r}") from exc


def get_image_provider(model_id: str) -> ImageProvider:
    entry = get_image_model(model_id)
    with _LOCK:
        inst = _INSTANCES.get(model_id)
        if inst is None:
            inst = entry.factory(entry)
            _INSTANCES[model_id] = inst
    return inst


def list_image_models() -> list[ImageModelEntry]:
    return list(_MODELS.values())


def is_registered(model_id: str) -> bool:
    return model_id in _MODELS


def reset_for_tests() -> None:
    _MODELS.clear()
    with _LOCK:
        _INSTANCES.clear()


_DEFAULTS_REGISTERED = False


def register_defaults() -> None:
    """Wire up Flow + Flux entries. Idempotent."""
    global _DEFAULTS_REGISTERED
    if _DEFAULTS_REGISTERED:
        return

    from .flow import FlowImageProvider, FLOW_DEFAULT_CAPABILITY
    from .flux import FluxImageProvider, FLUX_DEFAULT_CAPABILITY

    register(
        ImageModelEntry(
            model_id="flow-default-image",
            provider_name="flow",
            display_name="Google Flow (Nano Banana)",
            upstream_model_id=None,
            capabilities=FLOW_DEFAULT_CAPABILITY,
            factory=lambda entry: FlowImageProvider(entry),
        )
    )
    register(
        ImageModelEntry(
            model_id="flux-stub",
            provider_name="flux",
            display_name="Flux (not configured)",
            upstream_model_id="flux-pro-1.1",
            capabilities=FLUX_DEFAULT_CAPABILITY,
            factory=lambda entry: FluxImageProvider(entry),
        )
    )

    _DEFAULTS_REGISTERED = True
