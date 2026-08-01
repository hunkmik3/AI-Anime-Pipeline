"""Google Drive access for reviewing submitted cuts (Phase 11.3).

Employees deliver an episode by pasting a Drive link. Making that playable used
to mean asking them to share the file broadly — a company file on a public link.
Instead the app holds its own Drive identity (a dedicated Workspace account, e.g.
``giantflow@sleepygiant.studio``) that the submissions folder is shared with, and
**proxies** the bytes to the reviewer:

    reviewer → Flowboard (checks project permission) → Drive (as the robot) → reviewer

So files stay ``Restricted`` in Drive, reviewers need no Google account at all,
and nothing is copied into our storage — we only ever keep the file id.

Auth is plain OAuth over REST (no Google SDK): a one-time consent produces a
refresh token, which we exchange for short-lived access tokens. Organisation
policy blocks service-account *keys* here, which is why this is a user-delegated
robot account rather than a service account.

Credentials (never committed — see .gitignore):
  agent/oauth-client.json   the Desktop-app OAuth client from Google Cloud
  agent/drive-token.json    the refresh token, written by scripts/drive_auth.py
Both paths are overridable with FLOWBOARD_DRIVE_CLIENT / FLOWBOARD_DRIVE_TOKEN.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import AsyncIterator, Optional

import httpx

logger = logging.getLogger(__name__)

# Read-only: the robot must never be able to change or delete studio files.
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
API_ROOT = "https://www.googleapis.com/drive/v3"

_AGENT_DIR = Path(__file__).resolve().parents[2]  # …/agent


def _client_path() -> Path:
    return Path(os.getenv("FLOWBOARD_DRIVE_CLIENT", _AGENT_DIR / "oauth-client.json"))


def _token_path() -> Path:
    return Path(os.getenv("FLOWBOARD_DRIVE_TOKEN", _AGENT_DIR / "drive-token.json"))


class DriveError(RuntimeError):
    """Drive is unreachable, unconfigured, or refused the file."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ── credentials ─────────────────────────────────────────────────────────────


def load_client() -> dict:
    p = _client_path()
    if not p.exists():
        raise DriveError(
            "not_configured",
            f"missing {p.name} — create a Desktop-app OAuth client in Google Cloud "
            "and save it there",
        )
    data = json.loads(p.read_text())
    cfg = data.get("installed") or data.get("web")
    if not cfg or not cfg.get("client_id"):
        raise DriveError("not_configured", f"{p.name} is not a valid OAuth client file")
    return cfg


def load_token() -> Optional[dict]:
    p = _token_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (ValueError, OSError):
        return None


def save_token(data: dict) -> None:
    p = _token_path()
    p.write_text(json.dumps(data, indent=2))
    try:
        p.chmod(0o600)  # the refresh token is a long-lived credential
    except OSError:
        pass


def is_configured() -> bool:
    """True when the app can act on Drive (client + refresh token present)."""
    try:
        load_client()
    except DriveError:
        return False
    tok = load_token()
    return bool(tok and tok.get("refresh_token"))


# ── access tokens ───────────────────────────────────────────────────────────

_lock = threading.Lock()
_cached: dict = {"access_token": None, "expires_at": 0.0}


async def access_token_async(force: bool = False) -> str:
    """``access_token`` for async callers.

    The refresh is a blocking HTTP call; awaiting it in a thread keeps the event
    loop free. Without this, the once-an-hour refresh stalls *every* in-flight
    video stream while it runs.
    """
    import anyio

    return await anyio.to_thread.run_sync(lambda: access_token(force))


def access_token(force: bool = False) -> str:
    """A valid access token, refreshed on demand.

    Cached in-process with a 60s safety margin so a burst of range requests
    (video seeking fires many) doesn't hammer Google's token endpoint.
    """
    with _lock:
        now = time.time()
        if not force and _cached["access_token"] and _cached["expires_at"] > now + 60:
            return _cached["access_token"]

        cfg = load_client()
        tok = load_token()
        if not tok or not tok.get("refresh_token"):
            raise DriveError(
                "not_authorized",
                "Drive is not authorized yet — run scripts/drive_auth.py and sign in "
                "as the robot account",
            )
        resp = httpx.post(
            TOKEN_URL,
            data={
                "client_id": cfg["client_id"],
                "client_secret": cfg.get("client_secret", ""),
                "refresh_token": tok["refresh_token"],
                "grant_type": "refresh_token",
            },
            timeout=20.0,
        )
        if resp.status_code != 200:
            # A revoked/expired refresh token needs a human to re-consent; say so
            # plainly instead of failing every playback with a generic 500.
            raise DriveError(
                "not_authorized",
                f"Drive refresh failed ({resp.status_code}) — re-run scripts/drive_auth.py",
            )
        data = resp.json()
        _cached["access_token"] = data["access_token"]
        _cached["expires_at"] = now + float(data.get("expires_in", 3600))
        return _cached["access_token"]


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {access_token()}"}


# ── file access ─────────────────────────────────────────────────────────────


def get_metadata(file_id: str) -> dict:
    """Name / mime / size for a file the robot can see.

    Raises ``DriveError('no_access')`` when Drive says 404/403 — which in practice
    means the submitter's folder was never shared with the robot account. That is
    the message the UI shows at submit time, so the mistake surfaces immediately
    rather than when a reviewer tries to watch.
    """
    resp = httpx.get(
        f"{API_ROOT}/files/{file_id}",
        params={
            "fields": "id,name,mimeType,size,owners(emailAddress)",
            # The studio keeps submissions on a Shared drive; without this the
            # API pretends those files don't exist (404).
            "supportsAllDrives": "true",
        },
        headers=_auth_headers(),
        timeout=20.0,
    )
    if resp.status_code in (403, 404):
        raise DriveError(
            "no_access",
            "the app can't open this file — make sure it sits in the shared "
            "submissions folder on Drive",
        )
    if resp.status_code != 200:
        raise DriveError("upstream", f"Drive error {resp.status_code}")
    return resp.json()


async def aclose_pool() -> None:
    """Kept for the lifespan handler; there is no shared pool to close.

    A shared ``AsyncClient`` was tried here and had to be reverted: a video
    player opens a Range request per seek and abandons the ones it no longer
    needs, so connections were never handed back and the pool wedged — every
    later stream then hung. Streams are long-lived and frequently orphaned,
    which is exactly the workload connection pooling is bad at.
    """
    return None


async def stream_file(
    file_id: str, *, range_header: Optional[str] = None
) -> tuple[int, dict, AsyncIterator[bytes]]:
    """Open a byte stream for a Drive file, forwarding the browser's Range header.

    Returning the upstream status + headers (rather than buffering) is what lets
    the reviewer scrub the timeline: Drive answers 206 with Content-Range and the
    player seeks natively. Nothing is written to disk.

    Each call gets its own client so an abandoned seek can only ever leak its
    own connection — never block the next viewer (see ``aclose_pool``).
    """
    headers = {"Authorization": f"Bearer {await access_token_async()}"}
    if range_header:
        headers["Range"] = range_header

    client = httpx.AsyncClient(
        # read=None: a slow Drive read must not kill a legitimate long stream.
        timeout=httpx.Timeout(connect=15.0, read=None, write=30.0, pool=10.0),
        follow_redirects=True,
    )
    req = client.build_request(
        "GET",
        f"{API_ROOT}/files/{file_id}",
        params={"alt": "media", "supportsAllDrives": "true"},
        headers=headers,
    )
    try:
        resp = await client.send(req, stream=True)
    except Exception:
        await client.aclose()
        raise

    if resp.status_code >= 400:
        code = resp.status_code
        await resp.aclose()
        await client.aclose()
        if code in (403, 404):
            raise DriveError("no_access", "the app can't open this file on Drive")
        raise DriveError("upstream", f"Drive error {code}")

    passthrough = {
        k: v
        for k, v in resp.headers.items()
        if k.lower() in ("content-type", "content-length", "content-range", "accept-ranges")
    }
    passthrough.setdefault("accept-ranges", "bytes")

    async def body() -> AsyncIterator[bytes]:
        # 1 MB chunks: Drive sustains ~5 MB/s, so smaller reads just add
        # await churn without getting bytes to the player any sooner.
        try:
            async for chunk in resp.aiter_bytes(chunk_size=1024 * 1024):
                yield chunk
        finally:
            # Runs on normal end AND when the player abandons the seek (Starlette
            # closes the generator), so nothing is left dangling.
            await resp.aclose()
            await client.aclose()

    return resp.status_code, passthrough, body()
