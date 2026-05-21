"""Tests for the DreaminaVideoProvider.

Cover the dual-mode capability gating, inline-flag prompt building,
eager download on success, cost calc, and failure-mode classification.
All HTTP traffic mocked via ``httpx.MockTransport``.
"""
from __future__ import annotations

import json
from typing import Optional

import httpx
import pytest

from flowboard.services.llm import secrets
from flowboard.services.video import (
    VideoError,
    get_video_model,
    get_video_provider,
)
from flowboard.services.video import dreamina, registry as _r


# ── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _api_key(monkeypatch, tmp_path):
    """Stub the secrets file at a tmp path so writes don't leak."""
    monkeypatch.setenv("FLOWBOARD_SECRETS_PATH", str(tmp_path / "secrets.json"))
    secrets.set_api_key("dreamina", "ark-test-key")
    yield


@pytest.fixture(autouse=True)
def _registry():
    """Ensure registry is loaded for each test (cheap, idempotent)."""
    _r.register_defaults()
    yield


@pytest.fixture(autouse=True)
def _reset_http_factory():
    yield
    dreamina.reset_http_client_factory()


def _factory_with_handler(handler):
    """Build an AsyncClient factory backed by a MockTransport."""
    transport = httpx.MockTransport(handler)

    def factory():
        return httpx.AsyncClient(transport=transport, timeout=5.0)

    return factory


# ── prompt builder ─────────────────────────────────────────────────────


def test_inline_flag_builder_appends_rt_rs():
    out = dreamina.build_dreamina_prompt(
        "girl turns and smiles", aspect_ratio="16:9", resolution="1080p"
    )
    assert out == "girl turns and smiles --rt 16:9 --rs 1080p"


def test_inline_flag_builder_strips_existing_flags():
    """User-typed flags get cleaned to avoid double application."""
    out = dreamina.build_dreamina_prompt(
        "stuff --rt 1:1 --rs 720p more text",
        aspect_ratio="9:16",
        resolution="1080p",
    )
    assert out == "stuff more text --rt 9:16 --rs 1080p"


# ── capability gate / drop-with-warning ────────────────────────────────


@pytest.mark.asyncio
async def test_capability_warning_when_i2v_model_receives_multi_ref():
    """Seedance 1.5 Pro must drop refs + return a warning, not fail."""
    seen_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "cgt-test-id"})

    dreamina.set_http_client_factory(_factory_with_handler(handler))
    provider = get_video_provider("seedance-1-5-pro")

    result = await provider.submit({
        "first_frame_url": "https://example.com/frame.png",
        "reference_images": [
            "https://example.com/r1.png",
            "https://example.com/r2.png",
        ],
        "motion_prompt": "subtle pull-back",
        "duration_seconds": 5,
        "aspect_ratio": "16:9",
        "resolution": "720p",
    })

    assert result["external_job_id"] == "cgt-test-id"
    assert len(result["warnings"]) == 1
    assert "i2v-only" in result["warnings"][0]
    # Submitted body should be a *single* image_url block with no role
    # (per contract §2.6: role omitted on single-image submits).
    assert len(seen_bodies) == 1
    body = seen_bodies[0]
    image_blocks = [b for b in body["content"] if b.get("type") == "image_url"]
    assert len(image_blocks) == 1
    assert "role" not in image_blocks[0]


@pytest.mark.asyncio
async def test_r2v_model_accepts_multi_ref():
    """Seedance 2.0 must pass refs through with role: reference_image."""
    seen_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "cgt-r2v-test"})

    dreamina.set_http_client_factory(_factory_with_handler(handler))
    provider = get_video_provider("seedance-2-0")

    result = await provider.submit({
        "first_frame_url": "https://example.com/frame.png",
        "reference_images": [
            "https://example.com/r1.png",
            "https://example.com/r2.png",
        ],
        "motion_prompt": "anime girl walks",
        "duration_seconds": 5,
        "aspect_ratio": "16:9",
        "resolution": "720p",
    })

    assert result["warnings"] == []
    body = seen_bodies[0]
    image_blocks = [b for b in body["content"] if b.get("type") == "image_url"]
    assert len(image_blocks) == 3  # first_frame + 2 refs
    roles = [b.get("role") for b in image_blocks]
    assert roles[0] == "first_frame"
    assert roles[1] == "reference_image"
    assert roles[2] == "reference_image"


@pytest.mark.asyncio
async def test_refs_truncated_to_max_refs():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "cgt-test"})

    dreamina.set_http_client_factory(_factory_with_handler(handler))
    provider = get_video_provider("seedance-2-0")

    # Seedance 2.0 capability is max_refs=4 (per registry.py); pass 10.
    result = await provider.submit({
        "first_frame_url": "https://example.com/f.png",
        "reference_images": [f"https://example.com/r{i}.png" for i in range(10)],
        "motion_prompt": "many anchors",
        "duration_seconds": 5,
        "aspect_ratio": "16:9",
        "resolution": "720p",
    })
    assert any("Truncated" in w for w in result["warnings"])


# ── eager download ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_eager_download_on_success():
    """Provider must GET the signed URL before returning succeeded."""
    download_called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tasks/cgt-eager"):
            return httpx.Response(200, json={
                "id": "cgt-eager",
                "model": "seedance-1-5-pro-251215",
                "status": "succeeded",
                "content": {"video_url": "https://signed.example/clip.mp4"},
                "usage": {"completion_tokens": 108900, "total_tokens": 108900},
                "duration": 5,
                "resolution": "720p",
                "ratio": "1:1",
                "framespersecond": 24,
                "seed": 42,
            })
        if request.url.host == "signed.example":
            download_called["n"] += 1
            return httpx.Response(200, content=b"FAKE_MP4_BYTES" * 100)
        return httpx.Response(500, json={"error": "unexpected"})

    dreamina.set_http_client_factory(_factory_with_handler(handler))
    provider = get_video_provider("seedance-1-5-pro")
    result = await provider.poll("cgt-eager")

    assert result["status"] == "succeeded"
    assert download_called["n"] == 1, "video bytes were not eagerly downloaded"
    assert result["video_bytes"] is not None
    assert result["video_bytes"].startswith(b"FAKE_MP4_BYTES")
    assert result["video_url"] == "https://signed.example/clip.mp4"
    assert result["cost_tokens"] == 108900
    assert result["media_metadata"]["resolution"] == "720p"


@pytest.mark.asyncio
async def test_cost_usd_zero_without_pricing_config():
    """Default behavior: pricing rate unset → cost_usd = 0.0 even with tokens."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tasks/cgt-cost"):
            return httpx.Response(200, json={
                "id": "cgt-cost",
                "status": "succeeded",
                "content": {"video_url": "https://signed.example/clip.mp4"},
                "usage": {"completion_tokens": 100_000},
                "duration": 5, "resolution": "720p", "ratio": "1:1", "framespersecond": 24,
            })
        return httpx.Response(200, content=b"x")

    dreamina.set_http_client_factory(_factory_with_handler(handler))
    provider = get_video_provider("seedance-1-5-pro")
    result = await provider.poll("cgt-cost")
    assert result["cost_tokens"] == 100_000
    assert result["cost_usd"] == 0.0


@pytest.mark.asyncio
async def test_cost_usd_computed_from_pricing_file(tmp_path, monkeypatch):
    """When pricing rate IS configured, USD is computed correctly."""
    pricing = tmp_path / "pricing.json"
    pricing.write_text(json.dumps({
        "video": {"seedance-1-5-pro": {"usd_per_million_tokens": 5.00}}
    }))
    monkeypatch.setenv("FLOWBOARD_PRICING_PATH", str(pricing))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tasks/cgt-priced"):
            return httpx.Response(200, json={
                "id": "cgt-priced",
                "status": "succeeded",
                "content": {"video_url": "https://signed.example/x.mp4"},
                "usage": {"completion_tokens": 200_000},
                "duration": 5, "resolution": "720p", "ratio": "1:1", "framespersecond": 24,
            })
        return httpx.Response(200, content=b"x")

    dreamina.set_http_client_factory(_factory_with_handler(handler))
    provider = get_video_provider("seedance-1-5-pro")
    result = await provider.poll("cgt-priced")
    # 200_000 tokens × $5 / 1M = $1.00
    assert result["cost_usd"] == pytest.approx(1.0, rel=1e-6)


# ── failure modes ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_submit_auth_failure_returns_auth_code():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={
            "error": {"code": "AuthenticationError", "message": "The API key format is incorrect", "type": "auth"}
        })

    dreamina.set_http_client_factory(_factory_with_handler(handler))
    provider = get_video_provider("seedance-1-5-pro")

    with pytest.raises(VideoError) as exc_info:
        await provider.submit({
            "first_frame_url": "https://example.com/f.png",
            "motion_prompt": "x",
            "duration_seconds": 5,
            "aspect_ratio": "16:9",
            "resolution": "720p",
        })
    assert exc_info.value.code == "auth"


@pytest.mark.asyncio
async def test_submit_400_bad_image_returns_bad_input():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={
            "error": {"code": "InvalidParameter", "message": "unreachable image URL", "type": "invalid"}
        })

    dreamina.set_http_client_factory(_factory_with_handler(handler))
    provider = get_video_provider("seedance-1-5-pro")

    with pytest.raises(VideoError) as exc_info:
        await provider.submit({
            "first_frame_url": "https://broken/x.png",
            "motion_prompt": "x",
            "duration_seconds": 5,
            "aspect_ratio": "16:9",
            "resolution": "720p",
        })
    assert exc_info.value.code == "bad_input"


@pytest.mark.asyncio
async def test_poll_task_not_found_returns_bad_input():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={
            "error": {"code": "ResourceNotFound", "message": "task expired", "type": "not_found"}
        })

    dreamina.set_http_client_factory(_factory_with_handler(handler))
    provider = get_video_provider("seedance-1-5-pro")
    result = await provider.poll("cgt-gone")
    assert result["status"] == "failed"
    assert result["error"] == "bad_input"


@pytest.mark.asyncio
async def test_poll_content_filter_classification():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "cgt-cf",
            "status": "failed",
            "error": {
                "code": "ContentFilter",
                "message": "prompt rejected by safety classifier",
                "type": "safety_filter",
            },
        })

    dreamina.set_http_client_factory(_factory_with_handler(handler))
    provider = get_video_provider("seedance-1-5-pro")
    result = await provider.poll("cgt-cf")
    assert result["status"] == "failed"
    assert result["error"] == "content_filtered"


# ── validation ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_submit_rejects_unsupported_duration():
    dreamina.set_http_client_factory(_factory_with_handler(
        lambda req: httpx.Response(200, json={"id": "x"})
    ))
    provider = get_video_provider("seedance-1-5-pro")

    with pytest.raises(VideoError) as exc_info:
        await provider.submit({
            "first_frame_url": "https://example.com/f.png",
            "motion_prompt": "x",
            "duration_seconds": 999,
            "aspect_ratio": "16:9",
            "resolution": "720p",
        })
    assert exc_info.value.code == "bad_input"
    assert "duration_seconds" in str(exc_info.value)


@pytest.mark.asyncio
async def test_is_available_reflects_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOWBOARD_SECRETS_PATH", str(tmp_path / "missing.json"))
    provider = get_video_provider("seedance-1-5-pro")
    # Cached secret cleared because the path env var changed
    assert await provider.is_available() is False
    secrets.set_api_key("dreamina", "ark-real")
    assert await provider.is_available() is True
