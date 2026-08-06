"""The five-tier tree, end to end, hunting for what the migrations left behind.

Project → Series → Chapter → Batch → Panel. Three migrations rearranged this in
one sitting, and every id in it is a bare int, so the characteristic failure is
a lookup that runs, answers, and answers about the wrong row. These tests go
looking for that rather than restating what the code does.
"""
from __future__ import annotations

import io
import zipfile

from flowboard.db import get_session
from flowboard.services import panel_service as ps
from flowboard.services import user_service


def _login(client, username, password="pw123456"):
    r = client.post("/api/account/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _tree(client, *, chapters=2, batches=2, panels=2):
    """A comic with more than one of everything, so a mixed-up id shows."""
    user_service.create_user("tr_admin", "pw123456", role="admin")
    h = _login(client, "tr_admin")
    out = {"h": h, "chapters": [], "batches": [], "panels": []}
    with get_session() as s:
        project = ps.create_project(s, "Slate")
        series = ps.create_series(s, project.id, "Comic")
        out["project_id"], out["series_id"] = project.id, series.id
        for c in range(chapters):
            ch = ps.create_chapter(s, series.id, f"Chapter {c + 1}")
            out["chapters"].append(ch.id)
            for b in range(batches):
                batch = ps.create_batch(s, ch.id, f"c{c}b{b}")
                out["batches"].append(batch.id)
                made = ps.import_panels(
                    s,
                    batch.id,
                    entries=[(f"C{c}B{b}P{i}.png", f"raw-{c}-{b}-{i}") for i in range(panels)],
                )
                out["panels"] += [p.id for p in made]
    return out


# ── the tree holds together ───────────────────────────────────────────────


def test_counts_agree_at_every_tier(client):
    """Each level's totals must be the sum of the level below. A lookup that
    matched the wrong row would show up here as a number that does not add up."""
    t = _tree(client, chapters=2, batches=2, panels=3)
    h = t["h"]

    project = client.get("/api/flowstudio/projects", headers=h).json()[0]
    series = client.get("/api/flowstudio/series", headers=h).json()[0]
    chapters = client.get(f"/api/flowstudio/series/{t['series_id']}/chapters", headers=h).json()

    assert project["series_count"] == 1
    assert len(chapters) == 2
    assert project["panel_count"] == series["panel_count"] == 12
    assert series["chapter_count"] == 2
    assert series["batch_count"] == 4
    assert sum(c["panel_count"] for c in chapters) == 12
    assert sum(c["batch_count"] for c in chapters) == 4

    for c in chapters:
        rows = client.get(f"/api/flowstudio/chapters/{c['id']}/batches", headers=h).json()
        assert len(rows) == c["batch_count"]
        assert sum(b["panel_count"] for b in rows) == c["panel_count"]


def test_a_chapter_only_reports_its_own_batches(client):
    """Both chapters exist and both have batches; a query that lost the chapter
    boundary would hand back all four."""
    t = _tree(client)
    h = t["h"]
    first, second = t["chapters"]
    a = client.get(f"/api/flowstudio/chapters/{first}/batches", headers=h).json()
    b = client.get(f"/api/flowstudio/chapters/{second}/batches", headers=h).json()
    assert {x["id"] for x in a}.isdisjoint({x["id"] for x in b})
    assert len(a) == len(b) == 2


def test_filtering_all_panels_by_series_goes_through_the_chapters(client):
    t = _tree(client)
    h = t["h"]
    with get_session() as s:
        other = ps.create_series(s, t["project_id"], "Other comic")
        och = ps.create_chapter(s, other.id, "Ch")
        ob = ps.create_batch(s, och.id, "b")
        ps.import_panels(s, ob.id, entries=[("O.png", "raw-o")])

    mine = client.get(f"/api/flowstudio/panels?series_id={t['series_id']}", headers=h).json()
    assert len(mine) == 8
    assert all(p["series_id"] == t["series_id"] for p in mine)
    assert len(client.get("/api/flowstudio/panels", headers=h).json()) == 9


# ── deleting ──────────────────────────────────────────────────────────────


def test_deleting_a_chapter_takes_only_its_own_subtree(client):
    t = _tree(client)
    h = t["h"]
    doomed, kept = t["chapters"]
    before = len(client.get("/api/flowstudio/panels", headers=h).json())

    assert client.delete(f"/api/flowstudio/chapters/{doomed}", headers=h).status_code == 200
    after = client.get("/api/flowstudio/panels", headers=h).json()
    assert len(after) == before - 4
    # The sibling is untouched.
    assert len(client.get(f"/api/flowstudio/chapters/{kept}/batches", headers=h).json()) == 2


def test_deleting_a_project_leaves_nothing_behind(client):
    t = _tree(client)
    h = t["h"]
    assert client.delete(f"/api/flowstudio/projects/{t['project_id']}", headers=h).status_code == 200
    with get_session() as s:
        from sqlmodel import select

        from flowboard.db.models import FlowBatch, FlowChapter, FlowPanel, FlowSeries

        assert s.exec(select(FlowSeries)).all() == []
        assert s.exec(select(FlowChapter)).all() == []
        assert s.exec(select(FlowBatch)).all() == []
        assert s.exec(select(FlowPanel)).all() == []


# ── naming ────────────────────────────────────────────────────────────────


def test_generated_names_are_unique_across_the_tree(client):
    """Two chapters of the same comic both start at batch01; the name has to
    carry the chapter or the export folders collide."""
    t = _tree(client, chapters=2, batches=0, panels=0)
    h = t["h"]
    names = []
    for cid in t["chapters"]:
        made = client.post(
            f"/api/flowstudio/chapters/{cid}/batches/bulk",
            json={"batches": [{}, {}]},
            headers=h,
        ).json()
        names += [b["name"] for b in made]
    assert len(set(names)) == 4, names


# ── export ────────────────────────────────────────────────────────────────


def test_series_export_spans_its_chapters(client, tmp_path, monkeypatch):
    from flowboard.services import media as media_service

    t = _tree(client, chapters=2, batches=1, panels=1)
    h = t["h"]
    with get_session() as s:
        for pid in t["panels"]:
            ps.add_generated(s, pid, [f"gen-{pid}"])
            ps.submit_panel(s, pid)
            ps.review_panel(s, pid, approve=True)

    blob = tmp_path / "x.png"
    blob.write_bytes(b"bytes")
    monkeypatch.setattr(media_service, "cached_path", lambda mid: blob)

    r = client.get(f"/api/flowstudio/series/{t['series_id']}/export", headers=h)
    assert r.status_code == 200, r.text
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    # One panel per chapter, each in its own batch folder.
    assert len(names) == 2, names
    assert len({n.split("/")[0] for n in names}) == 2, names


def test_chapter_export_holds_only_that_chapter(client, tmp_path, monkeypatch):
    from flowboard.services import media as media_service

    t = _tree(client, chapters=2, batches=1, panels=1)
    h = t["h"]
    with get_session() as s:
        for pid in t["panels"]:
            ps.add_generated(s, pid, [f"gen-{pid}"])
            ps.submit_panel(s, pid)
            ps.review_panel(s, pid, approve=True)

    blob = tmp_path / "x.png"
    blob.write_bytes(b"bytes")
    monkeypatch.setattr(media_service, "cached_path", lambda mid: blob)

    r = client.get(f"/api/flowstudio/chapters/{t['chapters'][0]}/export", headers=h)
    assert r.status_code == 200, r.text
    assert len(zipfile.ZipFile(io.BytesIO(r.content)).namelist()) == 1


# ── reordering ────────────────────────────────────────────────────────────


def test_reordering_chapters_cannot_reach_another_comic(client):
    """`reorder_chapters` is handed a list of ids. Nothing in that list says
    which comic they belong to, so the endpoint has to check."""
    t = _tree(client, chapters=1, batches=0, panels=0)
    h = t["h"]
    with get_session() as s:
        other = ps.create_series(s, t["project_id"], "Other")
        other_id = other.id
        # TWO chapters, so the second sits at index 1. With only one it would
        # already be at 0 and a reorder to 0 would look like "nothing happened"
        # whether the endpoint checked ownership or not.
        ps.create_chapter(s, other_id, "Theirs A")
        foreign = ps.create_chapter(s, other_id, "Theirs B")
        foreign_id, before = foreign.id, foreign.order_index
    assert before == 1

    client.post(
        f"/api/flowstudio/series/{t['series_id']}/chapters/reorder",
        json={"ids": [foreign_id]},
        headers=h,
    )
    with get_session() as s:
        after = ps.get_chapter(s, foreign_id)
        assert after.series_id == other_id
        assert after.order_index == before, (
            "reordering one comic's chapters moved another comic's"
        )


def test_reordering_series_cannot_reach_another_slate(client):
    """Same hole as chapters, one tier up."""
    t = _tree(client, chapters=0, batches=0, panels=0)
    h = t["h"]
    with get_session() as s:
        other_project = ps.create_project(s, "Other slate")
        opid = other_project.id
        ps.create_series(s, opid, "A")
        foreign = ps.create_series(s, opid, "B")
        foreign_id, before = foreign.id, foreign.order_index
    assert before == 1

    client.post(
        f"/api/flowstudio/projects/{t['project_id']}/series/reorder",
        json={"ids": [foreign_id]},
        headers=h,
    )
    with get_session() as s:
        assert ps.get_series(s, foreign_id).order_index == before, (
            "reordering one slate's series moved another slate's"
        )

