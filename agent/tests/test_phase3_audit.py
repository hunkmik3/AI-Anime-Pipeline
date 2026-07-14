"""Phase 3 — audit trail: login events, admin actions, password change are
recorded and readable by admins only."""
from __future__ import annotations

from flowboard.services import user_service


def _login(client, u, p):
    return client.post("/api/account/login", json={"username": u, "password": p})


def _h(client, u, p):
    return {"Authorization": f"Bearer {_login(client, u, p).json()['token']}"}


def _actions(client, adminh):
    rows = client.get("/api/admin/audit?limit=500", headers=adminh).json()
    return [r["action"] for r in rows]


def test_login_events_audited(client):
    user_service.create_user("root", "adminpw1", role="admin")
    user_service.create_user("alice", "alicepw1")
    _login(client, "alice", "wrongpw")   # failed
    _login(client, "alice", "alicepw1")  # success
    acts = _actions(client, _h(client, "root", "adminpw1"))
    assert "login.failed" in acts
    assert "login.success" in acts


def test_admin_actions_audited(client):
    user_service.create_user("boss", "adminpw1", role="admin")
    h = _h(client, "boss", "adminpw1")
    mid = client.post(
        "/api/admin/users", headers=h, json={"username": "m1", "password": "memberpw1"}
    ).json()["id"]
    client.patch(f"/api/admin/users/{mid}", headers=h, json={"status": "suspended"})
    client.patch(f"/api/admin/users/{mid}", headers=h, json={"role": "admin"})
    acts = _actions(client, h)
    for a in ("user.create", "user.suspend", "user.role"):
        assert a in acts, a


def test_password_change_audited(client):
    user_service.create_user("root", "adminpw1", role="admin")
    user_service.create_user("carol", "carolpw1")
    ch = _h(client, "carol", "carolpw1")
    client.post(
        "/api/account/change-password",
        headers=ch,
        json={"current_password": "carolpw1", "new_password": "carolnew1"},
    )
    assert "password.change" in _actions(client, _h(client, "root", "adminpw1"))


def test_audit_requires_admin(client):
    user_service.create_user("plain", "plainpw1")
    h = _h(client, "plain", "plainpw1")
    assert client.get("/api/admin/audit", headers=h).status_code == 403


def test_audit_records_actor_and_target(client):
    user_service.create_user("boss2", "adminpw1", role="admin")
    h = _h(client, "boss2", "adminpw1")
    client.post("/api/admin/users", headers=h, json={"username": "tgt", "password": "tgtpw123"})
    rows = client.get("/api/admin/audit?action=user.create&limit=50", headers=h).json()
    assert rows and rows[0]["actor"] == "boss2" and rows[0]["target"] == "tgt"
    assert rows[0]["created_at"]  # timestamped
