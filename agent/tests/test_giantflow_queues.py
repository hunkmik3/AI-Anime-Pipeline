"""The three cross-cutting views: review queue, my work, all panels.

Each exists because the project tree cannot answer its question. A reviewer asks
"what is waiting on me, across everyone", an artist asks "what came back to me,
and why", a manager asks "where is anything". All three cross a boundary that
the batch grid enforces, so all three are queries rather than navigation — and a
query that silently returns the wrong set is worse than no page.
"""
from __future__ import annotations

from flowboard.db import get_session
from flowboard.services import panel_service as ps
from flowboard.services import user_service


def _series(session, name):
    """A comic needs a slate above it now; tests do not care which one."""
    project = ps.create_project(session, f"Slate for {name}")
    return ps.create_series(session, project.id, name)


def _login(client, username, password="pw123456"):
    r = client.post("/api/account/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _studio(client):
    """Two artists, one comic, a batch each, three panels each."""
    quan = user_service.create_user("q_artist", "pw123456", role="user")
    phat = user_service.create_user("p_artist", "pw123456", role="user")
    pm = user_service.create_user("q_pm", "pw123456", role="admin")

    out = {}
    with get_session() as s:
        series = _series(s, "X-MEN")
        for who, user in (("quan", quan), ("phat", phat)):
            batch = ps.create_batch(s, series.id, f"Batch {who}", assignee_user_id=user.id)
            panels = ps.import_panels(
                s,
                batch.id,
                entries=[(f"{who.upper()}_{i:03d}.png", f"raw-{who}-{i}") for i in (1, 2, 3)],
            )
            for p in panels:
                ps.add_generated(s, p.id, [f"gen-{who}-{p.id}"], model_used="m")
            out[who] = [p.id for p in panels]
        out["series_id"] = series.id
    return out, {"quan": quan, "phat": phat, "pm": pm}


# ── the PM's queue ────────────────────────────────────────────────────────


def test_review_queue_holds_only_submitted_work(client):
    ids, _ = _studio(client)
    with get_session() as s:
        ps.submit_panel(s, ids["quan"][0])
        ps.submit_panel(s, ids["phat"][1])
        # Approved and untouched work must not clutter the pile.
        ps.submit_panel(s, ids["quan"][1])
        ps.review_panel(s, ids["quan"][1], approve=True)

    rows = client.get("/api/flowstudio/review-queue").json()
    assert {r["id"] for r in rows} == {ids["quan"][0], ids["phat"][1]}
    # Every card carries what a verdict needs: the original, the chosen result,
    # and whose work it is.
    for r in rows:
        assert r["raw_media_id"]
        assert r["delivered_media_id"]
        assert r["series_name"] == "X-MEN"


def test_review_queue_can_be_narrowed_to_one_comic(client):
    ids, _ = _studio(client)
    with get_session() as s:
        other = _series(s, "Other")
        other_batch = ps.create_batch(s, other.id, "B")
        other_panel = ps.import_panels(s, other_batch.id, entries=[("O.png", "raw-o")])[0]
        ps.add_generated(s, other_panel.id, ["gen-o"])
        ps.submit_panel(s, other_panel.id)
        ps.submit_panel(s, ids["quan"][0])

    only = client.get(f"/api/flowstudio/review-queue?series_id={ids['series_id']}").json()
    assert [r["id"] for r in only] == [ids["quan"][0]]


# ── the artist's results ──────────────────────────────────────────────────


def test_my_work_is_scoped_to_the_batch_assignee(client):
    ids, users = _studio(client)
    with get_session() as s:
        ps.submit_panel(s, ids["quan"][0])
        ps.review_panel(s, ids["quan"][0], approve=False, notes=["BG lệch màu"])
        ps.submit_panel(s, ids["phat"][0])

    mine = client.get("/api/flowstudio/my-work", headers=_login(client, "q_artist")).json()
    assert [p["id"] for p in mine["changes_requested"]] == [ids["quan"][0]]
    # Phát's submission is Phát's business.
    assert mine["submitted"] == []
    # The reason is carried, because reading it is the entire point of the page.
    assert [n["body"] for n in mine["changes_requested"][0]["notes"]] == ["BG lệch màu"]


def test_my_work_hides_notes_already_ticked_off(client):
    ids, _ = _studio(client)
    with get_session() as s:
        ps.submit_panel(s, ids["quan"][0])
        ps.review_panel(s, ids["quan"][0], approve=False, notes=["old"])
        for n in ps.list_notes(s, ids["quan"][0]):
            ps.set_note_resolved(s, n.id, True)

    mine = client.get("/api/flowstudio/my-work", headers=_login(client, "q_artist")).json()
    assert mine["changes_requested"][0]["notes"] == []


def test_my_work_is_empty_for_someone_with_no_batch(client):
    _studio(client)
    user_service.create_user("nobody", "pw123456", role="user")
    mine = client.get("/api/flowstudio/my-work", headers=_login(client, "nobody")).json()
    assert mine == {"changes_requested": [], "submitted": [], "approved": []}


# ── the management table ──────────────────────────────────────────────────


def test_all_panels_spans_batches_and_keeps_page_order(client):
    ids, _ = _studio(client)
    rows = client.get("/api/flowstudio/panels").json()
    assert len(rows) == 6
    # The studio's own numbering, not recency: a manager reads a comic in order.
    assert [r["code"] for r in rows][:3] == ["QUAN_001", "QUAN_002", "QUAN_003"]


def test_all_panels_filters_combine(client):
    ids, _ = _studio(client)
    with get_session() as s:
        ps.submit_panel(s, ids["quan"][0])
        ps.review_panel(s, ids["quan"][0], approve=True)

    approved = client.get("/api/flowstudio/panels?status=approved").json()
    assert [r["id"] for r in approved] == [ids["quan"][0]]

    # Several statuses in one request, so "sent back or in review" is one call.
    # Generating moves a panel off `todo`, so the fixture's six are in_progress
    # until one is approved — asserting on `todo` here would pass for the wrong
    # reason.
    pair = client.get("/api/flowstudio/panels?status=in_progress,approved").json()
    assert len(pair) == 6

    found = client.get("/api/flowstudio/panels?q=PHAT_00").json()
    assert len(found) == 3
    assert all(r["code"].startswith("PHAT") for r in found)


def test_all_panels_rejects_a_status_that_does_not_exist(client):
    """A typo must not quietly return everything."""
    assert client.get("/api/flowstudio/panels?status=finished").status_code == 400
