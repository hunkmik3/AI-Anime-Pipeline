"""Gemini text/vision through the app's existing Atrium credentials.

The explicit ``atrium:`` model prefix selects this provider. It is not an
automatic fallback from another provider's refusal.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os

import httpx

from flowboard.services import avis_text


def _publish_image(part):
    """Atrium requires URL media; preserve the exact source bytes on app R2."""
    from flowboard.config import STORAGE_DIR
    from flowboard.services.flowstudio import r2
    if not r2.is_configured():
        raise avis_text.AvisTextError("Atrium vision requires the app's R2 media configuration")
    mime = part["mimeType"]
    extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(mime)
    if not extension:
        raise avis_text.AvisTextError("Unsupported Atrium image media type")
    raw = base64.b64decode(part["data"], validate=True)
    digest = hashlib.sha256(raw).hexdigest()
    directory = STORAGE_DIR / "atrium_source_inputs"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ("source-" + digest + extension)
    if not path.exists():
        # Concurrent requests can refer to the same frame; replace atomically.
        import tempfile
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as temp:
            temp.write(raw)
        from pathlib import Path
        Path(temp.name).replace(path)
    from flowboard.services.flowstudio.transfer_gate import transfer_gate
    with transfer_gate:
        url = r2.upload_file(path)
    return {"mimeType": mime, "fileUri": url}


async def prepare_body(body):
    # Bound source-media transfer independently of the model call concurrency.
    slots = asyncio.Semaphore(8)
    async def publish(part):
        if "inlineData" in part:
            async with slots:
                result = await asyncio.to_thread(_publish_image, part["inlineData"])
            part.clear()
            part["fileData"] = result
    await asyncio.gather(*(publish(part) for content in body["contents"] for part in content["parts"]))
    return body


def _parts(content):
    if isinstance(content, str):
        return [{"text": content}]
    parts = []
    for item in content:
        if item.get("type") == "text":
            parts.append({"text": item["text"]})
        elif item.get("type") == "imageBase64":
            parts.append({"inlineData": {"mimeType": item.get("mediaType", "image/jpeg"),
                                         "data": item["data"]}})
        else:
            raise avis_text.AvisTextError("Unsupported Atrium input part")
    return parts


def build_body(model, messages, temperature=None, max_tokens=None):
    body = {"model": model.removeprefix("atrium:"), "contents": [],
            "config": {"responseMimeType": "application/json"}}
    system = []
    for message in messages:
        parts = _parts(message["content"])
        if message["role"] == "system":
            system.extend(parts)
        elif message["role"] in {"user", "assistant"}:
            body["contents"].append({"role": "model" if message["role"] == "assistant" else "user",
                                      "parts": parts})
        else:
            raise avis_text.AvisTextError("Unsupported Atrium message role")
    if system:
        body["config"]["systemInstruction"] = {"parts": system}
    if temperature is not None:
        body["config"]["temperature"] = temperature
    if body["model"].startswith("gemini-2.5-"):
        # Gemini counts thinking against maxOutputTokens. Reserve the caller's
        # JSON answer budget instead of truncating it after internal reasoning.
        budget = min(24576, max(128, int(os.getenv("FLOWBOARD_ATRIUM_THINKING_BUDGET", "4096"))))
        body["config"]["thinkingConfig"] = {"thinkingBudget": budget}
        if max_tokens is not None:
            body["config"]["maxOutputTokens"] = min(65536, max_tokens + budget)
    elif max_tokens is not None:
        body["config"]["maxOutputTokens"] = max_tokens
    return body


def parse_response(model, data):
    blocked = (data.get("promptFeedback") or {}).get("blockReason")
    if blocked:
        raise avis_text.AvisContentRefusal(f"Atrium declined this request ({blocked})")
    candidates = data.get("candidates") or []
    if not candidates:
        raise avis_text.AvisTextError("Atrium returned no candidates")
    candidate = candidates[0]
    reason = str(candidate.get("finishReason") or "")
    if reason in {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"}:
        raise avis_text.AvisContentRefusal(f"Atrium declined this request ({reason})")
    text = "".join(p.get("text", "") for p in (candidate.get("content") or {}).get("parts", [])
                   if not p.get("thought"))
    finish = "length" if reason == "MAX_TOKENS" else reason.lower()
    avis_text.check_content_response(text, finish)
    if not text.strip():
        raise avis_text.AvisTextError(f"Atrium returned empty text (finish={reason})")
    usage = data.get("usageMetadata") or {}
    return avis_text.Completion(text=text, model=model, finish_reason=finish,
        prompt_tokens=int(usage.get("promptTokenCount") or 0),
        completion_tokens=int(usage.get("candidatesTokenCount") or 0), raw_usage=usage)


async def complete(model, messages, *, temperature=None, max_tokens=None, attempts=2):
    cid, secret = os.getenv("ATRIUM_CLIENT_ID", "").strip(), os.getenv("ATRIUM_CLIENT_SECRET", "").strip()
    if not cid or not secret:
        raise avis_text.AvisTextError("Atrium credentials are not configured")
    base = (os.getenv("ATRIUM_BASE_URL", "").strip() or "https://studio.atrium.art").rstrip("/")
    body = await prepare_body(build_body(model, messages, temperature, max_tokens))
    async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=20)) as client:
        for attempt in range(max(1, attempts)):
            try:
                response = await asyncio.wait_for(client.post(base + "/api/partner/llm/generate",
                    headers={"x-client-id": cid, "x-client-secret": secret}, json=body), timeout=150)
            except (httpx.TransportError, asyncio.TimeoutError) as exc:
                if attempt + 1 >= attempts:
                    raise avis_text.AvisTextError(f"Atrium transport failed: {type(exc).__name__}") from exc
                await asyncio.sleep(2 * (attempt + 1))
                continue
            if response.status_code in {429, 500, 502, 503, 504} and attempt + 1 < attempts:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            if response.status_code >= 400:
                raise avis_text._refusal(model, f"HTTP {response.status_code}: {response.text[:300]}")
            try:
                data = response.json()
            except ValueError as exc:
                raise avis_text.AvisTextError("Atrium returned invalid response JSON") from exc
            if data.get("error"):
                raise avis_text._refusal(model, str(data["error"])[:300])
            return parse_response(model, data)
    raise avis_text.AvisTextError("Atrium request did not complete")
