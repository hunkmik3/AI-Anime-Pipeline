#!/usr/bin/env python3
"""One-time Drive authorisation for the Flowboard robot account.

Opens Google's consent screen, catches the redirect on localhost, and stores the
refresh token in ``agent/drive-token.json``. Run it once per machine (or after
the token is revoked):

    agent/.venv/bin/python agent/scripts/drive_auth.py

**Sign in as the robot account** (e.g. giantflow@sleepygiant.studio) — whatever
account you pick here is the identity the app uses to read submitted cuts, so it
must be the one the submissions folder is shared with.

Requests read-only scope: the app can never modify or delete studio files.
"""
from __future__ import annotations

import http.server
import secrets
import socket
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from flowboard.services import drive  # noqa: E402

_result: dict = {}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _result.update({k: v[0] for k, v in q.items()})
        ok = "code" in _result
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        msg = (
            "<h2>Flowboard is authorised ✅</h2><p>You can close this tab.</p>"
            if ok
            else f"<h2>Authorisation failed</h2><pre>{_result}</pre>"
        )
        self.wfile.write(f"<html><body style='font-family:sans-serif'>{msg}</body></html>".encode())

    def log_message(self, *args):  # keep the console clean
        pass


def main() -> int:
    cfg = drive.load_client()
    port = _free_port()
    redirect_uri = f"http://localhost:{port}"
    state = secrets.token_urlsafe(16)

    params = {
        "client_id": cfg["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(drive.SCOPES),
        # offline + consent so Google actually returns a refresh_token (it omits
        # one on re-authorisation otherwise).
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    url = f"{drive.AUTH_URL}?{urllib.parse.urlencode(params)}"

    server = http.server.HTTPServer(("127.0.0.1", port), _Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()

    print("\nA browser window is opening.")
    print("👉 Sign in as the ROBOT account the submissions folder is shared with.")
    print(f"\nIf nothing opens, paste this into a browser:\n{url}\n")
    webbrowser.open(url)

    # handle_request serves exactly one hit, so joining the thread waits for it.
    for t in threading.enumerate():
        if t is not threading.current_thread() and t.daemon:
            t.join(timeout=300)

    if _result.get("state") != state:
        print("✗ state mismatch — aborting (possible CSRF).")
        return 2
    if "code" not in _result:
        print(f"✗ no authorisation code returned: {_result}")
        return 2

    resp = httpx.post(
        drive.TOKEN_URL,
        data={
            "client_id": cfg["client_id"],
            "client_secret": cfg.get("client_secret", ""),
            "code": _result["code"],
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        timeout=30.0,
    )
    if resp.status_code != 200:
        print(f"✗ token exchange failed ({resp.status_code}): {resp.text[:300]}")
        return 2

    data = resp.json()
    if not data.get("refresh_token"):
        print("✗ Google did not return a refresh_token. Revoke the app at")
        print("  https://myaccount.google.com/permissions and run this again.")
        return 2

    drive.save_token({"refresh_token": data["refresh_token"], "scope": data.get("scope")})
    print(f"\n✓ Saved refresh token → {drive._token_path()}")

    # Prove it works end to end rather than declaring success on a stored file.
    try:
        drive.access_token(force=True)
        print("✓ Access token refresh works")
    except drive.DriveError as exc:
        print(f"✗ token refresh failed: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
