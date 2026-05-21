"""Google Flow as an ``ImageProvider``.

Thin wrapper around ``flow_sdk.gen_image``. The existing worker's
``_handle_gen_image`` keeps its inline call site for Phase 5 — this
provider exists so the *registry* surface is complete and any future
caller (Phase 7 ffmpeg / preview pipeline, etc.) can route through
the abstraction. Migrating ``_handle_gen_image`` to call this provider
is a follow-up that doesn't change semantics.
"""
from __future__ import annotations

import logging

from flowboard.services import media as media_service
from flowboard.services.flow_client import flow_client
from flowboard.services.flow_sdk import is_valid_project_id

from .base import (
    ImageError,
    ImageGenParams,
    ImageGenResult,
    ImageProviderCapability,
)

logger = logging.getLogger(__name__)


FLOW_DEFAULT_CAPABILITY = ImageProviderCapability(
    supports_reference_images=True,
    supports_variants=True,
    max_variants=4,
    max_refs=8,
    aspect_ratios=("16:9", "9:16", "1:1"),
)


def _resolve_flow_sdk():
    """Match the video-provider hook so test monkeypatches on
    ``flowboard.worker.processor.get_flow_sdk`` are honored here too.

    Falls back to the SDK module directly when the worker isn't on
    sys.path (ad-hoc CLI / unit tests of this provider in isolation).
    """
    try:
        from flowboard.worker import processor as _proc
        return _proc.get_flow_sdk()
    except (ImportError, AttributeError):
        from flowboard.services.flow_sdk import get_flow_sdk as _fallback
        return _fallback()


class FlowImageProvider:
    name = "flow"

    def __init__(self, entry) -> None:
        self.capabilities = entry.capabilities

    async def is_available(self) -> bool:
        return True

    async def submit(self, params: ImageGenParams) -> ImageGenResult:
        prompt = (params.get("prompt") or "").strip()
        if not prompt:
            raise ImageError("bad_input", "missing prompt")
        project_id = (params.get("project_id") or "").strip()
        if not project_id or not is_valid_project_id(project_id):
            raise ImageError("bad_input", "invalid or missing project_id")
        tier = params.get("paygate_tier") or flow_client.paygate_tier
        if tier is None:
            raise ImageError("bad_input", "paygate_tier_unknown")
        variant_count = int(params.get("variant_count") or 1)
        ref_urls = list(params.get("reference_image_urls") or [])

        sdk = _resolve_flow_sdk()
        resp = await sdk.gen_image(
            prompt=prompt,
            project_id=project_id,
            aspect_ratio=params.get("aspect_ratio") or "IMAGE_ASPECT_RATIO_LANDSCAPE",
            paygate_tier=tier,
            ref_media_ids=ref_urls or None,
            variant_count=variant_count,
            image_model=params.get("image_model"),
        )
        if resp.get("error"):
            raise ImageError("internal", str(resp["error"])[:200], raw=resp)

        entries_with_urls = [
            e for e in (resp.get("media_entries") or []) if isinstance(e, dict) and e.get("url")
        ]
        if entries_with_urls:
            try:
                media_service.ingest_urls(entries_with_urls)
            except Exception:  # noqa: BLE001
                logger.exception("flow image auto-ingest failed")

        media_ids = [
            e.get("media_id")
            for e in (resp.get("media_entries") or [])
            if isinstance(e, dict) and isinstance(e.get("media_id"), str)
        ]
        return {
            "status": "succeeded",
            "media_ids": media_ids,
            "error": None,
            "error_message": None,
            "cost_usd": 0.0,
            "raw": resp,
        }
