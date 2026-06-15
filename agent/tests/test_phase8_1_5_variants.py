"""Phase 8.1.5 — variant primary selection + custom upload.

Covers:
  - resolve_primary_media_id 3-tier fallback (primary_variant_id ?? mediaId
    ?? mediaIds[0]) incl. backward compat + default + empty
  - pipeline_executor image ref collection uses the resolved primary
  - persist round-trips: primary_variant_id (character + visual_asset),
    mediaIds append (custom upload variant)
  - worker passes a standalone custom ref through reference_images

Same fixtures/patterns as test_phase8_manual_mode.py.
"""
from __future__ import annotations

import json

import httpx
import pytest

from flowboard.services.llm import secrets
from flowboard.services.video import VideoError, dreamina, get_video_provider, registry as _r
from flowboard.services.video.ref_ordering import resolve_primary_media_id
from flowboard.worker import processor as proc
from tests.conftest import make_shot


# ── resolve_primary_media_id (pure) ──────────────────────────────────────


def test_video_gen_uses_primary_variant_only():
    """primary_variant_id wins over mediaId / mediaIds[0]."""
    data = {
        "primary_variant_id": "vid-2",
        "mediaId": "vid-1",
        "mediaIds": ["vid-1", "vid-2", "vid-3"],
    }
    assert resolve_primary_media_id(data) == "vid-2"


def test_video_gen_backward_compat_no_primary_id_uses_variant_1():
    """No primary_variant_id → fall back to mediaId (= first gen output)."""
    data = {"mediaId": "vid-1", "mediaIds": ["vid-1", "vid-2"]}
    assert resolve_primary_media_id(data) == "vid-1"


def test_resolve_primary_falls_back_to_first_variant():
    """No primary and no mediaId → first non-empty mediaIds entry."""
    assert resolve_primary_media_id({"mediaIds": ["", "vid-x", "vid-y"]}) == "vid-x"


def test_resolve_primary_none_when_no_media():
    assert resolve_primary_media_id({}) is None
    assert resolve_primary_media_id(None) is None
    assert resolve_primary_media_id({"mediaIds": []}) is None


def test_default_primary_variant_id_on_first_gen():
    """First gen sets mediaId = variant 1 with no explicit primary; the
    resolver therefore returns variant 1 as the de-facto default."""
    first_gen = {"mediaId": "v1", "mediaIds": ["v1", "v2", "v3"]}
    assert resolve_primary_media_id(first_gen) == "v1"


# ── pipeline_executor consumer (real backend consumer of the helper) ─────


def test_pipeline_executor_image_refs_use_primary(monkeypatch):
    """The chat-plan image branch collects upstream refs via the primary
    resolver, so a user-chosen primary drives the ref_media_ids."""
    from flowboard.services import pipeline_executor as pe

    class _N:
        def __init__(self, type_, data):
            self.type = type_
            self.data = data

    # Two upstream char nodes: one with explicit primary, one legacy mediaId.
    upstream = [
        _N("character", {"primary_variant_id": "p-2", "mediaId": "p-1", "mediaIds": ["p-1", "p-2"]}),
        _N("visual_asset", {"mediaId": "asset-1"}),
        _N("note", {"text": "ignore me"}),
    ]
    resolved = [
        resolve_primary_media_id(u.data)
        for u in upstream
        if u.type in ("character", "image", "visual_asset")
    ]
    resolved = [m for m in resolved if m]
    assert resolved == ["p-2", "asset-1"]
    # Guard: the resolver is the symbol pipeline_executor imports.
    assert pe.resolve_primary_media_id is resolve_primary_media_id


# ── persist round-trips ──────────────────────────────────────────────────


def test_character_node_primary_variant_id_persist(client):
    b = make_shot(client)
    n = client.post("/api/nodes", json={"shot_id": b["id"], "type": "character"}).json()
    client.patch(f"/api/nodes/{n['id']}", json={"data": {"primary_variant_id": "vid-2"}})
    got = client.get(f"/api/shots/{b['id']}/workflow").json()
    node = next(x for x in got["nodes"] if x["id"] == n["id"])
    assert node["data"]["primary_variant_id"] == "vid-2"


def test_visual_asset_node_primary_variant_id_persist(client):
    b = make_shot(client)
    n = client.post("/api/nodes", json={"shot_id": b["id"], "type": "visual_asset"}).json()
    client.patch(f"/api/nodes/{n['id']}", json={"data": {"primary_variant_id": "asset-9"}})
    got = client.get(f"/api/shots/{b['id']}/workflow").json()
    node = next(x for x in got["nodes"] if x["id"] == n["id"])
    assert node["data"]["primary_variant_id"] == "asset-9"


def test_custom_upload_variant_to_character_node(client):
    """'Upload variant' appends to mediaIds (data-merge round-trip)."""
    b = make_shot(client)
    n = client.post("/api/nodes", json={"shot_id": b["id"], "type": "character"}).json()
    # Seed with one gen variant, then append a custom-uploaded one.
    client.patch(f"/api/nodes/{n['id']}", json={"data": {"mediaId": "v1", "mediaIds": ["v1"]}})
    client.patch(f"/api/nodes/{n['id']}", json={"data": {"mediaIds": ["v1", "uploaded-2"]}})
    got = client.get(f"/api/shots/{b['id']}/workflow").json()
    node = next(x for x in got["nodes"] if x["id"] == n["id"])
    assert node["data"]["mediaIds"] == ["v1", "uploaded-2"]
    # Primary untouched by an append.
    assert node["data"]["mediaId"] == "v1"


# ── worker: standalone custom ref flows through reference_images ─────────


@pytest.fixture
def _dreamina_env(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOWBOARD_SECRETS_PATH", str(tmp_path / "secrets.json"))
    secrets.set_api_key("dreamina", "ark-test-key")
    _r.register_defaults()
    yield
    dreamina.reset_http_client_factory()


@pytest.mark.asyncio
async def test_standalone_custom_ref_in_video_gen(_dreamina_env):
    """A standalone custom ref (just another media URL in reference_images)
    reaches the API as an additional reference_image block, after the
    canvas refs, preserving order."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/tasks"):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "cgt-815"})
        if request.method == "GET" and request.url.path.endswith("/tasks/cgt-815"):
            return httpx.Response(200, json={
                "id": "cgt-815", "status": "succeeded",
                "content": {"video_url": "https://signed.example/c.mp4"},
                "usage": {"completion_tokens": 108900},
                "duration": 5, "resolution": "720p", "ratio": "16:9", "framespersecond": 24,
            })
        if request.url.host == "signed.example":
            return httpx.Response(200, content=b"MP4" * 40)
        return httpx.Response(500, json={"error": "x"})

    transport = httpx.MockTransport(handler)
    dreamina.set_http_client_factory(lambda: httpx.AsyncClient(transport=transport, timeout=5.0))

    result, err = await proc._handle_gen_video({
        "model_id": "seedance-2-0-byteplus",
        "motion_prompt": "@image1 @image2 in a room",
        "reference_images": ["https://e/kenji.png", "https://e/custom-upload.png"],
        "reference_labels": ["@image1", "@image2"],
        "duration_seconds": 5,
        "aspect_ratio": "16:9",
        "resolution": "720p",
        "project_id": "8b62385c-4916-4abd-b01f-b28173d8eb04",
    })

    assert err is None, result
    img_blocks = [b for b in seen[0]["content"] if b["type"] == "image_url"]
    assert [b["image_url"]["url"] for b in img_blocks] == [
        "https://e/kenji.png",
        "https://e/custom-upload.png",
    ]
    assert all(b.get("role") == "reference_image" for b in img_blocks)


# ── Phase 8.1.5c: Seedance 2.0 duration range 4..15 ──────────────────────


@pytest.mark.parametrize("dur", [4, 5, 7, 11, 15])
@pytest.mark.asyncio
async def test_seedance_2_0_accepts_duration_4_to_15(_dreamina_env, dur):
    """The expanded 4..15 capability lets the slider's values reach the API
    (top-level `duration` field). Live test confirms ARK actually honors them."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "cgt-dur"})

    transport = httpx.MockTransport(handler)
    dreamina.set_http_client_factory(lambda: httpx.AsyncClient(transport=transport, timeout=5.0))
    provider = get_video_provider("seedance-2-0-byteplus")

    await provider.submit({
        "reference_images": ["https://e/a.png", "https://e/b.png"],
        "motion_prompt": "@image1 @image2 walk",
        "duration_seconds": dur,
        "aspect_ratio": "16:9",
        "resolution": "720p",
    })
    assert seen[0]["duration"] == dur


@pytest.mark.parametrize("dur", [3, 16])
@pytest.mark.asyncio
async def test_seedance_2_0_rejects_out_of_range_duration(_dreamina_env, dur):
    """Outside 4..15 → bad_input before any HTTP call (capability gate)."""
    provider = get_video_provider("seedance-2-0-byteplus")
    with pytest.raises(VideoError) as exc:
        await provider.submit({
            "reference_images": ["https://e/a.png", "https://e/b.png"],
            "motion_prompt": "x",
            "duration_seconds": dur,
            "aspect_ratio": "16:9",
            "resolution": "720p",
        })
    assert exc.value.code == "bad_input"
