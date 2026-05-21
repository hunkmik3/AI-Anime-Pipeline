"""Google Flow as a ``VideoProvider``.

Flow's gen_video is a batch operation: one dispatch can return N
``operation_names`` for an N-variant generation, and the polling step
must reconcile per-op outcomes (success / content-filter / timeout).
The wrapper keeps that batched semantics intact — the entire submit +
poll loop runs inside ``run_to_completion`` so the per-op error
aggregation and partial-success handling that the existing worker
test suite covers remains byte-identical.

``submit`` / ``poll`` are also exposed for tests that want to verify
provider Protocol conformance, but in production the worker only
calls ``run_to_completion`` — Flow doesn't expose a real polling
endpoint we could drive externally without leaking the
``operation_names`` list across function calls.

Capabilities:
- single-frame i2v (multi-variant via ``start_media_ids``)
- no multi-reference anchoring (Flow's ``IMAGE_INPUT_TYPE_REFERENCE`` is
  used by ``gen_image``, not ``gen_video``)
- no last-frame keyframe
- no audio toggle
- aspect ratios + resolution + duration are provider-fixed (encoded as
  the Flow enums inside the SDK)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from flowboard.services import media as media_service
from flowboard.services.flow_client import flow_client
from flowboard.services.flow_sdk import is_valid_project_id


def _resolve_flow_sdk():
    """Look up ``get_flow_sdk`` through the worker.processor module so that
    monkeypatches set in tests (``monkeypatch.setattr(proc, "get_flow_sdk", ...)``)
    are honored without altering 5 existing test files.

    Falls back to importing directly from the SDK module when the worker
    module isn't available (provider used outside the normal app boot,
    e.g. ad-hoc CLI scripts).
    """
    try:
        from flowboard.worker import processor as _proc  # noqa: WPS433 — circular by design
        return _proc.get_flow_sdk()
    except (ImportError, AttributeError):
        from flowboard.services.flow_sdk import get_flow_sdk as _fallback
        return _fallback()


def _resolve_poll_knobs() -> tuple[float, int]:
    """Same approach for VIDEO_POLL_INTERVAL_S / VIDEO_POLL_MAX_CYCLES."""
    try:
        from flowboard.worker import processor as _proc
        return (
            getattr(_proc, "VIDEO_POLL_INTERVAL_S", VIDEO_POLL_INTERVAL_S),
            getattr(_proc, "VIDEO_POLL_MAX_CYCLES", VIDEO_POLL_MAX_CYCLES),
        )
    except (ImportError, AttributeError):
        return VIDEO_POLL_INTERVAL_S, VIDEO_POLL_MAX_CYCLES

from .base import (
    VideoError,
    VideoGenPollResult,
    VideoGenSubmitParams,
    VideoGenSubmitResult,
    VideoProviderCapability,
)

logger = logging.getLogger(__name__)


# Polling knobs — matched 1:1 to the legacy worker constants so the
# existing test suite (test_processor_tier_fallback, etc.) keeps passing
# without timing-related re-tuning.
VIDEO_POLL_INTERVAL_S = 10.0
VIDEO_POLL_MAX_CYCLES = 42


FLOW_DEFAULT_CAPABILITY = VideoProviderCapability(
    supports_multi_ref=False,
    supports_last_frame=False,
    supports_audio_toggle=False,
    max_refs=0,
    # Flow's aspect ratios are enum-based at the SDK boundary; surface
    # the human values so the frontend dropdown stays consistent.
    aspect_ratios=("16:9", "9:16"),
    resolutions=("720p",),
    durations=(8,),  # Flow durations are provider-fixed; 8s is the canonical clip length
)


class FlowVideoProvider:
    """Wraps the existing flow_sdk + worker polling loop as a VideoProvider.

    State (operation_names, dispatch payload, workflows) is held on the
    instance keyed by a synthetic ``external_job_id``. This is process-
    local — survives across submit→poll within a single dispatch, but
    not across worker restarts. That matches Flow's existing semantics
    (legacy ``_handle_gen_video`` was a single tight loop).
    """

    name = "flow"

    def __init__(self, entry) -> None:
        # ``entry`` is the registry VideoModelEntry; we accept it for
        # signature uniformity with DreaminaVideoProvider but Flow has
        # no per-model nuance — capabilities come from the entry.
        self.capabilities = entry.capabilities
        self._dispatch_state: dict[str, dict] = {}

    async def is_available(self) -> bool:
        # Flow availability is governed by the extension bridge; the
        # paygate tier discovery happens via /api/auth/me. We treat
        # the provider as always-available at the Protocol level and
        # let submit-time errors surface failures.
        return True

    async def submit(self, params: VideoGenSubmitParams) -> VideoGenSubmitResult:
        """Dispatch a Flow gen_video. ``first_frame_url`` is interpreted
        as a Flow ``media_id`` (Flow's "URL" namespace is its own
        media ID system, not public HTTPS).

        Flow accepts a batch — when ``params['reference_images']`` looks
        like Flow media IDs, we treat them as the start_media_ids batch
        (multi-variant). Cross-provider reference images aren't a Flow
        concept; capability gates that elsewhere.
        """
        warnings: list[str] = []
        # Capability gate — multi-ref isn't supported on Flow video. The
        # worker stamps reference_images from VideoNode.data; if any are
        # set, drop them with a warning instead of failing.
        refs = list(params.get("reference_images") or [])
        if refs and not self.capabilities.supports_multi_ref:
            warnings.append(
                f"Flow gen_video does not support multi-ref; dropped {len(refs)} refs"
            )

        last_frame = params.get("last_frame_url")
        if last_frame and not self.capabilities.supports_last_frame:
            warnings.append("Flow gen_video does not support last_frame; ignored")

        prompt = params.get("motion_prompt") or ""
        if not prompt.strip():
            raise VideoError("bad_input", "missing motion_prompt")
        project_id = params.get("project_id") or ""
        if not project_id or not is_valid_project_id(project_id):
            raise VideoError("bad_input", "invalid or missing project_id")

        # Flow exposes start_media_id (single) or start_media_ids (batch).
        # We unify on the first_frame_url field for the Protocol; if the
        # worker supplied a batch of frame URLs, they're forwarded as
        # start_media_ids and Flow treats this as the multi-variant case.
        start_media_id = params.get("first_frame_url") or ""
        start_media_ids = list(params.get("_flow_start_media_ids") or [])  # private extension knob
        if not start_media_id and not start_media_ids:
            raise VideoError("bad_input", "missing first_frame_url (Flow media_id)")

        aspect = self._flow_aspect(params.get("aspect_ratio") or "16:9")
        tier = params.get("paygate_tier") or flow_client.paygate_tier
        if tier is None:
            raise VideoError("bad_input", "paygate_tier_unknown")
        video_quality = params.get("video_quality")
        if not isinstance(video_quality, str) or not video_quality.strip():
            video_quality = None

        sdk = _resolve_flow_sdk()
        dispatch = await sdk.gen_video(
            prompt=prompt.strip(),
            project_id=project_id,
            start_media_id=start_media_id if start_media_id else None,
            start_media_ids=start_media_ids or None,
            aspect_ratio=aspect,
            paygate_tier=tier,
            video_quality=video_quality,
        )
        if dispatch.get("error"):
            raise VideoError(
                _classify_flow_error(str(dispatch["error"])),
                str(dispatch["error"])[:200],
                raw=dispatch,
            )
        op_names = list(dispatch.get("operation_names") or [])
        if not op_names:
            raise VideoError(
                "internal",
                "no_operations_returned",
                raw=dispatch,
            )

        # Synthetic job id: operations are state — we stash them on the
        # provider instance and return a lookup key. Worker uses this
        # opaque id to drive run_to_completion's poll loop.
        job_id = f"flow:{op_names[0]}:{len(op_names)}"
        self._dispatch_state[job_id] = {
            "dispatch": dispatch,
            "op_names": op_names,
            "workflows": dispatch.get("workflows") or None,
            "done_by_name": {name: False for name in op_names},
            "entry_by_name": {},
            "op_errors": {},
            "last_poll": {},
        }
        return {
            "external_job_id": job_id,
            "submitted_at": int(asyncio.get_event_loop().time()),
            "warnings": warnings,
        }

    async def poll(self, external_job_id: str) -> VideoGenPollResult:
        """Single poll cycle against Flow.

        Updates the per-instance dispatch state in place and returns the
        aggregate status. ``run_to_completion`` drives this on a fixed
        interval; standalone callers can drive it themselves.
        """
        state = self._dispatch_state.get(external_job_id)
        if state is None:
            raise VideoError(
                "internal",
                f"unknown flow job_id: {external_job_id}",
            )
        sdk = _resolve_flow_sdk()
        last_poll = await sdk.check_async(
            state["op_names"], workflows=state["workflows"]
        )
        state["last_poll"] = last_poll
        if not last_poll.get("error"):
            for op in last_poll.get("operations") or []:
                if not isinstance(op, dict):
                    continue
                name = op.get("name")
                if not isinstance(name, str) or state["done_by_name"].get(name, False):
                    continue
                err = op.get("error")
                if isinstance(err, str) and err:
                    state["done_by_name"][name] = True
                    state["op_errors"][name] = err
                    continue
                if op.get("done"):
                    state["done_by_name"][name] = True
                    for e in op.get("media_entries") or []:
                        if isinstance(e, dict) and e.get("media_id"):
                            state["entry_by_name"][name] = e
                            break

        if all(state["done_by_name"].values()):
            return self._terminal_result(external_job_id, state)
        return {"status": "running", "cost_usd": 0.0}

    async def run_to_completion(
        self, params: VideoGenSubmitParams
    ) -> tuple[VideoGenSubmitResult, VideoGenPollResult]:
        submit_result = await self.submit(params)
        job_id = submit_result["external_job_id"]
        state = self._dispatch_state[job_id]
        interval_s, max_cycles = _resolve_poll_knobs()
        attempts = 0
        while attempts < max_cycles and not all(state["done_by_name"].values()):
            await asyncio.sleep(interval_s)
            attempts += 1
            poll = await self.poll(job_id)
            if poll.get("status") in {"succeeded", "failed", "cancelled"}:
                return submit_result, poll
        # Loop exhausted; mark any unresolved as timeout.
        for name in state["op_names"]:
            if not state["done_by_name"].get(name) and name not in state["op_errors"]:
                state["op_errors"][name] = "timeout_waiting_video"
                state["done_by_name"][name] = True
        return submit_result, self._terminal_result(job_id, state)

    # ── helpers ────────────────────────────────────────────────────────

    def _terminal_result(self, job_id: str, state: dict) -> VideoGenPollResult:
        """Aggregate per-op outcomes into a single VideoGenPollResult.

        Mirrors the legacy _handle_gen_video terminal logic so the
        ``raw`` dict carries the same positional ``media_ids`` /
        ``slot_errors`` / ``partial_error`` keys the existing tests
        and frontend depend on.
        """
        op_names = state["op_names"]
        entry_by_name = state["entry_by_name"]
        op_errors = state["op_errors"]
        positional_ids: list[Optional[str]] = []
        slot_errors: list[Optional[str]] = []
        succeeded_entries: list[dict] = []
        for name in op_names:
            e = entry_by_name.get(name)
            if isinstance(e, dict) and isinstance(e.get("media_id"), str):
                positional_ids.append(e["media_id"])
                succeeded_entries.append(e)
                slot_errors.append(None)
            else:
                positional_ids.append(None)
                slot_errors.append(op_errors.get(name))

        success_count = sum(1 for x in positional_ids if x)
        total = len(op_names)

        if success_count == 0:
            first_err = next(iter(op_errors.values()), "timeout_waiting_video")
            self._dispatch_state.pop(job_id, None)
            return {
                "status": "failed",
                "video_bytes": None,
                "video_url": None,
                "error": _classify_flow_error(first_err),
                "error_message": first_err,
                "error_raw": {
                    "raw_dispatch": state["dispatch"],
                    "last_poll": state["last_poll"],
                    "operation_names": op_names,
                    "done": state["done_by_name"],
                    "op_errors": op_errors,
                },
                "cost_usd": 0.0,
                "cost_tokens": None,
            }

        # Eager-ingest URLs + inline encoded_video bytes through the
        # existing media_service. This is also done by the Flow legacy
        # worker; preserving it here means the wrapper is a drop-in
        # replacement for `_handle_gen_video`'s media side-effects.
        entries_with_urls = [
            e for e in succeeded_entries if isinstance(e, dict) and e.get("url")
        ]
        if entries_with_urls:
            try:
                media_service.ingest_urls(entries_with_urls)
            except Exception:  # noqa: BLE001
                logger.exception("auto-ingest from flow gen_video response failed")
        for entry in succeeded_entries:
            if not isinstance(entry, dict):
                continue
            encoded = entry.get("encoded_video")
            mid = entry.get("media_id")
            if not isinstance(encoded, str) or not isinstance(mid, str):
                continue
            try:
                import base64 as _b64
                media_service.ingest_inline_bytes(
                    mid,
                    _b64.b64decode(encoded, validate=False),
                    kind="video",
                    mime="video/mp4",
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "inline ingest from workflow-mode poll failed for %s", mid
                )

        partial_error: Optional[str] = None
        if op_errors:
            unique_errs = sorted({err for err in op_errors.values()})
            partial_error = (
                f"{len(op_errors)}/{total} variants blocked: {', '.join(unique_errs)}"
            )

        raw_payload: dict = {
            "raw_dispatch": state["dispatch"],
            "last_poll": state["last_poll"],
            "operation_names": op_names,
            "media_ids": positional_ids,
            "media_entries": succeeded_entries,
            "op_errors": op_errors,
            "slot_errors": slot_errors,
            "partial_error": partial_error,
        }
        self._dispatch_state.pop(job_id, None)
        return {
            "status": "succeeded",
            "video_bytes": None,    # Flow's bytes are persisted through media_service, not surfaced here
            "video_url": None,
            "error": None,
            "error_message": None,
            "error_raw": None,
            "cost_usd": 0.0,        # Flow is credit-billed; no per-job USD signal
            "cost_tokens": None,
            "media_metadata": None,
            "raw": raw_payload,
        }

    @staticmethod
    def _flow_aspect(aspect: str) -> str:
        """Translate human aspect strings to Flow's enum."""
        mapping = {
            "16:9": "VIDEO_ASPECT_RATIO_LANDSCAPE",
            "9:16": "VIDEO_ASPECT_RATIO_PORTRAIT",
            "1:1": "VIDEO_ASPECT_RATIO_SQUARE",
        }
        # Accept Flow-native enum values verbatim too — older callers
        # pass them directly through node.data.
        if aspect.startswith("VIDEO_ASPECT_RATIO_"):
            return aspect
        return mapping.get(aspect, "VIDEO_ASPECT_RATIO_LANDSCAPE")


def _classify_flow_error(msg: str) -> str:
    """Map Flow's error vocabulary onto the uniform VideoErrorCode set."""
    lc = (msg or "").lower()
    if "filter" in lc or "unsafe" in lc:
        return "content_filtered"
    if "auth" in lc or "401" in lc or "403" in lc:
        return "auth"
    if "quota" in lc or "rate" in lc or "429" in lc:
        return "quota"
    if "timeout" in lc:
        return "timeout"
    if "invalid" in lc or "bad" in lc or "missing" in lc:
        return "bad_input"
    return "internal"
