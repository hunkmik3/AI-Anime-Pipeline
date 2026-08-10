"""Notifications: does each role get told the right things, and only those?

The feed is derived, not stored, which moves the risk. There is no "did the
fan-out fire" question; the questions are whether a lookup crossed a scope
boundary and whether a to-do outlived the work it was about. Both of those are
silent — a leak looks like a helpful notification, and a stale job looks like a
job. These tests go after exactly that.
"""
from __future__ import annotations

from datetime import date, timedelta

from flowboard.db import get_session
from flowboard.services import flow_notices as fn
from flowboard.services import panel_service as ps
from flowboard.services import user_service


def _login(client, username, password="pw123456"):
    r = client.post("/api/account/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _studio(client):
    """A PM, two artists with a batch each, and panels in both.

    Two artists on purpose: with one, every scope bug reads as a pass because
    there is nothing on the other side of the boundary to leak.
    """
    user_service.create_user("nx_admin", "pw123456", role="admin")
    user_service.create_user("nx_pm", "pw123456", role="user")
    user_service.create_user("nx_a", "pw123456", role="user")
    user_service.create_user("nx_b", "pw123456", role="user")
    out: dict = {}
    with get_session() as s:
        pm = user_service.get_by_username("nx_pm")
        a = user_service.get_by_username("nx_a")
        b = user_service.get_by_username("nx_b")
        project = ps.create_project(s, "Slate")
        series = ps.create_series(s, project.id, "Comic")
        ps.set_member(s, series.id, pm.id, "producer")
        chapter = ps.create_chapter(s, series.id, "Ch 1")
        out.update(series_id=series.id, chapter_id=chapter.id,
                   pm_id=pm.id, a_id=a.id, b_id=b.id)
        for who, key in ((a, "a"), (b, "b")):
            batch = ps.create_batch(s, chapter.id, f"batch-{key}")
            ps.update_batch(s, batch.id, assignee_user_id=who.id, set_assignee=True)
            panels = ps.import_panels(
                s, batch.id, entries=[(f"{key.upper()}{i}.png", f"raw-{key}-{i}") for i in range(3)]
            )
            out[f"batch_{key}"] = batch.id
            out[f"panels_{key}"] = [p.id for p in panels]
    return out


def _send_back(panel_id, *, by, artist=None, reason="fix the border"):
    """`artist` matters: an unattributed submit shows up as news to everyone,
    including the person who did it."""
    with get_session() as s:
        ps.add_generated(s, panel_id, [f"gen-{panel_id}"])
        ps.submit_panel(s, panel_id, actor_user_id=artist)
        ps.review_panel(s, panel_id, approve=False, notes=[reason], author_user_id=by)


def _approve(panel_id, *, by, artist=None):
    with get_session() as s:
        ps.add_generated(s, panel_id, [f"gen-{panel_id}"])
        ps.submit_panel(s, panel_id, actor_user_id=artist)
        ps.review_panel(s, panel_id, approve=True, author_user_id=by)


def _kinds(rows):
    return [r["kind"] for r in rows]


# ── scope: the thing that fails silently ────────────────────────────────────


def test_an_artist_is_never_told_about_another_artists_panel(client):
    """The leak this page could plausibly ship with. Both artists have work sent
    back at the same moment; each must hear about exactly their own."""
    t = _studio(client)
    _send_back(t["panels_a"][0], by=t["pm_id"], artist=t["a_id"], reason="A's problem")
    _send_back(t["panels_b"][0], by=t["pm_id"], artist=t["b_id"], reason="B's problem")

    got = client.get("/api/flowstudio/notices", headers=_login(client, "nx_a")).json()
    text = " ".join(str(r) for r in got["todo"] + got["feed"])
    assert "A0" in text
    assert "B0" not in text, "an artist was told about another artist's panel"
    assert "B's problem" not in text


def test_the_pm_is_told_about_both_artists(client):
    t = _studio(client)
    _send_back(t["panels_a"][0], by=t["pm_id"], artist=t["a_id"])
    _send_back(t["panels_b"][0], by=t["pm_id"], artist=t["b_id"])

    got = client.get("/api/flowstudio/notices", headers=_login(client, "nx_pm")).json()
    text = " ".join(str(r) for r in got["feed"])
    assert "A0" in text and "B0" in text


def test_an_outsider_gets_the_spectator_feed_and_no_jobs(client):
    """A signed-in account with no membership and no batch is a VIEWER, and
    giantflow's standing rule is that a viewer sees the whole slate read-only —
    `/giantflow/panels` already shows them every panel. So the feed is not
    withheld here either; withholding it would make this one page disagree with
    the rest of the app about what a viewer is.

    What a viewer must never have is a job. They cannot generate, submit or rule
    on anything, so a to-do list for them could only ever be someone else's work
    presented as theirs.
    """
    t = _studio(client)
    _send_back(t["panels_a"][0], by=t["pm_id"], artist=t["a_id"])
    # A spectator, deliberately: signing in is no longer a standing on the comic
    # side, so the outsider has to be given one to BE an outsider here rather than
    # somebody from the other product.
    out = user_service.create_user("nx_out", "pw123456", role="user")
    with get_session() as s:
        ps.set_member(s, t["series_id"], out.id, "viewer")

    got = client.get("/api/flowstudio/notices", headers=_login(client, "nx_out")).json()
    assert got["role"] == "viewer"
    assert got["todo"] == [], "a viewer was handed work they cannot do"
    assert got["feed"], "a viewer lost the read-only feed the rest of the app gives them"


# ── to-dos track the work, not the reading ──────────────────────────────────


def test_a_sent_back_panel_leaves_the_list_when_it_is_resubmitted(client):
    """The whole reason to derive rather than store: the job disappears because
    the work moved, with nothing marked read anywhere."""
    t = _studio(client)
    pid = t["panels_a"][0]
    _send_back(pid, by=t["pm_id"])
    h = _login(client, "nx_a")

    before = client.get("/api/flowstudio/notices", headers=h).json()["todo"]
    assert any(r["kind"] == "sent_back" and r["panel_id"] == pid for r in before)

    with get_session() as s:
        ps.add_generated(s, pid, ["gen-again"])
        ps.submit_panel(s, pid)

    after = client.get("/api/flowstudio/notices", headers=h).json()["todo"]
    assert not any(r["kind"] == "sent_back" and r["panel_id"] == pid for r in after)


def test_marking_read_never_clears_a_job(client):
    """"Mark all read" is about the feed. If it silenced the to-do list it would
    hide the work rather than clear it."""
    t = _studio(client)
    _send_back(t["panels_a"][0], by=t["pm_id"], artist=t["a_id"])
    h = _login(client, "nx_a")

    assert client.post("/api/flowstudio/notices/read", headers=h).status_code == 200
    got = client.get("/api/flowstudio/notices", headers=h).json()
    assert got["unread"] == 0
    assert any(r["kind"] == "sent_back" for r in got["todo"])


def test_the_pms_queue_is_a_job_and_the_artists_send_back_is_too(client):
    t = _studio(client)
    with get_session() as s:
        pid = t["panels_a"][0]
        ps.add_generated(s, pid, ["g"])
        ps.submit_panel(s, pid)

    pm = client.get("/api/flowstudio/notices", headers=_login(client, "nx_pm")).json()
    assert "to_review" in _kinds(pm["todo"])
    # The artist who handed it in is not asked to review their own work.
    a = client.get("/api/flowstudio/notices", headers=_login(client, "nx_a")).json()
    assert "to_review" not in _kinds(a["todo"])


# ── unread ──────────────────────────────────────────────────────────────────


def test_your_own_actions_are_shown_but_never_counted(client):
    """A history with your part cut out reads as if it never happened; a badge
    that counts your own clicks is noise. Both, at once.

    The panel passes through two hands — the artist hands it in, the PM rules on
    it — so the PM's feed holds one of each. Exactly one of them is news.
    """
    t = _studio(client)
    _approve(t["panels_a"][0], by=t["pm_id"], artist=t["a_id"])

    got = client.get("/api/flowstudio/notices", headers=_login(client, "nx_pm")).json()
    kinds = {r["kind"]: r for r in got["feed"]}
    assert kinds["approved"]["mine"] is True, "the PM's own verdict vanished"
    assert kinds["submitted"]["mine"] is False

    # One unread, and it is the artist's hand-in — not the PM's own approval.
    assert got["unread"] == 1
    client.post("/api/flowstudio/notices/read", headers=_login(client, "nx_pm"))

    # Now the PM rules on a second panel THEY submitted nothing on: their own
    # action is the only new event, so the badge must stay at zero.
    with get_session() as s:
        pid = t["panels_a"][1]
        ps.add_generated(s, pid, ["g"])
        ps.submit_panel(s, pid, actor_user_id=t["pm_id"])
        ps.review_panel(s, pid, approve=True, author_user_id=t["pm_id"])
    after = client.get("/api/flowstudio/notices", headers=_login(client, "nx_pm")).json()
    assert after["unread"] == 0, "the PM was badged for something they did themselves"


def test_reading_clears_the_count_until_something_else_happens(client):
    t = _studio(client)
    _send_back(t["panels_a"][0], by=t["pm_id"], artist=t["a_id"])
    h = _login(client, "nx_a")

    assert client.get("/api/flowstudio/notices", headers=h).json()["unread"] >= 1
    client.post("/api/flowstudio/notices/read", headers=h)
    assert client.get("/api/flowstudio/notices", headers=h).json()["unread"] == 0

    _send_back(t["panels_a"][1], by=t["pm_id"], artist=t["a_id"])
    assert client.get("/api/flowstudio/notices", headers=h).json()["unread"] == 1


def test_the_badge_endpoint_agrees_with_the_page(client):
    """Two endpoints answering the same question is two chances to disagree."""
    t = _studio(client)
    _send_back(t["panels_a"][0], by=t["pm_id"], artist=t["a_id"])
    h = _login(client, "nx_a")

    page = client.get("/api/flowstudio/notices", headers=h).json()
    badge = client.get("/api/flowstudio/notices/count", headers=h).json()
    assert badge["unread"] == page["unread"]
    assert badge["todo"] == len(page["todo"])


# ── deadlines ───────────────────────────────────────────────────────────────


def test_an_overdue_chapter_reaches_the_people_working_in_it(client):
    t = _studio(client)
    with get_session() as s:
        ps.update_chapter(
            s, t["chapter_id"], due_date=date.today() - timedelta(days=2), set_due=True
        )

    for who in ("nx_pm", "nx_a"):
        got = client.get("/api/flowstudio/notices", headers=_login(client, who)).json()
        assert "overdue" in _kinds(got["todo"]), who


def test_a_deadline_still_weeks_out_is_not_a_notification(client):
    t = _studio(client)
    with get_session() as s:
        ps.update_chapter(
            s, t["chapter_id"], due_date=date.today() + timedelta(days=30), set_due=True
        )
    got = client.get("/api/flowstudio/notices", headers=_login(client, "nx_pm")).json()
    assert "overdue" not in _kinds(got["todo"])
    assert "due_soon" not in _kinds(got["todo"])


# ── links ───────────────────────────────────────────────────────────────────


def test_every_notification_links_somewhere_the_reader_may_go(client):
    """A row that 403s on click is worse than no row. Panel links are checked
    against the reader's own scope, not merely against existence."""
    t = _studio(client)
    _send_back(t["panels_a"][0], by=t["pm_id"], artist=t["a_id"])
    _approve(t["panels_a"][1], by=t["pm_id"], artist=t["a_id"])
    h = _login(client, "nx_a")

    got = client.get("/api/flowstudio/notices", headers=h).json()
    checked = 0
    for row in got["todo"] + got["feed"]:
        assert row["href"], row
        if row["panel_id"]:
            r = client.get(f"/api/flowstudio/panels/{row['panel_id']}", headers=h)
            assert r.status_code == 200, (row, r.text)
            checked += 1
    assert checked > 0


# ── the survivable failures ─────────────────────────────────────────────────


def test_a_deleted_panel_does_not_break_the_feed(client):
    """The event outlives the panel it is about — an append-only log always will.
    The feed has to skip it rather than raise."""
    t = _studio(client)
    _approve(t["panels_a"][0], by=t["pm_id"], artist=t["a_id"])
    with get_session() as s:
        ps.delete_batch(s, t["batch_a"])

    r = client.get("/api/flowstudio/notices", headers=_login(client, "nx_pm"))
    assert r.status_code == 200, r.text


def test_the_service_survives_an_anonymous_session(client):
    """`user is None` is the whole existing test suite and dev with auth off."""
    with get_session() as s:
        assert fn.summary(s, None)["unread"] == 0


# ── what a row shows ────────────────────────────────────────────────────────


def test_a_row_carries_the_picture_it_is_about(client):
    """A studio that adapts pictures, telling you about a picture, with no
    picture. Six rows of "PANELxxx came back" are six identical grey bands
    otherwise — the code is the only part that differs and it is the part you
    cannot read at a glance."""
    t = _studio(client)
    _send_back(t["panels_a"][0], by=t["pm_id"], artist=t["a_id"])
    _approve(t["panels_a"][1], by=t["pm_id"], artist=t["a_id"])

    got = client.get("/api/flowstudio/notices", headers=_login(client, "nx_a")).json()
    panel_rows = [r for r in got["todo"] + got["feed"] if r["panel_id"]]
    assert panel_rows
    missing = [r["id"] for r in panel_rows if not r["thumb_media_id"]]
    assert not missing, f"panel rows with no picture: {missing}"


def test_a_tally_row_has_no_picture_and_says_how_many(client):
    """"70 panels not started" is not about one panel, so there is nothing to
    show — the count takes the slot instead."""
    t = _studio(client)
    got = client.get("/api/flowstudio/notices", headers=_login(client, "nx_a")).json()
    tally = next(r for r in got["todo"] if r["kind"] == "not_started")
    assert tally["thumb_media_id"] is None
    assert tally["panel_id"] is None
    assert tally["count"] == 3


def test_the_breadcrumb_drops_the_stem_the_server_itself_generated(client):
    """A batch is named ``Project_Series_Chapter_batchNN``, so printing it whole
    after the comic and chapter repeats both — the same 70 characters on every
    row, differing only in the last two digits."""
    t = _studio(client)
    with get_session() as s:
        # A batch with the CONVENTIONAL name, which is what real ones have.
        batch = ps.create_batch(s, t["chapter_id"], None)
        ps.update_batch(s, batch.id, assignee_user_id=t["a_id"], set_assignee=True)
        made = ps.import_panels(s, batch.id, entries=[("Z1.png", "raw-z")])
        full_name = batch.name
    _send_back(made[0].id, by=t["pm_id"], artist=t["a_id"])

    got = client.get("/api/flowstudio/notices", headers=_login(client, "nx_a")).json()
    row = next(r for r in got["todo"] if r["panel_id"] == made[0].id)

    # The chapter already holds two batches, so this one is batch03 — read the
    # tail off the name rather than assuming the number.
    tail = full_name.rsplit("_", 1)[-1]
    # The comic's own name is built by the studio's convention now, so the test
    # reads it rather than assuming what the fixture typed.
    with get_session() as s:
        comic_name = ps.get_series(s, t["series_id"]).name
    assert full_name == f"Slate_{comic_name.replace('_', '-')}_Ch-1_{tail}", full_name
    assert row["where"] == f"{comic_name} · Ch 1 · {tail}", row["where"]
    assert full_name not in row["where"], "the whole generated stem came through"
