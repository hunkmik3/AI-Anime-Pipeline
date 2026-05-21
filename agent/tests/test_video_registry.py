"""Sanity checks for the video model registry."""
from __future__ import annotations

import pytest

from flowboard.services.video import (
    VideoProviderCapability,
    get_default_model_id,
    get_video_model,
    list_video_models,
)
from flowboard.services.video import registry as _r


@pytest.fixture(autouse=True)
def _ensure_defaults_registered():
    _r.register_defaults()
    yield


def test_default_model_is_flow():
    assert get_default_model_id() == "flow-default"


def test_three_models_registered_at_phase5_boot():
    ids = {m.model_id for m in list_video_models()}
    assert ids == {"flow-default", "seedance-1-5-pro", "seedance-2-0"}


def test_unknown_model_raises_keyerror():
    with pytest.raises(KeyError):
        get_video_model("definitely-not-a-real-model")


def test_seedance_1_5_pro_is_i2v_only():
    entry = get_video_model("seedance-1-5-pro")
    assert entry.provider_name == "dreamina"
    assert entry.upstream_model_id == "seedance-1-5-pro-251215"
    assert entry.capabilities.supports_multi_ref is False
    assert entry.capabilities.max_refs == 0
    # Per the contract §2.6 keyframe interpolation IS supported on 1.5 Pro
    assert entry.capabilities.supports_last_frame is True


def test_seedance_2_0_advertises_r2v_and_audio():
    entry = get_video_model("seedance-2-0")
    assert entry.provider_name == "dreamina"
    assert entry.capabilities.supports_multi_ref is True
    assert entry.capabilities.max_refs >= 1
    assert entry.capabilities.supports_audio_toggle is True


def test_flow_capabilities_match_legacy_surface():
    entry = get_video_model("flow-default")
    assert entry.provider_name == "flow"
    assert entry.capabilities.supports_multi_ref is False
    assert entry.capabilities.supports_audio_toggle is False
    # Aspect ratios are the human strings; the Flow provider translates
    # them to the enum at submit time.
    assert "16:9" in entry.capabilities.aspect_ratios


def test_capability_is_frozen_dataclass():
    cap = VideoProviderCapability(
        supports_multi_ref=False,
        supports_last_frame=False,
        supports_audio_toggle=False,
        max_refs=0,
        aspect_ratios=("16:9",),
        resolutions=("720p",),
        durations=(5,),
    )
    with pytest.raises(AttributeError):
        cap.supports_multi_ref = True  # type: ignore[misc]
