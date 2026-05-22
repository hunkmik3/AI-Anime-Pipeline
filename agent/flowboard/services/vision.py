"""AI-vision brief generation for cached media (anime pipeline, Phase 6).

Asks the configured Vision provider (Claude / Gemini / OpenAI Codex)
to summarise an image into a short factual description ("aiBrief").
Used by:
- Character / visual_asset / image nodes — annotate uploaded or
  generated images
- Auto-prompt synthesizer — feed those briefs into the downstream
  anime prompt

Provider routing goes through ``run_llm("vision", ...)``. The user
picks which one in Settings → AI Providers — there is no default; the
forced setup gate ensures one is chosen before the app is usable. All
three shipped providers support vision, so the registry's vision-
capability gate is currently a defensive no-op — it kicks in if a
future text-only provider is added.

We always pass an ABSOLUTE path so the underlying transport (CLI flag
or HTTP base64) doesn't get tripped up by the agent's cwd.
"""
from __future__ import annotations

import logging

from typing import Optional

from flowboard.services import media as media_service
from flowboard.services.activity import record_activity
from flowboard.services.llm import run_llm
from flowboard.services.llm.base import LLMError

logger = logging.getLogger(__name__)

# Anime asset annotator. Briefs get spliced into downstream prompts —
# keep them short and technical. 220 chars is enough for "young woman
# w/ short black hair, twin braids, cel-shaded school uniform, neutral
# expression" or "rainy alley at night, neon reflections, low-key
# directional lighting from a vending machine".
_VISION_SYSTEM_ANIME = (
    "You are an anime visual asset annotator. Output one short factual "
    "sentence (max 220 characters) describing the image for downstream "
    "anime prompt synthesis. Focus on:\n"
    "  • For a character: apparent age range, hair color/style, eye "
    "color, distinctive features, expression, outfit silhouette, art "
    "style (rough sketch / cel-shaded / painted / 3D).\n"
    "  • For an environment / location: setting type, time of day, "
    "weather, dominant color palette, lighting direction, mood.\n"
    "  • For a prop / object: shape, material, color, scale relative to "
    "a human, condition (new / weathered / damaged).\n\n"
    "If the asset is an anime character with a name labeled in the "
    "image, preserve the name verbatim. Otherwise output English "
    "description.\n\n"
    "Stay technical — no marketing language, no narrative speculation, "
    "no opinions, no preamble. Output the description sentence only."
)

# Backwards-compatible alias — existing call sites referenced
# ``_VISION_SYSTEM``. Anime version is canonical going forward.
_VISION_SYSTEM = _VISION_SYSTEM_ANIME

_VISION_USER_PROMPT = "Describe this image."


class VisionError(RuntimeError):
    pass


async def describe_media(media_id: str, *, node_id: Optional[int] = None) -> str:
    """Return a short factual description of the cached media.

    Raises ``VisionError`` if the media is not cached locally or if the
    configured Vision provider fails. Caller decides whether to retry
    or fall back.

    ``node_id`` (optional) is forwarded to the activity log so the
    feed can show "Vision · #abc1" instead of an orphan row. Callers
    that know the node should pass it; the route-level handler that
    only has ``media_id`` can leave it None.

    Activity log wraps the entire body — cache misses, fetch failures,
    and provider errors all show up as a single "failed" row. The user
    debugging from the activity feed sees every Vision attempt rather
    than only the ones that reached the provider.
    """
    media_id = media_service.normalize_media_id(media_id)
    if not media_service.is_valid_media_id(media_id):
        raise VisionError("invalid media_id")

    async with record_activity(
        "vision", params={"media_id": media_id}, node_id=node_id
    ) as activity:
        cached = media_service.cached_path(media_id)
        if cached is None:
            # Try to fetch from the stored URL once before giving up.
            # Vision makes no sense without bytes.
            result = await media_service.fetch_and_cache(media_id)
            if result is None:
                raise VisionError("media not cached and could not be fetched")
            _bytes, _mime, path = result
            cached = path

        try:
            # 120s ceiling. Vision is usually fast (5-15s on Claude),
            # but Gemini CLI's cold-start adds ~15s per call and image
            # attachment via `@<path>` adds a few more seconds for the
            # CLI to read + base64-encode the file before sending — and
            # Gemini's image inference itself can stretch when the
            # subject is dense (group shots, fine-print products).
            text = await run_llm(
                "vision",
                _VISION_USER_PROMPT,
                system_prompt=_VISION_SYSTEM,
                attachments=[str(cached.resolve())],
                timeout=120.0,
            )
        except LLMError as exc:
            raise VisionError(f"vision provider failed: {exc}") from exc

        # Trim and cap — defence-in-depth in case the model ignores the
        # length cap from the system prompt.
        text = (text or "").strip()
        if not text:
            raise VisionError("empty response from vision provider")
        if len(text) > 400:
            text = text[:400].rstrip() + "…"
        activity.set_result({"description": text})
        return text
