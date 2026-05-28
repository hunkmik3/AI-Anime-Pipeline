"""Phase 8.3a — multi-shot SceneCanvas backend: canvas_state.shot_groups,
auto-migration, shot-group PATCH, scene-canvas aggregate GET.
"""
from __future__ import annotations

from tests.conftest import make_shot


def _second_shot(client, scene_id: str) -> str:
    return client.post(f"/api/scenes/{scene_id}/shots", json={}).json()["id"]


# ── Scene Bible removed ───────────────────────────────────────────────────


def test_scene_has_canvas_state_not_bible(client):
    b = make_shot(client)
    scene = client.get(f"/api/scenes/{b['scene_id']}").json()
    assert "scene_bible_text" not in scene
    assert scene["canvas_state"] == {}


# ── auto-migrate ──────────────────────────────────────────────────────────


def test_auto_migrate_creates_one_group_per_shot(client):
    b = make_shot(client)
    _second_shot(client, b["scene_id"])
    r = client.post(f"/api/scenes/{b['scene_id']}/auto-migrate")
    assert r.status_code == 200, r.text
    groups = r.json()["shot_groups"]
    assert len(groups) == 2
    # Vertical stack: distinct y, ascending with order.
    ys = [g["position"]["y"] for g in groups]
    assert ys[0] < ys[1]
    assert all(g["collapsed"] is False for g in groups)
    assert {g["label"] for g in groups} == {"Shot 1", "Shot 2"}


def test_auto_migrate_idempotent(client):
    b = make_shot(client)
    client.post(f"/api/scenes/{b['scene_id']}/auto-migrate")
    r2 = client.post(f"/api/scenes/{b['scene_id']}/auto-migrate")
    assert len(r2.json()["shot_groups"]) == 1  # not duplicated


def test_auto_migrate_preserves_moved_group(client):
    """A user-moved group is not clobbered when a new shot is migrated in."""
    b = make_shot(client)
    client.post(f"/api/scenes/{b['scene_id']}/auto-migrate")
    client.patch(f"/api/shots/{b['id']}/group", json={"position": {"x": 999, "y": 888}})
    # Add a 2nd shot, re-migrate.
    _second_shot(client, b["scene_id"])
    r = client.post(f"/api/scenes/{b['scene_id']}/auto-migrate")
    groups = {g["shot_id"]: g for g in r.json()["shot_groups"]}
    assert groups[b["id"]]["position"] == {"x": 999, "y": 888}  # preserved
    assert len(groups) == 2


def test_auto_migrate_missing_scene_404(client):
    import uuid
    r = client.post(f"/api/scenes/{uuid.uuid4()}/auto-migrate")
    assert r.status_code == 404


# ── shot-group PATCH ──────────────────────────────────────────────────────


def test_patch_shot_group_persists(client):
    b = make_shot(client)
    r = client.patch(
        f"/api/shots/{b['id']}/group",
        json={"position": {"x": 50, "y": 600}, "collapsed": True, "label": "Opening"},
    )
    assert r.status_code == 200, r.text
    g = r.json()
    assert g["shot_id"] == b["id"]
    assert g["position"] == {"x": 50, "y": 600}
    assert g["collapsed"] is True
    assert g["label"] == "Opening"
    # Reflected in scene.canvas_state via the scene detail.
    scene = client.get(f"/api/scenes/{b['scene_id']}").json()
    sg = scene["canvas_state"]["shot_groups"]
    assert sg[0]["shot_id"] == b["id"] and sg[0]["collapsed"] is True


def test_patch_shot_group_partial_keeps_other_fields(client):
    b = make_shot(client)
    client.patch(f"/api/shots/{b['id']}/group", json={"label": "A", "collapsed": True})
    r = client.patch(f"/api/shots/{b['id']}/group", json={"collapsed": False})
    g = r.json()
    assert g["collapsed"] is False
    assert g["label"] == "A"  # untouched


def test_patch_shot_group_missing_shot_404(client):
    import uuid
    r = client.patch(f"/api/shots/{uuid.uuid4()}/group", json={"collapsed": True})
    assert r.status_code == 404


# ── scene-canvas aggregate GET ────────────────────────────────────────────


def test_get_scene_canvas_aggregates_shots_nodes_edges_groups(client):
    b = make_shot(client)
    # two nodes + an edge in the shot
    n1 = client.post("/api/nodes", json={"shot_id": b["id"], "type": "character"}).json()
    n2 = client.post("/api/nodes", json={"shot_id": b["id"], "type": "video"}).json()
    client.post("/api/edges", json={"shot_id": b["id"], "source_id": n1["id"], "target_id": n2["id"]})
    client.post(f"/api/scenes/{b['scene_id']}/auto-migrate")

    r = client.get(f"/api/scenes/{b['scene_id']}/canvas")
    assert r.status_code == 200, r.text
    canvas = r.json()
    assert canvas["scene_id"] == b["scene_id"]
    assert len(canvas["shots"]) == 1
    assert len(canvas["nodes"]) == 2
    assert len(canvas["edges"]) == 1
    assert len(canvas["shot_groups"]) == 1
    # every node carries its shot_id so the frontend can group by it
    assert all(nd["shot_id"] == b["id"] for nd in canvas["nodes"])


def test_get_scene_canvas_missing_scene_404(client):
    import uuid
    r = client.get(f"/api/scenes/{uuid.uuid4()}/canvas")
    assert r.status_code == 404
