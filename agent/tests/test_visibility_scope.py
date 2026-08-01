"""Diagram 4 — what each person may see inside a project.

Being on a project used to mean seeing all of it: an artist handed one episode
could read every colleague's work in the same project. The diagram is narrower —
you see the subtree you are assigned to, plus the ancestors above it so you can
still navigate, but never your siblings.

The two cases the diagram draws:

    Worker assigned at Ep03   → Project, Series readable; Ep03 visible;
                                Ep01, Ep02 NOT visible
    Manager assigned at Series → Project readable; Series + every episode under
                                it visible
"""
from __future__ import annotations

import pytest

from flowboard.services import permissions, user_service


def _h(client, u, p="pw123456"):
    tok = client.post("/api/account/login", json={"username": u, "password": p}).json()
    return {"Authorization": f"Bearer {tok['token']}"}


@pytest.fixture()
def studio(client):
    """One project, two series, three episodes in the first — the diagram's shape."""
    user_service.create_user("boss", "pw123456", role="admin")
    ah = _h(client, "boss")
    pm = user_service.create_user("pm", "pw123456")
    worker = user_service.create_user("worker", "pw123456")
    manager = user_service.create_user("manager", "pw123456")
    other = user_service.create_user("other", "pw123456")

    pid = client.post(
        "/api/projects", json={"name": "MOGU", "owner_user_id": str(pm.id)}, headers=ah
    ).json()["id"]
    client.put(
        f"/api/projects/{pid}/members",
        json={
            "members": [
                {"user_id": str(pm.id), "role": "producer"},
                {"user_id": str(worker.id), "role": "artist"},
                {"user_id": str(manager.id), "role": "artist"},
                {"user_id": str(other.id), "role": "artist"},
            ]
        },
        headers=ah,
    )
    s1 = client.post(
        f"/api/projects/{pid}/series", json={"name": "S1", "code": "S1"}, headers=ah
    ).json()["id"]
    s2 = client.post(
        f"/api/projects/{pid}/series", json={"name": "S2", "code": "S2"}, headers=ah
    ).json()["id"]
    eps = {}
    for n in (1, 2, 3):
        eps[n] = client.post(
            f"/api/projects/{pid}/scenes",
            json={"name": f"EP0{n}", "code": f"EP0{n}", "series_id": s1},
            headers=ah,
        ).json()["id"]

    # Worker owns Ep03 only. Manager produces S2.
    client.patch(f"/api/scenes/{eps[3]}/assignee", json={"user_id": str(worker.id)}, headers=ah)
    client.patch(f"/api/series/{s2}/producer", json={"user_id": str(manager.id)}, headers=ah)
    ep_s2 = client.post(
        f"/api/projects/{pid}/scenes",
        json={"name": "S2-EP01", "series_id": s2},
        headers=ah,
    ).json()["id"]

    return {
        "ah": ah, "pid": pid, "s1": s1, "s2": s2, "eps": eps, "ep_s2": ep_s2,
        "h": {n: _h(client, n) for n in ("pm", "worker", "manager", "other")},
    }


# ── the worker case ────────────────────────────────────────────────────────


def test_worker_sees_only_the_episode_they_own(client, studio):
    got = client.get(f"/api/projects/{studio['pid']}/scenes", headers=studio["h"]["worker"])
    assert got.status_code == 200
    ids = {e["id"] for e in got.json()}
    assert ids == {studio["eps"][3]}          # Ep03 only…
    assert studio["eps"][1] not in ids        # …siblings stay hidden
    assert studio["eps"][2] not in ids


def test_worker_sees_the_series_holding_their_work_as_a_readable_ancestor(client, studio):
    got = client.get(f"/api/projects/{studio['pid']}/series", headers=studio["h"]["worker"])
    ids = {s["id"] for s in got.json()}
    assert studio["s1"] in ids   # ancestor of Ep03 — needed to navigate
    assert studio["s2"] not in ids  # nothing of theirs in there


def test_worker_cannot_open_a_colleagues_episode(client, studio):
    """404, not 403 — an episode they may not see must be indistinguishable
    from one that doesn't exist."""
    r = client.get(f"/api/scenes/{studio['eps'][1]}", headers=studio["h"]["worker"])
    assert r.status_code == 404


def test_worker_cannot_read_a_colleagues_canvas(client, studio):
    r = client.get(f"/api/scenes/{studio['eps'][2]}/canvas", headers=studio["h"]["worker"])
    assert r.status_code == 404


def test_worker_cannot_add_a_sequence_to_a_colleagues_episode(client, studio):
    r = client.post(f"/api/scenes/{studio['eps'][1]}/shots", json={}, headers=studio["h"]["worker"])
    assert r.status_code == 404


def test_worker_can_work_inside_their_own_episode(client, studio):
    """The scope must not get in the way of the work it exists to protect."""
    ep = studio["eps"][3]
    assert client.get(f"/api/scenes/{ep}", headers=studio["h"]["worker"]).status_code == 200
    seq = client.post(f"/api/scenes/{ep}/shots", json={}, headers=studio["h"]["worker"])
    assert seq.status_code == 200
    assert client.patch(
        f"/api/shots/{seq.json()['id']}",
        json={"script_text": "int. bar"},
        headers=studio["h"]["worker"],
    ).status_code == 200


# ── the manager case ───────────────────────────────────────────────────────


def test_manager_assigned_at_a_series_sees_every_episode_under_it(client, studio):
    got = client.get(f"/api/projects/{studio['pid']}/scenes", headers=studio["h"]["manager"])
    ids = {e["id"] for e in got.json()}
    assert studio["ep_s2"] in ids            # the whole subtree is theirs
    assert studio["eps"][3] not in ids       # S1's episodes are not


def test_manager_sees_their_series_but_not_the_other(client, studio):
    ids = {
        s["id"]
        for s in client.get(
            f"/api/projects/{studio['pid']}/series", headers=studio["h"]["manager"]
        ).json()
    }
    assert ids == {studio["s2"]}


# ── unassigned + managers ──────────────────────────────────────────────────


def test_an_artist_with_no_assignment_sees_nothing_inside_the_project(client, studio):
    """Deliberate: access follows assignment, so being added to a project grants
    nothing until work is handed over."""
    assert client.get(
        f"/api/projects/{studio['pid']}/scenes", headers=studio["h"]["other"]
    ).json() == []
    assert client.get(
        f"/api/projects/{studio['pid']}/series", headers=studio["h"]["other"]
    ).json() == []


def test_the_pm_still_sees_the_whole_project(client, studio):
    """Producers and leads build the structure others get assigned to — scoping
    them to their own assignments would stop them doing their job."""
    eps = client.get(f"/api/projects/{studio['pid']}/scenes", headers=studio["h"]["pm"]).json()
    assert len(eps) == 4
    ser = client.get(f"/api/projects/{studio['pid']}/series", headers=studio["h"]["pm"]).json()
    assert len(ser) == 2


def test_admin_is_never_scoped(client, studio):
    eps = client.get(f"/api/projects/{studio['pid']}/scenes", headers=studio["ah"]).json()
    assert len(eps) == 4


def test_only_artists_and_viewers_are_narrowed():
    assert permissions.is_scoped(permissions.ARTIST) is True
    assert permissions.is_scoped(permissions.VIEWER) is True
    assert permissions.is_scoped(permissions.LEAD) is False
    assert permissions.is_scoped(permissions.PRODUCER) is False
    assert permissions.is_scoped(permissions.ADMIN) is False


# ── reassignment moves the boundary ────────────────────────────────────────


def test_reassigning_an_episode_moves_access_with_it(client, studio):
    """Access derives from the assignment, so taking work back removes the
    access — nothing to keep in sync by hand."""
    ep, ah = studio["eps"][3], studio["ah"]
    wh = studio["h"]["worker"]
    assert client.get(f"/api/scenes/{ep}", headers=wh).status_code == 200

    client.patch(f"/api/scenes/{ep}/assignee", json={"user_id": None}, headers=ah)
    assert client.get(f"/api/scenes/{ep}", headers=wh).status_code == 404
