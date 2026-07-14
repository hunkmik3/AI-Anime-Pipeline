"""Phase 1 account lifecycle — self-service password change (+ force-first-login
flag), admin role/email changes, last-admin guard, password policy."""
from __future__ import annotations

from flowboard.services import user_service


def _login(client, u, p):
    return client.post("/api/account/login", json={"username": u, "password": p})


def _h(client, u, p):
    return {"Authorization": f"Bearer {_login(client, u, p).json()['token']}"}


# ── self-service change password ───────────────────────────────────────────


def test_change_own_password(client):
    user_service.create_user("alice", "oldpw123")
    h = _h(client, "alice", "oldpw123")

    # Wrong current password → 400, nothing changes.
    bad = client.post(
        "/api/account/change-password",
        json={"current_password": "nope", "new_password": "newpw123"},
        headers=h,
    )
    assert bad.status_code == 400

    ok = client.post(
        "/api/account/change-password",
        json={"current_password": "oldpw123", "new_password": "newpw123"},
        headers=h,
    )
    assert ok.status_code == 200
    new_token = ok.json()["token"]

    # Old session revoked; the returned fresh token keeps this session alive.
    assert client.get("/api/account/me", headers=h).status_code == 401
    assert client.get(
        "/api/account/me", headers={"Authorization": f"Bearer {new_token}"}
    ).status_code == 200

    assert _login(client, "alice", "newpw123").status_code == 200
    assert _login(client, "alice", "oldpw123").status_code == 401


def test_change_password_enforces_policy(client):
    user_service.create_user("shorty", "oldpw123")
    h = _h(client, "shorty", "oldpw123")
    r = client.post(
        "/api/account/change-password",
        json={"current_password": "oldpw123", "new_password": "short"},  # < 8
        headers=h,
    )
    assert r.status_code == 400


# ── admin provisioning + force-change-first-login ──────────────────────────


def test_admin_created_user_must_change_password(client):
    user_service.create_user("root", "adminpw1", role="admin")
    r = client.post(
        "/api/admin/users",
        headers=_h(client, "root", "adminpw1"),
        json={"username": "newhire", "password": "temppw12", "role": "user"},
    )
    assert r.status_code == 200
    assert r.json()["must_change_password"] is True
    # The flag is visible to the user at login so the SPA can force the change.
    assert _login(client, "newhire", "temppw12").json()["user"]["must_change_password"] is True


def test_password_policy_on_admin_create(client):
    user_service.create_user("root2", "adminpw1", role="admin")
    r = client.post(
        "/api/admin/users",
        headers=_h(client, "root2", "adminpw1"),
        json={"username": "weak", "password": "short"},  # < 8
    )
    assert r.status_code == 400


# ── admin role + email changes ─────────────────────────────────────────────


def test_admin_set_role_and_email(client):
    user_service.create_user("boss", "adminpw1", role="admin")
    target = user_service.create_user("member", "memberpw1")
    h = _h(client, "boss", "adminpw1")

    # Promote member → admin; then they can hit an admin-only route.
    r = client.patch(f"/api/admin/users/{target.id}", headers=h, json={"role": "admin"})
    assert r.status_code == 200 and r.json()["role"] == "admin"
    assert client.get("/api/admin/users", headers=_h(client, "member", "memberpw1")).status_code == 200

    # Set email.
    r = client.patch(
        f"/api/admin/users/{target.id}", headers=h, json={"email": "member@corp.com"}
    )
    assert r.status_code == 200 and r.json()["email"] == "member@corp.com"


def test_cannot_demote_last_admin(client):
    solo = user_service.create_user("onlyadmin", "adminpw1", role="admin")
    h = _h(client, "onlyadmin", "adminpw1")
    r = client.patch(f"/api/admin/users/{solo.id}", headers=h, json={"role": "user"})
    assert r.status_code == 400  # last-admin guard
    assert user_service.get_by_id(solo.id).role == "admin"
