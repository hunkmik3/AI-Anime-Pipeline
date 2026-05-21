"""Black Forest Labs Flux ``ImageProvider`` stub.

Slot reserved in the registry so future activation is a single-class
swap. Submit raises ``NotImplementedError`` until the integration is
written — keeping the stub explicit (rather than silently returning
nothing) means UI selection of this model surfaces a clear error
instead of a mysterious empty result.
"""
from __future__ import annotations

from .base import (
    ImageError,
    ImageGenParams,
    ImageGenResult,
    ImageProviderCapability,
)


FLUX_DEFAULT_CAPABILITY = ImageProviderCapability(
    supports_reference_images=True,
    supports_variants=False,
    max_variants=1,
    max_refs=4,
    aspect_ratios=("1:1", "16:9", "9:16", "4:3", "3:4"),
)


class FluxImageProvider:
    name = "flux"

    def __init__(self, entry) -> None:
        self.capabilities = entry.capabilities

    async def is_available(self) -> bool:
        # Always false until the integration ships — the UI hides
        # unavailable providers behind a "not configured" pill.
        return False

    async def submit(self, params: ImageGenParams) -> ImageGenResult:
        raise NotImplementedError(
            "Flux provider is not implemented yet. "
            "Track in docs/flowboard_modification_plan.md Phase 5.8 backlog."
        )
