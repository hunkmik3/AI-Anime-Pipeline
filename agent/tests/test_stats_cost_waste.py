"""Cost/waste analytics — how much money produced a KEPT clip vs burned on
re-rolled takes, plus download tracking.

The maths comes straight off the Avis bill (settled UsageRecords), so a user
cannot inflate their numbers by clicking anything.
"""
from __future__ import annotations

from flowboard.db import get_session
from flowboard.db.models import Node, Request, UsageRecord
from flowboard.services import stats_service, user_service
from tests.conftest import make_shot


def _login(client, u, p):
    return client.post("/api/account/login", json={"username": u, "password": p})


def _h(client, u, p):
    return {"Authorization": f"Bearer {_login(client, u, p).json()['token']}"}


def _gen(client, user_id, node_id: int, cost: float, *, model="seedance-2-0", failed=False):
    """Simulate one paid (or technically-failed → refunded) generation."""
    with get_session() as s:
        req = Request(node_id=node_id, type="gen_video", params={"resolution": "720p"}, status="done")
        s.add(req)
        s.commit()
        s.refresh(req)
        rid = req.id
        s.add(
            UsageRecord(
                user_id=user_id,
                request_id=rid,
                kind="video",
                model=model,
                estimated_usd=cost,
                actual_usd=(None if failed else cost),
                status=("released" if failed else "settled"),
            )
        )
        s.commit()
    return rid


def _video_node(client) -> int:
    b = make_shot(client)
    r = client.post(
        "/api/nodes",
        json={"shot_id": b["id"], "type": "video", "x": 0, "y": 0, "data": {"title": "Clip"}},
    )
    return r.json()["id"]


# ── the core split ──────────────────────────────────────────────────────────


def test_last_take_kept_earlier_takes_wasted(client):
    u = user_service.create_user("maker", "makerpw1")
    node = _video_node(client)
    _gen(client, u.id, node, 3.0)   # take 1 — re-rolled away
    _gen(client, u.id, node, 3.0)   # take 2 — re-rolled away
    _gen(client, u.id, node, 2.0)   # take 3 — kept (stopped re-rolling)

    rows = stats_service.user_costs()
    r = next(x for x in rows if x["username"] == "maker")
    assert r["spent_usd"] == 8.0
    assert r["wasted_usd"] == 6.0      # the two discarded takes
    assert r["kept_usd"] == 2.0        # the final one
    assert r["kept_clips"] == 1
    assert r["takes"] == 3
    assert r["waste_pct"] == 75.0


def test_failed_gen_is_free_and_excluded(client):
    u = user_service.create_user("unlucky", "unluckypw1")
    node = _video_node(client)
    _gen(client, u.id, node, 5.0, failed=True)   # Avis errored → refunded
    _gen(client, u.id, node, 2.0)                # the real one

    r = next(x for x in stats_service.user_costs() if x["username"] == "unlucky")
    assert r["spent_usd"] == 2.0        # the failed take costs nothing
    assert r["wasted_usd"] == 0.0
    assert r["kept_usd"] == 2.0
    assert r["takes"] == 1


def test_one_shot_one_take_zero_waste(client):
    u = user_service.create_user("sharp", "sharppw12")
    _gen(client, u.id, _video_node(client), 2.5)
    r = next(x for x in stats_service.user_costs() if x["username"] == "sharp")
    assert r["wasted_usd"] == 0.0 and r["kept_usd"] == 2.5 and r["waste_pct"] == 0.0


def test_per_clip_drilldown(client):
    u = user_service.create_user("driller", "drillpw12")
    node = _video_node(client)
    _gen(client, u.id, node, 4.0)
    _gen(client, u.id, node, 1.0)

    clips = stats_service.user_clips(u.id)
    assert len(clips) == 1
    c = clips[0]
    assert c["takes"] == 2 and c["wasted_usd"] == 4.0 and c["kept_usd"] == 1.0
    assert c["total_usd"] == 5.0
    assert c["downloaded"] is False


# ── download tracking ────────────────────────────────────────────────────────


def test_download_ping_marks_clip_as_taken(client):
    u = user_service.create_user("dl", "dlpw1234")
    h = _h(client, "dl", "dlpw1234")
    node = _video_node(client)
    _gen(client, u.id, node, 2.0)

    assert stats_service.user_clips(u.id)[0]["downloaded"] is False
    r = client.post(
        "/api/media/abc123def/downloaded", headers=h, json={"node_id": node}
    )
    assert r.status_code == 200
    assert stats_service.user_clips(u.id)[0]["downloaded"] is True

    r = next(x for x in stats_service.user_costs() if x["username"] == "dl")
    assert r["downloaded_clips"] == 1


def test_download_ping_rejects_bad_media_id(client):
    u = user_service.create_user("dl2", "dlpw1234")
    h = _h(client, "dl2", "dlpw1234")
    assert client.post(
        "/api/media/not a valid id!/downloaded", headers=h, json={}
    ).status_code in (400, 404)


# ── admin endpoints ──────────────────────────────────────────────────────────


def test_admin_stats_endpoints(client):
    user_service.create_user("root", "adminpw1", role="admin")
    u = user_service.create_user("worker", "workerpw1")
    node = _video_node(client)
    _gen(client, u.id, node, 6.0)
    _gen(client, u.id, node, 2.0)
    h = _h(client, "root", "adminpw1")

    ov = client.get("/api/admin/stats/overview", headers=h).json()
    assert ov["spent_usd"] == 8.0 and ov["wasted_usd"] == 6.0 and ov["kept_clips"] == 1

    users = client.get("/api/admin/stats/users", headers=h).json()
    assert any(x["username"] == "worker" and x["wasted_usd"] == 6.0 for x in users)

    clips = client.get(f"/api/admin/stats/users/{u.id}/clips", headers=h).json()
    assert clips[0]["takes"] == 2 and clips[0]["total_usd"] == 8.0

    assert client.get("/api/admin/stats/projects", headers=h).status_code == 200
    models = client.get("/api/admin/stats/models", headers=h).json()
    assert any(m["takes"] == 2 for m in models)


def test_stats_require_admin(client):
    user_service.create_user("plain", "plainpw1")
    h = _h(client, "plain", "plainpw1")
    assert client.get("/api/admin/stats/users", headers=h).status_code == 403
