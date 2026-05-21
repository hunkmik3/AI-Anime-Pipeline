"""Phase 2: /api/projects/{id}/bible + /api/scenes/{id}/bible tests."""
from __future__ import annotations

import uuid


_VALID_BIBLE = {
    "art_style": "cel-shaded",
    "color_palette": ["#FFF", "#000"],
    "line_style": "thin",
    "lighting_conventions": "rim",
    "negative_prompts": ["3D"],
    "style_anchor_asset_ids": [],
}


def _project(client, name: str = "P") -> str:
    return client.post("/api/projects", json={"name": name}).json()["id"]


def _scene(client, project_id: str, name: str = "S") -> str:
    return client.post(
        f"/api/projects/{project_id}/scenes", json={"name": name}
    ).json()["id"]


# ── Project Bible ────────────────────────────────────────────────────────


def test_project_bible_starts_empty(client):
    pid = _project(client)
    r = client.get(f"/api/projects/{pid}/bible")
    assert r.status_code == 200
    assert r.json() == {}


def test_put_then_get_project_bible(client):
    pid = _project(client)
    r = client.put(f"/api/projects/{pid}/bible", json=_VALID_BIBLE)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["art_style"] == "cel-shaded"
    assert out["color_palette"] == ["#FFF", "#000"]
    r = client.get(f"/api/projects/{pid}/bible")
    assert r.json()["art_style"] == "cel-shaded"


def test_project_bible_strict_validation(client):
    pid = _project(client)
    bad = dict(_VALID_BIBLE)
    bad["smuggle"] = "x"
    r = client.put(f"/api/projects/{pid}/bible", json=bad)
    assert r.status_code == 422


def test_project_bible_partial_payload_uses_defaults(client):
    """Empty bible PUT zeroes fields the caller didn't supply."""
    pid = _project(client)
    client.put(f"/api/projects/{pid}/bible", json=_VALID_BIBLE)
    r = client.put(f"/api/projects/{pid}/bible", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["art_style"] == ""
    assert body["color_palette"] == []


def test_project_bible_missing_project_get(client):
    r = client.get(f"/api/projects/{uuid.uuid4()}/bible")
    assert r.status_code == 404


def test_project_bible_missing_project_put(client):
    r = client.put(f"/api/projects/{uuid.uuid4()}/bible", json=_VALID_BIBLE)
    assert r.status_code == 404


# ── Scene Bible ──────────────────────────────────────────────────────────


def test_scene_bible_starts_empty(client):
    pid = _project(client)
    sid = _scene(client, pid)
    r = client.get(f"/api/scenes/{sid}/bible")
    assert r.status_code == 200
    body = r.json()
    assert body["scene_bible_text"] == ""
    assert body["master_establishing_asset_id"] is None


def test_put_then_get_scene_bible(client):
    pid = _project(client)
    sid = _scene(client, pid)
    r = client.put(
        f"/api/scenes/{sid}/bible",
        json={"scene_bible_text": "rooftop night, neon", "master_establishing_asset_id": None},
    )
    assert r.status_code == 200, r.text
    assert r.json()["scene_bible_text"] == "rooftop night, neon"


def test_scene_bible_validates_asset_belongs_to_project(client):
    """master_establishing_asset_id must point to an Asset whose
    project_id == scene.project_id. Cross-project asset → 400."""
    from flowboard.db import get_session
    from flowboard.db.models import Asset

    pid_a = _project(client, "A")
    pid_b = _project(client, "B")
    sid_a = _scene(client, pid_a)

    with get_session() as s:
        asset = Asset(
            project_id=uuid.UUID(pid_b),
            kind="image",
            uuid_media_id="22222222-3333-4444-5555-666666666666",
        )
        s.add(asset)
        s.commit()
        s.refresh(asset)
        wrong_asset_id = asset.id

    r = client.put(
        f"/api/scenes/{sid_a}/bible",
        json={
            "scene_bible_text": "x",
            "master_establishing_asset_id": wrong_asset_id,
        },
    )
    assert r.status_code == 400


def test_scene_bible_accepts_matching_project_asset(client):
    from flowboard.db import get_session
    from flowboard.db.models import Asset

    pid = _project(client)
    sid = _scene(client, pid)
    with get_session() as s:
        a = Asset(
            project_id=uuid.UUID(pid),
            kind="image",
            uuid_media_id="33333333-4444-5555-6666-777777777777",
        )
        s.add(a)
        s.commit()
        s.refresh(a)
        aid = a.id

    r = client.put(
        f"/api/scenes/{sid}/bible",
        json={"scene_bible_text": "ok", "master_establishing_asset_id": aid},
    )
    assert r.status_code == 200, r.text
    assert r.json()["master_establishing_asset_id"] == aid


def test_scene_bible_rejects_unknown_asset_id(client):
    pid = _project(client)
    sid = _scene(client, pid)
    r = client.put(
        f"/api/scenes/{sid}/bible",
        json={"scene_bible_text": "x", "master_establishing_asset_id": 999999},
    )
    assert r.status_code == 400


def test_scene_bible_strict_validation(client):
    pid = _project(client)
    sid = _scene(client, pid)
    r = client.put(
        f"/api/scenes/{sid}/bible",
        json={"scene_bible_text": "x", "smuggle": True},
    )
    assert r.status_code == 422


def test_scene_bible_missing_scene_get(client):
    r = client.get(f"/api/scenes/{uuid.uuid4()}/bible")
    assert r.status_code == 404


def test_scene_bible_missing_scene_put(client):
    r = client.put(
        f"/api/scenes/{uuid.uuid4()}/bible",
        json={"scene_bible_text": "x"},
    )
    assert r.status_code == 404
