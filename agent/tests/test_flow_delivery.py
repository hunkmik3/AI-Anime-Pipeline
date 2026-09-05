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


def test_approving_a_linked_comic_creates_the_episode(client):
    """The bridge stops at chapter→episode. Approving the first panel of a
    chapter makes that chapter's episode and NOTHING below it — the animator
    builds the sequences by hand from the exported panels."""
    w = _world(client)
    _link(client, w)
    got = _approve(client, w, w["panels"][w["chapters"][0]][0])["delivered"]

    assert got["created"] is True
    with get_session() as s:
        assert s.get(Scene, got["episode_id"]) is not None
    # The half that was removed. Without this line the test passes just as
    # happily if panel→sequence delivery comes back by accident.
    assert _shots(got["episode_id"]) == [], "the bridge created a sequence"


def test_reopening_and_approving_again_does_not_make_a_second_episode(client):
    """The one that shows up only as a count. A PM who mis-clicks Approve,
    reopens and approves again must leave one episode behind, not two."""
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

    assert second["episode_id"] == first["episode_id"]
    assert second["created"] is False
    with get_session() as s:
        episodes = s.exec(
            select(Scene).where(Scene.series_id == w["studio_series_id"])
        ).all()
    assert len(episodes) == 1


def test_a_second_panel_joins_the_same_episode(client):
    """Both panels land in their chapter's episode — not one episode per panel,
    which is what a naive "make the episode" would do."""
    w = _world(client)
    _link(client, w)
    ch = w["chapters"][0]
    a = _approve(client, w, w["panels"][ch][0])["delivered"]
    b = _approve(client, w, w["panels"][ch][1])["delivered"]

    assert a["episode_id"] == b["episode_id"]
    assert b["created"] is False
    assert _shots(a["episode_id"]) == []


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
    # `created` is what THIS run made — one episode, for the one chapter those
    # two panels share. `delivered` is the running total of panels that have
    # crossed. They were the same key once, and the total quietly overwrote the
    # count.
    assert r.json()["created"] == 1
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
    """An episode that exists is work someone may have started. Withdrawing it
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
        assert s.get(Scene, got["episode_id"]) is not None


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


# ── the PM crosses with the comic ───────────────────────────────────────────
#
# The counterpart is built by the app, so nobody was standing on it: an
# auto-created project had no owner and an auto-created series no producer. The
# handover worked and was invisible — only an admin could open any of it. These
# pin the half that makes it reachable, and the two edges that make it safe:
# never demoting somebody the production side already trusted, and never leaving
# a review pointed at somebody who is off the comic.


def _pm(client, w, username="pm_one", role="producer"):
    """Give somebody the comic, through the route the PM actually uses."""
    u = user_service.create_user(username, "pw123456", role="user")
    r = client.put(
        f"/api/flowstudio/series/{w['comic_id']}/members",
        json={"user_id": str(u.id), "role": role},
        headers=w["h"],
    )
    assert r.status_code == 200, r.text
    return u


def _studio_role(project_id, user_id):
    """What this account may do on the production project — None if it is not on
    it at all, which is the failure this whole section is about."""
    from flowboard.db.models import ProjectMember

    with get_session() as s:
        row = s.exec(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user_id,
            )
        ).first()
        return row.role if row else None


def _producer_of(series_id):
    from flowboard.db.models import Series

    with get_session() as s:
        return s.get(Series, series_id).producer_user_id


def test_naming_a_pm_on_the_comic_puts_them_on_the_production_series(client):
    w = _world(client, chapters=1, panels=1)
    _link(client, w)
    pm = _pm(client, w)
    assert _producer_of(w["studio_series_id"]) == pm.id


def test_the_pm_can_open_the_production_project_they_deliver_into(client):
    """The bug this fixes: the comic handed over correctly into a project its own
    PM could not see, so the episode and its sequences existed for nobody."""
    w = _world(client, chapters=1, panels=1)
    _link(client, w)
    pm = _pm(client, w)
    ids = [p["id"] for p in client.get("/api/projects", headers=_login(client, "pm_one")).json()]
    assert str(w["project_id"]) in ids


def test_the_pm_arrives_able_to_staff_the_project(client):
    """Option A: only the PM crosses, so the PM has to be able to hand the
    episode to whoever generates it. A row that cannot do that is decoration."""
    from flowboard.services import permissions as sp

    w = _world(client, chapters=1, panels=1)
    _link(client, w)
    pm = _pm(client, w)
    assert sp.role_allows(_studio_role(w["project_id"], pm.id), "member.manage")


def test_an_artist_on_the_comic_does_not_cross(client):
    """Who animates a panel is a different job from who drew it. Carrying the
    panel artist over would hand them an episode nobody gave them."""
    w = _world(client, chapters=1, panels=1)
    _link(client, w)
    drawer = _pm(client, w, username="drawer", role="artist")
    assert _studio_role(w["project_id"], drawer.id) is None
    assert _producer_of(w["studio_series_id"]) != drawer.id


def test_a_pm_named_before_the_link_still_crosses_when_it_is_made(client):
    """The comic is usually staffed first and pointed at a series afterwards, so
    the link has to carry whoever is already on it — not just future edits."""
    w = _world(client, chapters=1, panels=1)
    pm = _pm(client, w)
    assert _studio_role(w["project_id"], pm.id) is None, "nothing to cross yet"
    _link(client, w)
    assert _producer_of(w["studio_series_id"]) == pm.id


def test_the_first_pm_named_is_the_series_producer(client):
    """Two PMs on a comic, one producer field. Ordered by when they were named
    rather than by name, so a rename does not silently reroute reviews."""
    w = _world(client, chapters=1, panels=1)
    _link(client, w)
    first = _pm(client, w, username="pm_first")
    _pm(client, w, username="pm_second")
    assert _producer_of(w["studio_series_id"]) == first.id


def test_both_pms_can_open_the_project_even_though_one_holds_the_series(client):
    w = _world(client, chapters=1, panels=1)
    _link(client, w)
    _pm(client, w, username="pm_first")
    second = _pm(client, w, username="pm_second")
    assert _studio_role(w["project_id"], second.id) is not None


def test_a_comic_edit_never_demotes_somebody_the_studio_trusts(client):
    """The mirror grants; it does not rank people. Somebody already trusted with
    more on the production side keeps it."""
    from flowboard.db.models import ProjectMember
    from flowboard.services import permissions as sp

    w = _world(client, chapters=1, panels=1)
    _link(client, w)
    u = user_service.create_user("already_here", "pw123456", role="user")
    with get_session() as s:
        s.add(ProjectMember(project_id=w["project_id"], user_id=u.id, role=sp.PRODUCER))
        s.commit()
    client.put(
        f"/api/flowstudio/series/{w['comic_id']}/members",
        json={"user_id": str(u.id), "role": "producer"},
        headers=w["h"],
    )
    assert _studio_role(w["project_id"], u.id) == sp.PRODUCER


def test_an_editor_on_the_project_is_raised_not_replaced(client):
    """An editor row cannot staff a project, so a PM landing on top of one has to
    be raised — and must not lose the ability to pull material and hand a cut
    back, which producer rank already carries."""
    from flowboard.db.models import ProjectMember
    from flowboard.services import permissions as sp

    w = _world(client, chapters=1, panels=1)
    _link(client, w)
    u = user_service.create_user("cutter_pm", "pw123456", role="user")
    with get_session() as s:
        s.add(ProjectMember(project_id=w["project_id"], user_id=u.id, role=sp.EDITOR))
        s.commit()
    client.put(
        f"/api/flowstudio/series/{w['comic_id']}/members",
        json={"user_id": str(u.id), "role": "producer"},
        headers=w["h"],
    )
    role = _studio_role(w["project_id"], u.id)
    assert sp.role_allows(role, "member.manage")
    assert sp.role_allows(role, "cut.submit")


def test_dropping_the_pm_releases_the_series_producer(client):
    """That field routes a review. Left pointing at somebody off the comic, every
    future cut goes to them and nobody else can answer it."""
    w = _world(client, chapters=1, panels=1)
    _link(client, w)
    pm = _pm(client, w)
    r = client.delete(
        f"/api/flowstudio/series/{w['comic_id']}/members/{pm.id}", headers=w["h"]
    )
    assert r.status_code == 200, r.text
    assert _producer_of(w["studio_series_id"]) is None


def test_dropping_one_of_two_pms_hands_the_series_to_the_other(client):
    w = _world(client, chapters=1, panels=1)
    _link(client, w)
    first = _pm(client, w, username="pm_first")
    second = _pm(client, w, username="pm_second")
    client.delete(
        f"/api/flowstudio/series/{w['comic_id']}/members/{first.id}", headers=w["h"]
    )
    assert _producer_of(w["studio_series_id"]) == second.id


def test_dropping_a_pm_leaves_their_project_row_alone(client):
    """Deliberate: a membership row is also how somebody keeps reach into work
    they have already done. Removing it is a decision for the production project,
    not a side effect of a comic-side edit."""
    w = _world(client, chapters=1, panels=1)
    _link(client, w)
    pm = _pm(client, w)
    client.delete(
        f"/api/flowstudio/series/{w['comic_id']}/members/{pm.id}", headers=w["h"]
    )
    assert _studio_role(w["project_id"], pm.id) is not None


def test_an_unlinked_comic_carries_nobody_across(client):
    """A comic being adapted for print has no production side to appear on."""
    w = _world(client, chapters=1, panels=1)
    pm = _pm(client, w)
    assert _studio_role(w["project_id"], pm.id) is None


def test_syncing_people_twice_makes_one_row(client):
    """Runs on every membership edit and on every link, so the common case has to
    be idempotent rather than additive."""
    from flowboard.db.models import FlowSeries, ProjectMember

    w = _world(client, chapters=1, panels=1)
    _link(client, w)
    pm = _pm(client, w)
    with get_session() as s:
        fd.sync_people(s, s.get(FlowSeries, w["comic_id"]))
        rows = s.exec(
            select(ProjectMember).where(
                ProjectMember.project_id == w["project_id"],
                ProjectMember.user_id == pm.id,
            )
        ).all()
    assert len(rows) == 1
