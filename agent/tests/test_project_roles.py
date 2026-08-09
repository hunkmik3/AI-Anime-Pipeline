"""Phase 10: the per-project role matrix (producer | lead | artist | viewer).

Companion to ``test_project_structure_roles.py``, which covers the admin/owner
split. Here each role is staffed onto one project and we assert exactly where
its ceiling is — the point being that a role only ever *widens* downward:
producer ⊃ lead ⊃ artist ⊃ viewer.
"""
from __future__ import annotations

import pytest

from flowboard.services import permissions, user_service


def _h(client, u, p="pw123456"):
    token = client.post("/api/account/login", json={"username": u, "password": p}).json()
    return {"Authorization": f"Bearer {token['token']}"}


@pytest.fixture()
def staffed(client):
    """One project owned by ``prod``, with one member per non-owner role."""
    user_service.create_user("boss", "pw123456", role="admin")
    ah = _h(client, "boss")

    users = {}
    for name in ("prod", "artist", "viewer"):
        users[name] = user_service.create_user(name, "pw123456")

    pid = client.post(
        "/api/projects",
        json={"name": "Revenge Mafia", "owner_user_id": str(users["prod"].id)},
        headers=ah,
    ).json()["id"]

    roster = {
        "members": [
            {"user_id": str(users["prod"].id), "role": "producer"},
            {"user_id": str(users["artist"].id), "role": "artist"},
            {"user_id": str(users["viewer"].id), "role": "viewer"},
        ]
    }
    r = client.put(f"/api/projects/{pid}/members", json=roster, headers=ah)
    assert r.status_code == 200
    assert {m["role"] for m in r.json()["members"]} == {
        "producer",
        "artist",
        "viewer",
    }

    return {
        "pid": pid,
        "admin": ah,
        "users": users,
        "h": {name: _h(client, name) for name in users},
    }


def test_reported_role_matches_assignment(client, staffed):
    for name in ("prod", "artist", "viewer"):
        got = client.get(f"/api/projects/{staffed['pid']}", headers=staffed["h"][name]).json()
        expected = {"prod": "producer"}.get(name, name)
        assert got["my_role"] == expected
        # the can-map the UI reads is consistent with the role
        assert got["can"]["canvas.read"] is True
        assert got["can"]["project.manage"] is False


def test_only_the_pm_shapes_the_project(client, staffed):
    """Create, rename and delete a tier are all the PM's now.

    There used to be a `lead` between PM and artist holding rename but not
    create, delete-a-sequence but not delete-an-episode. The studio treats a
    lead and a PM as the same person, so that was one job under two names split
    by a line nobody could state. With the rank gone the split has nobody to
    belong to, so the whole tier moves together.
    """
    pid, h = staffed["pid"], staffed["h"]

    for headers, ok in ((h["artist"], False), (h["prod"], True)):
        r = client.post(
            f"/api/projects/{pid}/series", json={"name": "Season 1"}, headers=headers
        )
        assert (r.status_code == 200) is ok, r.text
        if ok:
            sid = r.json()["id"]

    assert client.patch(
        f"/api/series/{sid}", json={"name": "Season One"}, headers=h["artist"]
    ).status_code == 403
    assert client.patch(
        f"/api/series/{sid}", json={"name": "Season One"}, headers=h["prod"]
    ).status_code == 200
    assert client.delete(f"/api/series/{sid}", headers=h["prod"]).status_code == 200


def test_lead_is_read_as_pm_not_demoted_to_artist(client, staffed):
    """A row stored before the merge must come back as a PM.

    The unknown-value fallback is `artist`, so dropping the role from the list
    and letting it fall through would have silently demoted every lead — the
    wrong direction, and the kind of change nobody notices until somebody
    cannot do their job.
    """
    from flowboard.db import get_session
    from flowboard.db.models import ProjectMember
    from sqlmodel import select

    pid, h, users = staffed["pid"], staffed["h"], staffed["users"]
    with get_session() as s:
        row = s.exec(
            select(ProjectMember).where(
                ProjectMember.project_id == pid,
                ProjectMember.user_id == users["artist"].id,
            )
        ).first()
        row.role = "lead"          # as an old database holds it
        s.add(row)
        s.commit()

    assert permissions.normalize_role("lead") == permissions.PRODUCER
    # …and the real gate agrees: they can now do what only a PM can.
    assert client.post(
        f"/api/projects/{pid}/series", json={"name": "S"}, headers=h["artist"]
    ).status_code == 200


def test_nobody_can_delete_an_episode_they_could_not_recreate(client, staffed):
    """Create and delete stay on the same rank, whichever rank that is.

    The pairing is the point, not the level: whoever can remove an episode must
    be able to put it back. Leaving one half a rank below the other hands
    somebody the destructive move without the one that undoes it.
    """
    pid, h = staffed["pid"], staffed["h"]
    ep = client.post(
        f"/api/projects/{pid}/scenes", json={"name": "EP1"}, headers=h["prod"]
    ).json()["id"]

    # An artist can neither make one nor remove one.
    assert client.post(
        f"/api/projects/{pid}/scenes", json={"name": "EP2"}, headers=h["artist"]
    ).status_code == 403
    assert client.delete(f"/api/scenes/{ep}", headers=h["artist"]).status_code == 403

    assert client.delete(f"/api/scenes/{ep}", headers=h["prod"]).status_code == 200


def test_every_episode_lands_in_a_series_even_when_none_is_named(client, staffed):
    """`series_id` is NOT NULL now, and the create route does not require it —
    an episode asked for without one is adopted by the project's series rather
    than refused. Both halves matter: the hierarchy is guaranteed, and the
    caller does not have to know the tier below to use the tier above."""
    pid, h = staffed["pid"], staffed["h"]
    ep = client.post(
        f"/api/projects/{pid}/scenes", json={"name": "EP1"}, headers=h["prod"]
    )
    assert ep.status_code == 200
    assert ep.json()["series_id"], "an episode came back with no series"


def test_artist_works_in_sequences_but_cannot_restructure(client, staffed):
    pid, h = staffed["pid"], staffed["h"]
    ep = client.post(
        f"/api/projects/{pid}/scenes", json={"name": "EP1"}, headers=h["prod"]
    ).json()["id"]
    # Being on the project is no longer enough to work in an episode — the
    # artist has to be assigned to it (see test_visibility_scope.py). That
    # assignment is step "PM gán Employee" in the workflow.
    client.patch(
        f"/api/scenes/{ep}/assignee",
        json={"user_id": str(staffed["users"]["artist"].id)},
        headers=h["prod"],
    )

    # artist may add + edit a sequence and save its node graph
    seq = client.post(f"/api/scenes/{ep}/shots", json={}, headers=h["artist"])
    assert seq.status_code == 200
    seq_id = seq.json()["id"]
    assert client.patch(
        f"/api/shots/{seq_id}", json={"script_text": "int. bar"}, headers=h["artist"]
    ).status_code == 200
    assert client.put(
        f"/api/shots/{seq_id}/workflow", json={"nodes": [], "edges": []}, headers=h["artist"]
    ).status_code == 200

    # but not create episodes, delete sequences, or touch the roster
    assert client.post(
        f"/api/projects/{pid}/scenes", json={"name": "EP2"}, headers=h["artist"]
    ).status_code == 403
    assert client.delete(f"/api/shots/{seq_id}", headers=h["artist"]).status_code == 403
    assert client.put(
        f"/api/projects/{pid}/members", json={"members": []}, headers=h["artist"]
    ).status_code == 403


def test_viewer_is_read_only(client, staffed):
    pid, h = staffed["pid"], staffed["h"]
    ep = client.post(
        f"/api/projects/{pid}/scenes", json={"name": "EP1"}, headers=h["prod"]
    ).json()["id"]

    assert client.get(f"/api/projects/{pid}/scenes", headers=h["viewer"]).status_code == 200
    assert client.get(f"/api/projects/{pid}/series", headers=h["viewer"]).status_code == 200
    assert client.post(f"/api/scenes/{ep}/shots", json={}, headers=h["viewer"]).status_code == 403
    assert client.post(
        f"/api/projects/{pid}/series", json={"name": "S2"}, headers=h["viewer"]
    ).status_code == 403


def test_producer_staffs_the_project_without_an_admin(client, staffed):
    """The whole point of the role tier: a producer runs their own roster."""
    pid, h = staffed["pid"], staffed["h"]
    newbie = user_service.create_user("newbie", "pw123456")

    current = client.get(f"/api/projects/{pid}/members", headers=h["prod"]).json()["members"]
    roster = [{"user_id": m["user_id"], "role": m["role"]} for m in current]
    roster.append({"user_id": str(newbie.id), "role": "artist"})

    r = client.put(f"/api/projects/{pid}/members", json={"members": roster}, headers=h["prod"])
    assert r.status_code == 200
    assert client.get(f"/api/projects/{pid}", headers=_h(client, "newbie")).json()[
        "my_role"
    ] == "artist"


def test_stranger_sees_404_not_403(client, staffed):
    """A user with no role must not learn the project exists."""
    user_service.create_user("outsider", "pw123456")
    oh = _h(client, "outsider")
    pid = staffed["pid"]
    assert client.get(f"/api/projects/{pid}/series", headers=oh).status_code == 404
    assert client.post(
        f"/api/projects/{pid}/series", json={"name": "X"}, headers=oh
    ).status_code == 404


def test_series_with_episodes_refuses_deletion(client, staffed):
    pid, h = staffed["pid"], staffed["h"]
    sid = client.post(
        f"/api/projects/{pid}/series", json={"name": "Season 2"}, headers=h["prod"]
    ).json()["id"]
    client.post(
        f"/api/projects/{pid}/scenes", json={"name": "EP1", "series_id": sid}, headers=h["prod"]
    )
    r = client.delete(f"/api/series/{sid}", headers=h["prod"])
    assert r.status_code == 409


def test_unit_label_switches_episode_to_chapter(client, staffed):
    pid, h = staffed["pid"], staffed["h"]
    r = client.post(
        f"/api/projects/{pid}/series",
        json={"name": "Volume 1", "code": "V1", "unit_label": "Chapter"},
        headers=h["prod"],
    )
    assert r.status_code == 200 and r.json()["unit_label"] == "Chapter"


def test_capability_matrix_is_monotonic():
    """Every capability a lower role has, a higher role must also have —
    otherwise the UI's can-map would contradict the hierarchy."""
    order = [
        permissions.VIEWER,
        permissions.ARTIST,
        permissions.PRODUCER,
        permissions.ADMIN,
    ]
    for lower, higher in zip(order, order[1:]):
        low = permissions.capability_map(lower)
        high = permissions.capability_map(higher)
        for cap, allowed in low.items():
            if allowed:
                assert high[cap], f"{higher} lost {cap} that {lower} has"
