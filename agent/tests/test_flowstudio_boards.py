"""A studio board belongs to whoever made it.

The studio arrived as one shared list with no owner column at all, so this is not
a case of a check being wrong — there was nothing to check against. That makes
the tests worth writing carefully: the failure mode is not "the guard rejects the
wrong person", it is "the guard was never reached", and a test that only asserts
the happy path passes identically either way.

So every test here uses TWO accounts and asserts from both sides.
"""
from __future__ import annotations

from flowboard.services import user_service


def _login(client, username, password="pw123456"):
    r = client.post("/api/account/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _two(client):
    """Two ordinary accounts. One is never enough: with a single user, a route
    that forgot to filter at all returns exactly what a correct one does."""
    user_service.create_user("bd_a", "pw123456")
    user_service.create_user("bd_b", "pw123456")
    return _login(client, "bd_a"), _login(client, "bd_b")


def _make(client, headers, name):
    r = client.post("/api/flowstudio/boards", json={"name": name}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["id"]


# ── seeing ──────────────────────────────────────────────────────────────────


def test_you_see_your_own_boards_and_not_your_colleagues(client):
    a, b = _two(client)
    _make(client, a, "A's board")
    _make(client, b, "B's board")

    mine = client.get("/api/flowstudio/boards", headers=a).json()
    assert [x["name"] for x in mine] == ["A's board"]
    theirs = client.get("/api/flowstudio/boards", headers=b).json()
    assert [x["name"] for x in theirs] == ["B's board"]


def test_an_admin_sees_every_board(client):
    a, b = _two(client)
    user_service.create_user("bd_boss", "pw123456", role="admin")
    _make(client, a, "A's board")
    _make(client, b, "B's board")

    seen = client.get("/api/flowstudio/boards", headers=_login(client, "bd_boss")).json()
    assert {x["name"] for x in seen} == {"A's board", "B's board"}


def test_an_ownerless_board_is_visible_to_admins_only(client):
    """NULL owner is a board from before the column existed. "We do not know
    whose this is" is a reason to show it to fewer people, not more."""
    from flowboard.db import get_session
    from flowboard.db.models import FlowBoard

    a, _ = _two(client)
    with get_session() as s:
        s.add(FlowBoard(name="legacy", owner_user_id=None))
        s.commit()
    user_service.create_user("bd_boss", "pw123456", role="admin")

    assert "legacy" not in {x["name"] for x in client.get(
        "/api/flowstudio/boards", headers=a).json()}
    assert "legacy" in {x["name"] for x in client.get(
        "/api/flowstudio/boards", headers=_login(client, "bd_boss")).json()}


# ── touching ────────────────────────────────────────────────────────────────


def test_renaming_a_colleagues_board_is_a_404_not_a_403(client):
    """403 admits the row exists, which is enough to enumerate what colleagues
    are working on. A board you may not see must look like one that was never
    created."""
    a, b = _two(client)
    bid = _make(client, a, "A's board")

    r = client.patch(f"/api/flowstudio/boards/{bid}", json={"name": "mine now"}, headers=b)
    assert r.status_code == 404
    assert r.status_code == client.patch(
        "/api/flowstudio/boards/999999", json={"name": "x"}, headers=b
    ).status_code, "a real board and a missing one must answer the same"

    # And it really was not renamed.
    assert client.get("/api/flowstudio/boards", headers=a).json()[0]["name"] == "A's board"


def test_deleting_a_colleagues_board_is_refused(client):
    a, b = _two(client)
    bid = _make(client, a, "A's board")

    assert client.delete(f"/api/flowstudio/boards/{bid}", headers=b).status_code == 404
    assert len(client.get("/api/flowstudio/boards", headers=a).json()) == 1


def test_you_can_still_do_everything_to_your_own(client):
    """The scope must not get in the way of the work it exists to protect."""
    a, _ = _two(client)
    bid = _make(client, a, "Mine")

    assert client.patch(
        f"/api/flowstudio/boards/{bid}", json={"name": "Renamed"}, headers=a
    ).status_code == 200
    assert client.get("/api/flowstudio/boards", headers=a).json()[0]["name"] == "Renamed"
    assert client.delete(f"/api/flowstudio/boards/{bid}", headers=a).status_code == 200
    assert client.get("/api/flowstudio/boards", headers=a).json() == []


def test_an_admin_can_clean_up_anyones_board(client):
    a, _ = _two(client)
    bid = _make(client, a, "A's board")
    user_service.create_user("bd_boss", "pw123456", role="admin")
    boss = _login(client, "bd_boss")

    assert client.patch(
        f"/api/flowstudio/boards/{bid}", json={"name": "Tidied"}, headers=boss
    ).status_code == 200
    assert client.delete(f"/api/flowstudio/boards/{bid}", headers=boss).status_code == 200


def test_deleting_a_board_keeps_the_images(client):
    """Unchanged behaviour, pinned because ownership touched this route: the
    references are the user's library and outlive the board that made them."""
    from flowboard.db import get_session
    from flowboard.db.models import Reference
    from sqlmodel import select

    a, _ = _two(client)
    bid = _make(client, a, "Mine")
    with get_session() as s:
        s.add(Reference(media_id="m-keepme", kind="image", source_board_id=bid))
        s.commit()

    assert client.delete(f"/api/flowstudio/boards/{bid}", headers=a).status_code == 200
    with get_session() as s:
        kept = s.exec(select(Reference).where(Reference.media_id == "m-keepme")).first()
        assert kept is not None, "deleting a board threw away the images"
        assert kept.source_board_id is None
