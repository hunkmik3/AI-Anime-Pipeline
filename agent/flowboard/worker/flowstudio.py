"""Flow Studio (`/giantflow`) — the ``flow_gen_image`` worker handler.

Lifted from manga_extract's ``worker/processor.py`` (lines 2572-2938) into its own
module rather than merged into this repo's 1099-line processor. Two reasons: the
block is self-contained (one handler, three engine adapters, one colour-matching
helper), and keeping it separate makes the port reversible while the studio is
still standalone — it is not yet wired into projects, per-project RBAC, or the
credit budgets.

Only two things changed from the source: the engine imports point at
``services.flowstudio`` instead of ``services.comic``, and the two PNG-ingest
helpers are imported from ``processor`` instead of being duplicated.

Registered in ``processor._DEFAULT_HANDLERS`` as ``"flow_gen_image"``.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import Optional

from flowboard.db import get_session
from flowboard.db.models import Request
from flowboard.services import media as media_service


_PRESERVE_COLORS_CLAUSE = (
    " QUAN TRỌNG: giữ nguyên chính xác bảng màu của ảnh gốc — cùng tông màu (hue), "
    "độ bão hoà và cân bằng trắng. Tuyệt đối không color grading, không ám hồng/đỏ, "
    "không làm ấm màu. (Preserve the original color palette exactly: identical hues, "
    "saturation and white balance as the reference. No color grading, no warm or pink tint.)"
)


# Layer 2 of "keep original colors": how far to pull the result's chroma back
# onto the reference. 1.0 = fully locked to the reference, 0 = disabled.
_COLOR_MATCH_STRENGTH = float(os.getenv("FLOWBOARD_COLOR_MATCH_STRENGTH", "1.0"))


def _match_reference_colors(img_bytes: bytes, ref_bytes: bytes, strength: float) -> bytes:
    """Shift a generated image's mean a*/b* (LAB chroma) onto the reference's.

    This removes the global colour cast image models add while leaving L
    (luminance) alone, so the intended stylisation — contrast, shading, detail —
    survives; only the tint is corrected. Measured on the worst-cast samples:
    mean Δa*/Δb* vs the reference went from +11.0/+5.7 to −0.6/−0.2.

    Returns the input unchanged on any failure — colour matching must never
    break a generation. Sync + CPU-heavy: call via ``asyncio.to_thread``."""
    try:
        import cv2
        import numpy as np

        out = cv2.imdecode(np.frombuffer(img_bytes, np.uint8), cv2.IMREAD_COLOR)
        ref = cv2.imdecode(np.frombuffer(ref_bytes, np.uint8), cv2.IMREAD_COLOR)
        if out is None or ref is None:
            return img_bytes
        lab = cv2.cvtColor(out, cv2.COLOR_BGR2LAB).astype(np.float32)
        # Means come off 256² copies — identical to the full-res mean for our
        # purposes and keeps a 4K pair cheap.
        small = cv2.resize(out, (256, 256), interpolation=cv2.INTER_AREA)
        olab = cv2.cvtColor(small, cv2.COLOR_BGR2LAB).astype(np.float32)
        rlab = cv2.cvtColor(
            cv2.resize(ref, (256, 256), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2LAB
        ).astype(np.float32)
        for ch in (1, 2):  # a*, b* only — never touch L
            delta = (rlab[:, :, ch].mean() - olab[:, :, ch].mean()) * strength
            lab[:, :, ch] = np.clip(lab[:, :, ch] + delta, 0, 255)
        fixed = cv2.cvtColor(lab.astype(np.uint8), cv2.COLOR_LAB2BGR)
        # Re-encode in the SOURCE's format: PNG in → PNG out (stays lossless),
        # JPEG in (what Ark returns) → JPEG q95, which adds negligible loss on an
        # already-lossy image and avoids ballooning a 4K frame from ~1MB to ~18MB.
        if img_bytes[:3] == b"\xff\xd8\xff":
            ok, buf = cv2.imencode(".jpg", fixed, [cv2.IMWRITE_JPEG_QUALITY, 95])
        else:
            ok, buf = cv2.imencode(".png", fixed)
        return buf.tobytes() if ok else img_bytes
    except Exception:  # noqa: BLE001
        return img_bytes


async def handle_flow_gen_image(params: dict) -> tuple[dict, Optional[str]]:
    """Flow-clone studio: text→image / edit (1-4 variants), fully API-only (no
    Flow bridge, no paygate tier, no node binding). Interchangeable engines,
    chosen by ``provider`` (default env ``FLOW_IMAGE_PROVIDER`` else "gemini"):

      - "gemini"  → direct Gemini API, reference/source images sent inline
                    (works fully locally).
      - "atrium"  → Atrium passthrough; input images must be PUBLIC urls
                    (``PUBLIC_MEDIA_BASE_URL`` / tunnel), so refs+edit need that
                    set. Plain text→image works without it.
      - "avis"    → Avis gateway (Seedream 5.0 Pro), async job + poll under the
                    hood; input images inline (base64). Needs ``AVIS_API_KEY``.

    Each result is cached as a local media id, returned in ``media_ids``."""
    from flowboard.services.flowstudio.errors import BridgeEditError

    prompt = params.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return {}, "missing_prompt"
    prompt = prompt.strip()

    provider = params.get("provider")
    if not isinstance(provider, str) or not provider.strip():
        provider = os.getenv("FLOW_IMAGE_PROVIDER", "gemini")
    provider = provider.strip().lower() or "gemini"

    image_model = params.get("image_model")
    image_model = image_model.strip() if isinstance(image_model, str) and image_model.strip() else ""
    if provider == "avis":
        # Avis catalog id for Seedream 5.0 Pro — no version/date suffix.
        image_model = image_model or "dola-seedream-5-0-pro"
    elif not image_model.startswith("gemini-"):
        # The Gemini and Atrium engines both speak Gemini model ids.
        image_model = "gemini-2.5-flash-image"
    aspect = params.get("aspect_ratio")
    aspect = aspect if isinstance(aspect, str) and aspect else "1:1"
    image_size = params.get("image_size")
    image_size = image_size if isinstance(image_size, str) and image_size else None
    try:
        variant_count = int(params.get("variant_count") or 1)
    except (TypeError, ValueError):
        variant_count = 1

    ref_ids = [r for r in (params.get("ref_media_ids") or []) if isinstance(r, str) and r]
    source_id = params.get("source_media_id")
    source_id = source_id if isinstance(source_id, str) and source_id else None
    if source_id and media_service.cached_path(source_id) is None:
        return {}, "source_not_found"

    # "Keep original colors" — only meaningful when there IS a reference/source to
    # match. Appended here (not in the UI) so the stored prompt stays clean for
    # history/reuse.
    if params.get("preserve_colors") and (ref_ids or source_id):
        prompt += _PRESERVE_COLORS_CLAUSE

    # Live progress: write {done, total} onto the running Request row as variants
    # land, so the frontend can poll it for a "k/N · pct%" placeholder. Throttled
    # to ≤1 write / ~0.8s (the final done==total always writes) so a big-batch gen
    # doesn't flood the DB with one write per variant under concurrency.
    rid = params.get("__request_id")
    _last_progress = [0.0]

    def _progress(done: int, total: int) -> None:
        if rid is None:
            return
        now = time.monotonic()
        if done < total and (now - _last_progress[0]) < 0.8:
            return
        _last_progress[0] = now
        try:
            with get_session() as s:
                req = s.get(Request, rid)
                if req is not None and req.status == "running":
                    req.result = {"progress": {"done": int(done), "total": int(total)}}
                    s.add(req)
                    s.commit()
        except Exception:  # noqa: BLE001 — best-effort; never break a gen
            pass

    provider_used = provider
    try:
        if provider == "atrium":
            from flowboard.services.flowstudio import atrium_api, gemini_api, r2

            needs_input = bool(source_id) or bool(ref_ids)
            # Atrium can only ingest input images by PUBLIC url — either R2
            # (agent uploads, recommended) or a tunnel to /media. When NEITHER
            # is configured, reference/edit requests have no public url, so we
            # transparently fall back to the Gemini engine (inline bytes) for
            # THAT request; pure text→image stays on Atrium.
            has_public = r2.is_configured() or atrium_api.public_media_base() is not None
            if needs_input and not has_public and gemini_api.api_key():
                provider_used = "gemini"
                outs = await _flow_gen_gemini(
                    prompt, image_model, aspect, image_size, variant_count, ref_ids, source_id,
                    on_progress=_progress,
                )
            else:
                outs = await _flow_gen_atrium(
                    prompt, image_model, aspect, image_size, variant_count, ref_ids, source_id,
                    on_progress=_progress,
                )
        elif provider == "avis":
            outs = await _flow_gen_avis(
                prompt, image_model, aspect, image_size, variant_count, ref_ids, source_id,
                on_progress=_progress,
            )
        else:
            outs = await _flow_gen_gemini(
                prompt, image_model, aspect, image_size, variant_count, ref_ids, source_id,
                on_progress=_progress,
            )
    except BridgeEditError as exc:
        return {}, f"gen_failed: {exc.reason}"[:200]
    except _FlowGenError as exc:
        return {}, str(exc)[:200]
    except Exception as exc:  # noqa: BLE001
        return {}, f"gen_failed: {type(exc).__name__}: {exc}"[:200]

    if not outs:
        return {}, "no_image_generated"

    # "Keep original colors" layer 2 — deterministic cast removal. Only runs when
    # there's ONE unambiguous colour reference: the edit source, or a single ref.
    # A multi-reference gen (e.g. character + environment) has no single palette
    # to match, so we leave it to the prompt hint alone.
    color_ref_id = source_id or (ref_ids[0] if len(ref_ids) == 1 else None)
    color_matched = False
    if params.get("preserve_colors") and color_ref_id and _COLOR_MATCH_STRENGTH > 0:
        ref_path = media_service.cached_path(color_ref_id)
        if ref_path is not None:
            def _match_all() -> list:
                try:
                    ref_bytes = ref_path.read_bytes()
                except OSError:
                    return outs
                return [_match_reference_colors(o, ref_bytes, _COLOR_MATCH_STRENGTH) for o in outs]

            matched = await asyncio.to_thread(_match_all)
            color_matched = matched is not outs
            outs = matched

    # Ingest (PNG decode/encode + disk write + DB commit) off the event loop so a
    # 12-variant gen doesn't stall everyone else's polls during the writes.
    # Imported here, not at module scope: processor imports THIS module, so a
    # top-level import back into it would be a cycle.
    from flowboard.worker.processor import _ingest_pngs

    media_ids = await asyncio.to_thread(_ingest_pngs, outs)
    # Offload the results to R2 in the BACKGROUND so viewers on the public tunnel
    # hostname are served from the CDN (routes/media.py redirects) — never blocks
    # or fails the gen; local serving keeps working without it.
    _spawn_result_offload(list(media_ids))
    return {
        "media_ids": media_ids,
        "provider_used": provider_used,
        "image_model": image_model,
        "color_matched": color_matched,
        "node_id": params.get("__node_id"),
    }, None


# Keep strong references to fire-and-forget offload tasks so the event loop
# can't garbage-collect them mid-flight.
_offload_tasks: set = set()


def _spawn_result_offload(media_ids: list) -> None:
    """Push finished images to R2 in the background, best-effort.

    Purpose is CDN serving, not durability: a ~20 MB 4K PNG viewed repeatedly
    would otherwise stream up this machine's uplink every time, so
    ``routes/media.py`` redirects non-local viewers to the R2 copy when one
    exists. Failure is logged and ignored — local serving keeps working.
    """
    from flowboard.services.flowstudio import r2

    if not (r2.is_configured() and media_ids):
        return

    def _upload_all() -> None:
        for mid in media_ids:
            try:
                r2.upload_result(mid)
            except Exception as exc:  # noqa: BLE001 — the CDN copy is best-effort
                logger.warning("r2 result offload failed for %s: %s", mid, exc)

    task = asyncio.create_task(asyncio.to_thread(_upload_all), name="r2-result-offload")
    _offload_tasks.add(task)
    task.add_done_callback(_offload_tasks.discard)


class _FlowGenError(RuntimeError):
    """Caller-facing flow-gen failure carrying an already-formatted reason."""


async def _flow_gen_gemini(
    prompt: str, image_model: str, aspect: str, image_size: Optional[str],
    variant_count: int, ref_ids: list, source_id: Optional[str],
    on_progress=None,
) -> list[bytes]:
    """Direct Gemini engine — reference/source images sent inline (bytes)."""
    from flowboard.services.flowstudio import gemini_api

    def _load(mid: str) -> Optional[bytes]:
        p = media_service.cached_path(mid)
        if p is None:
            return None
        try:
            return p.read_bytes()
        except OSError:
            return None

    def _load_all() -> tuple[Optional[bytes], list[bytes]]:
        src = _load(source_id) if source_id else None
        refs = [b for b in (_load(r) for r in ref_ids) if b]
        return src, refs

    source_bytes, ref_bytes = await asyncio.to_thread(_load_all)
    if source_id and source_bytes is None:
        raise _FlowGenError("source_not_found")

    if source_bytes is not None:
        # Edit/refine: re-render the source, preserving its frame (empty aspect).
        return await gemini_api.edit_image_variants(
            source_bytes, prompt, ref_bytes or None,
            image_model=image_model, aspect_ratio="", variant_count=variant_count,
            on_progress=on_progress,
        )
    return await gemini_api.generate_image_variants(
        prompt, ref_bytes or None,
        image_model=image_model, aspect_ratio=aspect,
        variant_count=variant_count, image_size=image_size,
        on_progress=on_progress,
    )


async def _flow_gen_avis(
    prompt: str, image_model: str, aspect: str, image_size: Optional[str],
    variant_count: int, ref_ids: list, source_id: Optional[str],
    on_progress=None,
) -> list[bytes]:
    """Avis gateway (Seedream 5.0 Pro) engine — source + reference images sent
    INLINE as base64, so refs and edit work fully locally."""
    from flowboard.services.flowstudio import avis_api

    if not avis_api.is_configured():
        raise _FlowGenError("avis_not_configured: set AVIS_API_KEY in .env")

    def _load(mid: str) -> Optional[bytes]:
        p = media_service.cached_path(mid)
        if p is None:
            return None
        try:
            b = p.read_bytes()
        except OSError:
            return None
        return b or None

    def _load_all() -> tuple[Optional[bytes], list[bytes]]:
        src = _load(source_id) if source_id else None
        refs = [b for b in (_load(r) for r in ref_ids) if b]
        return src, refs

    source_bytes, ref_bytes = await asyncio.to_thread(_load_all)
    if source_id and source_bytes is None:
        raise _FlowGenError("source_not_found")

    images = ([source_bytes] if source_bytes else []) + ref_bytes
    return await avis_api.generate_image_variants(
        prompt, images or None,
        image_model=image_model,
        aspect_ratio="" if source_id else aspect,
        variant_count=variant_count,
        image_size=None if source_id else image_size,
        on_progress=on_progress,
    )


# Reference-counts R2 input uploads shared across CONCURRENT flow_gen_image jobs
# that happen to tag the same source/ref media_id. Without this, one job's cleanup
# could delete the R2 object while a still-running sibling job's Atrium call is
# mid-fetch of that same URL, surfacing as "atrium 400: fail to fetch media url
# (404)". A real threading.Lock, not asyncio.Lock: the mutations happen inside
# asyncio.to_thread'd sync functions, i.e. on worker-pool threads.
_r2_input_refcount: dict[str, int] = {}
_r2_input_refcount_lock = threading.Lock()


def _r2_input_acquire(media_id: str) -> None:
    with _r2_input_refcount_lock:
        _r2_input_refcount[media_id] = _r2_input_refcount.get(media_id, 0) + 1


def _r2_input_release_and_maybe_delete(media_id: str) -> None:
    from flowboard.services.flowstudio import r2

    with _r2_input_refcount_lock:
        # Not tracked → this input was never uploaded to R2 (e.g. it was
        # self-hosted through the tunnel), so there is nothing in the bucket to
        # release. No-op rather than blindly issuing a delete.
        if media_id not in _r2_input_refcount:
            return
        remaining = _r2_input_refcount[media_id] - 1
        if remaining <= 0:
            _r2_input_refcount.pop(media_id, None)
        else:
            _r2_input_refcount[media_id] = remaining
    if remaining <= 0:
        r2.delete_media(media_id)


def _atrium_input_url(media_id: str) -> Optional[str]:
    """Public URL Atrium can fetch this input image from.

    Prefer SELF-HOSTING through the tunnel: the machine already has the image
    cached, so hand Atrium a downscaled-JPEG thumbnail URL served straight off
    this box (``PUBLIC_MEDIA_BASE_URL`` → /api/media/<id>/thumb). That avoids the
    r2.dev input-fetch flakiness (intermittent "failed to fetch media URL") and
    the per-gen upload/delete churn. R2 stays in use for RESULT offload — only
    input hosting moves off it.

    Falls back to an R2 upload (with refcounted cleanup) only when no tunnel is
    configured. Sync (disk/boto3) — call via ``asyncio.to_thread``."""
    from flowboard.services.flowstudio import r2, atrium_api

    if atrium_api.public_media_base() is not None:
        return atrium_api.media_input_url(media_id)
    if r2.is_configured():
        url = r2.upload_media(media_id)
        if url:
            _r2_input_acquire(media_id)
        return url
    return None


async def _flow_gen_atrium(
    prompt: str, image_model: str, aspect: str, image_size: Optional[str],
    variant_count: int, ref_ids: list, source_id: Optional[str],
    on_progress=None,
) -> list[bytes]:
    """Atrium engine — input images must be PUBLIC urls (fileData.fileUri),
    served from R2 (preferred) or a tunnel."""
    from flowboard.services.flowstudio import atrium_api, r2

    if not atrium_api.is_configured():
        raise _FlowGenError("atrium_not_configured: set ATRIUM_CLIENT_ID/ATRIUM_CLIENT_SECRET in .env")

    needs_input = bool(source_id) or bool(ref_ids)
    if needs_input and not (r2.is_configured() or atrium_api.public_media_base() is not None):
        raise _FlowGenError(
            "atrium_needs_public_url: references/edit on Atrium need R2 (R2_* in .env) or a "
            "tunnel (PUBLIC_MEDIA_BASE_URL). Plain text→image works without either."
        )

    # Build the public input URLs (R2 uploads happen here, off the event loop).
    def _build_urls() -> list[str]:
        urls: list[str] = []
        for mid in ([source_id] if source_id else []) + list(ref_ids):
            try:
                u = _atrium_input_url(mid)
            except Exception as exc:  # noqa: BLE001 — upload/network failure
                raise _FlowGenError(f"r2_upload_failed: {type(exc).__name__}: {exc}"[:180])
            if u:
                urls.append(u)
        return urls

    image_urls = await asyncio.to_thread(_build_urls)
    if needs_input and not image_urls:
        raise _FlowGenError("atrium_input_unavailable: could not resolve a public URL for the input image")

    input_ids = ([source_id] if source_id else []) + list(ref_ids)
    try:
        # On edit (source present) preserve the frame by not forcing an aspect.
        return await atrium_api.generate_image_variants(
            prompt, image_urls or None,
            image_model=image_model,
            aspect_ratio="" if source_id else aspect,
            variant_count=variant_count,
            image_size=image_size,
            on_progress=on_progress,
        )
    finally:
        # Inputs are normally self-hosted through the tunnel now (nothing in R2
        # to clean up — the release below no-ops for those). This only does real
        # work on the R2-fallback path: Atrium has fetched the inputs by now, so
        # drop them from the bucket. Many jobs run at once, so a sibling may still
        # rely on the same media_id — the refcounted release only deletes once
        # every concurrent user is done. Local cache is always kept.
        if r2.is_configured() and input_ids:
            def _cleanup() -> None:
                for mid in input_ids:
                    _r2_input_release_and_maybe_delete(mid)
            await asyncio.to_thread(_cleanup)
