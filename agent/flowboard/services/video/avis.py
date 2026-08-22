"""Avis (api.avis.xyz) Seedance ``VideoProvider``.

Routes Seedance generation through the Avis gateway instead of talking to
BytePlus ARK directly (see ``dreamina.py`` for the direct path). Probed live
2026-06-15 against ``https://api.avis.xyz/api/v1``:

- **Auth** — header ``x-api-key: <key>`` (env ``AVIS_API_KEY`` or
  ``secrets.json`` ``apiKeys.avis``). Bearer JWT also accepted by Avis but we
  use the API key.
- **Submit** — ``POST /video/generations`` → ``{data:{taskId, generationId,
  status, ...}, success:true, status:200}``.
- **Poll** — ``GET /video/tasks/:taskId`` → ``{data:{status, videoUrl,
  downloadUrl, usage:{usdCost, completionTokens, totalTokens}, error}, ...}``.
- **Content** uses camelCase types + roles (vs BytePlus snake_case):
  ``{type:"text", text}``, ``{type:"imageUrl", url, role}``,
  ``{type:"videoUrl", url}``. Image roles: ``firstFrame`` | ``lastFrame`` |
  ``referenceImage``.
- **Top-level params** — ``model, content, duration, resolution, ratio,
  generateAudio``. No ``--rt/--rs`` inline flags (that's the BytePlus-direct
  quirk); aspect ratio rides the top-level ``ratio`` field.
- **Model id** — ``dreamina-seedance-2-0`` (Seedance 2.0; also a ``-fast``
  / ``-mini`` / 2.5 variant). Discover via ``GET /ai/models?outputModalities=video``.
- **B2B unmoderated** — ``content_filter_disabled=True`` routes submit/poll
  (and KYC asset create) to ``https://api.dancesee.io/api/v1/b2b/*``. Not a
  flag on the regular endpoints. Only Seedance 2.0/2.5; account must be
  ``userType: B2B``. Job ids are prefixed ``b2b:`` so later polls stay on
  that path.

Envelope: every 2xx response wraps the payload in ``{data, success, status,
timestamp}``; 4xx errors come back as ``{errors:[...], success:false,
status}``.

Audio reference (a voice/audio the clip should follow) is wired per the Avis
contract: a content part ``{type:"audioUrl", url, role:"referenceAudio"}`` for a
public wav/mp3, or ``{type:"audioBase64", data, mediaType}`` for a local media_id
(inlined, so it works in the no-R2 desktop build). Avis rejects audio sent
alone, so the part is only attached alongside an image/video reference. This is
distinct from ``generateAudio`` (a synthesized track, also supported).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from functools import lru_cache
from pathlib import Path
from typing import Optional

import httpx

from flowboard.services import media as media_service
from flowboard.services.llm import secrets
from flowboard.services.storage import ObjectStorageError, prepare_image_url

from .base import (
    VideoError,
    VideoErrorCode,
    VideoGenPollResult,
    VideoGenSubmitParams,
    VideoGenSubmitResult,
    VideoProviderCapability,
)
from .pricing import compute_cost_usd

logger = logging.getLogger(__name__)


BASE_URL = "https://api.avis.xyz/api/v1"

#: The three subtasks behind Seedance 2.5's single omni reference-to-video
#: endpoint, plus `auto`. Naming one lets Avis check that subtask's constraints
#: while the request is still synchronous instead of failing the task later.
OMNI_TASK_TYPES = ("auto", "reference", "edit", "extend")

# B2B unmoderated (content-filter-disabled) lives on a separate host + path
# prefix — not a flag on the regular KYC/video endpoints. Production traffic
# to /api/v1/b2b/* is routed at api.dancesee.io; override if the key is bound
# to a different gateway. Auth is the same AVIS_API_KEY (x-api-key).
B2B_BASE_URL = os.getenv("FLOWBOARD_AVIS_B2B_BASE_URL", "https://api.dancesee.io/api/v1").rstrip("/")
B2B_JOB_PREFIX = "b2b:"
B2B_ALLOWED_UPSTREAM_MODELS = frozenset({
    "dreamina-seedance-2-0",
    "dreamina-seedance-2-0-fast",
    "dreamina-seedance-2-0-mini",
    "dreamina-seedance-2-5",
})


def b2b_feature_enabled() -> bool:
    """Process-wide kill switch. Default on; set FLOWBOARD_AVIS_B2B_ENABLED=0 to hide."""
    raw = os.getenv("FLOWBOARD_AVIS_B2B_ENABLED", "1")
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def b2b_unmoderated_allowed(upstream_model_id: Optional[str]) -> bool:
    return b2b_feature_enabled() and (upstream_model_id or "") in B2B_ALLOWED_UPSTREAM_MODELS


def _b2b_urls(unmoderated: bool) -> tuple[str, str, str]:
    """Return (kyc_create, kyc_get_prefix, video_base) for the chosen path."""
    if unmoderated:
        base = B2B_BASE_URL
        return f"{base}/b2b/kyc/assets", f"{base}/b2b/kyc/assets", f"{base}/b2b"
    return f"{BASE_URL}/kyc/user/assets", f"{BASE_URL}/kyc/user/assets", BASE_URL

# Local poll cadence + ceiling for a running video task. Seedance 2.0 at
# 1080p / 15s / multi-ref (or person-driven) can take well over 7.5 min, so
# the ceiling is generous (default 160 × 15s = 40 min) and env-overridable.
# Poll cadence. We poll fast early (short/low-res clips finish quickly) and back
# off to this cap for long ones — cuts the "clip is done on Avis but the UI
# hasn't noticed yet" tail latency (was a flat 15s). Env-tunable.
AVIS_POLL_INTERVAL_S = float(os.getenv("FLOWBOARD_AVIS_POLL_INTERVAL_S", "5"))
AVIS_POLL_FIRST_S = float(os.getenv("FLOWBOARD_AVIS_POLL_FIRST_S", "3"))
AVIS_POLL_MAX_CYCLES = int(os.getenv("FLOWBOARD_AVIS_POLL_MAX_CYCLES", "300"))
# A video gen runs for minutes; a single transient network/TLS blip while
# polling must NOT kill it (the clip is still rendering on Avis). Tolerate this
# many CONSECUTIVE poll transport errors before giving up (reset on success).
AVIS_POLL_TRANSPORT_MAX = int(os.getenv("FLOWBOARD_AVIS_POLL_TRANSPORT_MAX", "10"))

# Process-local throttle on concurrent SUBMIT posts (released as soon as the
# POST returns a taskId — polling runs unbounded, so this does NOT cap how many
# generations run at once, only how many submit calls are in flight). Bumped for
# multi-user: Avis tolerates 20+ concurrent requests fine (measured), so a low
# cap here just needlessly staggers many users submitting at once. Env-tunable.
def _avis_submit_concurrency() -> int:
    try:
        return max(1, int(os.getenv("FLOWBOARD_AVIS_SUBMIT_CONCURRENCY", "12")))
    except ValueError:
        return 12


_CONCURRENCY_SEM = asyncio.Semaphore(_avis_submit_concurrency())

# On HTTP 429 (Avis "api key gen limit reached" — rate/concurrency cap), retry
# the submit with backoff instead of failing the generation. Env-tunable.
_SUBMIT_MAX_RETRIES = max(1, int(os.getenv("FLOWBOARD_AVIS_SUBMIT_RETRIES", "6")))


def _retry_after_s(resp: httpx.Response, attempt: int) -> float:
    """Seconds to wait before retrying a 429: honor the Retry-After header,
    else capped exponential backoff (2, 4, 8, … up to 30s)."""
    ra = resp.headers.get("retry-after")
    if ra:
        try:
            return min(60.0, float(ra))
        except ValueError:
            pass
    return min(30.0, 2.0 ** (attempt + 1))


# Avis Seedance 2.0 (`dreamina-seedance-2-0`). supportedParameters from
# GET /ai/models: duration, resolution, ratio, seed, watermark, generateAudio,
# audioInput, kycAssetInput. inputModalities: image, video, audio, text.
# audioInput is now wired (audioUrl role="referenceAudio" / audioBase64) per the
# published Avis contract → supports_audio_ref=True.
AVIS_SEEDANCE_2_0_CAPABILITY = VideoProviderCapability(
    supports_multi_ref=True,
    supports_last_frame=True,
    supports_audio_toggle=True,
    supports_audio_ref=True,
    supports_video_ref=True,
    supports_kyc=True,
    supports_b2b_unmoderated=True,
    max_refs=9,
    aspect_ratios=("1:1", "16:9", "9:16", "4:3"),
    # 480p/720p/1080p (4k intentionally disabled per request). Shared by the
    # 2.0 family (2.0, 2.0-fast, 2.0-mini) — all r2v + audio + KYC via Avis.
    resolutions=("480p", "720p", "1080p"),
    durations=tuple(range(4, 16)),
)


# Seedance 2.5 (upstream ``dreamina-seedance-2-5``). Same r2v + audio + KYC
# surface as 2.0 — KYC-part acceptance was confirmed against the live Avis API
# (a kyc* part on 2.5 fails only on asset lookup, exactly like 2.0, not on a
# model check).
#
# Every value below is read from ``GET /ai/models`` for this model, re-checked
# 21 Aug 2026 after the 20 Aug release, and it is worth saying why: Avis's PROSE
# documentation and its live capability payload disagree about 2.5 in three
# places. The prose says audio input, the kyc* parts and `generateAudio` are
# "Seedance 2.0 series only"; the live payload reports `audio accepted=True`,
# `kycAsset accepted=True` and a `generateAudio` bool for 2.5. The payload is
# what the API enforces, so the flags below follow it — do not "correct" them
# against the written docs.
#
# What the 20 Aug release actually changed for 2.5:
#   · 1080p, where this used to top out at 720p
#   · outputFormat mp4 | mov
#   · omniReferenceTaskType, the reference / edit / extend subtask hint
AVIS_SEEDANCE_2_5_CAPABILITY = VideoProviderCapability(
    supports_multi_ref=True,
    supports_last_frame=True,
    supports_audio_toggle=True,
    supports_audio_ref=True,
    supports_video_ref=True,
    supports_kyc=True,
    supports_b2b_unmoderated=True,
    supports_omni_reference=True,
    output_formats=("mp4", "mov"),
    # 2.5 accepts up to 30 reference images (Avis /ai/models inputs.image.max=30);
    # 2.0 is curated at 9. Capping at 9 here silently truncated multi-ref gens.
    max_refs=30,
    # `adaptive` is not a shape — it means "follow the source", and it is
    # REQUIRED by the edit and extend subtasks. 3:4 and 21:9 came with the same
    # release. Still no 4k.
    aspect_ratios=("1:1", "16:9", "9:16", "4:3", "3:4", "21:9", "adaptive"),
    resolutions=("480p", "720p", "1080p"),
    durations=tuple(range(4, 31)),
)


# i2v-only Seedance tiers on Avis (text+image inputs; no r2v / audio / KYC):
# seedance-1-5-pro, seedance-1-0-pro, seedance-1-0-pro-fast. Full resolutions
# minus 4k.
AVIS_SEEDANCE_I2V_CAPABILITY = VideoProviderCapability(
    supports_multi_ref=False,
    supports_last_frame=True,
    supports_audio_toggle=False,
    supports_audio_ref=False,
    supports_video_ref=False,
    supports_kyc=False,
    max_refs=0,
    aspect_ratios=("1:1", "16:9", "9:16", "4:3"),
    resolutions=("480p", "720p", "1080p"),
    durations=(5, 10),
)


# Test seam: monkeypatch to a MockTransport-backed client in unit tests.
_http_client_factory = lambda: httpx.AsyncClient(timeout=60.0)


def set_http_client_factory(factory) -> None:
    global _http_client_factory
    _http_client_factory = factory


def reset_http_client_factory() -> None:
    global _http_client_factory
    _http_client_factory = lambda: httpx.AsyncClient(timeout=60.0)


_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


# Inline reference images are downscaled + JPEG-recompressed: reference sheets
# are often several MB, and inlining a few raw blows past Avis's request-size
# limit (HTTP 413). 1280px / q85 preserves identity for a video reference while
# keeping each block ~100-400 KB.
_INLINE_MAX_DIM = 1280
_INLINE_JPEG_Q = 85


@lru_cache(maxsize=64)
def _encode_local_image_inline(path: Path) -> tuple[str, str]:
    """Return (base64, mediaType) for a local image, shrunk to <=_INLINE_MAX_DIM
    on the longest side and re-encoded as JPEG.

    Cached by path: media-cache files are content-addressed (immutable uuid
    names), so re-using the same reference across many generations skips the
    expensive open→resize→JPEG→base64 work. CPU-bound — call it off the event
    loop via asyncio.to_thread so it never blocks concurrent gens/polls."""
    from io import BytesIO

    from PIL import Image

    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((_INLINE_MAX_DIM, _INLINE_MAX_DIM))  # shrink-only, keeps aspect
        buf = BytesIO()
        im.save(buf, format="JPEG", quality=_INLINE_JPEG_Q, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii"), "image/jpeg"


def _image_content_part(ref: str, role: str) -> dict:
    """Build an Avis image content block.

    A public URL passes through as ``imageUrl``. A bare Flowboard media_id is
    read from the local media cache, downscaled, and sent INLINE as
    ``imageBase64`` — Avis stores it server-side and returns an internal
    assetId. This is what lets the self-contained desktop build run with **no
    R2** (the worker passes media_ids straight through for the Avis provider).
    """
    if ref.startswith(("http://", "https://", "data:")):
        return {"type": "imageUrl", "url": ref, "role": role}
    path = media_service.cached_path(ref)
    if path is None:
        raise VideoError(
            "bad_input",
            f"reference image {ref!r} has no local cache file — upload it first",
        )
    p = Path(path)
    try:
        data, mime = _encode_local_image_inline(p)
    except (OSError, ValueError) as exc:
        # Unreadable / non-image — fall back to raw bytes so small valid files
        # still work (large ones may then hit Avis's 413).
        logger.warning("avis: inline recompress failed for %s (%s); sending raw", ref, exc)
        data = base64.b64encode(p.read_bytes()).decode("ascii")
        mime = _MIME_BY_EXT.get(p.suffix.lower(), "image/png")
    return {"type": "imageBase64", "data": data, "mediaType": mime, "role": role}


_AUDIO_MIME_BY_EXT = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
}
# Exactly the formats Avis reports as accepted for a Seedance 2.0 reference-audio
# input — GET /ai/models → capabilities.video.inputs.audio.mediaTypes (probed
# 2026-07-20). m4a/aac/ogg are NOT accepted, so we drop them with an actionable
# warning instead of letting the whole (paid) generation 400 downstream.
_SEEDANCE_AUDIO_MIMES = {
    "audio/mpeg",
    "audio/mp3",
    "audio/wav",
    "audio/x-wav",
    "audio/wave",
}

_AUDIO_UNSUPPORTED = (
    "Audio reference dropped: {fmt} isn't a format Seedance 2.0 accepts — "
    "re-upload the voice as mp3 or wav."
)


def _audio_content_part(ref: str) -> tuple[Optional[dict], Optional[str]]:
    """Build an Avis reference-audio content block for Seedance 2.0.

    - public URL → ``{type:"audioUrl", url, role:"referenceAudio"}``
    - data: URL  → ``{type:"audioBase64", data, mediaType}`` (mime from the URL)
    - media_id   → read the local cache and inline as ``audioBase64`` (no R2
      needed — parity with the base64 image path, so audio works in the
      self-contained desktop build).

    Returns ``(part, warning)``. ``part`` is ``None`` when the ref can't be used
    (missing cache, or a format Avis rejects — m4a/aac/ogg) so the caller drops
    it with the returned warning rather than failing the whole generation. Only
    mp3/wav pass through; the model won't take anything else (verified against
    the live capability catalog).
    """
    if ref.startswith(("http://", "https://")):
        mime = _AUDIO_MIME_BY_EXT.get(Path(ref.split("?", 1)[0]).suffix.lower())
        # A known-but-rejected extension → drop; an unknown/absent extension →
        # trust the caller's URL (it may be a signed link with no suffix).
        if mime is not None and mime not in _SEEDANCE_AUDIO_MIMES:
            return None, _AUDIO_UNSUPPORTED.format(fmt=mime)
        return {"type": "audioUrl", "url": ref, "role": "referenceAudio"}, None
    if ref.startswith("data:"):
        # data:<mime>;base64,<payload>
        header = ref[5:].split(",", 1)[0]
        mime = header.split(";", 1)[0].strip() or None
        if not mime:
            return None, "Audio reference dropped: data URL has no media type."
        if mime not in _SEEDANCE_AUDIO_MIMES:
            return None, _AUDIO_UNSUPPORTED.format(fmt=mime)
        return {"type": "audioBase64", "data": ref, "mediaType": mime}, None
    # bare Flowboard media_id → inline base64 from the local cache
    path = media_service.cached_path(ref)
    if path is None:
        return None, f"Audio reference dropped: no local cache for {ref!r} — re-upload it."
    p = Path(path)
    mime = _AUDIO_MIME_BY_EXT.get(p.suffix.lower())
    if mime is None or mime not in _SEEDANCE_AUDIO_MIMES:
        return None, _AUDIO_UNSUPPORTED.format(fmt=(mime or p.suffix or "this format"))
    try:
        data = base64.b64encode(p.read_bytes()).decode("ascii")
    except OSError as exc:
        return None, f"Audio reference dropped: could not read cached file ({exc})."
    return {"type": "audioBase64", "data": data, "mediaType": mime}, None


# Avis caps the *combined* referenceAudio duration at 15.2s for Seedance 2.0 in
# r2v (confirmed live 2026-07-20: over-cap → 400 "audio total duration ... must
# be ≤ 15.2"). We enforce a slightly tighter budget so a couple of voices fit
# without brushing the wire limit.
AVIS_MAX_AUDIO_TOTAL_S = 15.0


def _audio_duration_seconds(ref: str) -> Optional[float]:
    """Best-effort duration (seconds) of an audio ref, or ``None`` when unknown
    (a public/data URL, or no probe tool). WAV goes through the stdlib ``wave``
    module — always available, so the no-ffmpeg desktop build still enforces the
    cap for the common voice-upload case; other local formats use ffprobe when
    it's installed. ``None`` means "don't count it" — Avis's own limit backstops.
    """
    if ref.startswith(("http://", "https://", "data:")):
        return None
    path = media_service.cached_path(ref)
    if path is None:
        return None
    p = Path(path)
    if p.suffix.lower() == ".wav":
        import wave

        try:
            with wave.open(str(p), "rb") as w:
                rate = w.getframerate()
                return w.getnframes() / float(rate) if rate else None
        except (OSError, wave.Error, EOFError):
            return None
    import subprocess

    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(p)],
            capture_output=True, text=True, timeout=10,
        )
        return float(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


# ── KYC assets (person-driven video) ────────────────────────────────────────
# Portrait→video / lip-sync / video-reference need an identity-verified Avis
# KYC asset. Unlike regular refs (base64 inline), a KYC asset is created from a
# PUBLIC HTTPS URL, so the local media is hoisted to R2 first. The created
# assetId is cached on the Asset row (reusable) so we don't re-upload+re-poll.

KYC_POLL_INTERVAL_S = 4.0
KYC_POLL_MAX_CYCLES = 75  # ~5 min — matches Avis's processing timeout
_KYC_ASSET_TYPES = {"Image", "Video", "Audio"}


def _kyc_headers() -> dict[str, str]:
    api_key = secrets.get_api_key("avis")
    if not api_key:
        raise VideoError("auth", "Avis API key not configured (AVIS_API_KEY)")
    return {"accept": "application/json", "x-api-key": api_key, "Content-Type": "application/json"}


def _kyc_cache_key(unmoderated: bool) -> str:
    # B2B Skip-moderation assets must not reuse a Default-moderated assetId
    # (or vice versa) — same file, different BytePlus strategy.
    return "avis_kyc_b2b" if unmoderated else "avis_kyc"


def _read_cached_kyc_asset(
    media_id: str, asset_type: str, *, unmoderated: bool = False
) -> Optional[str]:
    """Return a cached, still-active Avis KYC assetId for this media_id, if any."""
    from sqlmodel import select

    from flowboard.db import get_session
    from flowboard.db.models import Asset

    with get_session() as s:
        row = s.exec(select(Asset).where(Asset.uuid_media_id == media_id)).first()
        meta = (row.asset_metadata or {}) if row is not None else {}
        kyc = meta.get(_kyc_cache_key(unmoderated)) if isinstance(meta, dict) else None
        if (
            isinstance(kyc, dict)
            and kyc.get("status") == "active"
            and kyc.get("asset_type") == asset_type
            and isinstance(kyc.get("asset_id"), str)
        ):
            return kyc["asset_id"]
    return None


def _write_cached_kyc_asset(
    media_id: str, asset_type: str, asset_id: str, *, unmoderated: bool = False
) -> None:
    from sqlmodel import select

    from flowboard.db import get_session
    from flowboard.db.models import Asset

    with get_session() as s:
        row = s.exec(select(Asset).where(Asset.uuid_media_id == media_id)).first()
        if row is None:
            return  # no Asset row to cache on — harmless, re-resolve next time
        meta = dict(row.asset_metadata or {})
        meta[_kyc_cache_key(unmoderated)] = {
            "asset_id": asset_id,
            "asset_type": asset_type,
            "status": "active",
            "moderation_strategy": "Skip" if unmoderated else "Default",
        }
        row.asset_metadata = meta
        s.add(row)
        s.commit()


async def ensure_kyc_asset(
    media_id: str,
    asset_type: str,
    *,
    project_id: Optional[str] = None,
    unmoderated: bool = False,
) -> str:
    """Resolve a local media_id to an *active* Avis KYC assetId (cached, reused).

    Hoists the cached file to a public R2 URL, creates the KYC asset, polls until
    ``active``, caches the assetId on the Asset row, and returns it. Raises
    VideoError on R2 misconfig, processing failure, or timeout.

    Regular path requires a KYC-verified account (``isKyc: true``). The B2B
    unmoderated path (``unmoderated=True``) posts to ``/b2b/kyc/assets`` with
    moderation skipped, and requires ``userType: B2B`` instead.
    """
    if asset_type not in _KYC_ASSET_TYPES:
        raise VideoError("internal", f"bad KYC asset_type: {asset_type!r}")

    cached = _read_cached_kyc_asset(media_id, asset_type, unmoderated=unmoderated)
    if cached:
        return cached

    local = media_service.cached_path(media_id)
    if local is None:
        raise VideoError("bad_input", f"KYC asset media {media_id!r} has no local cache file")
    try:
        public_url = prepare_image_url(Path(local), project_id=project_id, asset_id=media_id)
    except ObjectStorageError as exc:
        raise VideoError(
            "bad_input",
            "Person-driven (KYC) video needs public file hosting (R2) configured — "
            f"set the R2 block in .env/secrets. ({exc})",
        ) from exc

    create_url, get_prefix, _video_base = _b2b_urls(unmoderated)
    async with _http_client_factory() as client:
        try:
            resp = await client.post(
                create_url,
                json={"url": public_url, "assetType": asset_type, "name": media_id[:64]},
                headers=_kyc_headers(),
            )
        except httpx.HTTPError as exc:
            raise VideoError("internal", f"avis kyc create transport error: {exc}") from exc
    if resp.status_code >= 400:
        raise _classify_avis_http_error(resp)
    data = _unwrap(resp)
    asset_id = data.get("assetId")
    if not isinstance(asset_id, str) or not asset_id:
        raise VideoError("internal", "avis kyc create missing assetId", raw=data)

    status = data.get("status")
    attempts = 0
    while status not in ("active", "failed") and attempts < KYC_POLL_MAX_CYCLES:
        await asyncio.sleep(KYC_POLL_INTERVAL_S)
        attempts += 1
        async with _http_client_factory() as client:
            try:
                presp = await client.get(
                    f"{get_prefix}/{asset_id}", headers=_kyc_headers()
                )
            except httpx.HTTPError as exc:
                raise VideoError("internal", f"avis kyc poll transport error: {exc}") from exc
        if presp.status_code >= 400:
            raise _classify_avis_http_error(presp)
        pdata = _unwrap(presp)
        status = pdata.get("status")
        if status == "failed":
            msg = pdata.get("errorMessage") or pdata.get("errorCode") or "kyc asset processing failed"
            code_raw = str(pdata.get("errorCode") or "").lower()
            code: VideoErrorCode = (
                "content_filtered" if ("sensitive" in code_raw or "policy" in code_raw) else "bad_input"
            )
            raise VideoError(code, str(msg)[:200], raw=pdata)

    if status != "active":
        raise VideoError("timeout", f"KYC asset {asset_id} not active after {attempts} polls")

    _write_cached_kyc_asset(media_id, asset_type, asset_id, unmoderated=unmoderated)
    return asset_id


async def _fetch_actual_usd_cost(
    generation_id: str, *, unmoderated: bool = False
) -> Optional[float]:
    """Real usdCost for a finished gen, looked up from the generations history
    (``/video/generations``) — the per-task endpoint doesn't include it. Used to
    settle per-user budgets on actual spend. Returns None if not found."""
    _create, _get, video_base = _b2b_urls(unmoderated)
    try:
        async with _http_client_factory() as client:
            resp = await client.get(
                f"{video_base}/video/generations?limit=50", headers=_kyc_headers()
            )
        if resp.status_code >= 400:
            return None
        data = _unwrap(resp)
    except (httpx.HTTPError, VideoError, ValueError):
        return None
    for rec in data.get("results") or []:
        if isinstance(rec, dict) and rec.get("id") == generation_id:
            usage = rec.get("usage") if isinstance(rec.get("usage"), dict) else {}
            cost = usage.get("usdCost")
            return float(cost) if isinstance(cost, (int, float)) else None
    return None


class AvisVideoProvider:
    """One instance per registered model. ``upstream_model_id`` is the Avis
    model id (e.g. ``dreamina-seedance-2-0``)."""

    name = "avis"

    def __init__(self, entry) -> None:
        self.entry = entry
        self.capabilities = entry.capabilities
        self.upstream_model_id = entry.upstream_model_id
        self.model_id = entry.model_id  # registry key, used for pricing fallback

    def _headers(self, *, post: bool = False) -> dict[str, str]:
        api_key = secrets.get_api_key("avis")
        if not api_key:
            raise VideoError(
                "auth",
                "Avis API key not configured — set AVIS_API_KEY in .env "
                "(or apiKeys.avis in ~/.flowboard/secrets.json)",
            )
        headers = {"accept": "application/json", "x-api-key": api_key}
        if post:
            headers["Content-Type"] = "application/json"
        return headers

    async def is_available(self) -> bool:
        return bool(secrets.get_api_key("avis"))

    # ── submit ────────────────────────────────────────────────────────

    async def submit(self, params: VideoGenSubmitParams) -> VideoGenSubmitResult:
        headers = self._headers(post=True)  # also validates the key is present
        warnings: list[str] = []

        motion_prompt = (params.get("motion_prompt") or "").strip()
        if not motion_prompt:
            raise VideoError("bad_input", "missing motion_prompt")

        unmoderated = bool(params.get("content_filter_disabled"))
        if unmoderated and not b2b_unmoderated_allowed(self.upstream_model_id):
            raise VideoError(
                "bad_input",
                "B2B unmoderated generation is only available on Seedance 2.0/2.5 "
                f"({', '.join(sorted(B2B_ALLOWED_UPSTREAM_MODELS))}); "
                f"got {self.upstream_model_id!r}",
            )

        # ── person-driven (KYC) path ────────────────────────────────────
        # Pre-resolved Avis KYC assetIds (the worker creates them from local
        # media_ids). When any is present this is portrait→video / lip-sync /
        # video-reference: emit kyc*AssetId parts and skip the regular refs.
        # Image identities can be MANY (one kycImageAssetId part each, in @imageN
        # order); audio/video stay single. Fall back to the legacy singular field.
        kyc_image_ids = [a for a in (params.get("kyc_image_asset_ids") or []) if a]
        if not kyc_image_ids and params.get("kyc_image_asset_id"):
            kyc_image_ids = [params["kyc_image_asset_id"]]
        kyc_audio_id = params.get("kyc_audio_asset_id")
        kyc_video_id = params.get("kyc_video_asset_id")
        if kyc_image_ids or kyc_audio_id or kyc_video_id:
            if not self.capabilities.supports_kyc:
                warnings.append(
                    f"Dropped KYC assets: {self.entry.display_name} doesn't "
                    f"support person-driven video."
                )
            else:
                return await self._submit_kyc(
                    params, motion_prompt, kyc_image_ids, kyc_audio_id, kyc_video_id, headers, warnings,
                    unmoderated=unmoderated,
                )

        first_frame_url = (params.get("first_frame_url") or "").strip()
        last_frame_url = params.get("last_frame_url")
        # Multi-ref audio (@audioN, worker-ordered) with a single-field fallback.
        audio_refs = [a for a in (params.get("audio_ref_urls") or []) if isinstance(a, str) and a]
        if not audio_refs:
            single = (params.get("audio_ref_url") or "").strip()
            if single:
                audio_refs = [single]

        # ── capability gate (drop-with-warning, never silent) ───────────
        reference_images = [
            r for r in (params.get("reference_images") or []) if isinstance(r, str) and r
        ]
        if reference_images and not self.capabilities.supports_multi_ref:
            warnings.append(
                f"Dropped {len(reference_images)} reference images: "
                f"{self.entry.display_name} is i2v-only."
            )
            reference_images = []
        elif reference_images and len(reference_images) > self.capabilities.max_refs:
            cut = self.capabilities.max_refs
            warnings.append(
                f"Truncated reference images from {len(reference_images)} → {cut} "
                f"(model max). Excess refs ignored."
            )
            reference_images = reference_images[:cut]

        reference_videos = [
            r for r in (params.get("reference_videos") or []) if isinstance(r, str) and r
        ]
        if reference_videos and not self.capabilities.supports_video_ref:
            warnings.append(
                f"Dropped {len(reference_videos)} reference video(s): "
                f"{self.entry.display_name} doesn't support reference video."
            )
            reference_videos = []

        if audio_refs and not self.capabilities.supports_audio_ref:
            warnings.append(
                f"Dropped {len(audio_refs)} audio reference(s): "
                f"{self.entry.display_name} doesn't accept a reference-audio "
                "input (Seedance 2.0 only)."
            )
            audio_refs = []

        # Resolve the audio parts up front — whether any attaches decides the
        # request mode. Encode off the event loop (file read + base64 for a local
        # media_id is blocking) and in parallel; order is preserved so @audio1
        # (worker-sorted first) leads the referenceAudio blocks.
        audio_parts: list[dict] = []
        if audio_refs and self.capabilities.supports_audio_ref:
            # Enforce Avis's combined referenceAudio cap (~15s in r2v): keep
            # voices in @audio order until the cumulative KNOWN duration would
            # exceed it, then drop the overflow with a warning — fail-fast and
            # graceful instead of a raw 400. Refs we can't measure (URLs, no
            # probe) don't add to the running total; Avis's limit backstops them.
            durations = await asyncio.gather(
                *[asyncio.to_thread(_audio_duration_seconds, a) for a in audio_refs]
            )
            kept_refs: list[str] = []
            total = 0.0
            for a, dur in zip(audio_refs, durations):
                if dur is not None and kept_refs and total + dur > AVIS_MAX_AUDIO_TOTAL_S:
                    break  # this voice + the rest don't fit
                kept_refs.append(a)
                total += dur or 0.0
            if len(kept_refs) < len(audio_refs):
                warnings.append(
                    f"Dropped {len(audio_refs) - len(kept_refs)} audio reference(s): "
                    "Seedance 2.0 caps total voice duration at ~15s — kept the first "
                    f"{len(kept_refs)} in @audio order. Trim or use fewer voices."
                )
            elif len(kept_refs) == 1 and (durations[0] or 0.0) > AVIS_MAX_AUDIO_TOTAL_S:
                warnings.append(
                    f"The audio reference is ~{durations[0]:.0f}s, over Seedance 2.0's "
                    "~15s cap — it may be rejected; trim it to 15s or less."
                )
            for part, audio_warn in await asyncio.gather(
                *[asyncio.to_thread(_audio_content_part, a) for a in kept_refs]
            ):
                if audio_warn:
                    warnings.append(audio_warn)
                if part is not None:
                    audio_parts.append(part)

        # ── audio ⇒ reference-media mode ─────────────────────────────────
        # Avis rejects an audio reference combined with a first/last-frame block
        # ("first/last frame content cannot be mixed with reference media
        # content", confirmed live). So a valid audio ref demotes the start image
        # to a referenceImage and forces r2v — the same rule the BytePlus-direct
        # path uses. Dropped audio (bad format) leaves the i2v path untouched.
        audio_mode = bool(audio_parts)
        if audio_mode:
            if first_frame_url and first_frame_url not in reference_images:
                reference_images.insert(0, first_frame_url)
            first_frame_url = ""
            if last_frame_url:
                warnings.append(
                    "Dropped last_frame: can't mix keyframe interpolation with an "
                    "audio reference (audio counts as reference media)."
                )
                last_frame_url = None
            # Images are always usable (inlined as base64); a reference video is
            # only usable if it's a public URL (Avis has no inline video upload).
            usable_video = any(
                isinstance(v, str) and v.startswith(("http://", "https://"))
                for v in reference_videos
            )
            if not reference_images and not usable_video:
                raise VideoError(
                    "bad_input",
                    "An audio reference needs an accompanying image or video "
                    "reference — audio can't drive a clip on its own.",
                )

        # ── mode detection ──────────────────────────────────────────────
        #   r2v : audio ref, ≥2 reference images, OR any reference video.
        #   i2v : single start frame (+ optional last frame).
        if audio_mode or (
            self.capabilities.supports_multi_ref
            and (len(reference_images) > 1 or reference_videos)
        ):
            mode = "r2v"
            if first_frame_url:
                warnings.append(
                    "Ignored the start frame in reference-to-video mode — "
                    "references drive generation."
                )
        else:
            mode = "i2v"
            if reference_images:
                if not first_frame_url:
                    first_frame_url = reference_images[0]
                else:
                    warnings.append(
                        f"Ignored {len(reference_images)} reference image(s) in i2v mode "
                        f"— attach ≥2 refs for r2v, or remove the start frame."
                    )
                reference_images = []

        if mode == "i2v" and not first_frame_url:
            raise VideoError(
                "bad_input",
                "i2v submit requires first_frame_url (publicly-reachable HTTPS URL)",
            )

        if last_frame_url and not self.capabilities.supports_last_frame:
            warnings.append(
                f"Dropped last_frame: {self.entry.display_name} doesn't "
                f"support keyframe interpolation."
            )
            last_frame_url = None
        elif last_frame_url and mode != "i2v":
            warnings.append(
                "Dropped last_frame: cannot mix keyframe interpolation with "
                "reference-media content on this submit."
            )
            last_frame_url = None

        duration = int(params.get("duration_seconds") or 5)
        if duration not in self.capabilities.durations:
            allowed = ", ".join(str(d) for d in self.capabilities.durations)
            raise VideoError(
                "bad_input", f"duration_seconds={duration} not supported (allowed: {allowed})"
            )
        aspect = params.get("aspect_ratio") or "1:1"
        if aspect not in self.capabilities.aspect_ratios:
            allowed = ", ".join(self.capabilities.aspect_ratios)
            raise VideoError(
                "bad_input", f"aspect_ratio={aspect!r} not supported (allowed: {allowed})"
            )
        resolution = params.get("resolution") or "720p"
        if resolution not in self.capabilities.resolutions:
            allowed = ", ".join(self.capabilities.resolutions)
            raise VideoError(
                "bad_input", f"resolution={resolution!r} not supported (allowed: {allowed})"
            )

        generate_audio = params.get("generate_audio")
        if generate_audio is not None and not self.capabilities.supports_audio_toggle:
            warnings.append(
                f"Ignored generate_audio toggle: {self.entry.display_name} "
                f"uses provider default."
            )
            generate_audio = None

        # ── assemble content (camelCase types + roles) ──────────────────
        # Images are sent inline as base64 when they're local media_ids (no R2);
        # public URLs pass through. See _image_content_part.
        content: list[dict] = [{"type": "text", "text": motion_prompt}]
        # Encode image parts OFF the event loop (Pillow resize + base64 is CPU-
        # bound and would otherwise block every other concurrent gen/poll) and
        # in parallel. Cached, so repeat refs across gens are near-instant.
        if mode == "i2v":
            jobs = [(first_frame_url, "firstFrame")]
            if last_frame_url:
                jobs.append((last_frame_url, "lastFrame"))
            content.extend(
                await asyncio.gather(
                    *[asyncio.to_thread(_image_content_part, r, role) for r, role in jobs]
                )
            )
        else:
            if reference_images:
                content.extend(
                    await asyncio.gather(
                        *[
                            asyncio.to_thread(_image_content_part, ref, "referenceImage")
                            for ref in reference_images
                        ]
                    )
                )
            for vref in reference_videos:
                # Avis has no inline video upload — reference videos must be a
                # public URL. Drop bare media_ids with a warning.
                if vref.startswith(("http://", "https://")):
                    content.append({"type": "videoUrl", "url": vref})
                else:
                    warnings.append(
                        "Dropped a local reference video: Avis needs a public "
                        "video URL (no inline video upload)."
                    )

        # ── audio references (Seedance 2.0 referenceAudio) ──────────────
        # Resolved up front (they set the mode + demoted the start frame to a
        # referenceImage above); appended after the image/video parts in @audioN
        # order. Multiple tracks are sent as-is — the model decides how it maps
        # them (Avis exposes no per-subject binding channel).
        content.extend(audio_parts)

        body: dict = {
            "model": self.upstream_model_id,
            "content": content,
            "duration": duration,
            "resolution": resolution,
            "ratio": aspect,
        }
        if generate_audio is not None:
            body["generateAudio"] = bool(generate_audio)
        self._apply_25_options(body, params, warnings, has_keyframe=(mode == "i2v"))

        return await self._post_generation(body, headers, warnings, unmoderated=unmoderated)

    def _apply_25_options(
        self,
        body: dict,
        params: VideoGenSubmitParams,
        warnings: list[str],
        *,
        has_keyframe: bool,
    ) -> None:
        """Attach the Seedance 2.5-only fields, and refuse what Avis would.

        Both fields 400 on any other model, so each is gated on the capability
        rather than on the model id — a future model that gains them needs no
        change here.

        The two `edit` / `extend` rules are checked LOCALLY on purpose. Avis
        validates them too, and that is the whole point of the parameter: it
        moves the failure from "the task died four minutes in" to "the request
        was refused". Doing it here as well moves it one step earlier again, to
        before the money is reserved, and lets the message name the field.
        """
        # 2.5 SILENTLY drops `ratio` when a first/last frame is present — the
        # output follows that image and the value is never stored. Say so, or the
        # recorded settings claim a ratio the clip does not have. Keyed on the
        # omni capability because that flag IS "this is the 2.5 family"; 2.0 has
        # no such rule and still honours an explicit ratio beside a keyframe.
        if has_keyframe and self.capabilities.supports_omni_reference and body.get("ratio"):
            warnings.append(
                "Aspect ratio ignored: with a first/last frame, Seedance 2.5 "
                "takes the ratio from that image."
            )

        fmt = params.get("output_format")
        if fmt:
            if not self.capabilities.output_formats:
                warnings.append(
                    f"Ignored output_format: {self.entry.display_name} has no "
                    f"container choice (mp4 only)."
                )
            elif fmt not in self.capabilities.output_formats:
                allowed = ", ".join(self.capabilities.output_formats)
                raise VideoError(
                    "bad_input", f"output_format={fmt!r} not supported (allowed: {allowed})"
                )
            else:
                body["outputFormat"] = fmt

        task_type = params.get("omni_reference_task_type")
        if not task_type:
            return
        if not self.capabilities.supports_omni_reference:
            warnings.append(
                f"Ignored omni_reference_task_type: {self.entry.display_name} "
                f"has no omni reference-to-video mode."
            )
            return
        if task_type not in OMNI_TASK_TYPES:
            allowed = ", ".join(OMNI_TASK_TYPES)
            raise VideoError(
                "bad_input",
                f"omni_reference_task_type={task_type!r} not supported (allowed: {allowed})",
            )

        if task_type in ("edit", "extend"):
            has_video = any(
                c.get("type") in ("videoUrl", "videoBase64", "videoAssetId")
                for c in body.get("content", [])
            )
            if not has_video:
                raise VideoError(
                    "bad_input",
                    f"omni_reference_task_type={task_type!r} needs a reference video "
                    f"(4–30s) — attach one before generating.",
                )
            if body.get("ratio") != "adaptive":
                raise VideoError(
                    "bad_input",
                    f"omni_reference_task_type={task_type!r} requires aspect_ratio "
                    f"'adaptive': the output follows the source clip, so a fixed "
                    f"ratio would be a contradiction rather than a crop.",
                )

        if task_type == "edit":
            # An edit REWRITES the source clip, so its length is the source's
            # length and `duration` carries no caller intent. BytePlus takes
            # `-1` as the sentinel for exactly that, and Avis documents it:
            # "When set to `edit`, `content` must contain at least one
            # `reference_video`, the video must be 4-30 seconds long, `ratio`
            # must be `adaptive`, and `duration` must be `-1`."
            #
            # Derive it rather than asking: any positive number the caller
            # picks is refused, and leaving the field out is worse — the
            # gateway substitutes its own default of 5 and the model rejects
            # that, after the task has been created.
            #
            # (Avis only began accepting -1 on 22 Aug 2026; before that the
            # gateway refused it as "must be a positive number" while the model
            # demanded it, and no edit could be submitted at all. Verified
            # working again after their fix — task cgt-20260822114311-972hs
            # returned a 4.736s cut of a 5.000s source, at the source's ratio.)
            previous = body.get("duration")
            body["duration"] = -1
            if isinstance(previous, int) and previous > 0:
                warnings.append(
                    f"Duration ({previous}s) ignored: an edit keeps the source "
                    f"clip's length."
                )

        # Avis refuses the field outright on anything that is not a multimodal
        # reference request: "This parameter is only supported in the multimodal
        # reference scenario, and is not applicable to text-to-video generation
        # or first-frame / first-last-frame generation." A lone reference image
        # becomes a START FRAME here (r2v needs two images, a video, or audio),
        # so the field has to come off or the whole generation 400s with a
        # message that reads like the model lacks the feature.
        #
        # Only `auto` and `reference` reach this: edit and extend already
        # demanded a video reference above, which forces r2v.
        if has_keyframe:
            warnings.append(
                "Ignored omni_reference_task_type: it applies only to a "
                "multimodal reference request. Attach at least two reference "
                "images, or a reference video, instead of a start frame."
            )
            return

        body["omniReferenceTaskType"] = task_type

    async def estimate_body(self, body: dict, *, unmoderated: bool = False) -> Optional[float]:
        """Ask Avis what a built body would cost, in USD.

        ``POST /video/estimate`` takes the exact generation body, deducts no
        credit and persists nothing, so it is also the only way to exercise a
        request shape — a new field, a new subtask — without paying for a clip.

        Returns ``None`` rather than raising when the endpoint is unavailable or
        answers oddly: a price preview must never be the reason a generation
        cannot be submitted. Avis's own note applies — the figure is indicative,
        and the real charge is known only once the clip finishes.
        """
        _create, _get, video_base = _b2b_urls(unmoderated)
        try:
            async with _http_client_factory() as client:
                resp = await client.post(
                    f"{video_base}/video/estimate", json=body, headers=self._headers()
                )
        except httpx.HTTPError as exc:
            logger.warning("avis estimate transport error: %s", exc)
            return None
        if resp.status_code >= 400:
            # Worth a log line: a 400 here is the estimate refusing the exact
            # body the submit is about to send, which is a real signal.
            logger.warning("avis estimate HTTP %s: %s", resp.status_code, resp.text[:300])
            return None
        try:
            cost = (resp.json().get("data") or {}).get("estimatedUserCost")
            return float(cost) if cost is not None else None
        except (ValueError, AttributeError):
            return None

    async def _post_generation(
        self, body: dict, headers: dict, warnings: list[str], *, unmoderated: bool = False
    ) -> VideoGenSubmitResult:
        """POST a built /video/generations body; return the submit result.

        Avis returns HTTP 429 ("api key gen limit reached") when the key's
        rate/concurrency cap is hit. Rather than fail the generation, retry the
        submit with backoff (honoring Retry-After) so it goes through once a
        slot frees up. B2B unmoderated posts to /b2b/video/generations instead."""
        _create, _get, video_base = _b2b_urls(unmoderated)
        submit_url = f"{video_base}/video/generations"
        await _CONCURRENCY_SEM.acquire()
        try:
            async with _http_client_factory() as client:
                resp = None
                for attempt in range(_SUBMIT_MAX_RETRIES):
                    try:
                        resp = await client.post(
                            submit_url, json=body, headers=headers
                        )
                    except httpx.HTTPError as exc:
                        raise VideoError("internal", f"avis submit transport error: {exc}") from exc
                    if resp.status_code != 429 or attempt == _SUBMIT_MAX_RETRIES - 1:
                        break
                    wait = _retry_after_s(resp, attempt)
                    logger.warning(
                        "avis submit 429 (rate/concurrency limit) — retrying in %.1fs (%d/%d)",
                        wait, attempt + 1, _SUBMIT_MAX_RETRIES - 1,
                    )
                    await asyncio.sleep(wait)
        finally:
            _CONCURRENCY_SEM.release()

        if resp.status_code >= 400:
            # Log WHAT WE SENT beside the refusal. Avis's messages name a
            # parameter ("the parameter duration ... is not valid") without
            # echoing the request, so without this there is no way to tell "we
            # sent a bad field" apart from "the field is absent and the
            # complaint is about something else" — you have to reconstruct the
            # body by hand and hope the reconstruction matches. Base64 payloads
            # collapse to their length: the shape is what matters and the bytes
            # would bury the log.
            logger.warning(
                "avis submit %d — body we sent: %s",
                resp.status_code,
                json.dumps(_redact_body_for_log(body), ensure_ascii=False)[:2000],
            )
            raise _classify_avis_http_error(resp)
        data = _unwrap(resp)
        task_id = data.get("taskId")
        if not isinstance(task_id, str) or not task_id:
            raise VideoError("internal", "avis submit missing taskId", raw=data)
        if unmoderated:
            task_id = f"{B2B_JOB_PREFIX}{task_id}"
        return {
            "external_job_id": task_id,
            "submitted_at": int(time.time()),
            "warnings": warnings,
        }

    async def _submit_kyc(
        self,
        params: VideoGenSubmitParams,
        motion_prompt: str,
        kyc_image_ids: list[str],
        kyc_audio_id: Optional[str],
        kyc_video_id: Optional[str],
        headers: dict,
        warnings: list[str],
        *,
        unmoderated: bool = False,
    ) -> VideoGenSubmitResult:
        """Person-driven submit: text + kyc*AssetId content parts (no regular refs).

        Emits one ``kycImageAssetId`` per identity, in @imageN order, so several
        real-person characters can be locked in a single generation."""
        duration = int(params.get("duration_seconds") or 5)
        if duration not in self.capabilities.durations:
            allowed = ", ".join(str(d) for d in self.capabilities.durations)
            raise VideoError(
                "bad_input", f"duration_seconds={duration} not supported (allowed: {allowed})"
            )
        aspect = params.get("aspect_ratio") or "1:1"
        if aspect not in self.capabilities.aspect_ratios:
            allowed = ", ".join(self.capabilities.aspect_ratios)
            raise VideoError("bad_input", f"aspect_ratio={aspect!r} not supported (allowed: {allowed})")
        resolution = params.get("resolution") or "720p"
        if resolution not in self.capabilities.resolutions:
            allowed = ", ".join(self.capabilities.resolutions)
            raise VideoError("bad_input", f"resolution={resolution!r} not supported (allowed: {allowed})")

        content: list[dict] = [{"type": "text", "text": motion_prompt}]
        for asset_id in kyc_image_ids:
            if asset_id:
                content.append({"type": "kycImageAssetId", "assetId": asset_id})
        if kyc_audio_id:
            content.append({"type": "kycAudioAssetId", "assetId": kyc_audio_id})
        if kyc_video_id:
            content.append({"type": "kycVideoAssetId", "assetId": kyc_video_id})

        body: dict = {
            "model": self.upstream_model_id,
            "content": content,
            "duration": duration,
            "resolution": resolution,
            "ratio": aspect,
        }
        generate_audio = params.get("generate_audio")
        if generate_audio is not None and self.capabilities.supports_audio_toggle:
            body["generateAudio"] = bool(generate_audio)
        # A person-driven gen carries no first/last frame, so the 2.5 ratio-drop
        # rule cannot apply here.
        self._apply_25_options(body, params, warnings, has_keyframe=False)
        return await self._post_generation(body, headers, warnings, unmoderated=unmoderated)

    # ── poll ──────────────────────────────────────────────────────────

    async def poll(self, external_job_id: str) -> VideoGenPollResult:
        headers = self._headers()
        unmoderated = external_job_id.startswith(B2B_JOB_PREFIX)
        job_id = external_job_id[len(B2B_JOB_PREFIX):] if unmoderated else external_job_id
        _create, _get, video_base = _b2b_urls(unmoderated)
        poll_url = f"{video_base}/video/tasks/{job_id}"
        async with _http_client_factory() as client:
            try:
                resp = await client.get(poll_url, headers=headers)
            except httpx.HTTPError as exc:
                raise VideoError("internal", f"avis poll transport error: {exc}") from exc
        if resp.status_code == 404:
            return {
                "status": "failed",
                "video_bytes": None,
                "video_url": None,
                "error": "bad_input",
                "error_message": "task not found (expired or invalid id)",
                "error_raw": _safe_json(resp),
                "cost_usd": 0.0,
                "cost_tokens": None,
            }
        if resp.status_code >= 400:
            raise _classify_avis_http_error(resp)
        data = _unwrap(resp)

        status = data.get("status")
        if status in {"queued", "running"}:
            return {
                "status": status,
                "video_bytes": None,
                "video_url": None,
                "error": None,
                "cost_usd": 0.0,
                "cost_tokens": None,
            }
        if status == "succeeded":
            return await self._on_succeeded(
                data, client_factory=_http_client_factory, unmoderated=unmoderated
            )
        if status in {"failed", "cancelled"}:
            err = data.get("error")
            err_msg = err if isinstance(err, str) else (
                (err or {}).get("message") if isinstance(err, dict) else None
            )
            code: VideoErrorCode = "internal"
            low = (str(err_msg) or "").lower()
            if "safety" in low or "filter" in low or "content" in low:
                code = "content_filtered"
            elif "quota" in low or "rate" in low or "limit" in low:
                code = "quota"
            elif "invalid" in low or "param" in low or "download" in low:
                code = "bad_input"
            return {
                "status": "failed",
                "video_bytes": None,
                "video_url": None,
                "error": code,
                "error_message": (str(err_msg)[:200] if err_msg else f"task {status}"),
                "error_raw": data,
                "cost_usd": 0.0,
                "cost_tokens": None,
            }
        logger.warning("avis poll: unknown status %r in payload %s", status, data)
        return {
            "status": "running",
            "video_bytes": None,
            "video_url": None,
            "error": None,
            "cost_usd": 0.0,
            "cost_tokens": None,
        }

    async def _on_succeeded(
        self, data: dict, *, client_factory, unmoderated: bool = False
    ) -> VideoGenPollResult:
        """Download bytes eagerly (Avis downloadUrl is a presigned R2 link that
        expires) + read provider-reported cost from ``usage.usdCost``."""
        video_url = data.get("downloadUrl") or data.get("videoUrl")
        if not isinstance(video_url, str) or not video_url:
            logger.error("avis poll succeeded but no videoUrl/downloadUrl: %s", data)
            return {
                "status": "failed",
                "video_bytes": None,
                "video_url": None,
                "error": "internal",
                "error_message": "succeeded envelope missing videoUrl/downloadUrl",
                "error_raw": data,
                "cost_usd": 0.0,
                "cost_tokens": None,
            }
        async with client_factory() as client:
            try:
                vresp = await client.get(video_url)
            except httpx.HTTPError as exc:
                raise VideoError(
                    "internal", f"avis video download failed: {exc}", raw=data
                ) from exc
        if vresp.status_code >= 400:
            raise VideoError(
                "internal", f"avis video download HTTP {vresp.status_code}", raw=data
            )
        video_bytes = vresp.content

        usage = data.get("usage") or {}
        tokens = None
        usd_cost: Optional[float] = None
        if isinstance(usage, dict):
            raw_tokens = usage.get("completionTokens") or usage.get("totalTokens")
            if isinstance(raw_tokens, (int, float)):
                tokens = int(raw_tokens)
            raw_cost = usage.get("usdCost")
            if isinstance(raw_cost, (int, float)):
                usd_cost = float(raw_cost)
        # The poll envelope omits usdCost — look it up from the generations
        # history so per-user budgets settle on the real charge.
        if usd_cost is None:
            gen_id = data.get("generationId")
            if isinstance(gen_id, str) and gen_id:
                usd_cost = await _fetch_actual_usd_cost(gen_id, unmoderated=unmoderated)
        # Prefer the real USD cost; fall back to local token pricing.
        cost_usd = usd_cost if usd_cost is not None else compute_cost_usd(self.model_id, tokens=tokens)

        return {
            "status": "succeeded",
            "video_bytes": video_bytes,
            "video_url": video_url,
            "error": None,
            "error_message": None,
            "error_raw": None,
            "duration_seconds": float(data.get("duration") or 0) or None,
            "cost_usd": cost_usd,
            "cost_tokens": tokens,
            "media_metadata": {
                "model": data.get("model"),
                "resolution": data.get("resolution"),
                "duration": data.get("duration"),
                "providerId": data.get("providerId"),
                "generationId": data.get("generationId"),
            },
            "raw": data,
        }

    # ── run_to_completion ─────────────────────────────────────────────

    async def run_to_completion(
        self, params: VideoGenSubmitParams
    ) -> tuple[VideoGenSubmitResult, VideoGenPollResult]:
        submit_result = await self.submit(params)
        job_id = submit_result["external_job_id"]
        attempts = 0
        transport_errs = 0
        while attempts < AVIS_POLL_MAX_CYCLES:
            # Ramp: start at AVIS_POLL_FIRST_S, add ~1s each cycle, cap at
            # AVIS_POLL_INTERVAL_S. Fast early → detects short clips quickly;
            # backs off so long renders don't hammer the API.
            delay = min(AVIS_POLL_INTERVAL_S, AVIS_POLL_FIRST_S + attempts * 1.0)
            await asyncio.sleep(delay)
            attempts += 1
            try:
                poll = await self.poll(job_id)
            except VideoError as exc:
                # A single network/TLS/timeout blip while polling must not kill a
                # gen that's still rendering on Avis. Tolerate a run of them and
                # only give up if they persist (a real outage).
                if "poll transport error" in str(exc):
                    transport_errs += 1
                    if transport_errs >= AVIS_POLL_TRANSPORT_MAX:
                        raise
                    logger.warning(
                        "avis poll transport blip %d/%d (job %s), retrying: %s",
                        transport_errs,
                        AVIS_POLL_TRANSPORT_MAX,
                        job_id,
                        exc,
                    )
                    continue
                raise
            transport_errs = 0
            if poll.get("status") in {"succeeded", "failed", "cancelled"}:
                return submit_result, poll
        return submit_result, {
            "status": "failed",
            "video_bytes": None,
            "video_url": None,
            "error": "timeout",
            "error_message": f"local poll exhausted after {attempts} cycles",
            "error_raw": {"external_job_id": job_id},
            "cost_usd": 0.0,
            "cost_tokens": None,
        }


# ── envelope + error helpers ────────────────────────────────────────────


def _unwrap(resp: httpx.Response) -> dict:
    """Pull the ``data`` object out of Avis's ``{data, success, status}`` envelope.

    Tolerates a bare (unwrapped) object too, so a future API shape change
    degrades gracefully instead of KeyError-ing.
    """
    try:
        payload = resp.json()
    except ValueError as exc:
        raise VideoError("internal", f"avis returned non-JSON: {resp.text[:200]}") from exc
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            return data
        return payload
    raise VideoError("internal", f"avis returned unexpected JSON shape: {str(payload)[:200]}")


def _safe_json(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {"raw": data}
    except (ValueError, AttributeError):
        return {"text": resp.text[:500]}


def _redact_body_for_log(body: dict) -> dict:
    """A generation body with the base64 blobs swapped for their size.

    Keeps every key and every content part, so the log answers "what shape did
    we actually send" — which field was present, which role each part carried —
    without dumping megabytes of image data into it.
    """
    out: dict = {}
    for k, v in body.items():
        if k != "content" or not isinstance(v, list):
            out[k] = v
            continue
        parts = []
        for c in v:
            if not isinstance(c, dict):
                parts.append(c)
                continue
            part = {}
            for ck, cv in c.items():
                if isinstance(cv, str) and len(cv) > 120:
                    part[ck] = f"<{len(cv)} chars>"
                else:
                    part[ck] = cv
            parts.append(part)
        out[k] = parts
    return out


def _classify_avis_http_error(resp: httpx.Response) -> VideoError:
    """Map an Avis ``{errors:[...], status, success:false}`` body onto VideoError."""
    payload = _safe_json(resp)
    errs = payload.get("errors") if isinstance(payload, dict) else None
    msg = ""
    if isinstance(errs, list) and errs:
        msg = "; ".join(str(e) for e in errs)[:300]
    elif isinstance(payload, dict):
        msg = str(payload.get("detail") or payload.get("title") or "")[:300]
    low = msg.lower()
    if resp.status_code in (401, 403):
        req_url = ""
        try:
            req_url = str(resp.request.url)
        except (AttributeError, RuntimeError):
            req_url = ""
        if "/b2b/" in req_url:
            return VideoError(
                "auth",
                msg or "B2B unmoderated requires a DanceSee account with userType=B2B",
                raw=payload,
            )
        return VideoError("auth", msg or "unauthorized", raw=payload)
    if resp.status_code == 404:
        return VideoError("bad_input", msg or "not found", raw=payload)
    if resp.status_code == 429:
        return VideoError("quota", msg or "rate limited", raw=payload)
    if resp.status_code == 400:
        if "filter" in low or "safety" in low:
            return VideoError("content_filtered", msg, raw=payload)
        return VideoError("bad_input", msg or "bad request", raw=payload)
    return VideoError("internal", msg or f"HTTP {resp.status_code}", raw=payload)


# ── helper: resolve a Flowboard media_id to an Avis-reachable URL ───────


# A reference/edit VIDEO must be 23.8–60 FPS for Seedance — anime/manga refs are
# frequently 12fps ("Frame rate must be between 23.8 FPS and 60 FPS"). Re-encode
# to 24fps (same duration, frame-duplicated) before hoisting when out of range.
_REF_VIDEO_MIN_FPS = 23.8
_REF_VIDEO_MAX_FPS = 60.0
_REF_VIDEO_TARGET_FPS = 24
_REF_VIDEO_EXTS = (".mp4", ".mov", ".webm", ".m4v", ".avi", ".mkv")


def _fit_ref_video_fps(path: Path) -> tuple[Path, bool]:
    """Return (path, changed). Re-encode to 24fps if the video's FPS is outside
    23.8–60; else return the original. Any probe/encode failure → original
    (let Avis validate). The re-encoded copy is cached by name."""
    import subprocess

    from flowboard.services.frame_extract import FFMPEG_BIN, FFPROBE_BIN

    try:
        pr = subprocess.run(
            [FFPROBE_BIN, "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=avg_frame_rate", "-of", "default=nk=1:nw=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        raw = (pr.stdout or "").strip()
        num, _, den = raw.partition("/")
        fps = (float(num) / float(den)) if (den and float(den)) else float(num or 0)
    except Exception:  # noqa: BLE001
        return path, False
    if fps <= 0 or (_REF_VIDEO_MIN_FPS <= fps <= _REF_VIDEO_MAX_FPS):
        return path, False
    dst = media_service.MEDIA_CACHE_DIR / f"fps24_{path.stem}.mp4"
    if dst.exists() and dst.stat().st_size > 0:
        return dst, True
    try:
        subprocess.run(
            [FFMPEG_BIN, "-y", "-i", str(path), "-r", str(_REF_VIDEO_TARGET_FPS),
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
             "-an", str(dst)],
            capture_output=True, text=True, timeout=300, check=True,
        )
    except Exception:  # noqa: BLE001
        return path, False
    return (dst, True) if (dst.exists() and dst.stat().st_size > 0) else (path, False)


def media_id_to_public_url(
    media_id: str,
    *,
    project_id: Optional[str] = None,
    asset_id: Optional[str] = None,
) -> str:
    """Hoist a local Flowboard ``media_id`` to a public URL via R2 so Avis (and
    its BytePlus backend) can fetch it. Mirrors the Dreamina-direct helper.

    A reference/edit VIDEO is re-encoded to a Seedance-legal FPS first; the
    re-encoded copy hoists under its own key so the original R2 object is kept."""
    local = media_service.cached_path(media_id)
    if local is None:
        raise VideoError(
            "bad_input",
            f"media_id {media_id!r} has no local cache file — fetch it first",
        )
    hoist_path = Path(local)
    hoist_id = asset_id or media_id
    if hoist_path.suffix.lower() in _REF_VIDEO_EXTS:
        fitted, changed = _fit_ref_video_fps(hoist_path)
        if changed:
            hoist_path = fitted
            hoist_id = f"{asset_id or media_id}-fps24"
    try:
        return prepare_image_url(hoist_path, project_id=project_id, asset_id=hoist_id)
    except ObjectStorageError as exc:
        raise VideoError("bad_input", str(exc)) from exc
