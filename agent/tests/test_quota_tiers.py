"""Credit ceilings at all four tiers — Project, Series, Episode, Sequence.

Diagram 5 asks "does this **Sequence** have quota left?" and diagram 1 has the PM
set a quota when creating an Episode. Budgets used to stop at Series, so an
artist could burn a whole series' credit on one episode without being stopped.
"""
from __future__ import annotations

import pytest

from flowboard.db import get_session
from flowboard.db.models import Node
from flowboard.services import scope_budget as sb
from flowboard.services import user_service


def _h(client, u, p="pw123456"):
    tok = client.post("/api/account/login", json={"username": u, "password": p}).json()
    return {"Authorization": f"Bearer {tok['token']}"}


@pytest.fixture()
def world(client):
    """A project → series → episode → sequence → node chain to meter against."""
    user_service.create_user("boss", "pw123456", role="admin")
    ah = _h(client, "boss")
    pid = client.post("/api/projects", json={"name": "MOGU"}, headers=ah).json()["id"]
    sid = client.post(
        f"/api/projects/{pid}/series", json={"name": "S1", "code": "S1"}, headers=ah
    ).json()["id"]
    ep = client.post(
        f"/api/projects/{pid}/scenes", json={"name": "EP01", "series_id": sid}, headers=ah
    ).json()["id"]
    seq = client.post(f"/api/scenes/{ep}/shots", json={"code": "SQ01"}, headers=ah).json()["id"]
    with get_session() as s:
        node = Node(shot_id=seq, short_id="n1", type="video")
        s.add(node)
        s.commit()
        s.refresh(node)
        node_id = node.id
    return {"ah": ah, "project": pid, "series": sid, "episode": ep, "sequence": seq,
            "node": node_id}


# ── the tiers exist ────────────────────────────────────────────────────────


def test_all_four_tiers_accept_a_budget(client, world):
    for scope, key in (
        ("project", "project"),
        ("series", "series"),
        ("scene", "episode"),
        ("shot", "sequence"),
    ):
        r = client.put(
            f"/api/budgets/{scope}/{world[key]}", json={"amount_usd": 25}, headers=world["ah"]
        )
        assert r.status_code == 200, (scope, r.text)
        assert r.json()["base_usd"] == 25
        assert r.json()["unlimited"] is False


def test_unknown_scope_is_rejected_with_a_helpful_message(client, world):
    r = client.put(
        f"/api/budgets/wombat/{world['project']}", json={"amount_usd": 5}, headers=world["ah"]
    )
    assert r.status_code == 400
    assert "sequence" in r.json()["detail"].lower()


def test_scope_words_match_how_the_studio_talks():
    """Alerts and errors go to people: they say Episode, not "scene"."""
    assert sb.scope_noun("scene") == "episode"
    assert sb.scope_noun("shot") == "sequence"
    assert sb.scope_noun("series") == "series"


# ── the gate ───────────────────────────────────────────────────────────────


def test_no_ceiling_anywhere_means_generation_is_free(client, world):
    with get_session() as s:
        assert sb.check(s, world["node"], 100.0) is None


def test_a_sequence_ceiling_blocks_on_its_own(client, world):
    """The tier diagram 5 actually asks about."""
    client.put(
        f"/api/budgets/shot/{world['sequence']}", json={"amount_usd": 2}, headers=world["ah"]
    )
    with get_session() as s:
        blocked = sb.check(s, world["node"], 5.0)
    assert blocked and blocked["scope"] == "shot"
    assert blocked["needed_usd"] == 5.0


def test_an_episode_ceiling_blocks_on_its_own(client, world):
    """The tier diagram 1 has the PM set at creation time."""
    client.put(
        f"/api/budgets/scene/{world['episode']}", json={"amount_usd": 1}, headers=world["ah"]
    )
    with get_session() as s:
        blocked = sb.check(s, world["node"], 3.0)
    assert blocked and blocked["scope"] == "scene"


def test_the_innermost_breached_tier_is_the_one_reported(client, world):
    """An artist must be told "this sequence is out of quota" — naming the
    project instead is both less true and not actionable."""
    for scope, key, amount in (
        ("project", "project", 1000),
        ("series", "series", 500),
        ("scene", "episode", 100),
        ("shot", "sequence", 3),
    ):
        client.put(
            f"/api/budgets/{scope}/{world[key]}",
            json={"amount_usd": amount},
            headers=world["ah"],
        )
    with get_session() as s:
        blocked = sb.check(s, world["node"], 10.0)
    assert blocked["scope"] == "shot"  # tightest, not the outermost


def test_an_outer_tier_still_binds_when_inner_ones_are_open(client, world):
    """Setting only the Episode quota leaves its sequences individually
    uncapped but collectively bounded — no invented division rule."""
    client.put(
        f"/api/budgets/scene/{world['episode']}", json={"amount_usd": 4}, headers=world["ah"]
    )
    with get_session() as s:
        assert sb.check(s, world["node"], 2.0) is None       # inside the episode budget
        assert sb.check(s, world["node"], 9.0)["scope"] == "scene"  # over it


def test_a_generous_inner_tier_does_not_override_a_tight_outer_one(client, world):
    """A big sequence quota must not buy past an exhausted project."""
    client.put(
        f"/api/budgets/shot/{world['sequence']}", json={"amount_usd": 900}, headers=world["ah"]
    )
    client.put(
        f"/api/budgets/project/{world['project']}", json={"amount_usd": 5}, headers=world["ah"]
    )
    with get_session() as s:
        blocked = sb.check(s, world["node"], 50.0)
    assert blocked and blocked["scope"] == "project"


# ── spend attribution ──────────────────────────────────────────────────────


def test_a_sequence_budget_only_counts_its_own_spend(client, world):
    """Two sequences in one episode must not consume each other's quota."""
    other = client.post(
        f"/api/scenes/{world['episode']}/shots", json={"code": "SQ02"}, headers=world["ah"]
    ).json()["id"]
    with get_session() as s:
        ids = [str(i) for i in sb._shot_ids_for_scope(s, "shot", world["sequence"])]
    assert ids == [world["sequence"]]
    assert other not in ids


def test_an_episode_budget_covers_every_sequence_under_it(client, world):
    client.post(
        f"/api/scenes/{world['episode']}/shots", json={"code": "SQ02"}, headers=world["ah"]
    )
    with get_session() as s:
        ids = sb._shot_ids_for_scope(s, "scene", world["episode"])
    assert len(ids) == 2


# ── grants reach the new tiers ─────────────────────────────────────────────


def test_more_credit_can_be_requested_for_a_sequence(client, world):
    """The grant loop from diagram 5 has to work at the tier that blocked."""
    client.put(
        f"/api/budgets/shot/{world['sequence']}", json={"amount_usd": 2}, headers=world["ah"]
    )
    g = client.post(
        f"/api/budgets/shot/{world['sequence']}/grants",
        json={"amount_usd": 8, "reason": "retakes after director notes"},
        headers=world["ah"],
    )
    assert g.status_code == 200
    grant = g.json()["grant"]
    assert grant["status"] == "pending"  # asking is not getting

    # Pending changes nothing…
    with get_session() as s:
        assert sb.check(s, world["node"], 5.0)["scope"] == "shot"

    client.post(
        f"/api/budgets/requests/{grant['id']}/approve", json={"note": "ok"}, headers=world["ah"]
    )
    # …approval is what raises the ceiling.
    with get_session() as s:
        assert sb.check(s, world["node"], 5.0) is None


def test_episode_and_sequence_budgets_appear_in_history(client, world):
    client.put(
        f"/api/budgets/scene/{world['episode']}", json={"amount_usd": 40}, headers=world["ah"]
    )
    entries = client.get(
        f"/api/history/scene/{world['episode']}", headers=world["ah"]
    ).json()["entries"]
    assert [e for e in entries if e["action"] == "budget.set"]
