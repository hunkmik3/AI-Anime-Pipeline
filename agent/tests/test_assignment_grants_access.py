"""Being given work is access to the project holding it.

Found live: a PM assigned an episode, it appeared on that person's "My work" page,
and opening it returned 404 — as did the project, the episode list and every
sequence inside. The sidebar said "No projects assigned to you yet" to somebody who
was assigned one. `/api/my/episodes` reads the assignment directly and never asks
`project_role`, so the one page that worked was the one that skipped the check.

The other half of this file is the part that must not break: an assignment grants
the project, and NOT the rest of what is in it.
"""
from __future__ import annotations

import uuid

import pytest

from flowboard.db import get_session
from flowboard.db.models import Scene, SceneCollaborator, Series
from flowboard.services import permissions, user_service


def _h(client, u, p="pw123456"):
    r = client.post("/api/account/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture()
def studio(client):
    """An admin's project with two episodes, and an artist holding neither."""
    boss = user_service.create_user("ag_admin", "pw123456", role="admin")
    hand = user_service.create_user("ag_artist", "pw123456")
    other = user_service.create_user("ag_other", "pw123456")
    ah = _h(client, "ag_admin")

    pid = client.post("/api/projects", json={"name": "AG"}, headers=ah).json()["id"]
    sid = client.post(
        f"/api/projects/{pid}/series", json={"name": "S"}, headers=ah
    ).json()["id"]
    eps = [
        client.post(
            f"/api/projects/{pid}/scenes", json={"name": f"EP{i}", "series_id": sid},
            headers=ah,
        ).json()["id"]
        for i in (1, 2)
    ]
    return {
        "admin": ah, "artist": _h(client, "ag_artist"), "other": _h(client, "ag_other"),
        "artist_id": hand.id, "other_id": other.id,
        "project_id": pid, "series_id": sid, "eps": eps,
    }


def _assign(client, studio, scene_id, user_id):
    r = client.patch(
        f"/api/scenes/{scene_id}/assignee",
        json={"user_id": str(user_id)},
        headers=studio["admin"],
    )
    assert r.status_code == 200, r.text


# ── the dead end ────────────────────────────────────────────────────────────


def test_an_unassigned_account_sees_no_projects(studio, client):
    """The baseline the fix must not erode."""
    r = client.get("/api/projects", headers=studio["other"])
    assert r.json() == []


def test_an_assigned_episode_puts_the_project_on_the_list(studio, client):
    before = client.get("/api/projects", headers=studio["artist"]).json()
    assert before == [], "precondition: nothing yet"
    _assign(client, studio, studio["eps"][0], studio["artist_id"])
    after = client.get("/api/projects", headers=studio["artist"]).json()
    assert [p["id"] for p in after] == [studio["project_id"]]


def test_the_assigned_episode_actually_opens(studio, client):
    """The 404 that made "My work" a list of things you cannot open."""
    ep = studio["eps"][0]
    assert client.get(f"/api/scenes/{ep}", headers=studio["artist"]).status_code == 404
    _assign(client, studio, ep, studio["artist_id"])
    assert client.get(f"/api/scenes/{ep}", headers=studio["artist"]).status_code == 200


def test_they_can_work_in_it_not_just_read_it(studio, client):
    """ARTIST, not VIEWER. An episode handed over to be built that cannot hold a
    sequence has been handed over in name only."""
    ep = studio["eps"][0]
    _assign(client, studio, ep, studio["artist_id"])
    r = client.post(f"/api/scenes/{ep}/shots", json={}, headers=studio["artist"])
    assert r.status_code == 200, r.text


def test_my_work_and_the_project_list_agree(studio, client):
    """The two pages disagreed, and only one of them was checked."""
    _assign(client, studio, studio["eps"][0], studio["artist_id"])
    mine = client.get("/api/my/episodes", headers=studio["artist"]).json()["episodes"]
    projects = client.get("/api/projects", headers=studio["artist"]).json()
    assert len(mine) == 1 and len(projects) == 1


# ── and grants nothing more ─────────────────────────────────────────────────


def test_only_the_assigned_episode_is_visible_inside(studio, client):
    """The whole point of granting the project is navigation, not contents. A
    colleague's episode in the same project stays invisible."""
    _assign(client, studio, studio["eps"][0], studio["artist_id"])
    rows = client.get(
        f"/api/projects/{studio['project_id']}/scenes", headers=studio["artist"]
    ).json()
    assert [r["id"] for r in rows] == [studio["eps"][0]]


def test_the_colleagues_episode_still_404s(studio, client):
    _assign(client, studio, studio["eps"][0], studio["artist_id"])
    r = client.get(f"/api/scenes/{studio['eps'][1]}", headers=studio["artist"])
    assert r.status_code == 404


def test_an_assignment_does_not_make_anyone_a_producer(studio, client):
    """The derived role is the FLOOR of what an assignment implies, not the role of
    whoever handed it over. Creating series is structure work."""
    _assign(client, studio, studio["eps"][0], studio["artist_id"])
    r = client.post(
        f"/api/projects/{studio['project_id']}/series",
        json={"name": "mine now"},
        headers=studio["artist"],
    )
    assert r.status_code == 403, r.text


def test_taking_the_assignment_away_takes_the_project_away(studio, client):
    """Derived from the assignment, so nothing has to be revoked. A membership row
    written on assignment would outlive it and leave the whole project readable to
    somebody who no longer works on it."""
    ep = studio["eps"][0]
    _assign(client, studio, ep, studio["artist_id"])
    assert len(client.get("/api/projects", headers=studio["artist"]).json()) == 1
    r = client.patch(
        f"/api/scenes/{ep}/assignee", json={"user_id": None}, headers=studio["admin"]
    )
    assert r.status_code == 200, r.text
    assert client.get("/api/projects", headers=studio["artist"]).json() == []
    assert client.get(f"/api/scenes/{ep}", headers=studio["artist"]).status_code == 404


# ── the other two ways of holding work ──────────────────────────────────────


def test_a_helper_added_to_an_episode_gets_in_too(studio, client):
    """An episode has one owner, but a chapter split between three panel artists
    arrives as one episode. A helper who cannot open it is not a helper."""
    with get_session() as s:
        s.add(
            SceneCollaborator(
                scene_id=uuid.UUID(studio["eps"][1]), user_id=studio["other_id"]
            )
        )
        s.commit()
    projects = client.get("/api/projects", headers=studio["other"]).json()
    assert [p["id"] for p in projects] == [studio["project_id"]]
    assert (
        client.get(f"/api/scenes/{studio['eps'][1]}", headers=studio["other"]).status_code
        == 200
    )


def test_producing_a_series_reaches_its_episodes(studio, client):
    with get_session() as s:
        sr = s.get(Series, uuid.UUID(studio["series_id"]))
        sr.producer_user_id = studio["other_id"]
        s.add(sr)
        s.commit()
    rows = client.get(
        f"/api/projects/{studio['project_id']}/scenes", headers=studio["other"]
    ).json()
    assert {r["id"] for r in rows} == set(studio["eps"])


def test_producing_a_series_does_not_upgrade_a_pm(studio, client):
    """Nobody is demoted by this either: a PM holding `producer_user_id` already has
    a membership row, and that is read first."""
    with get_session() as s:
        sr = s.get(Series, uuid.UUID(studio["series_id"]))
        sr.producer_user_id = studio["other_id"]
        s.add(sr)
        s.commit()
    r = client.post(
        f"/api/projects/{studio['project_id']}/series",
        json={"name": "nope"},
        headers=studio["other"],
    )
    assert r.status_code == 403


def test_the_role_is_scoped_so_the_scope_narrows_it(studio, client):
    """A derived role that was not scoped would return `None` from `visible_scope`,
    which means UNRESTRICTED — the assignment would have opened the whole project.
    """
    _assign(client, studio, studio["eps"][0], studio["artist_id"])
    with get_session() as s:
        u = user_service.get_by_username("ag_artist")
        pid = uuid.UUID(studio["project_id"])
        assert permissions.project_role(s, u, pid) == permissions.ARTIST
        scope = permissions.visible_scope(s, u, pid)
        assert scope is not None
        assert scope["scene_ids"] == {uuid.UUID(studio["eps"][0])}


def test_the_listed_project_can_also_be_read(studio, client):
    """Two expressions of one rule, and they had drifted.

    `list_projects` learned about assignments; `user_can_access_project` — the
    chokepoint behind `GET /api/projects/{id}` and everything hanging off it — did
    not. The artist's screen showed the project card sitting under a red "project
    not found", which is what a rule written in two places looks like from outside.
    """
    _assign(client, studio, studio["eps"][0], studio["artist_id"])
    listed = client.get("/api/projects", headers=studio["artist"]).json()
    assert listed, "precondition: it is on the list"
    for p in listed:
        assert (
            client.get(f"/api/projects/{p['id']}", headers=studio["artist"]).status_code
            == 200
        ), "listed but unreadable"


def test_everything_hanging_off_the_project_opens_too(studio, client):
    """The same chokepoint gates the bible and the picture pool, so a fix that only
    touched the project row would leave the page half broken."""
    _assign(client, studio, studio["eps"][0], studio["artist_id"])
    pid = studio["project_id"]
    for path in (f"/api/projects/{pid}/bible", f"/api/projects/{pid}/images"):
        assert client.get(path, headers=studio["artist"]).status_code == 200, path


def test_a_stranger_still_cannot_read_the_project(studio, client):
    """The other half: widening the read must not make it public."""
    pid = studio["project_id"]
    assert client.get(f"/api/projects/{pid}", headers=studio["other"]).status_code == 404
    assert (
        client.get(f"/api/projects/{pid}/bible", headers=studio["other"]).status_code
        == 404
    )


def test_a_project_with_no_scenes_at_all_is_not_granted(studio, client):
    """`Scene.project_id` is the join; an empty project must not fall through it."""
    empty = client.post(
        "/api/projects", json={"name": "EMPTY"}, headers=studio["admin"]
    ).json()["id"]
    _assign(client, studio, studio["eps"][0], studio["artist_id"])
    ids = [p["id"] for p in client.get("/api/projects", headers=studio["artist"]).json()]
    assert empty not in ids
