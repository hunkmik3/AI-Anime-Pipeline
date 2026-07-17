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


def _node_on_shot(client, shot_id, title="Clip") -> int:
    return client.post(
        "/api/nodes",
        json={"shot_id": shot_id, "type": "video", "x": 0, "y": 0, "data": {"title": title}},
    ).json()["id"]


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


def test_project_shots_and_shot_gens(client):
    """Per-shot cost tracking: a shot aggregates its nodes' takes; the gen
    drill-down lists every take with cost + kept/wasted + who ran it."""
    u = user_service.create_user("shotter", "shotterpw1")
    b = make_shot(client)  # → {id: shot uuid, project_id, scene_id}
    shot_id, project_id = b["id"], b["project_id"]
    node = client.post(
        "/api/nodes",
        json={"shot_id": shot_id, "type": "video", "x": 0, "y": 0, "data": {"title": "Clip A"}},
    ).json()["id"]
    _gen(client, u.id, node, 3.0)  # take 1 — re-rolled (wasted)
    _gen(client, u.id, node, 2.0)  # take 2 — kept

    shots = stats_service.project_shots(project_id)
    row = next(r for r in shots if r["shot_id"] == shot_id)
    assert row["total_usd"] == 5.0
    assert row["wasted_usd"] == 3.0
    assert row["kept_usd"] == 2.0
    assert row["clips"] == 1 and row["takes"] == 2

    gens = stats_service.shot_gens(shot_id)
    assert len(gens) == 2
    assert round(sum(g["cost_usd"] for g in gens), 2) == 5.0
    assert [g["kept"] for g in gens].count(True) == 1
    assert gens[-1]["kept"] is True  # last take on the node = kept
    assert all(g["user_name"] == "shotter" for g in gens)


def test_project_shots_lists_zero_cost_shots(client):
    """Every shot appears, even ones that never generated (spend $0)."""
    b = make_shot(client)
    rows = stats_service.project_shots(b["project_id"])
    assert len(rows) == 1
    assert rows[0]["total_usd"] == 0.0 and rows[0]["clips"] == 0


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


# ── cost tree: project → episode → sequence ──────────────────────────────────


def test_cost_tree_project_episode_sequence(client):
    """The admin Cost tab tree rolls settled Avis spend up at every level:
    project total → per-episode total → per-sequence spend."""
    proj = client.post("/api/projects", json={"name": "Anime P1"}).json()
    e1 = client.post(f"/api/projects/{proj['id']}/scenes", json={"name": "Episode 1"}).json()
    e2 = client.post(f"/api/projects/{proj['id']}/scenes", json={"name": "Episode 2"}).json()
    s1 = client.post(f"/api/scenes/{e1['id']}/shots", json={}).json()
    s2 = client.post(f"/api/scenes/{e2['id']}/shots", json={}).json()

    u = user_service.create_user("tree", "treepw123")
    n1 = _node_on_shot(client, s1["id"])
    n2 = _node_on_shot(client, s2["id"])
    _gen(client, u.id, n1, 6.0)   # take 1 on episode-1 / sequence-1
    _gen(client, u.id, n1, 2.0)   # take 2 on same node → 1 clip, 2 gens
    _gen(client, u.id, n2, 3.0)   # episode-2 / sequence-1

    p = next(x for x in stats_service.cost_tree() if x["project_id"] == proj["id"])
    assert p["total_usd"] == 11.0
    assert len(p["episodes"]) == 2
    # episodes keep story order (E1 before E2)
    assert [e["scene_id"] for e in p["episodes"]] == [e1["id"], e2["id"]]

    ep1 = next(e for e in p["episodes"] if e["scene_id"] == e1["id"])
    ep2 = next(e for e in p["episodes"] if e["scene_id"] == e2["id"])
    assert ep1["total_usd"] == 8.0 and ep2["total_usd"] == 3.0

    seq1 = ep1["sequences"][0]
    assert seq1["shot_id"] == s1["id"]
    assert seq1["total_usd"] == 8.0 and seq1["gens"] == 2 and seq1["clips"] == 1
    assert "_order" not in seq1  # internal sort key is stripped from the payload


def test_cost_tree_skips_zero_spend_branches(client):
    """A project with shots but no generations doesn't clutter the cost tree."""
    make_shot(client)
    assert stats_service.cost_tree() == []


def test_admin_cost_tree_route(client):
    user_service.create_user("boss", "bosspw123", role="admin")
    u = user_service.create_user("hand", "handpw123")
    _gen(client, u.id, _video_node(client), 4.0)
    h = _h(client, "boss", "bosspw123")
    r = client.get("/api/admin/stats/cost-tree", headers=h)
    assert r.status_code == 200
    tree = r.json()
    assert len(tree) == 1 and tree[0]["total_usd"] == 4.0
    assert tree[0]["episodes"][0]["sequences"][0]["total_usd"] == 4.0


def test_stats_require_admin(client):
    user_service.create_user("plain", "plainpw1")
    h = _h(client, "plain", "plainpw1")
    assert client.get("/api/admin/stats/users", headers=h).status_code == 403
