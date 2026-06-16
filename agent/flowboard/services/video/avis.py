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
  variant). Discover via ``GET /ai/models?outputModalities=video``.

Envelope: every 2xx response wraps the payload in ``{data, success, status,
timestamp}``; 4xx errors come back as ``{errors:[...], success:false,
status}``.

Audio reference (``audioInput``) is a supported Avis parameter but its wire
format is undocumented, so v1 drops audio refs with a warning
(``supports_audio_ref=False``). ``generateAudio`` (synthesized track) is fully
supported.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
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

# Local poll cadence + ceiling for a running video task. Seedance 2.0 at
# 1080p / 15s / multi-ref (or person-driven) can take well over 7.5 min, so
# the ceiling is generous (default 160 × 15s = 40 min) and env-overridable.
AVIS_POLL_INTERVAL_S = float(os.getenv("FLOWBOARD_AVIS_POLL_INTERVAL_S", "15"))
AVIS_POLL_MAX_CYCLES = int(os.getenv("FLOWBOARD_AVIS_POLL_MAX_CYCLES", "160"))

# Process-local concurrency cap (mirrors the Dreamina-direct provider).
_CONCURRENCY_SEM = asyncio.Semaphore(3)


# Avis Seedance 2.0 (`dreamina-seedance-2-0`). supportedParameters from
# GET /ai/models: duration, resolution, ratio, seed, watermark, generateAudio,
# audioInput, kycAssetInput. inputModalities: image, video, audio, text.
# audioInput wire-format is undocumented → supports_audio_ref=False in v1.
AVIS_SEEDANCE_2_0_CAPABILITY = VideoProviderCapability(
    supports_multi_ref=True,
    supports_last_frame=True,
    supports_audio_toggle=True,
    supports_audio_ref=False,
    supports_video_ref=True,
    supports_kyc=True,
    max_refs=9,
    aspect_ratios=("1:1", "16:9", "9:16"),
    resolutions=("720p", "1080p"),
    durations=tuple(range(4, 16)),
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


def _encode_local_image_inline(path: Path) -> tuple[str, str]:
    """Return (base64, mediaType) for a local image, shrunk to <=_INLINE_MAX_DIM
    on the longest side and re-encoded as JPEG."""
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


def _read_cached_kyc_asset(media_id: str, asset_type: str) -> Optional[str]:
    """Return a cached, still-active Avis KYC assetId for this media_id, if any."""
    from sqlmodel import select

    from flowboard.db import get_session
    from flowboard.db.models import Asset

    with get_session() as s:
        row = s.exec(select(Asset).where(Asset.uuid_media_id == media_id)).first()
        meta = (row.asset_metadata or {}) if row is not None else {}
        kyc = meta.get("avis_kyc") if isinstance(meta, dict) else None
        if (
            isinstance(kyc, dict)
            and kyc.get("status") == "active"
            and kyc.get("asset_type") == asset_type
            and isinstance(kyc.get("asset_id"), str)
        ):
            return kyc["asset_id"]
    return None


def _write_cached_kyc_asset(media_id: str, asset_type: str, asset_id: str) -> None:
    from sqlmodel import select

    from flowboard.db import get_session
    from flowboard.db.models import Asset

    with get_session() as s:
        row = s.exec(select(Asset).where(Asset.uuid_media_id == media_id)).first()
        if row is None:
            return  # no Asset row to cache on — harmless, re-resolve next time
        meta = dict(row.asset_metadata or {})
        meta["avis_kyc"] = {"asset_id": asset_id, "asset_type": asset_type, "status": "active"}
        row.asset_metadata = meta
        s.add(row)
        s.commit()


async def ensure_kyc_asset(
    media_id: str, asset_type: str, *, project_id: Optional[str] = None
) -> str:
    """Resolve a local media_id to an *active* Avis KYC assetId (cached, reused).

    Hoists the cached file to a public R2 URL, creates the KYC asset, polls until
    ``active``, caches the assetId on the Asset row, and returns it. Raises
    VideoError on R2 misconfig, processing failure, or timeout. The caller must
    have a KYC-verified account (``isKyc: true``) or creation returns 403.
    """
    if asset_type not in _KYC_ASSET_TYPES:
        raise VideoError("internal", f"bad KYC asset_type: {asset_type!r}")

    cached = _read_cached_kyc_asset(media_id, asset_type)
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

    async with _http_client_factory() as client:
        try:
            resp = await client.post(
                f"{BASE_URL}/kyc/user/assets",
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
                    f"{BASE_URL}/kyc/user/assets/{asset_id}", headers=_kyc_headers()
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

    _write_cached_kyc_asset(media_id, asset_type, asset_id)
    return asset_id


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

        # ── person-driven (KYC) path ────────────────────────────────────
        # Pre-resolved Avis KYC assetIds (the worker creates them from local
        # media_ids). When any is present this is portrait→video / lip-sync /
        # video-reference: emit kyc*AssetId parts and skip the regular refs.
        kyc_ids = {
            "kycImageAssetId": params.get("kyc_image_asset_id"),
            "kycAudioAssetId": params.get("kyc_audio_asset_id"),
            "kycVideoAssetId": params.get("kyc_video_asset_id"),
        }
        if any(kyc_ids.values()):
            if not self.capabilities.supports_kyc:
                warnings.append(
                    f"Dropped KYC assets: {self.entry.display_name} doesn't "
                    f"support person-driven video."
                )
            else:
                return await self._submit_kyc(params, motion_prompt, kyc_ids, headers, warnings)

        first_frame_url = (params.get("first_frame_url") or "").strip()
        last_frame_url = params.get("last_frame_url")
        audio_ref_url = (params.get("audio_ref_url") or "").strip() or None

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

        if audio_ref_url and not self.capabilities.supports_audio_ref:
            warnings.append(
                "Dropped audio reference: the Avis adapter doesn't wire "
                "audioInput yet (use generateAudio for a synthesized track)."
            )
            audio_ref_url = None

        # ── mode detection ──────────────────────────────────────────────
        #   r2v : ≥2 reference images OR any reference video (reference media).
        #   i2v : single start frame (+ optional last frame).
        if self.capabilities.supports_multi_ref and (
            len(reference_images) > 1 or reference_videos
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
        if mode == "i2v":
            content.append(_image_content_part(first_frame_url, "firstFrame"))
            if last_frame_url:
                content.append(_image_content_part(last_frame_url, "lastFrame"))
        else:
            for ref in reference_images:
                content.append(_image_content_part(ref, "referenceImage"))
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

        body: dict = {
            "model": self.upstream_model_id,
            "content": content,
            "duration": duration,
            "resolution": resolution,
            "ratio": aspect,
        }
        if generate_audio is not None:
            body["generateAudio"] = bool(generate_audio)

        return await self._post_generation(body, headers, warnings)

    async def _post_generation(
        self, body: dict, headers: dict, warnings: list[str]
    ) -> VideoGenSubmitResult:
        """POST a built /video/generations body; return the submit result."""
        await _CONCURRENCY_SEM.acquire()
        try:
            async with _http_client_factory() as client:
                try:
                    resp = await client.post(
                        f"{BASE_URL}/video/generations", json=body, headers=headers
                    )
                except httpx.HTTPError as exc:
                    raise VideoError("internal", f"avis submit transport error: {exc}") from exc
        finally:
            _CONCURRENCY_SEM.release()

        if resp.status_code >= 400:
            raise _classify_avis_http_error(resp)
        data = _unwrap(resp)
        task_id = data.get("taskId")
        if not isinstance(task_id, str) or not task_id:
            raise VideoError("internal", "avis submit missing taskId", raw=data)
        return {
            "external_job_id": task_id,
            "submitted_at": int(time.time()),
            "warnings": warnings,
        }

    async def _submit_kyc(
        self,
        params: VideoGenSubmitParams,
        motion_prompt: str,
        kyc_ids: dict,
        headers: dict,
        warnings: list[str],
    ) -> VideoGenSubmitResult:
        """Person-driven submit: text + kyc*AssetId content parts (no regular refs)."""
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
        for part_type, asset_id in kyc_ids.items():
            if asset_id:
                content.append({"type": part_type, "assetId": asset_id})

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
        return await self._post_generation(body, headers, warnings)

    # ── poll ──────────────────────────────────────────────────────────

    async def poll(self, external_job_id: str) -> VideoGenPollResult:
        headers = self._headers()
        async with _http_client_factory() as client:
            try:
                resp = await client.get(
                    f"{BASE_URL}/video/tasks/{external_job_id}", headers=headers
                )
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
            return await self._on_succeeded(data, client_factory=_http_client_factory)
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

    async def _on_succeeded(self, data: dict, *, client_factory) -> VideoGenPollResult:
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
        # Prefer the provider-reported USD cost; fall back to local token pricing.
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
        while attempts < AVIS_POLL_MAX_CYCLES:
            await asyncio.sleep(AVIS_POLL_INTERVAL_S)
            attempts += 1
            poll = await self.poll(job_id)
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


def media_id_to_public_url(
    media_id: str,
    *,
    project_id: Optional[str] = None,
    asset_id: Optional[str] = None,
) -> str:
    """Hoist a local Flowboard ``media_id`` to a public URL via R2 so Avis (and
    its BytePlus backend) can fetch it. Mirrors the Dreamina-direct helper."""
    local = media_service.cached_path(media_id)
    if local is None:
        raise VideoError(
            "bad_input",
            f"media_id {media_id!r} has no local cache file — fetch it first",
        )
    try:
        return prepare_image_url(
            Path(local), project_id=project_id, asset_id=asset_id or media_id
        )
    except ObjectStorageError as exc:
        raise VideoError("bad_input", str(exc)) from exc
