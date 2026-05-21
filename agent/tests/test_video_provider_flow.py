"""Provider-Protocol-level tests for FlowVideoProvider.

These hit the wrapper directly (bypassing the worker handler) to prove
the Protocol surface — submit / poll / run_to_completion — works as
the registry advertises. The worker-handler tests in test_requests.py
already validate the full end-to-end Flow lifecycle, so we don't
re-test that here.
"""
from __future__ import annotations

import pytest

from flowboard.services.flow_client import flow_client
from flowboard.services.video import VideoError, get_video_model, get_video_provider
from flowboard.services.video import registry as _r
from flowboard.services.video import flow as flow_provider


@pytest.fixture(autouse=True)
def _registry():
    _r.register_defaults()
    yield


@pytest.fixture(autouse=True)
def _reset_flow_tier():
    flow_client._paygate_tier = "PAYGATE_TIER_ONE"
    yield
    flow_client._paygate_tier = None


def _make_provider():
    entry = get_video_model("flow-default")
    return entry.factory(entry)


@pytest.mark.asyncio
async def test_submit_returns_synthetic_job_id_for_each_dispatch(monkeypatch):
    class _StubSdk:
        async def gen_video(self, **kwargs):
            return {"raw": {}, "operation_names": ["op-A", "op-B"]}

    from flowboard.worker import processor as _proc
    monkeypatch.setattr(_proc, "get_flow_sdk", lambda: _StubSdk())

    provider = _make_provider()
    out = await provider.submit({
        "motion_prompt": "x",
        "first_frame_url": "media-1",
        "project_id": "abcd1234",
        "paygate_tier": "PAYGATE_TIER_ONE",
        "aspect_ratio": "16:9",
    })
    assert out["external_job_id"].startswith("flow:op-A:")
    # No capability mismatch on a single-frame submit — no warnings.
    assert out["warnings"] == []


@pytest.mark.asyncio
async def test_submit_warns_when_refs_passed_to_flow(monkeypatch):
    class _StubSdk:
        async def gen_video(self, **kwargs):
            return {"raw": {}, "operation_names": ["op-1"]}

    from flowboard.worker import processor as _proc
    monkeypatch.setattr(_proc, "get_flow_sdk", lambda: _StubSdk())

    provider = _make_provider()
    out = await provider.submit({
        "motion_prompt": "x",
        "first_frame_url": "media-1",
        "reference_images": ["ref-1", "ref-2"],  # Flow ignores these
        "project_id": "abcd1234",
        "paygate_tier": "PAYGATE_TIER_ONE",
        "aspect_ratio": "16:9",
    })
    assert any("multi-ref" in w for w in out["warnings"])


@pytest.mark.asyncio
async def test_submit_rejects_missing_first_frame(monkeypatch):
    class _StubSdk:
        async def gen_video(self, **kwargs):
            return {"raw": {}, "operation_names": ["op"]}

    from flowboard.worker import processor as _proc
    monkeypatch.setattr(_proc, "get_flow_sdk", lambda: _StubSdk())

    provider = _make_provider()
    with pytest.raises(VideoError) as exc_info:
        await provider.submit({
            "motion_prompt": "x",
            "first_frame_url": "",
            "project_id": "abcd1234",
            "paygate_tier": "PAYGATE_TIER_ONE",
            "aspect_ratio": "16:9",
        })
    assert exc_info.value.code == "bad_input"


@pytest.mark.asyncio
async def test_aspect_string_translates_to_flow_enum(monkeypatch):
    captured: dict = {}

    class _StubSdk:
        async def gen_video(self, **kwargs):
            captured.update(kwargs)
            return {"raw": {}, "operation_names": ["op-1"]}

    from flowboard.worker import processor as _proc
    monkeypatch.setattr(_proc, "get_flow_sdk", lambda: _StubSdk())

    provider = _make_provider()
    await provider.submit({
        "motion_prompt": "x",
        "first_frame_url": "media-1",
        "project_id": "abcd1234",
        "paygate_tier": "PAYGATE_TIER_ONE",
        "aspect_ratio": "9:16",
    })
    assert captured["aspect_ratio"] == "VIDEO_ASPECT_RATIO_PORTRAIT"


@pytest.mark.asyncio
async def test_run_to_completion_resolves_dispatch(monkeypatch):
    """Verify the full Protocol-level driver picks up monkeypatched poll knobs."""
    monkeypatch.setattr("flowboard.worker.processor.VIDEO_POLL_INTERVAL_S", 0.001)
    monkeypatch.setattr("flowboard.worker.processor.VIDEO_POLL_MAX_CYCLES", 5)

    class _StubSdk:
        def __init__(self):
            self.poll_calls = 0

        async def gen_video(self, **kwargs):
            return {"raw": {}, "operation_names": ["op-1"]}

        async def check_async(self, names, workflows=None):
            self.poll_calls += 1
            done = self.poll_calls >= 2
            return {
                "raw": {},
                "operations": [{
                    "name": "op-1",
                    "done": done,
                    "media_entries": [{
                        "media_id": "vid-from-flow",
                        "url": "https://flow-content.google/v/vid-from-flow?sig=z",
                    }] if done else [],
                }],
            }

    stub = _StubSdk()
    from flowboard.worker import processor as _proc
    monkeypatch.setattr(_proc, "get_flow_sdk", lambda: stub)

    provider = _make_provider()
    submit_result, poll_result = await provider.run_to_completion({
        "motion_prompt": "x",
        "first_frame_url": "media-1",
        "project_id": "abcd1234",
        "paygate_tier": "PAYGATE_TIER_ONE",
        "aspect_ratio": "16:9",
    })
    assert poll_result["status"] == "succeeded"
    assert stub.poll_calls >= 2
    # Legacy shape preserved in raw — frontend depends on it.
    assert poll_result["raw"]["media_ids"] == ["vid-from-flow"]


def test_error_classifier_maps_common_phrases():
    assert flow_provider._classify_flow_error("content filter triggered") == "content_filtered"
    assert flow_provider._classify_flow_error("HTTP 401 unauthorized") == "auth"
    assert flow_provider._classify_flow_error("rate limited") == "quota"
    assert flow_provider._classify_flow_error("operation timeout") == "timeout"
    assert flow_provider._classify_flow_error("invalid project id") == "bad_input"
    assert flow_provider._classify_flow_error("something else weird") == "internal"
