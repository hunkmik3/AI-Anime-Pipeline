"""Atrium partner-API image engine — a third backend alongside the Flow bridge
and the direct Gemini API.

Atrium (https://studio.atrium.art) is a thin passthrough to Google's image
models. Two things differ from the direct Gemini engine:

  1. **Input media is by URL.** Reference/source images are sent as
     ``fileData.fileUri`` (a public http(s) URL Atrium downloads server-side).
     Raw inline base64 is rejected with 400. So the worker must hand us PUBLIC
     URLs — see ``public_media_base`` / ``PUBLIC_MEDIA_BASE_URL`` (the tunnel).
  2. **Output is a downloadUrl.** Each generated image part carries a
     ``downloadUrl`` (valid 24h) instead of inline bytes; we fetch it to get
     the PNG bytes, then the worker caches them locally like any other media.

Auth: ``ATRIUM_CLIENT_ID`` / ``ATRIUM_CLIENT_SECRET`` headers (in .env).
Models: ``gemini-3-pro-image`` / ``gemini-2.5-flash-image`` / ``gemini-3.1-flash-image``.
Limit: 1000 images / 24h per client.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Callable, Optional, Sequence

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_BASE = "https://studio.atrium.art"
_TIMEOUT_S = 240.0
MAX_ATTEMPTS = 5
# An empty (no-image) response is usually a safety filter — often a FALSE
# POSITIVE on anime art that clears on retry. Retry a few times only (a genuine
# block won't pass and each try costs quota).
SAFETY_MAX_ATTEMPTS = 3
_BACKOFF_S = 2.0

# --- downloadUrl fetch tuning -------------------------------------------------
# Atrium delivers each image as a presigned S3 ``downloadUrl``. That S3 hop is
# intermittently throttled to ~30 KB/s (measured live: 3 of 4 concurrent 4K
# downloads stuck at 31-33 KB/s → ~10 min each, while a sibling ran at
# 3,700 KB/s). The throttle is PER-CONNECTION, so we stream the body with a
# speed watchdog and, below a floor after a short grace, abort and retry on a
# brand-new connection — which almost always escapes it. Download retries are
# quota-free (S3 GET, no gen). Turn off with FLOWBOARD_ATRIUM_DL_TRIES=1.
_DL_FLOOR_KBPS = max(1, int(os.getenv("FLOWBOARD_ATRIUM_DL_FLOOR_KBPS", "300")))
_DL_GRACE_S = float(os.getenv("FLOWBOARD_ATRIUM_DL_GRACE_S", "8"))
_DL_TRIES = max(1, int(os.getenv("FLOWBOARD_ATRIUM_DL_TRIES", "6")))


class _SlowDownload(RuntimeError):
    """A download whose sustained speed fell below the floor — abort and retry
    on a fresh connection to escape a per-connection S3 throttle."""


class _AtriumSafetyEmpty(RuntimeError):
    """200 OK but no image — likely a (often transient) safety block."""


# Fail fast only on errors retrying can't fix: bad request, auth, not-found.
# 429 is retried — Atrium confirmed it's an intermittent Nano Banana / Veo model
# error (NOT an Atrium rate limit; they have no per-second/concurrent limits,
# only a daily quota) that usually clears after one or more retries.
_FATAL_STATUSES = {400, 401, 403, 404}


def base_url() -> str:
    return (os.getenv("ATRIUM_BASE_URL", "").strip() or _DEFAULT_BASE).rstrip("/")


def client_creds() -> Optional[tuple[str, str]]:
    cid = os.getenv("ATRIUM_CLIENT_ID", "").strip()
    secret = os.getenv("ATRIUM_CLIENT_SECRET", "").strip()
    if cid and secret:
        return cid, secret
    return None


def is_configured() -> bool:
    return client_creds() is not None


def public_media_base() -> Optional[str]:
    """Public base URL the agent's ``/media/<id>`` route is reachable at (e.g. a
    tunnel). Required for reference/source images on Atrium — without it we can
    only do pure text→image. Returns None when unset."""
    v = os.getenv("PUBLIC_MEDIA_BASE_URL", "").strip()
    return v.rstrip("/") or None


def media_public_url(media_id: str) -> Optional[str]:
    base = public_media_base()
    return f"{base}/media/{media_id}" if base else None


def _mime_for(url: str) -> str:
    low = url.lower().split("?", 1)[0]
    if low.endswith(".jpg") or low.endswith(".jpeg"):
        return "image/jpeg"
    if low.endswith(".webp"):
        return "image/webp"
    return "image/png"


def _build_body(
    prompt: str,
    image_urls: Optional[Sequence[str]],
    image_model: str,
    aspect_ratio: Optional[str],
    image_size: Optional[str],
) -> dict:
    contents: list[dict] = []
    for url in image_urls or []:
        if url:
            contents.append({"fileData": {"mimeType": _mime_for(url), "fileUri": url}})
    contents.append({"text": prompt})
    body: dict = {
        "model": image_model,
        "contents": contents,
        "config": {"responseModalities": ["IMAGE"]},
    }
    image_config: dict = {}
    if aspect_ratio:
        image_config["aspectRatio"] = aspect_ratio
    if image_size:
        image_config["imageSize"] = image_size
    if image_config:
        body["config"]["imageConfig"] = image_config
    return body


def _extract_download_url(payload: dict) -> Optional[str]:
    for cand in payload.get("candidates") or []:
        for part in (cand.get("content") or {}).get("parts") or []:
            du = part.get("downloadUrl") or part.get("download_url")
            if isinstance(du, str) and du:
                return du
    return None


async def _download_image(url: str) -> bytes:
    """Fetch a presigned S3 ``downloadUrl`` → image bytes, dodging the
    per-connection throttling Atrium's S3 delivery applies.

    Streams the body while watching the sustained rate; if it stays below
    ``_DL_FLOOR_KBPS`` past a short grace window the connection is likely being
    throttled (~30 KB/s), so we retry on a BRAND-NEW connection — a fresh 4-tuple
    usually escapes it. The final attempt drops the floor so a genuinely
    slow-but-only link still completes. Retries are quota-free (S3 GET, no gen).

    **Retries RESUME rather than restart.** They used to begin again from byte 0,
    which turned a slow patch of network into a much longer one: a real run
    (request 3025, four 4K variants) hit 63 → 21 → 14 → 46 → 41 KB/s and threw away
    up to 18 MB of already-downloaded bytes on each of five aborted attempts,
    finishing only on the sixth — about 50 s of pure re-download for one image. S3
    presigned GETs honour ``Range`` (verified: 206 with a Content-Range), so each
    attempt now continues from what the last one got. A fresh connection can still
    escape a throttle; the difference is that a slow attempt is no longer wasted.
    """
    floor_bps = _DL_FLOOR_KBPS * 1024
    last = "no attempt"
    buf = bytearray()  # carried ACROSS attempts — this is the resume
    total: Optional[int] = None
    for attempt in range(1, _DL_TRIES + 1):
        watchdog = attempt < _DL_TRIES  # last try: take whatever speed we get
        start = time.monotonic()
        got_this_attempt = 0
        try:
            # Fresh client per attempt → new TCP connection, not a pooled (maybe
            # throttled) one.
            headers = {"Range": f"bytes={len(buf)}-"} if buf else {}
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=15.0, read=60.0, write=15.0, pool=15.0)
            ) as dclient:
                async with dclient.stream("GET", url, headers=headers) as resp:
                    if resp.status_code not in (200, 206):
                        last = f"http_{resp.status_code}"
                        continue
                    if buf and resp.status_code == 200:
                        # Range ignored — this body starts at 0, so keeping the old
                        # bytes would corrupt the file. Start clean.
                        buf.clear()
                    if total is None:
                        total = _content_total(resp)
                    async for chunk in resp.aiter_bytes(65536):
                        buf += chunk
                        got_this_attempt += len(chunk)
                        elapsed = time.monotonic() - start
                        # Rate is measured over THIS attempt's bytes only — the
                        # carried-over prefix was downloaded at some other speed and
                        # would flatter a stalled connection.
                        if (
                            watchdog
                            and elapsed > _DL_GRACE_S
                            and got_this_attempt / elapsed < floor_bps
                        ):
                            raise _SlowDownload(
                                f"{got_this_attempt / 1024 / elapsed:,.0f} KB/s < "
                                f"{_DL_FLOOR_KBPS} floor"
                            )
            # A short read is not success: without Content-Length we can't tell a
            # complete body from a truncated one, so only a known total proves it.
            if buf and (total is None or len(buf) >= total):
                if attempt > 1:
                    logger.info("atrium download ok on attempt %d/%d (%.1f MB)",
                                attempt, _DL_TRIES, len(buf) / 1e6)
                return bytes(buf)
            last = "empty body" if not buf else f"short body {len(buf)}/{total}"
        except (_SlowDownload, httpx.TransportError, httpx.TimeoutException) as exc:
            last = f"{type(exc).__name__}: {exc}"[:120]
            logger.warning(
                "atrium download attempt %d/%d slow/failed: %s (keeping %.1f MB)",
                attempt, _DL_TRIES, last, len(buf) / 1e6,
            )
    raise RuntimeError(f"atrium downloadUrl fetch failed after {_DL_TRIES} tries: {last}")


def _content_total(resp: "httpx.Response") -> Optional[int]:
    """Full object size from a 200 or a 206 response, or None if unknown.

    S3 omits ``Accept-Ranges`` on these presigned URLs and answers HEAD with a
    zero length, so the size has to come off the GET we are already making.
    """
    cr = resp.headers.get("content-range", "")
    if "/" in cr:
        try:
            return int(cr.rsplit("/", 1)[1])
        except ValueError:
            pass
    try:
        n = int(resp.headers.get("content-length", ""))
    except ValueError:
        return None
    # On a 206 content-length is the SLICE, not the object.
    return n if resp.status_code == 200 else None


async def _generate_once(client: httpx.AsyncClient, headers: dict, body: dict) -> bytes:
    """One /image/generate call → image bytes (fetched from the downloadUrl).
    Raises BridgeEditError on a fatal API response; RuntimeError on retryable."""
    from flowboard.services.flowstudio.errors import BridgeEditError

    resp = await client.post(
        f"{base_url()}/api/partner/image/generate", headers=headers, json=body
    )
    if resp.status_code != 200:
        try:
            j = resp.json()
            err = j.get("error", {}) if isinstance(j, dict) else {}
            # Model errors use error.message; Atrium WRAPPER errors (e.g. "Failed
            # to fetch media URL", rate limit) put it at the TOP level.
            msg = err.get("message") or (j.get("message") if isinstance(j, dict) else "") or ""
            detail = f"{err.get('status', resp.status_code)}: {str(msg)[:300]}"
        except Exception:  # noqa: BLE001
            detail = f"http_{resp.status_code}"
        if resp.status_code in _FATAL_STATUSES:
            raise BridgeEditError(f"atrium: {detail}", attempts=1)
        raise RuntimeError(f"atrium retryable: {detail}")

    url = _extract_download_url(resp.json())
    if not url:
        # Retryable: empty responses are frequently a transient false-positive
        # safety block on anime art (see SAFETY_MAX_ATTEMPTS).
        raise _AtriumSafetyEmpty("atrium: no image in response (safety block?)")
    # The downloadUrl is a presigned S3 link — fetch (no auth headers) via the
    # speed-watchdog downloader that dodges S3's per-connection throttling.
    return await _download_image(url)


async def generate_image_variants(
    prompt: str,
    image_urls: Optional[Sequence[str]] = None,
    *,
    image_model: str,
    aspect_ratio: str = "1:1",
    variant_count: int = 1,
    image_size: Optional[str] = None,
    max_attempts: int = MAX_ATTEMPTS,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> list[bytes]:
    """Text→image (and image-conditioned) generation via Atrium. ``image_urls``
    are PUBLIC urls for source/reference frames (empty for plain text→image).
    Variants run IN PARALLEL (Atrium has no per-second/concurrent limit — only a
    daily quota); each retries 5xx/timeouts/429 with backoff, fatal API errors
    fail that variant. Partial success wins. ``on_progress(done, total)`` fires
    as each variant completes. Returns ≥1 image or raises BridgeEditError."""
    from flowboard.services.flowstudio.errors import BridgeEditError

    creds = client_creds()
    if not creds:
        raise BridgeEditError("atrium: ATRIUM_CLIENT_ID/ATRIUM_CLIENT_SECRET not set in .env", attempts=0)
    headers = {"x-client-id": creds[0], "x-client-secret": creds[1]}

    body = _build_body(prompt, image_urls, image_model, aspect_ratio, image_size)
    n = max(1, min(int(variant_count or 1), 4))
    completed = 0

    async def _one(client: httpx.AsyncClient) -> bytes:
        nonlocal completed
        last = "unknown"
        safety_tries = 0
        for attempt in range(1, max_attempts + 1):
            try:
                out = await _generate_once(client, headers, body)
                completed += 1  # asyncio single-threaded → no lock needed
                if on_progress:
                    on_progress(completed, n)
                return out
            except BridgeEditError:
                raise  # fatal for this variant
            except _AtriumSafetyEmpty as exc:
                safety_tries += 1
                last = str(exc)[:200]
                logger.warning("atrium safety-empty %d/%d", safety_tries, SAFETY_MAX_ATTEMPTS)
                if safety_tries >= SAFETY_MAX_ATTEMPTS:
                    raise BridgeEditError(
                        f"{last} — blocked after {safety_tries} tries", attempts=safety_tries
                    )
                await asyncio.sleep(_BACKOFF_S * attempt)
            except Exception as exc:  # noqa: BLE001 — 5xx / 429 / timeouts / transport
                last = f"{type(exc).__name__}: {exc}"[:200].rstrip(": ")
                logger.warning("atrium attempt %d/%d: %s", attempt, max_attempts, last)
                if attempt < max_attempts:
                    await asyncio.sleep(_BACKOFF_S * attempt)
        raise BridgeEditError(f"atrium: {last}", attempts=max_attempts)

    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        results = await asyncio.gather(*[_one(client) for _ in range(n)], return_exceptions=True)

    outs = [r for r in results if not isinstance(r, BaseException)]
    if outs:
        if len(outs) < n:
            logger.warning("atrium: %d/%d variant(s) ok (partial)", len(outs), n)
        else:
            logger.info("atrium ok: %d/%d via %s", len(outs), n, image_model)
        return outs
    first = next((r for r in results if isinstance(r, BaseException)), None)
    if isinstance(first, BridgeEditError):
        raise first
    raise BridgeEditError(f"atrium: {first}", attempts=max_attempts)
