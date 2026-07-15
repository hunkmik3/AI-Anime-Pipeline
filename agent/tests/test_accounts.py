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
    user_service.create_user("alice", "pw123456", role="user")
    r = _login(client, "alice", "pw123456")
    assert r.status_code == 200
    token = r.json()["token"]
    me = client.get("/api/account/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["username"] == "alice"
    assert _login(client, "alice", "nope").status_code == 401          # wrong pw
    assert client.get("/api/account/me").status_code == 401            # no token


def test_suspended_account_cannot_login(client):
    u = user_service.create_user("bob", "pw123456")
    user_service.set_status(u.id, "suspended")
    assert _login(client, "bob", "pw123456").status_code == 401


# ── admin gating ─────────────────────────────────────────────────────────


def test_admin_endpoints_require_admin(client):
    user_service.create_user("root", "pw123456", role="admin")
    user_service.create_user("joe", "pw123456", role="user")
    admin_t = _login(client, "root", "pw123456").json()["token"]
    user_t = _login(client, "joe", "pw123456").json()["token"]

    assert client.get("/api/admin/users", headers={"Authorization": f"Bearer {user_t}"}).status_code == 403
    assert client.get("/api/admin/users", headers={"Authorization": f"Bearer {admin_t}"}).status_code == 200

    r = client.post(
        "/api/admin/users",
        headers={"Authorization": f"Bearer {admin_t}"},
        json={"username": "newbie", "password": "pw123456", "role": "user"},
    )
    assert r.status_code == 200 and r.json()["username"] == "newbie"
    dup = client.post(
        "/api/admin/users",
        headers={"Authorization": f"Bearer {admin_t}"},
        json={"username": "newbie", "password": "pw123456"},
    )
    assert dup.status_code == 409


# ── project ownership isolation ──────────────────────────────────────────


def test_projects_scoped_to_owner(client):
    # Phase 9.1: only an admin creates projects, assigning each to a user.
    u1 = user_service.create_user("u1", "pw123456")
    u2 = user_service.create_user("u2", "pw123456")
    user_service.create_user("padmin", "pw123456", role="admin")
    t1 = _login(client, "u1", "pw123456").json()["token"]
    t2 = _login(client, "u2", "pw123456").json()["token"]
    ah = {"Authorization": f"Bearer {_login(client, 'padmin', 'pw123456').json()['token']}"}
    h1 = {"Authorization": f"Bearer {t1}"}
    h2 = {"Authorization": f"Bearer {t2}"}

    client.post("/api/projects", json={"name": "P1", "owner_user_id": str(u1.id)}, headers=ah)
    p2 = client.post(
        "/api/projects", json={"name": "P2", "owner_user_id": str(u2.id)}, headers=ah
    ).json()["id"]

    # each user sees only the project assigned to them
    assert [p["name"] for p in client.get("/api/projects", headers=h1).json()] == ["P1"]
    assert [p["name"] for p in client.get("/api/projects", headers=h2).json()] == ["P2"]

    # cross-user access is a 404 (don't leak existence)
    assert client.get(f"/api/projects/{p2}", headers=h1).status_code == 404
    assert client.get(f"/api/projects/{p2}", headers=h2).status_code == 200
    # a non-admin cannot delete even their own project (structural → admin only)
    assert client.delete(f"/api/projects/{p2}", headers=h2).status_code == 403
    # ...but the admin can, and sees every project
    assert {"P1", "P2"} <= {p["name"] for p in client.get("/api/projects", headers=ah).json()}
    assert client.delete(f"/api/projects/{p2}", headers=ah).status_code == 200


def test_non_admin_cannot_create_project(client):
    """Phase 9.1: a normal authenticated user is forbidden from creating
    project structure — that is admin-only."""
    user_service.create_user("plebe", "pw123456")
    h = {"Authorization": f"Bearer {_login(client, 'plebe', 'pw123456').json()['token']}"}
    assert client.post("/api/projects", json={"name": "Nope"}, headers=h).status_code == 403


def test_projects_unscoped_without_token(client):
    """Auth off (no token) -> unscoped: behaves like the single-user app.
    The no-auth path must still create + list freely (dev / test parity)."""
    u = user_service.create_user("solo", "pw123456")
    user_service.create_user("uadmin", "pw123456", role="admin")
    ah = {"Authorization": f"Bearer {_login(client, 'uadmin', 'pw123456').json()['token']}"}
    client.post("/api/projects", json={"name": "Owned", "owner_user_id": str(u.id)}, headers=ah)
    client.post("/api/projects", json={"name": "Orphan"})  # no token -> owner NULL
    names = {p["name"] for p in client.get("/api/projects").json()}  # no token -> all
    assert {"Owned", "Orphan"} <= names


# ── delete account ───────────────────────────────────────────────────────


def test_admin_delete_user(client):
    user_service.create_user("delroot", "pw123456", role="admin")
    target = user_service.create_user("delme", "pw123456")
    h = {"Authorization": f"Bearer {_login(client, 'delroot', 'pw123456').json()['token']}"}
    assert client.delete(f"/api/admin/users/{target.id}", headers=h).status_code == 200
    assert user_service.get_by_id(target.id) is None


def test_admin_cannot_delete_self(client):
    me = user_service.create_user("delself", "pw123456", role="admin")
    h = {"Authorization": f"Bearer {_login(client, 'delself', 'pw123456').json()['token']}"}
    assert client.delete(f"/api/admin/users/{me.id}", headers=h).status_code == 400
    assert user_service.get_by_id(me.id) is not None  # still there


def test_admin_delete_orphans_projects(client):
    from flowboard.db import get_session
    from flowboard.db.models import Project

    user_service.create_user("delowner_admin", "pw123456", role="admin")
    owner = user_service.create_user("delowner", "pw123456")
    with get_session() as s:
        p = Project(name="Owned", owner_user_id=owner.id)
        s.add(p)
        s.commit()
        s.refresh(p)
        pid = p.id
    h = {"Authorization": f"Bearer {_login(client, 'delowner_admin', 'pw123456').json()['token']}"}
    assert client.delete(f"/api/admin/users/{owner.id}", headers=h).status_code == 200
    with get_session() as s:
        survived = s.get(Project, pid)
        assert survived is not None          # content not destroyed
        assert survived.owner_user_id is None  # just orphaned


def test_delete_user_requires_admin(client):
    user_service.create_user("plain_del", "pw123456")
    target = user_service.create_user("victim", "pw123456")
    h = {"Authorization": f"Bearer {_login(client, 'plain_del', 'pw123456').json()['token']}"}
    assert client.delete(f"/api/admin/users/{target.id}", headers=h).status_code == 403
