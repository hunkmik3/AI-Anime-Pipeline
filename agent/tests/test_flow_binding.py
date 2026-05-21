"""Phase 4: tests for POST/GET /api/projects/{id}/flow-project — idempotent
Flow project bootstrap.

Pre-Phase-4 these lived at ``/api/boards/{id}/project`` under the legacy
shim (see the removed ``test_board_project.py``); the shim is gone and
the canonical surface is the projects sub-resource. SDK is patched so
tests don't reach a real extension.
"""
from unittest.mock import AsyncMock, patch

import pytest


def _project(client, name: str = "T") -> dict:
    return client.post("/api/projects", json={"name": name}).json()


def test_bootstrap_creates_flow_project_first_time(client):
    async def fake_create(title, tool="PINHOLE"):
        assert title == "Scene 01"
        return {"raw": {"status": 200}, "project_id": "flow-proj-1"}

    p = _project(client, "Scene 01")
    with patch("flowboard.routes.projects.get_flow_sdk") as m:
        m.return_value.create_project = AsyncMock(side_effect=fake_create)
        r = client.post(f"/api/projects/{p['id']}/flow-project")
        assert r.status_code == 200
        body = r.json()
        assert body["flow_project_id"] == "flow-proj-1"
        assert body["created"] is True

        # Second call is idempotent and does NOT re-invoke the SDK.
        r2 = client.post(f"/api/projects/{p['id']}/flow-project")
        assert r2.status_code == 200
        body2 = r2.json()
        assert body2["flow_project_id"] == "flow-proj-1"
        assert body2["created"] is False

        # SDK was only called once.
        assert m.return_value.create_project.await_count == 1


def test_get_flow_project_returns_404_when_unbound(client):
    p = _project(client)
    r = client.get(f"/api/projects/{p['id']}/flow-project")
    assert r.status_code == 404


def test_get_returns_existing_binding(client):
    async def fake_create(title, tool="PINHOLE"):
        return {"raw": {}, "project_id": "pid-x"}

    p = _project(client)
    with patch("flowboard.routes.projects.get_flow_sdk") as m:
        m.return_value.create_project = AsyncMock(side_effect=fake_create)
        client.post(f"/api/projects/{p['id']}/flow-project")

    r = client.get(f"/api/projects/{p['id']}/flow-project")
    assert r.status_code == 200
    assert r.json() == {"flow_project_id": "pid-x", "created": False}


def test_bootstrap_surfaces_sdk_error_as_502(client):
    async def failing_create(title, tool="PINHOLE"):
        return {
            "raw": {"error": "extension_disconnected"},
            "error": "extension_disconnected",
        }

    p = _project(client)
    with patch("flowboard.routes.projects.get_flow_sdk") as m:
        m.return_value.create_project = AsyncMock(side_effect=failing_create)
        r = client.post(f"/api/projects/{p['id']}/flow-project")
        assert r.status_code == 502
        detail = r.json()["detail"]
        assert detail["message"] == "extension_disconnected"
        assert detail["raw"]["error"] == "extension_disconnected"


def test_bootstrap_rejects_unknown_project(client):
    r = client.post(
        "/api/projects/00000000-0000-0000-0000-000000000000/flow-project"
    )
    assert r.status_code == 404


def test_bootstrap_502_when_flow_returns_no_project_id(client):
    async def missing_id(title, tool="PINHOLE"):
        return {"raw": {"status": 200}, "error": "no_project_id_in_response"}

    p = _project(client)
    with patch("flowboard.routes.projects.get_flow_sdk") as m:
        m.return_value.create_project = AsyncMock(side_effect=missing_id)
        r = client.post(f"/api/projects/{p['id']}/flow-project")
        assert r.status_code == 502


@pytest.mark.asyncio
async def test_bootstrap_is_concurrency_safe(client):
    """Two parallel callers must not produce two bindings."""
    call_count = 0

    async def fake_create(title, tool="PINHOLE"):
        nonlocal call_count
        call_count += 1
        return {"raw": {}, "project_id": f"pid-{call_count}"}

    p = _project(client)
    with patch("flowboard.routes.projects.get_flow_sdk") as m:
        m.return_value.create_project = AsyncMock(side_effect=fake_create)
        r1 = client.post(f"/api/projects/{p['id']}/flow-project")
        r2 = client.post(f"/api/projects/{p['id']}/flow-project")

    pid1 = r1.json()["flow_project_id"]
    pid2 = r2.json()["flow_project_id"]
    assert pid1 == pid2
