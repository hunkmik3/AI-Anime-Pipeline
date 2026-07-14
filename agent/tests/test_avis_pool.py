"""Global Avis pool — Avis has no balance API, so the admin enters the top-up
and we draw it down against the REAL per-generation usdCost. Guards: refuse to
reserve past the shared key's remaining money; warn when granted user budgets
exceed it."""
from __future__ import annotations

from flowboard.services import budget_service as b
from flowboard.services import user_service


def _login(client, u, p):
    return client.post("/api/account/login", json={"username": u, "password": p})


def _h(client, u, p):
    return {"Authorization": f"Bearer {_login(client, u, p).json()['token']}"}


# ── pool math ───────────────────────────────────────────────────────────────


def test_pool_unconfigured_means_no_guard(client):
    u = user_service.create_user("u1", "userpw12")
    user_service.set_budget(u.id, 100.0)
    assert b.pool_available_usd() is None          # unconfigured → guard off
    assert b.reserve(u.id, request_id=None, estimated_usd=10.0, model="m") is True
    assert b.pool_summary()["configured"] is False


def test_pool_summary_tracks_real_spend(client):
    u = user_service.create_user("u2", "userpw12")
    user_service.set_budget(u.id, 100.0)
    b.set_pool_usd(50.0)
    assert b.reserve(u.id, request_id=901, estimated_usd=10.0, model="m") is True

    s = b.pool_summary()
    assert s["pool_usd"] == 50.0
    assert s["reserved_usd"] == 10.0            # outstanding hold
    assert s["available_usd"] == 40.0           # 50 − 0 spent − 10 held

    b.settle(901, 7.5)                          # real Avis usdCost came back lower
    s = b.pool_summary()
    assert s["spent_usd"] == 7.5
    assert s["reserved_usd"] == 0.0
    assert s["available_usd"] == 42.5           # hold released, real cost drawn


def test_pool_blocks_reserve_when_exhausted(client):
    u = user_service.create_user("u3", "userpw12")
    user_service.set_budget(u.id, 1000.0)       # plenty of personal budget…
    b.set_pool_usd(5.0)                         # …but the shared key is nearly empty
    assert b.reserve(u.id, request_id=None, estimated_usd=20.0, model="m") is False
    assert b.reserve(u.id, request_id=None, estimated_usd=4.0, model="m") is True


def test_over_allocated_flag(client):
    u = user_service.create_user("u4", "userpw12")
    user_service.set_budget(u.id, 500.0)        # granted more than the key holds
    b.set_pool_usd(100.0)
    s = b.pool_summary()
    assert s["over_allocated"] is True
    assert s["exhausted"] is False

    b.set_pool_usd(600.0)                       # top up → back under control
    assert b.pool_summary()["over_allocated"] is False


# ── admin API ───────────────────────────────────────────────────────────────


def test_admin_pool_endpoints(client):
    user_service.create_user("root", "adminpw1", role="admin")
    h = _h(client, "root", "adminpw1")

    assert client.get("/api/admin/pool", headers=h).json()["configured"] is False

    r = client.patch("/api/admin/pool", headers=h, json={"pool_usd": 444.32})
    assert r.status_code == 200 and r.json()["pool_usd"] == 444.32

    r = client.patch("/api/admin/pool", headers=h, json={"add_usd": 55.68})
    assert r.json()["pool_usd"] == 500.0        # top-up

    # audited
    acts = [a["action"] for a in client.get("/api/admin/audit", headers=h).json()]
    assert "pool.set" in acts and "pool.topup" in acts


def test_pool_endpoint_requires_admin(client):
    user_service.create_user("plain", "plainpw1")
    h = _h(client, "plain", "plainpw1")
    assert client.patch("/api/admin/pool", headers=h, json={"pool_usd": 1}).status_code == 403


# ── the hard block on gen dispatch ──────────────────────────────────────────


def test_create_request_refused_when_pool_exhausted(client):
    u = user_service.create_user("gen", "userpw12")
    user_service.set_budget(u.id, 1000.0)       # user can afford it…
    b.set_pool_usd(0.5)                         # …the shared key can't (5s/720p ≈ $0.90)
    h = _h(client, "gen", "userpw12")
    r = client.post(
        "/api/requests",
        headers=h,
        json={"type": "gen_video", "params": {"duration_seconds": 5, "resolution": "720p"}},
    )
    assert r.status_code == 402
    assert r.json()["detail"] == "avis_pool_exhausted"   # not "insufficient_budget"
