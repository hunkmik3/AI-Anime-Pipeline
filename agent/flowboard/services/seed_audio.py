"""Dola Seed Audio 1.0 — all-in-one audio-scene generator, via the Avis gateway.

One text prompt → voice + background music + ambience + SFX in a single pass.
Provider is **Avis** (same API key as video, ``AVIS_API_KEY``); model
``seed-audio-1.0-multilingual`` ("Dola Seed Audio 1.0"; Avis fronts BytePlus).

Avis contract (live-verified 2026-07-30, base ``https://api.avis.xyz/api/v1``):

- **Submit** — ``POST /audio/generation-with-reference`` (auth header
  ``x-api-key``) → ``{"data": {"generationId": ...}}``. Body::

    {"model": "seed-audio-1.0-multilingual",
     "textPrompt": "<scene script>",
     "format": "mp3"|"ogg_opus"|"pcm", "sampleRate": 24000,
     "speechRate": 0, "loudnessRate": 0, "pitchRate": 0,
     "references": [{"audioUrl"|"audioData"} | {"imageUrl"|"imageData"} ...]}   # ≤3

- **Poll** — ``GET /audio/generations/:generationId`` → ``{"data": {"status",
  "audioUrl", "durationSeconds", ...}}``; ``processing`` → ``succeeded`` /
  ``failed``. The clip is a presigned ``audioUrl`` we download + cache.

WAV: Avis's audio models expose only mp3 / ogg_opus / pcm (no native wav — the
DTO rejects it). To serve wav LOSSLESSLY we request **pcm** (raw s16le at the
requested sample rate) and wrap it in a WAV header with ffmpeg — no re-encode,
so it's bit-identical to the model output. If Avis later adds native wav, set
``FLOWBOARD_AVIS_AUDIO_NATIVE_WAV=1`` to request ``wav`` directly instead.

Limits: prompt ≤ 3000 chars, ≤ 3 reference audio clips (5–30 s each) OR 1 image.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import subprocess
import uuid
from pathlib import Path
from typing import Optional

import httpx

from flowboard.services import media as media_service
from flowboard.services.llm import secrets

logger = logging.getLogger(__name__)

BASE_URL = os.getenv("FLOWBOARD_AVIS_BASE_URL", "https://api.avis.xyz/api/v1")
MODEL_ID = "seed-audio-1.0-multilingual"
MODEL_NAME = "Dola Seed Audio 1.0"

MAX_PROMPT_CHARS = 3000
MAX_REFERENCES = 3

# Formats Avis returns natively for this model (GET /ai/models → capabilities).
_FORMATS = {"mp3", "ogg_opus", "pcm"}
# What the UI may pick. "wav" is produced locally (pcm → WAV wrap) unless Avis
# native wav is enabled below.
_OUTPUT_FORMATS = {"mp3", "ogg_opus", "pcm", "wav"}
_SAMPLE_RATES = {8000, 16000, 24000, 32000, 44100, 48000}
_MIME_BY_FORMAT = {"mp3": "audio/mpeg", "ogg_opus": "audio/ogg", "pcm": "audio/basic", "wav": "audio/wav"}
_EXT_BY_FORMAT = {"mp3": ".mp3", "ogg_opus": ".ogg", "pcm": ".pcm", "wav": ".wav"}

_RATE_MIN, _RATE_MAX = -50, 100
_PITCH_MIN, _PITCH_MAX = -12, 12

# Poll cadence for the async audio task. Env-tunable.
_POLL_INTERVAL_S = float(os.getenv("FLOWBOARD_SEED_AUDIO_POLL_S", "1.5"))
_POLL_MAX_CYCLES = int(os.getenv("FLOWBOARD_SEED_AUDIO_POLL_MAX", "160"))

# ffmpeg for the pcm→wav wrap (LocalSystem service can't see a per-user shim →
# FLOWBOARD_FFMPEG_BIN points at C:\ffmpeg\bin\ffmpeg.exe; falls back to PATH).
_FFMPEG_BIN = os.getenv("FLOWBOARD_FFMPEG_BIN") or "ffmpeg"

# A voice-reference clip is inlined as base64 audioData; a full-res stereo WAV
# (commonly 7–12 MB) makes the request body blow past Avis's size limit → HTTP
# 413 → the gen fails. Downsample to mono at this rate and trim to this many
# seconds before inlining (Avis caps a reference clip at 30 s anyway); mono
# 24 kHz keeps the speaker characteristics voice-cloning needs. Env-tunable.
_REF_SHRINK_SR = int(os.getenv("FLOWBOARD_SEED_AUDIO_REF_SR", "24000"))
_REF_SHRINK_MAX_SEC = int(os.getenv("FLOWBOARD_SEED_AUDIO_REF_MAX_SEC", "30"))
# An IMAGE reference is likewise inlined as base64 imageData; a multi-MB PNG
# (e.g. a 23 MB character sheet) 413s just the same. Downscale to this max
# dimension and recompress to JPEG before inlining.
_REF_IMAGE_MAX_DIM = int(os.getenv("FLOWBOARD_SEED_AUDIO_REF_IMG_DIM", "1024"))


def _native_wav() -> bool:
    """When Avis provisions native wav for the audio model, flip this on to ask
    for ``format=wav`` directly instead of wrapping pcm."""
    return (os.getenv("FLOWBOARD_AVIS_AUDIO_NATIVE_WAV") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


class SeedAudioError(RuntimeError):
    """Uniform error for the Seed Audio provider. ``code`` is a short vocab the
    route maps to an HTTP status; ``raw`` keeps the upstream envelope."""

    def __init__(self, code: str, message: str, *, raw: Optional[dict] = None) -> None:
        super().__init__(message)
        self.code = code
        self.raw = raw or {}


def _headers() -> dict[str, str]:
    key = secrets.get_api_key("avis")
    if not key:
        raise SeedAudioError(
            "auth",
            "Avis API key not configured — set AVIS_API_KEY in .env "
            "(or apiKeys.avis in ~/.flowboard/secrets.json)",
        )
    return {"Content-Type": "application/json", "accept": "application/json", "x-api-key": key}


def _clamp(value, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return 0


def _shrink_audio_ref(path: Path) -> bytes:
    """Downsample a reference clip to mono/``_REF_SHRINK_SR`` WAV, trimmed to
    ``_REF_SHRINK_MAX_SEC`` seconds, so the inlined base64 stays under Avis's
    request-size limit (a raw stereo WAV otherwise 413s). Blocking → call via
    ``asyncio.to_thread``. Falls back to the original bytes if ffmpeg fails."""
    d = media_service.MEDIA_CACHE_DIR
    d.mkdir(parents=True, exist_ok=True)
    dst = d / f"_seedref_{uuid.uuid4().hex}.wav"
    try:
        subprocess.run(
            [_FFMPEG_BIN, "-v", "error", "-y", "-t", str(_REF_SHRINK_MAX_SEC),
             "-i", str(path), "-ac", "1", "-ar", str(_REF_SHRINK_SR),
             "-c:a", "pcm_s16le", str(dst)],
            check=True, capture_output=True, timeout=120,
        )
        out = dst.read_bytes()
        if out:
            return out
    except Exception:  # noqa: BLE001 — fall back to the original bytes below
        logger.warning("seed-audio ref shrink failed; inlining original", exc_info=True)
    finally:
        try:
            dst.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        return path.read_bytes()
    except OSError as exc:
        raise SeedAudioError("bad_input", f"could not read reference audio ({exc})")


def _reference_block(ref: str) -> dict:
    """One audio ``references[]`` entry. Public URL → ``audioUrl``; a local
    Flowboard media_id is downsampled (mono/24 kHz, ≤30 s) then inlined as base64
    ``audioData`` — keeping the request body under Avis's size limit (else 413)."""
    if ref.startswith(("http://", "https://")):
        return {"audioUrl": ref}
    path = media_service.cached_path(ref)
    if path is None:
        raise SeedAudioError(
            "bad_input", f"reference audio {ref!r} has no local cache file — re-upload it"
        )
    return {"audioData": base64.b64encode(_shrink_audio_ref(Path(path))).decode("ascii")}


def _shrink_image_ref(path: Path) -> bytes:
    """Downscale + JPEG-recompress a reference image so its inlined base64 stays
    under Avis's request-size limit (a multi-MB PNG otherwise 413s). Blocking →
    call via ``asyncio.to_thread``. Falls back to the original bytes on failure."""
    try:
        import io

        from PIL import Image

        with Image.open(path) as im:
            im = im.convert("RGB")
            im.thumbnail((_REF_IMAGE_MAX_DIM, _REF_IMAGE_MAX_DIM))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=85)
        data = buf.getvalue()
        if data:
            return data
    except Exception:  # noqa: BLE001 — fall back to the original bytes below
        logger.warning("seed-audio image ref shrink failed; inlining original", exc_info=True)
    try:
        return path.read_bytes()
    except OSError as exc:
        raise SeedAudioError("bad_input", f"could not read reference image ({exc})")


def _image_reference_block(ref: str) -> dict:
    if ref.startswith(("http://", "https://")):
        return {"imageUrl": ref}
    path = media_service.cached_path(ref)
    if path is None:
        raise SeedAudioError("bad_input", f"reference image {ref!r} has no local cache file")
    return {"imageData": base64.b64encode(_shrink_image_ref(Path(path))).decode("ascii")}


def _extract_error(status: int, data: dict) -> SeedAudioError:
    """Map an Avis error envelope (``{errors:[...], success:false}`` or
    ``{message}``) to our vocab."""
    errs = data.get("errors") if isinstance(data, dict) else None
    if isinstance(errs, list) and errs:
        msg = "; ".join(str(e) for e in errs)[:300]
    else:
        msg = str((data or {}).get("message") or f"HTTP {status}")[:300]
    low = msg.lower()
    if status in (401, 403):
        vocab = "auth"
    elif status == 429 or "rate" in low or "quota" in low or "limit" in low:
        vocab = "quota"
    elif 400 <= status < 500:
        vocab = "bad_input"
    else:
        vocab = "internal"
    return SeedAudioError(vocab, msg, raw=data if isinstance(data, dict) else {})


def _pcm_to_wav(pcm_bytes: bytes, sample_rate: int, channels: int) -> bytes:
    """Wrap raw s16le PCM in a WAV container via ffmpeg — a header only, no
    re-encode, so it stays lossless. Blocking; call via ``asyncio.to_thread``."""
    d = media_service.MEDIA_CACHE_DIR
    d.mkdir(parents=True, exist_ok=True)
    stem = uuid.uuid4().hex
    src = d / f"_pcmsrc_{stem}.pcm"
    dst = d / f"_wavout_{stem}.wav"
    try:
        src.write_bytes(pcm_bytes)
        subprocess.run(
            [_FFMPEG_BIN, "-v", "error", "-y", "-f", "s16le", "-ar", str(sample_rate),
             "-ac", str(channels), "-i", str(src), "-c:a", "pcm_s16le", str(dst)],
            check=True, capture_output=True, timeout=120,
        )
        out = dst.read_bytes()
        if not out:
            raise SeedAudioError("internal", "wav wrap produced no output")
        return out
    except FileNotFoundError as exc:
        raise SeedAudioError("internal", f"ffmpeg not found for wav wrap ({exc})") from exc
    except subprocess.TimeoutExpired as exc:
        raise SeedAudioError("internal", "wav wrap timed out") from exc
    except subprocess.CalledProcessError as exc:
        tail = (exc.stderr or b"").decode("utf-8", "ignore").strip().splitlines()[-1:] or ["ffmpeg failed"]
        raise SeedAudioError("internal", f"wav wrap failed: {tail[0][:200]}") from exc
    finally:
        for p in (src, dst):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


async def generate(
    *,
    prompt: str,
    audio_format: str = "mp3",
    sample_rate: int = 24000,
    speech_rate: int = 0,
    loudness_rate: int = 0,
    pitch_rate: int = 0,
    references: Optional[list[str]] = None,
    image_ref: Optional[str] = None,
    node_id: Optional[int] = None,
    timeout: float = 180.0,
) -> dict:
    """Generate an audio scene via Avis and ingest it as a local audio media_id.

    Returns ``{media_id, mime, duration, size}``. Raises ``SeedAudioError`` on
    bad input / auth / provider failure.
    """
    prompt = (prompt or "").strip()
    if not prompt:
        raise SeedAudioError("bad_input", "prompt is required")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise SeedAudioError(
            "bad_input", f"prompt too long ({len(prompt)} > {MAX_PROMPT_CHARS} chars)"
        )

    out_fmt = audio_format if audio_format in _OUTPUT_FORMATS else "mp3"
    # wav: request native wav if Avis has it (env), else request lossless pcm and
    # wrap it locally. Everything else passes through.
    wav_via_pcm = out_fmt == "wav" and not _native_wav()
    req_fmt = "pcm" if wav_via_pcm else out_fmt
    if req_fmt not in _FORMATS and req_fmt != "wav":
        req_fmt = "mp3"

    try:
        sr = int(sample_rate)
    except (TypeError, ValueError):
        sr = 24000
    if sr not in _SAMPLE_RATES:
        sr = 24000
    speech = _clamp(speech_rate, _RATE_MIN, _RATE_MAX)
    loud = _clamp(loudness_rate, _RATE_MIN, _RATE_MAX)
    pitch = _clamp(pitch_rate, _PITCH_MIN, _PITCH_MAX)

    body: dict = {
        "model": MODEL_ID,
        "textPrompt": prompt,
        "format": req_fmt,
        "sampleRate": sr,
        "speechRate": speech,
        "loudnessRate": loud,
        "pitchRate": pitch,
    }
    # Audio references (voice clone, ≤3 → @audio1..3) and an image reference are
    # mutually exclusive; the image wins if both are somehow supplied.
    refs = [r for r in (references or []) if isinstance(r, str) and r][:MAX_REFERENCES]
    if image_ref:
        body["references"] = [await asyncio.to_thread(_image_reference_block, image_ref)]
    elif refs:
        # Each block now transcodes via ffmpeg (blocking) → run off the loop.
        body["references"] = list(
            await asyncio.gather(*[asyncio.to_thread(_reference_block, r) for r in refs])
        )

    # Inlined base64 references dominate the body; log its size so a 413 (Avis
    # request-size limit) is diagnosable at a glance.
    logger.info(
        "seed-audio(avis): submit refs=%d image=%s body=%dKB fmt=%s",
        len(body.get("references") or []),
        bool(image_ref),
        len(json.dumps(body)) // 1024,
        out_fmt,
    )
    headers = _headers()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            # 1) submit → generationId
            resp = await client.post(
                f"{BASE_URL}/audio/generation-with-reference", headers=headers, json=body
            )
            try:
                data = resp.json()
            except ValueError:
                raise SeedAudioError("internal", f"non-JSON response: {resp.text[:200]}")
            if resp.status_code >= 400 or not (isinstance(data, dict) and data.get("success", True)):
                raise _extract_error(resp.status_code, data if isinstance(data, dict) else {})
            gen_id = ((data.get("data") or {}) if isinstance(data, dict) else {}).get("generationId")
            if not gen_id:
                raise SeedAudioError("internal", f"no generationId in submit response: {str(data)[:200]}")

            # 2) poll until the task finishes
            result: Optional[dict] = None
            for _ in range(_POLL_MAX_CYCLES):
                await asyncio.sleep(_POLL_INTERVAL_S)
                pr = await client.get(f"{BASE_URL}/audio/generations/{gen_id}", headers=headers)
                pd = (pr.json().get("data") or {}) if pr.status_code < 400 else {}
                st = pd.get("status")
                if st in ("succeeded", "completed", "done"):
                    result = pd
                    break
                if st in ("failed", "error", "cancelled"):
                    raise SeedAudioError(
                        "internal",
                        str(pd.get("error") or pd.get("message") or "audio generation failed"),
                        raw=pd,
                    )
            if result is None:
                raise SeedAudioError("internal", "audio generation timed out")

            # 3) download the finished clip (presigned URL)
            audio_url = result.get("audioUrl") or result.get("providerUrl")
            if not audio_url:
                raise SeedAudioError("internal", f"no audioUrl in result: {str(result)[:200]}")
            dl = await client.get(audio_url)
            if dl.status_code >= 400 or not dl.content:
                raise SeedAudioError("internal", f"failed to download audio (HTTP {dl.status_code})")
            raw = dl.content
    except httpx.HTTPError as exc:
        raise SeedAudioError("internal", f"seed audio transport error: {exc}") from exc

    dur = result.get("durationSeconds")
    duration_val = float(dur) if isinstance(dur, (int, float)) else None

    # wav via pcm: Avis returns raw s16le at the requested rate. Detect channel
    # count from the byte count (2 bytes/sample) so mono/stereo both wrap right,
    # then add a WAV header with ffmpeg (lossless — no re-encode).
    if wav_via_pcm:
        channels = 2
        if duration_val and sr:
            est = round(len(raw) / (duration_val * sr * 2))
            channels = 2 if est >= 2 else 1
        raw = await asyncio.to_thread(_pcm_to_wav, raw, sr, channels)

    media_id = str(uuid.uuid4())
    ext = _EXT_BY_FORMAT.get(out_fmt, ".mp3")
    mime = _MIME_BY_FORMAT.get(out_fmt, "audio/mpeg")
    cache_path = media_service.MEDIA_CACHE_DIR / f"{media_id}{ext}"
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(raw)
    except OSError as exc:
        raise SeedAudioError("internal", f"failed to cache generated audio ({exc})") from exc

    from datetime import datetime, timezone

    from flowboard.db import get_session
    from flowboard.db.models import Asset, Request

    with get_session() as s:
        s.add(Asset(uuid_media_id=media_id, kind="audio", local_path=str(cache_path), mime=mime, node_id=node_id))
        # Record the generation so the node's ⏱ history can replay it later with
        # its prompt + settings + material (mirrors how video gens land in
        # `request`). Only persisted (numeric) nodes get a row.
        if node_id is not None:
            s.add(
                Request(
                    node_id=node_id,
                    type="gen_audio",
                    status="done",
                    params={
                        "prompt": prompt,
                        "format": out_fmt,
                        "sample_rate": sr,
                        "speech_rate": speech,
                        "loudness_rate": loud,
                        "pitch_rate": pitch,
                        "references": list(refs),
                        "image_ref": image_ref or None,
                    },
                    result={"media_ids": [media_id], "duration": duration_val},
                    finished_at=datetime.now(timezone.utc),
                )
            )
        s.commit()

    logger.info("seed-audio(avis): media_id=%s fmt=%s dur=%s size=%d", media_id, out_fmt, duration_val, len(raw))
    return {
        "media_id": media_id,
        "mime": mime,
        "duration": duration_val,
        "size": len(raw),
    }


def is_configured() -> bool:
    return bool(secrets.get_api_key("avis"))
