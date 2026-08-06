"""Which version a panel delivers, and getting it back out.

Submitting used to record only THAT a panel was handed over, never WHICH of its
versions — every reader fell back to the newest one, so an artist who made ten
tries and preferred the seventh was overruled silently. These tests pin the
behaviour that replaced it, and the export that depends on it.
"""
from __future__ import annotations

import io
import zipfile

from flowboard.db import get_session
from flowboard.services import panel_service as ps


def _series(session, name):
    """A comic needs a slate above it now; these tests do not care which one."""
    project = ps.create_project(session, f"Slate for {name}")
    return ps.create_series(session, project.id, name)


def _chapter(session, name="Comic"):
    """A batch hangs off a chapter now; tests that only care about batches take
    the shortest path to one."""
    series = _series(session, name)
    return ps.create_chapter(session, series.id, "Chapter 1")


def _panel(n_versions: int = 3, extra_panels: int = 0):
    """A comic whose first panel has ``n_versions`` generated versions.

    ``extra_panels`` are imported in the SAME call on purpose: a batch refuses a
    second import, precisely so two numbering schemes cannot interleave.
    """
    entries = [("PANEL001.png", "raw-1")]
    entries += [(f"PANEL{i + 2:03d}.png", f"raw-{i + 2}") for i in range(extra_panels)]
    with get_session() as s:
        chapter = _chapter(s, "Comic")
        batch = ps.create_batch(s, chapter.id, "Batch")
        panel = ps.import_panels(s, batch.id, entries=entries)[0]
        ps.add_generated(
            s,
            panel.id,
            [f"gen-{i}" for i in range(1, n_versions + 1)],
            model_used="test-model",
        )
        return chapter.series_id, batch.id, panel.id


# ── choosing ──────────────────────────────────────────────────────────────


def test_submitting_records_which_version(client):
    _, _, panel_id = _panel(3)
    with get_session() as s:
        ps.submit_panel(s, panel_id, media_id="gen-2")
        panel = ps.get_panel(s, panel_id)
        assert panel.status == "submitted"
        assert panel.final_media_id == "gen-2"
        # The point: NOT the newest one.
        assert ps.delivered(s, panel_id).media_id == "gen-2"
        assert ps.latest_generated(s, panel_id).media_id == "gen-3"


def test_submitting_without_a_choice_takes_the_latest(client):
    """A caller with one version should not have to name it."""
    _, _, panel_id = _panel(2)
    with get_session() as s:
        ps.submit_panel(s, panel_id)
        assert ps.get_panel(s, panel_id).final_media_id == "gen-2"


def test_an_earlier_choice_survives_a_bare_resubmit(client):
    _, _, panel_id = _panel(3)
    with get_session() as s:
        ps.submit_panel(s, panel_id, media_id="gen-1")
        ps.review_panel(s, panel_id, approve=False, notes=["fix it"])
        ps.submit_panel(s, panel_id)
        assert ps.get_panel(s, panel_id).final_media_id == "gen-1"


def test_cannot_submit_an_image_that_is_not_a_version_of_this_panel(client):
    """Otherwise a submission could point at raw material, or another panel's
    work, and sail through review as the deliverable."""
    _, _, panel_id = _panel(2)
    with get_session() as s:
        try:
            ps.submit_panel(s, panel_id, media_id="raw-1")
        except ps.PanelError as exc:
            assert exc.code == "bad_input"
        else:
            raise AssertionError("raw material was accepted as a submission")


def test_delivered_falls_back_when_nobody_has_chosen(client):
    """A panel submitted before `final_media_id` existed, or still being worked
    on, has no pick — the newest version is the honest answer."""
    _, _, panel_id = _panel(2)
    with get_session() as s:
        assert ps.get_panel(s, panel_id).final_media_id is None
        assert ps.delivered(s, panel_id).media_id == "gen-2"


def test_approval_closes_generation(client):
    _, _, panel_id = _panel(1)
    with get_session() as s:
        ps.submit_panel(s, panel_id)
        ps.review_panel(s, panel_id, approve=True)
        try:
            ps.add_generated(s, panel_id, ["gen-99"])
        except ps.PanelError as exc:
            assert exc.code == "closed"
        else:
            raise AssertionError("an approved panel accepted a new version")


def test_sending_back_requires_a_reason(client):
    _, _, panel_id = _panel(1)
    with get_session() as s:
        ps.submit_panel(s, panel_id)
        try:
            ps.review_panel(s, panel_id, approve=False, notes=[])
        except ps.PanelError as exc:
            assert exc.code == "bad_input"
        else:
            raise AssertionError("a panel was sent back with no reason")


# ── the API surface ───────────────────────────────────────────────────────


def test_submit_over_http_reports_the_delivered_version(client):
    _, _, panel_id = _panel(3)
    r = client.post(f"/api/flowstudio/panels/{panel_id}/submit", json={"media_id": "gen-1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["final_media_id"] == "gen-1"
    assert body["delivered_media_id"] == "gen-1"
    assert body["delivered_version"] == 1
    assert body["version_count"] == 3


def test_bad_submit_is_a_400_not_a_500(client):
    _, _, panel_id = _panel(1)
    r = client.post(f"/api/flowstudio/panels/{panel_id}/submit", json={"media_id": "nope"})
    assert r.status_code == 400


# ── export ────────────────────────────────────────────────────────────────


def test_export_refuses_when_nothing_is_approved(client):
    _, batch_id, panel_id = _panel(1)
    r = client.get(f"/api/flowstudio/batches/{batch_id}/export")
    assert r.status_code == 404


def test_batch_export_contains_only_approved_panels(client, tmp_path, monkeypatch):
    """Named after the cutter's code, and holding the chosen version's bytes."""
    from flowboard.services import media as media_service

    # The second panel never gets approved — it must not appear in the zip.
    _, batch_id, panel_id = _panel(2, extra_panels=1)
    with get_session() as s:
        ps.submit_panel(s, panel_id, media_id="gen-1")
        ps.review_panel(s, panel_id, approve=True)

    png = tmp_path / "gen-1.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    monkeypatch.setattr(
        media_service, "cached_path", lambda mid: png if mid == "gen-1" else None
    )

    r = client.get(f"/api/flowstudio/batches/{batch_id}/export")
    assert r.status_code == 200, r.text
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert names == ["PANEL001.png"], names
    assert r.headers["x-export-written"] == "1"


def test_download_serves_the_chosen_version_not_the_latest(client, tmp_path, monkeypatch):
    from flowboard.services import media as media_service

    _, _, panel_id = _panel(3)
    with get_session() as s:
        ps.submit_panel(s, panel_id, media_id="gen-2")

    chosen = tmp_path / "gen-2.png"
    chosen.write_bytes(b"chosen-bytes")
    monkeypatch.setattr(
        media_service, "cached_path", lambda mid: chosen if mid == "gen-2" else None
    )

    r = client.get(f"/api/flowstudio/panels/{panel_id}/download")
    assert r.status_code == 200
    assert r.content == b"chosen-bytes"
    assert 'filename="PANEL001.png"' in r.headers["content-disposition"]
