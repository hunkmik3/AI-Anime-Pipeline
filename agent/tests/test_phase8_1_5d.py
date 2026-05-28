"""Phase 8.1.5d — reference_video support + custom upload endpoints.

- supports_video_ref capability (2.0 yes, 1.5/flow no)
- provider emits a video_url role="reference_video" block in r2v (§11.9)
- video refs dropped-with-warning on a model that lacks support
- worker hoists/forwards reference_videos
- /upload-video endpoint mime gate

(Dialog scroll, progress overlay, legacy-input removal are frontend-only
with no vitest → verified by live test.)
"""
from __future__ import annotations

import io
import json

import httpx
import pytest

from flowboard.services.llm import secrets
from flowboard.services.video import get_video_model, get_video_provider, registry as _r
from flowboard.services.video import dreamina
from flowboard.worker import processor as proc
from tests.conftest import make_shot


@pytest.fixture
def _dreamina_env(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOWBOARD_SECRETS_PATH", str(tmp_path / "secrets.json"))
    secrets.set_api_key("dreamina", "ark-test-key")
    _r.register_defaults()
    yield
    dreamina.reset_http_client_factory()


def _factory(handler):
    t = httpx.MockTransport(handler)
    return lambda: httpx.AsyncClient(transport=t, timeout=5.0)


# ── capability ───────────────────────────────────────────────────────────


def test_seedance_2_0_supports_video_ref(_dreamina_env):
    assert get_video_model("seedance-2-0").capabilities.supports_video_ref is True
    assert get_video_model("seedance-1-5-pro").capabilities.supports_video_ref is False
    assert get_video_model("flow-default").capabilities.supports_video_ref is False


# ── provider emits the block ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_video_ref_emits_reference_video_block(_dreamina_env):
    seen: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"id": "cgt-vref"})

    dreamina.set_http_client_factory(_factory(handler))
    provider = get_video_provider("seedance-2-0")
    res = await provider.submit({
        "reference_images": ["https://e/kenji.png"],
        "reference_videos": ["https://e/clip.mp4"],
        "motion_prompt": "@image1 moves like the ref clip",
        "duration_seconds": 5,
        "aspect_ratio": "16:9",
        "resolution": "720p",
    })
    assert res["external_job_id"] == "cgt-vref"
    blocks = seen[0]["content"]
    vblocks = [b for b in blocks if b.get("type") == "video_url"]
    assert len(vblocks) == 1
    assert vblocks[0]["role"] == "reference_video"
    assert vblocks[0]["video_url"]["url"] == "https://e/clip.mp4"
    # image ref still present → r2v (no first_frame block)
    assert any(b.get("role") == "reference_image" for b in blocks)
    assert not any(b.get("role") == "first_frame" for b in blocks)


@pytest.mark.asyncio
async def test_video_ref_dropped_with_warning_on_1_5(_dreamina_env):
    seen: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"id": "cgt-x"})

    dreamina.set_http_client_factory(_factory(handler))
    provider = get_video_provider("seedance-1-5-pro")
    res = await provider.submit({
        "first_frame_url": "https://e/frame.png",
        "reference_videos": ["https://e/clip.mp4"],
        "motion_prompt": "pan",
        "duration_seconds": 5,
        "aspect_ratio": "16:9",
        "resolution": "720p",
    })
    assert any("reference video" in w.lower() for w in res["warnings"])
    assert not any(b.get("type") == "video_url" for b in seen[0]["content"])


# ── worker forwards reference_videos ──────────────────────────────────────


@pytest.mark.asyncio
async def test_worker_forwards_reference_videos(_dreamina_env):
    seen: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path.endswith("/tasks"):
            seen.append(json.loads(req.content))
            return httpx.Response(200, json={"id": "cgt-w"})
        if req.method == "GET" and req.url.path.endswith("/tasks/cgt-w"):
            return httpx.Response(200, json={
                "id": "cgt-w", "status": "succeeded",
                "content": {"video_url": "https://signed.example/c.mp4"},
                "usage": {"completion_tokens": 108900},
                "duration": 5, "resolution": "720p", "ratio": "16:9", "framespersecond": 24,
            })
        if req.url.host == "signed.example":
            return httpx.Response(200, content=b"MP4" * 40)
        return httpx.Response(500, json={"error": "x"})

    dreamina.set_http_client_factory(_factory(handler))
    result, err = await proc._handle_gen_video({
        "model_id": "seedance-2-0",
        "motion_prompt": "@image1 + ref clip",
        "reference_images": ["https://e/a.png"],
        "reference_videos": ["https://e/clip.mp4"],
        "duration_seconds": 5,
        "aspect_ratio": "16:9",
        "resolution": "720p",
        "project_id": "8b62385c-4916-4abd-b01f-b28173d8eb04",
    })
    assert err is None, result
    vblocks = [b for b in seen[0]["content"] if b.get("type") == "video_url"]
    assert [b["video_url"]["url"] for b in vblocks] == ["https://e/clip.mp4"]


# ── /upload-video endpoint ────────────────────────────────────────────────


def test_upload_video_accepts_mp4(client):
    b = make_shot(client)
    r = client.post(
        "/api/upload-video",
        data={"project_id": b["project_id"]},
        files={"file": ("clip.mp4", io.BytesIO(b"\x00\x00\x00\x18ftypmp42fake"), "video/mp4")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["mime"] == "video/mp4"


def test_upload_video_rejects_wrong_mime(client):
    b = make_shot(client)
    r = client.post(
        "/api/upload-video",
        data={"project_id": b["project_id"]},
        files={"file": ("x.png", io.BytesIO(b"\x89PNG\r\n"), "image/png")},
    )
    assert r.status_code == 415


def test_mov_serves_video_quicktime_mime():
    """Phase 8.1.5e: .mov uploads must serve with video/quicktime so the
    dialog <video> preview renders (was application/octet-stream)."""
    from flowboard.services import media as m
    assert m._mime_from_ext(".mov") == "video/quicktime"
    assert m._mime_from_ext(".mp4") == "video/mp4"
