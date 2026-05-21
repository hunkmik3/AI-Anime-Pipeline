"""Verify GET /api/video/models exposes the registry."""
from __future__ import annotations


def test_lists_all_registered_models(client):
    resp = client.get("/api/video/models")
    assert resp.status_code == 200
    body = resp.json()
    assert body["default_model_id"] == "flow-default"
    ids = {m["model_id"] for m in body["models"]}
    assert {"flow-default", "seedance-1-5-pro", "seedance-2-0"} <= ids


def test_capability_block_is_present(client):
    body = client.get("/api/video/models").json()
    for m in body["models"]:
        cap = m["capabilities"]
        for key in (
            "supports_multi_ref",
            "supports_last_frame",
            "supports_audio_toggle",
            "max_refs",
            "aspect_ratios",
            "resolutions",
            "durations",
        ):
            assert key in cap, f"{m['model_id']} missing capability key {key}"


def test_project_settings_rejects_unknown_model(client):
    """Pydantic validator should 422 when the project tries to pin a
    model that isn't registered (typo, removed model, etc.)."""
    # Create a project, then PATCH it with a bad default_video_model
    proj = client.post("/api/projects", json={"name": "p1"}).json()
    resp = client.patch(
        f"/api/projects/{proj['id']}",
        json={"settings": {"default_video_model": "not-a-real-model"}},
    )
    assert resp.status_code == 422
    assert "default_video_model" in resp.text


def test_project_settings_accepts_known_model(client):
    proj = client.post("/api/projects", json={"name": "p2"}).json()
    resp = client.patch(
        f"/api/projects/{proj['id']}",
        json={"settings": {"default_video_model": "seedance-1-5-pro"}},
    )
    assert resp.status_code == 200
    assert resp.json()["settings"]["default_video_model"] == "seedance-1-5-pro"
