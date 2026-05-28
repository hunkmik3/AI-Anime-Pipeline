"""Phase 2: /api/projects/{id}/scenes + /api/scenes/* endpoint tests."""
from __future__ import annotations

import uuid

from tests.conftest import make_shot


def _make_project(client, name: str = "T") -> str:
    return client.post("/api/projects", json={"name": name}).json()["id"]


def test_create_scene_under_project(client):
    pid = _make_project(client)
    r = client.post(
        f"/api/projects/{pid}/scenes",
        json={"name": "Opening"},
    )
    assert r.status_code == 200, r.text
    scene = r.json()
    assert scene["project_id"] == pid
    assert scene["name"] == "Opening"
    # Phase 8.3: Scene Bible removed; new scenes start with empty canvas_state.
    assert scene["canvas_state"] == {}
    # First scene under a fresh project defaults to order_index 0.
    assert scene["order_index"] == 0


def test_scene_order_index_auto_increments(client):
    pid = _make_project(client)
    a = client.post(f"/api/projects/{pid}/scenes", json={"name": "A"}).json()
    b = client.post(f"/api/projects/{pid}/scenes", json={"name": "B"}).json()
    c = client.post(f"/api/projects/{pid}/scenes", json={"name": "C"}).json()
    assert [a["order_index"], b["order_index"], c["order_index"]] == [0, 1, 2]


def test_scene_explicit_order_index_respected(client):
    pid = _make_project(client)
    r = client.post(
        f"/api/projects/{pid}/scenes",
        json={"name": "Slot 5", "order_index": 5},
    )
    assert r.json()["order_index"] == 5


def test_list_scenes_orders_by_order_index(client):
    pid = _make_project(client)
    a = client.post(
        f"/api/projects/{pid}/scenes", json={"name": "A", "order_index": 2}
    ).json()
    b = client.post(
        f"/api/projects/{pid}/scenes", json={"name": "B", "order_index": 0}
    ).json()
    c = client.post(
        f"/api/projects/{pid}/scenes", json={"name": "C", "order_index": 1}
    ).json()
    r = client.get(f"/api/projects/{pid}/scenes")
    assert r.status_code == 200
    listing = r.json()
    assert [s["id"] for s in listing] == [b["id"], c["id"], a["id"]]


def test_list_scenes_under_missing_project_404(client):
    r = client.get(f"/api/projects/{uuid.uuid4()}/scenes")
    assert r.status_code == 404


def test_create_scene_under_missing_project_404(client):
    r = client.post(f"/api/projects/{uuid.uuid4()}/scenes", json={"name": "x"})
    assert r.status_code == 404


def test_get_scene_includes_shot_count(client):
    """make_shot creates Project+Scene+Shot together; reuse it to seed a
    scene-with-shots situation and assert shot_count > 0."""
    b = make_shot(client, name="board")
    # Pull the project + first scene back.
    project_id = b["project_id"]
    scenes = client.get(f"/api/projects/{project_id}/scenes").json()
    assert len(scenes) == 1
    sid = scenes[0]["id"]
    r = client.get(f"/api/scenes/{sid}")
    assert r.status_code == 200
    detail = r.json()
    assert detail["id"] == sid
    assert detail["shot_count"] == 1  # make_shot seeds 1 shot per project


def test_get_missing_scene_404(client):
    assert client.get(f"/api/scenes/{uuid.uuid4()}").status_code == 404


def test_patch_scene_updates_fields(client):
    pid = _make_project(client)
    scene = client.post(
        f"/api/projects/{pid}/scenes", json={"name": "Old"}
    ).json()
    r = client.patch(
        f"/api/scenes/{scene['id']}",
        json={"name": "New", "order_index": 9},
    )
    assert r.status_code == 200
    out = r.json()
    assert out["name"] == "New"
    assert out["order_index"] == 9


def test_patch_missing_scene_404(client):
    r = client.patch(f"/api/scenes/{uuid.uuid4()}", json={"name": "x"})
    assert r.status_code == 404


def test_delete_scene_cascades_shots_and_nodes(client):
    """DELETE /api/scenes/{id} must cascade Shot → Node/Edge."""
    from sqlmodel import select

    from flowboard.db import get_session
    from flowboard.db.models import Edge, Node, Scene, Shot

    b = make_shot(client, name="tree")
    project_id = b["project_id"]
    scene_id = client.get(f"/api/projects/{project_id}/scenes").json()[0]["id"]
    n1 = client.post("/api/nodes", json={"shot_id": b["id"], "type": "image"}).json()
    n2 = client.post("/api/nodes", json={"shot_id": b["id"], "type": "video"}).json()
    client.post(
        "/api/edges",
        json={"shot_id": b["id"], "source_id": n1["id"], "target_id": n2["id"]},
    )

    r = client.delete(f"/api/scenes/{scene_id}")
    assert r.status_code == 200
    assert r.json() == {"deleted": scene_id}

    with get_session() as s:
        scene_uuid = uuid.UUID(scene_id)
        shot_uuid = uuid.UUID(b["id"])
        assert (
            s.exec(select(Scene).where(Scene.id == scene_uuid)).all() == []
        )
        assert s.exec(select(Shot).where(Shot.id == shot_uuid)).all() == []
        assert s.exec(select(Node).where(Node.shot_id == shot_uuid)).all() == []
        assert s.exec(select(Edge).where(Edge.shot_id == shot_uuid)).all() == []


def test_delete_missing_scene_404(client):
    r = client.delete(f"/api/scenes/{uuid.uuid4()}")
    assert r.status_code == 404


def test_compose_scene_returns_501(client):
    pid = _make_project(client)
    scene = client.post(f"/api/projects/{pid}/scenes", json={"name": "S"}).json()
    r = client.post(f"/api/scenes/{scene['id']}/compose")
    assert r.status_code == 501


def test_compose_missing_scene_404(client):
    r = client.post(f"/api/scenes/{uuid.uuid4()}/compose")
    assert r.status_code == 404
