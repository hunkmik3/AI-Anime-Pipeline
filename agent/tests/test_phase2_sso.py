"""Phase 2 — Google Workspace SSO: state CSRF, domain allow-list, id_token
claim validation, auto-provisioning, and the callback redirect."""
from __future__ import annotations

import json
import time

import httpx
import pytest

from flowboard.services import auth, sso, user_service


def _cfg(monkeypatch, domains="sleepygiant.studio"):
    monkeypatch.setenv("FLOWBOARD_SSO_GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("FLOWBOARD_SSO_GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv(
        "FLOWBOARD_SSO_REDIRECT_URI", "https://app.example/api/account/sso/google/callback"
    )
    monkeypatch.setenv("FLOWBOARD_SSO_ALLOWED_DOMAINS", domains)
    monkeypatch.setenv("FLOWBOARD_FRONTEND_URL", "https://app.example")


def _fake_id_token(claims: dict) -> str:
    return f"hdr.{auth._b64u(json.dumps(claims).encode())}.sig"


def _mock_google_token(monkeypatch, id_token: str):
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id_token": id_token})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        sso, "_http_client_factory", lambda: httpx.AsyncClient(transport=transport, timeout=5.0)
    )


def _claims(email="dev@sleepygiant.studio", verified=True):
    return {
        "aud": "cid.apps.googleusercontent.com",
        "iss": "https://accounts.google.com",
        "exp": int(time.time()) + 300,
        "email": email,
        "email_verified": verified,
        "name": "Dev",
    }


# ── unit ────────────────────────────────────────────────────────────────────


def test_state_roundtrip():
    st = sso.sign_state()
    assert sso.verify_state(st) is True
    assert sso.verify_state(st[:-2] + "xx") is False  # tampered sig
    assert sso.verify_state("garbage") is False


def test_email_allowed(monkeypatch):
    _cfg(monkeypatch)
    assert sso.email_allowed("a@sleepygiant.studio", email_verified=True) is True
    assert sso.email_allowed("a@evil.com", email_verified=True) is False       # wrong domain
    assert sso.email_allowed("a@sleepygiant.studio", email_verified=False) is False  # unverified


def test_email_allowed_fails_closed(monkeypatch):
    monkeypatch.delenv("FLOWBOARD_SSO_ALLOWED_DOMAINS", raising=False)
    assert sso.email_allowed("a@anything.com", email_verified=True) is False


def test_get_or_create_sso_user(client):
    u = user_service.get_or_create_sso_user("dev@sleepygiant.studio", "Dev")
    assert u.role == "user" and u.email == "dev@sleepygiant.studio"
    assert auth.verify_password("anything", u.password_hash) is False  # no usable password
    u2 = user_service.get_or_create_sso_user("dev@sleepygiant.studio")  # idempotent
    assert str(u2.id) == str(u.id)
    user_service.set_status(u.id, "suspended")
    with pytest.raises(user_service.UserError):
        user_service.get_or_create_sso_user("dev@sleepygiant.studio")  # suspended → refused


# ── callback flow ────────────────────────────────────────────────────────────


def test_sso_start_redirects_to_google(client, monkeypatch):
    _cfg(monkeypatch)
    r = client.get("/api/account/sso/google/start", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("https://accounts.google.com/o/oauth2/v2/auth")


def test_sso_callback_provisions_and_redirects_with_token(client, monkeypatch):
    _cfg(monkeypatch)
    _mock_google_token(monkeypatch, _fake_id_token(_claims("newbie@sleepygiant.studio")))
    r = client.get(
        f"/api/account/sso/google/callback?code=abc&state={sso.sign_state()}",
        follow_redirects=False,
    )
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith("https://app.example/login#") and "sso_token=" in loc
    token = loc.split("sso_token=")[1]
    u = user_service.authenticate_token(token)
    assert u is not None and u.email == "newbie@sleepygiant.studio"


def test_sso_callback_rejects_foreign_domain(client, monkeypatch):
    _cfg(monkeypatch)
    _mock_google_token(monkeypatch, _fake_id_token(_claims("intruder@evil.com")))
    r = client.get(
        f"/api/account/sso/google/callback?code=abc&state={sso.sign_state()}",
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "sso_error=domain_not_allowed" in r.headers["location"]


def test_sso_callback_rejects_bad_state(client, monkeypatch):
    _cfg(monkeypatch)
    r = client.get(
        "/api/account/sso/google/callback?code=abc&state=forged", follow_redirects=False
    )
    assert r.status_code == 302
    assert "sso_error=bad_state" in r.headers["location"]


def test_sso_404_when_unconfigured(client, monkeypatch):
    for v in (
        "FLOWBOARD_SSO_GOOGLE_CLIENT_ID",
        "FLOWBOARD_SSO_GOOGLE_CLIENT_SECRET",
        "FLOWBOARD_SSO_REDIRECT_URI",
    ):
        monkeypatch.delenv(v, raising=False)
    assert client.get("/api/account/sso/google/start", follow_redirects=False).status_code == 404
