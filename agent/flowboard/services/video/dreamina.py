"""BytePlus ARK / Dreamina Seedance ``VideoProvider``.

Implementation follows the contract in ``docs/dreamina_api_contract.md``
(probed 2026-05-21 on the ap-southeast-1 BytePlus endpoint). All API
shapes below are evidence-backed; deviations from upstream surface in
the existing sample fixtures.

Two registered models share this class:

- ``seedance-1-5-pro`` (upstream ``seedance-1-5-pro-251215``): i2v only.
  Passing ``reference_images`` triggers a capability-drop warning.
- ``seedance-2-0`` (upstream ``dreamina-seedance-2-0-260128``): r2v +
  audio refs. Verified live on 2026-05-25 (contract §11). ``submit``
  dispatches three content shapes — see ``_DISPATCH_MODES`` discussion
  in ``submit``: i2v (legacy), r2v (reference_image multi-ref), and
  r2v+audio (reference_image + reference_audio).

Key design points:

1. **Inline-flag prompt builder** — ``--rt W:H --rs Np`` get appended to
   the user's motion_prompt before submit (the API consumes them, then
   echoes the parsed values back in the poll envelope). r2v honors them
   too: a live probe on 2026-05-25 (--rt 9:16 + 2 reference_image refs)
   returned a genuine 720×1280 vertical clip, so the flags apply in ALL
   modes (this resolved the §11.7 "untested on r2v" open question).
2. **@imageN injection** — r2v binds reference semantics through the
   prompt text (§11.2): ``inject_image_labels`` prepends positional
   ``@imageN`` tags, post-capability-gate so labels match the final
   reference_image block count.
3. **Eager download on success** — the signed TOS URL has a 24h
   expiry that aligns with file lifecycle, so ``poll`` GETs the bytes
   before returning ``status: succeeded``.
4. **Self-cap concurrent submits at 3** — no rate-limit headers are
   exposed; the contract observed 3.5 min latency under 3-job load.
5. **Image hosting via R2** — Dreamina requires ``image_url.url`` (and
   ``audio_url.url``) to be publicly reachable; ``media_id_to_public_url``
   mirrors local media to a Cloudflare R2 bucket and returns a presigned URL.
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
    supports_audio_ref=False,
    max_refs=0,
    aspect_ratios=("1:1", "16:9", "9:16"),
    resolutions=("720p", "1080p"),
    durations=(5, 8, 10),
)

# Seedance 2.0 r2v + audio. Verified live on 2026-05-25 (contract §11):
# role="reference_image" multi-ref accepted (≥3 confirmed), audio via
# role="reference_audio", 5s/8s durations confirmed. max_refs=9 per the
# Dreamina UI matrix. --rt/--rs ARE honored on r2v (live probe returned
# 720×1280 for --rt 9:16), so the inline flags apply in all modes.
SEEDANCE_2_0_CAPABILITY = VideoProviderCapability(
    supports_multi_ref=True,
    supports_last_frame=True,
    supports_audio_toggle=True,
    supports_audio_ref=True,
    max_refs=9,
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
        last_frame_url = params.get("last_frame_url")
        audio_ref_url = (params.get("audio_ref_url") or "").strip() or None

        # ── capability gate (drop-with-warning, never silent) ───────────
        reference_images = [r for r in (params.get("reference_images") or []) if isinstance(r, str) and r]
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

        if audio_ref_url and not self.capabilities.supports_audio_ref:
            warnings.append(
                f"Dropped audio reference: {self.entry.display_name} "
                f"doesn't support reference_audio. Switch to Seedance 2.0."
            )
            audio_ref_url = None

        # ── mode detection (POST-gate so @imageN labels match the final
        #    reference_image block count — mitigates positional desync) ──
        #
        #   i2v       : single image as first_frame (legacy 1.5 path, and
        #               2.0 with <2 refs + no audio per contract §11.7).
        #   r2v       : ≥2 reference_image blocks, NO first_frame.
        #   r2v+audio : reference_image block(s) + one reference_audio. NO
        #               first_frame (audio = "reference media mode", §11.3).
        if audio_ref_url:
            mode = "r2v+audio"
            # Audio must pair with a reference_image, never a first_frame.
            if not reference_images and first_frame_url:
                reference_images = [first_frame_url]
                warnings.append(
                    "Audio reference present but no reference images — "
                    "promoted the start image to @image1 (audio mode forbids first_frame)."
                )
            if not reference_images:
                raise VideoError(
                    "bad_input",
                    "audio reference requires at least one reference image",
                )
        elif self.capabilities.supports_multi_ref and len(reference_images) > 1:
            mode = "r2v"
            if first_frame_url:
                warnings.append(
                    "Ignored the upstream start frame in r2v mode — references "
                    "drive generation; first_frame is not sent with reference media."
                )
        else:
            mode = "i2v"
            # On a multi-ref model, a lone ref with no start frame becomes
            # the first_frame; a lone ref alongside a start frame is the
            # ambiguous case — keep i2v, drop the extra ref with a warning.
            if reference_images:
                if not first_frame_url:
                    first_frame_url = reference_images[0]
                else:
                    warnings.append(
                        f"Ignored {len(reference_images)} reference image(s) in i2v mode "
                        f"— attach ≥2 refs for r2v, or remove the upstream start frame."
                    )
                reference_images = []

        if mode == "i2v" and not first_frame_url:
            raise VideoError(
                "bad_input",
                "Dreamina i2v submit requires first_frame_url (publicly-reachable HTTPS or data: URL)",
            )

        # last_frame gate: only valid in i2v. Reference-media modes (r2v /
        # r2v+audio) forbid mixing first/last frame with reference content.
        if last_frame_url and not self.capabilities.supports_last_frame:
            warnings.append(
                f"Dropped last_frame: {self.entry.display_name} doesn't "
                f"support keyframe interpolation."
            )
            last_frame_url = None
        elif last_frame_url and mode != "i2v":
            warnings.append(
                "Dropped last_frame: cannot mix keyframe interpolation with "
                "reference-media (r2v) content on this submit."
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

        # Build the prompt. For reference-media modes we inject positional
        # @imageN tags (skipped if the caller's prompt already carries them)
        # so each reference_image block binds to its label. Then Dreamina's
        # inline flags: duration is a top-level field per §2.3; ratio +
        # resolution go inline. NOTE: --rt/--rs are UNTESTED on 2.0 r2v
        # (contract §11.7) — kept on faith, gate on the live shot test.
        prompt_text = motion_prompt
        if mode != "i2v":
            prompt_text = inject_image_labels(prompt_text, len(reference_images))
        final_prompt = build_dreamina_prompt(
            prompt_text, aspect_ratio=aspect, resolution=resolution
        )

        # Assemble content blocks per mode.
        content: list[dict] = [{"type": "text", "text": final_prompt}]
        if mode == "i2v":
            if last_frame_url:
                # Two-image keyframe interpolation — roles required (§2.6).
                content.append({
                    "type": "image_url",
                    "image_url": {"url": first_frame_url},
                    "role": "first_frame",
                })
                content.append({
                    "type": "image_url",
                    "image_url": {"url": last_frame_url},
                    "role": "last_frame",
                })
            else:
                # Single-image submit — role MUST be omitted per §2.6.
                content.append({
                    "type": "image_url",
                    "image_url": {"url": first_frame_url},
                })
        else:
            # r2v / r2v+audio: ordered reference_image blocks (NO first_frame).
            # Array order IS the @imageN positional binding.
            for ref in reference_images:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": ref},
                    "role": "reference_image",
                })
            if audio_ref_url:
                content.append({
                    "type": "audio_url",
                    "audio_url": {"url": audio_ref_url},
                    "role": "reference_audio",
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

# Detects any pre-existing @imageN token so we don't double-tag a prompt
# that Phase 6 synthesis already composed with rich '@image1 = Kenji' lines.
_IMAGE_LABEL_RE = re.compile(r"@image\d+", re.IGNORECASE)


def inject_image_labels(motion_prompt: str, n_refs: int) -> str:
    """Prepend ``@image1 @image2 … @imageN`` positional tags to the prompt.

    Seedance 2.0 binds reference semantics through the TEXT, not the role
    (contract §11.2): ``@imageN`` maps to the Nth ``reference_image`` block
    in array order. This helper injects the bare tags so the model knows
    how many refs to expect and in what order.

    Idempotent / non-destructive:

    - ``n_refs <= 0`` → returned unchanged (no refs to label).
    - If the prompt already contains any ``@imageN`` token, it's returned
      unchanged — the caller (e.g. Phase 6 prompt synthesis) is assumed to
      have authored richer, semantically-described labels and we must not
      clobber or duplicate them.
    """
    if n_refs <= 0:
        return motion_prompt
    if _IMAGE_LABEL_RE.search(motion_prompt or ""):
        return motion_prompt
    tags = " ".join(f"@image{i}" for i in range(1, n_refs + 1))
    body = (motion_prompt or "").strip()
    return f"{tags} {body}".strip()


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
