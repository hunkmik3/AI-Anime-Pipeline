"""Episode and sequence codes are built, not left blank.

The convention was already written down — `episode_code` produces
`<SERIES_CODE>_EP<NN>`, mirroring the Episode_Tracker sheet — and exactly one
caller used it: the bulk "scaffold N episodes" helper. An episode made the
ordinary way, which is how they are actually made, came out with `code = ""`,
and the tracker column it mirrors stayed empty.

Same shape as the comic names: a rule that exists in one place and is not
applied on the path people take is a rule the data does not have.
"""
from __future__ import annotations

from flowboard.db import get_session
from flowboard.services import project_service, scene_service, series_service, shot_service


def _world(series_code="S1"):
    with get_session() as s:
        p = project_service.create_project(s, name="MoguTV")
        sr = series_service.create_series(s, p.id, name="Season 1", code=series_code)
        return p.id, sr.id


def _episode(pid, sid, name="Episode"):
    with get_session() as s:
        return scene_service.create_scene(s, pid, name=name, series_id=sid).code


def _sequence(scene_id):
    with get_session() as s:
        return shot_service.create_shot(s, scene_id).code


def _scene_id(pid, sid):
    with get_session() as s:
        return scene_service.create_scene(s, pid, name="E", series_id=sid).id


# ── episodes ────────────────────────────────────────────────────────────────


def test_an_episode_made_the_ordinary_way_gets_a_code(client):
    """It used to get an empty string."""
    pid, sid = _world()
    assert _episode(pid, sid) == "S1_EP01"


def test_episodes_number_on_within_their_series(client):
    pid, sid = _world()
    assert [_episode(pid, sid) for _ in range(3)] == ["S1_EP01", "S1_EP02", "S1_EP03"]


def test_each_series_numbers_from_one(client):
    pid, sid = _world()
    _episode(pid, sid)
    with get_session() as s:
        other = series_service.create_series(s, pid, name="Season 2", code="S2")
        oid = other.id
    assert _episode(pid, oid) == "S2_EP01"


def test_a_series_with_no_code_still_produces_one(client):
    """A bare EP01 is worse than nothing only if nothing is an option. It is
    not — the tracker needs a value in that column."""
    pid, sid = _world(series_code="")
    assert _episode(pid, sid) == "EP01"


def test_a_deleted_episodes_code_IS_reissued(client):
    """And that is the right answer here, unlike one tier up.

    A comic's slate number (GCSA_26001) is an ISSUED identifier: it names that
    comic for good, appears in export folders and in conversation, and outlives
    the row — so it is taken from a high-water mark and never handed out twice.

    An episode number is POSITIONAL. A season runs EP01, EP02, EP03 in order,
    and an episode that was deleted is one that did not happen. Leaving a
    permanent hole at EP02 would make every later episode's number disagree with
    where it actually sits in the season, which is the thing the number is for.
    """
    pid, sid = _world()
    _episode(pid, sid)
    second = _scene_id(pid, sid)          # EP02
    with get_session() as s:
        scene_service.delete_scene(s, second)
    assert _episode(pid, sid) == "S1_EP02", "the gap should close, not persist"


def test_a_code_the_caller_gives_is_kept(client):
    pid, sid = _world()
    with get_session() as s:
        sc = scene_service.create_scene(s, pid, name="E", series_id=sid, code="PILOT")
        assert sc.code == "PILOT"


# ── sequences ───────────────────────────────────────────────────────────────


def test_a_sequence_carries_its_episode(client):
    """A bare SQ01 is unique only inside one episode, and these codes are read
    on their own — in an export folder, in a message, in the tracker."""
    pid, sid = _world()
    eid = _scene_id(pid, sid)
    assert _sequence(eid).endswith("_SQ01")
    with get_session() as s:
        ep_code = scene_service.get_scene(s, eid).code
    assert _sequence(eid) == f"{ep_code}_SQ02"


def test_sequences_number_within_their_episode(client):
    pid, sid = _world()
    a, b = _scene_id(pid, sid), _scene_id(pid, sid)
    assert [_sequence(a) for _ in range(2)] == ["S1_EP01_SQ01", "S1_EP01_SQ02"]
    assert _sequence(b) == "S1_EP02_SQ01"


def test_a_deleted_sequences_code_IS_reissued(client):
    """Positional for the same reason an episode is: sequences are the order
    the episode is cut in, and a hole in that order is not information."""
    pid, sid = _world()
    eid = _scene_id(pid, sid)
    _sequence(eid)
    with get_session() as s:
        sh = shot_service.create_shot(s, eid)      # SQ02
        shot_service.delete_shot(s, sh.id)
    assert _sequence(eid) == "S1_EP01_SQ02"
