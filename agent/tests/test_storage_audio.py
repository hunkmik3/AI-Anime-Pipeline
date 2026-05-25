"""Phase 7: audio MIME plumbing for Seedance 2.0 reference_audio.

The storage + media layers must recognise audio extensions so an uploaded
voice sample gets a correct Content-Type when mirrored to R2 and a correct
mime when served from the local cache.
"""
from __future__ import annotations

from flowboard.services.media import _EXT_BY_MIME, _mime_from_ext
from flowboard.services.storage.base import _CONTENT_TYPES, _guess_content_type


def test_storage_guesses_audio_content_types():
    assert _guess_content_type("mp3") == "audio/mpeg"
    assert _guess_content_type("wav") == "audio/wav"
    assert _guess_content_type("m4a") == "audio/mp4"
    # Case-insensitive (suffix may arrive upper-cased).
    assert _guess_content_type("MP3") == "audio/mpeg"


def test_storage_still_handles_images():
    assert _CONTENT_TYPES["png"] == "image/png"
    assert _guess_content_type("png") == "image/png"


def test_media_round_trips_audio_ext():
    assert _EXT_BY_MIME["audio/mpeg"] == ".mp3"
    assert _mime_from_ext(".mp3") == "audio/mpeg"
    assert _mime_from_ext(".wav") in ("audio/wav", "audio/x-wav")
