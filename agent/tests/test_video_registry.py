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


def test_default_model_is_a_registered_model():
    # The fallback must name a model `register_defaults` actually registers. It
    # used to be "flow-default", which stopped existing when the Flow bridge was
    # removed — an unset env var then failed at generation time, not at boot.
    from flowboard.services.video.registry import FALLBACK_VIDEO_MODEL, is_registered

    assert get_default_model_id() == FALLBACK_VIDEO_MODEL
    assert is_registered(FALLBACK_VIDEO_MODEL)


def test_models_registered_at_boot():
    ids = {m.model_id for m in list_video_models()}
    # Everything routes through Avis since the Flow bridge and the BytePlus-direct
    # path were removed.
    assert ids == {
        "seedance-1-5-pro",
        "seedance-1-0-pro",
        "seedance-1-0-pro-fast",
        "seedance-2-0",
        "dreamina-seedance-2-0-fast",
        "dreamina-seedance-2-0-mini",
    }
    assert {m.provider_name for m in list_video_models()} == {"avis"}


def test_unknown_model_raises_keyerror():
    with pytest.raises(KeyError):
        get_video_model("definitely-not-a-real-model")


def test_seedance_1_5_pro_is_i2v_only():
    entry = get_video_model("seedance-1-5-pro")
    # Re-pointed to Avis; the BytePlus-direct entry it used to shadow is gone.
    assert entry.provider_name == "avis"
    assert entry.upstream_model_id == "seedance-1-5-pro"
    assert entry.capabilities.supports_multi_ref is False
    assert entry.capabilities.max_refs == 0
    # Per the contract §2.6 keyframe interpolation IS supported on 1.5 Pro
    assert entry.capabilities.supports_last_frame is True


def test_seedance_2_0_routes_through_avis():
    # Seedance 2.0 was repointed to the Avis gateway (see registry). It
    # advertises r2v, the generate-audio toggle, AND audio-reference (audioInput
    # is now wired in the Avis adapter per the published contract).
    entry = get_video_model("seedance-2-0")
    assert entry.provider_name == "avis"
    assert entry.upstream_model_id == "dreamina-seedance-2-0"
    assert entry.capabilities.supports_multi_ref is True
    assert entry.capabilities.max_refs >= 1
    assert entry.capabilities.supports_audio_toggle is True
    assert entry.capabilities.supports_audio_ref is True
    # Person-driven (KYC) supported on Avis Seedance 2.0; not on the byteplus path.
    assert entry.capabilities.supports_kyc is True




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
