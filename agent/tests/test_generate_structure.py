"""Phase 10 CRM: series 'generate structure' bulk-creates Episodes + Sequences
(idempotent, non-destructive)."""
from __future__ import annotations


def _mk_series(client, name="Show", code="SH"):
    pid = client.post("/api/projects", json={"name": "MOGU"}).json()["id"]
    sid = client.post(
        f"/api/projects/{pid}/series", json={"name": name, "code": code}
    ).json()["id"]
    return pid, sid


def test_generate_creates_episodes_and_sequences(client):
    _pid, sid = _mk_series(client)
    r = client.post(
        f"/api/series/{sid}/generate-structure",
        json={"episodes": 4, "sequences_per_episode": 3},
    ).json()
    assert r == {
        "episodes_created": 4,
        "sequences_created": 12,
        "total_episodes": 4,
        "sequences_per_episode": 3,
    }
    eps = client.get(f"/api/series/{sid}/episodes").json()
    # Episode ids follow the sheet convention: <SERIES_CODE>_EP<NN>
    assert [e["code"] for e in eps] == ["SH_EP01", "SH_EP02", "SH_EP03", "SH_EP04"]
    shots = client.get(f"/api/scenes/{eps[0]['id']}/shots").json()
    assert [s["code"] for s in shots] == ["SQ01", "SQ02", "SQ03"]


def test_generate_is_idempotent(client):
    _pid, sid = _mk_series(client)
    client.post(
        f"/api/series/{sid}/generate-structure",
        json={"episodes": 3, "sequences_per_episode": 2},
    )
    again = client.post(
        f"/api/series/{sid}/generate-structure",
        json={"episodes": 3, "sequences_per_episode": 2},
    ).json()
    assert again["episodes_created"] == 0 and again["sequences_created"] == 0


def test_generate_tops_up_missing_episodes_only(client):
    _pid, sid = _mk_series(client)
    client.post(
        f"/api/series/{sid}/generate-structure",
        json={"episodes": 2, "sequences_per_episode": 1},
    )
    grow = client.post(
        f"/api/series/{sid}/generate-structure",
        json={"episodes": 5, "sequences_per_episode": 1},
    ).json()
    # only the 3 new episodes (+ their sequences) are added
    assert grow["episodes_created"] == 3 and grow["sequences_created"] == 3
    assert grow["total_episodes"] == 5


def test_generate_keeps_existing_sequences(client):
    """An episode that already has sequences is left untouched."""
    _pid, sid = _mk_series(client)
    ep = client.post(
        f"/api/series/{sid}/scenes" if False else f"/api/projects/{_pid}/scenes",
        json={"name": "EP1", "series_id": sid},
    ).json()["id"]
    client.post(f"/api/scenes/{ep}/shots", json={"code": "MANUAL"})
    r = client.post(
        f"/api/series/{sid}/generate-structure",
        json={"episodes": 1, "sequences_per_episode": 5},
    ).json()
    # episode already exists with a shot → no episodes and no sequences added
    assert r["episodes_created"] == 0 and r["sequences_created"] == 0
    shots = client.get(f"/api/scenes/{ep}/shots").json()
    assert [s["code"] for s in shots] == ["MANUAL"]


def test_generate_respects_caps(client):
    _pid, sid = _mk_series(client)
    too_many = client.post(
        f"/api/series/{sid}/generate-structure",
        json={"episodes": 99999, "sequences_per_episode": 1},
    )
    assert too_many.status_code == 422
