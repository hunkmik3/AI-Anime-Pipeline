"""Phase 9.1: only an admin may create/rename/reorder/delete project structure
(Project · Scene · Shot). A normal user works only *inside* a shot they own
(nodes, prompts, generations, downloads). The no-auth path (REQUIRE_AUTH off,
i.e. dev + the legacy test suite) stays fully open.
"""
from __future__ import annotations

from flowboard.services import user_service


def _login(client, u, p):
    return client.post("/api/account/login", json={"username": u, "password": p})


def _h(client, u, p):
    return {"Authorization": f"Bearer {_login(client, u, p).json()['token']}"}


def _admin(client):
    user_service.create_user("boss", "pw123456", role="admin")
    return _h(client, "boss", "pw123456")


def _user(client, name="worker"):
    u = user_service.create_user(name, "pw123456")
    return u, _h(client, name, "pw123456")


# ── structural creation is admin-only ───────────────────────────────────────


def test_admin_provisions_full_tree_for_a_user(client):
    ah = _admin(client)
    u, uh = _user(client)

    proj = client.post(
        "/api/projects", json={"name": "For Worker", "owner_user_id": str(u.id)}, headers=ah
    )
    assert proj.status_code == 200
    pid = proj.json()["id"]
    assert proj.json()["owner_user_id"] == str(u.id)
    assert proj.json()["owner_name"] == "worker"

    scene = client.post(f"/api/projects/{pid}/scenes", json={"name": "S1"}, headers=ah)
    assert scene.status_code == 200
    sid = scene.json()["id"]

    shot = client.post(f"/api/scenes/{sid}/shots", json={}, headers=ah)
    assert shot.status_code == 200

    # the assigned user can SEE the whole tree...
    assert [p["name"] for p in client.get("/api/projects", headers=uh).json()] == ["For Worker"]
    assert client.get(f"/api/projects/{pid}", headers=uh).status_code == 200
    assert client.get(f"/api/projects/{pid}/scenes", headers=uh).status_code == 200
    assert client.get(f"/api/scenes/{sid}/shots", headers=uh).status_code == 200


def test_non_admin_cannot_create_scene_or_shot(client):
    ah = _admin(client)
    u, uh = _user(client)
    pid = client.post(
        "/api/projects", json={"name": "P", "owner_user_id": str(u.id)}, headers=ah
    ).json()["id"]
    sid = client.post(f"/api/projects/{pid}/scenes", json={"name": "S"}, headers=ah).json()["id"]

    # the OWNER of the project still may not add scenes/shots — structural.
    assert client.post(f"/api/projects/{pid}/scenes", json={"name": "X"}, headers=uh).status_code == 403
    assert client.post(f"/api/scenes/{sid}/shots", json={}, headers=uh).status_code == 403


def test_non_admin_cannot_delete_or_rename_structure(client):
    ah = _admin(client)
    u, uh = _user(client)
    pid = client.post(
        "/api/projects", json={"name": "P", "owner_user_id": str(u.id)}, headers=ah
    ).json()["id"]
    sid = client.post(f"/api/projects/{pid}/scenes", json={"name": "S"}, headers=ah).json()["id"]
    shot_id = client.post(f"/api/scenes/{sid}/shots", json={}, headers=ah).json()["id"]

    assert client.patch(f"/api/projects/{pid}", json={"name": "R"}, headers=uh).status_code == 403
    assert client.patch(f"/api/scenes/{sid}", json={"name": "R"}, headers=uh).status_code == 403
    assert client.delete(f"/api/shots/{shot_id}", headers=uh).status_code == 403
    assert client.delete(f"/api/scenes/{sid}", headers=uh).status_code == 403
    assert client.delete(f"/api/projects/{pid}", headers=uh).status_code == 403
    assert client.post(f"/api/scenes/{sid}/reorder", json={"shot_ids": [shot_id]}, headers=uh).status_code == 403


# ── the user CAN work inside their own shot ──────────────────────────────────


def test_owner_can_work_inside_their_shot(client):
    ah = _admin(client)
    u, uh = _user(client)
    pid = client.post(
        "/api/projects", json={"name": "P", "owner_user_id": str(u.id)}, headers=ah
    ).json()["id"]
    sid = client.post(f"/api/projects/{pid}/scenes", json={"name": "S"}, headers=ah).json()["id"]
    shot_id = client.post(f"/api/scenes/{sid}/shots", json={}, headers=ah).json()["id"]

    # edit the shot script (working inside) — allowed
    assert client.patch(
        f"/api/shots/{shot_id}", json={"script_text": "int. room"}, headers=uh
    ).status_code == 200
    # save the node graph — allowed
    assert client.put(
        f"/api/shots/{shot_id}/workflow", json={"nodes": [], "edges": []}, headers=uh
    ).status_code == 200
    # run + read jobs — allowed
    assert client.post(f"/api/shots/{shot_id}/run", headers=uh).status_code == 200
    assert client.get(f"/api/shots/{shot_id}/jobs", headers=uh).status_code == 200


# ── ownership isolation: a stranger cannot touch another user's tree ─────────


def test_stranger_cannot_read_or_touch_another_users_tree(client):
    ah = _admin(client)
    owner, _oh = _user(client, "owner1")
    _stranger, sh = _user(client, "stranger")

    pid = client.post(
        "/api/projects", json={"name": "Private", "owner_user_id": str(owner.id)}, headers=ah
    ).json()["id"]
    sid = client.post(f"/api/projects/{pid}/scenes", json={"name": "S"}, headers=ah).json()["id"]
    shot_id = client.post(f"/api/scenes/{sid}/shots", json={}, headers=ah).json()["id"]

    # even knowing the UUIDs, a stranger gets 404 everywhere (don't leak existence)
    assert client.get(f"/api/projects/{pid}", headers=sh).status_code == 404
    assert client.get(f"/api/projects/{pid}/scenes", headers=sh).status_code == 404
    assert client.get(f"/api/scenes/{sid}", headers=sh).status_code == 404
    assert client.get(f"/api/scenes/{sid}/shots", headers=sh).status_code == 404
    assert client.get(f"/api/shots/{shot_id}", headers=sh).status_code == 404
    assert client.get(f"/api/shots/{shot_id}/workflow", headers=sh).status_code == 404
    # and cannot mutate it either
    assert client.patch(
        f"/api/shots/{shot_id}", json={"script_text": "hax"}, headers=sh
    ).status_code == 404
    assert client.put(
        f"/api/shots/{shot_id}/workflow", json={"nodes": [], "edges": []}, headers=sh
    ).status_code == 404


# ── admin bypasses ownership; owner reassignment ─────────────────────────────


def test_admin_sees_all_and_can_reassign_owner(client):
    ah = _admin(client)
    u1, _ = _user(client, "alpha")
    u2, u2h = _user(client, "bravo")
    pid = client.post(
        "/api/projects", json={"name": "Movable", "owner_user_id": str(u1.id)}, headers=ah
    ).json()["id"]

    # reassign to bravo
    r = client.patch(f"/api/projects/{pid}", json={"owner_user_id": str(u2.id)}, headers=ah)
    assert r.status_code == 200 and r.json()["owner_user_id"] == str(u2.id)
    # now bravo sees it, alpha does not
    assert client.get(f"/api/projects/{pid}", headers=u2h).status_code == 200

    # reassign to a non-existent user → 404
    import uuid as _uuid

    assert client.patch(
        f"/api/projects/{pid}", json={"owner_user_id": str(_uuid.uuid4())}, headers=ah
    ).status_code == 404


def test_no_auth_path_still_fully_open(client):
    """REQUIRE_AUTH off (no token): create the whole tree with no gate — this
    is the legacy single-user / test behaviour and must not regress."""
    pid = client.post("/api/projects", json={"name": "Solo"}).json()["id"]
    sid = client.post(f"/api/projects/{pid}/scenes", json={"name": "S"}).json()["id"]
    shot = client.post(f"/api/scenes/{sid}/shots", json={})
    assert shot.status_code == 200
    assert client.delete(f"/api/shots/{shot.json()['id']}").status_code == 200
