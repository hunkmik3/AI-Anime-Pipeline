"""Giantflow per-project roles — that the gate actually gates.

The route-authorization sweep already proves every ``/api`` route refuses an
anonymous caller. It cannot prove the interesting thing: that an *artist* is
refused a *review*. That distinction is the whole point of having roles, and
until this file it was verified once by hand and never again — so a typo in
``CAPABILITIES`` would have handed approval rights to everyone silently.
"""
from __future__ import annotations

from flowboard.services import flow_permissions as fp
from flowboard.services import panel_service as ps
from flowboard.services import user_service
from flowboard.db import get_session


def _login(client, username, password="pw123456"):
    r = client.post("/api/account/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _fixture(client):
    """One comic, one batch, one panel, and an account per role."""
    admin = user_service.create_user("gf_admin", "pw123456", role="admin")
    pm = user_service.create_user("gf_pm", "pw123456", role="user")
    artist = user_service.create_user("gf_artist", "pw123456", role="user")
    viewer = user_service.create_user("gf_viewer", "pw123456", role="user")

    with get_session() as s:
        project = ps.create_project(s, "Comic")
        batch = ps.create_batch(s, project.id, "Batch")
        panel = ps.import_panels(s, batch.id, entries=[("PANEL001.png", "media-1")])[0]
        ps.set_member(s, project.id, pm.id, "producer")
        ps.set_member(s, project.id, artist.id, "artist")
        ids = (project.id, batch.id, panel.id)

    return ids, {
        "admin": _login(client, "gf_admin"),
        "pm": _login(client, "gf_pm"),
        "artist": _login(client, "gf_artist"),
        "viewer": _login(client, "gf_viewer"),
    }


# ── the capability table itself ───────────────────────────────────────────


def test_ranking_is_ordered():
    assert fp.allows("admin", "project.manage")
    assert not fp.allows("producer", "project.manage")
    assert fp.allows("producer", "panel.review")
    assert not fp.allows("artist", "panel.review")
    assert fp.allows("artist", "panel.generate")
    assert not fp.allows("viewer", "panel.generate")
    assert fp.allows("viewer", "panel.read")


def test_unknown_capability_raises_rather_than_allowing():
    """A typo must fail loudly. Returning False would be safe; returning True by
    omission would not, and a silent False hides the bug just as well."""
    try:
        fp.allows("admin", "panel.teleport")
    except KeyError:
        return
    raise AssertionError("unknown capability was answered instead of refused")


def test_unknown_role_string_never_outranks_anyone():
    assert fp.normalize_role("wizard") == fp.ARTIST
    assert not fp.allows("wizard", "panel.review")


# ── role resolution ───────────────────────────────────────────────────────


def test_batch_assignee_is_an_artist_without_a_member_row(client):
    (project_id, batch_id, _), _ = _fixture(client)
    hand = user_service.create_user("gf_hand", "pw123456", role="user")
    with get_session() as s:
        assert fp.role_for(s, hand, project_id) == fp.VIEWER
        ps.update_batch(s, batch_id, assignee_user_id=hand.id, set_assignee=True)
        # Assigning work already says "this is yours"; a membership row saying it
        # again would be the same fact stored twice.
        assert fp.role_for(s, hand, project_id) == fp.ARTIST


def test_signed_in_stranger_can_read_but_not_act(client):
    (project_id, _, _), _ = _fixture(client)
    stranger = user_service.create_user("gf_stranger", "pw123456", role="user")
    with get_session() as s:
        role = fp.role_for(s, stranger, project_id)
    assert role == fp.VIEWER
    assert fp.allows(role, "panel.read")
    assert not fp.allows(role, "panel.generate")


# ── the routes ────────────────────────────────────────────────────────────


def test_review_is_refused_to_artists_over_http(client):
    """The one that matters: hiding the button is not the protection."""
    (_, _, panel_id), h = _fixture(client)
    body = {"approve": True, "notes": []}
    assert client.post(f"/api/flowstudio/panels/{panel_id}/review", json=body, headers=h["artist"]).status_code == 403
    assert client.post(f"/api/flowstudio/panels/{panel_id}/review", json=body, headers=h["viewer"]).status_code == 403


def test_batch_management_is_refused_to_artists(client):
    (project_id, batch_id, _), h = _fixture(client)
    body = {"name": "New"}
    assert client.post(f"/api/flowstudio/projects/{project_id}/batches", json=body, headers=h["artist"]).status_code == 403
    assert client.delete(f"/api/flowstudio/batches/{batch_id}", headers=h["artist"]).status_code == 403
    assert client.post(f"/api/flowstudio/projects/{project_id}/batches", json=body, headers=h["pm"]).status_code in (200, 201)


def test_only_an_admin_manages_the_comic_itself(client):
    (project_id, _, _), h = _fixture(client)
    assert client.delete(f"/api/flowstudio/projects/{project_id}", headers=h["pm"]).status_code == 403
    assert client.post("/api/flowstudio/projects", json={"name": "X"}, headers=h["artist"]).status_code == 403
    assert client.post("/api/flowstudio/projects", json={"name": "X"}, headers=h["admin"]).status_code in (200, 201)


def test_viewer_cannot_generate_or_submit(client):
    (_, _, panel_id), h = _fixture(client)
    assert client.post(f"/api/flowstudio/panels/{panel_id}/generate", json={"prompt": "x"}, headers=h["viewer"]).status_code == 403
    assert client.post(f"/api/flowstudio/panels/{panel_id}/submit", json={}, headers=h["viewer"]).status_code == 403


def test_everyone_signed_in_can_read(client):
    (_, batch_id, panel_id), h = _fixture(client)
    for who in ("admin", "pm", "artist", "viewer"):
        assert client.get(f"/api/flowstudio/panels/{panel_id}", headers=h[who]).status_code == 200
        assert client.get(f"/api/flowstudio/batches/{batch_id}/panels", headers=h[who]).status_code == 200


def test_membership_is_managed_by_the_pm_not_the_artist(client):
    (project_id, _, _), h = _fixture(client)
    someone = user_service.create_user("gf_new", "pw123456", role="user")
    body = {"user_id": str(someone.id), "role": "artist"}
    assert client.put(f"/api/flowstudio/projects/{project_id}/members", json=body, headers=h["artist"]).status_code == 403
    assert client.put(f"/api/flowstudio/projects/{project_id}/members", json=body, headers=h["pm"]).status_code == 200


def test_a_role_on_one_comic_grants_nothing_on_another(client):
    """Authority is per comic. A PM elsewhere must not review here."""
    (_, _, _), h = _fixture(client)
    with get_session() as s:
        other = ps.create_project(s, "Other comic")
        other_batch = ps.create_batch(s, other.id, "B")
        other_panel = ps.import_panels(s, other_batch.id, entries=[("P.png", "media-9")])[0].id
    body = {"approve": True, "notes": []}
    assert client.post(f"/api/flowstudio/panels/{other_panel}/review", json=body, headers=h["pm"]).status_code == 403
