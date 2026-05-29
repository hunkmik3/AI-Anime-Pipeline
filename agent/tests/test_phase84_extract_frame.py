"""Phase 8.4a — frame extraction from a video output.

POST /api/media/{media_id}/extract-frame: ffprobe validates the timestamp,
ffmpeg cuts one frame → a new visual_asset (kind=image) with source tracking
in asset_metadata. ffmpeg/ffprobe are mocked at the two thin subprocess
boundaries so the suite never spawns real binaries.
"""
from __future__ import annotations

import uuid

import pytest
from sqlmodel import select

from flowboard.db import get_session
from flowboard.db.models import Asset
from flowboard.services import frame_extract
from flowboard.services import media as media_service
from tests.conftest import make_shot


def _cache_fake_video() -> str:
    """Write a dummy cached video and return its media_id."""
    mid = uuid.uuid4().hex  # hex (no dashes) — valid media_id
    (media_service.MEDIA_CACHE_DIR / f"{mid}.mp4").write_bytes(b"\x00\x00fakevideo")
    return mid


@pytest.fixture
def mock_ffmpeg(monkeypatch):
    """Probe → fixed 5s/1280x720; extract → write a fake JPEG to the out path."""
    def fake_probe(_path):
        return {"duration": 5.0, "width": 1280, "height": 720}

    def fake_extract(_src, _time, out):
        out.write_bytes(b"\xff\xd8\xff\xe0fakejpegbytes")

    monkeypatch.setattr(frame_extract, "_probe_video", fake_probe)
    monkeypatch.setattr(frame_extract, "_run_ffmpeg_extract", fake_extract)


# ── happy path ────────────────────────────────────────────────────────────


def test_extract_frame_creates_visual_asset(client, mock_ffmpeg):
    b = make_shot(client)
    vid = _cache_fake_video()

    r = client.post(
        f"/api/media/{vid}/extract-frame",
        json={"time": 4.8, "shot_id": b["scene_id"]},  # any uuid str is fine
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert media_service.is_valid_media_id(out["media_id"])
    assert out["time"] == 4.8
    assert out["duration"] == 5.0
    assert out["width"] == 1280 and out["height"] == 720
    assert out["mime"] == "image/jpeg"

    # The extracted frame is served by the existing /media/{id} route.
    got = client.get(f"/media/{out['media_id']}")
    assert got.status_code == 200
    assert got.headers["content-type"].startswith("image/jpeg")


def test_extract_frame_metadata_source_tracking(client, mock_ffmpeg):
    b = make_shot(client)
    vid = _cache_fake_video()
    shot_id = b["id"]

    r = client.post(
        f"/api/media/{vid}/extract-frame",
        json={"time": 2.5, "shot_id": shot_id, "request_id": 7},
    )
    assert r.status_code == 200, r.text
    new_id = r.json()["media_id"]

    with get_session() as s:
        row = s.exec(select(Asset).where(Asset.uuid_media_id == new_id)).first()
    assert row is not None
    assert row.kind == "image"
    assert row.mime == "image/jpeg"
    meta = row.asset_metadata
    assert meta["source_type"] == "extracted_frame"
    assert meta["source_media_id"] == vid
    assert meta["source_time"] == 2.5
    assert meta["source_shot_id"] == str(shot_id)
    assert meta["source_request_id"] == 7


# ── validation / error paths ────────────────────────────────────────────────


def test_extract_frame_time_out_of_range_422(client, mock_ffmpeg):
    make_shot(client)
    vid = _cache_fake_video()
    r = client.post(f"/api/media/{vid}/extract-frame", json={"time": 99.0})
    assert r.status_code == 422, r.text
    assert "outside" in r.json()["detail"]


def test_extract_frame_negative_time_422(client, mock_ffmpeg):
    make_shot(client)
    vid = _cache_fake_video()
    # ge=0 on the body field → Pydantic rejects before the service runs.
    r = client.post(f"/api/media/{vid}/extract-frame", json={"time": -1.0})
    assert r.status_code == 422


def test_extract_frame_missing_source_404(client, mock_ffmpeg):
    make_shot(client)
    missing = uuid.uuid4().hex  # never written to cache
    r = client.post(f"/api/media/{missing}/extract-frame", json={"time": 1.0})
    assert r.status_code == 404


def test_extract_frame_invalid_media_id_400(client, mock_ffmpeg):
    make_shot(client)
    r = client.post("/api/media/not a valid id!/extract-frame", json={"time": 1.0})
    assert r.status_code in (400, 404)  # path may 404 at routing, else 400


def test_extract_frame_ffmpeg_missing_503(client, monkeypatch):
    make_shot(client)
    vid = _cache_fake_video()

    def boom(_path):
        raise frame_extract.FrameExtractError("ffmpeg_missing", "ffprobe not installed")

    monkeypatch.setattr(frame_extract, "_probe_video", boom)
    r = client.post(f"/api/media/{vid}/extract-frame", json={"time": 1.0})
    assert r.status_code == 503, r.text


def test_extract_frame_at_exact_duration_ok(client, mock_ffmpeg):
    """time == duration is the inclusive upper bound (the very last frame)."""
    make_shot(client)
    vid = _cache_fake_video()
    r = client.post(f"/api/media/{vid}/extract-frame", json={"time": 5.0})
    assert r.status_code == 200, r.text
