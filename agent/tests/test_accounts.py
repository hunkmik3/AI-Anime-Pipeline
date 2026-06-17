"""Phase 9 multi-user: account auth, login, admin gating, project isolation."""
from __future__ import annotations

from flowboard.services import auth, user_service


# ── auth primitives ──────────────────────────────────────────────────────


def test_password_hash_roundtrip():
    h = auth.hash_password("s3cret!")
    assert h.startswith("pbkdf2_sha256$")
    assert auth.verify_password("s3cret!", h) is True
    assert auth.verify_password("wrong", h) is False


def test_token_roundtrip_and_expiry():
    t = auth.make_token("user-123", ttl_seconds=60)
    assert auth.verify_token(t) == "user-123"
    assert auth.verify_token(t[:-2] + "xx") is None       # tampered sig
    assert auth.verify_token(auth.make_token("u", ttl_seconds=-1)) is None  # expired


# ── login ────────────────────────────────────────────────────────────────


def _login(client, username, password):
    return client.post("/api/account/login", json={"username": username, "password": password})


def test_login_and_me(client):
    user_service.create_user("alice", "pw12345", role="user")
    r = _login(client, "alice", "pw12345")
    assert r.status_code == 200
    token = r.json()["token"]
    me = client.get("/api/account/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["username"] == "alice"
    assert _login(client, "alice", "nope").status_code == 401          # wrong pw
    assert client.get("/api/account/me").status_code == 401            # no token


def test_suspended_account_cannot_login(client):
    u = user_service.create_user("bob", "pw12345")
    user_service.set_status(u.id, "suspended")
    assert _login(client, "bob", "pw12345").status_code == 401


# ── admin gating ─────────────────────────────────────────────────────────


def test_admin_endpoints_require_admin(client):
    user_service.create_user("root", "pw12345", role="admin")
    user_service.create_user("joe", "pw12345", role="user")
    admin_t = _login(client, "root", "pw12345").json()["token"]
    user_t = _login(client, "joe", "pw12345").json()["token"]

    assert client.get("/api/admin/users", headers={"Authorization": f"Bearer {user_t}"}).status_code == 403
    assert client.get("/api/admin/users", headers={"Authorization": f"Bearer {admin_t}"}).status_code == 200

    r = client.post(
        "/api/admin/users",
        headers={"Authorization": f"Bearer {admin_t}"},
        json={"username": "newbie", "password": "pw12345", "role": "user"},
    )
    assert r.status_code == 200 and r.json()["username"] == "newbie"
    dup = client.post(
        "/api/admin/users",
        headers={"Authorization": f"Bearer {admin_t}"},
        json={"username": "newbie", "password": "pw12345"},
    )
    assert dup.status_code == 409


# ── project ownership isolation ──────────────────────────────────────────


def test_projects_scoped_to_owner(client):
    user_service.create_user("u1", "pw12345")
    user_service.create_user("u2", "pw12345")
    t1 = _login(client, "u1", "pw12345").json()["token"]
    t2 = _login(client, "u2", "pw12345").json()["token"]
    h1 = {"Authorization": f"Bearer {t1}"}
    h2 = {"Authorization": f"Bearer {t2}"}

    client.post("/api/projects", json={"name": "P1"}, headers=h1)
    p2 = client.post("/api/projects", json={"name": "P2"}, headers=h2).json()["id"]

    assert [p["name"] for p in client.get("/api/projects", headers=h1).json()] == ["P1"]
    assert [p["name"] for p in client.get("/api/projects", headers=h2).json()] == ["P2"]

    # cross-user access is a 404 (don't leak existence)
    assert client.get(f"/api/projects/{p2}", headers=h1).status_code == 404
    assert client.delete(f"/api/projects/{p2}", headers=h1).status_code == 404
    assert client.get(f"/api/projects/{p2}", headers=h2).status_code == 200


def test_projects_unscoped_without_token(client):
    """Auth off (no token) -> unscoped: behaves like the single-user app."""
    user_service.create_user("solo", "pw12345")
    t = _login(client, "solo", "pw12345").json()["token"]
    client.post("/api/projects", json={"name": "Owned"}, headers={"Authorization": f"Bearer {t}"})
    client.post("/api/projects", json={"name": "Orphan"})  # no token -> owner NULL
    names = {p["name"] for p in client.get("/api/projects").json()}  # no token -> all
    assert {"Owned", "Orphan"} <= names
