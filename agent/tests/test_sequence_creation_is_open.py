"""Adding a sequence is never refused. The ceiling is on takes, not containers.

This file used to assert the opposite: an episode was capped at
``ceil(duration / sec_per_video) + 2`` sequences and the 15th got a 409. The cap
was in the wrong place. A sequence is an empty container — deciding that one beat
needs two angles is the artist's job, and it costs nothing until something is
generated into it. The ceiling that costs money is five VIDEO takes per sequence
(`sequence_quota`), which is where a PM's attention belongs.

Refusing the container while the expensive thing inside it stays allowed does not
save anything. It teaches people to cram two shots into one sequence to get under
the count, which is worse than the thing the cap was for: now the takes ceiling
covers two shots' worth of work, and nobody can tell from the list what happened.

The tests stay because the cap is easy to re-add — the producer types a duration
and a seconds-per-video on the series form, so the number is sitting right there
looking like a rule.
"""
from __future__ import annotations


def _series(client, production=None):
    pid = client.post("/api/projects", json={"name": "MOGU"}).json()["id"]
    body = {"name": "S"}
    if production is not None:
        body["production"] = production
    sid = client.post(f"/api/projects/{pid}/series", json=body).json()["id"]
    ep = client.post(
        f"/api/projects/{pid}/scenes", json={"name": "EP1", "series_id": sid}
    ).json()["id"]
    return ep


def test_a_planned_duration_does_not_cap_sequences(client):
    """120s ÷ 10s = 12 videos planned. The 13th, 14th and 20th sequence are all
    still allowed: the duration says how long the episode should run, which is
    planning, not something the API refuses."""
    ep = _series(client, {"episode_duration_sec": 120, "sec_per_video": 10})
    for i in range(20):
        r = client.post(f"/api/scenes/{ep}/shots", json={})
        assert r.status_code == 200, f"sequence {i + 1} was refused: {r.text}"


def test_no_plan_at_all_is_the_same(client):
    """Unplanned series behaved this way before and must not start differing —
    otherwise filling in a duration would quietly change what is allowed."""
    ep = _series(client)
    for _ in range(30):
        assert client.post(f"/api/scenes/{ep}/shots", json={}).status_code == 200


def test_the_ceiling_that_does_exist_is_on_takes(client):
    """The point of removing the other one: there is still a limit, and it is the
    one that spends money. Five video takes on a sequence, then a PM looks."""
    from flowboard.services import sequence_quota as sq

    assert sq.DEFAULT_LIMIT == 5
