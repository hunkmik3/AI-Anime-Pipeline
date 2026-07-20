"""BytePlus Seed Audio 1.0 — all-in-one audio-scene generator.

One text prompt → voice + background music + ambience + SFX in a single pass
(ByteDance Seed's non-streaming model). Distinct from Seed-TTS (plain
text→speech) and from the Seedance video reference-audio path.

Native BytePlus Seed Speech API — reverse-engineered + live-verified 2026-07-20
(the in-console "Document" was still "not published yet"):

- ``POST https://voice.ap-southeast-1.bytepluses.com/api/v3/tts/create`` —
  **synchronous** (returns the finished audio inline, no polling).
- Auth: header ``X-Api-Key: <key>`` — the new single-key method; **no AppID /
  Access-Token pair** (that's the legacy dual-header flow).
- Body::

    {"model": "seed-audio-1.0",
     "text_prompt": "<scene script: narration + \"dialogue\" + (voice traits) + SFX/music>",
     "audio_config": {"format", "sample_rate", "pitch_rate", "speech_rate", "loudness_rate"},
     "references": [{"audio_url"|"audio_data"} ...],   # ≤3 → @audio1/@audio2/@audio3
     "watermark": {}}

- Returns ``{"audio": <base64>, "duration", "original_duration", "url"}``.
- Errors arrive as ``{"code", "message"}`` (top level) or ``{"header": {"code",
  "message"}}`` — e.g. ``45000030`` resource-not-granted, ``45001131`` reference
  download failed.

Limits (BytePlus, 2026-07): prompt ≤ 2048 chars, output ≤ 120 s, ≤ 3 reference
audio clips (5–30 s each) OR 1 reference image (mutually exclusive).
"""
from __future__ import annotations

import base64
import logging
import os
import uuid
from pathlib import Path
from typing import Optional

import httpx

from flowboard.services import media as media_service
from flowboard.services.llm import secrets

logger = logging.getLogger(__name__)

BASE_URL = os.getenv("FLOWBOARD_SEED_AUDIO_BASE_URL", "https://voice.ap-southeast-1.bytepluses.com")
MODEL_ID = "seed-audio-1.0"

MAX_PROMPT_CHARS = 2048
MAX_REFERENCES = 3

_FORMATS = {"wav", "mp3", "pcm", "ogg_opus"}
_SAMPLE_RATES = {8000, 16000, 24000, 32000, 44100, 48000}
_MIME_BY_FORMAT = {"wav": "audio/wav", "mp3": "audio/mpeg", "pcm": "audio/basic", "ogg_opus": "audio/ogg"}
_EXT_BY_FORMAT = {"wav": ".wav", "mp3": ".mp3", "pcm": ".pcm", "ogg_opus": ".ogg"}

# BytePlus rate knobs are integer adjustments centred on 0 (0 = neutral); we clamp
# to a sane window so a stray value can't produce a garbled take.
_RATE_MIN, _RATE_MAX = -50, 100
_PITCH_MIN, _PITCH_MAX = -12, 12


class SeedAudioError(RuntimeError):
    """Uniform error for the Seed Audio provider. ``code`` is a short vocab the
    route maps to an HTTP status; ``raw`` keeps the upstream envelope."""

    def __init__(self, code: str, message: str, *, raw: Optional[dict] = None) -> None:
        super().__init__(message)
        self.code = code
        self.raw = raw or {}


def _headers() -> dict[str, str]:
    key = secrets.get_api_key("byteplus_seed_speech")
    if not key:
        raise SeedAudioError(
            "auth",
            "BytePlus Seed Audio key not configured — set BYTEPLUS_SEED_SPEECH_API_KEY "
            "in .env (or apiKeys.byteplus_seed_speech in ~/.flowboard/secrets.json)",
        )
    return {
        "Content-Type": "application/json",
        "X-Api-Key": key,
        "X-Api-Request-Id": str(uuid.uuid4()),
    }


def _clamp(value, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return 0


def _reference_block(ref: str) -> dict:
    """Build one ``references[]`` entry. A public URL passes through as
    ``audio_url``; a local Flowboard media_id is inlined as base64 ``audio_data``
    (so uploaded voice refs work with no R2/public hosting)."""
    if ref.startswith(("http://", "https://")):
        return {"audio_url": ref}
    path = media_service.cached_path(ref)
    if path is None:
        raise SeedAudioError("bad_input", f"reference audio {ref!r} has no local cache file — re-upload it")
    try:
        data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    except OSError as exc:
        raise SeedAudioError("bad_input", f"could not read reference audio {ref!r} ({exc})")
    return {"audio_data": data}


def _image_reference_block(ref: str) -> dict:
    if ref.startswith(("http://", "https://")):
        return {"image_url": ref}
    path = media_service.cached_path(ref)
    if path is None:
        raise SeedAudioError("bad_input", f"reference image {ref!r} has no local cache file")
    try:
        data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    except OSError as exc:
        raise SeedAudioError("bad_input", f"could not read reference image {ref!r} ({exc})")
    return {"image_data": data}


def _extract_error(status: int, data: dict) -> SeedAudioError:
    node = data.get("header") if isinstance(data.get("header"), dict) else data
    code = node.get("code")
    msg = str(node.get("message") or data.get("message") or f"HTTP {status}")[:300]
    low = msg.lower()
    if code == 45000030 or status in (401, 403):
        vocab = "auth"
    elif "download" in low or "reference" in low or code == 45001131:
        vocab = "bad_input"
    elif status == 429 or "rate" in low or "quota" in low or "limit" in low:
        vocab = "quota"
    elif 400 <= status < 500:
        vocab = "bad_input"
    else:
        vocab = "internal"
    return SeedAudioError(vocab, msg, raw=data)


async def generate(
    *,
    prompt: str,
    audio_format: str = "mp3",
    sample_rate: int = 24000,
    speech_rate: int = 0,
    loudness_rate: int = 0,
    pitch_rate: int = 0,
    references: Optional[list[str]] = None,
    image_ref: Optional[str] = None,
    node_id: Optional[int] = None,
    timeout: float = 180.0,
) -> dict:
    """Generate an audio scene and ingest it as a local audio media_id.

    Returns ``{media_id, mime, duration, size}``. Raises ``SeedAudioError`` on
    bad input / auth / provider failure.
    """
    prompt = (prompt or "").strip()
    if not prompt:
        raise SeedAudioError("bad_input", "prompt is required")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise SeedAudioError(
            "bad_input", f"prompt too long ({len(prompt)} > {MAX_PROMPT_CHARS} chars)"
        )

    fmt = audio_format if audio_format in _FORMATS else "mp3"
    try:
        sr = int(sample_rate)
    except (TypeError, ValueError):
        sr = 24000
    if sr not in _SAMPLE_RATES:
        sr = 24000

    body: dict = {
        "model": MODEL_ID,
        "text_prompt": prompt,
        "audio_config": {
            "format": fmt,
            "sample_rate": sr,
            "pitch_rate": _clamp(pitch_rate, _PITCH_MIN, _PITCH_MAX),
            "speech_rate": _clamp(speech_rate, _RATE_MIN, _RATE_MAX),
            "loudness_rate": _clamp(loudness_rate, _RATE_MIN, _RATE_MAX),
        },
        "watermark": {},
    }

    # Audio references (voice clone, ≤3 → @audio1..3) and an image reference are
    # mutually exclusive; the image wins if both are somehow supplied.
    refs = [r for r in (references or []) if isinstance(r, str) and r][:MAX_REFERENCES]
    if image_ref:
        body["references"] = [_image_reference_block(image_ref)]
    elif refs:
        body["references"] = [_reference_block(r) for r in refs]

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{BASE_URL}/api/v3/tts/create", headers=_headers(), json=body)
    except httpx.HTTPError as exc:
        raise SeedAudioError("internal", f"seed audio transport error: {exc}") from exc

    try:
        data = resp.json()
    except ValueError as exc:
        raise SeedAudioError("internal", f"non-JSON response: {resp.text[:200]}") from exc

    audio_b64 = data.get("audio") if isinstance(data, dict) else None
    if resp.status_code >= 400 or not audio_b64:
        raise _extract_error(resp.status_code, data if isinstance(data, dict) else {})

    raw = base64.b64decode(audio_b64)
    media_id = str(uuid.uuid4())
    ext = _EXT_BY_FORMAT.get(fmt, ".mp3")
    mime = _MIME_BY_FORMAT.get(fmt, "audio/mpeg")
    cache_path = media_service.MEDIA_CACHE_DIR / f"{media_id}{ext}"
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(raw)
    except OSError as exc:
        raise SeedAudioError("internal", f"failed to cache generated audio ({exc})") from exc

    from flowboard.db import get_session
    from flowboard.db.models import Asset

    with get_session() as s:
        s.add(Asset(uuid_media_id=media_id, kind="audio", local_path=str(cache_path), mime=mime, node_id=node_id))
        s.commit()

    duration = data.get("duration")
    logger.info("seed-audio: media_id=%s dur=%s size=%d", media_id, duration, len(raw))
    return {
        "media_id": media_id,
        "mime": mime,
        "duration": float(duration) if isinstance(duration, (int, float)) else None,
        "size": len(raw),
    }


def is_configured() -> bool:
    return bool(secrets.get_api_key("byteplus_seed_speech"))
