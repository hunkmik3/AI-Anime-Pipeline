"""Per-node generation history — "what have I already tried on this node,
and what did each attempt cost?"

Unlike the admin shot_gens view this includes failed / in-flight attempts, and
it must never show a charge for money that wasn't actually taken.
"""
from __future__ import annotations

from flowboard.db import get_session
from flowboard.db.models import Request, UsageRecord
from flowboard.services import user_service
from tests.conftest import make_shot


def _login(client, u, p):
    return client.post("/api/account/login", json={"username": u, "password": p})


def _h(client, u, p):
    return {"Authorization": f"Bearer {_login(client, u, p).json()['token']}"}


def _video_node(client, shot_id) -> int:
    return client.post(
        "/api/nodes",
        json={"shot_id": shot_id, "type": "video", "x": 0, "y": 0, "data": {"title": "Clip"}},
    ).json()["id"]


def _take(node_id, user_id, *, cost=None, status="settled", req_status="done",
          media=(), error=None, resolution="720p"):
    """One attempt on a node. `cost=None` → never billed (failed/in-flight)."""
    with get_session() as s:
        req = Request(
            node_id=node_id,
            type="gen_video",
            params={"resolution": resolution, "duration_seconds": 5, "prompt": "a shot"},
            status=req_status,
            result={"media_ids": list(media)},
            error=error,
        )
        s.add(req)
        s.commit()
        s.refresh(req)
        if cost is not None:
            s.add(
                UsageRecord(
                    user_id=user_id,
                    request_id=req.id,
                    kind="video",
                    model="seedance-2-0",
                    estimated_usd=cost,
                    actual_usd=cost,
                    status=status,
                )
            )
            s.commit()
        return req.id


def test_history_lists_every_take_newest_first(client):
    u = user_service.create_user("maker", "makerpw1")
    b = make_shot(client)
    node = _video_node(client, b["id"])
    _take(node, u.id, cost=3.0, media=["m1"])
    _take(node, u.id, cost=2.0, media=["m2"])

    rows = client.get(f"/api/nodes/{node}/history").json()
    assert len(rows) == 2
    # newest first
    assert rows[0]["media_ids"] == ["m2"] and rows[1]["media_ids"] == ["m1"]
    assert rows[0]["cost_usd"] == 2.0 and rows[1]["cost_usd"] == 3.0
    assert rows[0]["user_name"] == "maker"
    assert rows[0]["resolution"] == "720p" and rows[0]["duration_seconds"] == 5
    assert rows[0]["prompt"] == "a shot"


def test_failed_take_shows_the_error_and_no_charge(client):
    """A failed attempt is exactly what you re-open the history to see — and it
    must not be shown as money spent."""
    u = user_service.create_user("unlucky", "unluckypw1")
    b = make_shot(client)
    node = _video_node(client, b["id"])
    _take(node, u.id, cost=5.0, status="released", req_status="failed",
          error="provider exploded")

    row = client.get(f"/api/nodes/{node}/history").json()[0]
    assert row["status"] == "failed"
    assert row["error"] == "provider exploded"
    assert row["cost_usd"] is None            # released ≠ charged
    assert row["ledger_status"] == "released"


def test_reserved_hold_is_not_reported_as_spend(client):
    """An in-flight take holds budget but hasn't been billed yet."""
    u = user_service.create_user("pending", "pendingpw1")
    b = make_shot(client)
    node = _video_node(client, b["id"])
    _take(node, u.id, cost=4.0, status="reserved", req_status="running")

    row = client.get(f"/api/nodes/{node}/history").json()[0]
    assert row["status"] == "running"
    assert row["cost_usd"] is None
    assert row["ledger_status"] == "reserved"


def test_kept_marks_the_take_the_node_is_showing(client):
    u = user_service.create_user("keeper", "keeperpw1")
    b = make_shot(client)
    node = _video_node(client, b["id"])
    _take(node, u.id, cost=3.0, media=["old"])
    _take(node, u.id, cost=2.0, media=["current"])
    client.patch(f"/api/nodes/{node}", json={"data": {"mediaId": "current"}})

    rows = client.get(f"/api/nodes/{node}/history").json()
    by_media = {r["media_ids"][0]: r for r in rows}
    assert by_media["current"]["kept"] is True
    assert by_media["old"]["kept"] is False


def test_history_of_an_untouched_node_is_empty(client):
    b = make_shot(client)
    node = _video_node(client, b["id"])
    assert client.get(f"/api/nodes/{node}/history").json() == []


def test_missing_node_is_404(client):
    assert client.get("/api/nodes/99999999/history").status_code == 404


def test_history_does_not_leak_another_users_spend(client):
    """The history exposes cost — a non-owner must not be able to read it."""
    owner = user_service.create_user("owner", "ownerpw1")
    user_service.create_user("nosy", "nosypw123")
    proj = client.post(
        "/api/projects", json={"name": "P", "owner_user_id": str(owner.id)}
    ).json()
    scene = client.post(f"/api/projects/{proj['id']}/scenes", json={"name": "EP01"}).json()
    shot = client.post(f"/api/scenes/{scene['id']}/shots", json={}).json()
    node = _video_node(client, shot["id"])
    _take(node, owner.id, cost=9.0, media=["secret"])

    nosy = _h(client, "nosy", "nosypw123")
    assert client.get(f"/api/nodes/{node}/history", headers=nosy).status_code == 404

    # ...but the owner sees it
    mine = _h(client, "owner", "ownerpw1")
    rows = client.get(f"/api/nodes/{node}/history", headers=mine).json()
    assert len(rows) == 1 and rows[0]["cost_usd"] == 9.0
