"""Phase 9.2 budgeting: estimate, reserve/settle/release, gen_video hard-cap."""
from __future__ import annotations

import pytest

from flowboard.services import budget_service, user_service


def test_estimate_video_usd():
    assert budget_service.estimate_video_usd(5, "720p") == pytest.approx(0.90)
    assert budget_service.estimate_video_usd(15, "1080p") == pytest.approx(6.30)
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


def test_cancel_releases_budget_hold(client):
    """Cancelling a queued gen_video must give the reserved estimate back —
    the worker skips canceled rows, so it never settles/releases them."""
    u = user_service.create_user("canceller", "pw12345")
    user_service.set_budget(u.id, 20.0)
    h = {"Authorization": f"Bearer {_login(client, 'canceller')}"}
    r = client.post(
        "/api/requests",
        json={"type": "gen_video", "params": {"duration_seconds": 15, "resolution": "1080p"}},
        headers=h,
    )
    assert r.status_code == 200
    rid = r.json()["id"]
    assert budget_service.has_reservation(rid) is True
    assert budget_service.available_usd(u.id) == pytest.approx(20.0 - 6.30)  # held

    c = client.post(f"/api/requests/{rid}/cancel")
    assert c.status_code == 200
    assert budget_service.has_reservation(rid) is False
    assert budget_service.available_usd(u.id) == pytest.approx(20.0)  # fully restored


def test_worker_backstop_releases_drifted_hold():
    """A reservation left behind by a row that drifted out of 'queued' (e.g.
    canceled) must still be freed by the worker's _settle_budget backstop."""
    from flowboard.worker.processor import _settle_budget

    u = user_service.create_user("backstop", "pw12345")
    user_service.set_budget(u.id, 20.0)
    assert budget_service.reserve(u.id, request_id=99001, estimated_usd=6.30, model="m") is True
    assert budget_service.available_usd(u.id) == pytest.approx(13.70)

    _settle_budget(99001, "gen_video", None, failed=True)  # the drift-skip branch
    assert budget_service.has_reservation(99001) is False
    assert budget_service.available_usd(u.id) == pytest.approx(20.0)
    assert budget_service.summary(u.id)["spent_usd"] == pytest.approx(0.0)  # nothing charged


def test_double_release_is_idempotent():
    """cancel (Fix 1) and the worker backstop (Fix 2) can both fire for one
    request — the second release must not double-refund."""
    u = user_service.create_user("idem", "pw12345")
    user_service.set_budget(u.id, 20.0)
    budget_service.reserve(u.id, request_id=99002, estimated_usd=6.30, model="m")
    budget_service.release(99002)
    budget_service.release(99002)  # second call is a no-op
    assert budget_service.available_usd(u.id) == pytest.approx(20.0)  # not 26.30


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
    assert budget_service.available_usd(u.id) == pytest.approx(20.0 - 0.90)  # 5*0.18 held


def test_admin_user_activity(client):
    """Admin can see a user's generation ledger joined to request output/cost."""
    user_service.create_user("act_admin", "pw12345", role="admin")
    h = {"Authorization": f"Bearer {_login(client, 'act_admin')}"}
    u = user_service.create_user("act_target", "pw12345")
    user_service.set_budget(u.id, 50.0)

    from flowboard.db import get_session
    from flowboard.db.models import Request

    with get_session() as s:
        req = Request(
            type="gen_video",
            params={"duration_seconds": 15, "resolution": "1080p", "prompt": "a neon cat"},
            status="done",
            result={"media_ids": ["mid_abc"], "cost_usd": 6.19},
        )
        s.add(req)
        s.commit()
        s.refresh(req)
        rid = req.id

    assert budget_service.reserve(u.id, request_id=rid, estimated_usd=6.30, model="seedance-2-0") is True
    budget_service.settle(rid, 6.19)

    r = client.get(f"/api/admin/users/{u.id}/activity", headers=h)
    assert r.status_code == 200
    data = r.json()
    assert data["user"]["username"] == "act_target"
    assert data["summary"]["spent_usd"] == pytest.approx(6.19)
    assert data["summary"]["gen_count"] == 1
    assert len(data["items"]) == 1
    it = data["items"][0]
    assert it["model"] == "seedance-2-0"
    assert it["ledger_status"] == "settled"
    assert it["actual_usd"] == pytest.approx(6.19)
    assert it["cost_usd"] == pytest.approx(6.19)
    assert it["request_type"] == "gen_video"
    assert it["request_status"] == "done"
    assert it["resolution"] == "1080p"
    assert it["duration_seconds"] == 15
    assert it["prompt"] == "a neon cat"
    assert it["media_ids"] == ["mid_abc"]


def test_admin_user_activity_requires_admin(client):
    user_service.create_user("act_plain", "pw12345")
    target = user_service.create_user("act_other", "pw12345")
    h = {"Authorization": f"Bearer {_login(client, 'act_plain')}"}
    r = client.get(f"/api/admin/users/{target.id}/activity", headers=h)
    assert r.status_code == 403
