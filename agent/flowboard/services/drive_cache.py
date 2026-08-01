"""Read-ahead disk cache for Drive video review (Phase 11.4).

Playing a Drive file straight through the proxy stutters, and the measurements
say why:

    one large sequential read from Drive : ~16 MB/s
    many small Range requests           : ~0.3 MB/s  (≈2 s latency *per request*)
    a 4K review cut needs               :  ~1.9 MB/s to play in real time

Drive is not slow — it charges a fixed toll per request, and a video player asks
in small pieces. So instead of forwarding each of those pieces, the first play
kicks off **one sequential download** into a local file and the player is served
from that. Download outruns playback ~8x, so after a second or two the file is
always ahead of the playhead; the second viewing is entirely local and seeks
instantly — the YouTube feel.

Bounded on purpose: this is a cache, not storage. Total size is capped
(``FLOWBOARD_DRIVE_CACHE_GB``, default 20 GB) and the least-recently-used files
are evicted, so it can never grow without limit.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from functools import partial
from pathlib import Path
from typing import AsyncIterator, Optional

from flowboard.services import drive

logger = logging.getLogger(__name__)

CACHE_GB = float(os.getenv("FLOWBOARD_DRIVE_CACHE_GB", "20"))
_MAX_BYTES = int(CACHE_GB * 1024**3)

# How long a request will wait for the downloader to reach the bytes it needs
# before giving up and fetching that range straight from Drive.
_WAIT_TIMEOUT_S = float(os.getenv("FLOWBOARD_DRIVE_CACHE_WAIT_S", "20"))
# How long to wait before the download has produced anything to estimate from.
# Short on purpose: the very first play must not sit behind Drive's startup.
_GRACE_S = float(os.getenv("FLOWBOARD_DRIVE_CACHE_GRACE_S", "1.5"))

# Drive occasionally stalls mid-transfer, especially while player fallbacks are
# reading the same file. Cut a dead read loose reasonably fast and resume it
# rather than waiting out a long timeout.
_READ_TIMEOUT_S = float(os.getenv("FLOWBOARD_DRIVE_CACHE_READ_TIMEOUT_S", "45"))
_MAX_STRIKES = 5  # consecutive failures with NO progress before giving up
_RETRY_BACKOFF_S = 1.0
_POLL_S = 0.15


def cache_dir() -> Path:
    base = os.getenv("FLOWBOARD_DRIVE_CACHE_DIR")
    if base:
        d = Path(base)
    else:
        storage = os.getenv("FLOWBOARD_STORAGE")
        d = Path(storage) / "drive-cache" if storage else Path.home() / ".flowboard" / "drive-cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _data_path(file_id: str) -> Path:
    return cache_dir() / f"{file_id}.bin"


def _meta_path(file_id: str) -> Path:
    return cache_dir() / f"{file_id}.json"


# ── per-file download state ─────────────────────────────────────────────────


class _Entry:
    """Tracks one file's background download.

    ``done_bytes`` is the count of contiguous bytes written from the start, so a
    reader can answer "is offset N ready?" without touching the filesystem.
    """

    def __init__(self, file_id: str, total: int, mime: str) -> None:
        self.file_id = file_id
        self.total = total
        self.mime = mime
        self.done_bytes = 0
        self.started_at = time.monotonic()
        self.complete = False
        self.error: Optional[str] = None
        self.thread: Optional[threading.Thread] = None


_entries: dict[str, _Entry] = {}
_lock = asyncio.Lock()


def _write_meta(entry: _Entry) -> None:
    try:
        _meta_path(entry.file_id).write_text(
            json.dumps(
                {
                    "file_id": entry.file_id,
                    "total": entry.total,
                    "mime": entry.mime,
                    "complete": entry.complete,
                    "cached_at": time.time(),
                }
            )
        )
    except OSError:
        pass


def _read_meta(file_id: str) -> Optional[dict]:
    try:
        return json.loads(_meta_path(file_id).read_text())
    except (OSError, ValueError):
        return None


def cached_complete(file_id: str) -> Optional[dict]:
    """Metadata for a fully-cached file, or None. Also touches the file so LRU
    eviction keeps what people actually watch."""
    meta = _read_meta(file_id)
    data = _data_path(file_id)
    if not meta or not meta.get("complete") or not data.exists():
        return None
    if data.stat().st_size != int(meta.get("total", -1)):
        return None  # truncated (crash mid-write) — treat as absent
    try:
        os.utime(data, None)
    except OSError:
        pass
    return meta


# ── eviction ────────────────────────────────────────────────────────────────


def evict_if_needed() -> int:
    """Drop least-recently-used complete files until under the cap. Returns the
    number of bytes freed. Partial downloads are never evicted — something is
    reading them."""
    d = cache_dir()
    files = []
    total = 0
    for p in d.glob("*.bin"):
        try:
            st = p.stat()
        except OSError:
            continue
        files.append((st.st_atime, st.st_size, p))
        total += st.st_size
    if total <= _MAX_BYTES:
        return 0

    files.sort()  # oldest access first
    freed = 0
    for _atime, size, p in files:
        if total - freed <= _MAX_BYTES:
            break
        fid = p.stem
        if fid in _entries and not _entries[fid].complete:
            continue  # in-flight
        try:
            p.unlink()
            _meta_path(fid).unlink(missing_ok=True)
            # Drop the in-memory record too, or the next request would trust it
            # and stream from a file that no longer exists.
            _entries.pop(fid, None)
            freed += size
            logger.info("drive-cache: evicted %s (%.0f MB)", fid, size / 1024 / 1024)
        except OSError:
            pass
    return freed


def cache_stats() -> dict:
    d = cache_dir()
    files = [p for p in d.glob("*.bin")]
    used = sum(p.stat().st_size for p in files if p.exists())
    return {
        "files": len(files),
        "used_bytes": used,
        "used_gb": round(used / 1024**3, 2),
        "cap_gb": CACHE_GB,
    }


# ── the downloader ──────────────────────────────────────────────────────────


def _fetch_from(entry: _Entry, tmp: Path) -> None:
    """Append to ``tmp`` from ``entry.done_bytes`` until the stream ends."""
    import httpx

    headers = {"Authorization": f"Bearer {drive.access_token()}"}
    if entry.done_bytes:
        headers["Range"] = f"bytes={entry.done_bytes}-"

    with httpx.stream(
        "GET",
        f"{drive.API_ROOT}/files/{entry.file_id}",
        params={"alt": "media", "supportsAllDrives": "true"},
        headers=headers,
        timeout=httpx.Timeout(connect=15.0, read=_READ_TIMEOUT_S, write=30.0, pool=10.0),
        follow_redirects=True,
    ) as resp:
        if resp.status_code not in (200, 206):
            raise drive.DriveError("upstream", f"unexpected status {resp.status_code}")
        # A resume that comes back 200 means Drive ignored the Range and is
        # replaying from zero; appending that would corrupt the file.
        mode = "ab" if (entry.done_bytes and resp.status_code == 206) else "wb"
        if mode == "wb":
            entry.done_bytes = 0
        with open(tmp, mode) as fh:
            for chunk in resp.iter_bytes(1024 * 1024):
                fh.write(chunk)
                fh.flush()  # readers tail this file while it grows
                entry.done_bytes += len(chunk)


def _download_blocking(entry: _Entry) -> None:
    """Fill the cache with sequential reads — the only access pattern Drive is
    fast at — resuming wherever a read gives out.

    Deliberately synchronous and run on a dedicated thread. An earlier async
    version starved: sibling requests doing blocking work pinned the event loop
    and this task never got scheduled, so the download sat at 0 bytes while
    every player request timed out waiting for it.

    Resuming is not a nicety. A 288 MB pull over the public internet *will* be
    interrupted, and an earlier version threw away everything it had on the first
    stall — which put the reviewer back on the slow path indefinitely, because
    each retry started from zero and stalled again. Now a broken read costs only
    the bytes still outstanding, and the retry budget resets whenever progress is
    made, so only a genuinely stuck download gives up.
    """
    tmp = _data_path(entry.file_id).with_suffix(".part")
    tmp.unlink(missing_ok=True)
    strikes = 0
    while entry.done_bytes < entry.total:
        mark = entry.done_bytes
        try:
            _fetch_from(entry, tmp)
        except Exception as exc:  # noqa: BLE001 - retried, or surfaced below
            if entry.done_bytes > mark:
                strikes = 0  # made progress; the stall was transient
            else:
                strikes += 1
                if strikes >= _MAX_STRIKES:
                    entry.error = str(exc)
                    logger.warning(
                        "drive-cache: giving up on %s at %.0f/%.0f MB: %s",
                        entry.file_id, entry.done_bytes / 1024 / 1024,
                        entry.total / 1024 / 1024, exc,
                    )
                    tmp.unlink(missing_ok=True)
                    return
                time.sleep(_RETRY_BACKOFF_S * strikes)
            logger.info(
                "drive-cache: resuming %s at %.0f MB (%s)",
                entry.file_id, entry.done_bytes / 1024 / 1024, exc,
            )
            continue
        if entry.done_bytes < entry.total:
            # Clean EOF short of the end — also a resumable interruption.
            strikes = 0 if entry.done_bytes > mark else strikes + 1
            if strikes >= _MAX_STRIKES:
                entry.error = "stream ended early"
                tmp.unlink(missing_ok=True)
                return

    # Windows refuses to rename a file that another handle has open, and a
    # viewer may be reading the .part right now. Those readers hold it only for
    # a chunk at a time, so a few retries clear it; POSIX never gets here twice.
    for attempt in range(10):
        try:
            tmp.replace(_data_path(entry.file_id))
            break
        except OSError as exc:
            if attempt == 9:
                entry.error = str(exc)
                logger.warning(
                    "drive-cache: could not finalise %s: %s", entry.file_id, exc
                )
                return
            time.sleep(0.2)
    entry.complete = True
    _write_meta(entry)

    secs = max(time.monotonic() - entry.started_at, 0.001)
    mb = entry.done_bytes / 1024 / 1024
    logger.info(
        "drive-cache: cached %s (%.0f MB in %.0fs, %.1f MB/s)",
        entry.file_id, mb, secs, mb / secs,
    )
    evict_if_needed()


def _start_download(entry: _Entry) -> None:
    """Run the download on a dedicated daemon thread.

    Not ``anyio.to_thread``: that pool is shared with every sync route handler,
    and a download that runs for minutes would hold one of its slots the whole
    time. A plain thread also means the download is never subject to request
    scheduling — it just runs.
    """

    def run() -> None:
        try:
            _download_blocking(entry)
        finally:
            if not entry.complete:
                _entries.pop(entry.file_id, None)

    th = threading.Thread(target=run, name=f"drive-cache-{entry.file_id[:8]}", daemon=True)
    entry.thread = th
    th.start()


async def ensure_download(file_id: str, total: int, mime: str) -> _Entry:
    """Start (or join) the background download for a file."""
    async with _lock:
        entry = _entries.get(file_id)
        # A finished entry whose data is gone (evicted, or the cache directory
        # was cleaned underneath us) is stale: keeping it would make `serve`
        # believe the bytes are on disk and hand the player an empty body.
        if entry is not None and entry.complete and not _data_path(file_id).exists():
            entry = None
        if entry is None:
            entry = _Entry(file_id, total, mime)
            _entries[file_id] = entry
            _start_download(entry)
        return entry


async def _wait_for(entry: _Entry, upto: int) -> bool:
    """Wait until ``upto`` bytes are on disk. False if it won't arrive in time
    (a seek far ahead) or the download failed — the caller then goes to Drive.

    The wait is budgeted from the download's *measured* rate rather than a flat
    timeout. That distinction is the whole cold-start experience: when the reader
    has merely caught up with a downloader running a second behind, waiting is
    right; when the cache is still cold or the reviewer jumped to the end of the
    timeline, waiting is just dead air, so we bail and let Drive serve that range
    directly while the read-ahead keeps warming underneath.
    """
    deadline = time.monotonic() + _GRACE_S  # extended below once a rate is known
    while True:
        if entry.error:
            return False
        if entry.complete or entry.done_bytes >= upto:
            return True

        elapsed = time.monotonic() - entry.started_at
        if entry.done_bytes and elapsed > 0:
            rate = entry.done_bytes / elapsed
            eta = (upto - entry.done_bytes) / rate
            if eta > _WAIT_TIMEOUT_S:
                return False  # far ahead of the download head — go to Drive
            deadline = max(deadline, time.monotonic() + min(eta * 1.5, _WAIT_TIMEOUT_S))

        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(_POLL_S)


# ── serving ─────────────────────────────────────────────────────────────────


def _parse_range(header: Optional[str], total: int) -> Optional[tuple[int, int]]:
    """`bytes=start-end` → inclusive (start, end). None when absent/unsupported."""
    if not header or not header.startswith("bytes="):
        return None
    spec = header[6:].split(",")[0].strip()
    try:
        if spec.startswith("-"):  # suffix range: last N bytes
            n = int(spec[1:])
            return max(0, total - n), total - 1
        start_s, _, end_s = spec.partition("-")
        start = int(start_s)
        end = int(end_s) if end_s else total - 1
        return start, min(end, total - 1)
    except ValueError:
        return None


def _read_at(path: Path, offset: int, size: int) -> bytes:
    with open(path, "rb") as fh:
        fh.seek(offset)
        return fh.read(size)


async def _file_slice(path: Path, start: int, end: int) -> AsyncIterator[bytes]:
    """Stream a byte range off the local file (reads on a thread, so a big
    sequential read never blocks other viewers)."""
    import anyio

    pos, remaining = start, end - start + 1
    while remaining > 0:
        want = min(1024 * 1024, remaining)
        chunk = await anyio.to_thread.run_sync(partial(_read_at, path, pos, want))
        if not chunk:
            break
        pos += len(chunk)
        remaining -= len(chunk)
        yield chunk


async def _partial_slice(entry: _Entry, start: int, end: int) -> AsyncIterator[bytes]:
    """Stream from the still-growing .part file, waiting for the downloader when
    the reader catches up with it."""
    part = _data_path(entry.file_id).with_suffix(".part")
    final = _data_path(entry.file_id)
    pos, remaining = start, end - start + 1
    while remaining > 0:
        path = final if entry.complete and final.exists() else part
        if not path.exists():
            return
        avail = (entry.total if entry.complete else entry.done_bytes) - pos
        if avail <= 0:
            if entry.complete or entry.error:
                return
            await asyncio.sleep(_POLL_S)
            continue
        want = min(1024 * 1024, remaining, avail)
        import anyio

        chunk = await anyio.to_thread.run_sync(partial(_read_at, path, pos, want))
        if not chunk:
            await asyncio.sleep(_POLL_S)
            continue
        pos += len(chunk)
        remaining -= len(chunk)
        yield chunk


async def serve(
    file_id: str, *, range_header: Optional[str] = None
) -> tuple[int, dict, AsyncIterator[bytes]]:
    """Answer a player's range request, using the cache when it can.

    Three paths, in order of preference:
      1. **fully cached** → straight off disk, instant seeks anywhere
      2. **downloading, bytes ready (or nearly)** → off the partial file
      3. **cold, or a seek far past the download head** → straight from Drive,
         so scrubbing never blocks on the cache warming up
    """
    meta = cached_complete(file_id)
    if meta:
        total, mime = int(meta["total"]), meta.get("mime") or "video/mp4"
        rng = _parse_range(range_header, total)
        path = _data_path(file_id)
        if rng is None:
            headers = {
                "content-type": mime,
                "content-length": str(total),
                "accept-ranges": "bytes",
            }
            return 200, headers, _file_slice(path, 0, total - 1)
        start, end = rng
        headers = {
            "content-type": mime,
            "content-length": str(end - start + 1),
            "content-range": f"bytes {start}-{end}/{total}",
            "accept-ranges": "bytes",
        }
        return 206, headers, _file_slice(path, start, end)

    # Not cached yet — learn the size, then start filling the cache.
    # (blocking HTTP → thread, or it stalls every other stream)
    import anyio

    info = await anyio.to_thread.run_sync(partial(drive.get_metadata, file_id))
    total = int(info.get("size") or 0)
    mime = info.get("mimeType") or "video/mp4"
    if total <= 0:  # Google Docs and friends have no byte size
        return await drive.stream_file(file_id, range_header=range_header)

    entry = await ensure_download(file_id, total, mime)

    rng = _parse_range(range_header, total)
    start, end = rng if rng else (0, total - 1)
    status = 206 if rng else 200
    headers = {
        "content-type": mime,
        "content-length": str(end - start + 1),
        "accept-ranges": "bytes",
    }
    if rng:
        headers["content-range"] = f"bytes {start}-{end}/{total}"

    # Can the cache reach these bytes soon? If not (a jump to the end of the
    # timeline), don't make the reviewer wait on the download — go to Drive.
    #
    # The test is the range's END, not its start. Once we answer from the
    # partial file we are committed: headers are sent and there is no switching
    # to Drive mid-body, so `_partial_slice` can only sit and wait for whatever
    # is still missing. Checking the start byte alone let a cold, slow download
    # pass that check and then stall the request for the rest of the chunk.
    if await _wait_for(entry, min(end + 1, total)):
        return status, headers, _partial_slice(entry, start, end)
    return await drive.stream_file(file_id, range_header=range_header)
