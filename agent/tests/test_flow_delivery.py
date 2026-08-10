"""The handover: an approved panel becomes a sequence.

Two products with separate databases' worth of rows, joined by three nullable
columns. The failures worth hunting are all quiet ones:

  * delivering the same panel twice, which a reopen-and-re-approve does, and
    which shows up only as a sequence count nobody checks;
  * delivering into the wrong episode, which is a correct-looking row in the
    wrong place;
  * delivering when the comic was never linked, which would put a comic being
    adapted for print onto the animation board.

None of those raise. Each one needs a test that counts.
"""
from __future__ import annotations

import uuid

from flowboard.db import get_session
from flowboard.db.models import Scene, Shot
from flowboard.services import flow_delivery as fd
from flowboard.services import panel_service as ps
from flowboard.services import project_service, scene_service, series_service
from flowboard.services import user_service
from sqlmodel import select


def _login(client, username, password="pw123456"):
    r = client.post("/api/account/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _world(client, *, chapters=2, panels=2):
    """A comic with two chapters, and a production project to deliver into.

    Two chapters because one is enough to pass a test that ignores the chapter
    entirely and puts everything in the first episode it finds.
    """
    user_service.create_user("dl_admin", "pw123456", role="admin")
    out: dict = {"h": _login(client, "dl_admin"), "chapters": [], "panels": {}}
    with get_session() as s:
        proj = project_service.create_project(s, name="Anime side")
        studio = series_service.create_series(s, proj.id, name="MAGMEL")
        out["project_id"], out["studio_series_id"] = proj.id, studio.id

        slate = ps.create_project(s, "Comics")
        comic = ps.create_series(s, slate.id, "MAGMEL")
        out["comic_id"] = comic.id
        for c in range(chapters):
            ch = ps.create_chapter(s, comic.id, f"Chapter {c + 1}")
            out["chapters"].append(ch.id)
            if panels == 0:
                # `import_panels` refuses an empty folder, and rightly — but a
                # caller asking for a bare chapter is not importing anything.
                out["panels"][ch.id] = []
                continue
            batch = ps.create_batch(s, ch.id, f"b{c}")
            made = ps.import_panels(
                s, batch.id,
                entries=[(f"C{c}P{i}.png", f"raw-{c}-{i}") for i in range(panels)],
            )
            out["panels"][ch.id] = [p.id for p in made]
    return out


def _link(client, w):
    r = client.put(
        f"/api/flowstudio/series/{w['comic_id']}/delivery",
        json={"studio_series_id": str(w["studio_series_id"])},
        headers=w["h"],
    )
    assert r.status_code == 200, r.text
    return r.json()


def _approve(client, w, panel_id):
    """Through the ROUTE, because that is where the handover is hooked."""
    with get_session() as s:
        ps.add_generated(s, panel_id, [f"gen-{panel_id}"])
        ps.submit_panel(s, panel_id)
    r = client.post(
        f"/api/flowstudio/panels/{panel_id}/review", json={"approve": True}, headers=w["h"]
    )
    assert r.status_code == 200, r.text
    return r.json()


def _shots(scene_id):
    with get_session() as s:
        return s.exec(select(Shot).where(Shot.scene_id == scene_id)).all()


# ── the link is the only decision ───────────────────────────────────────────


def test_an_unlinked_comic_delivers_nothing(client):
    """Most comics never hand over. Approving in one must not quietly create
    production rows for a comic being adapted for print."""
    w = _world(client)
    got = _approve(client, w, w["panels"][w["chapters"][0]][0])
    assert "delivered" not in got

    with get_session() as s:
        assert s.exec(select(Scene).where(Scene.series_id == w["studio_series_id"])).all() == []


def test_approving_a_linked_comic_creates_the_sequence(client):
    w = _world(client)
    _link(client, w)
    got = _approve(client, w, w["panels"][w["chapters"][0]][0])

    assert got["delivered"]["created"] is True
    pid = w["panels"][w["chapters"][0]][0]
    with get_session() as s:
        shot = s.get(Shot, got["delivered"]["sequence_id"])
        assert shot is not None
        assert shot.code == "C0P0"
        # The sequence says what it is a picture OF, not just that it exists.
        assert shot.production["source_panel"]["media_id"] == f"gen-{pid}"


def test_the_panel_arrives_on_the_canvas_not_just_in_a_column(client):
    """The failure the first cut shipped: the media id was recorded on the shot
    and NOTHING read it, so the animator opened an empty box and had to go find
    the panel they had just been handed. A record nobody reads is not a handover.
    """
    from flowboard.db.models import Node

    w = _world(client)
    _link(client, w)
    pid = w["panels"][w["chapters"][0]][0]
    got = _approve(client, w, pid)["delivered"]

    with get_session() as s:
        nodes = s.exec(
            select(Node).where(Node.shot_id == got["sequence_id"])
        ).all()
    assert len(nodes) == 1, "the sequence opened empty"
    node = nodes[0]
    # The SAME node the library's click-to-spawn makes — anything else would be
    # a second kind of image node for every consumer to learn.
    assert node.type == "visual_asset"
    assert node.data["mediaId"] == f"gen-{pid}"
    assert node.data["status"] == "done"
    assert node.data["title"] == "C0P0"
    assert node.data["sourcePanelId"] == pid
    assert node.short_id, "a node with no short id cannot be wired to anything"


def test_delivering_twice_does_not_leave_two_copies_on_the_canvas(client):
    """The idempotency check has to cover the node too — a second approval that
    re-ran only this part would stack panels on top of each other."""
    from flowboard.db.models import Node

    w = _world(client)
    _link(client, w)
    pid = w["panels"][w["chapters"][0]][0]
    got = _approve(client, w, pid)["delivered"]

    client.post(f"/api/flowstudio/panels/{pid}/reopen", headers=w["h"])
    with get_session() as s:
        ps.submit_panel(s, pid)
    client.post(
        f"/api/flowstudio/panels/{pid}/review", json={"approve": True}, headers=w["h"]
    )

    with get_session() as s:
        nodes = s.exec(select(Node).where(Node.shot_id == got["sequence_id"])).all()
    assert len(nodes) == 1


# ── the quiet failure: delivering twice ─────────────────────────────────────


def test_reopening_and_approving_again_does_not_make_a_second_sequence(client):
    """The one that shows up only as a count. A PM who mis-clicks Approve,
    reopens and approves again must leave one sequence behind, not two."""
    w = _world(client)
    _link(client, w)
    pid = w["panels"][w["chapters"][0]][0]
    first = _approve(client, w, pid)["delivered"]

    assert client.post(
        f"/api/flowstudio/panels/{pid}/reopen", headers=w["h"]
    ).status_code == 200
    with get_session() as s:
        ps.submit_panel(s, pid)
    second = client.post(
        f"/api/flowstudio/panels/{pid}/review", json={"approve": True}, headers=w["h"]
    ).json()["delivered"]

    assert second["sequence_id"] == first["sequence_id"]
    assert second["created"] is False
    # Two panels in this chapter, so two slots — and exactly ONE of them is
    # this panel's. Counting shots alone would not catch a duplicate now that
    # the empty slots are real rows too.
    with get_session() as s:
        mine = [
            sh for sh in _shots(second["episode_id"])
            if (sh.production or {}).get("source_panel", {}).get("panel_id") == pid
        ]
    assert len(mine) == 1


def test_a_second_panel_joins_the_same_episode(client):
    """One sequence each, both in their chapter's episode — not one episode per
    panel, which is what a naive "make the episode" would do."""
    w = _world(client)
    _link(client, w)
    ch = w["chapters"][0]
    a = _approve(client, w, w["panels"][ch][0])["delivered"]
    b = _approve(client, w, w["panels"][ch][1])["delivered"]

    assert a["episode_id"] == b["episode_id"]
    assert len(_shots(a["episode_id"])) == 2


# ── the quiet failure: the wrong episode ────────────────────────────────────


def test_each_chapter_becomes_its_own_episode(client):
    w = _world(client)
    _link(client, w)
    first = _approve(client, w, w["panels"][w["chapters"][0]][0])["delivered"]
    second = _approve(client, w, w["panels"][w["chapters"][1]][0])["delivered"]

    assert first["episode_id"] != second["episode_id"], "two chapters landed in one episode"
    with get_session() as s:
        names = {
            s.get(Scene, first["episode_id"]).name,
            s.get(Scene, second["episode_id"]).name,
        }
    assert names == {"Chapter 1", "Chapter 2"}


def test_the_episode_lands_under_the_linked_series(client):
    """It would be entirely possible to create the episode in the right project
    and the wrong series — the project is what `create_scene` takes."""
    w = _world(client)
    _link(client, w)
    got = _approve(client, w, w["panels"][w["chapters"][0]][0])["delivered"]
    with get_session() as s:
        scene = s.get(Scene, got["episode_id"])
        assert scene.series_id == w["studio_series_id"]
        assert scene.project_id == w["project_id"]


# ── catching up work approved before the link ───────────────────────────────


def test_sync_carries_over_panels_approved_before_the_comic_was_linked(client):
    """Without this, linking a half-finished comic would only ever carry its
    FUTURE approvals, and the work already done would have to be re-approved."""
    w = _world(client)
    ch = w["chapters"][0]
    _approve(client, w, w["panels"][ch][0])
    _approve(client, w, w["panels"][ch][1])
    _link(client, w)

    r = client.post(f"/api/flowstudio/series/{w['comic_id']}/delivery/sync", headers=w["h"])
    assert r.status_code == 200, r.text
    # `created` is what THIS run made; `delivered` is the running total. They
    # were the same key once, and the total quietly overwrote the count.
    assert r.json()["created"] == 2
    assert r.json()["delivered"] == 2
    assert r.json()["approved"] == 2

    # And running it again makes nothing — sync is not a duplicator.
    again = client.post(
        f"/api/flowstudio/series/{w['comic_id']}/delivery/sync", headers=w["h"]
    ).json()
    assert again["created"] == 0
    assert again["delivered"] == 2, "the total should not move"


def test_only_approved_panels_cross_over(client):
    """A submitted panel is a request for a verdict, not a verdict. Delivering
    then would put work on the production board that is about to be rejected."""
    w = _world(client)
    _link(client, w)
    ch = w["chapters"][0]
    pid = w["panels"][ch][0]
    with get_session() as s:
        ps.add_generated(s, pid, ["g"])
        ps.submit_panel(s, pid)

    r = client.post(f"/api/flowstudio/series/{w['comic_id']}/delivery/sync", headers=w["h"])
    assert r.json()["created"] == 0

    # Sent back, still nothing.
    client.post(
        f"/api/flowstudio/panels/{pid}/review",
        json={"approve": False, "notes": ["fix it"]},
        headers=w["h"],
    )
    assert client.post(
        f"/api/flowstudio/series/{w['comic_id']}/delivery/sync", headers=w["h"]
    ).json()["created"] == 0


# ── unlinking ───────────────────────────────────────────────────────────────


def test_unlinking_leaves_delivered_work_alone(client):
    """Sequences that exist are work someone may have started. Withdrawing them
    because a routing decision changed would destroy it."""
    w = _world(client)
    _link(client, w)
    got = _approve(client, w, w["panels"][w["chapters"][0]][0])["delivered"]

    r = client.put(
        f"/api/flowstudio/series/{w['comic_id']}/delivery",
        json={"studio_series_id": None},
        headers=w["h"],
    )
    assert r.status_code == 200
    assert r.json()["linked"] is False
    with get_session() as s:
        assert s.get(Shot, got["sequence_id"]) is not None


def test_linking_to_a_series_that_does_not_exist_is_refused(client):
    import uuid as _uuid

    w = _world(client)
    r = client.put(
        f"/api/flowstudio/series/{w['comic_id']}/delivery",
        json={"studio_series_id": str(_uuid.uuid4())},
        headers=w["h"],
    )
    assert r.status_code == 404


# ── reporting ───────────────────────────────────────────────────────────────


def test_the_delivery_state_counts_what_has_crossed(client):
    w = _world(client)
    _link(client, w)
    ch = w["chapters"][0]
    _approve(client, w, w["panels"][ch][0])

    got = client.get(f"/api/flowstudio/series/{w['comic_id']}/delivery", headers=w["h"]).json()
    assert got["linked"] is True
    assert got["studio_series_name"] == "MAGMEL"
    assert got["approved"] == 1
    assert got["delivered"] == 1
    assert got["episodes"] == 1


# ── the survivable failures ─────────────────────────────────────────────────


def test_deleting_the_sequence_downstream_lets_the_panel_deliver_again(client):
    """The pointer goes stale rather than the panel becoming undeliverable. It
    is still approved, so it still owes a sequence."""
    w = _world(client)
    _link(client, w)
    pid = w["panels"][w["chapters"][0]][0]
    first = _approve(client, w, pid)["delivered"]

    with get_session() as s:
        from flowboard.services import shot_service
        shot_service.delete_shot(s, first["sequence_id"])

    with get_session() as s:
        again = fd.deliver_panel(s, pid)
    assert again is not None and again.created is True
    assert str(again.shot_id) != first["sequence_id"]


def test_a_production_series_holding_delivered_work_cannot_be_deleted(client):
    """The dangerous case is closed at the far end rather than handled here.

    Once a panel has crossed, the production series holds an episode, and a
    series holding episodes refuses to be deleted at all. So "linked to a series
    that was deleted underneath us" cannot arise for any comic that has actually
    delivered something.
    """
    w = _world(client)
    _link(client, w)
    _approve(client, w, w["panels"][w["chapters"][0]][0])

    with get_session() as s:
        try:
            series_service.delete_series(s, w["studio_series_id"])
        except series_service.SeriesNotEmpty as exc:
            assert "episode" in str(exc)
        else:
            raise AssertionError("a series holding delivered work was deleted")


def test_deleting_an_EMPTY_production_series_just_unlinks_the_comic(client):
    """The harmless remainder. Nothing has crossed, so there is nothing to lose;
    the foreign key nulls the link and the comic stops delivering. Silent, but
    visible: `delivery_state` says `linked: false`, which is the screen a PM
    looks at to ask this exact question."""
    w = _world(client)
    _link(client, w)
    with get_session() as s:
        series_service.delete_series(s, w["studio_series_id"])

    state = client.get(
        f"/api/flowstudio/series/{w['comic_id']}/delivery", headers=w["h"]
    ).json()
    assert state["linked"] is False

    # And approving still works — a routing gap is not a reason to reject work.
    got = _approve(client, w, w["panels"][w["chapters"][0]][0])
    assert got["status"] == "approved"
    assert "delivered" not in got


# ── position ────────────────────────────────────────────────────────────────


def test_a_panel_keeps_its_position_however_late_it_is_approved(client):
    """The failure this is here to stop: panels are approved in whatever order
    the PM gets to them, so arrival order would make the LAST panel Sequence 1
    whenever it happened to be reviewed first — and every number would then
    shuffle as the rest caught up."""
    w = _world(client, chapters=1, panels=4)
    _link(client, w)
    ch = w["chapters"][0]

    # Approve backwards: the 4th panel first.
    last = _approve(client, w, w["panels"][ch][3])["delivered"]
    first = _approve(client, w, w["panels"][ch][0])["delivered"]

    with get_session() as s:
        assert s.get(Shot, last["sequence_id"]).order_index == 3, (
            "the last panel took the first slot"
        )
        assert s.get(Shot, first["sequence_id"]).order_index == 0


def test_every_panel_has_a_slot_and_the_unapproved_ones_are_empty(client):
    """The whole chapter's shape is standing there from the first delivery.

    Before this, only approved panels had a sequence, so the episode read
    "Sequence 2, Sequence 5, Sequence 8" with nothing between them — the numbers
    were right and the shape was unreadable. An episode IS the chapter, and the
    chapter's length is known the moment the panels are imported, so those gaps
    are not unknowns. They are work that has not arrived.
    """
    from flowboard.db.models import Node

    w = _world(client, chapters=1, panels=4)
    _link(client, w)
    ch = w["chapters"][0]
    _approve(client, w, w["panels"][ch][0])
    got = _approve(client, w, w["panels"][ch][3])["delivered"]

    with get_session() as s:
        shots = sorted(
            s.exec(select(Shot).where(Shot.scene_id == got["episode_id"])).all(),
            key=lambda x: x.order_index,
        )
        # Four panels, four slots, no holes.
        assert [x.order_index for x in shots] == [0, 1, 2, 3]
        # Each slot names the panel it is for, so an empty one is not a blank box.
        assert [x.code for x in shots] == ["C0P0", "C0P1", "C0P2", "C0P3"]

        filled, empty = [], []
        for sh in shots:
            nodes = s.exec(select(Node).where(Node.shot_id == sh.id)).all()
            (filled if nodes else empty).append(sh.order_index)
    assert filled == [0, 3], "the wrong slots carry artwork"
    assert empty == [1, 2], "a slot with no approved panel is not empty"


def test_a_panel_approved_later_fills_its_waiting_slot(client):
    """It must land in the row already standing there, not append a second one
    beside it."""
    w = _world(client, chapters=1, panels=4)
    _link(client, w)
    ch = w["chapters"][0]
    got = _approve(client, w, w["panels"][ch][0])["delivered"]

    with get_session() as s:
        before = len(s.exec(select(Shot).where(Shot.scene_id == got["episode_id"])).all())
    later = _approve(client, w, w["panels"][ch][2])["delivered"]
    with get_session() as s:
        after = s.exec(select(Shot).where(Shot.scene_id == got["episode_id"])).all()

    assert len(after) == before == 4, "delivering added a row instead of filling one"
    with get_session() as s:
        assert s.get(Shot, later["sequence_id"]).order_index == 2


def test_position_is_measured_in_the_CHAPTER_not_the_batch(client):
    """A batch is one artist's share of a chapter, so a chapter split three ways
    holds three panels all numbered 0. Using `panel.order_index` directly would
    stack them all in slot 1."""
    t = _world(client, chapters=1, panels=0)
    _link(client, t)
    ch = t["chapters"][0]
    with get_session() as s:
        made = []
        for k in range(2):
            b = ps.create_batch(s, ch, f"artist-{k}")
            made += ps.import_panels(
                s, b.id, entries=[(f"B{k}P{i}.png", f"raw-{k}-{i}") for i in range(3)]
            )
        ids = [p.id for p in made]
        # Both batches number their panels 0,1,2 — the collision this guards.
        assert [p.order_index for p in made] == [0, 1, 2, 0, 1, 2]

    seen = []
    for pid in ids:
        seen.append(_approve(client, t, pid)["delivered"]["sequence_id"])

    with get_session() as s:
        got = sorted(s.get(Shot, sid).order_index for sid in seen)
    assert got == [0, 1, 2, 3, 4, 5], f"batch positions collided: {got}"


def test_the_skeleton_repairs_itself_for_an_episode_that_predates_it(client):
    """Building the slots only at episode-creation left two holes: an episode
    made before this feature existed never got them, and a chapter that gains a
    batch afterwards never got the new ones. Rebuilding on every delivery is
    idempotent and needs no backfill anybody has to remember."""
    w = _world(client, chapters=1, panels=2)
    _link(client, w)
    ch = w["chapters"][0]
    got = _approve(client, w, w["panels"][ch][0])["delivered"]

    # A second batch arrives in the same chapter, after the episode exists.
    with get_session() as s:
        b = ps.create_batch(s, ch, "late-arrival")
        extra = ps.import_panels(
            s, b.id, entries=[(f"LATE{i}.png", f"raw-late-{i}") for i in range(2)]
        )
        extra_ids = [p.id for p in extra]

    _approve(client, w, extra_ids[0])
    with get_session() as s:
        shots = s.exec(select(Shot).where(Shot.scene_id == got["episode_id"])).all()
    assert len(shots) == 4, "the late batch got no slots"
    assert sorted(x.code for x in shots) == ["C0P0", "C0P1", "LATE0", "LATE1"]


def test_a_fully_delivered_comic_still_gains_its_empty_slots(client):
    """The bug the demo caught: `deliver_panel` short-circuited on
    already-delivered BEFORE resolving the episode, so re-running sync on a
    comic whose approved panels had all crossed returned "already done" without
    ever looking at the episode — and the repair never ran."""
    w = _world(client, chapters=1, panels=4)
    _link(client, w)
    ch = w["chapters"][0]
    got = _approve(client, w, w["panels"][ch][1])["delivered"]

    # Strip the episode back to only the delivered slot, as an episode created
    # before the skeleton existed would look.
    with get_session() as s:
        for sh in s.exec(select(Shot).where(Shot.scene_id == got["episode_id"])).all():
            if sh.id != uuid.UUID(got["sequence_id"]):
                s.delete(sh)
        s.commit()
        assert len(s.exec(select(Shot).where(Shot.scene_id == got["episode_id"])).all()) == 1

    r = client.post(f"/api/flowstudio/series/{w['comic_id']}/delivery/sync", headers=w["h"])
    assert r.status_code == 200
    assert r.json()["created"] == 0, "it re-delivered instead of repairing"

    with get_session() as s:
        shots = s.exec(select(Shot).where(Shot.scene_id == got["episode_id"])).all()
    assert len(shots) == 4, "the empty slots were never rebuilt"


def test_a_delivered_sequence_keeps_the_panel_code_not_a_positional_one(client):
    """The code is how a panel finds its slot, so it is not free to be prettier.

    Giant Studio names its own sequences positionally — S1_EP01_SQ01 — and
    applying that here looks like tidying up. It is not: `_slot_for` matches a
    panel to the sequence waiting for it BY CODE, so restamping them means the
    next approval finds nothing and appends a duplicate instead of filling the
    slot. (I did exactly this by hand to a demo database and broke all 34.)

    Position is not lost by keeping the panel code — `order_index` carries it,
    and the canvas label is built from that.
    """
    from flowboard.db.models import FlowPanel

    w = _world(client, chapters=1, panels=3)
    _link(client, w)
    ch = w["chapters"][0]
    got = _approve(client, w, w["panels"][ch][0])["delivered"]

    with get_session() as s:
        codes = {
            sh.code
            for sh in s.exec(select(Shot).where(Shot.scene_id == got["episode_id"])).all()
        }
        panels = {
            s.get(FlowPanel, pid).code for pid in w["panels"][ch]
        }
    assert codes == panels, "a sequence stopped naming the panel it is for"

    # And the matching still works: approving another fills its slot rather
    # than appending beside it.
    before = len(_shots(got["episode_id"]))
    _approve(client, w, w["panels"][ch][2])
    assert len(_shots(got["episode_id"])) == before


# ── every comic gets its counterpart ────────────────────────────────────────


def test_creating_a_comic_creates_and_links_its_production_series(client):
    """Chosen over asking a PM to wire each one up: the two sides carry the same
    shape from the moment a comic exists."""
    slate = client.post("/api/flowstudio/projects", json={"name": "Slate"}).json()
    comic = client.post(
        "/api/flowstudio/series", json={"project_id": slate["id"], "name": "Sea Devils"}
    ).json()
    state = client.get(f"/api/flowstudio/series/{comic['id']}/delivery").json()
    assert state["linked"] is True
    assert state["studio_series_name"] == "SEA-DEVILS"


def test_a_second_comic_on_the_slate_reuses_the_same_project(client):
    """One slate is one production project. Creating a second would split a
    studio's comics across two boards that mean the same thing."""
    slate = client.post("/api/flowstudio/projects", json={"name": "Slate"}).json()
    before = len(client.get("/api/projects").json())
    for name in ("One", "Two"):
        client.post(
            "/api/flowstudio/series", json={"project_id": slate["id"], "name": name}
        )
    after = client.get("/api/projects").json()
    assert len(after) == before + 1, "a second project was created for the same slate"


def test_the_link_survives_a_repair_run(client):
    """`ensure_counterpart` is idempotent. Re-running it must not strand delivered
    work under a second series nobody is looking at."""
    from flowboard.db import get_session
    from flowboard.db.models import FlowSeries
    from flowboard.services import flow_delivery as fd

    slate = client.post("/api/flowstudio/projects", json={"name": "Slate"}).json()
    comic = client.post(
        "/api/flowstudio/series", json={"project_id": slate["id"], "name": "Once"}
    ).json()
    with get_session() as s:
        row = s.get(FlowSeries, comic["id"])
        first = fd.ensure_counterpart(s, row)
        again = fd.ensure_counterpart(s, row)
    assert first.id == again.id


def test_a_vietnamese_title_keeps_its_letters(client):
    """The pattern behind these names keeps ASCII only, and applied straight to
    Vietnamese it ATE the letters rather than transliterating them: "Đường Về Nhà"
    came out "NG-V-NH" — unreadable, and inherited by the production side as the
    name of a series."""
    slate = client.post("/api/flowstudio/projects", json={"name": "Slate"}).json()
    comic = client.post(
        "/api/flowstudio/series",
        json={"project_id": slate["id"], "name": "Đường Về Nhà"},
    ).json()
    assert comic["name"].endswith("DUONG-VE-NHA")
    state = client.get(f"/api/flowstudio/series/{comic['id']}/delivery").json()
    assert state["studio_series_name"] == "DUONG-VE-NHA"
