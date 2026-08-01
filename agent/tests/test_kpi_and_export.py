"""KPI rollups and CSV export.

Both exist to keep the app the single source of truth. KPI answers "how is
delivery going" from the recorded history rather than a hand-maintained sheet;
export means that when someone needs a view the app doesn't render, they take a
snapshot instead of retyping — a retyped copy drifts immediately, which is the
failure that made the studio leave spreadsheets in the first place.
"""
from __future__ import annotations

import csv
import io

import pytest

from flowboard.services import user_service


def _h(client, u, p="pw123456"):
    tok = client.post("/api/account/login", json={"username": u, "password": p}).json()
    return {"Authorization": f"Bearer {tok['token']}"}


def _rows(resp) -> list[dict]:
    text = resp.text.lstrip("﻿")
    return list(csv.DictReader(io.StringIO(text)))


@pytest.fixture()
def delivered(client):
    """Two episodes: one approved first try, one approved after a rejection."""
    user_service.create_user("boss", "pw123456", role="admin")
    ah = _h(client, "boss")
    pm = user_service.create_user("pm", "pw123456")
    emp = user_service.create_user("emp", "pw123456")

    pid = client.post(
        "/api/projects", json={"name": "MOGU", "owner_user_id": str(pm.id)}, headers=ah
    ).json()["id"]
    sid = client.post(
        f"/api/projects/{pid}/series", json={"name": "S1", "code": "S1"}, headers=ah
    ).json()["id"]

    eps = []
    for n in (1, 2):
        ep = client.post(
            f"/api/projects/{pid}/scenes",
            json={"name": f"EP0{n}", "code": f"EP0{n}", "series_id": sid},
            headers=ah,
        ).json()["id"]
        client.patch(f"/api/scenes/{ep}/assignee", json={"user_id": str(emp.id)}, headers=ah)
        eps.append(ep)

    eh = _h(client, "emp")
    D = "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUv/view"

    # EP01: approved on the first attempt.
    s1 = client.post(f"/api/scenes/{eps[0]}/submissions", json={"drive_url": D}, headers=eh).json()
    client.post(f"/api/submissions/{s1['id']}/approve", json={"note": "good"}, headers=ah)

    # EP02: rejected once, then approved.
    s2 = client.post(f"/api/scenes/{eps[1]}/submissions", json={"drive_url": D}, headers=eh).json()
    client.post(f"/api/submissions/{s2['id']}/reject", json={"note": "lip sync off"}, headers=ah)
    s2b = client.post(f"/api/scenes/{eps[1]}/submissions", json={"drive_url": D}, headers=eh).json()
    client.post(f"/api/submissions/{s2b['id']}/approve", json={"note": "ok now"}, headers=ah)

    return {"ah": ah, "pid": pid, "sid": sid, "eps": eps, "emp": emp, "pm": pm}


# ── KPI ────────────────────────────────────────────────────────────────────


def test_kpi_counts_delivery_and_rework_per_person(client, delivered):
    r = client.get(f"/api/kpi/projects/{delivered['pid']}", headers=delivered["ah"])
    assert r.status_code == 200
    rows = {p["name"]: p for p in r.json()["people"]}
    emp = rows["emp"]

    assert emp["assigned"] == 2
    assert emp["delivered"] == 2
    assert emp["attempts"] == 3       # EP02 took two goes
    assert emp["rejections"] == 1
    assert emp["first_pass"] == 1     # only EP01 landed first time
    assert emp["first_pass_rate"] == 0.5
    assert emp["avg_attempts"] == 1.5


def test_kpi_surfaces_unassigned_work_instead_of_dropping_it(client, delivered):
    """A PM needs to see the gap, so episodes with no owner are their own row."""
    client.post(
        f"/api/projects/{delivered['pid']}/scenes",
        json={"name": "EP99", "series_id": delivered["sid"]},
        headers=delivered["ah"],
    )
    people = client.get(
        f"/api/kpi/projects/{delivered['pid']}", headers=delivered["ah"]
    ).json()["people"]
    unassigned = [p for p in people if p["user_id"] is None]
    assert unassigned and unassigned[0]["assigned"] == 1
    assert people[-1]["user_id"] is None  # sorted last — it reads as a residue


def test_series_rollup_reports_completion(client, delivered):
    out = client.get(f"/api/kpi/series/{delivered['sid']}", headers=delivered["ah"]).json()
    assert out["episodes"] == 2
    assert out["delivered"] == 2
    assert out["completion_pct"] == 100.0
    assert out["unassigned"] == 0
    assert "budget" in out and "people" in out


def test_kpi_has_no_targets_or_scores(client, delivered):
    """Deliberate: what counts as good is a management decision nobody has
    stated. Inventing a score here would bake in a rule no one asked for."""
    emp = [
        p
        for p in client.get(
            f"/api/kpi/projects/{delivered['pid']}", headers=delivered["ah"]
        ).json()["people"]
        if p["name"] == "emp"
    ][0]
    assert not any(k in emp for k in ("score", "grade", "target", "rating"))


@pytest.mark.parametrize("role", ["artist", "lead", "producer"])
def test_kpi_is_for_the_board_not_for_peers(client, delivered, role):
    """The studio treats this as a board tracker: admins and BOD only.

    A producer running the project is deliberately excluded too — comparing
    colleagues' delivery and rework is not information the app hands to a peer,
    even a senior one.
    """
    peer = user_service.create_user(f"peer_{role}", "pw123456")
    client.put(
        f"/api/projects/{delivered['pid']}/members",
        json={
            "members": [
                {"user_id": str(delivered["pm"].id), "role": "producer"},
                {"user_id": str(peer.id), "role": role},
            ]
        },
        headers=delivered["ah"],
    )
    h = _h(client, f"peer_{role}")
    assert client.get(f"/api/kpi/projects/{delivered['pid']}", headers=h).status_code == 403
    assert client.get(f"/api/kpi/overview", headers=h).status_code == 403
    assert client.get(f"/api/kpi/series/{delivered['sid']}", headers=h).status_code == 403


def test_overview_aggregates_the_whole_app(client, delivered):
    """The tracker reports across every project, and a person appears once with
    their real total rather than one partial row per project."""
    out = client.get("/api/kpi/overview", headers=delivered["ah"]).json()
    assert out["totals"]["projects"] >= 1
    assert out["totals"]["episodes"] == 2
    assert out["totals"]["delivered"] == 2
    assert out["totals"]["completion_pct"] == 100.0
    rows = [p for p in out["people"] if p["name"] == "emp"]
    assert len(rows) == 1 and rows[0]["delivered"] == 2
    assert any(p["project_name"] == "MOGU" for p in out["projects"])


def test_overview_counts_a_person_once_across_projects(client, delivered):
    """Someone on two shows must not appear as two half-rows to be added by eye."""
    emp = delivered["emp"]
    other = client.post(
        "/api/projects", json={"name": "SECOND"}, headers=delivered["ah"]
    ).json()["id"]
    ep = client.post(
        f"/api/projects/{other}/scenes", json={"name": "EP01"}, headers=delivered["ah"]
    ).json()["id"]
    client.patch(
        f"/api/scenes/{ep}/assignee", json={"user_id": str(emp.id)}, headers=delivered["ah"]
    )

    out = client.get("/api/kpi/overview", headers=delivered["ah"]).json()
    rows = [p for p in out["people"] if p["name"] == "emp"]
    assert len(rows) == 1
    assert rows[0]["assigned"] == 3  # 2 from MOGU + 1 from SECOND


# ── export ─────────────────────────────────────────────────────────────────


def test_episode_export_carries_owner_status_and_cost(client, delivered):
    r = client.get(
        f"/api/export/projects/{delivered['pid']}/episodes", headers=delivered["ah"]
    )
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert "attachment" in r.headers["content-disposition"]
    rows = _rows(r)
    assert len(rows) == 2
    assert {x["episode"] for x in rows} == {"EP01", "EP02"}
    assert all(x["assignee"] == "emp" for x in rows)
    assert all(x["status"] == "approved" for x in rows)


def test_export_opens_cleanly_in_excel(client, delivered):
    """Vietnamese names come out as mojibake without the BOM."""
    r = client.get(
        f"/api/export/projects/{delivered['pid']}/episodes", headers=delivered["ah"]
    )
    assert r.text.startswith("﻿")


def test_submission_export_keeps_the_whole_back_and_forth(client, delivered):
    rows = _rows(
        client.get(
            f"/api/export/projects/{delivered['pid']}/submissions", headers=delivered["ah"]
        )
    )
    assert len(rows) == 3  # not just the final accepted cuts
    rejected = [x for x in rows if x["status"] == "rejected"]
    assert len(rejected) == 1
    assert rejected[0]["review_note"] == "lip sync off"  # the reason survives
    assert rejected[0]["reviewed_by"] == "boss"


def test_spend_export_shows_ceiling_against_actual(client, delivered):
    client.put(
        f"/api/budgets/scene/{delivered['eps'][0]}",
        json={"amount_usd": 50},
        headers=delivered["ah"],
    )
    rows = _rows(
        client.get(f"/api/export/projects/{delivered['pid']}/spend", headers=delivered["ah"])
    )
    capped = [x for x in rows if x["episode"] == "EP01"][0]
    assert capped["budget_usd"] == "50.0"
    assert capped["unlimited"] == "no"
    uncapped = [x for x in rows if x["episode"] == "EP02"][0]
    assert uncapped["unlimited"] == "yes"


def test_history_export_is_a_readable_trail(client, delivered):
    rows = _rows(
        client.get(
            f"/api/export/history/scene/{delivered['eps'][0]}", headers=delivered["ah"]
        )
    )
    assert rows
    assert {"when", "action", "who", "change"} <= set(rows[0])
    assert any(r["action"] == "episode.assignee" for r in rows)


def test_export_respects_the_visibility_scope(client, delivered):
    """An export must not become a way around diagram 4."""
    lead = user_service.create_user("lead2", "pw123456")
    client.put(
        f"/api/projects/{delivered['pid']}/members",
        json={
            "members": [
                {"user_id": str(delivered["pm"].id), "role": "producer"},
                {"user_id": str(lead.id), "role": "producer"},
            ]
        },
        headers=delivered["ah"],
    )
    # A producer sees everything, which is the baseline…
    assert len(_rows(client.get(
        f"/api/export/projects/{delivered['pid']}/episodes", headers=_h(client, "lead2")
    ))) == 2


def test_export_is_not_open_to_outsiders(client, delivered):
    user_service.create_user("nosy", "pw123456")
    r = client.get(
        f"/api/export/projects/{delivered['pid']}/episodes", headers=_h(client, "nosy")
    )
    assert r.status_code == 404
