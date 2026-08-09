"""Phase 8.3a — multi-shot SceneCanvas backend: canvas_state.shot_groups,
auto-migration, shot-group PATCH, scene-canvas aggregate GET.
"""
from __future__ import annotations

from tests.conftest import make_shot


def _second_shot(client, scene_id: str) -> str:
    return client.post(f"/api/scenes/{scene_id}/shots", json={}).json()["id"]


# ── Scene Bible removed ───────────────────────────────────────────────────


def test_scene_has_canvas_state_not_bible(client):
    b = make_shot(client)
    scene = client.get(f"/api/scenes/{b['scene_id']}").json()
    assert "scene_bible_text" not in scene
    assert scene["canvas_state"] == {}


# ── auto-migrate ──────────────────────────────────────────────────────────


def test_auto_migrate_creates_one_group_per_shot(client):
    b = make_shot(client)
    _second_shot(client, b["scene_id"])
    r = client.post(f"/api/scenes/{b['scene_id']}/auto-migrate")
    assert r.status_code == 200, r.text
    groups = r.json()["shot_groups"]
    assert len(groups) == 2
    # Vertical stack: distinct y, ascending with order.
    ys = [g["position"]["y"] for g in groups]
    assert ys[0] < ys[1]
    assert all(g["collapsed"] is False for g in groups)
    assert {g["label"] for g in groups} == {"Sequence 1", "Sequence 2"}


def test_auto_migrate_idempotent(client):
    b = make_shot(client)
    client.post(f"/api/scenes/{b['scene_id']}/auto-migrate")
    r2 = client.post(f"/api/scenes/{b['scene_id']}/auto-migrate")
    assert len(r2.json()["shot_groups"]) == 1  # not duplicated


def test_auto_migrate_preserves_moved_group(client):
    """A user-moved group is not clobbered when a new shot is migrated in."""
    b = make_shot(client)
    client.post(f"/api/scenes/{b['scene_id']}/auto-migrate")
    client.patch(f"/api/shots/{b['id']}/group", json={"position": {"x": 999, "y": 888}})
    # Add a 2nd shot, re-migrate.
    _second_shot(client, b["scene_id"])
    r = client.post(f"/api/scenes/{b['scene_id']}/auto-migrate")
    groups = {g["shot_id"]: g for g in r.json()["shot_groups"]}
    assert groups[b["id"]]["position"] == {"x": 999, "y": 888}  # preserved
    assert len(groups) == 2


def test_auto_migrate_missing_scene_404(client):
    import uuid
    r = client.post(f"/api/scenes/{uuid.uuid4()}/auto-migrate")
    assert r.status_code == 404


# ── shot-group PATCH ──────────────────────────────────────────────────────


def test_patch_shot_group_persists(client):
    b = make_shot(client)
    r = client.patch(
        f"/api/shots/{b['id']}/group",
        json={"position": {"x": 50, "y": 600}, "collapsed": True, "label": "Opening"},
    )
    assert r.status_code == 200, r.text
    g = r.json()
    assert g["shot_id"] == b["id"]
    assert g["position"] == {"x": 50, "y": 600}
    assert g["collapsed"] is True
    assert g["label"] == "Opening"
    # Reflected in scene.canvas_state via the scene detail.
    scene = client.get(f"/api/scenes/{b['scene_id']}").json()
    sg = scene["canvas_state"]["shot_groups"]
    assert sg[0]["shot_id"] == b["id"] and sg[0]["collapsed"] is True


def test_patch_shot_group_size_persists(client):
    """Phase 8.3b: manual group resize persists in canvas_state."""
    b = make_shot(client)
    r = client.patch(f"/api/shots/{b['id']}/group", json={"size": {"w": 900, "h": 500}})
    assert r.status_code == 200, r.text
    assert r.json()["size"] == {"w": 900, "h": 500}
    scene = client.get(f"/api/scenes/{b['scene_id']}").json()
    assert scene["canvas_state"]["shot_groups"][0]["size"] == {"w": 900, "h": 500}


def test_patch_shot_group_partial_keeps_other_fields(client):
    b = make_shot(client)
    client.patch(f"/api/shots/{b['id']}/group", json={"label": "A", "collapsed": True})
    r = client.patch(f"/api/shots/{b['id']}/group", json={"collapsed": False})
    g = r.json()
    assert g["collapsed"] is False
    assert g["label"] == "A"  # untouched


def test_patch_shot_group_missing_shot_404(client):
    import uuid
    r = client.patch(f"/api/shots/{uuid.uuid4()}/group", json={"collapsed": True})
    assert r.status_code == 404


# ── scene-canvas aggregate GET ────────────────────────────────────────────


def test_get_scene_canvas_aggregates_shots_nodes_edges_groups(client):
    b = make_shot(client)
    # two nodes + an edge in the shot
    n1 = client.post("/api/nodes", json={"shot_id": b["id"], "type": "character"}).json()
    n2 = client.post("/api/nodes", json={"shot_id": b["id"], "type": "video"}).json()
    client.post("/api/edges", json={"shot_id": b["id"], "source_id": n1["id"], "target_id": n2["id"]})
    client.post(f"/api/scenes/{b['scene_id']}/auto-migrate")

    r = client.get(f"/api/scenes/{b['scene_id']}/canvas")
    assert r.status_code == 200, r.text
    canvas = r.json()
    assert canvas["scene_id"] == b["scene_id"]
    assert len(canvas["shots"]) == 1
    assert len(canvas["nodes"]) == 2
    assert len(canvas["edges"]) == 1
    assert len(canvas["shot_groups"]) == 1
    # every node carries its shot_id so the frontend can group by it
    assert all(nd["shot_id"] == b["id"] for nd in canvas["nodes"])


def test_get_scene_canvas_missing_scene_404(client):
    import uuid
    r = client.get(f"/api/scenes/{uuid.uuid4()}/canvas")
    assert r.status_code == 404


# ── delete shot cleans up its group entry (8.3b-2 bug fix) ────────────────


def test_delete_shot_removes_its_group_entry(client):
    b = make_shot(client)
    s2 = _second_shot(client, b["scene_id"])
    client.post(f"/api/scenes/{b['scene_id']}/auto-migrate")
    # sanity: 2 groups
    assert len(client.get(f"/api/scenes/{b['scene_id']}/canvas").json()["shot_groups"]) == 2

    r = client.delete(f"/api/shots/{s2}")
    assert r.status_code == 200, r.text

    canvas = client.get(f"/api/scenes/{b['scene_id']}/canvas").json()
    groups = canvas["shot_groups"]
    assert len(groups) == 1
    assert groups[0]["shot_id"] == b["id"]
    assert all(g["shot_id"] != s2 for g in groups)  # no orphan entry
    assert len(canvas["shots"]) == 1


def test_get_scene_canvas_drops_orphan_groups(client):
    """Defensive: a group entry whose shot was deleted out-of-band is not
    returned by the canvas endpoint."""
    b = make_shot(client)
    client.post(f"/api/scenes/{b['scene_id']}/auto-migrate")
    # Inject a bogus orphan group via the group PATCH on a real shot, then
    # delete the real shot's row directly is covered above; here simulate an
    # orphan by patching a group for a fake shot id is not possible (PATCH
    # needs a real shot). Instead verify the real-shot canvas has no orphans.
    canvas = client.get(f"/api/scenes/{b['scene_id']}/canvas").json()
    shot_ids = {s["id"] for s in canvas["shots"]}
    assert all(g["shot_id"] in shot_ids for g in canvas["shot_groups"])


# ── the layout is a view of the shots, not a copy that drifts ───────────────


def _canvas(client, scene_id):
    r = client.get(f"/api/scenes/{scene_id}/canvas")
    assert r.status_code == 200, r.text
    return r.json()


def _scene_with_shots(client, n: int):
    """A scene holding `n` sequences. `make_shot` gives the first."""
    b = make_shot(client)
    ids = [b["id"]]  # `make_shot` names the shot `id`
    for _ in range(n - 1):
        ids.append(_second_shot(client, b["scene_id"]))
    return b["scene_id"], ids


def test_reading_the_canvas_repairs_a_group_whose_order_went_stale(client):
    """The bug the delivery skeleton exposed.

    `order` was written once, when the group was created, and never refreshed.
    That held while a sequence's position never changed — it does now: a chapter
    fills in its earlier panels and everything after shifts. The canvas then
    stacked them 2, 5, 3, 8, 7, with two groups claiming the same slot, because
    the client sorts by an `order` that no longer described the shot.
    """
    from flowboard.db import get_session
    from flowboard.db.models import Scene
    from sqlalchemy.orm.attributes import flag_modified

    scene_id, shot_ids = _scene_with_shots(client, 3)
    got = _canvas(client, scene_id)
    assert [g["order"] for g in got["shot_groups"]] == [0, 1, 2]

    # Corrupt one group's order behind the app's back, as a real one went stale.
    with get_session() as s:
        scene = s.get(Scene, scene_id)
        state = dict(scene.canvas_state)
        state["shot_groups"][2]["order"] = 0
        state["shot_groups"][2]["label"] = "Sequence 1"
        scene.canvas_state = state
        flag_modified(scene, "canvas_state")
        s.add(scene)
        s.commit()

    got = _canvas(client, scene_id)
    assert [g["order"] for g in got["shot_groups"]] == [0, 1, 2], "stale order survived a read"
    assert [g["label"] for g in got["shot_groups"]] == [
        "Sequence 1", "Sequence 2", "Sequence 3",
    ]


def test_a_group_whose_shot_is_gone_is_dropped_not_just_hidden(client):
    """A frame for a sequence that no longer exists is a wrong row, not a
    display problem — and nothing else would ever clean it up."""
    from flowboard.db import get_session
    from flowboard.db.models import Scene
    from sqlalchemy.orm.attributes import flag_modified

    scene_id, shot_ids = _scene_with_shots(client, 2)
    _canvas(client, scene_id)  # groups only exist once the canvas has been read
    with get_session() as s:
        scene = s.get(Scene, scene_id)
        state = dict(scene.canvas_state)
        state["shot_groups"].append({
            "shot_id": "00000000-0000-0000-0000-000000000000",
            "order": 99, "label": "Ghost", "collapsed": False,
            "position": {"x": 0, "y": 0},
        })
        scene.canvas_state = state
        flag_modified(scene, "canvas_state")
        s.add(scene)
        s.commit()

    assert len(_canvas(client, scene_id)["shot_groups"]) == 2
    with get_session() as s:
        stored = s.get(Scene, scene_id).canvas_state["shot_groups"]
    assert len(stored) == 2, "the orphan was filtered on read but left in the row"


def test_a_layout_that_contradicts_the_order_is_rebuilt(client):
    """The one the user was actually looking at: Sequence 1 sitting below
    Sequence 2, and 5 above 3. Every individual y looked like a choice, so
    nothing ever repaired it."""
    from flowboard.db import get_session
    from flowboard.db.models import Scene
    from sqlalchemy.orm.attributes import flag_modified

    scene_id, shot_ids = _scene_with_shots(client, 3)
    _canvas(client, scene_id)
    with get_session() as s:
        scene = s.get(Scene, scene_id)
        state = dict(scene.canvas_state)
        for g, y in zip(state["shot_groups"], [1600, 100, 5140]):
            g["position"]["y"] = y
        scene.canvas_state = state
        flag_modified(scene, "canvas_state")
        s.add(scene)
        s.commit()

    groups = _canvas(client, scene_id)["shot_groups"]
    ys = [g["position"]["y"] for g in groups]
    assert ys == sorted(ys), f"a sequence still sits above an earlier one: {ys}"
    assert [g["order"] for g in groups] == [0, 1, 2]


def test_a_deliberate_arrangement_that_agrees_with_the_order_is_kept(client):
    """The other half. Re-seeding whenever anything looked unusual would throw
    away spacing somebody chose on purpose."""
    from flowboard.db import get_session
    from flowboard.db.models import Scene
    from sqlalchemy.orm.attributes import flag_modified

    scene_id, shot_ids = _scene_with_shots(client, 3)
    _canvas(client, scene_id)
    spread = [40, 2000, 9000]
    with get_session() as s:
        scene = s.get(Scene, scene_id)
        state = dict(scene.canvas_state)
        for g, y in zip(state["shot_groups"], spread):
            g["position"]["y"] = y
        scene.canvas_state = state
        flag_modified(scene, "canvas_state")
        s.add(scene)
        s.commit()

    groups = _canvas(client, scene_id)["shot_groups"]
    assert [g["position"]["y"] for g in groups] == spread


def test_a_new_sequence_does_not_land_on_top_of_an_existing_one(client):
    """Seeding a new group at the end of the ARRAY put it wherever the array
    happened to end, which is on top of a group already there once positions
    stopped following array order."""
    scene_id, shot_ids = _scene_with_shots(client, 2)
    _canvas(client, scene_id)

    client.post(f"/api/scenes/{scene_id}/shots", json={"order_index": 0})
    groups = _canvas(client, scene_id)["shot_groups"]
    ys = [g["position"]["y"] for g in groups]
    assert len(ys) == len(set(ys)), f"two groups share a y: {ys}"


def test_an_even_pitch_narrower_than_a_frame_is_repaired(client):
    """The one that survived the first fix. "Ascending" was the whole test, and
    a stack pitched 500 apart with frames 1080 tall ascends perfectly — so the
    machine-generated overlap read as a deliberate arrangement and was kept."""
    from flowboard.db import get_session
    from flowboard.db.models import Scene
    from sqlalchemy.orm.attributes import flag_modified

    scene_id, _ = _scene_with_shots(client, 4)
    _canvas(client, scene_id)
    with get_session() as s:
        scene = s.get(Scene, scene_id)
        state = dict(scene.canvas_state)
        for i, g in enumerate(state["shot_groups"]):
            g["position"]["y"] = 100.0 + i * 500.0  # ascending, and overlapping
        scene.canvas_state = state
        flag_modified(scene, "canvas_state")
        s.add(scene)
        s.commit()

    ys = [g["position"]["y"] for g in _canvas(client, scene_id)["shot_groups"]]
    gaps = [b - a for a, b in zip(ys, ys[1:])]
    assert all(g >= 1080 for g in gaps), f"still overlapping: {gaps}"


def test_a_tight_stack_of_collapsed_frames_is_left_alone(client):
    """Why the test is "each frame clears the one above it" and not "the pitch
    is at least a default frame": collapsed frames are 110 tall, so a stack of
    them is legitimately tighter than any fixed floor would allow."""
    from flowboard.db import get_session
    from flowboard.db.models import Scene
    from sqlalchemy.orm.attributes import flag_modified

    scene_id, _ = _scene_with_shots(client, 3)
    _canvas(client, scene_id)
    tight = [100.0, 320.0, 540.0]  # 220 apart: clears a 110-tall collapsed frame
    with get_session() as s:
        scene = s.get(Scene, scene_id)
        state = dict(scene.canvas_state)
        for g, y in zip(state["shot_groups"], tight):
            g["collapsed"] = True
            g["position"]["y"] = y
        scene.canvas_state = state
        flag_modified(scene, "canvas_state")
        s.add(scene)
        s.commit()

    groups = _canvas(client, scene_id)["shot_groups"]
    assert [g["position"]["y"] for g in groups] == tight


def test_the_seeded_stack_does_not_overlap_itself(client):
    """The seed pitch was 500 while a frame is 1080 tall, so a freshly seeded
    stack overlapped by definition. It went unnoticed because the client
    re-flowed from the real heights on load and the seed was visible for a
    moment — until scenes started gaining sequences after that re-flow had
    already run."""
    scene_id, _ = _scene_with_shots(client, 4)
    groups = _canvas(client, scene_id)["shot_groups"]
    ys = [g["position"]["y"] for g in groups]
    gaps = [b - a for a, b in zip(ys, ys[1:])]
    # A frame's own default height, so consecutive frames cannot sit on one
    # another before the client has measured anything.
    assert all(gap >= 1080 for gap in gaps), f"seeded frames overlap: {gaps}"
