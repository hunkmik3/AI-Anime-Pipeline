"""Atrium 4K image upscaler — Nano Banana Pro (GEM_PIX_2).

A self-contained client for the batch-upscale tool. It re-renders a source image
at 4K through Atrium's partner image API, conditioned on the source frame + a
fixed "clean anime master" enhance prompt (see UPSCALE_PROMPT). The source is
passed as a PUBLIC URL (Atrium downloads it server-side) — the caller hoists the
local media to R2 via ``media_id_to_public_url`` and passes that URL here.

Ported/trimmed from GiantFlowStudio's ``services/comic/atrium_api.py``:
- KEPT: the per-connection download-rate watchdog (S3 throttles per TCP 4-tuple,
  so a slow download re-draws a fresh connection instead of waiting out a crawl),
  jittered-exponential retry for 5xx/429/timeouts, and the safety-empty retry
  (anime art trips a transient false-positive safety block that clears on retry).
- DROPPED: the 4K concurrency gate — Atrium confirmed no per-second/concurrent
  limit for this account, so every image is submitted at once.

Auth: ``ATRIUM_CLIENT_ID`` / ``ATRIUM_CLIENT_SECRET`` (headers). Base:
``ATRIUM_BASE_URL`` or https://studio.atrium.art. Model: ``GEM_PIX_2`` (override
with ``FLOWBOARD_ATRIUM_UPSCALE_MODEL``).
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_BASE = "https://studio.atrium.art"
_TIMEOUT_S = 240.0
_DOWNLOAD_BUDGET_S = float(os.getenv("FLOWBOARD_ATRIUM_DOWNLOAD_BUDGET_S", "90"))
_DOWNLOAD_GRACE_S = 8.0
_DOWNLOAD_MIN_RATE = 300 * 1024  # bytes/s — below this after grace ⇒ re-draw
_DOWNLOAD_READ_TIMEOUT_S = 15.0
_DOWNLOAD_ATTEMPT_CAP_S = 120.0
MAX_ATTEMPTS = int(os.getenv("FLOWBOARD_ATRIUM_MAX_ATTEMPTS", "6"))
SAFETY_MAX_ATTEMPTS = 3
_BACKOFF_S = float(os.getenv("FLOWBOARD_ATRIUM_BACKOFF_S", "2.0"))
_BACKOFF_CAP_S = float(os.getenv("FLOWBOARD_ATRIUM_BACKOFF_CAP_S", "16.0"))
# Cap concurrent Atrium calls so a big "Upscale all" batch doesn't hammer the
# Gemini backend into 429 RESOURCE_EXHAUSTED. Excess jobs await this gate and
# run as slots free up. Per-process (per env). Overridable via env.
_MAX_CONCURRENT = max(1, int(os.getenv("FLOWBOARD_UPSCALE_CONCURRENCY", "6") or "6"))
_gate = asyncio.Semaphore(_MAX_CONCURRENT)

# Keep the upscaled frame's colours true to the ORIGINAL. The upscale model adds a
# consistent red/pink cast (measured ~a* +2.9 across real frames). We neutralise it
# in post by shifting ONLY the result's mean a*/b* (LAB chroma) onto the source's —
# L (luminance/detail/contrast) is never touched, so only the tint is corrected.
# Default on; FLOWBOARD_UPSCALE_KEEP_COLOR=0 disables. Strength 1.0 = fully locked.
KEEP_ORIGINAL_COLOR = (os.getenv("FLOWBOARD_UPSCALE_KEEP_COLOR", "1") or "1").strip().lower() not in (
    "0", "false", "no", "off",
)
COLOR_MATCH_STRENGTH = float(os.getenv("FLOWBOARD_UPSCALE_COLOR_STRENGTH", "1.0") or "1.0")

_FATAL_STATUSES = {400, 401, 403, 404}
_RETRYABLE_4XX_SUBSTR = ("fetch media url", "image is not valid")

# Nano Banana Pro on Atrium = the Vertex publisher model "gemini-3-pro-image"
# (the user calls it "nano banana 2"). NOTE: "GEM_PIX_2" is the Google FLOW
# bridge's key for the same model — Atrium is NOT the Flow bridge; it takes the
# real gemini-* id verbatim (a bad id → 400 "Invalid Endpoint name"). Alternatives
# Atrium accepts: gemini-2.5-flash-image (Nano Banana 1), gemini-3.1-flash-image.
UPSCALE_MODEL = (
    os.getenv("FLOWBOARD_ATRIUM_UPSCALE_MODEL", "gemini-3-pro-image").strip()
    or "gemini-3-pro-image"
)

# The fixed enhance/upscale prompt (supplied by the user 2026-08-26). Kept
# verbatim — it encodes the "clean anime master frame" intent + a long negative.
UPSCALE_PROMPT = (
    "Enhance the source image into a clean, high-quality professional 2D anime frame "
    "while preserving the original composition, camera angle, framing, character design, "
    "pose, facial features, expression, clothing, background layout, lighting direction, "
    "and original color palette.\n\n"
    "MAIN GOAL:\n"
    "Improve overall image fidelity and technical cleanliness without redesigning or "
    "altering the artwork.\n\n"
    "IMAGE CLEANUP:\n"
    "Remove compression artifacts, JPEG artifacts, AI generation artifacts, dirty pixels, "
    "bad pixels, pixelation, blockiness, mosquito noise, grain, chroma noise, edge noise, "
    "color speckles, and random texture contamination.\n"
    "Remove color banding, posterization artifacts, broken gradients, uneven color patches, "
    "dirty color transitions, and unwanted dithering.\n"
    "Repair malformed edges, broken contours, accidental blobs, tiny visual glitches, "
    "distorted details, and inconsistent rendering.\n"
    "Clean up jagged edges and aliasing while keeping the artwork crisp.\n"
    "Do not introduce artificial sharpening halos.\n\n"
    "ANIME RENDERING:\n"
    "Preserve an authentic high-quality Japanese 2D anime appearance.\n"
    "Clean, thin, controlled line art with consistent line weight.\n"
    "Smooth and confident contours.\n"
    "Clean facial features, anime eyes, hair strands, clothing edges, fingers, and silhouette.\n"
    "Preserve intentional line-weight variation.\n"
    "Avoid thick vector-like outlines.\n\n"
    "CEL SHADING:\n"
    "Use clean solid-color anime cel shading.\n"
    "Clearly separated local colors and shadow shapes.\n"
    "Maintain approximately 2-tone solid-color cel shading where applicable.\n"
    "Shadows must form deliberate, readable graphic shapes.\n"
    "Keep skin, hair, clothes, and environment colors clean and stable.\n"
    "No muddy blended shading.\n"
    "No painterly brush texture.\n"
    "No excessive micro-shading.\n"
    "No fake 3D rendering.\n\n"
    "COLOR QUALITY:\n"
    "Smooth large color areas.\n"
    "Eliminate patchy or contaminated colors.\n"
    "Repair banding while preserving intentional hard cel-shading boundaries.\n"
    "Preserve the original hue, saturation, brightness, white balance, and overall color grading.\n"
    "Do not recolor the image.\n"
    "Do not introduce purple, green, blue, yellow, or magenta color casts.\n"
    "Maintain clean solid local colors.\n\n"
    "DETAIL ENHANCEMENT:\n"
    "Recover clean high-resolution anime details where the source is soft or degraded.\n"
    "Improve clarity of eyes, eyelashes, eyebrows, hair edges, clothing seams, accessories, "
    "hands, and important environmental details.\n"
    "Sharpen structural details selectively, not globally.\n"
    "Keep flat areas smooth and simple rather than adding unnecessary texture.\n"
    "Preserve intentional depth of field and existing blur.\n\n"
    "ABSOLUTE PRESERVATION:\n"
    "Do not change composition, crop, aspect ratio, perspective, camera position, lens "
    "appearance, character proportions, anatomy, pose, expression, facial identity, hairstyle, "
    "costume design, object placement, background architecture, lighting setup, or scene content.\n\n"
    "Do not add or remove objects.\n\n"
    "FINAL RESULT:\n"
    "A pristine, high-resolution anime master frame with smooth solid colors, clean thin line "
    "art, crisp cel-shading boundaries, stable local colors, clean silhouettes, natural anime "
    "detail hierarchy, and virtually no visible digital or AI artifacts.\n\n"
    "NEGATIVE:\n"
    "noise, grain, JPEG artifacts, compression blocks, pixelation, bad pixels, dead pixels, "
    "chromatic noise, color speckles, banding, dirty gradients, posterization artifacts, dithering, "
    "aliasing, jagged contours, oversharpening, sharpening halos, blurry line art, double lines, "
    "broken lines, warped anatomy, malformed hands, distorted facial features, AI artifacts, random "
    "texture, muddy colors, color bleeding, color drift, unwanted color cast, painterly rendering, "
    "watercolor texture, airbrush shading, excessive gradients, soft 3D shading, photorealism, "
    "plastic skin, CGI look, over-detailing, oversmoothing, vector-art look, thick outlines."
)


class UpscaleError(RuntimeError):
    """A terminal upscale failure for one image (bad request / auth / exhausted)."""

    def __init__(self, message: str, *, attempts: int = 0):
        super().__init__(message)
        self.attempts = attempts


class _SafetyEmpty(RuntimeError):
    """200 OK but no image — likely a (often transient) safety block."""


def base_url() -> str:
    return (os.getenv("ATRIUM_BASE_URL", "").strip() or _DEFAULT_BASE).rstrip("/")


def client_creds() -> Optional[tuple[str, str]]:
    cid = os.getenv("ATRIUM_CLIENT_ID", "").strip()
    secret = os.getenv("ATRIUM_CLIENT_SECRET", "").strip()
    return (cid, secret) if cid and secret else None


def is_configured() -> bool:
    return client_creds() is not None


def _retry_delay(attempt: int) -> float:
    base = min(_BACKOFF_CAP_S, _BACKOFF_S * (2 ** (attempt - 1)))
    return base * (0.5 + random.random() * 0.5)


def _mime_for(url: str) -> str:
    low = url.lower().split("?", 1)[0]
    if low.endswith("/thumb") or low.endswith(".jpg") or low.endswith(".jpeg"):
        return "image/jpeg"
    if low.endswith(".webp"):
        return "image/webp"
    return "image/png"


def _build_body(source_url: str) -> dict:
    return {
        "model": UPSCALE_MODEL,
        "contents": [
            {"fileData": {"mimeType": _mime_for(source_url), "fileUri": source_url}},
            {"text": UPSCALE_PROMPT},
        ],
        "config": {
            "responseModalities": ["IMAGE"],
            "imageConfig": {"imageSize": "4K"},
        },
    }


def _extract_download_url(payload: dict) -> Optional[str]:
    for cand in payload.get("candidates") or []:
        for part in (cand.get("content") or {}).get("parts") or []:
            du = part.get("downloadUrl") or part.get("download_url")
            if isinstance(du, str) and du:
                return du
    return None


def _download_watchdog(url: str) -> bytes:
    """Stream the file on ONE FRESH connection with an average-rate watchdog.
    S3 throttles per TCP 4-tuple, so a slow connection is abandoned (re-drawn by
    the caller) rather than waited out."""
    timeout = httpx.Timeout(connect=10.0, read=_DOWNLOAD_READ_TIMEOUT_S, write=10.0, pool=10.0)
    with httpx.Client(timeout=timeout) as c:
        with c.stream("GET", url) as r:
            if r.status_code != 200:
                raise RuntimeError(f"atrium downloadUrl fetch http_{r.status_code}")
            buf = bytearray()
            start = time.monotonic()
            for chunk in r.iter_bytes(1 << 16):
                buf.extend(chunk)
                elapsed = time.monotonic() - start
                if elapsed > _DOWNLOAD_ATTEMPT_CAP_S:
                    raise TimeoutError(f"download exceeded {_DOWNLOAD_ATTEMPT_CAP_S:.0f}s ({len(buf)} bytes)")
                if elapsed > _DOWNLOAD_GRACE_S:
                    rate = len(buf) / elapsed
                    if rate < _DOWNLOAD_MIN_RATE:
                        raise TimeoutError(
                            f"throttled connection: {rate / 1024:.0f} KB/s after "
                            f"{elapsed:.0f}s ({len(buf)} bytes) — redrawing"
                        )
    if not buf:
        raise RuntimeError("atrium downloadUrl fetch: empty body")
    return bytes(buf)


def _fetch_download(url: str) -> bytes:
    deadline = time.monotonic() + _DOWNLOAD_BUDGET_S
    last_exc: Optional[BaseException] = None
    dl_try = 0
    while time.monotonic() < deadline:
        dl_try += 1
        try:
            return _download_watchdog(url)
        except (httpx.HTTPError, TimeoutError, RuntimeError) as exc:
            last_exc = exc
            logger.warning("atrium upscale downloadUrl slow/failed (draw %d): %r", dl_try, exc)
    raise UpscaleError(
        f"atrium: 4K image download did not complete within {_DOWNLOAD_BUDGET_S:.0f}s "
        f"after {dl_try} fresh connections ({last_exc!r})",
        attempts=dl_try,
    )


def _generate_once(client: httpx.Client, headers: dict, body: dict) -> bytes:
    resp = client.post(f"{base_url()}/api/partner/image/generate", headers=headers, json=body)
    if resp.status_code != 200:
        try:
            j = resp.json()
            err = j.get("error", {}) if isinstance(j, dict) else {}
            msg = err.get("message") or (j.get("message") if isinstance(j, dict) else "") or ""
            detail = f"{err.get('status', resp.status_code)}: {str(msg)[:300]}"
        except Exception:  # noqa: BLE001
            detail = f"http_{resp.status_code}"
        detail_l = detail.lower()
        if resp.status_code in _FATAL_STATUSES and not any(s in detail_l for s in _RETRYABLE_4XX_SUBSTR):
            raise UpscaleError(f"atrium: {detail}", attempts=1)
        raise RuntimeError(f"atrium retryable: {detail}")
    url = _extract_download_url(resp.json())
    if not url:
        raise _SafetyEmpty("atrium: no image in response (safety block?)")
    return _fetch_download(url)


def _upscale_sync(source_url: str) -> bytes:
    creds = client_creds()
    if not creds:
        raise UpscaleError("atrium: ATRIUM_CLIENT_ID/ATRIUM_CLIENT_SECRET not set in .env", attempts=0)
    headers = {"x-client-id": creds[0], "x-client-secret": creds[1]}
    body = _build_body(source_url)
    last = "unknown"
    safety_tries = 0
    # Blocking client in a thread → OS-level timeouts always fire (the Windows
    # asyncio proactor loop can lose socket events under load and hang forever).
    with httpx.Client(timeout=_TIMEOUT_S) as client:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return _generate_once(client, headers, body)
            except UpscaleError:
                raise
            except _SafetyEmpty as exc:
                safety_tries += 1
                last = str(exc)[:200]
                logger.warning("atrium upscale safety-empty %d/%d", safety_tries, SAFETY_MAX_ATTEMPTS)
                if safety_tries >= SAFETY_MAX_ATTEMPTS:
                    raise UpscaleError(f"{last} — blocked after {safety_tries} tries", attempts=safety_tries)
                time.sleep(_BACKOFF_S * attempt)
            except Exception as exc:  # noqa: BLE001 — 5xx / 429 / timeouts / transport
                last = f"{type(exc).__name__}: {exc}"[:200].rstrip(": ")
                logger.warning("atrium upscale attempt %d/%d: %s", attempt, MAX_ATTEMPTS, last)
                if attempt < MAX_ATTEMPTS:
                    time.sleep(_retry_delay(attempt))
    raise UpscaleError(f"atrium: {last}", attempts=MAX_ATTEMPTS)


async def upscale_to_4k(source_url: str) -> bytes:
    """Upscale/enhance the image at ``source_url`` (a public http(s) URL Atrium
    can fetch) to a 4K PNG via Nano Banana Pro. Returns the PNG bytes. Raises
    ``UpscaleError`` on a terminal failure. Concurrency-gated to _MAX_CONCURRENT
    (default 6) so a big batch doesn't trip the Gemini backend's rate limit;
    excess calls queue on the gate."""
    async with _gate:
        return await asyncio.to_thread(_upscale_sync, source_url)


def match_reference_colors(result_png: bytes, source_bytes: bytes, strength: float = COLOR_MATCH_STRENGTH) -> bytes:
    """Neutralise the upscale model's colour cast: shift the 4K result's mean a*/b*
    (LAB chroma) onto the ORIGINAL source's, leaving L (luminance) untouched — so the
    upscaled detail/contrast survive and only the red/pink tint is corrected. Same
    technique as GiantFlow's "keep original colors". Best-effort: returns the input
    unchanged on any error (colour matching must never fail an upscale). Sync +
    CPU-heavy — call via ``asyncio.to_thread``."""
    try:
        import cv2
        import numpy as np

        out = cv2.imdecode(np.frombuffer(result_png, np.uint8), cv2.IMREAD_COLOR)
        ref = cv2.imdecode(np.frombuffer(source_bytes, np.uint8), cv2.IMREAD_COLOR)
        if out is None or ref is None:
            return result_png
        lab = cv2.cvtColor(out, cv2.COLOR_BGR2LAB).astype(np.float32)
        # Means off 256² copies — identical to the full-res mean for our purpose and
        # keeps a 4K pair cheap.
        small = cv2.resize(out, (256, 256), interpolation=cv2.INTER_AREA)
        olab = cv2.cvtColor(small, cv2.COLOR_BGR2LAB).astype(np.float32)
        rlab = cv2.cvtColor(
            cv2.resize(ref, (256, 256), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2LAB
        ).astype(np.float32)
        for ch in (1, 2):  # a*, b* only — never touch L
            delta = (rlab[:, :, ch].mean() - olab[:, :, ch].mean()) * strength
            lab[:, :, ch] = np.clip(lab[:, :, ch] + delta, 0, 255)
        fixed = cv2.cvtColor(lab.astype(np.uint8), cv2.COLOR_LAB2BGR)
        # Upscale output is PNG (lossless) → keep PNG; a JPEG in → JPEG q95.
        if result_png[:3] == b"\xff\xd8\xff":
            ok, buf = cv2.imencode(".jpg", fixed, [cv2.IMWRITE_JPEG_QUALITY, 95])
        else:
            ok, buf = cv2.imencode(".png", fixed)
        return buf.tobytes() if ok else result_png
    except Exception:  # noqa: BLE001
        logger.warning("keep-original-color match failed (keeping raw 4K)")
        return result_png
