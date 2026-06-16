"""In-process worker that drains queued generation requests.

Scope for Run 3 (Phase 2 bridge): a single handler type `"proxy"` that
forwards `params = {url, method?, headers?, body?}` through the extension
via ``flow_client.api_request``. Further types (gen_image, gen_video,
upload_image, etc.) land in later runs once the full Flow protocol + captcha
round-trip is ported.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from flowboard.db import get_session
from flowboard.db.models import Request
from flowboard.services import media as media_service
from flowboard.services.flow_client import flow_client
from flowboard.services.flow_sdk import get_flow_sdk

logger = logging.getLogger(__name__)


# type → coroutine(params) → (result_dict, error_or_None)
Handler = Callable[[dict], Awaitable[tuple[dict, Optional[str]]]]


_ALLOWED_URL_PREFIXES: tuple[str, ...] = (
    "https://aisandbox-pa.googleapis.com/",
)


async def _handle_proxy(params: dict) -> tuple[dict, Optional[str]]:
    url = params.get("url")
    method = params.get("method", "POST")
    if not isinstance(url, str) or not url:
        return {}, "missing_url"
    # Defense-in-depth: refuse to proxy URLs outside the expected allowlist
    # even if the extension's own check was somehow bypassed.
    if not any(url.startswith(p) for p in _ALLOWED_URL_PREFIXES):
        return {}, "url_not_allowed"
    resp = await flow_client.api_request(
        url=url,
        method=method,
        headers=params.get("headers") or {},
        body=params.get("body"),
    )
    if not isinstance(resp, dict):
        return {"value": resp}, None
    if resp.get("error"):
        return resp, str(resp["error"])[:200]
    status = resp.get("status")
    if isinstance(status, int) and status >= 400:
        return resp, f"API_{status}"
    return resp, None


async def _handle_create_project(params: dict) -> tuple[dict, Optional[str]]:
    name = params.get("name") or params.get("title") or "Untitled"
    if not isinstance(name, str) or not name.strip():
        return {}, "missing_name"
    tool = params.get("tool", "PINHOLE")
    resp = await get_flow_sdk().create_project(name.strip(), tool)
    if resp.get("error"):
        return resp, str(resp["error"])[:200]
    return resp, None


async def _handle_gen_image(params: dict) -> tuple[dict, Optional[str]]:
    from flowboard.services.flow_sdk import is_valid_project_id

    prompt = params.get("prompt")
    project_id = params.get("project_id")
    if not isinstance(prompt, str) or not prompt.strip():
        return {}, "missing_prompt"
    if not isinstance(project_id, str) or not project_id.strip():
        return {}, "missing_project_id"
    project_id = project_id.strip()
    if not is_valid_project_id(project_id):
        return {}, "invalid_project_id"
    aspect = params.get("aspect_ratio") or "IMAGE_ASPECT_RATIO_LANDSCAPE"
    # Tier resolution: caller-stamped value first (set at dispatch time),
    # then the live value from `flow_client` (resolved authoritatively
    # via /v1/credits on token capture). NO silent default — if both
    # are absent we fail loud with `paygate_tier_unknown`. The old
    # behaviour (default `PAYGATE_TIER_ONE`) silently downgraded Ultra
    # users to Pro and stamped the wrong tier into request.params, which
    # then fed back through `_last_observed_paygate_tier_from_db()` and
    # corrupted /api/auth/me responses for the rest of the session.
    tier = params.get("paygate_tier") or flow_client.paygate_tier
    if tier is None:
        return {}, "paygate_tier_unknown"
    # `ref_media_ids` is the broader name (any upstream image / character /
    # visual_asset feeds in as IMAGE_INPUT_TYPE_REFERENCE). Older callers used
    # `character_media_ids` — accept both.
    raw_ref_ids = params.get("ref_media_ids")
    if not isinstance(raw_ref_ids, list):
        raw_ref_ids = params.get("character_media_ids")
    ref_media_ids: Optional[list[str]] = None
    if isinstance(raw_ref_ids, list):
        cleaned = [m for m in raw_ref_ids if isinstance(m, str) and m]
        ref_media_ids = cleaned or None
    raw_count = params.get("variant_count")
    variant_count = 1
    if isinstance(raw_count, int) and raw_count > 0:
        variant_count = raw_count
    # Per-variant prompts (optional). When provided, each variant gets its
    # own text — used by auto-prompt batch mode so variants don't collapse
    # to the same stance.
    raw_prompts = params.get("prompts")
    per_variant_prompts: Optional[list[str]] = None
    if isinstance(raw_prompts, list):
        cleaned = [p for p in raw_prompts if isinstance(p, str) and p.strip()]
        per_variant_prompts = cleaned or None
    image_model = params.get("image_model")
    if not isinstance(image_model, str) or not image_model.strip():
        image_model = None
    resp = await get_flow_sdk().gen_image(
        prompt=prompt.strip(),
        project_id=project_id,
        aspect_ratio=aspect,
        paygate_tier=tier,
        ref_media_ids=ref_media_ids,
        variant_count=variant_count,
        prompts=per_variant_prompts,
        image_model=image_model,
    )
    if resp.get("error"):
        return resp, str(resp["error"])[:200]
    # Flow returns signed fifeUrls directly in the response — persist them
    # immediately so `/media/:id` can serve bytes without any extra round-trip.
    entries_with_urls = [
        e for e in (resp.get("media_entries") or []) if isinstance(e, dict) and e.get("url")
    ]
    if entries_with_urls:
        try:
            media_service.ingest_urls(entries_with_urls)
        except Exception:  # noqa: BLE001
            logger.exception("auto-ingest from gen_image response failed")
    return resp, None


# Video polling knobs — overridable in tests via ``monkeypatch.setattr(proc, …)``.
# Flow's gen_video routinely takes 4-6 min; 42 cycles × 10s = 7 min wall ceiling.
# The provider layer (services/video/flow.py) reads these knobs through a
# module lookup so tests can keep patching them without touching the new file.
VIDEO_POLL_INTERVAL_S = 10.0
VIDEO_POLL_MAX_CYCLES = 42


async def _handle_gen_video(params: dict) -> tuple[dict, Optional[str]]:
    """Thin dispatcher: resolve model → run provider → translate result.

    Phase 5 refactor — the heavy lifting moved into
    ``services/video/{flow,dreamina}.py``. The handler:

    1. Picks a model from ``params["model_id"]``, falling back to the
       process-wide default (``flow-default``). Per-project + per-node
       overrides are resolved upstream when the dispatcher stamps
       ``params`` from the Node row + Project.settings.
    2. Builds ``VideoGenSubmitParams`` from the wire payload — backward
       compatible names (``start_media_id``, ``prompt``, ``project_id``)
       are kept so existing callers (Flow path) don't need to change.
    3. Calls ``provider.run_to_completion(params)``.
    4. Flattens the provider's two-step return (submit_result, poll_result)
       into a ``(dict, Optional[str])`` shape so the worker loop's status
       transition logic stays unchanged.

    The Flow shape (``media_ids`` / ``slot_errors`` / ``partial_error``)
    is preserved verbatim on the Flow path so the existing test suite
    and frontend continue to work.
    """
    from flowboard.services.video import (
        VideoError,
        get_default_model_id,
        get_video_model,
    )
    from flowboard.services.video import registry as _video_registry
    from flowboard.services.video.dreamina import media_id_to_public_url

    _video_registry.register_defaults()

    model_id = params.get("model_id") or get_default_model_id()
    try:
        entry = get_video_model(model_id)
    except KeyError:
        return {"model_id": model_id}, f"unknown_video_model:{model_id}"

    provider = _video_registry.get_video_provider(model_id)

    # Translate legacy + new keys onto VideoGenSubmitParams. Flow keeps
    # its existing param names; Dreamina expects motion_prompt/first_frame_url/
    # reference_images. The mapping below accepts both.
    motion_prompt = params.get("motion_prompt") or params.get("prompt") or ""
    if not isinstance(motion_prompt, str) or not motion_prompt.strip():
        return {}, "missing_prompt"

    # first_frame_url:
    # - Flow path: a Flow media_id (Flow's "URL" namespace = its own ID system).
    # - Dreamina path: a public HTTPS URL. We hoist Flowboard media_ids
    #   through R2 when the caller pre-resolved nothing but a media_id.
    first_frame = params.get("first_frame_url") or params.get("start_media_id") or params.get("startMediaId")
    if isinstance(first_frame, str):
        first_frame = first_frame.strip()

    provider_params: dict = {
        "motion_prompt": motion_prompt.strip(),
    }

    # Person-driven (KYC) path — resolve up to one image/audio/video media_id
    # into Avis KYC assetIds and dispatch with those (portrait→video / lip-sync
    # / video-reference). Bypasses Flow + the regular base64/R2 ref resolution.
    if params.get("kyc_mode") and entry.capabilities.supports_kyc:
        from flowboard.services.video.avis import ensure_kyc_asset

        proj = params.get("project_id")
        img_mid = first_frame or next(
            (r for r in (params.get("reference_images") or []) if isinstance(r, str) and r),
            None,
        )
        aud_mid = (params.get("audio_ref_url") or params.get("audio_ref_media_id") or "").strip() or None
        vid_mid = next(
            (v for v in (params.get("reference_videos") or []) if isinstance(v, str) and v),
            None,
        )
        if not (img_mid or aud_mid or vid_mid):
            return {}, "missing_first_frame_url"
        try:
            if img_mid:
                provider_params["kyc_image_asset_id"] = await ensure_kyc_asset(
                    img_mid, "Image", project_id=proj
                )
            if aud_mid:
                provider_params["kyc_audio_asset_id"] = await ensure_kyc_asset(
                    aud_mid, "Audio", project_id=proj
                )
            if vid_mid:
                provider_params["kyc_video_asset_id"] = await ensure_kyc_asset(
                    vid_mid, "Video", project_id=proj
                )
        except VideoError as exc:
            return {"error": str(exc), "code": exc.code, "raw": exc.raw}, f"{exc.code}:{exc}"
        provider_params.update({
            "duration_seconds": int(params.get("duration_seconds") or 5),
            "aspect_ratio": params.get("aspect_ratio") or "16:9",
            "resolution": params.get("resolution") or "720p",
        })
        if "generate_audio" in params:
            provider_params["generate_audio"] = bool(params["generate_audio"])
    elif entry.provider_name == "flow":
        # Flow keeps the broader legacy param surface. start_media_ids
        # (batch) is forwarded through a private knob so the provider
        # can split it back out for sdk.gen_video.
        raw_starts = params.get("start_media_ids")
        start_media_ids: Optional[list[str]] = None
        if isinstance(raw_starts, list):
            cleaned = [m for m in raw_starts if isinstance(m, str) and m.strip()]
            start_media_ids = [m.strip() for m in cleaned] or None
        if start_media_ids is None and (not first_frame):
            return {}, "missing_start_media_id"
        provider_params.update({
            "first_frame_url": first_frame or "",
            "_flow_start_media_ids": start_media_ids or [],
            "project_id": params.get("project_id") or "",
            "paygate_tier": params.get("paygate_tier") or flow_client.paygate_tier,
            "video_quality": params.get("video_quality"),
            "aspect_ratio": params.get("aspect_ratio") or "16:9",
            "duration_seconds": 8,
            "resolution": "720p",
        })
    else:
        # Non-flow providers resolve a bare Flowboard media_id into something
        # the provider can consume. Dreamina (BytePlus direct) needs a publicly
        # reachable URL → hoist to an R2 presigned URL. Avis sends reference
        # media INLINE (base64) and needs no R2, so the bare media_id is passed
        # straight through and the provider reads the local cache + encodes.
        hoist = entry.provider_name != "avis"

        def _resolve(ref: str) -> str:
            if ref.startswith(("http://", "https://", "data:")) or not hoist:
                return ref
            return media_id_to_public_url(ref, project_id=params.get("project_id"))

        try:
            if first_frame:
                first_frame = _resolve(first_frame)
        except VideoError as exc:
            return {"error": str(exc)}, f"{exc.code}:{exc}"

        # Phase 8.1: reorder refs by their per-node @image label digit BEFORE
        # resolution so the Nth reference block matches @imageN.
        # `reference_labels` is positionally parallel to `reference_images`;
        # all-null (Automation / label-less canvas) is a no-op.
        ref_inputs = [r for r in (params.get("reference_images") or []) if isinstance(r, str) and r]
        raw_labels = params.get("reference_labels")
        if isinstance(raw_labels, list) and ref_inputs:
            from flowboard.services.video.ref_ordering import order_refs_by_label

            labels = [(lbl if isinstance(lbl, str) else None) for lbl in raw_labels]
            ref_inputs = order_refs_by_label(ref_inputs, labels)

        resolved_refs: list[str] = []
        for r in ref_inputs:
            try:
                resolved_refs.append(_resolve(r))
            except VideoError as exc:
                logger.warning("video: skipped unreachable ref %s: %s", r, exc)

        # Audio reference (Seedance 2.0 r2v+audio).
        audio_ref = params.get("audio_ref_url") or params.get("audio_ref_media_id")
        resolved_audio: Optional[str] = None
        if isinstance(audio_ref, str) and audio_ref:
            try:
                resolved_audio = _resolve(audio_ref)
            except VideoError as exc:
                logger.warning("video: skipped unreachable audio ref: %s", exc)

        # Reference videos (Seedance 2.0 r2v, contract §11.9).
        raw_video_refs = params.get("reference_videos") or params.get("video_ref_urls") or []
        resolved_videos: list[str] = []
        if isinstance(raw_video_refs, list):
            for v in raw_video_refs:
                if not isinstance(v, str) or not v:
                    continue
                try:
                    resolved_videos.append(_resolve(v))
                except VideoError as exc:
                    logger.warning("video: skipped unreachable video ref: %s", exc)

        # first_frame is optional in reference-media (r2v / r2v+audio) modes;
        # the provider derives mode from refs/audio/video and validates per-mode.
        # Only hard-fail when there's nothing to generate from at all.
        if not first_frame and not resolved_refs and not resolved_audio and not resolved_videos:
            return {}, "missing_first_frame_url"

        last_frame = params.get("last_frame_url")
        if isinstance(last_frame, str) and last_frame and not last_frame.startswith(("http://", "https://", "data:")):
            try:
                last_frame = media_id_to_public_url(
                    last_frame, project_id=params.get("project_id")
                )
            except VideoError as exc:
                logger.warning("dreamina: skipped unreachable last_frame: %s", exc)
                last_frame = None

        provider_params.update({
            "first_frame_url": first_frame or "",
            "reference_images": resolved_refs,
            "reference_videos": resolved_videos,
            "last_frame_url": last_frame if isinstance(last_frame, str) else None,
            "audio_ref_url": resolved_audio,
            "duration_seconds": int(params.get("duration_seconds") or 5),
            "aspect_ratio": params.get("aspect_ratio") or "1:1",
            "resolution": params.get("resolution") or "720p",
        })
        if "generate_audio" in params:
            provider_params["generate_audio"] = bool(params["generate_audio"])

    try:
        submit_result, poll_result = await provider.run_to_completion(provider_params)
    except VideoError as exc:
        # Flow callers (and the legacy test suite) expect the raw error
        # string, not a code-prefixed wrapper. Other providers surface
        # the uniform code so the UI can map consistently.
        msg = str(exc)
        if entry.provider_name == "flow":
            return {"error": msg, "raw": exc.raw}, msg[:200]
        return {"error": msg, "code": exc.code, "raw": exc.raw}, f"{exc.code}:{msg}"

    raw = dict(poll_result.get("raw") or {})
    raw.setdefault("external_job_id", submit_result.get("external_job_id"))
    raw.setdefault("model_id", model_id)
    raw.setdefault("provider", entry.provider_name)
    warnings = list(submit_result.get("warnings") or [])
    if warnings:
        raw["warnings"] = warnings
    if poll_result.get("cost_usd") is not None:
        raw["cost_usd"] = poll_result["cost_usd"]
    if poll_result.get("cost_tokens") is not None:
        raw["cost_tokens"] = poll_result["cost_tokens"]
    if poll_result.get("media_metadata"):
        raw["media_metadata"] = poll_result["media_metadata"]

    # Eager-persist Dreamina bytes locally so the existing /media/<id>
    # route can serve them like any other asset. We mint a fresh
    # media_id from the external_job_id so each provider's output gets
    # a stable Flowboard-side ID even when the upstream uses a
    # different identifier shape.
    if poll_result.get("status") == "succeeded" and poll_result.get("video_bytes"):
        synthetic_mid = _synthesize_media_id(submit_result["external_job_id"])
        try:
            media_service.ingest_inline_bytes(
                synthetic_mid,
                poll_result["video_bytes"],
                kind="video",
                mime="video/mp4",
            )
            raw["media_ids"] = [synthetic_mid]
        except Exception:  # noqa: BLE001
            logger.exception(
                "failed to persist provider=%s video bytes for job=%s",
                entry.provider_name, submit_result["external_job_id"],
            )

    status = poll_result.get("status")
    if status == "succeeded":
        return raw, None
    if status in {"failed", "cancelled"}:
        # Tie back to the legacy contract: tests assert
        # error == "timeout_waiting_video" for the timeout path. Flow
        # provider's partial-success branch surfaces that string in
        # raw.op_errors values; for the Dreamina path we use the
        # uniform code prefix.
        err_msg = poll_result.get("error_message") or poll_result.get("error") or "failed"
        if entry.provider_name == "flow":
            # Preserve the legacy single-token error for the "all ops
            # timed out" case — test_worker_gen_video_times_out asserts
            # exact equality.
            op_errors = (raw.get("op_errors") or {}).values()
            if op_errors and all(v == "timeout_waiting_video" for v in op_errors):
                return raw, "timeout_waiting_video"
            first_err = next(iter(op_errors), None)
            return raw, first_err or err_msg
        return raw, err_msg
    # Non-terminal status returned from run_to_completion is a provider bug.
    return raw, f"non_terminal_status:{status}"


def _synthesize_media_id(external_job_id: str) -> str:
    """Map an external job id to a Flowboard media_id.

    media_id_validation only allows hex-with-dashes; the simplest stable
    transformation is a SHA1 hex digest of the job id. Collisions are a
    non-issue at single-user scale.
    """
    import hashlib
    return hashlib.sha1(external_job_id.encode("utf-8")).hexdigest()


async def _handle_edit_image(params: dict) -> tuple[dict, Optional[str]]:
    from flowboard.services.flow_sdk import is_valid_project_id

    prompt = params.get("prompt")
    project_id = params.get("project_id")
    source_media_id = params.get("source_media_id") or params.get("sourceMediaId")
    if not isinstance(prompt, str) or not prompt.strip():
        return {}, "missing_prompt"
    if not isinstance(project_id, str) or not project_id.strip():
        return {}, "missing_project_id"
    project_id = project_id.strip()
    if not is_valid_project_id(project_id):
        return {}, "invalid_project_id"
    if not isinstance(source_media_id, str) or not source_media_id.strip():
        return {}, "missing_source_media_id"
    aspect = params.get("aspect_ratio") or "IMAGE_ASPECT_RATIO_LANDSCAPE"
    # Tier resolution — see _handle_gen_image for rationale. Fail loud,
    # no silent fallback to Pro.
    tier = params.get("paygate_tier") or flow_client.paygate_tier
    if tier is None:
        return {}, "paygate_tier_unknown"
    raw_refs = params.get("ref_media_ids")
    ref_ids: Optional[list[str]] = None
    if isinstance(raw_refs, list):
        cleaned = [m for m in raw_refs if isinstance(m, str) and m]
        ref_ids = cleaned or None
    image_model = params.get("image_model")
    if not isinstance(image_model, str) or not image_model.strip():
        image_model = None

    resp = await get_flow_sdk().edit_image(
        prompt=prompt.strip(),
        project_id=project_id,
        source_media_id=source_media_id.strip(),
        ref_media_ids=ref_ids,
        aspect_ratio=aspect,
        paygate_tier=tier,
        image_model=image_model,
    )
    if resp.get("error"):
        return resp, str(resp["error"])[:200]
    entries_with_urls = [
        e for e in (resp.get("media_entries") or []) if isinstance(e, dict) and e.get("url")
    ]
    if entries_with_urls:
        try:
            media_service.ingest_urls(entries_with_urls)
        except Exception:  # noqa: BLE001
            logger.exception("auto-ingest from edit_image response failed")
    return resp, None


# ── Storyboard ────────────────────────────────────────────────────────────
# Plan: .omc/plans/storyboard-image-node.md §6.3-6.5.
# A storyboard request fans out into:
#   Phase A — gen_image for every root shot (parents[k] is None) in
#             parallel chunks of ≤4 (Flow's hard cap).
#   Phase B — BFS through the continuity tree; siblings whose parent
#             just turned `done` dispatch in parallel as edit_image
#             with `base_media_id = shots[parent].mediaId`.
# Refs are global (locked OPEN-4): same array passed to every dispatch.
# Failures keep the partial state — descendants of a failed shot are
# marked "blocked" so the user can retry just the failed level.

def _propagate_blocked(shots: list[dict]) -> None:
    """Any shot whose parent is error/blocked → blocked + parent_failed."""
    n = len(shots)
    changed = True
    while changed:
        changed = False
        for k in range(n):
            if shots[k].get("status") != "queued":
                continue
            p = shots[k].get("parentShotIdx")
            if p is None:
                continue
            if shots[p].get("status") in ("error", "blocked"):
                shots[k]["status"] = "blocked"
                shots[k]["error"] = "parent_failed"
                changed = True


def _aggregate_node_status(shots: list[dict]) -> str:
    statuses = {s.get("status") for s in shots}
    if statuses == {"done"}:
        return "done"
    if statuses <= {"error", "blocked"}:
        return "error"
    if "done" in statuses:
        return "partial"
    return "running"


def _persist_storyboard_progress(
    node_id: int, shots: list[dict], node_status: str
) -> None:
    """Write the in-progress shots[] state to Node.data so the frontend
    sees Phase A roots populate before Phase B finishes. Mirrors the final
    patchNode payload the frontend writes on done — idempotent w.r.t.
    that final write.

    For 30-60s storyboards the request-level polling path only patches
    the node ONCE on completion. Persisting after each phase lets a poll
    of `/api/nodes/:id` return real-time progress.
    """
    from flowboard.db.models import Node
    with get_session() as s:
        node = s.get(Node, node_id)
        if node is None:
            return
        new_data = dict(node.data or {})
        new_data["shots"] = shots
        new_data["shotCount"] = len(shots)
        new_data["mediaIds"] = [sh.get("mediaId") for sh in shots]
        node.data = new_data
        node.status = node_status
        s.add(node)
        s.commit()


async def _handle_gen_storyboard(params: dict) -> tuple[dict, Optional[str]]:
    from flowboard.services import prompt_synth
    from flowboard.services.flow_sdk import is_valid_project_id

    n = params.get("shot_count")
    if not isinstance(n, int) or not 1 <= n <= 8:
        return {}, "shot_count_out_of_range"

    project_id = params.get("project_id")
    if not isinstance(project_id, str) or not project_id.strip():
        return {}, "missing_project_id"
    project_id = project_id.strip()
    if not is_valid_project_id(project_id):
        return {}, "invalid_project_id"

    aspect = params.get("aspect_ratio") or "IMAGE_ASPECT_RATIO_LANDSCAPE"
    tier = params.get("paygate_tier") or flow_client.paygate_tier
    if tier is None:
        return {}, "paygate_tier_unknown"

    image_model = params.get("image_model")
    if not isinstance(image_model, str) or not image_model.strip():
        image_model = None

    raw_refs = params.get("global_ref_media_ids")
    refs: list[str] = []
    if isinstance(raw_refs, list):
        refs = [m for m in raw_refs if isinstance(m, str) and m]

    # Hoist node_id read here so progressive-persistence helpers below can
    # reach Node.data without depending on planner branch. The escape-hatch
    # path (caller supplies shot_prompts) doesn't require a node_id — tests
    # of the validation-only path call without one. Persistence is gated on
    # `isinstance(node_id, int)` so a missing node_id is a no-op, not an
    # error, here.
    node_id = params.get("__node_id") or params.get("node_id")

    # 1. Plan beats (or use caller-supplied)
    if isinstance(params.get("shot_prompts"), list):
        prompts = list(params["shot_prompts"])
        parents = list(params.get("shot_parents") or [])
        if len(prompts) != n or len(parents) != n:
            return {}, "shot_prompts_length_mismatch"
    else:
        if not isinstance(node_id, int):
            return {}, "missing_node_id_for_planner"
        try:
            plan = await prompt_synth.auto_prompt_storyboard(
                node_id, count=n,
                narrative_seed=params.get("narrative_seed", "") or "",
            )
        except prompt_synth.PromptSynthError as exc:
            return {}, f"planner:{exc}"[:200]
        prompts = list(plan["prompts"])
        parents = list(plan["parents"])

    # 2. Validate parents
    if parents[0] is not None:
        return {}, "parents_root_must_be_null"
    for k in range(1, n):
        v = parents[k]
        # Reject bool explicitly: in Python `bool` subclasses `int`, so a
        # caller posting `shot_parents=[null, true]` would otherwise pass
        # the `isinstance(v, int)` check and `True == 1` would silently be
        # used as the parent index. Mirrors the planner-side validation in
        # `auto_prompt_storyboard` (services/prompt_synth.py).
        if v is not None and not (
            isinstance(v, int) and not isinstance(v, bool) and 0 <= v < k
        ):
            return {}, f"parents_oob_at_{k}"

    # 3. Initialise shots state
    shots: list[dict] = [
        {
            "idx": k,
            "prompt": prompts[k],
            "parentShotIdx": parents[k],
            "mediaId": None,
            "status": "queued",
            "error": None,
        }
        for k in range(n)
    ]

    sdk = get_flow_sdk()

    async def _ingest(entries: list[dict]) -> None:
        urls = [e for e in entries if isinstance(e, dict) and e.get("url")]
        if not urls:
            return
        try:
            media_service.ingest_urls(urls)
        except Exception:  # noqa: BLE001
            logger.exception("auto-ingest failed in gen_storyboard")

    # 4. Phase A — dispatch roots in chunks of ≤4
    roots = [k for k in range(n) if parents[k] is None]
    for chunk_start in range(0, len(roots), 4):
        chunk = roots[chunk_start:chunk_start + 4]
        try:
            res = await sdk.gen_image(
                prompt=prompts[chunk[0]],
                project_id=project_id,
                aspect_ratio=aspect,
                paygate_tier=tier,
                ref_media_ids=refs or None,
                variant_count=len(chunk),
                prompts=[prompts[k] for k in chunk],
                image_model=image_model,
            )
            if (res or {}).get("error"):
                err_msg = str(res["error"])[:200]
                for k in chunk:
                    shots[k]["status"] = "error"
                    shots[k]["error"] = err_msg
                continue
            await _ingest((res or {}).get("media_entries") or [])
            ids = (res or {}).get("media_ids") or []
            for i, k in enumerate(chunk):
                mid = ids[i] if i < len(ids) else None
                shots[k]["mediaId"] = mid
                shots[k]["status"] = "done" if mid else "error"
                shots[k]["error"] = None if mid else "missing_media"
        except Exception as exc:  # noqa: BLE001
            logger.exception("gen_storyboard root chunk failed")
            err_msg = str(exc)[:200]
            for k in chunk:
                shots[k]["status"] = "error"
                shots[k]["error"] = err_msg
    _propagate_blocked(shots)
    # Persist Phase A progress so the frontend sees roots populate before
    # Phase B finishes (mirrors the final patchNode payload — idempotent).
    if isinstance(node_id, int):
        _persist_storyboard_progress(
            node_id, shots, _aggregate_node_status(shots)
        )

    # 5. Phase B — BFS children level by level
    while True:
        eligible = [
            k for k in range(n)
            if shots[k]["status"] == "queued"
            and parents[k] is not None
            and shots[parents[k]]["status"] == "done"
        ]
        if not eligible:
            break

        async def _edit_one(k: int) -> None:
            try:
                res = await sdk.edit_image(
                    prompt=prompts[k],
                    project_id=project_id,
                    source_media_id=shots[parents[k]]["mediaId"],
                    ref_media_ids=refs or None,
                    aspect_ratio=aspect,
                    paygate_tier=tier,
                    image_model=image_model,
                )
                if (res or {}).get("error"):
                    shots[k]["status"] = "error"
                    shots[k]["error"] = str(res["error"])[:200]
                    return
                await _ingest((res or {}).get("media_entries") or [])
                ids = (res or {}).get("media_ids") or []
                mid = ids[0] if ids else None
                shots[k]["mediaId"] = mid
                shots[k]["status"] = "done" if mid else "error"
                shots[k]["error"] = None if mid else "missing_media"
            except Exception as exc:  # noqa: BLE001
                logger.exception("gen_storyboard child %d failed", k)
                shots[k]["status"] = "error"
                shots[k]["error"] = str(exc)[:200]

        await asyncio.gather(*[_edit_one(k) for k in eligible])
        _propagate_blocked(shots)
        # Persist after each BFS level so a long Phase B reveals progress
        # tile-by-tile. The next eligibility scan uses the in-memory shots
        # list, not the persisted copy, so this write is purely for
        # frontend visibility.
        if isinstance(node_id, int):
            _persist_storyboard_progress(
                node_id, shots, _aggregate_node_status(shots)
            )

    return (
        {
            "shots": shots,
            "media_ids": [s["mediaId"] for s in shots],
            "node_status": _aggregate_node_status(shots),
        },
        None,
    )


async def _handle_retry_storyboard_shot(params: dict) -> tuple[dict, Optional[str]]:
    from flowboard.db.models import Node
    from flowboard.services.flow_sdk import is_valid_project_id

    shot_idx = params.get("shot_idx")
    if not isinstance(shot_idx, int) or shot_idx < 0:
        return {}, "missing_shot_idx"

    node_id = params.get("__node_id") or params.get("node_id")
    if not isinstance(node_id, int):
        return {}, "missing_node_id"

    # Pull current shots[] + node-level config from Node.data.
    with get_session() as s:
        node = s.get(Node, node_id)
        if node is None:
            return {}, "node_not_found"
        data = dict(node.data or {})
        shots = list(data.get("shots") or [])
        if not (0 <= shot_idx < len(shots)):
            return {}, "shot_idx_out_of_range"
        shot = dict(shots[shot_idx])
        node_aspect = data.get("aspectRatio")
        node_refs_raw = data.get("globalRefMediaIds")
        node_refs = (
            [m for m in node_refs_raw if isinstance(m, str) and m]
            if isinstance(node_refs_raw, list) else []
        )
        node_model = data.get("imageModel")
        node_tier = data.get("paygateTier")
        node_project = data.get("projectId")

    prompt = shot.get("prompt")
    parent_idx = shot.get("parentShotIdx")
    if not isinstance(prompt, str) or not prompt.strip():
        return {}, "shot_has_no_prompt"

    project_id = params.get("project_id") or node_project
    aspect = params.get("aspect_ratio") or node_aspect or "IMAGE_ASPECT_RATIO_LANDSCAPE"
    tier = params.get("paygate_tier") or node_tier or flow_client.paygate_tier
    raw_refs = params.get("ref_media_ids")
    refs = (
        [m for m in raw_refs if isinstance(m, str) and m]
        if isinstance(raw_refs, list) else node_refs
    )
    model = params.get("image_model") or node_model
    if not isinstance(model, str) or not model.strip():
        model = None

    if not isinstance(project_id, str) or not project_id.strip():
        return {}, "missing_project_id"
    project_id = project_id.strip()
    if not is_valid_project_id(project_id):
        return {}, "invalid_project_id"
    if tier is None:
        return {}, "paygate_tier_unknown"

    sdk = get_flow_sdk()

    if parent_idx is None:
        # Root retry — gen_image with variant_count=1.
        try:
            res = await sdk.gen_image(
                prompt=prompt,
                project_id=project_id,
                aspect_ratio=aspect,
                paygate_tier=tier,
                ref_media_ids=refs or None,
                variant_count=1,
                prompts=[prompt],
                image_model=model,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("retry_storyboard_shot root dispatch failed")
            return {}, f"gen_image:{exc}"[:200]
    else:
        if not (0 <= parent_idx < len(shots)):
            return {}, "parent_idx_out_of_range"
        parent_status = shots[parent_idx].get("status")
        if parent_status != "done":
            return {}, "parent_not_ready"
        parent_mid = shots[parent_idx].get("mediaId")
        if not isinstance(parent_mid, str) or not parent_mid:
            return {}, "parent_has_no_media"
        try:
            res = await sdk.edit_image(
                prompt=prompt,
                project_id=project_id,
                source_media_id=parent_mid,
                ref_media_ids=refs or None,
                aspect_ratio=aspect,
                paygate_tier=tier,
                image_model=model,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("retry_storyboard_shot child dispatch failed")
            return {}, f"edit_image:{exc}"[:200]

    if (res or {}).get("error"):
        return res, str(res["error"])[:200]
    entries = (res or {}).get("media_entries") or []
    urls = [e for e in entries if isinstance(e, dict) and e.get("url")]
    if urls:
        try:
            media_service.ingest_urls(urls)
        except Exception:  # noqa: BLE001
            logger.exception("auto-ingest failed in retry_storyboard_shot")
    ids = (res or {}).get("media_ids") or []
    new_mid = ids[0] if ids else None
    return (
        {
            "shot_idx": shot_idx,
            "media_id": new_mid,
            "media_ids": [new_mid] if new_mid else [],
        },
        None,
    )


_DEFAULT_HANDLERS: dict[str, Handler] = {
    "proxy": _handle_proxy,
    "create_project": _handle_create_project,
    "gen_image": _handle_gen_image,
    "gen_video": _handle_gen_video,
    "edit_image": _handle_edit_image,
    "gen_storyboard": _handle_gen_storyboard,
    "retry_storyboard_shot": _handle_retry_storyboard_shot,
}


class WorkerController:
    """Single-consumer async queue worker."""

    def __init__(self, handlers: Optional[dict[str, Handler]] = None) -> None:
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._handlers = dict(handlers or _DEFAULT_HANDLERS)
        self._shutdown = asyncio.Event()
        self._active = 0
        self._started_at: Optional[float] = None

    # ── enqueue ────────────────────────────────────────────────────────────
    def enqueue(self, request_id: int) -> None:
        self._queue.put_nowait(request_id)

    # ── lifecycle ──────────────────────────────────────────────────────────
    async def start(self) -> None:
        self._started_at = time.time()
        logger.info("worker started")
        while not self._shutdown.is_set():
            try:
                rid = await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            await self._process_one(rid)

    def request_shutdown(self) -> None:
        self._shutdown.set()

    async def drain(self) -> None:
        # Wait for any in-flight task to finish.
        while self._active > 0:
            await asyncio.sleep(0.05)

    @property
    def active_count(self) -> int:
        return self._active

    @property
    def uptime_s(self) -> Optional[float]:
        if self._started_at is None:
            return None
        return time.time() - self._started_at

    # ── execution ──────────────────────────────────────────────────────────
    async def _process_one(self, rid: int) -> None:
        self._active += 1
        try:
            with get_session() as s:
                req = s.get(Request, rid)
                if req is None:
                    logger.warning("worker: request %s not found", rid)
                    return
                # Drift guard — the row might have been canceled (or
                # otherwise transitioned out of queued) between enqueue
                # and pop. The cancel endpoint mutates the DB row only;
                # it can't yank the rid back off the in-memory queue, so
                # we re-check here and bail without flipping status.
                if req.status != "queued":
                    logger.info(
                        "worker: skipping rid=%s (status=%s)", rid, req.status
                    )
                    return
                handler = self._handlers.get(req.type)
                if handler is None:
                    req.status = "failed"
                    req.error = f"unknown_request_type:{req.type}"
                    req.finished_at = datetime.now(timezone.utc)
                    s.add(req)
                    s.commit()
                    return

                req.status = "running"
                s.add(req)
                s.commit()
                params = dict(req.params or {})
                # Enrich with the request's node_id so handlers that need
                # to look up Node.data (e.g. storyboard) don't depend on
                # the caller copying it into params explicitly. Underscore
                # prefix avoids colliding with handler-defined fields.
                if req.node_id is not None and "__node_id" not in params:
                    params["__node_id"] = req.node_id

            # Release the session during the possibly-long RPC.
            result, err = await handler(params)

            with get_session() as s:
                req = s.get(Request, rid)
                if req is None:
                    return
                req.result = result if isinstance(result, dict) else {"value": result}
                req.finished_at = datetime.now(timezone.utc)
                if err:
                    req.status = "failed"
                    req.error = err
                else:
                    req.status = "done"
                    req.error = None
                s.add(req)
                s.commit()
        except Exception as exc:  # noqa: BLE001
            logger.exception("worker exception on rid=%s", rid)
            try:
                with get_session() as s:
                    req = s.get(Request, rid)
                    if req is not None:
                        req.status = "failed"
                        req.error = str(exc)[:500]
                        req.finished_at = datetime.now(timezone.utc)
                        s.add(req)
                        s.commit()
            except Exception:  # noqa: BLE001
                logger.exception("worker: failed to record failure for rid=%s", rid)
        finally:
            self._active -= 1


_worker: Optional[WorkerController] = None


def get_worker() -> WorkerController:
    global _worker
    if _worker is None:
        _worker = WorkerController()
    return _worker
