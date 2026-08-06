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


def _series(session, name):
    """A comic needs a slate above it now; tests do not care which one."""
    project = ps.create_project(session, f"Slate for {name}")
    return ps.create_series(session, project.id, name)


def _chapter(session, name="Comic"):
    """A batch hangs off a chapter now; tests that only care about batches take
    the shortest path to one."""
    series = _series(session, name)
    return ps.create_chapter(session, series.id, "Chapter 1")


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
        series = _series(s, "Comic")
        chapter = ps.create_chapter(s, series.id, "Chapter 1")
        batch = ps.create_batch(s, chapter.id, "Batch")
        panel = ps.import_panels(s, batch.id, entries=[("PANEL001.png", "media-1")])[0]
        ps.set_member(s, series.id, pm.id, "producer")
        ps.set_member(s, series.id, artist.id, "artist")
        ids = (series.id, batch.id, panel.id)

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
    (series_id, batch_id, _), _ = _fixture(client)
    hand = user_service.create_user("gf_hand", "pw123456", role="user")
    with get_session() as s:
        assert fp.role_for(s, hand, series_id) == fp.VIEWER
        ps.update_batch(s, batch_id, assignee_user_id=hand.id, set_assignee=True)
        # Assigning work already says "this is yours"; a membership row saying it
        # again would be the same fact stored twice.
        assert fp.role_for(s, hand, series_id) == fp.ARTIST


def test_signed_in_stranger_can_read_but_not_act(client):
    (series_id, _, _), _ = _fixture(client)
    stranger = user_service.create_user("gf_stranger", "pw123456", role="user")
    with get_session() as s:
        role = fp.role_for(s, stranger, series_id)
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
    (series_id, batch_id, _), h = _fixture(client)
    with get_session() as s:
        chapter_id = ps.list_chapters(s, series_id)[0].id
    body = {"name": "New"}
    assert client.post(f"/api/flowstudio/chapters/{chapter_id}/batches", json=body, headers=h["artist"]).status_code == 403
    assert client.delete(f"/api/flowstudio/batches/{batch_id}", headers=h["artist"]).status_code == 403
    assert client.post(f"/api/flowstudio/chapters/{chapter_id}/batches", json=body, headers=h["pm"]).status_code in (200, 201)


def test_creating_a_chapter_is_refused_to_artists(client):
    """A chapter is a division of work, so it is the PM's to make."""
    (series_id, _, _), h = _fixture(client)
    body = {"name": "Chapter 2"}
    assert client.post(f"/api/flowstudio/series/{series_id}/chapters", json=body, headers=h["artist"]).status_code == 403
    assert client.post(f"/api/flowstudio/series/{series_id}/chapters", json=body, headers=h["pm"]).status_code in (200, 201)


def test_only_an_admin_manages_the_comic_itself(client):
    (series_id, _, _), h = _fixture(client)
    assert client.delete(f"/api/flowstudio/series/{series_id}", headers=h["pm"]).status_code == 403
    body = {"project_id": 1, "name": "X"}
    assert client.post("/api/flowstudio/series", json=body, headers=h["artist"]).status_code == 403
    # A slate of their own, to prove the admin path works end to end.
    made = client.post("/api/flowstudio/projects", json={"name": "Slate"}, headers=h["admin"])
    assert made.status_code in (200, 201), made.text
    assert client.post(
        "/api/flowstudio/series",
        json={"project_id": made.json()["id"], "name": "X"},
        headers=h["admin"],
    ).status_code in (200, 201)


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
    (series_id, _, _), h = _fixture(client)
    someone = user_service.create_user("gf_new", "pw123456", role="user")
    body = {"user_id": str(someone.id), "role": "artist"}
    assert client.put(f"/api/flowstudio/series/{series_id}/members", json=body, headers=h["artist"]).status_code == 403
    assert client.put(f"/api/flowstudio/series/{series_id}/members", json=body, headers=h["pm"]).status_code == 200


def test_a_role_on_one_comic_grants_nothing_on_another(client):
    """Authority is per comic. A PM elsewhere must not review here."""
    (_, _, _), h = _fixture(client)
    with get_session() as s:
        other = _series(s, "Other comic")
        other_chapter = ps.create_chapter(s, other.id, "Chapter 1")
        other_batch = ps.create_batch(s, other_chapter.id, "B")
        other_panel = ps.import_panels(s, other_batch.id, entries=[("P.png", "media-9")])[0].id
    body = {"approve": True, "notes": []}
    assert client.post(f"/api/flowstudio/panels/{other_panel}/review", json=body, headers=h["pm"]).status_code == 403


# ── the "view as" preview ─────────────────────────────────────────────────


def _as(role):
    return {"X-Giantflow-View-As": role}


def test_view_as_makes_the_server_answer_as_that_role(client):
    """The preview used to change only what the browser drew, so an admin
    checking the artist's view still got the admin's answers — every endpoint
    said yes and the review queue stayed full. It could not show the one thing
    it existed for."""
    (series_id, _, panel_id), h = _fixture(client)
    admin = h["admin"]

    # Admin, previewing nobody: allowed.
    assert client.post("/api/flowstudio/projects", json={"name": "P"}, headers=admin).status_code in (200, 201)

    # Previewing a PM: a PM cannot create a project.
    hdr = {**admin, **_as("producer")}
    assert client.post("/api/flowstudio/projects", json={"name": "P2"}, headers=hdr).status_code == 403

    # Previewing an artist: cannot rule on a panel either.
    hdr = {**admin, **_as("artist")}
    body = {"approve": True, "notes": []}
    assert client.post(f"/api/flowstudio/panels/{panel_id}/review", json=body, headers=hdr).status_code == 403

    # Reading stays open at every level.
    for role in ("producer", "artist", "viewer"):
        assert client.get("/api/flowstudio/series", headers={**admin, **_as(role)}).status_code == 200


def test_view_as_can_only_take_rights_away(client):
    """What makes trusting a header safe. A viewer forging `admin` must gain
    nothing — the cap lowers, it never raises."""
    (series_id, _, _), h = _fixture(client)
    forged = {**h["viewer"], **_as("admin")}
    assert client.post("/api/flowstudio/projects", json={"name": "X"}, headers=forged).status_code == 403
    assert client.post(
        f"/api/flowstudio/series/{series_id}/chapters", json={"name": "C"}, headers=forged
    ).status_code == 403


def test_me_reports_the_previewed_role(client):
    """The nav strip decides which tabs to show from this."""
    _, h = _fixture(client)
    assert client.get("/api/flowstudio/me", headers=h["admin"]).json()["best_role"] == "admin"
    got = client.get("/api/flowstudio/me", headers={**h["admin"], **_as("artist")}).json()
    assert got["best_role"] == "artist"
    assert got["capabilities"]["panel.review"] is False
    assert got["capabilities"]["panel.generate"] is True


def test_a_nonsense_view_as_header_does_not_escalate(client):
    """An unknown role normalises to artist, which is a demotion for an admin —
    never a promotion."""
    (series_id, _, _), h = _fixture(client)
    hdr = {**h["admin"], **_as("wizard")}
    assert client.post("/api/flowstudio/projects", json={"name": "X"}, headers=hdr).status_code == 403


def test_the_preview_never_hides_its_own_switch(client):
    """`best_role` is capped by the preview, so reading it to decide whether to
    offer the switch made the control disappear on first use — an admin who
    picked "Artist" was stuck there with nothing left to click. `true_role` is
    the uncapped answer, and it is what the switch reads."""
    _, h = _fixture(client)
    for role in ("producer", "artist", "viewer"):
        got = client.get("/api/flowstudio/me", headers={**h["admin"], **_as(role)}).json()
        assert got["best_role"] == role          # what the UI draws for
        assert got["true_role"] == "admin"       # who is really holding the switch

