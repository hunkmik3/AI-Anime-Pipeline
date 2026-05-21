"""Phase 2: top-level /api/projects/* endpoint tests."""
from __future__ import annotations

import uuid

from tests.conftest import make_shot


def test_create_list_get_project(client):
    r = client.post("/api/projects", json={"name": "Anime 01"})
    assert r.status_code == 200, r.text
    project = r.json()
    assert project["name"] == "Anime 01"
    pid = uuid.UUID(project["id"])
    assert project["project_bible"] == {}
    assert project["settings"] == {}

    r = client.get("/api/projects")
    assert r.status_code == 200
    listing = r.json()
    assert any(p["id"] == str(pid) for p in listing)

    r = client.get(f"/api/projects/{pid}")
    assert r.status_code == 200
    detail = r.json()
    assert detail["id"] == str(pid)
    assert detail["scene_count"] == 0
    assert detail["asset_count"] == 0


def test_create_project_with_bible_and_settings(client):
    body = {
        "name": "Bible Anime",
        "project_bible": {
            "art_style": "cel-shaded 2D",
            "color_palette": ["#FF0", "#000"],
            "line_style": "thin ink",
            "lighting_conventions": "rim light",
            "negative_prompts": ["3D", "photoreal"],
            "style_anchor_asset_ids": [],
        },
        "settings": {"default_video_provider": "flow"},
    }
    r = client.post("/api/projects", json=body)
    assert r.status_code == 200, r.text
    proj = r.json()
    assert proj["project_bible"]["art_style"] == "cel-shaded 2D"
    assert proj["settings"] == {"default_video_provider": "flow"}


def test_create_project_bible_rejects_extra_keys(client):
    body = {
        "name": "Bad Bible",
        "project_bible": {
            "art_style": "x",
            "color_palette": [],
            "line_style": "",
            "lighting_conventions": "",
            "negative_prompts": [],
            "style_anchor_asset_ids": [],
            "secret_smuggle": "bad",
        },
    }
    r = client.post("/api/projects", json=body)
    assert r.status_code == 422


def test_create_project_rejects_blank_name(client):
    r = client.post("/api/projects", json={"name": ""})
    assert r.status_code == 422


def test_get_missing_project_returns_404(client):
    r = client.get(f"/api/projects/{uuid.uuid4()}")
    assert r.status_code == 404


def test_patch_project_updates_name_and_settings(client):
    p = client.post("/api/projects", json={"name": "Old"}).json()
    r = client.patch(
        f"/api/projects/{p['id']}",
        json={"name": "New", "settings": {"k": "v"}},
    )
    assert r.status_code == 200
    proj = r.json()
    assert proj["name"] == "New"
    assert proj["settings"] == {"k": "v"}


def test_patch_project_settings_only_doesnt_clobber_name(client):
    p = client.post("/api/projects", json={"name": "Keep"}).json()
    r = client.patch(f"/api/projects/{p['id']}", json={"settings": {"a": 1}})
    assert r.status_code == 200
    assert r.json()["name"] == "Keep"
    assert r.json()["settings"] == {"a": 1}


def test_patch_missing_project_returns_404(client):
    r = client.patch(f"/api/projects/{uuid.uuid4()}", json={"name": "x"})
    assert r.status_code == 404


def test_delete_project_cascades_full_tree(client):
    """Project delete must clear scene → shot → node/edge across the tree,
    plus chat / plans / pipeline runs / Flow mapping.
    """
    from sqlmodel import select

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

    # Project+Scene+Shot pyramid via the new REST surface.
    b = make_shot(client, name="doomed")
    shot_uuid = uuid.UUID(b["id"])
    project_uuid = uuid.UUID(b["project_id"])

    n1 = client.post("/api/nodes", json={"shot_id": b["id"], "type": "image"}).json()
    n2 = client.post("/api/nodes", json={"shot_id": b["id"], "type": "video"}).json()
    client.post(
        "/api/edges",
        json={"shot_id": b["id"], "source_id": n1["id"], "target_id": n2["id"]},
    )
    client.post(
        "/api/requests",
        json={
            "node_id": n1["id"],
            "type": "proxy",
            "params": {"url": "https://aisandbox-pa.googleapis.com/v1/x"},
        },
    )
    with get_session() as s:
        s.add(
            Asset(
                uuid_media_id="ffffffff-1111-2222-3333-444444444444",
                node_id=n1["id"],
                project_id=project_uuid,
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
        s.add(ProjectFlowMapping(project_id=project_uuid, flow_project_id="ffff1111"))
        s.commit()

    r = client.delete(f"/api/projects/{project_uuid}")
    assert r.status_code == 200, r.text
    assert r.json() == {"deleted": str(project_uuid)}

    # Detail 404s.
    assert client.get(f"/api/projects/{project_uuid}").status_code == 404

    with get_session() as s:
        for table, where in [
            (Project, Project.id == project_uuid),
            (Scene, Scene.project_id == project_uuid),
            (Shot, Shot.id == shot_uuid),
            (Node, Node.shot_id == shot_uuid),
            (Edge, Edge.shot_id == shot_uuid),
            (ChatMessage, ChatMessage.project_id == project_uuid),
            (Plan, Plan.shot_id == shot_uuid),
            (ProjectFlowMapping, ProjectFlowMapping.project_id == project_uuid),
        ]:
            rows = s.exec(select(table).where(where)).all()
            assert rows == [], f"{table.__name__} not cleared: {rows}"
        # Asset / Request referenced nodes that no longer exist.
        assert (
            s.exec(select(Asset).where(Asset.node_id.in_([n1["id"], n2["id"]]))).all()
            == []
        )
        assert (
            s.exec(
                select(Request).where(Request.node_id.in_([n1["id"], n2["id"]]))
            ).all()
            == []
        )


def test_delete_missing_project_returns_404(client):
    r = client.delete(f"/api/projects/{uuid.uuid4()}")
    assert r.status_code == 404


def test_project_cost_zero_when_no_jobs(client):
    p = client.post("/api/projects", json={"name": "no jobs"}).json()
    r = client.get(f"/api/projects/{p['id']}/cost")
    assert r.status_code == 200
    assert r.json() == {"cost_usd": 0.0}


def test_project_cost_sums_requests_across_shots(client):
    """Cost rollup walks project → scene → shot → node → request."""
    from flowboard.db import get_session
    from flowboard.db.models import Request

    b = make_shot(client, name="cost")
    project_id = b["project_id"]
    n = client.post("/api/nodes", json={"shot_id": b["id"], "type": "image"}).json()

    with get_session() as s:
        s.add(
            Request(
                node_id=n["id"],
                type="gen_image",
                params={},
                status="done",
                result={"cost_usd": 0.0123},
            )
        )
        s.add(
            Request(
                node_id=n["id"],
                type="gen_image",
                params={},
                status="done",
                result={"cost_usd": 0.5},
            )
        )
        # Junk row with no cost_usd — must be ignored.
        s.add(
            Request(
                node_id=n["id"],
                type="proxy",
                params={},
                status="done",
                result={"foo": "bar"},
            )
        )
        s.commit()

    r = client.get(f"/api/projects/{project_id}/cost")
    assert r.status_code == 200
    assert r.json()["cost_usd"] == 0.5123


def test_project_cost_missing_project_returns_404(client):
    r = client.get(f"/api/projects/{uuid.uuid4()}/cost")
    assert r.status_code == 404


def test_project_chat_lists_messages(client):
    from flowboard.db import get_session
    from flowboard.db.models import ChatMessage

    b = make_shot(client, name="chat")
    project_id = b["project_id"]
    with get_session() as s:
        s.add(ChatMessage(project_id=uuid.UUID(project_id), role="user", content="hi"))
        s.add(
            ChatMessage(project_id=uuid.UUID(project_id), role="assistant", content="hello")
        )
        s.commit()

    r = client.get(f"/api/projects/{project_id}/chat")
    assert r.status_code == 200
    msgs = r.json()
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant"


def test_project_chat_missing_project_returns_404(client):
    r = client.get(f"/api/projects/{uuid.uuid4()}/chat")
    assert r.status_code == 404


def test_flow_project_get_404_when_unbound(client):
    p = client.post("/api/projects", json={"name": "unbound"}).json()
    r = client.get(f"/api/projects/{p['id']}/flow-project")
    assert r.status_code == 404


def test_flow_project_get_returns_binding_when_present(client):
    """The GET path requires no extension round-trip, so we can seed
    the mapping table directly and exercise the read path."""
    from flowboard.db import get_session
    from flowboard.db.models import ProjectFlowMapping

    p = client.post("/api/projects", json={"name": "bound"}).json()
    with get_session() as s:
        s.add(
            ProjectFlowMapping(
                project_id=uuid.UUID(p["id"]),
                flow_project_id="extABCD",
            )
        )
        s.commit()

    r = client.get(f"/api/projects/{p['id']}/flow-project")
    assert r.status_code == 200
    assert r.json() == {"flow_project_id": "extABCD", "created": False}


def test_flow_project_get_404_for_missing_project(client):
    r = client.get(f"/api/projects/{uuid.uuid4()}/flow-project")
    assert r.status_code == 404
