"""Handing over a whole series, which is the ordinary case.

A PM does not assign twelve episodes one at a time — one person takes a series.
The field for it had to be new: `producer_user_id` sits right beside it and looks
like the answer, but that is the first REVIEWER in the approver chain, so an artist
put there would approve their own submissions. Two ends of the same handover, two
columns.

The tests that matter most are the ones where the two must not be confused, and the
one where an episode is lent to somebody else — that is how a series gets split when
one person is overloaded, and it moves the WORK without moving the hand-in.
"""
from __future__ import annotations

import uuid

import pytest

from flowboard.db import get_session
from flowboard.db.models import Scene, Series
from flowboard.services import permissions, user_service
from flowboard.services import submission_service as subs


def _h(client, u, p="pw123456"):
    r = client.post("/api/account/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture()
def world(client):
    """A project with one series of three episodes; an artist and a bystander."""
    user_service.create_user("sa_admin", "pw123456", role="admin")
    artist = user_service.create_user("sa_artist", "pw123456")
    other = user_service.create_user("sa_other", "pw123456")
    ah = _h(client, "sa_admin")

    pid = client.post("/api/projects", json={"name": "SA"}, headers=ah).json()["id"]
    sid = client.post(
        f"/api/projects/{pid}/series", json={"name": "S"}, headers=ah
    ).json()["id"]
    eps = [
        client.post(
            f"/api/projects/{pid}/scenes", json={"name": f"EP{i}", "series_id": sid},
            headers=ah,
        ).json()["id"]
        for i in (1, 2, 3)
    ]
    return {
        "admin": ah, "artist": _h(client, "sa_artist"), "other": _h(client, "sa_other"),
        "artist_id": artist.id, "other_id": other.id,
        "project_id": pid, "series_id": sid, "eps": eps,
    }


def _give(client, world, user_id, headers=None):
    return client.patch(
        f"/api/series/{world['series_id']}/assignee",
        json={"user_id": str(user_id) if user_id else None},
        headers=headers or world["admin"],
    )


# ── the handover ────────────────────────────────────────────────────────────


def test_the_whole_series_arrives_at_once(world, client):
    """One row on "My work", not one per episode — the series is what is handed in,
    and the episode count is how somebody sees how much is behind the one link."""
    assert client.get("/api/my/series", headers=world["artist"]).json()["series"] == []
    assert _give(client, world, world["artist_id"]).status_code == 200
    mine = client.get("/api/my/series", headers=world["artist"]).json()["series"]
    assert [s["id"] for s in mine] == [world["series_id"]]
    assert mine[0]["episode_count"] == len(world["eps"])
    assert {e["id"] for e in mine[0]["episodes"]} == set(world["eps"])


def test_the_project_appears_and_opens(world, client):
    """The failure this whole line of work started from: work you can see named and
    cannot open."""
    _give(client, world, world["artist_id"])
    projects = client.get("/api/projects", headers=world["artist"]).json()
    assert [p["id"] for p in projects] == [world["project_id"]]
    for ep in world["eps"]:
        assert client.get(f"/api/scenes/{ep}", headers=world["artist"]).status_code == 200


def test_they_can_work_in_every_episode_of_it(world, client):
    _give(client, world, world["artist_id"])
    for ep in world["eps"]:
        r = client.post(f"/api/scenes/{ep}/shots", json={}, headers=world["artist"])
        assert r.status_code == 200, r.text


def test_taking_it_back_takes_all_of_it(world, client):
    """Derived from the field, so one call undoes the whole grant. Nothing was
    copied onto the episodes that would have to be found and undone."""
    _give(client, world, world["artist_id"])
    assert _give(client, world, None).status_code == 200
    assert client.get("/api/my/series", headers=world["artist"]).json()["series"] == []
    assert client.get("/api/projects", headers=world["artist"]).json() == []
    assert (
        client.get(f"/api/scenes/{world['eps'][0]}", headers=world["artist"]).status_code
        == 404
    )


def test_a_bystander_is_unaffected(world, client):
    _give(client, world, world["artist_id"])
    assert client.get("/api/projects", headers=world["other"]).json() == []
    assert (
        client.get(f"/api/scenes/{world['eps'][0]}", headers=world["other"]).status_code
        == 404
    )


# ── it is not the producer field ────────────────────────────────────────────


def test_being_given_a_series_does_not_make_you_its_reviewer(world, client):
    """The reason this is a second column. `producer_user_id` is the first link in
    the approver chain; if handing the work over had written there, the artist
    would review their own submissions."""
    _give(client, world, world["artist_id"])
    with get_session() as s:
        sr = s.get(Series, uuid.UUID(world["series_id"]))
        assert sr.assignee_user_id == world["artist_id"]
        assert sr.producer_user_id is None


def test_the_two_fields_are_set_by_two_different_calls(world, client):
    _give(client, world, world["artist_id"])
    r = client.patch(
        f"/api/series/{world['series_id']}/producer",
        json={"user_id": str(world["other_id"])},
        headers=world["admin"],
    )
    assert r.status_code == 200
    with get_session() as s:
        sr = s.get(Series, uuid.UUID(world["series_id"]))
        assert sr.assignee_user_id == world["artist_id"]
        assert sr.producer_user_id == world["other_id"]


def test_it_grants_work_not_structure(world, client):
    """ARTIST is the floor of what an assignment implies. Creating series inside
    somebody else's project is structure work and stays refused."""
    _give(client, world, world["artist_id"])
    r = client.post(
        f"/api/projects/{world['project_id']}/series",
        json={"name": "mine"},
        headers=world["artist"],
    )
    assert r.status_code == 403


# ── the narrower assignment still wins ──────────────────────────────────────


def test_lending_out_an_episode_does_not_move_the_hand_in(world, client):
    """How a series is split when one person is overloaded — and what does NOT move
    with it.

    The helper gets to work in that episode. They do not get the series on their
    "My work", because they are not the one who hands it in: a deliverable two
    people can submit is one nobody is accountable for.
    """
    _give(client, world, world["artist_id"])
    client.patch(
        f"/api/scenes/{world['eps'][1]}/assignee",
        json={"user_id": str(world["other_id"])},
        headers=world["admin"],
    )
    mine = client.get("/api/my/series", headers=world["artist"]).json()["series"]
    assert [s["id"] for s in mine] == [world["series_id"]], "still theirs to deliver"
    assert client.get("/api/my/series", headers=world["other"]).json()["series"] == []
    # …but the episode itself opens for them, which is the point of lending it.
    assert (
        client.get(f"/api/scenes/{world['eps'][1]}", headers=world["other"]).status_code
        == 200
    )


def test_the_helper_can_still_see_it_though(world, client):
    """Visibility is wider than the work list on purpose — the series holder is
    responsible for the series, and an episode inside it that they cannot open is
    a hole in that."""
    _give(client, world, world["artist_id"])
    client.patch(
        f"/api/scenes/{world['eps'][1]}/assignee",
        json={"user_id": str(world["other_id"])},
        headers=world["admin"],
    )
    r = client.get(f"/api/scenes/{world['eps'][1]}", headers=world["artist"])
    assert r.status_code == 200


def test_holding_both_the_series_and_an_episode_is_still_one_row(world, client):
    """Assigned the series AND one of its episodes — one thing to hand in, so one
    row. The old per-episode page could list the same work twice this way."""
    _give(client, world, world["artist_id"])
    client.patch(
        f"/api/scenes/{world['eps'][0]}/assignee",
        json={"user_id": str(world["artist_id"])},
        headers=world["admin"],
    )
    mine = client.get("/api/my/series", headers=world["artist"]).json()["series"]
    assert [s["id"] for s in mine] == [world["series_id"]]


# ── handing the work in ─────────────────────────────────────────────────────


def test_the_series_holder_may_submit_an_episode(world, client):
    """Without this the grant is half a grant: they could open all twelve episodes
    and hand none of them in, because submit checked the episode's own assignee."""
    _give(client, world, world["artist_id"])
    with get_session() as s:
        scene = s.get(Scene, uuid.UUID(world["eps"][0]))
        owner = subs._work_owner(s, scene)
    assert owner == world["artist_id"]


def test_someone_elses_episode_is_still_theirs_to_submit(world, client):
    _give(client, world, world["artist_id"])
    client.patch(
        f"/api/scenes/{world['eps'][1]}/assignee",
        json={"user_id": str(world["other_id"])},
        headers=world["admin"],
    )
    with get_session() as s:
        scene = s.get(Scene, uuid.UUID(world["eps"][1]))
        assert subs._work_owner(s, scene) == world["other_id"]


def test_an_unassigned_episode_of_an_unassigned_series_has_no_owner(world, client):
    with get_session() as s:
        scene = s.get(Scene, uuid.UUID(world["eps"][0]))
        assert subs._work_owner(s, scene) is None


# ── who may hand a series over ──────────────────────────────────────────────


def test_an_artist_cannot_assign_a_series_to_themselves(world, client):
    _give(client, world, world["artist_id"])
    r = _give(client, world, world["artist_id"], headers=world["artist"])
    assert r.status_code == 403, r.text


def test_assigning_to_a_user_that_does_not_exist_is_refused(world, client):
    r = client.patch(
        f"/api/series/{world['series_id']}/assignee",
        json={"user_id": str(uuid.uuid4())},
        headers=world["admin"],
    )
    assert r.status_code == 404


def test_the_scope_is_the_series_not_the_project(world, client):
    """A second series in the same project stays invisible: the grant is a subtree,
    not the project it hangs in."""
    other_sid = client.post(
        f"/api/projects/{world['project_id']}/series",
        json={"name": "S2"}, headers=world["admin"],
    ).json()["id"]
    hidden = client.post(
        f"/api/projects/{world['project_id']}/scenes",
        json={"name": "X1", "series_id": other_sid}, headers=world["admin"],
    ).json()["id"]
    _give(client, world, world["artist_id"])
    rows = client.get(
        f"/api/projects/{world['project_id']}/scenes", headers=world["artist"]
    ).json()
    assert hidden not in {r["id"] for r in rows}
    assert client.get(f"/api/scenes/{hidden}", headers=world["artist"]).status_code == 404


def test_the_derived_role_stays_scoped(world, client):
    """A role that is not scoped makes `visible_scope` return None, which means
    UNRESTRICTED — the grant would silently become the whole project."""
    _give(client, world, world["artist_id"])
    with get_session() as s:
        u = user_service.get_by_username("sa_artist")
        scope = permissions.visible_scope(s, u, uuid.UUID(world["project_id"]))
    assert scope is not None
    assert scope["scene_ids"] == {uuid.UUID(e) for e in world["eps"]}
