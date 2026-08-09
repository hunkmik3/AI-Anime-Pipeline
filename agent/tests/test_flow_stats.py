"""The comic side, as the admin console sees it.

`stats_service` reads eight tables and not one of them belongs to Giantflow, so
the console could answer "how is MoguTV going" and had nothing at all to say
about the panels. These cover the numbers that fill that hole, and two shapes of
wrong answer neither of which raises:

  * a total that is right by accident because two comics' panels were added
    together — so every test uses TWO comics with different amounts of work;
  * spend attributed to nobody and silently dropped, which leaves the ledger
    looking complete while money goes unexplained.
"""
from __future__ import annotations

import pytest

from flowboard.db import get_session
from flowboard.db.models import Request, UsageRecord
from flowboard.services import flow_stats as fs
from flowboard.services import panel_service as ps
from flowboard.services import user_service


def _h(client, u, p="pw123456"):
    r = client.post("/api/account/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture()
def slate(client):
    """Two comics with DIFFERENT amounts of work, and an artist on each.

    Different amounts on purpose: a rollup that added both together, or read
    only the first, would still produce a plausible-looking number if they
    matched.
    """
    user_service.create_user("fs_admin", "pw123456", role="admin")
    a = user_service.create_user("fs_a", "pw123456")
    b = user_service.create_user("fs_b", "pw123456")
    out: dict = {"h": _h(client, "fs_admin"), "a": a, "b": b}
    with get_session() as s:
        slate = ps.create_project(s, "Comics")
        for key, who, n in (("x", a, 5), ("y", b, 2)):
            comic = ps.create_series(s, slate.id, f"COMIC-{key.upper()}")
            ch = ps.create_chapter(s, comic.id, "Ch 1")
            batch = ps.create_batch(s, ch.id, f"b-{key}")
            ps.update_batch(s, batch.id, assignee_user_id=who.id, set_assignee=True)
            made = ps.import_panels(
                s, batch.id,
                entries=[(f"{key.upper()}{i}.png", f"raw-{key}-{i}") for i in range(n)],
            )
            out[f"comic_{key}"] = comic.id
            out[f"panels_{key}"] = [p.id for p in made]
    return out


def _approve(panel_id, by):
    with get_session() as s:
        ps.add_generated(s, panel_id, [f"g-{panel_id}"])
        ps.submit_panel(s, panel_id)
        ps.review_panel(s, panel_id, approve=True, author_user_id=by)


def _send_back(panel_id, by, times=1):
    for _ in range(times):
        with get_session() as s:
            ps.add_generated(s, panel_id, [f"g-{panel_id}"])
            ps.submit_panel(s, panel_id)
            ps.review_panel(s, panel_id, approve=False, notes=["nope"], author_user_id=by)


# ── progress ────────────────────────────────────────────────────────────────


def test_the_slate_total_is_the_sum_of_its_comics(client, slate):
    """A rollup that read only the first comic would look right on its own."""
    got = fs.overview()
    assert got["comics"] == 2
    assert got["panels"] == 7          # 5 + 2, not 5 and not 10
    assert got["status_counts"]["todo"] == 7


def test_approving_moves_the_slate_percentage(client, slate):
    _approve(slate["panels_x"][0], slate["a"].id)
    got = fs.overview()
    assert got["approved"] == 1
    assert got["approved_pct"] == round(1 / 7 * 100, 1)


def test_each_comic_counts_only_its_own_panels(client, slate):
    _approve(slate["panels_x"][0], slate["a"].id)
    _approve(slate["panels_x"][1], slate["a"].id)
    _approve(slate["panels_y"][0], slate["b"].id)

    rows = {r["name"]: r for r in fs.by_comic()}
    assert rows["COMIC-X"]["panels"] == 5
    assert rows["COMIC-X"]["approved"] == 2
    assert rows["COMIC-Y"]["panels"] == 2
    assert rows["COMIC-Y"]["approved"] == 1
    assert rows["COMIC-Y"]["approved_pct"] == 50.0


def test_a_comic_with_no_panels_still_appears(client, slate):
    """A comic nobody has started is a real state and the row a producer looks
    for — dropping it would make the slate look shorter than it is."""
    with get_session() as s:
        proj = ps.list_projects(s)[0]
        ps.create_series(s, proj.id, "COMIC-EMPTY")
    rows = {r["name"]: r for r in fs.by_comic()}
    assert "COMIC-EMPTY" in rows
    assert rows["COMIC-EMPTY"]["panels"] == 0
    assert rows["COMIC-EMPTY"]["approved_pct"] == 0.0


# ── by artist ───────────────────────────────────────────────────────────────


def test_work_is_attributed_through_the_batch_assignee(client, slate):
    """A panel has no owner of its own on the comic side; the batch does."""
    _approve(slate["panels_x"][0], slate["a"].id)
    rows = {r["name"]: r for r in fs.by_artist()}
    assert rows["fs_a"]["panels"] == 5
    assert rows["fs_a"]["approved"] == 1
    assert rows["fs_b"]["panels"] == 2
    assert rows["fs_b"]["approved"] == 0


def test_send_backs_count_rounds_not_panels(client, slate):
    """A panel returned three times is three send-backs. Counting panels would
    hide exactly the thing this column exists to show."""
    _send_back(slate["panels_x"][0], slate["a"].id, times=3)
    rows = {r["name"]: r for r in fs.by_artist()}
    assert rows["fs_a"]["sent_back"] == 3


def test_unassigned_panels_are_named_not_dropped(client, slate):
    with get_session() as s:
        proj = ps.list_projects(s)[0]
        comic = ps.create_series(s, proj.id, "COMIC-Z")
        ch = ps.create_chapter(s, comic.id, "Ch 1")
        batch = ps.create_batch(s, ch.id, "nobody")
        ps.import_panels(s, batch.id, entries=[("Z0.png", "raw-z")])

    rows = {r["name"]: r for r in fs.by_artist()}
    assert "Chưa giao" in rows, "panels with no artist vanished from the tally"
    assert rows["Chưa giao"]["panels"] == 1
    assert fs.overview()["panels"] == 8, "…while still counting in the total"


def test_runs_per_approved_is_zero_rather_than_infinity(client, slate):
    """Nobody of theirs is approved yet. Dividing by zero and reporting the
    result would put an infinity in a spreadsheet."""
    rows = {r["name"]: r for r in fs.by_artist()}
    assert rows["fs_a"]["runs_per_approved"] == 0.0


# ── money ───────────────────────────────────────────────────────────────────


def test_spend_follows_the_panel_a_run_was_for(client, slate):
    """The whole point of `Request.flow_panel_id`. Before it, a panel
    generation could not be attributed at all — the panel id lived in a JSON
    key nothing could join on."""
    pid = slate["panels_x"][0]
    with get_session() as s:
        req = Request(flow_panel_id=pid, type="flow_gen_image", status="done")
        s.add(req)
        s.commit()
        s.refresh(req)
        s.add(UsageRecord(user_id=slate["a"].id, request_id=req.id,
                          status="settled", actual_usd=0.25))
        s.commit()

    rows = {r["name"]: r for r in fs.by_comic()}
    assert rows["COMIC-X"]["spent_usd"] == 0.25
    assert rows["COMIC-X"]["runs"] == 1
    assert rows["COMIC-Y"]["spent_usd"] == 0.0, "spend leaked into the other comic"
    assert fs.overview()["spent_usd"] == 0.25


def test_a_hold_that_never_billed_is_not_spend(client, slate):
    """`estimated_usd` on a reserved record is a hold placed before a run.
    Counting it charges for generations that never happened."""
    with get_session() as s:
        req = Request(flow_panel_id=slate["panels_x"][0], type="flow_gen_image")
        s.add(req)
        s.commit()
        s.refresh(req)
        s.add(UsageRecord(user_id=slate["a"].id, request_id=req.id,
                          status="reserved", estimated_usd=9.99))
        s.commit()
    assert fs.overview()["spent_usd"] == 0.0


def test_money_with_nothing_to_attach_it_to_is_reported(client, slate):
    """Reported, not dropped. A ledger that silently omits what it cannot
    explain still totals correctly, so nobody goes looking."""
    with get_session() as s:
        req = Request(type="gen_video", status="done")   # no node, no panel
        s.add(req)
        s.commit()
        s.refresh(req)
        s.add(UsageRecord(user_id=slate["a"].id, request_id=req.id,
                          status="settled", actual_usd=1.5))
        s.commit()

    got = fs.unattributed()
    assert got["spent_usd"] == 1.5
    assert got["runs"] == 1
    # …and it did NOT quietly land on a comic.
    assert fs.overview()["spent_usd"] == 0.0


def test_cost_per_approved_divides_by_what_shipped(client, slate):
    """The re-dos are part of the price of the panel that shipped, so the
    divisor is approved panels, not runs."""
    pid = slate["panels_x"][0]
    with get_session() as s:
        for usd in (0.10, 0.10, 0.30):
            req = Request(flow_panel_id=pid, type="flow_gen_image", status="done")
            s.add(req)
            s.commit()
            s.refresh(req)
            s.add(UsageRecord(user_id=slate["a"].id, request_id=req.id,
                              status="settled", actual_usd=usd))
            s.commit()
    _approve(pid, slate["a"].id)

    got = fs.overview()
    assert got["runs"] == 3
    assert got["spent_usd"] == 0.5
    assert got["cost_per_approved"] == 0.5


# ── the endpoints ───────────────────────────────────────────────────────────


def test_the_console_can_read_all_of_it(client, slate):
    h = slate["h"]
    for path in (
        "/api/admin/stats/comics",
        "/api/admin/stats/comics/by-comic",
        "/api/admin/stats/comics/by-artist",
        "/api/admin/stats/unattributed",
    ):
        assert client.get(path, headers=h).status_code == 200, path


def test_a_member_cannot_read_the_console(client, slate):
    assert client.get(
        "/api/admin/stats/comics", headers=_h(client, "fs_a")
    ).status_code == 403
