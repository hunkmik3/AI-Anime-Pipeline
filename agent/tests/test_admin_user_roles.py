"""What one person holds, across both products, from one screen.

The roles the studio talks about — "PM giantflow", "artist giantstudio" — were
always expressible: a producer row on a comic, an artist row on a project. What
was missing was anywhere to see them. Membership could only be asked per project
and per comic, so "what does this person have" meant opening every one of them.

Two things these go after specifically:

  * that granting from here does not disturb anyone else on the project — the
    admin console shows one person and knows nothing about the rest of the
    roster, so a partial view must not be able to write a total one;
  * that the two products stay separate. They are different tables with
    different guards, and a grant landing on the wrong one would look right on
    the screen it was made from.
"""
from __future__ import annotations

import pytest

from flowboard.db import get_session
from flowboard.services import panel_service as pn
from flowboard.services import user_service


def _h(client, u, p="pw123456"):
    r = client.post("/api/account/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture()
def world(client):
    """An owner, a manager, and two workers; a studio project and a comic."""
    user_service.create_user("ur_owner", "pw123456", role="admin")
    user_service.create_user("ur_mgr", "pw123456", role="manager")
    a = user_service.create_user("ur_a", "pw123456")
    b = user_service.create_user("ur_b", "pw123456")
    owner = _h(client, "ur_owner")
    boss = user_service.get_by_username("ur_owner")

    # Owned explicitly: `POST /api/projects` leaves owner_user_id NULL unless the
    # admin names one, and an ownerless project cannot exercise the owner rules.
    pid = client.post(
        "/api/projects",
        json={"name": "MoguTV", "owner_user_id": str(boss.id)},
        headers=owner,
    ).json()["id"]
    with get_session() as s:
        slate = pn.create_project(s, "Comics")
        comic = pn.create_series(s, slate.id, "26001_MAGMEL")
        comic_id = comic.id
    return {
        "owner": owner, "mgr": _h(client, "ur_mgr"),
        "a": a, "b": b, "a_h": _h(client, "ur_a"),
        "pid": pid, "comic_id": comic_id,
    }


def _roles(client, w, who, headers=None):
    r = client.get(f"/api/admin/users/{who.id}/roles", headers=headers or w["owner"])
    assert r.status_code == 200, r.text
    return r.json()


# ── the question nothing could answer before ────────────────────────────────


def test_one_request_answers_what_this_person_holds_on_both_sides(client, world):
    w = world
    client.put(
        f"/api/admin/users/{w['a'].id}/roles/studio/{w['pid']}",
        json={"role": "producer"}, headers=w["owner"],
    )
    client.put(
        f"/api/admin/users/{w['a'].id}/roles/flow/{w['comic_id']}",
        json={"role": "producer"}, headers=w["owner"],
    )

    got = _roles(client, w, w["a"])
    assert [(x["name"], x["role"]) for x in got["studio"]] == [("MoguTV", "producer")]
    assert [(x["name"], x["role"]) for x in got["flow"]] == [("26001_MAGMEL", "producer")]
    assert got["system_role"] == "user", "a project role is not a system role"


def test_someone_with_nothing_reads_as_nothing(client, world):
    got = _roles(client, world, world["b"])
    assert got["studio"] == []
    assert got["flow"] == []


# ── the two products do not leak into each other ────────────────────────────


def test_a_studio_grant_gives_nothing_in_giantflow(client, world):
    """Different tables, different guards. A grant landing on the wrong one
    would look right on the screen it was made from."""
    w = world
    client.put(
        f"/api/admin/users/{w['a'].id}/roles/studio/{w['pid']}",
        json={"role": "producer"}, headers=w["owner"],
    )
    got = _roles(client, w, w["a"])
    assert got["flow"] == []
    # …and the real gate agrees.
    me = client.get("/api/flowstudio/me", headers=w["a_h"]).json()
    assert me["capabilities"]["panel.review"] is False


def test_a_flow_grant_gives_nothing_in_giantstudio(client, world):
    w = world
    client.put(
        f"/api/admin/users/{w['a'].id}/roles/flow/{w['comic_id']}",
        json={"role": "producer"}, headers=w["owner"],
    )
    assert _roles(client, w, w["a"])["studio"] == []
    # A PM of a comic cannot build the production project.
    assert client.post(
        f"/api/projects/{w['pid']}/series", json={"name": "S1"}, headers=w["a_h"]
    ).status_code in (403, 404)


# ── granting from here does not disturb the rest of the roster ──────────────


def test_granting_one_person_leaves_everyone_else_on_the_project(client, world):
    """The console has one person in front of it and no idea who else is on the
    project. The whole-roster call would have removed them."""
    w = world
    client.put(
        f"/api/admin/users/{w['b'].id}/roles/studio/{w['pid']}",
        json={"role": "artist"}, headers=w["owner"],
    )
    client.put(
        f"/api/admin/users/{w['a'].id}/roles/studio/{w['pid']}",
        json={"role": "producer"}, headers=w["owner"],
    )

    roster = client.get(f"/api/projects/{w['pid']}/members", headers=w["owner"]).json()
    got = {m["user_id"]: m["role"] for m in roster["members"]}
    assert got[str(w["b"].id)] == "artist", "granting A removed B"
    assert got[str(w["a"].id)] == "producer"


def test_changing_a_role_replaces_it_rather_than_adding_a_second(client, world):
    w = world
    for role in ("viewer", "artist", "producer"):
        client.put(
            f"/api/admin/users/{w['a'].id}/roles/studio/{w['pid']}",
            json={"role": role}, headers=w["owner"],
        )
    studio = _roles(client, w, w["a"])["studio"]
    assert len(studio) == 1
    assert studio[0]["role"] == "producer"


# ── revoking ────────────────────────────────────────────────────────────────


def test_revoking_takes_the_role_away(client, world):
    w = world
    client.put(
        f"/api/admin/users/{w['a'].id}/roles/flow/{w['comic_id']}",
        json={"role": "artist"}, headers=w["owner"],
    )
    r = client.delete(
        f"/api/admin/users/{w['a'].id}/roles/flow/{w['comic_id']}", headers=w["owner"]
    )
    assert r.status_code == 200
    assert r.json()["flow"] == []


def test_revoking_something_they_never_had_is_not_an_error(client, world):
    """The caller asked for them to be off it, and they are."""
    w = world
    assert client.delete(
        f"/api/admin/users/{w['a'].id}/roles/studio/{w['pid']}", headers=w["owner"]
    ).status_code == 200


def test_an_owner_cannot_be_revoked_off_their_own_project(client, world):
    """They are a producer implicitly, with no member row to delete — the
    request would report success and change nothing."""
    w = world
    owner_user = user_service.get_by_username("ur_owner")
    listed = _roles(client, w, owner_user)["studio"]
    assert listed and listed[0]["is_owner"] is True, "an owner reads as having no role"

    r = client.delete(
        f"/api/admin/users/{owner_user.id}/roles/studio/{w['pid']}", headers=w["owner"]
    )
    assert r.status_code == 409
    assert "own" in r.json()["detail"]


# ── who may do this ─────────────────────────────────────────────────────────


def test_a_studio_manager_can_staff_projects(client, world):
    """Provisioning the people who do the work is the manager's job."""
    w = world
    assert client.put(
        f"/api/admin/users/{w['a'].id}/roles/studio/{w['pid']}",
        json={"role": "artist"}, headers=w["mgr"],
    ).status_code == 200


def test_a_member_cannot_grant_themselves_anything(client, world):
    w = world
    assert client.put(
        f"/api/admin/users/{w['a'].id}/roles/studio/{w['pid']}",
        json={"role": "producer"}, headers=w["a_h"],
    ).status_code == 403


def test_granting_onto_something_that_does_not_exist_is_a_404(client, world):
    import uuid as _uuid

    w = world
    assert client.put(
        f"/api/admin/users/{w['a'].id}/roles/studio/{_uuid.uuid4()}",
        json={"role": "artist"}, headers=w["owner"],
    ).status_code == 404
    assert client.put(
        f"/api/admin/users/{w['a'].id}/roles/flow/999999",
        json={"role": "artist"}, headers=w["owner"],
    ).status_code == 404
