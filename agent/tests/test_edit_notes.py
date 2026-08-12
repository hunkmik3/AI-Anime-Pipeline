"""The editor's notes, and the artist finding out which sequence is wrong.

`shot_id` is the point of the feature and it is CHOSEN, not computed. The cut is
assembled outside the app — trimmed, reordered, shots dropped — so a timecode
cannot be resolved back to a sequence by arithmetic. The editor is already paused
on the frame; picking the clip is one click and is always right, where a guess is
silently wrong the first time somebody trims a shot.
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
    user_service.create_user("nt_admin", "pw123456", role="admin")
    artist = user_service.create_user("nt_artist", "pw123456")
    editor = user_service.create_user("nt_editor", "pw123456", display_name="Hải")
    viewer = user_service.create_user("nt_viewer", "pw123456")
    ah = _h(client, "nt_admin")
    pid = client.post("/api/projects", json={"name": "NT"}, headers=ah).json()["id"]
    sid = client.post(f"/api/projects/{pid}/series", json={"name": "S"}, headers=ah).json()["id"]
    ep = client.post(f"/api/projects/{pid}/scenes",
                     json={"name": "EP1", "series_id": sid}, headers=ah).json()["id"]
    sq = client.post(f"/api/scenes/{ep}/shots", json={}, headers=ah).json()["id"]
    client.patch(f"/api/series/{sid}/assignee", json={"user_id": str(artist.id)}, headers=ah)
    client.put(f"/api/projects/{pid}/members",
               json={"members": [{"user_id": str(artist.id), "role": "artist"},
                                 {"user_id": str(editor.id), "role": "editor"},
                                 {"user_id": str(viewer.id), "role": "viewer"}]},
               headers=ah)
    cut = client.post(f"/api/series/{sid}/edits", json={"drive_url": DRIVE},
                      headers=_h(client, "nt_editor")).json()
    return {"admin": ah, "artist": _h(client, "nt_artist"), "editor": _h(client, "nt_editor"),
            "viewer": _h(client, "nt_viewer"), "pid": pid, "sid": sid, "ep": ep,
            "sq": sq, "cut": cut["id"]}


def _note(client, world, **kw):
    payload = {"at_seconds": 167.12, "body": "Mặt bị méo", "shot_id": world["sq"]}
    payload.update(kw)
    return client.post(f"/api/submissions/{world['cut']}/notes", json=payload,
                       headers=world["editor"])


# ── leaving one ─────────────────────────────────────────────────────────────


def test_a_note_pins_the_sequence_the_editor_picked(world, client):
    r = _note(client, world)
    assert r.status_code == 200, r.text
    assert r.json()["shot_id"] == world["sq"]
    assert r.json()["at_seconds"] == 167.12


def test_the_timecode_keeps_its_fraction(world, client):
    """A note lands on a FRAME. Rounded to the second it points at the wrong one
    at 24fps, which is exactly the granularity the complaint is about."""
    assert _note(client, world, at_seconds=167.04).json()["at_seconds"] == 167.04


def test_a_note_need_not_name_a_sequence(world, client):
    """"The whole thing is too dark" is a real note and pinning it to whichever
    shot happened to be on screen would send one artist to fix everyone's."""
    assert _note(client, world, shot_id=None).json()["shot_id"] is None


def test_a_viewer_cannot_leave_notes(world, client):
    r = client.post(f"/api/submissions/{world['cut']}/notes",
                    json={"at_seconds": 1, "body": "x"}, headers=world["viewer"])
    assert r.status_code == 403


# ── the artist's half ───────────────────────────────────────────────────────


def test_the_artist_finds_the_notes_on_their_own_sequence(world, client):
    """The half that matters. An artist opening a sequence asks "what is wrong
    with THIS one" — making them open the editor's timeline and find their own
    shot in it is asking them to do the app's job."""
    _note(client, world)
    _note(client, world, at_seconds=171.0, body="Chuyển cảnh gắt")
    out = client.get(f"/api/shots/{world['sq']}/notes", headers=world["artist"]).json()
    assert out["open_count"] == 2
    assert [n["body"] for n in out["notes"]] == ["Mặt bị méo", "Chuyển cảnh gắt"]


def test_a_general_note_does_not_land_on_a_sequence(world, client):
    _note(client, world, shot_id=None)
    out = client.get(f"/api/shots/{world['sq']}/notes", headers=world["artist"]).json()
    assert out["open_count"] == 0


def test_resolving_takes_it_off_the_count_and_can_be_undone(world, client):
    """"Fixed" that cannot be undone makes a mis-click a lie the editor then has
    to argue with."""
    nid = _note(client, world).json()["id"]
    assert client.post(f"/api/notes/{nid}/resolve", headers=world["artist"]).json()["resolved"] is True
    assert client.get(f"/api/shots/{world['sq']}/notes",
                      headers=world["artist"]).json()["open_count"] == 0
    client.post(f"/api/notes/{nid}/resolve?done=false", headers=world["artist"])
    assert client.get(f"/api/shots/{world['sq']}/notes",
                      headers=world["artist"]).json()["open_count"] == 1


def test_open_notes_sort_before_resolved_ones(world, client):
    """The sequence page is opened to find out what is still wrong."""
    first = _note(client, world, at_seconds=10).json()["id"]
    _note(client, world, at_seconds=200, body="còn lỗi")
    client.post(f"/api/notes/{first}/resolve", headers=world["artist"])
    out = client.get(f"/api/shots/{world['sq']}/notes", headers=world["artist"]).json()
    assert out["notes"][0]["body"] == "còn lỗi"


def test_notes_come_back_in_play_order_on_the_cut(world, client):
    """The order they are worked through."""
    _note(client, world, at_seconds=200, body="b")
    _note(client, world, at_seconds=20, body="a")
    got = client.get(f"/api/submissions/{world['cut']}/notes", headers=world["editor"]).json()
    assert [n["body"] for n in got["notes"]] == ["a", "b"]
