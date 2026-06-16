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


def test_models_registered_at_boot():
    ids = {m.model_id for m in list_video_models()}
    assert ids == {
        "flow-default",
        "seedance-1-5-pro",
        "seedance-2-0",
        "seedance-2-0-byteplus",
    }


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


def test_seedance_2_0_routes_through_avis():
    # Seedance 2.0 was repointed to the Avis gateway (see registry). It still
    # advertises r2v + the generate-audio toggle, but NOT audio-reference
    # (audioInput isn't wired in the Avis adapter yet).
    entry = get_video_model("seedance-2-0")
    assert entry.provider_name == "avis"
    assert entry.upstream_model_id == "dreamina-seedance-2-0"
    assert entry.capabilities.supports_multi_ref is True
    assert entry.capabilities.max_refs >= 1
    assert entry.capabilities.supports_audio_toggle is True
    assert entry.capabilities.supports_audio_ref is False
    # Person-driven (KYC) supported on Avis Seedance 2.0; not on the byteplus path.
    assert entry.capabilities.supports_kyc is True
    assert get_video_model("seedance-2-0-byteplus").capabilities.supports_kyc is False


def test_seedance_2_0_byteplus_keeps_direct_path():
    # The BytePlus-direct path is retained under a distinct id, still r2v+audio.
    entry = get_video_model("seedance-2-0-byteplus")
    assert entry.provider_name == "dreamina"
    assert entry.upstream_model_id == "dreamina-seedance-2-0-260128"
    assert entry.capabilities.supports_multi_ref is True
    assert entry.capabilities.supports_audio_ref is True


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
        supports_audio_ref=False,
        max_refs=0,
        aspect_ratios=("16:9",),
        resolutions=("720p",),
        durations=(5,),
    )
    with pytest.raises(AttributeError):
        cap.supports_multi_ref = True  # type: ignore[misc]
