"""End to end: a comic from import to export, and the ways it can go wrong.

Written to look for bugs rather than to record that today's code does what today's
code does. Each test states the situation a studio would actually hit, so a
failure names a real consequence instead of a broken assertion.
"""
from __future__ import annotations

import io
import zipfile

import pytest

from flowboard.db import get_session
from flowboard.services import flow_permissions as fp
from flowboard.services import panel_service as ps
from flowboard.services import user_service


def _login(client, username, password="pw123456"):
    r = client.post("/api/account/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture
def studio(client):
    """An admin, a PM, an artist, and an empty comic."""
    user_service.create_user("lc_admin", "pw123456", role="admin")
    pm = user_service.create_user("lc_pm", "pw123456", role="user")
    artist = user_service.create_user("lc_artist", "pw123456", role="user")
    h = {
        "admin": _login(client, "lc_admin"),
        "pm": _login(client, "lc_pm"),
        "artist": _login(client, "lc_artist"),
    }
    # A comic needs a slate above it now.
    proj = client.post(
        "/api/flowstudio/projects", json={"name": "Global Comix"}, headers=h["admin"]
    )
    assert proj.status_code in (200, 201), proj.text
    r = client.post(
        "/api/flowstudio/series",
        json={"project_id": proj.json()["id"], "name": "MAGMEL"},
        headers=h["admin"],
    )
    assert r.status_code in (200, 201), r.text
    series_id = r.json()["id"]
    client.put(
        f"/api/flowstudio/series/{series_id}/members",
        json={"user_id": str(pm.id), "role": "producer"},
        headers=h["admin"],
    )
    return {"h": h, "series_id": series_id, "pm": pm, "artist": artist}


# ── the whole road ────────────────────────────────────────────────────────


def test_a_comic_from_import_to_export(client, studio, tmp_path, monkeypatch):
    """The path the studio walks every day, in one test, over HTTP only."""
    from flowboard.services import media as media_service

    h, series_id, artist = studio["h"], studio["series_id"], studio["artist"]

    # A chapter first — that is the tier the work is divided on.
    ch = client.post(
        f"/api/flowstudio/series/{series_id}/chapters",
        json={"name": "Chapter 1"},
        headers=h["pm"],
    )
    assert ch.status_code in (200, 201), ch.text
    chapter_id = ch.json()["id"]

    # PM divides the work and hands a share to the artist.
    r = client.post(
        f"/api/flowstudio/chapters/{chapter_id}/batches",
        json={"name": "Quân", "assignee_user_id": str(artist.id)},
        headers=h["pm"],
    )
    assert r.status_code in (200, 201), r.text
    batch_id = r.json()["id"]

    with get_session() as s:
        panels = ps.import_panels(
            s, batch_id, entries=[(f"PANEL{i:03d}.png", f"raw-{i}") for i in (1, 2)]
        )
        panel_id = panels[0].id
        # Being handed a batch is what makes them an artist here.
        assert fp.role_for(s, artist, series_id) == fp.ARTIST

    # Artist generates three tries and hands over the second.
    with get_session() as s:
        ps.add_generated(s, panel_id, ["g1", "g2", "g3"], model_used="m")
    r = client.post(
        f"/api/flowstudio/panels/{panel_id}/submit", json={"media_id": "g2"}, headers=h["artist"]
    )
    assert r.status_code == 200, r.text
    assert r.json()["delivered_media_id"] == "g2"

    # It appears in the PM's pile, and nowhere else.
    queue = client.get("/api/flowstudio/review-queue", headers=h["pm"]).json()
    assert [q["id"] for q in queue] == [panel_id]

    # PM sends it back with a reason; the artist can read it.
    r = client.post(
        f"/api/flowstudio/panels/{panel_id}/review",
        json={"approve": False, "notes": ["Màu nền lệch"]},
        headers=h["pm"],
    )
    assert r.status_code == 200, r.text
    mine = client.get("/api/flowstudio/my-work", headers=h["artist"]).json()
    assert [n["body"] for n in mine["changes_requested"][0]["notes"]] == ["Màu nền lệch"]

    # Artist fixes it — a file from outside software becomes the next version.
    with get_session() as s:
        ps.add_generated(s, panel_id, ["fixed"], model_used=None)
    client.post(
        f"/api/flowstudio/panels/{panel_id}/submit", json={"media_id": "fixed"}, headers=h["artist"]
    )

    # PM approves. Generation closes.
    r = client.post(
        f"/api/flowstudio/panels/{panel_id}/review",
        json={"approve": True, "notes": []},
        headers=h["pm"],
    )
    assert r.json()["status"] == "approved"
    assert (
        client.post(
            f"/api/flowstudio/panels/{panel_id}/generate",
            json={"prompt": "more"},
            headers=h["artist"],
        ).status_code
        == 409
    )

    # Export hands back the approved version — the chosen one.
    blob = tmp_path / "fixed.png"
    blob.write_bytes(b"the-approved-bytes")
    monkeypatch.setattr(
        media_service, "cached_path", lambda mid: blob if mid == "fixed" else None
    )
    r = client.get(f"/api/flowstudio/batches/{batch_id}/export", headers=h["pm"])
    assert r.status_code == 200, r.text
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert z.namelist() == ["PANEL001.png"]
    assert z.read("PANEL001.png") == b"the-approved-bytes"

    # And the artist sees it settled.
    mine = client.get("/api/flowstudio/my-work", headers=h["artist"]).json()
    assert [p["id"] for p in mine["approved"]] == [panel_id]
    assert mine["changes_requested"] == []


def test_reopening_returns_the_panel_to_work_without_losing_the_pick(client, studio):
    h, series_id = studio["h"], studio["series_id"]
    with get_session() as s:
        chapter = ps.create_chapter(s, series_id, "Chapter 1")
        batch = ps.create_batch(s, chapter.id, "B")
        panel_id = ps.import_panels(s, batch.id, entries=[("P.png", "raw")])[0].id
        ps.add_generated(s, panel_id, ["g1", "g2"])
        ps.submit_panel(s, panel_id, media_id="g1")
        ps.review_panel(s, panel_id, approve=True)

    r = client.post(f"/api/flowstudio/panels/{panel_id}/reopen", headers=h["pm"])
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "changes_requested"
    # The PM's remarks refer to a specific image; losing it would orphan them.
    assert r.json()["final_media_id"] == "g1"
    with get_session() as s:
        ps.add_generated(s, panel_id, ["g3"])  # generation is open again


# ── the ways it goes wrong ────────────────────────────────────────────────


def test_a_panel_with_no_versions_cannot_be_approved(client, studio):
    """Approving untouched work locks generation on a panel nobody has made, and
    puts a row into the export with no image behind it."""
    h, series_id = studio["h"], studio["series_id"]
    with get_session() as s:
        chapter = ps.create_chapter(s, series_id, "Chapter 1")
        batch = ps.create_batch(s, chapter.id, "B")
        panel_id = ps.import_panels(s, batch.id, entries=[("P.png", "raw")])[0].id
        assert ps.get_panel(s, panel_id).status == "todo"

    r = client.post(
        f"/api/flowstudio/panels/{panel_id}/review",
        json={"approve": True, "notes": []},
        headers=h["pm"],
    )
    assert r.status_code == 400, (
        "a panel with nothing in it was approved — export would then list a "
        f"deliverable that does not exist (got {r.status_code})"
    )


def test_a_panel_nobody_submitted_cannot_be_ruled_on(client, studio):
    """A verdict is a reply to a handover. Ruling on work still in progress takes
    it away from the artist mid-edit."""
    h, series_id = studio["h"], studio["series_id"]
    with get_session() as s:
        chapter = ps.create_chapter(s, series_id, "Chapter 1")
        batch = ps.create_batch(s, chapter.id, "B")
        panel_id = ps.import_panels(s, batch.id, entries=[("P.png", "raw")])[0].id
        ps.add_generated(s, panel_id, ["g1"])  # in_progress, never submitted

    r = client.post(
        f"/api/flowstudio/panels/{panel_id}/review",
        json={"approve": True, "notes": []},
        headers=h["pm"],
    )
    assert r.status_code == 400, f"in-progress work was ruled on (got {r.status_code})"


def test_a_member_row_can_never_confer_admin(client, studio):
    """`flow_project_member.role` is a project role. If a stored `admin` string
    resolved to system admin, one member row would grant the right to delete
    other people's comics."""
    h, series_id = studio["h"], studio["series_id"]
    someone = user_service.create_user("lc_x", "pw123456", role="user")

    # The route refuses it outright.
    r = client.put(
        f"/api/flowstudio/series/{series_id}/members",
        json={"user_id": str(someone.id), "role": "admin"},
        headers=h["pm"],
    )
    assert r.status_code == 400

    # And if one ever got in by another path, it must not resolve to admin.
    with get_session() as s:
        from flowboard.db.models import FlowSeriesMember
        from sqlmodel import select

        s.add(FlowSeriesMember(series_id=series_id, user_id=someone.id, role="admin"))
        s.commit()
        role = fp.role_for(s, someone, series_id)
        assert select is not None
    assert role != fp.ADMIN, "a member row granted system-admin authority"


def test_deleting_a_batch_takes_its_panels_and_notes_with_it(client, studio):
    h, series_id = studio["h"], studio["series_id"]
    with get_session() as s:
        chapter = ps.create_chapter(s, series_id, "Chapter 1")
        batch = ps.create_batch(s, chapter.id, "B")
        batch_id = batch.id
        panel_id = ps.import_panels(s, batch_id, entries=[("P.png", "raw")])[0].id
        ps.add_generated(s, panel_id, ["g1"])
        ps.submit_panel(s, panel_id)
        ps.review_panel(s, panel_id, approve=False, notes=["fix"])

    assert client.delete(f"/api/flowstudio/batches/{batch_id}", headers=h["pm"]).status_code == 200
    with get_session() as s:
        from flowboard.db.models import FlowPanel, FlowPanelImage, FlowPanelNote
        from sqlmodel import select

        assert s.exec(select(FlowPanel).where(FlowPanel.batch_id == batch_id)).all() == []
        assert s.exec(select(FlowPanelImage).where(FlowPanelImage.panel_id == panel_id)).all() == []
        assert s.exec(select(FlowPanelNote).where(FlowPanelNote.panel_id == panel_id)).all() == []


def test_reassigning_a_batch_moves_the_work_to_the_new_artist(client, studio):
    """`my-work` reads the batch's assignee, so a hand-over must be complete —
    the old artist keeps nothing, the new one inherits everything."""
    h, series_id = studio["h"], studio["series_id"]
    first = studio["artist"]
    second = user_service.create_user("lc_artist2", "pw123456", role="user")
    with get_session() as s:
        chapter = ps.create_chapter(s, series_id, "Chapter 1")
        batch = ps.create_batch(s, chapter.id, "B", assignee_user_id=first.id)
        panel_id = ps.import_panels(s, batch.id, entries=[("P.png", "raw")])[0].id
        ps.add_generated(s, panel_id, ["g1"])
        ps.submit_panel(s, panel_id)
        batch_id = batch.id

    assert len(client.get("/api/flowstudio/my-work", headers=h["artist"]).json()["submitted"]) == 1
    client.patch(
        f"/api/flowstudio/batches/{batch_id}",
        json={"assignee_user_id": str(second.id), "set_assignee": True},
        headers=h["pm"],
    )
    assert client.get("/api/flowstudio/my-work", headers=h["artist"]).json()["submitted"] == []
    h2 = _login(client, "lc_artist2")
    assert len(client.get("/api/flowstudio/my-work", headers=h2).json()["submitted"]) == 1


def test_export_skips_an_image_it_cannot_read_instead_of_failing(client, studio, tmp_path, monkeypatch):
    """One broken file must not cost a producer the other two hundred."""
    from flowboard.services import media as media_service

    h, series_id = studio["h"], studio["series_id"]
    with get_session() as s:
        chapter = ps.create_chapter(s, series_id, "Chapter 1")
        batch = ps.create_batch(s, chapter.id, "B")
        panels = ps.import_panels(
            s, batch.id, entries=[("A.png", "raw-a"), ("B.png", "raw-b")]
        )
        for p, mid in zip(panels, ("ok", "broken")):
            ps.add_generated(s, p.id, [mid])
            ps.submit_panel(s, p.id, media_id=mid)
            ps.review_panel(s, p.id, approve=True)
        batch_id = batch.id

    good = tmp_path / "ok.png"
    good.write_bytes(b"fine")
    monkeypatch.setattr(media_service, "cached_path", lambda mid: good if mid == "ok" else None)

    async def _no_fetch(media_id):
        return None

    monkeypatch.setattr(media_service, "fetch_and_cache", _no_fetch)

    r = client.get(f"/api/flowstudio/batches/{batch_id}/export", headers=h["pm"])
    assert r.status_code == 200
    assert zipfile.ZipFile(io.BytesIO(r.content)).namelist() == ["A.png"]
    assert r.headers["x-export-written"] == "1"
    assert r.headers["x-export-skipped"] == "1"


def test_a_second_import_into_the_same_batch_is_refused(client, studio):
    """Two numbering schemes interleaved cannot be untangled by hand."""
    _, series_id = studio["h"], studio["series_id"]
    with get_session() as s:
        chapter = ps.create_chapter(s, series_id, "Chapter 1")
        batch = ps.create_batch(s, chapter.id, "B")
        ps.import_panels(s, batch.id, entries=[("A.png", "raw-a")])
        with pytest.raises(ps.PanelError):
            ps.import_panels(s, batch.id, entries=[("B.png", "raw-b")])


def test_two_batches_may_each_have_their_own_PANEL001(client, studio):
    """Codes are unique per batch, not per comic — every artist's folder starts
    at one."""
    _, series_id = studio["h"], studio["series_id"]
    with get_session() as s:
        chapter = ps.create_chapter(s, series_id, "Chapter 1")
        a = ps.create_batch(s, chapter.id, "A")
        chapter = ps.create_chapter(s, series_id, "Chapter 1")
        b = ps.create_batch(s, chapter.id, "B")
        ps.import_panels(s, a.id, entries=[("PANEL001.png", "raw-1")])
        ps.import_panels(s, b.id, entries=[("PANEL001.png", "raw-2")])
        assert len(ps.list_series_panels(s, series_id)) == 2


def test_import_sorts_by_the_cutters_numbering_not_arrival_order(client, studio):
    """A browser hands folders over in filesystem order. Trusting it produced
    PANEL111, PANEL105, PANEL065…"""
    _, series_id = studio["h"], studio["series_id"]
    with get_session() as s:
        chapter = ps.create_chapter(s, series_id, "Chapter 1")
        batch = ps.create_batch(s, chapter.id, "B")
        ps.import_panels(
            s,
            batch.id,
            entries=[("PANEL010.png", "m10"), ("PANEL002.png", "m2"), ("PANEL001.png", "m1")],
        )
        codes = [p.code for p in ps.list_panels(s, batch.id)]
    assert codes == ["PANEL001", "PANEL002", "PANEL010"]


def test_deleting_a_comic_leaves_nothing_behind(client, studio):
    h, series_id = studio["h"], studio["series_id"]
    with get_session() as s:
        chapter = ps.create_chapter(s, series_id, "Chapter 1")
        batch = ps.create_batch(s, chapter.id, "B")
        ps.import_panels(s, batch.id, entries=[("P.png", "raw")])
        batch_id = batch.id

    assert client.delete(f"/api/flowstudio/series/{series_id}", headers=h["admin"]).status_code == 200
    with get_session() as s:
        from flowboard.db.models import FlowBatch, FlowPanel
        from sqlmodel import select

        assert s.exec(select(FlowBatch).where(FlowBatch.id == batch_id)).all() == []
        assert s.exec(select(FlowPanel).where(FlowPanel.batch_id == batch_id)).all() == []


def test_every_tier_is_reachable_over_http(client, studio):
    """Walk the whole tree through the API.

    Every id in this hierarchy is a plain int, so handing a chapter id to a
    series lookup type-checks, runs, and 404s on something that exists — which is
    exactly what shipped on the batches endpoint. Walking each tier is the cheap
    way to catch that class of mistake.
    """
    h, series_id = studio["h"], studio["series_id"]
    ch = client.post(
        f"/api/flowstudio/series/{series_id}/chapters",
        json={"name": "Chapter 1"},
        headers=h["pm"],
    )
    assert ch.status_code in (200, 201), ch.text
    chapter_id = ch.json()["id"]

    assert client.get("/api/flowstudio/projects", headers=h["pm"]).status_code == 200
    assert client.get("/api/flowstudio/series", headers=h["pm"]).status_code == 200
    assert client.get(f"/api/flowstudio/series/{series_id}/chapters", headers=h["pm"]).status_code == 200
    assert client.get(f"/api/flowstudio/chapters/{chapter_id}", headers=h["pm"]).status_code == 200
    # The one that was broken: a chapter id validated against the series table.
    r = client.get(f"/api/flowstudio/chapters/{chapter_id}/batches", headers=h["pm"])
    assert r.status_code == 200, r.text

    made = client.post(
        f"/api/flowstudio/chapters/{chapter_id}/batches",
        json={"name": "B"},
        headers=h["pm"],
    )
    assert made.status_code in (200, 201), made.text
    batch_id = made.json()["id"]
    assert made.json()["chapter_id"] == chapter_id
    assert client.get(f"/api/flowstudio/batches/{batch_id}", headers=h["pm"]).status_code == 200
    assert client.get(f"/api/flowstudio/batches/{batch_id}/panels", headers=h["pm"]).status_code == 200


def test_every_dto_carries_the_tier_above_it(client, studio):
    """Each level has to name its parent, or the UI cannot build a link back.

    A missing `project_id` on the series DTO produced `/giantflow/p/undefined`,
    which then asked the API for project "NaN" — an error two screens away from
    the field that was actually absent.
    """
    h, series_id = studio["h"], studio["series_id"]
    ch = client.post(
        f"/api/flowstudio/series/{series_id}/chapters",
        json={"name": "Chapter 1"},
        headers=h["pm"],
    )
    chapter_id = ch.json()["id"]
    batch = client.post(
        f"/api/flowstudio/chapters/{chapter_id}/batches",
        json={"name": "B"},
        headers=h["pm"],
    ).json()

    series = next(
        s for s in client.get("/api/flowstudio/series", headers=h["pm"]).json()
        if s["id"] == series_id
    )
    assert isinstance(series["project_id"], int)
    assert client.get(f"/api/flowstudio/chapters/{chapter_id}", headers=h["pm"]).json()["series_id"] == series_id
    assert batch["chapter_id"] == chapter_id


def test_series_counts_are_reached_through_its_chapters(client, studio):
    """`list_batches` takes a CHAPTER id. Handing it a series id returned the
    batches of whichever chapter shared that number — never an error, just a
    wrong count on the card."""
    h, series_id = studio["h"], studio["series_id"]
    ch = client.post(
        f"/api/flowstudio/series/{series_id}/chapters",
        json={"name": "Chapter 1"},
        headers=h["pm"],
    ).json()
    for name in ("A", "B"):
        client.post(
            f"/api/flowstudio/chapters/{ch['id']}/batches",
            json={"name": name},
            headers=h["pm"],
        )
    series = next(
        s for s in client.get("/api/flowstudio/series", headers=h["pm"]).json()
        if s["id"] == series_id
    )
    assert series["chapter_count"] == 1
    assert series["batch_count"] == 2

