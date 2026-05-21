"""Phase 2: /api/scenes/{id}/shots + /api/shots/* tests."""
from __future__ import annotations

import uuid

from tests.conftest import make_shot as _make_shot  # noqa: F401


def _project(client, name: str = "P") -> str:
    return client.post("/api/projects", json={"name": name}).json()["id"]


def _scene(client, project_id: str, name: str = "S") -> str:
    return client.post(
        f"/api/projects/{project_id}/scenes", json={"name": name}
    ).json()["id"]


def test_create_shot_under_scene(client):
    pid = _project(client)
    sid = _scene(client, pid)
    r = client.post(f"/api/scenes/{sid}/shots", json={"script_text": "shot 1 script"})
    assert r.status_code == 200, r.text
    shot = r.json()
    uuid.UUID(shot["id"])
    assert shot["scene_id"] == sid
    assert shot["order_index"] == 0
    assert shot["script_text"] == "shot 1 script"
    assert shot["status"] == "idle"


def test_shot_order_index_auto_increments(client):
    pid = _project(client)
    sid = _scene(client, pid)
    a = client.post(f"/api/scenes/{sid}/shots", json={}).json()
    b = client.post(f"/api/scenes/{sid}/shots", json={}).json()
    c = client.post(f"/api/scenes/{sid}/shots", json={}).json()
    assert [a["order_index"], b["order_index"], c["order_index"]] == [0, 1, 2]


def test_list_shots_orders_by_order_index(client):
    pid = _project(client)
    sid = _scene(client, pid)
    a = client.post(f"/api/scenes/{sid}/shots", json={"order_index": 2}).json()
    b = client.post(f"/api/scenes/{sid}/shots", json={"order_index": 0}).json()
    c = client.post(f"/api/scenes/{sid}/shots", json={"order_index": 1}).json()
    r = client.get(f"/api/scenes/{sid}/shots")
    assert r.status_code == 200
    listing = r.json()
    assert [s["id"] for s in listing] == [b["id"], c["id"], a["id"]]


def test_create_shot_under_missing_scene_404(client):
    r = client.post(f"/api/scenes/{uuid.uuid4()}/shots", json={})
    assert r.status_code == 404


def test_get_shot(client):
    pid = _project(client)
    sid = _scene(client, pid)
    shot = client.post(f"/api/scenes/{sid}/shots", json={}).json()
    r = client.get(f"/api/shots/{shot['id']}")
    assert r.status_code == 200
    assert r.json()["id"] == shot["id"]


def test_get_missing_shot_404(client):
    r = client.get(f"/api/shots/{uuid.uuid4()}")
    assert r.status_code == 404


def test_patch_shot_updates_script_and_status(client):
    pid = _project(client)
    sid = _scene(client, pid)
    shot = client.post(f"/api/scenes/{sid}/shots", json={}).json()
    r = client.patch(
        f"/api/shots/{shot['id']}",
        json={"script_text": "new script", "status": "awaiting_approval"},
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["script_text"] == "new script"
    assert out["status"] == "awaiting_approval"


def test_patch_shot_rejects_invalid_status(client):
    pid = _project(client)
    sid = _scene(client, pid)
    shot = client.post(f"/api/scenes/{sid}/shots", json={}).json()
    r = client.patch(f"/api/shots/{shot['id']}", json={"status": "bogus"})
    assert r.status_code == 422


def test_patch_empty_body_is_noop(client):
    """Empty PATCH must not mutate timestamps or status."""
    pid = _project(client)
    sid = _scene(client, pid)
    shot = client.post(f"/api/scenes/{sid}/shots", json={}).json()
    r = client.patch(f"/api/shots/{shot['id']}", json={})
    assert r.status_code == 200
    assert r.json()["status"] == "idle"


def test_patch_missing_shot_404(client):
    r = client.patch(f"/api/shots/{uuid.uuid4()}", json={"script_text": "x"})
    assert r.status_code == 404


def test_delete_shot_cascades_nodes_edges(client):
    """DELETE /api/shots/{id} cascades to Nodes + Edges within."""
    from sqlmodel import select

    from flowboard.db import get_session
    from flowboard.db.models import Edge, Node, Shot

    b = _make_shot(client, name="shot-cascade")
    shot_id = b["id"]
    n1 = client.post("/api/nodes", json={"shot_id": shot_id, "type": "image"}).json()
    n2 = client.post("/api/nodes", json={"shot_id": shot_id, "type": "video"}).json()
    client.post(
        "/api/edges",
        json={"shot_id": shot_id, "source_id": n1["id"], "target_id": n2["id"]},
    )

    r = client.delete(f"/api/shots/{shot_id}")
    assert r.status_code == 200

    with get_session() as s:
        su = uuid.UUID(shot_id)
        assert s.exec(select(Shot).where(Shot.id == su)).all() == []
        assert s.exec(select(Node).where(Node.shot_id == su)).all() == []
        assert s.exec(select(Edge).where(Edge.shot_id == su)).all() == []


def test_delete_missing_shot_404(client):
    r = client.delete(f"/api/shots/{uuid.uuid4()}")
    assert r.status_code == 404


def test_workflow_get_returns_existing_graph(client):
    """The legacy board shim seeds a shot; we attach nodes via /api/nodes
    and observe them through the new workflow GET."""
    b = _make_shot(client, name="graph")
    n1 = client.post("/api/nodes", json={"shot_id": b["id"], "type": "image"}).json()
    n2 = client.post("/api/nodes", json={"shot_id": b["id"], "type": "video"}).json()
    client.post(
        "/api/edges",
        json={"shot_id": b["id"], "source_id": n1["id"], "target_id": n2["id"]},
    )
    r = client.get(f"/api/shots/{b['id']}/workflow")
    assert r.status_code == 200
    body = r.json()
    assert len(body["nodes"]) == 2
    assert len(body["edges"]) == 1


def test_workflow_get_missing_shot_404(client):
    r = client.get(f"/api/shots/{uuid.uuid4()}/workflow")
    assert r.status_code == 404


def test_workflow_put_snapshot_replaces_graph(client):
    """PUT /workflow wipes the old graph and recreates from payload.
    Edges resolve by short_id between the new nodes."""
    b = _make_shot(client, name="snapshot")
    # Seed an existing node so we can confirm it gets wiped.
    pre = client.post("/api/nodes", json={"shot_id": b["id"], "type": "note"}).json()

    r = client.put(
        f"/api/shots/{b['id']}/workflow",
        json={
            "nodes": [
                {"short_id": "aa01", "type": "image", "x": 100, "y": 100},
                {"short_id": "aa02", "type": "video", "x": 400, "y": 100},
            ],
            "edges": [
                {"source_id": "aa01", "target_id": "aa02", "kind": "ref"},
            ],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["nodes"]) == 2
    short_ids = {n["short_id"] for n in body["nodes"]}
    assert short_ids == {"aa01", "aa02"}
    assert len(body["edges"]) == 1

    # The previously-seeded node is gone.
    follow = client.get(f"/api/shots/{b['id']}/workflow").json()
    assert all(n["id"] != pre["id"] for n in follow["nodes"])


def test_workflow_put_rejects_node_without_type(client):
    b = _make_shot(client, name="bad")
    r = client.put(
        f"/api/shots/{b['id']}/workflow",
        json={"nodes": [{"short_id": "xx99"}], "edges": []},
    )
    assert r.status_code == 400


def test_workflow_put_missing_shot_404(client):
    r = client.put(
        f"/api/shots/{uuid.uuid4()}/workflow",
        json={"nodes": [], "edges": []},
    )
    assert r.status_code == 404


def test_run_shot_sets_status_running(client):
    pid = _project(client)
    sid = _scene(client, pid)
    shot = client.post(f"/api/scenes/{sid}/shots", json={}).json()
    assert shot["status"] == "idle"
    r = client.post(f"/api/shots/{shot['id']}/run")
    assert r.status_code == 200
    assert r.json()["status"] == "running"


def test_run_missing_shot_404(client):
    r = client.post(f"/api/shots/{uuid.uuid4()}/run")
    assert r.status_code == 404


def test_cancel_shot_resets_status_and_marks_requests(client):
    """cancel must set status=idle and fail any queued/running Request rows
    tied to the shot's nodes."""
    from flowboard.db import get_session
    from flowboard.db.models import Request

    b = _make_shot(client, name="cancel")
    n = client.post("/api/nodes", json={"shot_id": b["id"], "type": "image"}).json()
    with get_session() as s:
        s.add(Request(node_id=n["id"], type="gen_image", params={}, status="queued"))
        s.add(Request(node_id=n["id"], type="gen_image", params={}, status="running"))
        # A pre-existing done row must NOT be touched.
        s.add(
            Request(
                node_id=n["id"],
                type="gen_image",
                params={},
                status="done",
                result={"ok": True},
            )
        )
        s.commit()

    # First flip to running so cancel actually has work to do.
    client.post(f"/api/shots/{b['id']}/run")

    r = client.post(f"/api/shots/{b['id']}/cancel")
    assert r.status_code == 200
    assert r.json()["status"] == "idle"

    from sqlmodel import select

    with get_session() as s:
        rows = list(s.exec(select(Request).where(Request.node_id == n["id"])).all())
        statuses = sorted(row.status for row in rows)
        # The two pending → failed; the done stays done.
        assert statuses == ["done", "failed", "failed"]
        for row in rows:
            if row.status == "failed":
                assert row.error == "cancelled"


def test_cancel_missing_shot_404(client):
    r = client.post(f"/api/shots/{uuid.uuid4()}/cancel")
    assert r.status_code == 404


def test_jobs_returns_request_rows_for_shot(client):
    from flowboard.db import get_session
    from flowboard.db.models import Request

    b = _make_shot(client, name="jobs")
    n = client.post("/api/nodes", json={"shot_id": b["id"], "type": "image"}).json()
    with get_session() as s:
        s.add(Request(node_id=n["id"], type="gen_image", params={"a": 1}, status="done"))
        s.add(Request(node_id=n["id"], type="gen_image", params={"a": 2}, status="done"))
        s.commit()

    r = client.get(f"/api/shots/{b['id']}/jobs")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2


def test_jobs_missing_shot_404(client):
    r = client.get(f"/api/shots/{uuid.uuid4()}/jobs")
    assert r.status_code == 404
