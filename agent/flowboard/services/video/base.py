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
    # r2v+audio: a publicly-reachable HTTPS URL to a voice/audio reference
    # (role="reference_audio"). Only honored on models with
    # ``capabilities.supports_audio_ref``; dropped-with-warning otherwise.
    # Contract §11.3: audio puts the request into "reference media mode",
    # which forbids a first_frame block — the provider drops first_frame
    # when audio is present.
    audio_ref_url: Optional[str]
    motion_prompt: str
    duration_seconds: int
    aspect_ratio: str          # "1:1" | "16:9" | "9:16"
    resolution: str            # "720p" | "1080p"
    generate_audio: bool
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
