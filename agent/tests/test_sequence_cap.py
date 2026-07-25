"""Phase 10 CRM: hard cap on sequences per episode = ceil(duration/sec_per_video)+2."""
from __future__ import annotations


def _setup(client, duration, sec_per_video):
    pid = client.post("/api/projects", json={"name": "MOGU"}).json()["id"]
    sid = client.post(
        f"/api/projects/{pid}/series",
        json={"name": "S", "production": {"episode_duration_sec": duration, "sec_per_video": sec_per_video}},
    ).json()["id"]
    ep = client.post(
        f"/api/projects/{pid}/scenes", json={"name": "EP1", "series_id": sid}
    ).json()["id"]
    return ep


def test_cap_blocks_beyond_limit(client):
    # 120s / 10s = 12 standard, +2 = 14 cap
    ep = _setup(client, 120, 10)
    for i in range(14):
        r = client.post(f"/api/scenes/{ep}/shots", json={})
        assert r.status_code == 200, f"seq {i+1} should be allowed"
    over = client.post(f"/api/scenes/{ep}/shots", json={})
    assert over.status_code == 409


def test_no_cap_without_duration(client):
    # series has no duration/sec_per_video → unlimited
    pid = client.post("/api/projects", json={"name": "MOGU"}).json()["id"]
    sid = client.post(f"/api/projects/{pid}/series", json={"name": "S"}).json()["id"]
    ep = client.post(f"/api/projects/{pid}/scenes", json={"name": "EP", "series_id": sid}).json()["id"]
    for _ in range(30):
        assert client.post(f"/api/scenes/{ep}/shots", json={}).status_code == 200
