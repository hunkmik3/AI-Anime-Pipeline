"""An episode's status answers itself until somebody answers it.

`production["status"]` is a dropdown, and dropdowns do not get updated by people
in the middle of the work they describe. Every episode read NotStarted while takes
were running in them, so the column — and the rollup quoted off it — said nothing
had started.

Most of these tests are about what must NOT be overwritten. Filling in a blank is
easy; the risk is a derived value trampling a decision somebody made on purpose,
and "Dropped" quietly becoming "Production" again is the one that costs real
money.
"""
from __future__ import annotations

import pytest

from flowboard.db import get_session
from flowboard.db.models import Request, Scene
from flowboard.services import scene_service as ss
from flowboard.services import series_service as sers


@pytest.fixture()
def ep(client):
    """A project → series → one episode, with nothing in it."""
    pid = client.post("/api/projects", json={"name": "MOGU"}).json()["id"]
    sid = client.post(f"/api/projects/{pid}/series", json={"name": "S"}).json()["id"]
    scene = client.post(
        f"/api/projects/{pid}/scenes", json={"name": "EP1", "series_id": sid}
    ).json()
    return {"project_id": pid, "series_id": sid, "id": scene["id"]}


def _status(client, ep) -> str:
    return client.get(f"/api/scenes/{ep['id']}").json()["production"]["status"]


def _seq(client, ep):
    return client.post(f"/api/scenes/{ep['id']}/shots", json={}).json()


def _node(client, shot_id):
    return client.post(
        "/api/nodes",
        json={"shot_id": shot_id, "type": "video", "x": 0, "y": 0, "data": {}},
    ).json()


def _ran(node_id, *, status="done", kind="gen_video"):
    with get_session() as s:
        s.add(Request(node_id=node_id, type=kind, status=status, params={}))
        s.commit()


def _set(client, ep, value):
    r = client.patch(f"/api/scenes/{ep['id']}", json={"production": {"status": value}})
    assert r.status_code == 200, r.text


# ── it fills itself in ──────────────────────────────────────────────────────


def test_an_empty_episode_has_not_started(client, ep):
    assert _status(client, ep) == "NotStarted"


def test_a_sequence_means_the_episode_is_broken_down(client, ep):
    """Sequences exist and nothing has run: the episode has been planned out but
    no footage has been asked for. That is the pre-production stage, not
    NotStarted — and not Production, which would claim work that has not
    happened."""
    _seq(client, ep)
    assert _status(client, ep) == "Script"


def test_a_generation_puts_the_episode_in_production(client, ep):
    """The unambiguous signal: a run happened. Somebody was in here and it cost
    money."""
    sq = _seq(client, ep)
    _ran(_node(client, sq["id"])["id"])
    assert _status(client, ep) == "Production"


def test_a_failed_run_alone_does_not_promote_it(client, ep):
    """The gateway erroring is not progress. Promoting on it would mark an episode
    as in production because one request bounced."""
    sq = _seq(client, ep)
    _ran(_node(client, sq["id"])["id"], status="error")
    assert _status(client, ep) == "Script"


def test_a_reference_image_counts_as_working_in_the_episode(client, ep):
    """Deliberately wider than the takes ceiling, which counts video only. That
    ceiling is rationing an expensive thing; this is answering "has anyone been in
    here", and building a reference is being in here."""
    sq = _seq(client, ep)
    _ran(_node(client, sq["id"])["id"], kind="gen_image")
    assert _status(client, ep) == "Production"


def test_it_goes_back_down_when_the_work_is_removed(client, ep):
    """Read from the work every time, so nothing has to be un-set. A stored status
    advanced on the event would sit at Production forever after the sequence it was
    describing was deleted."""
    sq = _seq(client, ep)
    _ran(_node(client, sq["id"])["id"])
    assert _status(client, ep) == "Production"
    assert client.delete(f"/api/shots/{sq['id']}").status_code in (200, 204)
    assert _status(client, ep) == "NotStarted"


# ── it never overrules a person ─────────────────────────────────────────────


def test_a_dropped_episode_stays_dropped(client, ep):
    """The one that matters most. Somebody decided to abandon an episode that has
    work in it; a stray generation must not walk it back into Production and back
    onto everybody's list."""
    sq = _seq(client, ep)
    _ran(_node(client, sq["id"])["id"])
    _set(client, ep, "Dropped")
    assert _status(client, ep) == "Dropped"


def test_completed_is_not_walked_back(client, ep):
    sq = _seq(client, ep)
    _ran(_node(client, sq["id"])["id"])
    _set(client, ep, "Completed")
    _ran(_node(client, sq["id"])["id"])
    assert _status(client, ep) == "Completed"


def test_a_pm_holding_it_at_script_is_respected(client, ep):
    """Not only the terminal values. A PM saying "this is still script stage"
    despite a test render is a judgement about the episode, and the derived value
    knows less than they do."""
    sq = _seq(client, ep)
    _ran(_node(client, sq["id"])["id"])
    _set(client, ep, "Script")
    assert _status(client, ep) == "Script"


def test_setting_it_back_to_notstarted_does_not_stick_as_a_decision(client, ep):
    """NotStarted is the absence of an answer, not an answer. Treating it as one
    would give anyone a way to freeze an episode's status by picking the default —
    and it is what every untouched row already holds, so it cannot be told apart
    from never having been set."""
    sq = _seq(client, ep)
    _ran(_node(client, sq["id"])["id"])
    _set(client, ep, "NotStarted")
    assert _status(client, ep) == "Production"


# ── what the UI is told ─────────────────────────────────────────────────────


def test_the_payload_says_whether_anyone_chose_it(client, ep):
    _seq(client, ep)
    assert client.get(f"/api/scenes/{ep['id']}").json()["status_auto"] is True
    _set(client, ep, "Completed")
    assert client.get(f"/api/scenes/{ep['id']}").json()["status_auto"] is False


def test_the_list_resolves_it_too(client, ep):
    """The episode table lists episodes; if only the single-scene read resolved
    the status, the table would be the one place still showing NotStarted."""
    sq = _seq(client, ep)
    _ran(_node(client, sq["id"])["id"])
    rows = client.get(f"/api/projects/{ep['project_id']}/scenes").json()
    assert rows[0]["production"]["status"] == "Production"


# ── the rollup agrees with the rows ─────────────────────────────────────────


def test_the_series_rollup_counts_the_same_statuses(client, ep):
    """Otherwise the overview reports 1 × NotStarted next to an episode row
    reading Production, and the rollup is the number people quote."""
    sq = _seq(client, ep)
    _ran(_node(client, sq["id"])["id"])
    with get_session() as s:
        stats = sers.series_stats(s, __import__("uuid").UUID(ep["series_id"]))
    assert stats["by_status"] == {"Production": 1}


def test_completion_percent_still_needs_a_human(client, ep):
    """Completed is never derived, so the completion figure cannot drift upwards on
    its own — somebody has to say an episode is done."""
    sq = _seq(client, ep)
    _ran(_node(client, sq["id"])["id"])
    with get_session() as s:
        assert sers.series_stats(s, __import__("uuid").UUID(ep["series_id"]))[
            "completion_pct"
        ] == 0


# ── batching ────────────────────────────────────────────────────────────────


def test_many_episodes_resolve_in_one_pass(client, ep):
    """The map is what the list route and the rollup call. Per-scene it would be
    three queries per row on the busiest page in the app."""
    pid, sid = ep["project_id"], ep["series_id"]
    made = [ep["id"]]
    for i in range(3):
        made.append(
            client.post(
                f"/api/projects/{pid}/scenes", json={"name": f"EP{i + 2}", "series_id": sid}
            ).json()["id"]
        )
    sq = _seq(client, {"id": made[1]})
    _ran(_node(client, sq["id"])["id"])
    _seq(client, {"id": made[2]})

    with get_session() as s:
        scenes = [s.get(Scene, __import__("uuid").UUID(i)) for i in made]
        out = ss.effective_status_map(s, scenes)
    assert [out[sc.id] for sc in scenes] == [
        "NotStarted",
        "Production",
        "Script",
        "NotStarted",
    ]
