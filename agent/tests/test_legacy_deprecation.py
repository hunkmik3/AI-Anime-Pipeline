"""Phase 2: the legacy ``/api/boards/*`` shim must carry RFC 8594
``Deprecation: true`` so frontend / curl consumers see a clear cutover
signal during the Phase 2→3 transition. ``/api/projects/*`` (the
successor) MUST NOT carry it.
"""
from __future__ import annotations

import uuid


def test_legacy_boards_list_carries_deprecation_header(client):
    r = client.get("/api/boards")
    assert r.status_code == 200
    assert r.headers.get("Deprecation") == "true"
    assert r.headers.get("Sunset") == "Phase 4"
    link = r.headers.get("Link")
    assert link is not None and "successor-version" in link
    assert "/api/projects" in link


def test_legacy_board_detail_carries_deprecation_header(client):
    b = client.post("/api/boards", json={"name": "x"}).json()
    r = client.get(f"/api/boards/{b['id']}")
    assert r.headers.get("Deprecation") == "true"


def test_legacy_board_project_sub_resource_carries_deprecation_header(client):
    """The Flow-binding legacy shim under /api/boards/{id}/project also
    must signal deprecation."""
    b = client.post("/api/boards", json={"name": "x"}).json()
    r = client.get(f"/api/boards/{b['id']}/project")
    # 404 unbound is fine — the middleware fires on all /api/boards/* paths.
    assert r.headers.get("Deprecation") == "true"


def test_new_projects_endpoint_has_no_deprecation_header(client):
    p = client.post("/api/projects", json={"name": "new"}).json()
    r = client.get(f"/api/projects/{p['id']}")
    assert r.status_code == 200
    assert "Deprecation" not in r.headers


def test_new_scenes_endpoint_has_no_deprecation_header(client):
    p = client.post("/api/projects", json={"name": "new"}).json()
    r = client.get(f"/api/projects/{p['id']}/scenes")
    assert r.status_code == 200
    assert "Deprecation" not in r.headers


def test_unrelated_endpoints_have_no_deprecation_header(client):
    """``/api/nodes``, ``/api/edges`` etc. are unchanged Phase 1 surfaces;
    they must not be tagged."""
    r = client.get("/api/health")
    assert "Deprecation" not in r.headers


def test_missing_board_still_carries_deprecation_header(client):
    """Middleware runs on every response in the legacy prefix, including
    404s."""
    r = client.get(f"/api/boards/{uuid.uuid4()}")
    assert r.status_code == 404
    assert r.headers.get("Deprecation") == "true"
