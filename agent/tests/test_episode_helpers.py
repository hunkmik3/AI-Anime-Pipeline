"""An episode has one owner and, when they are overloaded, helpers.

The shape comes from the handover: a chapter split between three panel artists
arrives in production as ONE episode, so "one person animates all of it" stops
being realistic the moment that chapter is large.

The two halves that must both hold:

  * a helper gets everything the owner has INSIDE the episode — otherwise they
    were not really added;
  * and nothing outside it, and not the right to hand the cut in — a deliverable
    two people can submit is one nobody is accountable for.

Both halves fail silently. Granting too little looks like a broken page;
granting too much looks like nothing at all.
"""
from __future__ import annotations

import pytest

from flowboard.db import get_session
from flowboard.services import (
    project_service,
    scene_service,
    series_service,
    user_service,
)


def _h(client, u, p="pw123456"):
    r = client.post("/api/account/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture()
def studio(client):
    """One project, one series, TWO episodes — an owner on each.

    Two episodes because a helper who can see everything and a helper who can
    see their own are indistinguishable when there is only one.
    """
    user_service.create_user("hb_boss", "pw123456", role="admin")
    owner = user_service.create_user("hb_owner", "pw123456")
    helper = user_service.create_user("hb_helper", "pw123456")
    other = user_service.create_user("hb_other", "pw123456")
    boss = _h(client, "hb_boss")

    pid = client.post(
        "/api/projects", json={"name": "Anime"}, headers=boss
    ).json()["id"]
    client.put(
        f"/api/projects/{pid}/members",
        json={
            "members": [
                {"user_id": str(owner.id), "role": "artist"},
                {"user_id": str(helper.id), "role": "artist"},
                {"user_id": str(other.id), "role": "artist"},
            ]
        },
        headers=boss,
    )
    with get_session() as s:
        series = series_service.create_series(s, pid, name="S1")
        ep1 = scene_service.create_scene(s, pid, name="EP1", series_id=series.id)
        ep2 = scene_service.create_scene(s, pid, name="EP2", series_id=series.id)
        ep1_id, ep2_id = ep1.id, ep2.id
    for ep, who in ((ep1_id, owner), (ep2_id, other)):
        client.patch(
            f"/api/scenes/{ep}/assignee", json={"user_id": str(who.id)}, headers=boss
        )
    return {
        "pid": pid, "ep1": str(ep1_id), "ep2": str(ep2_id),
        "boss": boss,
        "owner": (owner, _h(client, "hb_owner")),
        "helper": (helper, _h(client, "hb_helper")),
        "other": (other, _h(client, "hb_other")),
    }


def _add_helper(client, studio, ep, who, headers=None):
    return client.post(
        f"/api/scenes/{ep}/helpers",
        json={"user_id": str(who.id)},
        headers=headers or studio["boss"],
    )


# ── before being added ──────────────────────────────────────────────────────


def test_someone_not_on_the_episode_cannot_see_it(client, studio):
    """The baseline the rest of the file is measured against. Without it, a
    helper "gaining" access proves nothing."""
    _, hh = studio["helper"]
    assert client.get(f"/api/scenes/{studio['ep1']}", headers=hh).status_code == 404
    ids = {e["id"] for e in client.get(
        f"/api/projects/{studio['pid']}/scenes", headers=hh).json()}
    assert studio["ep1"] not in ids


# ── being added grants the work ─────────────────────────────────────────────


def test_a_helper_can_open_the_episode_and_work_in_it(client, studio):
    who, hh = studio["helper"]
    assert _add_helper(client, studio, studio["ep1"], who).status_code == 200

    assert client.get(f"/api/scenes/{studio['ep1']}", headers=hh).status_code == 200
    assert client.get(f"/api/scenes/{studio['ep1']}/canvas", headers=hh).status_code == 200
    seq = client.post(f"/api/scenes/{studio['ep1']}/shots", json={}, headers=hh)
    assert seq.status_code == 200, seq.text
    assert client.patch(
        f"/api/shots/{seq.json()['id']}", json={"script_text": "int. bar"}, headers=hh
    ).status_code == 200


def test_the_episode_appears_in_their_list(client, studio):
    who, hh = studio["helper"]
    _add_helper(client, studio, studio["ep1"], who)
    ids = {e["id"] for e in client.get(
        f"/api/projects/{studio['pid']}/scenes", headers=hh).json()}
    assert studio["ep1"] in ids


# ── and nothing more ────────────────────────────────────────────────────────


def test_a_helper_on_one_episode_still_cannot_see_the_other(client, studio):
    """The half that fails silently: granting too much looks like nothing."""
    who, hh = studio["helper"]
    _add_helper(client, studio, studio["ep1"], who)

    assert client.get(f"/api/scenes/{studio['ep2']}", headers=hh).status_code == 404
    ids = {e["id"] for e in client.get(
        f"/api/projects/{studio['pid']}/scenes", headers=hh).json()}
    assert ids == {studio["ep1"]}


def test_a_helper_cannot_hand_the_cut_in(client, studio):
    """One owner stays one owner. A deliverable two people can submit is one
    nobody is accountable for."""
    who, hh = studio["helper"]
    _add_helper(client, studio, studio["ep1"], who)

    r = client.post(
        f"/api/scenes/{studio['ep1']}/submissions",
        json={"drive_url": "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUvWx/view"},
        headers=hh,
    )
    assert r.status_code in (403, 404), r.text

    # …while the owner still can.
    _, oh = studio["owner"]
    assert client.post(
        f"/api/scenes/{studio['ep1']}/submissions",
        json={"drive_url": "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUvWx/view"},
        headers=oh,
    ).status_code == 200


def test_a_helper_cannot_add_more_helpers(client, studio):
    who, hh = studio["helper"]
    _add_helper(client, studio, studio["ep1"], who)
    other, _ = studio["other"]

    r = _add_helper(client, studio, studio["ep1"], other, headers=hh)
    assert r.status_code == 403


# ── removing ────────────────────────────────────────────────────────────────


def test_removing_a_helper_takes_the_access_back(client, studio):
    who, hh = studio["helper"]
    _add_helper(client, studio, studio["ep1"], who)
    assert client.get(f"/api/scenes/{studio['ep1']}", headers=hh).status_code == 200

    assert client.delete(
        f"/api/scenes/{studio['ep1']}/helpers/{who.id}", headers=studio["boss"]
    ).status_code == 200
    assert client.get(f"/api/scenes/{studio['ep1']}", headers=hh).status_code == 404


# ── bookkeeping ─────────────────────────────────────────────────────────────


def test_adding_the_same_person_twice_is_not_an_error(client, studio):
    """A double-click on Add must not 500 on a unique constraint."""
    who, _ = studio["helper"]
    assert _add_helper(client, studio, studio["ep1"], who).status_code == 200
    r = _add_helper(client, studio, studio["ep1"], who)
    assert r.status_code == 200
    assert len(r.json()) == 1


def test_adding_the_owner_as_their_own_helper_changes_nothing(client, studio):
    """They already have everything the row would grant, and more."""
    owner, _ = studio["owner"]
    r = _add_helper(client, studio, studio["ep1"], owner)
    assert r.status_code == 200
    assert r.json() == []


def test_the_list_says_who_added_them(client, studio):
    who, hh = studio["helper"]
    _add_helper(client, studio, studio["ep1"], who)
    rows = client.get(f"/api/scenes/{studio['ep1']}/helpers", headers=hh).json()
    assert len(rows) == 1
    assert rows[0]["name"] == "hb_helper"
    assert rows[0]["added_by_name"] == "hb_boss"
