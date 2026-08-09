"""Five attempts per sequence, and a PM decides whether there is a sixth.

A per-person budget stops somebody spending and has no opinion about where the
money goes. It does nothing about one shot quietly eating a season's worth of
tries, which is what this is for.

Most of these are about what counts as an attempt. Getting that wrong is not
visible: too generous and the ceiling never bites, too strict and an artist
spends their five on the gateway having a bad afternoon.
"""
from __future__ import annotations

import pytest

from flowboard.db import get_session
from flowboard.db.models import Node, Request, Shot
from flowboard.services import sequence_quota as sq
from flowboard.services import user_service
from tests.conftest import make_shot


def _h(client, u, p="pw123456"):
    r = client.post("/api/account/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture()
def seq(client):
    """One sequence with a node on it — the thing a generation attaches to."""
    boss = user_service.create_user("sq_admin", "pw123456", role="admin")
    # Video generation is metered, so an account with no credit is refused for
    # money before this ceiling is ever reached. Fund it, or every test here
    # would be measuring the budget instead.
    user_service.set_budget(boss.id, 500)
    b = make_shot(client)
    node = client.post(
        "/api/nodes",
        json={"shot_id": b["id"], "type": "video", "x": 0, "y": 0, "data": {}},
    ).json()
    return {
        "h": _h(client, "sq_admin"),
        "shot_id": b["id"],
        "node_id": node["id"],
        "project_id": b["project_id"],
    }


def _ran(node_id, n=1, *, kind="gen_video", status="done"):
    with get_session() as s:
        for _ in range(n):
            s.add(Request(node_id=node_id, type=kind, status=status, params={}))
        s.commit()


def _gen(client, seq):
    return client.post(
        "/api/requests",
        json={"node_id": seq["node_id"], "type": "gen_video", "params": {}},
        headers=seq["h"],
    )


# ── what counts as an attempt ───────────────────────────────────────────────


def test_the_house_limit_is_five(client):
    assert sq.DEFAULT_LIMIT == 5


def test_one_request_is_one_attempt_however_many_variants(client, seq):
    """The artist tried one thing four ways. That is one idea, and charging four
    would make variants too expensive to use for what they are for."""
    with get_session() as s:
        s.add(Request(node_id=seq["node_id"], type="gen_video", status="done",
                      params={"variant_count": 4}))
        s.commit()
        assert sq.used(s, seq["shot_id"]) == 1


def test_a_failed_run_does_not_spend_an_attempt(client, seq):
    """It produced nothing. Charging for an upstream failure spends the
    artist's five on the gateway having a bad afternoon."""
    _ran(seq["node_id"], 3, status="error")
    with get_session() as s:
        assert sq.used(s, seq["shot_id"]) == 0


def test_analysing_an_image_is_not_a_generation(client, seq):
    """`vision` looks at an image the artist already has. Charging an attempt
    for it would punish looking closely at your own work."""
    _ran(seq["node_id"], 4, kind="vision")
    with get_session() as s:
        assert sq.used(s, seq["shot_id"]) == 0


def test_attempts_are_counted_across_every_node_of_the_sequence(client, seq):
    """The ceiling is on the SEQUENCE. Counting per node would let anyone past
    it by adding a second node."""
    second = client.post(
        "/api/nodes",
        json={"shot_id": seq["shot_id"], "type": "video", "x": 300, "y": 0, "data": {}},
    ).json()
    _ran(seq["node_id"], 2)
    _ran(second["id"], 2)
    with get_session() as s:
        assert sq.used(s, seq["shot_id"]) == 4


def test_another_sequences_attempts_do_not_count(client, seq):
    other = make_shot(client)
    n = client.post(
        "/api/nodes",
        json={"shot_id": other["id"], "type": "video", "x": 0, "y": 0, "data": {}},
    ).json()
    _ran(n["id"], 5)
    with get_session() as s:
        assert sq.used(s, seq["shot_id"]) == 0


# ── it actually stops ───────────────────────────────────────────────────────


def test_the_sixth_generation_is_refused(client, seq):
    _ran(seq["node_id"], 4)
    assert _gen(client, seq).status_code == 200      # the fifth
    r = _gen(client, seq)                            # the sixth
    assert r.status_code == 423, r.text
    body = r.json()["detail"]
    assert body["used"] == 5 and body["limit"] == 5


def test_a_refusal_leaves_no_request_behind(client, seq):
    """Checked before the row exists, so it does not itself count against the
    limit on the retry."""
    _ran(seq["node_id"], 5)
    with get_session() as s:
        before = len(s.exec(__import__("sqlmodel").select(Request)).all())
    _gen(client, seq)
    with get_session() as s:
        assert len(s.exec(__import__("sqlmodel").select(Request)).all()) == before


def test_a_generation_with_no_sequence_is_not_affected(client, seq):
    """Panel generation carries no node. It has its own cap and must not fall
    through this one."""
    r = client.post(
        "/api/requests",
        json={"type": "flow_gen_image", "params": {"provider": "atrium"}},
        headers=seq["h"],
    )
    assert r.status_code != 423


# ── the queue a PM looks at ─────────────────────────────────────────────────


def test_a_sequence_appears_on_the_list_only_once_it_is_out(client, seq):
    _ran(seq["node_id"], 4)
    with get_session() as s:
        assert sq.blocked(s) == []
    _ran(seq["node_id"], 1)
    with get_session() as s:
        rows = sq.blocked(s)
    assert len(rows) == 1
    assert rows[0]["shot_id"] == seq["shot_id"]
    assert rows[0]["used"] == 5


def test_the_list_says_where_the_sequence_lives(client, seq):
    """A PM cannot judge "SQ01 is stuck" without knowing which episode."""
    _ran(seq["node_id"], 5)
    with get_session() as s:
        row = sq.blocked(s)[0]
    assert row["episode"] and row["project_id"]


def test_the_list_is_read_fresh_so_a_freed_sequence_leaves_it(client, seq):
    """Nothing is recorded when the limit is hit, so nothing has to be taken
    off the list — the day that is forgotten a PM is looking at work that is
    already moving again."""
    _ran(seq["node_id"], 5)
    with get_session() as s:
        assert len(sq.blocked(s)) == 1
        sq.unlock(s, seq["shot_id"], extra=5)
        assert sq.blocked(s) == []


# ── unlocking ───────────────────────────────────────────────────────────────


def test_unlocking_lets_the_work_continue(client, seq):
    _ran(seq["node_id"], 5)
    assert _gen(client, seq).status_code == 423

    r = client.post(
        f"/api/admin/sequences/{seq['shot_id']}/unlock",
        json={"extra": 3}, headers=seq["h"],
    )
    assert r.status_code == 200, r.text
    assert r.json()["remaining"] == 3
    assert _gen(client, seq).status_code == 200


def test_the_grant_is_counted_from_where_the_sequence_is(client, seq):
    """`used + extra`, not `limit + extra`. Those differ once a sequence is past
    its ceiling, and adding to the old limit would hand back fewer attempts than
    the number on the button."""
    _ran(seq["node_id"], 7)          # past five: a run in flight is not refused
    with get_session() as s:
        out = sq.unlock(s, seq["shot_id"], extra=2)
    assert out["remaining"] == 2, "the grant was eaten by the overshoot"


def test_relocking_puts_it_back_on_the_house_default(client, seq):
    _ran(seq["node_id"], 5)
    client.post(f"/api/admin/sequences/{seq['shot_id']}/unlock",
                json={"extra": 5}, headers=seq["h"])
    r = client.post(f"/api/admin/sequences/{seq['shot_id']}/relock", headers=seq["h"])
    assert r.status_code == 200
    assert r.json()["limit"] == sq.DEFAULT_LIMIT
    assert r.json()["locked"] is True


def test_only_staff_can_unlock(client, seq):
    """The point of the ceiling is that the person who has just rolled five
    times is not the one who decides there is a sixth."""
    user_service.create_user("sq_artist", "pw123456")
    _ran(seq["node_id"], 5)
    r = client.post(
        f"/api/admin/sequences/{seq['shot_id']}/unlock",
        json={"extra": 5}, headers=_h(client, "sq_artist"),
    )
    assert r.status_code == 403


# ── video only ──────────────────────────────────────────────────────────────


def test_making_a_reference_image_is_not_a_take(client, seq):
    """The ceiling is about video takes, and the five is calibrated to that.

    `gen_image` and `edit_image` build and fix the references a take is made
    FROM, and they carry a node exactly like a video does. Counting them would
    make the artist ration the preparation that stops them wasting takes, which
    is backwards.
    """
    for kind in ("gen_image", "edit_image", "gen_storyboard", "retry_storyboard_shot"):
        _ran(seq["node_id"], 3, kind=kind)
    with get_session() as s:
        assert sq.used(s, seq["shot_id"]) == 0
    # …and generating is still allowed.
    assert _gen(client, seq).status_code == 200


def test_only_video_is_counted(client, seq):
    _ran(seq["node_id"], 2, kind="gen_image")
    _ran(seq["node_id"], 2, kind="gen_video")
    with get_session() as s:
        assert sq.used(s, seq["shot_id"]) == 2


def test_panel_generation_is_capped_elsewhere_and_not_here(client, seq):
    """`flow_gen_image` was on this list and should not have been: it is the
    comic path, capped by its own daily image quota, and it carries no node —
    so it never bit. A line that is harmless only because nothing reaches it is
    one refactor from being wrong."""
    _ran(seq["node_id"], 9, kind="flow_gen_image")
    with get_session() as s:
        assert sq.used(s, seq["shot_id"]) == 0
