"""Phase 11.1 — Project/Series credit budgets: hard block + PM grants.

The BOD sets a ceiling per scope; when it's used up generation is refused (402)
and the block is recorded. A PM can top it up, but only with a reason.
"""
from __future__ import annotations

from flowboard.db import get_session
from flowboard.db.models import AuditLog, Request, User
from flowboard.services import user_service
from sqlmodel import select


def _h(client, u, p="pw123456"):
    tok = client.post("/api/account/login", json={"username": u, "password": p}).json()
    return {"Authorization": f"Bearer {tok['token']}"}


def _funded_admin(client, name="boss"):
    """An admin with a per-user budget — the Phase 9.2 per-user gate runs BEFORE
    the scope gate, so without this every gen 402s for the wrong reason."""
    u = user_service.create_user(name, "pw123456", role="admin")
    with get_session() as s:
        row = s.get(User, u.id)
        row.budget_usd = 10_000.0
        s.add(row)
        s.commit()
    return u, _h(client, name)


def _tree(client, headers, owner_id=None):
    """project → series → episode → sequence → a video node."""
    body = {"name": "MOGU"}
    if owner_id is not None:
        body["owner_user_id"] = str(owner_id)
    pid = client.post("/api/projects", json=body, headers=headers).json()["id"]
    sid = client.post(
        f"/api/projects/{pid}/series", json={"name": "S1", "code": "S1"}, headers=headers
    ).json()["id"]
    ep = client.post(
        f"/api/projects/{pid}/scenes", json={"name": "EP1", "series_id": sid}, headers=headers
    ).json()["id"]
    shot = client.post(f"/api/scenes/{ep}/shots", json={}, headers=headers).json()["id"]
    node = client.post(
        "/api/nodes", json={"shot_id": shot, "type": "video", "x": 0, "y": 0}, headers=headers
    ).json()["id"]
    return pid, sid, ep, node


def _gen(client, node_id, headers, seconds=5):
    return client.post(
        "/api/requests",
        json={
            "node_id": node_id,
            "type": "gen_video",
            "params": {"duration_seconds": seconds, "resolution": "1080p"},
        },
        headers=headers,
    )


# ── budget maths ───────────────────────────────────────────────────────────


def test_unlimited_by_default(client):
    boss, ah = _funded_admin(client)
    pid, _sid, _ep, _node = _tree(client, ah, owner_id=boss.id)
    b = client.get(f"/api/budgets/project/{pid}", headers=ah).json()
    assert b["unlimited"] is True and b["remaining_usd"] is None


def test_set_budget_is_admin_only(client):
    boss, ah = _funded_admin(client)
    pm = user_service.create_user("pm", "pw123456")
    pid, _sid, _ep, _node = _tree(client, ah, owner_id=boss.id)
    client.patch(f"/api/projects/{pid}", json={"member_user_ids": [str(pm.id)]}, headers=ah)

    assert client.put(
        f"/api/budgets/project/{pid}", json={"amount_usd": 10}, headers=_h(client, "pm")
    ).status_code == 403
    ok = client.put(f"/api/budgets/project/{pid}", json={"amount_usd": 10}, headers=ah)
    assert ok.status_code == 200 and ok.json()["effective_usd"] == 10.0


# ── the hard block ─────────────────────────────────────────────────────────


def test_generation_blocked_when_scope_budget_exhausted(client):
    boss, ah = _funded_admin(client)
    pid, _sid, _ep, node = _tree(client, ah, owner_id=boss.id)
    # a ceiling far below one generation's estimate
    client.put(f"/api/budgets/project/{pid}", json={"amount_usd": 0.01}, headers=ah)

    r = _gen(client, node, ah)
    assert r.status_code == 402
    detail = r.json()["detail"]
    assert detail["code"] == "scope_budget_exhausted"
    assert detail["budget"]["scope"] == "project"
    # nothing was queued
    with get_session() as s:
        assert s.exec(select(Request)).all() == []


def test_block_is_recorded_in_the_audit_log(client):
    boss, ah = _funded_admin(client)
    pid, _sid, _ep, node = _tree(client, ah, owner_id=boss.id)
    client.put(f"/api/budgets/project/{pid}", json={"amount_usd": 0.01}, headers=ah)
    _gen(client, node, ah)
    with get_session() as s:
        rows = s.exec(select(AuditLog).where(AuditLog.action == "budget.blocked")).all()
    assert len(rows) == 1 and "exhausted" in (rows[0].detail or "")


def test_series_ceiling_blocks_even_when_project_has_room(client):
    """The tighter of the two ceilings wins."""
    boss, ah = _funded_admin(client)
    pid, sid, _ep, node = _tree(client, ah, owner_id=boss.id)
    client.put(f"/api/budgets/project/{pid}", json={"amount_usd": 1000}, headers=ah)
    client.put(f"/api/budgets/series/{sid}", json={"amount_usd": 0.01}, headers=ah)
    r = _gen(client, node, ah)
    assert r.status_code == 402
    assert r.json()["detail"]["budget"]["scope"] == "series"


def test_generation_allowed_within_budget(client):
    boss, ah = _funded_admin(client)
    pid, _sid, _ep, node = _tree(client, ah, owner_id=boss.id)
    client.put(f"/api/budgets/project/{pid}", json={"amount_usd": 500}, headers=ah)
    assert _gen(client, node, ah).status_code == 200


# ── grants ─────────────────────────────────────────────────────────────────


def test_request_needs_a_reason(client):
    boss, ah = _funded_admin(client)
    pid, _sid, _ep, _node = _tree(client, ah, owner_id=boss.id)
    assert client.post(
        f"/api/budgets/project/{pid}/grants", json={"amount_usd": 50}, headers=ah
    ).status_code == 422
    assert client.post(
        f"/api/budgets/project/{pid}/grants",
        json={"amount_usd": 50, "reason": ""},
        headers=ah,
    ).status_code == 422


def test_pending_request_does_not_raise_the_ceiling(client):
    """Asking must not be the same as getting — the block stays until approval."""
    boss, ah = _funded_admin(client)
    pid, _sid, _ep, node = _tree(client, ah, owner_id=boss.id)
    client.put(f"/api/budgets/project/{pid}", json={"amount_usd": 0.01}, headers=ah)
    assert _gen(client, node, ah).status_code == 402

    r = client.post(
        f"/api/budgets/project/{pid}/grants",
        json={"amount_usd": 50, "reason": "reshoot after client notes"},
        headers=ah,
    )
    assert r.status_code == 200
    assert r.json()["grant"]["status"] == "pending"
    assert r.json()["granted_usd"] == 0.0          # nothing landed yet
    assert r.json()["effective_usd"] == 0.01
    assert _gen(client, node, ah).status_code == 402  # still blocked


def test_admin_approval_unblocks(client):
    boss, ah = _funded_admin(client)
    pid, _sid, _ep, node = _tree(client, ah, owner_id=boss.id)
    client.put(f"/api/budgets/project/{pid}", json={"amount_usd": 0.01}, headers=ah)
    gid = client.post(
        f"/api/budgets/project/{pid}/grants",
        json={"amount_usd": 50, "reason": "reshoot after client notes"},
        headers=ah,
    ).json()["grant"]["id"]

    # it shows up in the admin inbox
    pend = client.get("/api/budgets/requests/pending", headers=ah).json()["requests"]
    assert [g["id"] for g in pend] == [gid]

    ok = client.post(f"/api/budgets/requests/{gid}/approve", json={}, headers=ah)
    assert ok.status_code == 200
    assert ok.json()["granted_usd"] == 50.0 and ok.json()["effective_usd"] == 50.01
    assert _gen(client, node, ah).status_code == 200
    # and it leaves the inbox
    assert client.get("/api/budgets/requests/pending", headers=ah).json()["requests"] == []


def test_admin_rejection_keeps_the_block_and_needs_a_note(client):
    boss, ah = _funded_admin(client)
    pid, _sid, _ep, node = _tree(client, ah, owner_id=boss.id)
    client.put(f"/api/budgets/project/{pid}", json={"amount_usd": 0.01}, headers=ah)
    gid = client.post(
        f"/api/budgets/project/{pid}/grants",
        json={"amount_usd": 50, "reason": "just because"},
        headers=ah,
    ).json()["grant"]["id"]

    # a note is required to reject
    assert client.post(
        f"/api/budgets/requests/{gid}/reject", json={}, headers=ah
    ).status_code == 400
    r = client.post(
        f"/api/budgets/requests/{gid}/reject",
        json={"note": "cut the scene instead"},
        headers=ah,
    )
    assert r.status_code == 200
    assert r.json()["grant"]["status"] == "rejected"
    assert r.json()["granted_usd"] == 0.0
    assert _gen(client, node, ah).status_code == 402  # still blocked


def test_a_request_is_decided_once(client):
    boss, ah = _funded_admin(client)
    pid, _sid, _ep, _node = _tree(client, ah, owner_id=boss.id)
    gid = client.post(
        f"/api/budgets/project/{pid}/grants",
        json={"amount_usd": 10, "reason": "x"},
        headers=ah,
    ).json()["grant"]["id"]
    assert client.post(f"/api/budgets/requests/{gid}/approve", json={}, headers=ah).status_code == 200
    assert client.post(f"/api/budgets/requests/{gid}/approve", json={}, headers=ah).status_code == 400


def test_pm_cannot_decide_their_own_request(client):
    """A PM may ask; only an admin decides."""
    boss, ah = _funded_admin(client)
    pm = user_service.create_user("pm2", "pw123456")
    pid, _sid, _ep, _node = _tree(client, ah, owner_id=pm.id)
    ph = _h(client, "pm2")
    gid = client.post(
        f"/api/budgets/project/{pid}/grants",
        json={"amount_usd": 30, "reason": "need more takes"},
        headers=ph,
    ).json()["grant"]["id"]
    assert client.post(f"/api/budgets/requests/{gid}/approve", json={}, headers=ph).status_code == 403
    assert client.get("/api/budgets/requests/pending", headers=ph).status_code == 403


def test_grant_log_keeps_who_and_why(client):
    boss, ah = _funded_admin(client)
    pid, _sid, _ep, _node = _tree(client, ah, owner_id=boss.id)
    client.post(
        f"/api/budgets/project/{pid}/grants",
        json={"amount_usd": 25, "reason": "extra takes for the finale"},
        headers=ah,
    )
    log = client.get(f"/api/budgets/project/{pid}/grants", headers=ah).json()["grants"]
    assert len(log) == 1
    assert log[0]["reason"] == "extra takes for the finale"
    assert log[0]["granted_by_name"] == "boss"
    assert log[0]["status"] == "pending"   # waiting on an admin verdict


def test_artist_cannot_request(client):
    boss, ah = _funded_admin(client)
    emp = user_service.create_user("emp", "pw123456")
    pid, _sid, _ep, _node = _tree(client, ah, owner_id=boss.id)
    # Keep the owner in the roster — PUT /members replaces the whole set, and
    # the first entry becomes the primary owner (an implicit producer).
    client.put(
        f"/api/projects/{pid}/members",
        json={
            "members": [
                {"user_id": str(boss.id), "role": "producer"},
                {"user_id": str(emp.id), "role": "artist"},
            ]
        },
        headers=ah,
    )
    r = client.post(
        f"/api/budgets/project/{pid}/grants",
        json={"amount_usd": 10, "reason": "please"},
        headers=_h(client, "emp"),
    )
    assert r.status_code == 403
