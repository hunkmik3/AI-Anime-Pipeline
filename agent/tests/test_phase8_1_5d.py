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
from flowboard.services.video import avis
from flowboard.worker import processor as proc
from tests.conftest import make_shot


@pytest.fixture
def _avis_env(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOWBOARD_SECRETS_PATH", str(tmp_path / "secrets.json"))
    secrets.set_api_key("avis", "avis-test-key")
    _r.register_defaults()
    yield
    avis.reset_http_client_factory()


def _factory(handler):
    t = httpx.MockTransport(handler)
    return lambda: httpx.AsyncClient(transport=t, timeout=5.0)


# ── capability ───────────────────────────────────────────────────────────


def test_seedance_2_0_supports_video_ref(_avis_env):
    assert get_video_model("seedance-2-0").capabilities.supports_video_ref is True
    assert get_video_model("seedance-1-5-pro").capabilities.supports_video_ref is False


# ── provider emits the block ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_video_ref_emits_reference_video_block(_avis_env):
    seen: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"data": {"taskId": "cgt-vref"}, "success": True})

    avis.set_http_client_factory(_factory(handler))
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
    # Avis' own shape, not ARK's: a flat `videoUrl` part rather than a
    # `video_url` block carrying a `role`. The behaviour under test — that a
    # reference video reaches the API — is the same one; only the wire is.
    vblocks = [b for b in blocks if b.get("type") == "videoUrl"]
    assert len(vblocks) == 1, blocks
    assert vblocks[0]["url"] == "https://e/clip.mp4"
    # image ref still present → r2v, so it is a referenceImage and not a start frame
    assert any(b.get("role") == "referenceImage" for b in blocks), blocks
    assert not any(b.get("role") == "firstFrame" for b in blocks), blocks


@pytest.mark.asyncio
async def test_video_ref_dropped_with_warning_on_1_5(_avis_env):
    seen: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"data": {"taskId": "cgt-x"}, "success": True})

    avis.set_http_client_factory(_factory(handler))
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
async def test_worker_forwards_reference_videos(_avis_env):
    seen: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        # Avis' endpoints and envelope, not ARK's: POST /video/generations to
        # submit, GET /video/tasks/{id} to poll, and a flat `videoUrl`.
        if req.method == "POST" and req.url.path.endswith("/video/generations"):
            seen.append(json.loads(req.content))
            return httpx.Response(200, json={"data": {"taskId": "cgt-w"}, "success": True})
        if req.method == "GET" and "/video/tasks/" in req.url.path:
            return httpx.Response(200, json={
                "success": True,
                "data": {
                    "taskId": "cgt-w",
                    "status": "succeeded",
                    "videoUrl": "https://signed.example/c.mp4",
                    "duration": 5,
                    "resolution": "720p",
                    "ratio": "16:9",
                },
            })
        if req.url.host == "signed.example":
            return httpx.Response(200, content=b"MP4" * 40)
        return httpx.Response(500, json={"error": "x"})

    avis.set_http_client_factory(_factory(handler))
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
    vblocks = [b for b in seen[0]["content"] if b.get("type") == "videoUrl"]
    assert [b["url"] for b in vblocks] == ["https://e/clip.mp4"]


@pytest.mark.asyncio
async def test_avis_hoists_bare_video_ref_to_r2(_avis_env, monkeypatch):
    """Avis sends image refs inline (no R2) but has NO inline video upload, so a
    bare media_id video ref MUST still be hoisted to a public R2 URL."""
    secrets.set_api_key("avis", "avis-test-key")
    _r.register_defaults()

    # Fake the R2 hoist (function-level import in the worker reads this attr).
    monkeypatch.setattr(
        avis,
        "media_id_to_public_url",
        lambda mid, project_id=None: f"https://r2.example/{mid}.mp4",
    )

    captured: dict = {}

    async def fake_run(self, params):
        captured.update(params)
        return (
            {"external_job_id": "cgt-avis", "media_ids": []},
            {"status": "succeeded", "video_url": "https://signed/c.mp4", "cost_usd": None},
        )

    from flowboard.services.video.avis import AvisVideoProvider

    monkeypatch.setattr(AvisVideoProvider, "run_to_completion", fake_run)

    result, err = await proc._handle_gen_video({
        "model_id": "seedance-2-0",  # → Avis provider
        "motion_prompt": "@image1 moves like the clip",
        "reference_images": ["https://e/a.png"],  # URL → passes through (no R2)
        "reference_videos": ["vid_media_42"],      # bare media_id → MUST hoist
        "duration_seconds": 5,
        "aspect_ratio": "16:9",
        "resolution": "720p",
        "project_id": "8b62385c-4916-4abd-b01f-b28173d8eb04",
    })
    assert err is None, result
    # The bare media_id was hoisted to an R2 URL; the inline image ref was not.
    assert captured["reference_videos"] == ["https://r2.example/vid_media_42.mp4"]
    assert captured["reference_images"] == ["https://e/a.png"]


@pytest.mark.asyncio
async def test_video_refs_ordered_by_label(_avis_env, monkeypatch):
    """reference_videos reorder by @video label digit (parity with @image)."""
    secrets.set_api_key("avis", "k")
    _r.register_defaults()
    monkeypatch.setattr(
        avis, "media_id_to_public_url",
        lambda mid, project_id=None: f"https://r2/{mid}.mp4",
    )
    captured: dict = {}

    async def fake_run(self, params):
        captured.update(params)
        return (
            {"external_job_id": "x", "media_ids": []},
            {"status": "succeeded", "video_url": "https://s/c.mp4", "cost_usd": None},
        )

    from flowboard.services.video.avis import AvisVideoProvider

    monkeypatch.setattr(AvisVideoProvider, "run_to_completion", fake_run)

    result, err = await proc._handle_gen_video({
        "model_id": "seedance-2-0",
        "motion_prompt": "x",
        "reference_images": ["https://e/a.png"],
        "reference_videos": ["vidB", "vidA"],          # edge order
        "reference_video_labels": ["@video2", "@video1"],  # → reorder to vidA, vidB
        "duration_seconds": 5,
        "aspect_ratio": "16:9",
        "resolution": "720p",
        "project_id": "8b62385c-4916-4abd-b01f-b28173d8eb04",
    })
    assert err is None, result
    assert captured["reference_videos"] == ["https://r2/vidA.mp4", "https://r2/vidB.mp4"]


# ── /upload-video endpoint ────────────────────────────────────────────────


def test_create_video_ref_node(client):
    """The video_ref node type must be accepted by the node-create endpoint.
    (Backend Literal allowlist gates node.type; if missing the POST 422s and
    the canvas silently fails to add the node.)"""
    b = make_shot(client)
    r = client.post(
        "/api/nodes",
        json={"shot_id": b["id"], "type": "video_ref", "x": 0, "y": 0, "data": {"title": "Video ref"}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["type"] == "video_ref"


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
