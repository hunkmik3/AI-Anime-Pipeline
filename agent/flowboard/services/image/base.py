"""Protocol + shared types for the still image provider layer.

Mirrors ``services/video/base.py`` — same uniform error vocab, same
capability declaration pattern. Image-specific quirks:

- Multiple variants per request (Flow ships 1-4)
- Reference images as ``IMAGE_INPUT_TYPE_REFERENCE`` array (Flow), or
  whatever the provider's native shape is
- Result is a list of mediaIds (not a single one) because variant
  count > 1 is the common case
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional, Protocol, TypedDict, runtime_checkable


ImageErrorCode = Literal[
    "content_filtered",
    "auth",
    "quota",
    "bad_input",
    "timeout",
    "internal",
]


class ImageError(RuntimeError):
    def __init__(
        self,
        code: ImageErrorCode,
        message: str,
        *,
        raw: Optional[dict] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.raw = raw or {}


@dataclass(frozen=True)
class ImageProviderCapability:
    supports_reference_images: bool
    supports_variants: bool
    max_variants: int
    max_refs: int
    aspect_ratios: tuple[str, ...]


class ImageGenParams(TypedDict, total=False):
    prompt: str
    reference_image_urls: list[str]
    variant_count: int
    aspect_ratio: str
    # Flow-specific passthrough — other providers ignore.
    project_id: str
    paygate_tier: str
    image_model: Optional[str]


class ImageGenResult(TypedDict, total=False):
    status: Literal["succeeded", "failed"]
    media_ids: list[str]
    error: Optional[ImageErrorCode]
    error_message: Optional[str]
    cost_usd: float
    raw: dict


@runtime_checkable
class ImageProvider(Protocol):
    name: str
    capabilities: ImageProviderCapability

    async def submit(self, params: ImageGenParams) -> ImageGenResult:
        """One-shot: Flow's gen_image is fully synchronous on the SDK
        side (variants ship inline on the dispatch response), and the
        Dreamina image counterpart (if/when added) follows the same
        task → poll lifecycle as the video provider does. The Protocol
        keeps a single ``submit`` for the synchronous case; an async
        variant can be added under a different method name when the
        first real polling provider lands.
        """
        ...

    async def is_available(self) -> bool:
        ...
