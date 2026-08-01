"""Phase 11.4 — the read-ahead disk cache behind video review.

Covers the parts that decide correctness without touching Google: range parsing
(a wrong answer here corrupts playback), the LRU cap (this is a cache, and must
never grow without bound), and the crash-safety check that stops a truncated
file from being served as if it were whole.
"""
from __future__ import annotations

import json
import time

import pytest

from flowboard.services import drive_cache as dc


@pytest.fixture()
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWBOARD_DRIVE_CACHE_DIR", str(tmp_path))
    dc._entries.clear()
    yield tmp_path
    dc._entries.clear()


def _put(cache_dir, file_id: str, size: int, *, complete: bool = True) -> None:
    (cache_dir / f"{file_id}.bin").write_bytes(b"\0" * size)
    (cache_dir / f"{file_id}.json").write_text(
        json.dumps({"file_id": file_id, "total": size, "mime": "video/mp4",
                    "complete": complete, "cached_at": time.time()})
    )


# ── range parsing ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "header,expected",
    [
        ("bytes=0-511", (0, 511)),
        ("bytes=100-", (100, 999)),        # open-ended: to the last byte
        ("bytes=-200", (800, 999)),        # suffix: the final 200 bytes
        ("bytes=0-99999", (0, 999)),       # clamped to the real end
        (None, None),
        ("", None),
        ("items=0-10", None),              # not a byte range
        ("bytes=abc", None),
    ],
)
def test_parse_range(header, expected):
    assert dc._parse_range(header, 1000) == expected


# ── completeness ───────────────────────────────────────────────────────────


def test_cached_complete_reports_a_whole_file(cache):
    _put(cache, "whole", 4096)
    meta = dc.cached_complete("whole")
    assert meta and meta["total"] == 4096


def test_truncated_file_is_not_served(cache):
    """A crash mid-write leaves the data short of its metadata. Serving that
    would hand the player a broken video, so it must read as absent."""
    _put(cache, "cut", 4096)
    (cache / "cut.bin").write_bytes(b"\0" * 10)
    assert dc.cached_complete("cut") is None


def test_incomplete_and_missing_are_absent(cache):
    _put(cache, "partial", 4096, complete=False)
    assert dc.cached_complete("partial") is None
    assert dc.cached_complete("never-seen") is None


# ── eviction ───────────────────────────────────────────────────────────────


def test_eviction_drops_least_recently_used_until_under_cap(cache, monkeypatch):
    monkeypatch.setattr(dc, "_MAX_BYTES", 3000)
    for name in ("old", "mid", "new"):
        _put(cache, name, 2000)
    now = time.time()
    for name, age in (("old", 3000), ("mid", 2000), ("new", 10)):
        import os
        os.utime(cache / f"{name}.bin", (now - age, now - age))

    assert dc.evict_if_needed() >= 3000  # 6000 stored, cap 3000
    assert not (cache / "old.bin").exists()
    assert (cache / "new.bin").exists()
    assert not (cache / "old.json").exists()  # metadata goes with the data


def test_eviction_is_a_noop_under_the_cap(cache, monkeypatch):
    monkeypatch.setattr(dc, "_MAX_BYTES", 10_000)
    _put(cache, "small", 1000)
    assert dc.evict_if_needed() == 0
    assert (cache / "small.bin").exists()


def test_in_flight_download_is_never_evicted(cache, monkeypatch):
    """Evicting a file that a viewer is streaming from would cut them off."""
    monkeypatch.setattr(dc, "_MAX_BYTES", 1)
    _put(cache, "live", 2000)
    entry = dc._Entry("live", 2000, "video/mp4")
    dc._entries["live"] = entry  # complete=False → still downloading
    dc.evict_if_needed()
    assert (cache / "live.bin").exists()


def test_cache_stats_reports_usage(cache):
    _put(cache, "a", 1000)
    _put(cache, "b", 2000)
    st = dc.cache_stats()
    assert st["files"] == 2
    assert st["used_bytes"] == 3000
    assert st["cap_gb"] == dc.CACHE_GB


# ── waiting on the download head ───────────────────────────────────────────


@pytest.mark.anyio
async def test_wait_returns_immediately_when_bytes_are_ready():
    entry = dc._Entry("f", 1000, "video/mp4")
    entry.done_bytes = 500
    assert await dc._wait_for(entry, 400) is True


@pytest.mark.anyio
async def test_wait_gives_up_on_a_seek_far_past_the_download_head(monkeypatch):
    """Scrubbing to the end of a 4K cut must not park the reviewer on a spinner
    waiting for the download to crawl there — the caller falls back to Drive."""
    monkeypatch.setattr(dc, "_WAIT_TIMEOUT_S", 2.0)
    entry = dc._Entry("f", 10**9, "video/mp4")
    entry.started_at = time.monotonic() - 1.0
    entry.done_bytes = 1_000_000  # ~1 MB/s → the end is ~1000s away

    t0 = time.monotonic()
    assert await dc._wait_for(entry, 10**9) is False
    assert time.monotonic() - t0 < 1.0  # gave up on the estimate, didn't sit out the timeout


@pytest.mark.anyio
async def test_wait_fails_fast_when_the_download_errored():
    entry = dc._Entry("f", 1000, "video/mp4")
    entry.error = "drive said no"
    assert await dc._wait_for(entry, 1) is False


# ── stale bookkeeping ──────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_entry_whose_data_vanished_is_rebuilt(cache, monkeypatch):
    """If the cached file is evicted or wiped while the process keeps its
    record, the next play must re-download — not stream an empty body."""
    started = []
    monkeypatch.setattr(dc, "_start_download", lambda e: started.append(e))

    stale = dc._Entry("gone", 4096, "video/mp4")
    stale.complete = True
    stale.done_bytes = 4096
    dc._entries["gone"] = stale  # data file was never written / was deleted

    entry = await dc.ensure_download("gone", 4096, "video/mp4")
    assert entry is not stale
    assert entry.complete is False
    assert started == [entry]  # a fresh download was kicked off


def test_eviction_forgets_the_entry_it_deleted(cache, monkeypatch):
    monkeypatch.setattr(dc, "_MAX_BYTES", 1)
    _put(cache, "doomed", 2000)
    done = dc._Entry("doomed", 2000, "video/mp4")
    done.complete = True
    dc._entries["doomed"] = done

    dc.evict_if_needed()
    assert not (cache / "doomed.bin").exists()
    assert "doomed" not in dc._entries
