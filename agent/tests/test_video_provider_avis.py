"""Tests for the AvisVideoProvider (Seedance 2.0 via api.avis.xyz).

Covers the camelCase content assembly (imageUrl/videoUrl + firstFrame/
referenceImage roles), the {data, success} envelope unwrap, eager download
on success, provider-reported vs fallback cost, and error classification.
All HTTP traffic mocked via ``httpx.MockTransport``.
"""
from __future__ import annotations

import json

import httpx
import pytest

from flowboard.services.llm import secrets
from flowboard.services.video import VideoError, get_video_provider
from flowboard.services.video import avis, registry as _r


# ── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _api_key(monkeypatch, tmp_path):
    monkeypatch.delenv("AVIS_API_KEY", raising=False)
    monkeypatch.setenv("FLOWBOARD_SECRETS_PATH", str(tmp_path / "secrets.json"))
    secrets.set_api_key("avis", "avis-test-key")
    yield


@pytest.fixture(autouse=True)
def _registry():
    _r.register_defaults()
    yield


@pytest.fixture(autouse=True)
def _reset_http_factory():
    yield
    avis.reset_http_client_factory()


def _factory(handler):
    transport = httpx.MockTransport(handler)
    return lambda: httpx.AsyncClient(transport=transport, timeout=5.0)


def _provider():
    # seedance-2-0 routes through Avis (see registry).
    return get_video_provider("seedance-2-0")


# ── submit ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_submit_i2v_body_shape_and_taskid():
    seen: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return httpx.Response(
            200, json={"data": {"taskId": "cgt-i2v", "status": "queued"}, "success": True}
        )

    avis.set_http_client_factory(_factory(handler))
    res = await _provider().submit({
        "first_frame_url": "https://e/frame.png",
        "motion_prompt": "slow push in",
        "duration_seconds": 5,
        "aspect_ratio": "16:9",
        "resolution": "720p",
        "generate_audio": False,
    })

    assert res["external_job_id"] == "cgt-i2v"
    body = seen[0]
    assert body["model"] == "dreamina-seedance-2-0"
    assert body["duration"] == 5
    assert body["resolution"] == "720p"
    assert body["ratio"] == "16:9"
    assert body["generateAudio"] is False
    assert body["content"][0] == {"type": "text", "text": "slow push in"}
    img = [b for b in body["content"] if b["type"] == "imageUrl"]
    assert img == [{"type": "imageUrl", "url": "https://e/frame.png", "role": "firstFrame"}]


@pytest.mark.asyncio
async def test_submit_r2v_emits_reference_and_video_blocks():
    seen: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"data": {"taskId": "cgt-r2v"}, "success": True})

    avis.set_http_client_factory(_factory(handler))
    res = await _provider().submit({
        "reference_images": ["https://e/a.png", "https://e/b.png"],
        "reference_videos": ["https://e/clip.mp4"],
        "motion_prompt": "they walk like the clip",
        "duration_seconds": 6,
        "aspect_ratio": "9:16",
        "resolution": "1080p",
    })

    assert res["external_job_id"] == "cgt-r2v"
    blocks = seen[0]["content"]
    refs = [b for b in blocks if b["type"] == "imageUrl"]
    assert all(b["role"] == "referenceImage" for b in refs)
    assert [b["url"] for b in refs] == ["https://e/a.png", "https://e/b.png"]
    vids = [b for b in blocks if b["type"] == "videoUrl"]
    assert vids == [{"type": "videoUrl", "url": "https://e/clip.mp4"}]
    # reference media ⇒ no firstFrame block
    assert not any(b.get("role") == "firstFrame" for b in blocks)


@pytest.mark.asyncio
async def test_local_media_id_sent_as_downscaled_jpeg_base64(monkeypatch, tmp_path):
    """A bare media_id (local cache, no R2) is downscaled + JPEG-recompressed and
    sent inline as imageBase64; a public URL stays an imageUrl. Downscaling keeps
    multi-ref payloads under Avis's request-size limit (no more HTTP 413)."""
    import base64 as _b
    import io

    from PIL import Image

    from flowboard.services.video import avis as avis_mod

    # A large PNG (3000x2000) — must come back shrunk to <=1280 and as JPEG.
    img = tmp_path / "ref.png"
    Image.new("RGB", (3000, 2000), (200, 50, 50)).save(img, "PNG")
    monkeypatch.setattr(
        avis_mod.media_service, "cached_path",
        lambda mid: str(img) if mid == "local-mid-1" else None,
    )

    seen: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"data": {"taskId": "cgt-b64"}, "success": True})

    avis.set_http_client_factory(_factory(handler))
    await _provider().submit({
        "reference_images": ["local-mid-1", "https://e/remote.png"],
        "motion_prompt": "x",
        "duration_seconds": 5,
        "aspect_ratio": "16:9",
        "resolution": "720p",
    })

    blocks = seen[0]["content"]
    b64 = next(b for b in blocks if b["type"] == "imageBase64")
    assert b64["role"] == "referenceImage" and b64["mediaType"] == "image/jpeg"
    decoded = _b.b64decode(b64["data"])
    assert decoded[:3] == b"\xff\xd8\xff"  # JPEG magic
    with Image.open(io.BytesIO(decoded)) as out:
        assert max(out.size) <= avis_mod._INLINE_MAX_DIM  # downscaled
    # public URL stays a URL, not base64
    assert any(b.get("type") == "imageUrl" and b.get("url") == "https://e/remote.png" for b in blocks)


@pytest.mark.asyncio
async def test_audio_ref_dropped_with_warning():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"taskId": "cgt-a"}, "success": True})

    avis.set_http_client_factory(_factory(handler))
    res = await _provider().submit({
        "reference_images": ["https://e/a.png", "https://e/b.png"],
        "audio_ref_url": "https://e/voice.mp3",
        "motion_prompt": "x",
        "duration_seconds": 5,
        "aspect_ratio": "16:9",
        "resolution": "720p",
    })
    assert any("audio" in w.lower() for w in res["warnings"])


@pytest.mark.asyncio
async def test_submit_rejects_out_of_range_duration():
    with pytest.raises(VideoError) as exc:
        await _provider().submit({
            "first_frame_url": "https://e/f.png",
            "motion_prompt": "x",
            "duration_seconds": 99,
            "aspect_ratio": "16:9",
            "resolution": "720p",
        })
    assert exc.value.code == "bad_input"


# ── poll ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_poll_running_passthrough():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"status": "running"}, "success": True})

    avis.set_http_client_factory(_factory(handler))
    res = await _provider().poll("cgt-x")
    assert res["status"] == "running"
    assert res["video_bytes"] is None


@pytest.mark.asyncio
async def test_poll_succeeded_downloads_and_costs():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/video/tasks/cgt-ok"):
            return httpx.Response(200, json={"data": {
                "status": "succeeded",
                "videoUrl": "https://r2.example/clip.mp4",
                "downloadUrl": "https://r2.example/clip.mp4",
                "usage": {"completionTokens": 1000, "totalTokens": 1000, "usdCost": 0.42},
                "model": "dreamina-seedance-2-0",
            }, "success": True})
        if req.url.host == "r2.example":
            return httpx.Response(200, content=b"MP4DATA" * 10)
        return httpx.Response(500, json={"error": "x"})

    avis.set_http_client_factory(_factory(handler))
    res = await _provider().poll("cgt-ok")
    assert res["status"] == "succeeded"
    assert res["video_bytes"] == b"MP4DATA" * 10
    assert res["cost_usd"] == 0.42
    assert res["cost_tokens"] == 1000


@pytest.mark.asyncio
async def test_poll_succeeded_without_usdcost_falls_back_to_pricing():
    """Live Avis omitted usdCost in the probed envelope — provider must not
    crash; cost falls back to the local pricing table (0.0 = not configured)."""
    def handler(req: httpx.Request) -> httpx.Response:
        if "/video/tasks/" in req.url.path:
            return httpx.Response(200, json={"data": {
                "status": "succeeded",
                "downloadUrl": "https://r2.example/c.mp4",
                "usage": {"completionTokens": 109586, "totalTokens": 109586},
            }, "success": True})
        return httpx.Response(200, content=b"BYTES")

    avis.set_http_client_factory(_factory(handler))
    res = await _provider().poll("cgt-nousd")
    assert res["status"] == "succeeded"
    assert res["cost_usd"] == 0.0
    assert res["cost_tokens"] == 109586


@pytest.mark.asyncio
async def test_poll_failed_classifies_error():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {
            "status": "failed", "error": "content safety filter blocked the prompt",
        }, "success": True})

    avis.set_http_client_factory(_factory(handler))
    res = await _provider().poll("cgt-bad")
    assert res["status"] == "failed"
    assert res["error"] == "content_filtered"


# ── error envelope + availability ────────────────────────────────────────


@pytest.mark.asyncio
async def test_submit_http_400_maps_to_bad_input():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={
            "errors": ["The parameter `content[1].image_url` is not valid: resource download failed"],
            "status": 400, "success": False,
        })

    avis.set_http_client_factory(_factory(handler))
    with pytest.raises(VideoError) as exc:
        await _provider().submit({
            "first_frame_url": "https://e/f.png",
            "motion_prompt": "x",
            "duration_seconds": 5,
            "aspect_ratio": "16:9",
            "resolution": "720p",
        })
    assert exc.value.code == "bad_input"
    assert "download failed" in str(exc.value)


@pytest.mark.asyncio
async def test_is_available_tracks_key(monkeypatch):
    assert await _provider().is_available() is True
    secrets.set_api_key("avis", None)
    assert await _provider().is_available() is False


# ── KYC (person-driven) ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_kyc_asset_create_poll_active(monkeypatch, tmp_path):
    """Hoist -> POST /kyc/user/assets -> poll until active -> cache + return id."""
    from flowboard.services.video import avis as avis_mod

    img = tmp_path / "p.png"
    img.write_bytes(b"x")
    monkeypatch.setattr(avis_mod.media_service, "cached_path", lambda mid: str(img))
    monkeypatch.setattr(avis_mod, "prepare_image_url", lambda *a, **k: "https://r2.example/p.png")
    monkeypatch.setattr(avis_mod, "_read_cached_kyc_asset", lambda *a, **k: None)
    written: dict = {}
    monkeypatch.setattr(
        avis_mod, "_write_cached_kyc_asset",
        lambda mid, t, aid: written.update({"mid": mid, "t": t, "aid": aid}),
    )
    monkeypatch.setattr(avis_mod, "KYC_POLL_INTERVAL_S", 0)

    seen: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path.endswith("/kyc/user/assets"):
            seen.append(json.loads(req.content))
            return httpx.Response(201, json={"data": {"assetId": "asset-x", "status": "processing"}, "success": True})
        if req.method == "GET" and req.url.path.endswith("/kyc/user/assets/asset-x"):
            return httpx.Response(200, json={"data": {"status": "active"}, "success": True})
        return httpx.Response(500, json={"error": "x"})

    avis.set_http_client_factory(_factory(handler))
    asset_id = await avis_mod.ensure_kyc_asset("mid-1", "Image", project_id="p")
    assert asset_id == "asset-x"
    assert seen[0] == {"url": "https://r2.example/p.png", "assetType": "Image", "name": "mid-1"}
    assert written == {"mid": "mid-1", "t": "Image", "aid": "asset-x"}


@pytest.mark.asyncio
async def test_ensure_kyc_asset_cache_hit(monkeypatch):
    """A cached active assetId short-circuits — no network, no R2 hoist."""
    from flowboard.services.video import avis as avis_mod

    monkeypatch.setattr(
        avis_mod, "_read_cached_kyc_asset",
        lambda mid, t: "asset-cached" if t == "Image" else None,
    )

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("must not hit the network on a cache hit")

    avis.set_http_client_factory(_factory(handler))
    assert await avis_mod.ensure_kyc_asset("mid-1", "Image") == "asset-cached"


@pytest.mark.asyncio
async def test_ensure_kyc_asset_requires_public_hosting(monkeypatch, tmp_path):
    """No R2 -> ObjectStorageError -> clear bad_input mentioning hosting."""
    from flowboard.services.storage import ObjectStorageError
    from flowboard.services.video import avis as avis_mod

    img = tmp_path / "p.png"
    img.write_bytes(b"x")
    monkeypatch.setattr(avis_mod.media_service, "cached_path", lambda mid: str(img))
    monkeypatch.setattr(avis_mod, "_read_cached_kyc_asset", lambda *a, **k: None)

    def boom(*a, **k):
        raise ObjectStorageError("R2 not configured")

    monkeypatch.setattr(avis_mod, "prepare_image_url", boom)
    with pytest.raises(VideoError) as exc:
        await avis_mod.ensure_kyc_asset("mid-1", "Image")
    assert exc.value.code == "bad_input"
    assert "R2" in str(exc.value)


@pytest.mark.asyncio
async def test_ensure_kyc_asset_failed_policy(monkeypatch, tmp_path):
    """A 'failed' poll with a policy errorCode maps to content_filtered."""
    from flowboard.services.video import avis as avis_mod

    img = tmp_path / "p.png"
    img.write_bytes(b"x")
    monkeypatch.setattr(avis_mod.media_service, "cached_path", lambda mid: str(img))
    monkeypatch.setattr(avis_mod, "prepare_image_url", lambda *a, **k: "https://r2.example/p.png")
    monkeypatch.setattr(avis_mod, "_read_cached_kyc_asset", lambda *a, **k: None)
    monkeypatch.setattr(avis_mod, "_write_cached_kyc_asset", lambda *a, **k: None)
    monkeypatch.setattr(avis_mod, "KYC_POLL_INTERVAL_S", 0)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(201, json={"data": {"assetId": "asset-y", "status": "processing"}, "success": True})
        return httpx.Response(200, json={"data": {
            "status": "failed",
            "errorCode": "InputImageSensitiveContentDetected.PolicyViolation",
            "errorMessage": "blocked",
        }, "success": True})

    avis.set_http_client_factory(_factory(handler))
    with pytest.raises(VideoError) as exc:
        await avis_mod.ensure_kyc_asset("mid-1", "Image")
    assert exc.value.code == "content_filtered"


@pytest.mark.asyncio
async def test_submit_kyc_emits_kyc_content_parts():
    """KYC assetIds in params -> kyc*AssetId content parts, no regular refs."""
    seen: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"data": {"taskId": "cgt-kyc"}, "success": True})

    avis.set_http_client_factory(_factory(handler))
    res = await _provider().submit({
        "motion_prompt": "the person walks",
        "kyc_image_asset_id": "asset-img",
        "kyc_audio_asset_id": "asset-aud",
        "duration_seconds": 5,
        "aspect_ratio": "16:9",
        "resolution": "720p",
        "generate_audio": False,
    })
    assert res["external_job_id"] == "cgt-kyc"
    content = seen[0]["content"]
    assert {"type": "kycImageAssetId", "assetId": "asset-img"} in content
    assert {"type": "kycAudioAssetId", "assetId": "asset-aud"} in content
    assert not any(b.get("type") in ("imageUrl", "imageBase64", "videoUrl") for b in content)
    assert seen[0]["generateAudio"] is False
