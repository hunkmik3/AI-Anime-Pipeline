"""Protocol + shared types for the video provider layer.

Every provider implementation (Flow, Dreamina, future Kling/Hailuo) conforms
to ``VideoProvider``. The registry (``registry.py``) is the only thing that
knows the concrete classes; everything else routes through ``get_video_model``.

Design choices (locked at Phase 5 kickoff):

- **Per-model registry**, not per-provider. ``VideoModelEntry`` carries a
  ``capabilities`` declaration so the frontend can render the right
  controls without hard-coding model knowledge.
- **Dual-mode submit** from day one: ``submit()`` accepts both ``first_frame_url``
  AND ``reference_images``. Providers whose model is i2v-only drop the refs
  with a warning rather than failing — the caller is told via
  ``VideoGenSubmitResult.warnings``.
- **Eager download** on the poll-success path. Dreamina's signed TOS URL
  expires alongside its 24h file lifecycle, so the provider downloads bytes
  before reporting success.
- **Uniform error vocab** (``VideoErrorCode``) so the UI maps one set of
  codes regardless of which provider failed; the raw envelope is preserved
  in ``error_raw`` for diagnosis.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional, Protocol, TypedDict, runtime_checkable


VideoErrorCode = Literal[
    "content_filtered",  # provider's safety filter rejected the prompt or input
    "auth",              # invalid / missing / expired API key
    "quota",             # rate limit or per-period cap hit
    "bad_input",         # unreachable image URL, invalid params, etc.
    "timeout",           # local polling exhausted before terminal state
    "internal",          # everything else (provider 5xx, parse failures)
]


class VideoError(RuntimeError):
    """Raised by provider methods for unrecoverable failures.

    ``code`` is the uniform vocab the UI displays; ``raw`` retains the full
    upstream envelope for diagnosis. Submit-time failures are raised; poll
    failures are returned via ``VideoGenPollResult`` (so partial state
    survives) but reuse the same vocab.
    """

    def __init__(
        self,
        code: VideoErrorCode,
        message: str,
        *,
        raw: Optional[dict] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.raw = raw or {}


@dataclass(frozen=True)
class VideoProviderCapability:
    """Static declaration of what a given model supports.

    Frontend reads these via ``GET /api/video/models`` and renders
    settings panels accordingly. Backend uses them to gate-and-warn on
    submit (drop unsupported fields, never silent-fail).
    """

    supports_multi_ref: bool          # r2v: pass N reference_images (role="reference_image")
    supports_last_frame: bool         # keyframe interpolation
    supports_audio_toggle: bool       # can flip generate_audio on/off
    supports_audio_ref: bool          # r2v+audio: accept an audio_url role="reference_audio" block
    max_refs: int                     # 0 when supports_multi_ref is False
    aspect_ratios: tuple[str, ...]    # e.g. ("1:1", "16:9", "9:16")
    resolutions: tuple[str, ...]      # e.g. ("720p", "1080p")
    durations: tuple[int, ...]        # allowed seconds
    # Phase 8.1.5d: r2v with video_url role="reference_video" (Seedance 2.0;
    # contract §11.9). Default False so pre-existing caps need no change.
    supports_video_ref: bool = False
    # Person-driven (KYC) inputs — portrait→video / lip-sync / video-reference
    # via identity-verified KYC assets. Only the Avis Seedance 2.0 models.
    # The frontend shows the "Person-driven (KYC)" toggle when this is True.
    supports_kyc: bool = False
    # Dedicated DanceSee /api/v1/b2b/* path (content-filter disabled). Only
    # Seedance 2.0/2.5; the account behind AVIS_API_KEY must be userType=B2B.
    supports_b2b_unmoderated: bool = False
    # Output container. An EMPTY tuple means the model has no `outputFormat`
    # param at all and sending one is a 400 — so the field is only ever emitted
    # when this is non-empty. Seedance 2.5 (Avis, 20 Aug 2026): mp4 | mov, where
    # mov keeps higher colour precision and is what edit/extend want.
    output_formats: tuple[str, ...] = ()
    # Seedance 2.5's omni reference-to-video family. One endpoint covers three
    # subtasks with DIFFERENT constraints — plain reference-to-video, editing an
    # existing clip, and extending one — and naming which you mean lets Avis
    # check that subtask's rules while the request is still synchronous. Without
    # it a bad edit/extend is accepted, queued, and fails minutes later with the
    # reservation already taken.
    supports_omni_reference: bool = False


class VideoGenSubmitParams(TypedDict, total=False):
    """Inputs to ``VideoProvider.submit``.

    ``first_frame_url`` is required for all current models (Seedance treats
    a lone image as first-frame; Flow always needs at least one start
    media). ``reference_images`` is the r2v anchor list — empty when the
    caller has no refs OR when capability gate strips them.

    ``motion_prompt`` is the user-authored description BEFORE any inline
    flag mangling — providers that use inline flags (Dreamina) build the
    final prompt themselves from these structured fields.
    """

    first_frame_url: str
    reference_images: list[str]
    last_frame_url: Optional[str]
    # Voice/audio reference(s) the clip should follow: media_ids, data URLs, or
    # public HTTPS URLs. ``audio_ref_url`` is the single/first ref (back-compat);
    # ``audio_ref_urls`` is the ordered multi-ref list. Only honored on models
    # with ``capabilities.supports_audio_ref``; dropped-with-warning otherwise.
    # Audio is REFERENCE MEDIA — it can't be combined with a first/last-frame
    # block (Avis 400s), so the provider demotes the start image to a
    # referenceImage and switches to r2v when an audio ref is present.
    audio_ref_url: Optional[str]
    audio_ref_urls: list[str]
    # @audioN labels parallel to ``audio_ref_urls`` — the provider orders the
    # referenceAudio blocks by label digit so @audio1 is first (parity with
    # @image / @video). All-null (no labels) preserves input order.
    audio_ref_labels: list[Optional[str]]
    # Phase 8.1.5d: reference video URLs (role="reference_video", §11.9).
    # Only honored when ``capabilities.supports_video_ref``; dropped-with-
    # warning otherwise. Reference media → r2v mode (no first_frame).
    reference_videos: list[str]
    motion_prompt: str
    duration_seconds: int
    aspect_ratio: str          # "1:1" | "16:9" | "9:16" | … | "adaptive"
    resolution: str            # "720p" | "1080p"
    generate_audio: bool
    # Output container — "mp4" | "mov". Only honored on models declaring
    # ``capabilities.output_formats``; dropped-with-warning otherwise.
    output_format: Optional[str]
    # Which omni reference-to-video subtask this is: "auto" | "reference" |
    # "edit" | "extend". Only honored when ``capabilities.supports_omni_reference``.
    #
    # `edit` and `extend` both operate ON an existing clip, so both require a
    # video reference and an adaptive ratio — the output follows the source, and
    # asking for 16:9 on a 9:16 source is a contradiction rather than a crop.
    # Those two rules are checked here so the caller is told at submit time
    # instead of by a task that fails minutes later.
    omni_reference_task_type: Optional[str]
    # Person-driven (KYC) — Avis Seedance 2.0 only. Already-resolved Avis KYC
    # assetIds (the worker creates/caches them from media_ids before dispatch).
    # When any is set the provider emits kyc*AssetId content parts and skips the
    # regular reference parts. Only honored when ``capabilities.supports_kyc``.
    kyc_image_asset_id: Optional[str]
    # Multiple KYC image identities, ordered by @imageN. When set, the provider
    # emits one kycImageAssetId content part per id — this is how a person-driven
    # gen locks MANY real-person characters, not just the first one.
    kyc_image_asset_ids: Optional[list[str]]
    kyc_audio_asset_id: Optional[str]
    kyc_video_asset_id: Optional[str]
    # DanceSee B2B unmoderated path. When True the provider POSTs/GETs
    # /api/v1/b2b/video/* (and KYC assets via /api/v1/b2b/kyc/assets) instead
    # of the regular moderated endpoints. No per-request flag is sent upstream
    # — the dedicated prefix is the switch. Only honored on Seedance 2.0/2.5.
    content_filter_disabled: bool
    # Flow-only fields. Other providers ignore these. Kept on the same
    # TypedDict so the worker doesn't need to branch params per provider.
    project_id: str
    paygate_tier: str
    video_quality: Optional[str]


class VideoGenSubmitResult(TypedDict):
    external_job_id: str
    submitted_at: int            # unix seconds (for TTL math + telemetry)
    # Capability-degradation warnings — caller surfaces in Request.result
    # so the UI can show "1 ref dropped: model is i2v-only".
    warnings: list[str]


class VideoGenPollResult(TypedDict, total=False):
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    # Set only on terminal "succeeded". Provider downloads eagerly before
    # returning so the worker can persist via media_service.ingest_inline_bytes.
    video_bytes: Optional[bytes]
    # The (already-expired-soon) signed URL. Kept so we can log the source
    # for debugging, but never fetched again — bytes are authoritative.
    video_url: Optional[str]
    # Uniform error code on "failed"; None otherwise.
    error: Optional[VideoErrorCode]
    error_message: Optional[str]
    error_raw: Optional[dict]
    duration_seconds: Optional[float]
    cost_usd: float              # 0.0 when pricing rate is not configured
    cost_tokens: Optional[int]   # raw billable units when the provider reports them
    media_metadata: Optional[dict]
    # Provider-native job state to attach to Request.result for diagnosis.
    raw: Optional[dict]


@runtime_checkable
class VideoProvider(Protocol):
    """Every video provider conforms to this surface.

    Lifecycle:
      1. ``submit(params)`` — POST to upstream, return opaque job id + warnings
      2. ``poll(external_job_id)`` — GET upstream, return state + bytes on success
      3. ``run_to_completion(params)`` — convenience driver that calls submit +
         polls on the provider's natural cadence until terminal state. The
         worker calls this; tests can drive submit/poll individually.

    Providers handle their own capability gating: pass any params you want,
    unsupported fields are dropped (with warnings) rather than rejected.
    """

    name: str                              # registry key (e.g. "dreamina")
    capabilities: VideoProviderCapability  # may be model-specific; resolved at instantiation

    async def submit(self, params: VideoGenSubmitParams) -> VideoGenSubmitResult:
        ...

    async def poll(self, external_job_id: str) -> VideoGenPollResult:
        ...

    async def run_to_completion(
        self, params: VideoGenSubmitParams
    ) -> tuple[VideoGenSubmitResult, VideoGenPollResult]:
        ...

    async def is_available(self) -> bool:
        """Cheap probe: API key configured, CLI present, etc. No real call."""
        ...
