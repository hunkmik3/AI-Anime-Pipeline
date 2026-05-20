import uuid


def test_create_list_get_board(client):
    r = client.post("/api/boards", json={"name": "Scene 01"})
    assert r.status_code == 200
    board = r.json()
    assert board["name"] == "Scene 01"
    # board.id is the Shot UUID under the Phase 1 shim.
    assert isinstance(board["id"], str)
    uuid.UUID(board["id"])  # raises ValueError on malformed → fail test
    assert isinstance(board["project_id"], str)

    r = client.get("/api/boards")
    assert r.status_code == 200
    listing = r.json()
    assert any(b["id"] == board["id"] for b in listing)

    r = client.get(f"/api/boards/{board['id']}")
    assert r.status_code == 200
    detail = r.json()
    assert detail["board"]["id"] == board["id"]
    assert detail["nodes"] == []
    assert detail["edges"] == []


def test_get_missing_board_returns_404(client):
    r = client.get(f"/api/boards/{uuid.uuid4()}")
    assert r.status_code == 404


def test_patch_board_rename(client):
    b = client.post("/api/boards", json={"name": "Old"}).json()
    r = client.patch(f"/api/boards/{b['id']}", json={"name": "New"})
    assert r.status_code == 200
    assert r.json()["name"] == "New"

    # persistence
    r = client.get(f"/api/boards/{b['id']}")
    assert r.json()["board"]["name"] == "New"


def test_patch_missing_board_returns_404(client):
    r = client.patch(f"/api/boards/{uuid.uuid4()}", json={"name": "x"})
    assert r.status_code == 404


def test_delete_board_cascades_children(client):
    """DELETE /api/boards/{id} must remove every child row that references
    the board's project tree. Project CASCADE handles most; we also
    explicitly clear plan/pipelinerun/projectflowmapping so the assertion
    can verify."""
    from flowboard.db import get_session
    from flowboard.db.models import (
        Asset,
        ChatMessage,
        Edge,
        Node,
        PipelineRun,
        Plan,
        PlanRevision,
        Project,
        ProjectFlowMapping,
        Request,
        Scene,
        Shot,
    )
    from sqlmodel import select

    b = client.post("/api/boards", json={"name": "to-be-deleted"}).json()
    shot_uuid = uuid.UUID(b["id"])
    project_uuid = uuid.UUID(b["project_id"])

    # Seed: 2 nodes + 1 edge + 1 request + 1 asset + 1 chat + 1 plan with
    # 1 revision + 1 pipeline run + Flow-project mapping.
    n1 = client.post("/api/nodes", json={"shot_id": b["id"], "type": "image"}).json()
    n2 = client.post("/api/nodes", json={"shot_id": b["id"], "type": "video"}).json()
    client.post(
        "/api/edges",
        json={"shot_id": b["id"], "source_id": n1["id"], "target_id": n2["id"]},
    ).json()
    client.post(
        "/api/requests",
        json={
            "node_id": n1["id"],
            "type": "proxy",
            "params": {"url": "https://aisandbox-pa.googleapis.com/v1/x"},
        },
    ).json()
    with get_session() as s:
        s.add(
            Asset(
                uuid_media_id="11111111-2222-3333-4444-555555555555",
                node_id=n1["id"],
                kind="image",
            )
        )
        s.add(ChatMessage(project_id=project_uuid, role="user", content="hi"))
        plan = Plan(shot_id=shot_uuid, spec={"k": "v"})
        s.add(plan)
        s.commit()
        s.refresh(plan)
        s.add(PlanRevision(plan_id=plan.id, rev_no=1, spec={}, edits={}))
        s.add(PipelineRun(plan_id=plan.id, status="pending"))
        s.add(
            ProjectFlowMapping(project_id=project_uuid, flow_project_id="fpfpfpfp")
        )
        s.commit()

    # Delete.
    r = client.delete(f"/api/boards/{b['id']}")
    assert r.status_code == 200, r.text
    assert r.json() == {"deleted": b["id"]}

    # Board itself gone.
    assert client.get(f"/api/boards/{b['id']}").status_code == 404

    # Every child table swept.
    with get_session() as s:
        for table, where in [
            (Node, Node.shot_id == shot_uuid),
            (Edge, Edge.shot_id == shot_uuid),
            (ChatMessage, ChatMessage.project_id == project_uuid),
            (Plan, Plan.shot_id == shot_uuid),
            (
                ProjectFlowMapping,
                ProjectFlowMapping.project_id == project_uuid,
            ),
            (Shot, Shot.id == shot_uuid),
            (Scene, Scene.project_id == project_uuid),
            (Project, Project.id == project_uuid),
        ]:
            rows = s.exec(select(table).where(where)).all()
            assert rows == [], f"{table.__name__} not cleared: {rows}"
        # Asset / Request reference node_id, which no longer exists.
        assert s.exec(select(Asset).where(Asset.node_id.in_([n1["id"], n2["id"]]))).all() == []
        assert s.exec(select(Request).where(Request.node_id.in_([n1["id"], n2["id"]]))).all() == []


def test_delete_missing_board_returns_404(client):
    r = client.delete(f"/api/boards/{uuid.uuid4()}")
    assert r.status_code == 404
