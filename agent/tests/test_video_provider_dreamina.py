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


def test_inject_image_labels_prepends_positional_tags():
    out = dreamina.inject_image_labels("they walk together", 3)
    assert out == "@image1 @image2 @image3 they walk together"


def test_inject_image_labels_noop_when_no_refs():
    assert dreamina.inject_image_labels("solo shot", 0) == "solo shot"


def test_inject_image_labels_skips_when_already_tagged():
    """Phase 6 synth may author rich '@image1 = X' lines — don't double-tag."""
    prompt = "@image1 = Kenji, @image2 = Ren"
    assert dreamina.inject_image_labels(prompt, 2) == prompt


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
async def test_r2v_model_emits_reference_image_blocks_no_first_frame():
    """Seedance 2.0 with ≥2 refs → pure r2v: only reference_image blocks,
    NO first_frame (contract §11.7), and @imageN tags injected into text.

    This corrects the Phase 5 conflation bug (dreamina.py:222-247) which
    emitted a first_frame block alongside reference_image blocks.
    """
    seen_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "cgt-r2v-test"})

    dreamina.set_http_client_factory(_factory_with_handler(handler))
    provider = get_video_provider("seedance-2-0")

    result = await provider.submit({
        # An upstream start frame is present but MUST be ignored in r2v.
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

    body = seen_bodies[0]
    image_blocks = [b for b in body["content"] if b.get("type") == "image_url"]
    # Exactly the 2 refs, both reference_image; no first_frame.
    assert len(image_blocks) == 2
    assert all(b.get("role") == "reference_image" for b in image_blocks)
    assert [b["image_url"]["url"] for b in image_blocks] == [
        "https://example.com/r1.png",
        "https://example.com/r2.png",
    ]
    # @imageN positional tags injected into the prompt text.
    text = next(b["text"] for b in body["content"] if b.get("type") == "text")
    assert "@image1" in text and "@image2" in text
    # The ignored first_frame must surface as a warning, not a silent drop.
    assert any("i2v mode" in w or "reference" in w.lower() for w in result["warnings"])


@pytest.mark.asyncio
async def test_r2v_skips_label_injection_when_prompt_has_tags():
    """If the caller's prompt already carries @imageN tags (Phase 6 synth),
    the provider must not double-tag."""
    seen_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "cgt-r2v-tags"})

    dreamina.set_http_client_factory(_factory_with_handler(handler))
    provider = get_video_provider("seedance-2-0")
    await provider.submit({
        "reference_images": ["https://e/r1.png", "https://e/r2.png"],
        "motion_prompt": "@image1 = Kenji left, @image2 = Ren right, they talk",
        "duration_seconds": 5,
        "aspect_ratio": "16:9",
        "resolution": "720p",
    })
    text = next(b["text"] for b in seen_bodies[0]["content"] if b.get("type") == "text")
    # Exactly one occurrence each — no duplicate prepended tag block.
    assert text.count("@image1") == 1
    assert text.count("@image2") == 1


@pytest.mark.asyncio
async def test_r2v_plus_audio_emits_reference_audio_block():
    """Audio ref → r2v+audio: reference_image blocks + one reference_audio,
    NO first_frame (audio = reference media mode, §11.3)."""
    seen_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "cgt-audio"})

    dreamina.set_http_client_factory(_factory_with_handler(handler))
    provider = get_video_provider("seedance-2-0")
    result = await provider.submit({
        "reference_images": ["https://e/kenji.png"],
        "audio_ref_url": "https://e/voice.mp3",
        "motion_prompt": "Kenji speaks with authority",
        "duration_seconds": 8,
        "aspect_ratio": "16:9",
        "resolution": "720p",
    })

    body = seen_bodies[0]
    image_blocks = [b for b in body["content"] if b.get("type") == "image_url"]
    audio_blocks = [b for b in body["content"] if b.get("type") == "audio_url"]
    assert all(b.get("role") == "reference_image" for b in image_blocks)
    assert not any(b.get("role") in ("first_frame", "last_frame") for b in image_blocks)
    assert len(audio_blocks) == 1
    assert audio_blocks[0]["role"] == "reference_audio"
    assert audio_blocks[0]["audio_url"]["url"] == "https://e/voice.mp3"
    assert result["warnings"] == []


@pytest.mark.asyncio
async def test_audio_ref_dropped_with_warning_on_i2v_model():
    """Seedance 1.5 has no reference_audio — drop the audio with a warning,
    fall back to plain i2v."""
    seen_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "cgt-noaudio"})

    dreamina.set_http_client_factory(_factory_with_handler(handler))
    provider = get_video_provider("seedance-1-5-pro")
    result = await provider.submit({
        "first_frame_url": "https://e/f.png",
        "audio_ref_url": "https://e/voice.mp3",
        "motion_prompt": "a girl smiles",
        "duration_seconds": 5,
        "aspect_ratio": "16:9",
        "resolution": "720p",
    })
    assert any("audio" in w.lower() for w in result["warnings"])
    assert not any(b.get("type") == "audio_url" for b in seen_bodies[0]["content"])


@pytest.mark.asyncio
async def test_single_ref_on_2_0_stays_i2v():
    """Per contract §11.7 + spec: a lone ref (no audio) is i2v, not r2v.
    With no start frame, the lone ref is promoted to the first_frame."""
    seen_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "cgt-single"})

    dreamina.set_http_client_factory(_factory_with_handler(handler))
    provider = get_video_provider("seedance-2-0")
    await provider.submit({
        "reference_images": ["https://e/only.png"],
        "motion_prompt": "pan across",
        "duration_seconds": 5,
        "aspect_ratio": "16:9",
        "resolution": "720p",
    })
    image_blocks = [b for b in seen_bodies[0]["content"] if b.get("type") == "image_url"]
    assert len(image_blocks) == 1
    # i2v single-image submit → role omitted (§2.6).
    assert "role" not in image_blocks[0]


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
