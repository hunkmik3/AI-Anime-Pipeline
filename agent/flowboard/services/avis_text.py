"""Avis text completions — the one client for every LLM call routed through Avis.

    POST {base}/api/v1/text/completions        header: x-api-key
    {"model": "...", "messages": [{"role": "user", "content": [...]}], ...}

Facts about this endpoint that are easy to get wrong, all confirmed against the
live API:

* It ALWAYS streams. ``"stream": false`` is ignored; the body is Server-Sent
  Events, one ``data: {"delta": "..."}`` per chunk, and a final chunk carrying
  ``finishReason`` and ``usage``.
* ``usage`` reports tokens only. There is no cost field on text, so this client
  records tokens per model and never invents a dollar figure.
* Images are ``{"type": "imageBase64", "data": <b64>, "mediaType": "image/jpeg"}``.
  The OpenAI shape (``image_url``) and a data: URL in ``imageUrl`` are both
  rejected with "url must be a URL address".

Kept model-agnostic on purpose: 300+ text models sit behind the same endpoint,
and callers pick one per job.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

_BASE = (os.getenv("AVIS_BASE_URL", "").strip() or "https://api.avis.xyz").rstrip("/")
_TIMEOUT = httpx.Timeout(connect=20.0, read=240.0, write=60.0, pool=20.0)
# Avis answers a busy key with 429; a short backoff clears most of them. 520-524
# are Cloudflare's own "the origin did not answer properly" — the gateway behind
# it recovers the same way.
_RETRYABLE = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524}

# Models the gateway refuses a temperature for. Learned at runtime rather than
# hardcoded: on 2026-09-18 claude-sonnet-5 and gpt-5-4 began answering
# `HTTP 400 ['temperature is not supported by this model']` to a parameter they
# had accepted for months, which silently killed the sequence, entity and
# tier-2 vision passes. A rejected temperature is dropped and the call retried,
# and the model is remembered so the next call never spends the round trip.
# Avis's active gpt-6-luna catalog explicitly excludes temperature. Omit it on
# the first request too; other gateway models are still learned dynamically.
_NO_TEMPERATURE: set[str] = {"gpt-6-luna", "gpt-6-astra"}
# Confirmed by Avis's explicit parameter rejection on 2026-10-06. Gateway
# capabilities can change independently of the model alias; learn the same
# rejection for other models without treating it as a model/content refusal.
_NO_MAX_TOKENS: set[str] = {"gpt-6-luna", "gpt-6-astra"}


def _unsupported_max_tokens(detail: str) -> bool:
    return bool(re.search(r"\bmax_?tokens[\"']?\s+is not supported by this model\b", detail, re.I))


class AvisTextError(RuntimeError):
    pass


class AvisEmptyResponse(AvisTextError):
    """The provider ended the request without usable output, not a JSON typo."""
    pass


class AvisContentRefusal(AvisTextError):
    """A content refusal is terminal, not an availability/fallback signal."""


def check_content_response(text: str, finish_reason: str = "") -> None:
    head = text.strip().replace("’", "'")
    if finish_reason.lower() in {"content_filter", "safety", "blocked", "refusal"} or re.match(
        r"^(?:I'm sorry[,.:]?\s*|I am sorry[,.:]?\s*|Sorry[,.:]?\s*)?(?:but\s+)?"
        r"I (?:cannot|can't|am unable to) (?:assist|help|comply|fulfill|provide|generate)", head, re.I
    ):
        raise AvisContentRefusal("Provider declined this content request; automatic retries and fallback stopped.")


class AvisModelError(AvisTextError):
    """The gateway refused the MODEL, not the request: this key has no access to
    it, it has no price, it does not exist, or it cannot take what was sent.
    Asking it again changes nothing — ask another model."""


# What the gateway says when the model itself is the problem. On 2026-09-24
# claude-opus-5 answered every call with "Invalid or missing API key." while
# the same key served claude-opus-4-5 — so this is per model, not per key.
_MODEL_REFUSALS = ("api key", "pricing not configured", "not supported", "not found",
                   "unknown model", "does not exist", "not available", "permission", "not allowed")


@dataclass
class Completion:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    finish_reason: str = ""
    raw_usage: dict[str, Any] = field(default_factory=dict)


def api_key() -> Optional[str]:
    return os.getenv("AVIS_API_KEY", "").strip() or None


def image_part(path: Path, media_type: str = "image/jpeg") -> dict:
    return {
        "type": "imageBase64",
        "data": base64.b64encode(path.read_bytes()).decode("ascii"),
        "mediaType": media_type,
    }


def text_part(text: str) -> dict:
    return {"type": "text", "text": text}


def _parse_sse(body: str) -> tuple[str, dict, str]:
    """Join every delta; return the text, the final chunk's metadata, and the
    error the stream carried, if any.

    The gateway reports a refused request INSIDE a 200 stream — one
    ``data: {"error": "..."}`` and nothing else. Read as text alone that is an
    empty reply, which is how a day of "the model is overloaded" turned out to
    be "temperature is not supported" and "Invalid or missing API key".
    """
    parts: list[str] = []
    final: dict = {}
    error = ""
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if chunk.get("error"):
            error = str(chunk["error"])
        if chunk.get("delta"):
            parts.append(chunk["delta"])
        if chunk.get("finishReason") or chunk.get("usage"):
            final.update(chunk)  # finish reason and usage can arrive separately
    return "".join(parts), final, error


def _refusal(model: str, detail: str) -> AvisTextError:
    low = detail.lower()
    if any(s in low for s in ("content_filter", "content policy", "safety policy", "safety filter")):
        return AvisContentRefusal(f"{model}: provider declined this content request")
    if any(s in low for s in _MODEL_REFUSALS):
        return AvisModelError(f"{model}: {detail[:300]}")
    return AvisTextError(f"{model}: {detail[:300]}")


async def complete(
    model: str,
    messages: list[dict],
    *,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    attempts: int = 4,
) -> Completion:
    if model.startswith("atrium:"):
        from flowboard.services import atrium_text
        return await atrium_text.complete(model, messages, temperature=temperature,
                                           max_tokens=max_tokens, attempts=attempts)
    key = api_key()
    if not key:
        raise AvisTextError("AVIS_API_KEY is not set in .env")

    body: dict[str, Any] = {"model": model, "messages": messages}
    if temperature is not None and model not in _NO_TEMPERATURE:
        body["temperature"] = temperature
    if max_tokens is not None and model not in _NO_MAX_TOKENS:
        body["maxTokens"] = max_tokens

    last = ""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for attempt in range(1, attempts + 1):
            try:
                res = await client.post(
                    f"{_BASE}/api/v1/text/completions",
                    headers={"x-api-key": key, "Content-Type": "application/json"},
                    json=body,
                )
            except httpx.HTTPError as exc:
                last = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(1.5 * attempt)
                continue

            if res.status_code in _RETRYABLE and attempt < attempts:
                last = f"HTTP {res.status_code}"
                await asyncio.sleep(2.0 * attempt)
                continue
            if res.status_code >= 400:
                try:
                    detail = res.json().get("errors") or res.text
                except ValueError:
                    detail = res.text
                text = str(detail)
                refused = _refusal(model, f"HTTP {res.status_code} {text}")
                if isinstance(refused, AvisContentRefusal):
                    raise refused
                if res.status_code == 400 and "maxTokens" in body and _unsupported_max_tokens(text):
                    _NO_MAX_TOKENS.add(model)
                    body.pop("maxTokens")
                    last = text
                    logger.info("avis_text: %s refuses maxTokens; retrying without it", model)
                    continue
                if res.status_code == 400 and "temperature" in text.lower() and "temperature" in body:
                    _NO_TEMPERATURE.add(model)
                    body.pop("temperature", None)
                    logger.info("avis_text: %s refuses a temperature; retrying without it", model)
                    continue
                raise refused

            text, final, error = _parse_sse(res.text)
            check_content_response(text, str(final.get("finishReason") or ""))
            if error and not text:
                # The same refusals, arriving in-stream since 2026-09-24.
                refused = _refusal(model, error)
                if isinstance(refused, AvisContentRefusal):
                    raise refused
                if "maxTokens" in body and _unsupported_max_tokens(error):
                    _NO_MAX_TOKENS.add(model)
                    body.pop("maxTokens")
                    last = error
                    logger.info("avis_text: %s refuses maxTokens; retrying without it", model)
                    continue
                # A different/ambiguous maxTokens failure is not evidence that
                # removing a parameter makes replay safe.
                if re.search(r"\bmax_?tokens\b", error, re.I):
                    raise refused
                if "temperature" in error.lower() and "temperature" in body:
                    _NO_TEMPERATURE.add(model)
                    body.pop("temperature", None)
                    logger.info("avis_text: %s refuses a temperature; retrying without it", model)
                    continue
                if isinstance(refused, (AvisModelError, AvisContentRefusal)) or attempt >= attempts:
                    raise refused
                last = error
                await asyncio.sleep(2.0 * attempt)
                continue
            usage = final.get("usage") or {}
            if not text.strip():
                raise AvisEmptyResponse(f"{model}: provider returned empty text (finish={final.get('finishReason')!r})")
            return Completion(
                text=text,
                model=model,
                prompt_tokens=int(usage.get("promptTokens") or 0),
                completion_tokens=int(usage.get("completionTokens") or 0),
                finish_reason=str(final.get("finishReason") or ""),
                raw_usage=usage,
            )
    raise AvisTextError(f"{model}: gave up after {attempts} attempts ({last})")


def extract_json(text: str) -> Any:
    """Pull the JSON value out of a model reply that may be fenced or chatty."""
    import re

    s = text.strip()
    # A complete object must win over an array nested in one of its fields.
    # Slicing from the first '[' used to return {"coverage": [...]} as only
    # the coverage list, silently discarding its sibling prompt/end_state.
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", s, re.DOTALL)
    if fence:
        s = fence.group(1).strip()
    start = min((p for p in (s.find("{"), s.find("[")) if p >= 0), default=-1)
    if start >= 0:
        try:
            value, _ = json.JSONDecoder().raw_decode(s, start)
            return value
        except json.JSONDecodeError:
            # A truncated enclosing object is not repaired by accepting one
            # complete child array: the caller must retry the whole contract.
            pass
    raise AvisTextError("model reply contained no parseable top-level JSON")
