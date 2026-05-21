"""BytePlus ARK / Dreamina Seedance ``VideoProvider``.

Implementation follows the contract in ``docs/dreamina_api_contract.md``
(probed 2026-05-21 on the ap-southeast-1 BytePlus endpoint). All API
shapes below are evidence-backed; deviations from upstream surface in
the existing sample fixtures.

Two registered models share this class:

- ``seedance-1-5-pro`` (upstream ``seedance-1-5-pro-251215``): i2v only.
  Passing ``reference_images`` triggers a capability-drop warning.
- ``seedance-2-0`` (upstream ``dreamina-seedance-2-0-260128``): r2v +
  audio refs + last_frame. Not yet activated on the user's BytePlus
  account; tests mock the multi-ref path, real e2e deferred per Phase 5
  stop-point notes.

Key design points:

1. **Inline-flag prompt builder** — ``--rt W:H --rs Np`` get appended to
   the user's motion_prompt before submit (the API consumes them, then
   echoes the parsed values back in the poll envelope).
2. **Eager download on success** — the signed TOS URL has a 24h
   expiry that aligns with file lifecycle, so ``poll`` GETs the bytes
   before returning ``status: succeeded``.
3. **Self-cap concurrent submits at 3** — no rate-limit headers are
   exposed; the contract observed 3.5 min latency under 3-job load.
4. **Image hosting via R2** — Dreamina requires ``image_url.url`` to be
   publicly reachable; ``prepare_image_url`` mirrors local media to a
   Cloudflare R2 bucket and returns a presigned URL.
"""
from __future__ import annotations

import asyncio
import logging
import re
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


# Endpoint base. Region is encoded in the host; if BytePlus adds other
# regions, expose as a config knob then.
BASE_URL = "https://ark.ap-southeast.bytepluses.com/api/v3"

# Polling cadence — the contract says status moves running → succeeded
# in one step, so polling more aggressively wastes API calls.
DREAMINA_POLL_INTERVAL_S = 15.0
DREAMINA_POLL_MAX_CYCLES = 30  # 30 × 15s = 7.5 min wall ceiling; >> the 90-220s typical


# Concurrency cap. Process-local — multi-worker deployments would need
# distributed coordination, but Phase 5 deploys single-worker.
_CONCURRENCY_SEM = asyncio.Semaphore(3)


SEEDANCE_1_5_PRO_CAPABILITY = VideoProviderCapability(
    supports_multi_ref=False,
    supports_last_frame=True,
    supports_audio_toggle=False,
    max_refs=0,
    aspect_ratios=("1:1", "16:9", "9:16"),
    resolutions=("720p", "1080p"),
    durations=(5, 8, 10),
)

# Hypothetical r2v capabilities — the user hasn't activated seedance-2-0
# yet, so these are conservative defaults derived from the contract's
# §2.5 / §7 notes ("r2v supports role: reference_image array"). Update
# once we have a real probe.
SEEDANCE_2_0_CAPABILITY = VideoProviderCapability(
    supports_multi_ref=True,
    supports_last_frame=True,
    supports_audio_toggle=True,
    max_refs=4,
    aspect_ratios=("1:1", "16:9", "9:16"),
    resolutions=("720p", "1080p"),
    durations=(5, 8, 10),
)


# Test seam: monkeypatch ``_http_client_factory`` to a sync httpx.MockTransport
# instance so the provider doesn't talk to live ARK during unit tests.
_http_client_factory = lambda: httpx.AsyncClient(timeout=60.0)


def set_http_client_factory(factory) -> None:
    """Test hook — providers reset to live factory on production."""
    global _http_client_factory
    _http_client_factory = factory


def reset_http_client_factory() -> None:
    global _http_client_factory
    _http_client_factory = lambda: httpx.AsyncClient(timeout=60.0)


class DreaminaVideoProvider:
    """One instance per registered model. Capability declaration comes
    from the registry entry; upstream model id is the BytePlus full id
    (e.g. ``seedance-1-5-pro-251215``)."""

    name = "dreamina"

    def __init__(self, entry) -> None:
        self.entry = entry
        self.capabilities = entry.capabilities
        self.upstream_model_id = entry.upstream_model_id
        self.model_id = entry.model_id  # registry key, used for pricing lookup

    async def is_available(self) -> bool:
        return bool(secrets.get_api_key("dreamina"))

    # ── submit ────────────────────────────────────────────────────────

    async def submit(self, params: VideoGenSubmitParams) -> VideoGenSubmitResult:
        api_key = secrets.get_api_key("dreamina")
        if not api_key:
            raise VideoError(
                "auth",
                "Dreamina API key not configured — set apiKeys.dreamina in ~/.flowboard/secrets.json",
            )

        # Validate + capability-degrade params. Per the user's
        # 'persistent warning, no silent drop' decision: refs that the
        # model doesn't support are dropped BUT a warning is surfaced
        # all the way through to Request.result.warnings so the UI can
        # display a non-dismissible banner.
        warnings: list[str] = []

        motion_prompt = (params.get("motion_prompt") or "").strip()
        if not motion_prompt:
            raise VideoError("bad_input", "missing motion_prompt")

        first_frame_url = (params.get("first_frame_url") or "").strip()
        if not first_frame_url:
            raise VideoError(
                "bad_input",
                "Dreamina submit requires first_frame_url (publicly-reachable HTTPS or data: URL)",
            )

        reference_images = list(params.get("reference_images") or [])
        if reference_images and not self.capabilities.supports_multi_ref:
            warnings.append(
                f"Dropped {len(reference_images)} reference images: "
                f"{self.entry.display_name} is i2v-only. "
                f"Switch to a model with multi-ref support to use these."
            )
            reference_images = []
        elif reference_images and len(reference_images) > self.capabilities.max_refs:
            cut = self.capabilities.max_refs
            warnings.append(
                f"Truncated reference images from {len(reference_images)} → {cut} "
                f"(model max). Excess refs ignored."
            )
            reference_images = reference_images[:cut]

        last_frame_url = params.get("last_frame_url")
        if last_frame_url and not self.capabilities.supports_last_frame:
            warnings.append(
                f"Dropped last_frame: {self.entry.display_name} doesn't "
                f"support keyframe interpolation."
            )
            last_frame_url = None

        duration = int(params.get("duration_seconds") or 5)
        if duration not in self.capabilities.durations:
            allowed = ", ".join(str(d) for d in self.capabilities.durations)
            raise VideoError(
                "bad_input",
                f"duration_seconds={duration} not supported (allowed: {allowed})",
            )
        aspect = params.get("aspect_ratio") or "1:1"
        if aspect not in self.capabilities.aspect_ratios:
            allowed = ", ".join(self.capabilities.aspect_ratios)
            raise VideoError(
                "bad_input",
                f"aspect_ratio={aspect!r} not supported (allowed: {allowed})",
            )
        resolution = params.get("resolution") or "720p"
        if resolution not in self.capabilities.resolutions:
            allowed = ", ".join(self.capabilities.resolutions)
            raise VideoError(
                "bad_input",
                f"resolution={resolution!r} not supported (allowed: {allowed})",
            )

        generate_audio = params.get("generate_audio")
        if generate_audio is not None and not self.capabilities.supports_audio_toggle:
            warnings.append(
                f"Ignored generate_audio toggle: {self.entry.display_name} "
                f"uses provider default."
            )
            generate_audio = None

        # Build the prompt with Dreamina's inline flags. duration is a
        # top-level field per §2.3; ratio + resolution go inline.
        final_prompt = build_dreamina_prompt(
            motion_prompt, aspect_ratio=aspect, resolution=resolution
        )

        # Assemble content blocks. The first image_url is the first_frame;
        # additional refs are role=reference_image; optional last_frame
        # tagged role=last_frame.
        content: list[dict] = [{"type": "text", "text": final_prompt}]
        if reference_images or last_frame_url:
            # When MORE than one image_url is sent, role is required.
            content.append({
                "type": "image_url",
                "image_url": {"url": first_frame_url},
                "role": "first_frame",
            })
            for ref in reference_images:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": ref},
                    "role": "reference_image",
                })
            if last_frame_url:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": last_frame_url},
                    "role": "last_frame",
                })
        else:
            # Single-image submit — role MUST be omitted per the contract §2.6.
            content.append({
                "type": "image_url",
                "image_url": {"url": first_frame_url},
            })

        body: dict = {
            "model": self.upstream_model_id,
            "duration": duration,
            "content": content,
        }
        if generate_audio is not None:
            body["generate_audio"] = bool(generate_audio)

        await _CONCURRENCY_SEM.acquire()
        try:
            async with _http_client_factory() as client:
                try:
                    resp = await client.post(
                        f"{BASE_URL}/contents/generations/tasks",
                        json=body,
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                    )
                except httpx.HTTPError as exc:
                    raise VideoError(
                        "internal",
                        f"dreamina submit transport error: {exc}",
                    ) from exc
        finally:
            _CONCURRENCY_SEM.release()

        if resp.status_code >= 400:
            raise _classify_dreamina_http_error(resp)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise VideoError(
                "internal",
                f"dreamina submit returned non-JSON: {resp.text[:200]}",
            ) from exc
        task_id = payload.get("id")
        if not isinstance(task_id, str) or not task_id:
            raise VideoError("internal", "dreamina submit missing task id", raw=payload)
        return {
            "external_job_id": task_id,
            "submitted_at": int(time.time()),
            "warnings": warnings,
        }

    # ── poll ──────────────────────────────────────────────────────────

    async def poll(self, external_job_id: str) -> VideoGenPollResult:
        api_key = secrets.get_api_key("dreamina")
        if not api_key:
            raise VideoError("auth", "Dreamina API key not configured")
        async with _http_client_factory() as client:
            try:
                resp = await client.get(
                    f"{BASE_URL}/contents/generations/tasks/{external_job_id}",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            except httpx.HTTPError as exc:
                raise VideoError(
                    "internal",
                    f"dreamina poll transport error: {exc}",
                ) from exc
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
            raise _classify_dreamina_http_error(resp)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise VideoError("internal", f"poll returned non-JSON: {resp.text[:200]}") from exc

        status = payload.get("status")
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
            return await self._on_succeeded(payload, client_factory=_http_client_factory)
        if status in {"failed", "cancelled"}:
            # Contract §3 explicitly notes the failed envelope hasn't
            # been captured in a single probe — log everything and
            # surface a best-effort error code.
            logger.warning(
                "dreamina poll: %s status payload (full envelope): %s",
                status, payload,
            )
            err_block = payload.get("error") or {}
            err_msg = err_block.get("message") if isinstance(err_block, dict) else str(err_block)
            code: VideoErrorCode = "internal"
            if isinstance(err_block, dict):
                err_type = (err_block.get("type") or "").lower()
                err_code = (err_block.get("code") or "").lower()
                if "safety" in err_type or "filter" in err_type or "filter" in err_code:
                    code = "content_filtered"
                elif "auth" in err_type:
                    code = "auth"
                elif "quota" in err_type or "rate" in err_type:
                    code = "quota"
                elif "invalid" in err_type or "param" in err_type:
                    code = "bad_input"
            return {
                "status": "failed",
                "video_bytes": None,
                "video_url": None,
                "error": code,
                "error_message": str(err_msg)[:200] if err_msg else f"task {status}",
                "error_raw": payload,
                "cost_usd": 0.0,
                "cost_tokens": None,
            }
        # Unknown status — defensive: treat as still-running, log loudly.
        logger.warning("dreamina poll: unknown status %r in payload %s", status, payload)
        return {
            "status": "running",
            "video_bytes": None,
            "video_url": None,
            "error": None,
            "cost_usd": 0.0,
            "cost_tokens": None,
        }

    async def _on_succeeded(self, payload: dict, *, client_factory) -> VideoGenPollResult:
        """Download video bytes eagerly + compute cost from token usage.

        Contract §4 / §1: the signed TOS URL aligns with a 24h file
        lifecycle; if Flowboard ever re-derives the URL it could find
        the underlying object gone. Download HERE, persist bytes, never
        rely on the URL after this method returns.
        """
        content = payload.get("content") or {}
        video_url = content.get("video_url") if isinstance(content, dict) else None
        if not isinstance(video_url, str) or not video_url:
            logger.error("dreamina poll succeeded but no video_url in payload: %s", payload)
            return {
                "status": "failed",
                "video_bytes": None,
                "video_url": None,
                "error": "internal",
                "error_message": "succeeded envelope missing content.video_url",
                "error_raw": payload,
                "cost_usd": 0.0,
                "cost_tokens": None,
            }
        async with client_factory() as client:
            try:
                vresp = await client.get(video_url)
            except httpx.HTTPError as exc:
                raise VideoError(
                    "internal",
                    f"dreamina video download failed: {exc}",
                    raw=payload,
                ) from exc
        if vresp.status_code >= 400:
            raise VideoError(
                "internal",
                f"dreamina video download HTTP {vresp.status_code}",
                raw=payload,
            )
        video_bytes = vresp.content

        usage = payload.get("usage") or {}
        tokens = None
        if isinstance(usage, dict):
            raw_tokens = usage.get("completion_tokens") or usage.get("total_tokens")
            if isinstance(raw_tokens, int):
                tokens = raw_tokens
        cost_usd = compute_cost_usd(self.model_id, tokens=tokens)

        media_metadata = {
            "ratio": payload.get("ratio"),
            "resolution": payload.get("resolution"),
            "duration": payload.get("duration"),
            "framespersecond": payload.get("framespersecond"),
            "seed": payload.get("seed"),
            "model": payload.get("model"),
        }
        return {
            "status": "succeeded",
            "video_bytes": video_bytes,
            "video_url": video_url,
            "error": None,
            "error_message": None,
            "error_raw": None,
            "duration_seconds": float(payload.get("duration") or 0) or None,
            "cost_usd": cost_usd,
            "cost_tokens": tokens,
            "media_metadata": media_metadata,
            "raw": payload,
        }

    # ── run_to_completion ─────────────────────────────────────────────

    async def run_to_completion(
        self, params: VideoGenSubmitParams
    ) -> tuple[VideoGenSubmitResult, VideoGenPollResult]:
        submit_result = await self.submit(params)
        job_id = submit_result["external_job_id"]
        attempts = 0
        while attempts < DREAMINA_POLL_MAX_CYCLES:
            await asyncio.sleep(DREAMINA_POLL_INTERVAL_S)
            attempts += 1
            poll = await self.poll(job_id)
            status = poll.get("status")
            if status in {"succeeded", "failed", "cancelled"}:
                return submit_result, poll
        # Local timeout — the upstream task may still finish, but we
        # stop waiting. Worker surfaces this as a `timeout` error code.
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


# ── prompt builder ──────────────────────────────────────────────────────


_FLAG_RE = re.compile(r"\s+--(?:rt|rs)\s+\S+")


def build_dreamina_prompt(
    motion_prompt: str, *, aspect_ratio: str, resolution: str
) -> str:
    """Append --rt and --rs inline flags to the user's prompt text.

    Idempotent: if the user already included one of these flags in their
    prompt, we strip them first to avoid double-application (which the
    API rejects with InvalidParameter).
    """
    cleaned = _FLAG_RE.sub("", motion_prompt or "").strip()
    return f"{cleaned} --rt {aspect_ratio} --rs {resolution}"


# ── HTTP error classification ───────────────────────────────────────────


def _safe_json(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {"raw": data}
    except (ValueError, AttributeError):
        return {"text": resp.text[:500]}


def _classify_dreamina_http_error(resp: httpx.Response) -> VideoError:
    """Map ARK error envelope onto VideoError(uniform code)."""
    payload = _safe_json(resp)
    err_block = payload.get("error") if isinstance(payload, dict) else None
    msg = ""
    err_type = ""
    if isinstance(err_block, dict):
        msg = str(err_block.get("message") or "")[:300]
        err_type = (err_block.get("type") or "").lower()
    if resp.status_code == 401:
        return VideoError("auth", msg or "unauthorized", raw=payload)
    if resp.status_code == 404:
        return VideoError("bad_input", msg or "not found", raw=payload)
    if resp.status_code == 429:
        return VideoError("quota", msg or "rate limited", raw=payload)
    if resp.status_code == 400:
        if "filter" in err_type or "safety" in err_type:
            return VideoError("content_filtered", msg, raw=payload)
        return VideoError("bad_input", msg or "bad request", raw=payload)
    return VideoError(
        "internal",
        msg or f"HTTP {resp.status_code}",
        raw=payload,
    )


# ── helper: resolve a Flowboard media_id to a Dreamina-reachable URL ───


def media_id_to_public_url(
    media_id: str,
    *,
    project_id: Optional[str] = None,
    asset_id: Optional[str] = None,
) -> str:
    """Hoist a local Flowboard ``media_id`` to a public URL via R2.

    Used by the worker when assembling VideoGenSubmitParams from a
    VideoNode's upstream image. The local cache file is uploaded once
    per submit and a presigned URL is returned (1h expiry, comfortably
    longer than Dreamina's 5-10 min generation window).
    """
    local = media_service.cached_path(media_id)
    if local is None:
        raise VideoError(
            "bad_input",
            f"media_id {media_id!r} has no local cache file — fetch it first",
        )
    try:
        return prepare_image_url(
            Path(local),
            project_id=project_id,
            asset_id=asset_id or media_id,
        )
    except ObjectStorageError as exc:
        raise VideoError("bad_input", str(exc)) from exc
