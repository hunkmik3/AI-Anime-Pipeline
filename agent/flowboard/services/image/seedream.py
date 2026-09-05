"""Seedream image engine for the manga colorizer (self-contained).

Reaches BytePlus **Seedream** via one of two async gateways, chosen by
``COLORIZE_PROVIDER``:

  * ``dancesee`` (default) — DanceSee B2B, content-filter-DISABLED
    (``/api/v1/b2b/image/generations/async`` at api.dancesee.io), so manga pages
    with sensitive content aren't rejected. Needs a B2B-whitelisted key.
  * ``avis`` — the regular moderated Avis gateway
    (``/api/v1/image/generations/async`` at api.avis.xyz). Fine for SFW pages.

Both speak the SAME async submit/poll shape + auth (Bearer + X-Api-Key).
Reference/source images are sent INLINE as base64 — no R2/tunnel needed, so the
engine runs fully locally. Key: ``DANCESEE_API_KEY`` (falls back to
``AVIS_API_KEY``). A process-wide semaphore caps concurrent gens
(``COLORIZE_CONCURRENCY``, default 6) so a big chapter doesn't trip the backend's
rate limit. Public entry: ``generate_image_variants``.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import math
import os
import time
from typing import Callable, Optional, Sequence

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT_S = 120.0
MAX_ATTEMPTS = max(1, int(os.getenv("COLORIZE_MAX_ATTEMPTS", "5") or "5"))
SAFETY_MAX_ATTEMPTS = 3
_BACKOFF_S = 2.0
_FATAL_STATUSES = {400, 401, 403, 404}
_DEFAULT_MODEL = "dola-seedream-5-0-pro"

_POLL_INTERVAL_S = 3.0
_POLL_TIMEOUT_S = 360.0

# Process-wide concurrency gate: total concurrent Seedream gens across all pages
# and variants. Excess calls queue. Mirrors the upscale gate; tune via env.
_MAX_CONCURRENT = max(1, int(os.getenv("COLORIZE_CONCURRENCY", "6") or "6"))
_gate = asyncio.Semaphore(_MAX_CONCURRENT)

# Seedream valid-area band (same model whether via Avis or DanceSee/Ark).
_RATIOS = {
    "1:1": (1, 1), "16:9": (16, 9), "9:16": (9, 16), "4:3": (4, 3),
    "3:4": (3, 4), "3:2": (3, 2), "2:3": (2, 3), "21:9": (21, 9), "9:21": (9, 21),
}
_EDGE = {"1K": 1024, "2K": 2048, "4K": 4096}
_MIN_PIXELS = 3_686_400
_MAX_PIXELS = 4_624_220
_MAX_EDGE = 4096

# B2B (content-filter-disabled) supported model slugs + id aliases.
IMAGE_MODELS = frozenset({
    "seedream-4-0", "seedream-4-5", "seedream-5-0", "dola-seedream-5-0-pro",
})
_MODEL_ALIASES = {
    "dola-seedream-5-0-pro-260628": "dola-seedream-5-0-pro",
    "seedream-4.0": "seedream-4-0",
    "seedream-4.5": "seedream-4-5",
    "seedream-5.0": "seedream-5-0",
    "seedream-5": "seedream-5-0",
}


class SeedreamError(RuntimeError):
    """A terminal engine failure (fatal HTTP, moderation block, or exhausted
    retries). Carries the attempt count for logging."""

    def __init__(self, message: str, *, attempts: int = 0):
        super().__init__(message)
        self.attempts = attempts


class _Empty(RuntimeError):
    """Job succeeded but returned no image — retryable."""


def provider() -> str:
    return os.getenv("COLORIZE_PROVIDER", "").strip().lower() or "dancesee"


def _is_b2b() -> bool:
    return provider() in ("dancesee", "b2b")


def base_url() -> str:
    if _is_b2b():
        return (os.getenv("DANCESEE_BASE_URL", "").strip() or "https://api.dancesee.io").rstrip("/")
    return (os.getenv("AVIS_BASE_URL", "").strip() or "https://api.avis.xyz").rstrip("/")


def _submit_path() -> str:
    return "/api/v1/b2b/image/generations/async" if _is_b2b() else "/api/v1/image/generations/async"


def api_key() -> Optional[str]:
    k = os.getenv("DANCESEE_API_KEY", "").strip() or os.getenv("AVIS_API_KEY", "").strip()
    return k or None


def is_configured() -> bool:
    return api_key() is not None


def default_model() -> str:
    return os.getenv("COLORIZE_MODEL", "").strip() or os.getenv("SEEDREAM_MODEL", "").strip() or _DEFAULT_MODEL


def _headers() -> dict:
    k = api_key() or ""
    return {"Authorization": f"Bearer {k}", "X-Api-Key": k}


def resolve_model(image_model: str) -> str:
    """Map a caller model id onto a supported slug. On the B2B (dancesee) gateway
    only the content-filter-disabled slugs work; on Avis anything is passed
    through."""
    raw = image_model.strip() if isinstance(image_model, str) and image_model.strip() else default_model()
    model = _MODEL_ALIASES.get(raw, raw)
    if _is_b2b() and model not in IMAGE_MODELS:
        raise SeedreamError(
            f"seedream: model {raw!r} does not support contentFilterDisabled "
            f"(supported: {', '.join(sorted(IMAGE_MODELS))})",
            attempts=0,
        )
    return model


# ── sizing (Seedream area band) ───────────────────────────────────────────────

def _round16(x: float) -> int:
    return max(256, int(round(x / 16.0)) * 16)


def _clamp_band(W: float, H: float) -> str:
    m = max(W, H)
    if m > _MAX_EDGE:
        W, H = W * _MAX_EDGE / m, H * _MAX_EDGE / m
    area = W * H
    lo, hi = _MIN_PIXELS * 1.02, _MAX_PIXELS * 0.98
    if area < lo:
        k = math.sqrt(lo / area); W, H = W * k, H * k
    elif area > hi:
        k = math.sqrt(hi / area); W, H = W * k, H * k
    return f"{_round16(W)}x{_round16(H)}"


def size_token(aspect_ratio: Optional[str], image_size: Optional[str]) -> Optional[str]:
    edge = _EDGE.get((image_size or "").upper())
    if edge is None:
        return None
    w, h = _RATIOS.get(aspect_ratio or "1:1", (1, 1))
    if w >= h:
        W, H = float(edge), edge * h / w
    else:
        W, H = edge * w / h, float(edge)
    return _clamp_band(W, H)


def size_for_dims(w: int, h: int) -> Optional[str]:
    """``<W>x<H>`` from arbitrary pixel dims, clamped to Seedream's band."""
    if not w or not h or w < 1 or h < 1:
        return None
    return _clamp_band(float(w), float(h))


# ── payload / response helpers ────────────────────────────────────────────────

def _mime_of(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def _build_body(prompt: str, images: Optional[Sequence[bytes]], model: str, size: Optional[str]) -> dict:
    content: list[dict] = []
    for data in images or []:
        content.append({
            "type": "imageBase64",
            "data": base64.b64encode(data).decode("ascii"),
            "mediaType": _mime_of(data),
        })
    content.append({"type": "text", "text": prompt})
    body: dict = {
        "model": model, "content": content, "numberOfImages": 1,
        "responseFormat": "b64_json", "outputFormat": "png",
        # Seedream stamps an "AI generated" watermark unless disabled; send both
        # spellings so whichever the wrapper forwards takes effect.
        "watermark": False, "addWatermark": False,
    }
    if size:
        body["size"] = size
    return body


def _error_detail(resp: httpx.Response) -> str:
    try:
        j = resp.json()
        if isinstance(j, dict):
            errs = j.get("errors")
            if isinstance(errs, list) and errs:
                return f"{resp.status_code}: {str(errs[0])[:300]}"
            msg = j.get("message") or ((j.get("error") or {}).get("message") if isinstance(j.get("error"), dict) else "") or ""
            if msg:
                return f"{resp.status_code}: {str(msg)[:300]}"
    except Exception:  # noqa: BLE001
        pass
    return f"http_{resp.status_code}"


def _is_moderation_block(err: str) -> bool:
    low = (err or "").lower()
    return "sensitive information" in low or "contentfilter" in low.replace(" ", "")


def _gid(payload: dict) -> Optional[str]:
    root = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
    g = (root.get("generationId") or root.get("id")) if isinstance(root, dict) else None
    return g if isinstance(g, str) and g else None


def _extract_image(payload: dict) -> tuple[Optional[bytes], Optional[str]]:
    root = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
    if isinstance(root, dict) and isinstance(root.get("result"), dict):
        root = root["result"]
    imgs = root.get("images") if isinstance(root, dict) else None
    if not isinstance(imgs, list) or not imgs:
        return None, None
    first = imgs[0] if isinstance(imgs[0], dict) else {}
    b64 = first.get("b64") or first.get("b64_json")
    if isinstance(b64, str) and b64:
        try:
            return base64.b64decode(b64), None
        except Exception:  # noqa: BLE001
            pass
    url = first.get("url") or first.get("downloadUrl") or first.get("download_url")
    if isinstance(url, str) and url:
        return None, url
    return None, None


async def _generate_once(client: httpx.AsyncClient, body: dict) -> bytes:
    """Submit one async gen + poll to completion. Gated so at most
    ``_MAX_CONCURRENT`` gens run at once across the process."""
    async with _gate:
        submit = f"{base_url()}{_submit_path()}"
        resp = await client.post(submit, headers=_headers(), json=body)
        if resp.status_code not in (200, 202):
            detail = _error_detail(resp)
            if resp.status_code in _FATAL_STATUSES:
                raise SeedreamError(f"seedream: {detail}", attempts=1)
            raise RuntimeError(f"seedream retryable: {detail}")
        gid = _gid(resp.json())
        if not gid:
            raise RuntimeError("seedream: async submit returned no generationId")

        poll_url = f"{submit}/{gid}"
        deadline = time.monotonic() + _POLL_TIMEOUT_S
        while time.monotonic() < deadline:
            await asyncio.sleep(_POLL_INTERVAL_S)
            p = await client.get(poll_url, headers=_headers())
            if p.status_code != 200:
                continue
            payload = p.json()
            root = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
            status = (root.get("status") if isinstance(root, dict) else "") or ""
            if status == "succeeded":
                inline, url = _extract_image(payload)
                if inline:
                    return inline
                if url:
                    img = await client.get(url)
                    if img.status_code != 200 or not img.content:
                        raise RuntimeError(f"seedream image fetch http_{img.status_code}")
                    return img.content
                raise _Empty("seedream: job succeeded but no image")
            if status in ("failed", "expired"):
                err = str(root.get("error") if isinstance(root, dict) else "")[:200]
                if _is_moderation_block(err):
                    raise SeedreamError(f"seedream: {err}", attempts=1)
                raise RuntimeError(f"seedream job {status}: {err}")
        raise RuntimeError("seedream: async poll timed out")


async def generate_image_variants(
    prompt: str,
    images: Optional[Sequence[bytes]] = None,
    *,
    image_model: str = "",
    aspect_ratio: str = "1:1",
    variant_count: int = 1,
    image_size: Optional[str] = None,
    size_override: Optional[str] = None,
    max_attempts: int = MAX_ATTEMPTS,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> list[bytes]:
    """Text→image (and image-conditioned) via Seedream. ``images`` are raw bytes
    sent inline as ``imageBase64`` content parts. Variants run IN PARALLEL; each
    retries with backoff. Returns ≥1 PNG or raises SeedreamError."""
    if not is_configured():
        raise SeedreamError("seedream: DANCESEE_API_KEY (or AVIS_API_KEY) not set in .env", attempts=0)
    model = resolve_model(image_model)
    size = size_override or size_token(aspect_ratio, image_size)
    body = _build_body(prompt, images, model, size)
    n = max(1, min(int(variant_count or 1), 4))
    completed = 0

    async def _one(client: httpx.AsyncClient) -> bytes:
        nonlocal completed
        last = "unknown"
        empty = 0
        for attempt in range(1, max_attempts + 1):
            try:
                out = await _generate_once(client, body)
                completed += 1
                if on_progress:
                    on_progress(completed, n)
                return out
            except SeedreamError:
                raise
            except _Empty as exc:
                empty += 1
                last = str(exc)[:200]
                if empty >= SAFETY_MAX_ATTEMPTS:
                    raise SeedreamError(f"{last} — empty after {empty} tries", attempts=empty)
                await asyncio.sleep(_BACKOFF_S * attempt)
            except Exception as exc:  # noqa: BLE001
                last = f"{type(exc).__name__}: {exc}"[:200].rstrip(": ")
                logger.warning("seedream attempt %d/%d: %s", attempt, max_attempts, last)
                if attempt < max_attempts:
                    await asyncio.sleep(_BACKOFF_S * attempt)
        raise SeedreamError(f"seedream: {last}", attempts=max_attempts)

    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        results = await asyncio.gather(*[_one(client) for _ in range(n)], return_exceptions=True)
    outs = [r for r in results if not isinstance(r, BaseException)]
    if outs:
        logger.info("seedream ok: %d/%d via %s (%s)", len(outs), n, model, provider())
        return outs
    first = next((r for r in results if isinstance(r, BaseException)), None)
    if isinstance(first, SeedreamError):
        raise first
    raise SeedreamError(f"seedream: {first}", attempts=max_attempts)
