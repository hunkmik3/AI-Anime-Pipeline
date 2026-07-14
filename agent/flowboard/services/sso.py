"""Google Workspace SSO (Phase 2) — OAuth2 authorization-code flow.

Stdlib + httpx only (no google-auth / PyJWT dep). Flow:

    /sso/google/start  → 302 to Google's consent screen (with a signed state)
    Google             → 302 back to /sso/google/callback?code=...&state=...
    callback           → exchange code for an id_token at Google's token
                         endpoint (server-to-server, authenticated with the
                         client secret over TLS), validate the claims, enforce
                         the allowed email domain, find-or-create the account,
                         mint an app token, and redirect to the SPA.

The id_token comes straight from Google's token endpoint over an authenticated
TLS channel, so we validate its claims (aud / iss / exp / email_verified /
domain) rather than re-verifying the JWKS signature — standard for the
confidential auth-code flow. State is a short-lived HMAC blob (no server
session needed) to stop CSRF.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from urllib.parse import urlencode

import httpx

from flowboard.services import auth

logger = logging.getLogger(__name__)

_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_VALID_ISS = {"accounts.google.com", "https://accounts.google.com"}
_STATE_TTL = 600  # 10 min

# Test seam (monkeypatch to an httpx.MockTransport client in unit tests).
_http_client_factory = lambda: httpx.AsyncClient(timeout=15.0)


class SSOError(RuntimeError):
    pass


def _client_id() -> str:
    return os.getenv("FLOWBOARD_SSO_GOOGLE_CLIENT_ID", "").strip()


def _client_secret() -> str:
    return os.getenv("FLOWBOARD_SSO_GOOGLE_CLIENT_SECRET", "").strip()


def _redirect_uri() -> str:
    return os.getenv("FLOWBOARD_SSO_REDIRECT_URI", "").strip()


def allowed_domains() -> list[str]:
    return [
        d.strip().lower().lstrip("@")
        for d in os.getenv("FLOWBOARD_SSO_ALLOWED_DOMAINS", "").split(",")
        if d.strip()
    ]


def is_configured() -> bool:
    return bool(_client_id() and _client_secret() and _redirect_uri())


def frontend_url() -> str:
    """Where to send the browser after SSO. Defaults to the redirect URI's
    origin (same-origin prod); override with FLOWBOARD_FRONTEND_URL for dev."""
    explicit = os.getenv("FLOWBOARD_FRONTEND_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    uri = _redirect_uri()
    if "://" in uri:
        scheme, rest = uri.split("://", 1)
        return f"{scheme}://{rest.split('/', 1)[0]}"
    return ""


# ── CSRF state (signed, no server session) ─────────────────────────────────


def sign_state() -> str:
    payload = auth._b64u(
        json.dumps({"n": os.urandom(8).hex(), "exp": int(time.time()) + _STATE_TTL}).encode()
    )
    sig = auth._b64u(hmac.new(auth._server_secret(), payload.encode("ascii"), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def verify_state(state: str) -> bool:
    try:
        payload, sig = state.split(".", 1)
        expected = auth._b64u(
            hmac.new(auth._server_secret(), payload.encode("ascii"), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(sig, expected):
            return False
        data = json.loads(auth._b64u_decode(payload))
        return int(data.get("exp", 0)) >= int(time.time())
    except (ValueError, AttributeError, TypeError, json.JSONDecodeError):
        return False


# ── OAuth flow ──────────────────────────────────────────────────────────────


def authorization_url(state: str) -> str:
    domains = allowed_domains()
    params = {
        "client_id": _client_id(),
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    if domains:
        params["hd"] = domains[0]  # domain hint (not a security control on its own)
    return f"{_AUTH_URL}?{urlencode(params)}"


def _decode_id_token(id_token: str) -> dict:
    """Base64url-decode the JWT payload (signature not re-verified — see module
    docstring) and enforce aud / iss / exp claims."""
    try:
        _header, payload_b64, _sig = id_token.split(".")
        claims = json.loads(auth._b64u_decode(payload_b64))
    except (ValueError, AttributeError, TypeError, json.JSONDecodeError) as exc:
        raise SSOError(f"malformed id_token: {exc}") from exc
    if claims.get("aud") != _client_id():
        raise SSOError("id_token audience mismatch")
    if claims.get("iss") not in _VALID_ISS:
        raise SSOError("id_token issuer mismatch")
    if int(claims.get("exp", 0)) < int(time.time()):
        raise SSOError("id_token expired")
    return claims


async def exchange_code(code: str) -> dict:
    """Exchange an auth code for the verified id_token claims."""
    data = {
        "code": code,
        "client_id": _client_id(),
        "client_secret": _client_secret(),
        "redirect_uri": _redirect_uri(),
        "grant_type": "authorization_code",
    }
    async with _http_client_factory() as client:
        resp = await client.post(_TOKEN_URL, data=data)
    if resp.status_code != 200:
        raise SSOError(f"token exchange failed (HTTP {resp.status_code})")
    body = resp.json()
    id_token = body.get("id_token")
    if not id_token:
        raise SSOError("no id_token in token response")
    return _decode_id_token(id_token)


def email_allowed(email: str, *, email_verified: bool) -> bool:
    if not email or not email_verified:
        return False
    domains = allowed_domains()
    if not domains:
        return False  # fail closed — never allow all domains by accident
    return email.split("@")[-1].lower() in domains
