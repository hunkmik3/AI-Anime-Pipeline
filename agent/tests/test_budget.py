"""Phase 9.2 budgeting: estimate, reserve/settle/release, gen_video hard-cap."""
from __future__ import annotations

import pytest

from flowboard.services import budget_service, user_service


def test_estimate_video_usd():
    assert budget_service.estimate_video_usd(5, "720p") == pytest.approx(1.1)
    assert budget_service.estimate_video_usd(15, "1080p") == pytest.approx(7.5)
    assert budget_service.estimate_video_usd(None, None) > 0  # safe default


def test_reserve_settle_release_flow():
    u = user_service.create_user("acct", "pw12345")
    user_service.set_budget(u.id, 10.0)
    assert budget_service.available_usd(u.id) == pytest.approx(10.0)

    assert budget_service.reserve(u.id, request_id=101, estimated_usd=4.0, model="seedance-2-0") is True
    assert budget_service.available_usd(u.id) == pytest.approx(6.0)  # held

    # settle cheaper than estimate -> charge actual, free the hold
    budget_service.settle(101, 2.5)
    assert budget_service.available_usd(u.id) == pytest.approx(7.5)  # 10 - 2.5 spent

    # a second hold, then release -> nothing charged
    budget_service.reserve(u.id, request_id=102, estimated_usd=3.0, model="m")
    assert budget_service.available_usd(u.id) == pytest.approx(4.5)
    budget_service.release(102)
    assert budget_service.available_usd(u.id) == pytest.approx(7.5)


def test_reserve_refused_over_budget():
    u = user_service.create_user("tight", "pw12345")
    user_service.set_budget(u.id, 2.0)
    assert budget_service.reserve(u.id, request_id=200, estimated_usd=5.0, model="m") is False
    assert budget_service.available_usd(u.id) == pytest.approx(2.0)  # unchanged


def _login(client, username, password="pw12345"):
    return client.post("/api/account/login", json={"username": username, "password": password}).json()["token"]


def test_gen_video_blocked_when_over_budget(client):
    u = user_service.create_user("poor", "pw12345")
    user_service.set_budget(u.id, 1.0)
    h = {"Authorization": f"Bearer {_login(client, 'poor')}"}
    r = client.post(
        "/api/requests",
        json={"type": "gen_video", "params": {"duration_seconds": 15, "resolution": "1080p"}},
        headers=h,
    )
    assert r.status_code == 402  # est 7.5 > 1.0


def test_gen_video_reserves_within_budget(client):
    u = user_service.create_user("rich", "pw12345")
    user_service.set_budget(u.id, 20.0)
    h = {"Authorization": f"Bearer {_login(client, 'rich')}"}
    r = client.post(
        "/api/requests",
        json={"type": "gen_video", "params": {"duration_seconds": 5, "resolution": "720p"}},
        headers=h,
    )
    assert r.status_code == 200
    rid = r.json()["id"]
    assert budget_service.has_reservation(rid) is True
    assert budget_service.available_usd(u.id) == pytest.approx(20.0 - 1.1)  # 5*0.22 held
