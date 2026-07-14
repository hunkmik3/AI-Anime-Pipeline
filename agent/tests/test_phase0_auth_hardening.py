"""Phase 0 security hardening — token revocation, lockout, rehash, middleware.

Closes the offboarding hole (suspend/delete/password-change must invalidate
outstanding tokens on the very next request) + brute-force lockout + hash
upgrade + baseline security headers.
"""
from __future__ import annotations

import hashlib

from flowboard.db import get_session
from flowboard.db.models import User
from flowboard.services import auth, user_service


def _login(client, username, password):
    return client.post("/api/account/login", json={"username": username, "password": password})


def _auth_h(client, username, password):
    return {"Authorization": f"Bearer {_login(client, username, password).json()['token']}"}


def _old_pbkdf2(pw: str, iters: int = 200_000) -> str:
    salt = b"\x11" * 16
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, iters)
    return f"pbkdf2_sha256${iters}${salt.hex()}${dk.hex()}"


# ── auth primitives ──────────────────────────────────────────────────────


def test_needs_rehash():
    assert auth.needs_rehash(_old_pbkdf2("x", 200_000)) is True       # fewer iters
    assert auth.needs_rehash("scrypt$whatever") is True               # foreign scheme
    assert auth.needs_rehash(auth.hash_password("x")) is False        # current cost


def test_token_carries_and_verifies_tv():
    t = auth.make_token("u-1", 7, ttl_seconds=60)
    data = auth.decode_token(t)
    assert data == {"uid": "u-1", "tv": 7, "exp": data["exp"]}
    assert auth.verify_token(t) == "u-1"                              # back-compat
    assert auth.decode_token(t[:-2] + "xx") is None                   # tampered


# ── token revocation (the offboarding fix) ────────────────────────────────


def test_suspend_revokes_outstanding_token(client):
    u = user_service.create_user("alice", "pw123456")
    token = _login(client, "alice", "pw123456").json()["token"]
    assert client.get("/api/account/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200

    user_service.set_status(u.id, "suspended")
    # Same still-unexpired token is now dead (status + bumped token_version).
    assert client.get("/api/account/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401

    # Re-activating does NOT resurrect the old token (its tv is stale).
    user_service.set_status(u.id, "active")
    assert client.get("/api/account/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    # A fresh login works again.
    assert _login(client, "alice", "pw123456").status_code == 200


def test_password_change_revokes_other_sessions(client):
    u = user_service.create_user("bob", "pw123456")
    t1 = _login(client, "bob", "pw123456").json()["token"]
    assert client.get("/api/account/me", headers={"Authorization": f"Bearer {t1}"}).status_code == 200

    user_service.set_password(u.id, "newpw999")          # admin reset → tv bump
    assert client.get("/api/account/me", headers={"Authorization": f"Bearer {t1}"}).status_code == 401
    assert _login(client, "bob", "newpw999").status_code == 200


def test_authenticate_token_enforces_tv():
    u = user_service.create_user("carol", "pw123456")
    stale = auth.make_token(str(u.id), 0)               # tv 0
    assert user_service.authenticate_token(stale) is not None
    user_service.bump_token_version(u.id)               # tv → 1
    assert user_service.authenticate_token(stale) is None  # stale tv rejected


# ── brute-force lockout ────────────────────────────────────────────────────


def test_login_lockout_after_repeated_failures(client):
    user_service.create_user("dave", "pw123456")
    for _ in range(5):                                   # default threshold = 5
        assert _login(client, "dave", "wrong").status_code == 401
    # Now locked — even the CORRECT password is refused with 429.
    assert _login(client, "dave", "pw123456").status_code == 429


# ── transparent hash upgrade on login ──────────────────────────────────────


def test_rehash_on_login_upgrades_weak_hash(client):
    u = user_service.create_user("erin", "pw123456")
    with get_session() as s:
        row = s.get(User, u.id)
        row.password_hash = _old_pbkdf2("pw123456", 200_000)   # simulate legacy hash
        s.add(row)
        s.commit()
    assert _login(client, "erin", "pw123456").status_code == 200
    with get_session() as s:
        iters = int(s.get(User, u.id).password_hash.split("$")[1])
    assert iters == 600_000                                    # upgraded


# ── middleware enforces auth on ALL routes when REQUIRE_AUTH is on ──────────


def test_middleware_blocks_suspended_on_data_route(client, monkeypatch):
    import flowboard.main as main

    monkeypatch.setattr(main, "REQUIRE_AUTH", True)
    u = user_service.create_user("frank", "pw123456")
    h = _auth_h(client, "frank", "pw123456")

    # A normal data route (not admin, not /me) — works with a valid token.
    assert client.get("/api/projects", headers=h).status_code == 200
    # No token at all → the middleware 401s (auth now required everywhere).
    assert client.get("/api/projects").status_code == 401

    # Suspend → the SAME token is rejected by the middleware on this data route
    # (previously it degraded to unscoped/anonymous and kept working).
    user_service.set_status(u.id, "suspended")
    assert client.get("/api/projects", headers=h).status_code == 401


# ── security headers ────────────────────────────────────────────────────────


def test_security_headers_present(client):
    r = client.get("/api/health")
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert "max-age=" in (r.headers.get("Strict-Transport-Security") or "")
