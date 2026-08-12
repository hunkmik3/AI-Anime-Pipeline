"""The editor hands the assembled episode back.

A SECOND hand-over on the same series, not another attempt at the artist's. The
distinction is the whole file: an artist's v2 and an editor's v2 are different
rounds of different work, and every place that mixes them tells somebody the
back-and-forth went twice as long as it did.
"""
from __future__ import annotations

import pytest

from flowboard.services import user_service

DRIVE = "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUv/view"


def _h(client, u, p="pw123456"):
    r = client.post("/api/account/login", json={"username": u, "password": p})
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture()
def world(client):
    user_service.create_user("ec_admin", "pw123456", role="admin")
    artist = user_service.create_user("ec_artist", "pw123456")
    editor = user_service.create_user("ec_editor", "pw123456")
    ah = _h(client, "ec_admin")
    pid = client.post("/api/projects", json={"name": "EC"}, headers=ah).json()["id"]
    sid = client.post(f"/api/projects/{pid}/series", json={"name": "S"}, headers=ah).json()["id"]
    client.patch(f"/api/series/{sid}/assignee", json={"user_id": str(artist.id)}, headers=ah)
    client.put(
        f"/api/projects/{pid}/members",
        json={"members": [{"user_id": str(artist.id), "role": "artist"},
                          {"user_id": str(editor.id), "role": "editor"}]},
        headers=ah,
    )
    return {"admin": ah, "artist": _h(client, "ec_artist"), "editor": _h(client, "ec_editor"),
            "pid": pid, "sid": sid}


def test_the_editor_hands_a_cut_back(world, client):
    r = client.post(f"/api/series/{world['sid']}/edits",
                    json={"drive_url": DRIVE}, headers=world["editor"])
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "edit" and r.json()["version"] == 1


def test_the_editor_need_not_be_the_series_assignee(world, client):
    """The assignee is the ARTIST. Requiring it would mean the only person allowed
    to hand the edit back is the one who did not make it."""
    r = client.post(f"/api/series/{world['sid']}/edits",
                    json={"drive_url": DRIVE}, headers=world["editor"])
    assert r.status_code == 200


def test_the_two_hand_overs_keep_separate_version_numbers(world, client):
    """Mixed, an editor's first cut would arrive as v2 of the artist's work."""
    client.post(f"/api/series/{world['sid']}/submissions",
                json={"drive_url": DRIVE}, headers=world["artist"])
    r = client.post(f"/api/series/{world['sid']}/edits",
                    json={"drive_url": DRIVE}, headers=world["editor"])
    assert r.json()["version"] == 1, "the editor's first cut is v1, not v2"


def test_an_edit_does_not_move_the_series_delivery_state(world, client):
    """`deliverable_status` tracks the artist's hand-over to the PM. An editor
    delivering must not put the series 'in review' with the PM."""
    client.post(f"/api/series/{world['sid']}/edits",
                json={"drive_url": DRIVE}, headers=world["editor"])
    mine = client.get("/api/my/series", headers=world["artist"]).json()["series"]
    assert mine[0]["deliverable_status"] == "draft"


def test_an_approved_series_can_still_be_re_cut(world, client):
    """A fix after sign-off has to be possible; the artist's path is closed then,
    the editor's is not."""
    sub = client.post(f"/api/series/{world['sid']}/submissions",
                      json={"drive_url": DRIVE}, headers=world["artist"]).json()
    client.post(f"/api/submissions/{sub['id']}/approve", json={}, headers=world["admin"])
    assert client.post(f"/api/series/{world['sid']}/submissions",
                       json={"drive_url": DRIVE}, headers=world["artist"]).status_code == 409
    assert client.post(f"/api/series/{world['sid']}/edits",
                       json={"drive_url": DRIVE}, headers=world["editor"]).status_code == 200


def test_the_artists_history_does_not_show_the_editors_cuts(world, client):
    client.post(f"/api/series/{world['sid']}/submissions",
                json={"drive_url": DRIVE}, headers=world["artist"])
    client.post(f"/api/series/{world['sid']}/edits",
                json={"drive_url": DRIVE}, headers=world["editor"])
    hist = client.get(f"/api/series/{world['sid']}/submissions",
                      headers=world["admin"]).json()["submissions"]
    assert [h["kind"] for h in hist] == ["cut"]
    edits = client.get(f"/api/series/{world['sid']}/edits", headers=world["admin"]).json()["edits"]
    assert [e["kind"] for e in edits] == ["edit"]
