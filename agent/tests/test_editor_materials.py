"""The raw material an editor pulls out of a series.

The name each file gets is the point of this module, not a detail: these leave the
app and sit in somebody's NLE bin for a week. `SQ03_v2.mp4` in a folder of forty
is unusable and a bare media id is worse — and the name is also the vocabulary the
editor uses coming back to say which sequence is wrong, so it has to be the same
words the app uses.
"""
from __future__ import annotations

import uuid

import pytest

from flowboard.db import get_session
from flowboard.db.models import Node, Request, Scene, Shot
from flowboard.services import editor_service as es
from flowboard.services import user_service
from flowboard.short_id import generate_unique_short_id


def _h(client, u, p="pw123456"):
    r = client.post("/api/account/login", json={"username": u, "password": p})
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture()
def series(client):
    """One series, two episodes, sequences with takes on them."""
    user_service.create_user("em_admin", "pw123456", role="admin")
    ah = _h(client, "em_admin")
    pid = client.post("/api/projects", json={"name": "EM"}, headers=ah).json()["id"]
    sid = client.post(
        f"/api/projects/{pid}/series", json={"name": "S", "code": "DUON"}, headers=ah
    ).json()["id"]
    eps = [
        client.post(
            f"/api/projects/{pid}/scenes", json={"name": f"EP{i}", "series_id": sid},
            headers=ah,
        ).json()["id"]
        for i in (1, 2)
    ]
    return {"admin": ah, "pid": pid, "sid": sid, "eps": eps}


def _take(scene_id, *, n=1, media=None, status="done"):
    """One sequence carrying `n` generations, the way the worker records them."""
    with get_session() as s:
        sc = s.get(Scene, uuid.UUID(scene_id))
        shot = Shot(scene_id=sc.id, order_index=0, code=f"{sc.code}_SQ01")
        s.add(shot); s.commit(); s.refresh(shot)
        node = Node(shot_id=shot.id, short_id=generate_unique_short_id(s, shot.id),
                    type="video", x=0, y=0, data={})
        s.add(node); s.commit(); s.refresh(node)
        for i in range(n):
            s.add(Request(node_id=node.id, type="gen_video", status=status, params={},
                          result={"media_ids": [(media or f"m{i+1}")]}))
        s.commit()
        return shot.id


# ── what comes out ──────────────────────────────────────────────────────────


def test_the_filename_is_the_vocabulary_people_already_use(series, client):
    _take(series["eps"][0], n=1)
    with get_session() as s:
        out = es.materials(s, uuid.UUID(series["sid"]))
    name = out["episodes"][0]["sequences"][0]["clips"][0]["filename"]
    assert name.endswith("_v1.mp4")
    assert "SQ01" in name and "EP01" in name


def test_the_episode_stem_is_not_repeated(series, client):
    """Sequence codes already carry the episode (`DUON_EP01_SQ01`), so prefixing
    the episode again gives `DUON_EP01_DUON_EP01_SQ01`."""
    _take(series["eps"][0], n=1)
    with get_session() as s:
        out = es.materials(s, uuid.UUID(series["sid"]))
    assert out["episodes"][0]["sequences"][0]["clips"][0]["filename"].count("EP01") == 1


def test_only_the_latest_take_by_default(series, client):
    """Four of five attempts are things the artist rejected. Handing all five over
    makes the editor do the artist's triage."""
    _take(series["eps"][0], n=3)
    with get_session() as s:
        out = es.materials(s, uuid.UUID(series["sid"]))
        every = es.materials(s, uuid.UUID(series["sid"]), all_takes=True)
    seq = out["episodes"][0]["sequences"][0]
    assert len(seq["clips"]) == 1 and seq["clips"][0]["take"] == 3
    assert seq["take_count"] == 3, "the count still says how many there were"
    assert len(every["episodes"][0]["sequences"][0]["clips"]) == 3


def test_a_failed_generation_is_not_material(series, client):
    _take(series["eps"][0], n=2, status="error")
    with get_session() as s:
        out = es.materials(s, uuid.UUID(series["sid"]))
    assert out["clip_count"] == 0


def test_order_is_play_order(series, client):
    """An editor pulling forty clips is about to lay them on a timeline in exactly
    this order; sorted by anything else they do the sorting twice."""
    _take(series["eps"][0], n=1)
    _take(series["eps"][1], n=1)
    with get_session() as s:
        out = es.materials(s, uuid.UUID(series["sid"]))
    assert [e["code"] for e in out["episodes"]] == ["DUON_EP01", "DUON_EP02"]


def test_a_sequence_with_nothing_generated_is_left_out(series, client):
    """The material list is files, not structure — an empty sequence contributes
    no file and would only pad the list the editor is counting."""
    _take(series["eps"][0], n=1)
    with get_session() as s:
        sc = s.get(Scene, uuid.UUID(series["eps"][0]))
        s.add(Shot(scene_id=sc.id, order_index=9, code=f"{sc.code}_SQ09")); s.commit()
        out = es.materials(s, uuid.UUID(series["sid"]))
    assert out["episodes"][0]["sequence_count"] == 1


# ── who may pull it ─────────────────────────────────────────────────────────


def test_an_editor_may_pull_and_a_viewer_may_not(series, client):
    """The raw material is the whole of somebody's unfinished work. Being able to
    READ a project is not the same as being handed all of it as files."""
    ed = user_service.create_user("em_editor", "pw123456")
    vw = user_service.create_user("em_viewer", "pw123456")
    client.put(
        f"/api/projects/{series['pid']}/members",
        json={"members": [{"user_id": str(ed.id), "role": "editor"},
                          {"user_id": str(vw.id), "role": "viewer"}]},
        headers=series["admin"],
    )
    _take(series["eps"][0], n=1)
    url = f"/api/series/{series['sid']}/materials"
    assert client.get(url, headers=_h(client, "em_editor")).status_code == 200
    assert client.get(url, headers=_h(client, "em_viewer")).status_code == 403


def test_someone_off_the_project_gets_a_404_not_a_403(series, client):
    """403 admits the series exists."""
    user_service.create_user("em_out", "pw123456")
    r = client.get(f"/api/series/{series['sid']}/materials",
                   headers=_h(client, "em_out"))
    assert r.status_code == 404
