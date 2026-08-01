"""Change history for production objects.

The studio moved off Google Sheets because edits overwrote each other and left no
trace. These tests hold that line: every management decision that used to be a
silent cell edit — reassignment, a budget change, a rename, a deletion — has to
end up on a readable record, attributed to whoever made it.
"""
from __future__ import annotations

from flowboard.services import audit_service, user_service


def _h(client, u, p="pw123456"):
    tok = client.post("/api/account/login", json={"username": u, "password": p}).json()
    return {"Authorization": f"Bearer {tok['token']}"}


# ── the formatter ──────────────────────────────────────────────────────────


def test_no_op_edits_are_not_recorded():
    """A trail full of "changed nothing" entries is a trail nobody reads."""
    audit_service.record_change(
        "series.updated", object_type="series", object_id="x", changes={"name": ("A", "A")}
    )
    assert audit_service.history_for("series", "x") == []


def test_empty_and_unset_are_distinguishable():
    """"" and None mean different things to a producer reading the log."""
    assert audit_service._fmt(None) == "(none)"
    assert audit_service._fmt("") == "(empty)"
    assert audit_service._fmt("  ") == "(empty)"
    assert audit_service._fmt(True) == "yes"
    assert audit_service._fmt(3.0) == "3"


def test_long_values_are_truncated_not_dropped():
    out = audit_service._fmt("x" * 400)
    assert out.endswith("…") and len(out) <= 121


# ── the real flows ─────────────────────────────────────────────────────────


def test_reassigning_an_episode_records_who_did_it(client):
    """"Who was on Ep03 last week" — the question a Sheet could never answer."""
    user_service.create_user("boss", "pw123456", role="admin")
    ah = _h(client, "boss")
    a = user_service.create_user("artist_a", "pw123456")
    b = user_service.create_user("artist_b", "pw123456")

    pid = client.post("/api/projects", json={"name": "MOGU"}, headers=ah).json()["id"]
    ep = client.post(
        f"/api/projects/{pid}/scenes", json={"name": "EP03"}, headers=ah
    ).json()["id"]

    client.patch(f"/api/scenes/{ep}/assignee", json={"user_id": str(a.id)}, headers=ah)
    client.patch(f"/api/scenes/{ep}/assignee", json={"user_id": str(b.id)}, headers=ah)

    r = client.get(f"/api/history/scene/{ep}", headers=ah)
    assert r.status_code == 200
    entries = r.json()["entries"]
    moves = [e for e in entries if e["action"] == "episode.assignee"]
    assert len(moves) == 2
    # Newest first, and each names both ends of the move plus who made it.
    assert "artist_a" in moves[0]["detail"] and "artist_b" in moves[0]["detail"]
    assert moves[0]["actor"] == "boss"
    assert "(none)" in moves[1]["detail"]  # first assignment came from nothing


def test_budget_change_is_recorded_with_the_amounts(client):
    """Setting a ceiling directly bypasses request+approve, so it must be logged
    — otherwise it is the one unaudited way to authorise spend."""
    user_service.create_user("boss", "pw123456", role="admin")
    ah = _h(client, "boss")
    pid = client.post("/api/projects", json={"name": "MOGU"}, headers=ah).json()["id"]

    client.put(f"/api/budgets/project/{pid}", json={"amount_usd": 500}, headers=ah)
    client.put(f"/api/budgets/project/{pid}", json={"amount_usd": 900}, headers=ah)

    entries = client.get(f"/api/history/project/{pid}", headers=ah).json()["entries"]
    sets = [e for e in entries if e["action"] == "budget.set"]
    assert len(sets) == 2
    assert "$500" in sets[0]["detail"] and "$900" in sets[0]["detail"]
    # 0 means "no ceiling" — it must not read as a budget of zero.
    client.put(f"/api/budgets/project/{pid}", json={"amount_usd": 0}, headers=ah)
    latest = client.get(f"/api/history/project/{pid}", headers=ah).json()["entries"][0]
    assert "unlimited" in latest["detail"]


def test_grant_request_and_verdict_land_on_the_series_timeline(client):
    user_service.create_user("boss", "pw123456", role="admin")
    ah = _h(client, "boss")
    pid = client.post("/api/projects", json={"name": "MOGU"}, headers=ah).json()["id"]
    sid = client.post(
        f"/api/projects/{pid}/series", json={"name": "S1", "code": "S1"}, headers=ah
    ).json()["id"]
    client.put(f"/api/budgets/series/{sid}", json={"amount_usd": 100}, headers=ah)

    g = client.post(
        f"/api/budgets/series/{sid}/grants",
        json={"amount_usd": 50, "reason": "extra retakes on EP02"},
        headers=ah,
    ).json()["grant"]
    client.post(
        f"/api/budgets/requests/{g['id']}/approve", json={"note": "ok"}, headers=ah
    )

    actions = [
        e["action"] for e in client.get(f"/api/history/series/{sid}", headers=ah).json()["entries"]
    ]
    assert "budget.grant_requested" in actions
    assert "budget.grant_approved" in actions


def test_crm_field_edits_are_recorded(client):
    """The ~27 CRM fields sit in a JSONB bag; without this they would overwrite
    silently — the same failure mode as the spreadsheet."""
    user_service.create_user("boss", "pw123456", role="admin")
    ah = _h(client, "boss")
    pid = client.post("/api/projects", json={"name": "MOGU"}, headers=ah).json()["id"]
    sid = client.post(
        f"/api/projects/{pid}/series",
        json={"name": "S1", "code": "S1", "production": {"tier": "A"}},
        headers=ah,
    ).json()["id"]

    client.patch(
        f"/api/series/{sid}", json={"production": {"tier": "S", "status": "in_production"}},
        headers=ah,
    )
    detail = " ".join(
        e["detail"] or ""
        for e in client.get(f"/api/history/series/{sid}", headers=ah).json()["entries"]
    )
    assert "production.tier" in detail and "A" in detail and "S" in detail


def test_deleting_an_episode_leaves_a_mark(client):
    """The most destructive action in the app — it takes generated work with it."""
    user_service.create_user("boss", "pw123456", role="admin")
    ah = _h(client, "boss")
    pid = client.post("/api/projects", json={"name": "MOGU"}, headers=ah).json()["id"]
    ep = client.post(
        f"/api/projects/{pid}/scenes", json={"name": "EP09"}, headers=ah
    ).json()["id"]
    client.delete(f"/api/scenes/{ep}", headers=ah)

    # The object is gone, but its record is not.
    entries = audit_service.history_for("scene", ep)
    assert [e["action"] for e in entries if e["action"] == "episode.deleted"]


def test_roster_change_names_who_joined_and_who_changed_role(client):
    user_service.create_user("boss", "pw123456", role="admin")
    ah = _h(client, "boss")
    emp = user_service.create_user("emp", "pw123456")
    pid = client.post("/api/projects", json={"name": "MOGU"}, headers=ah).json()["id"]

    boss = user_service.get_by_username("boss")
    roster_of = lambda role: {  # noqa: E731 - keeps the two saves readable
        "members": [
            {"user_id": str(boss.id), "role": "producer"},
            {"user_id": str(emp.id), "role": role},
        ]
    }
    client.put(f"/api/projects/{pid}/members", json=roster_of("artist"), headers=ah)
    client.put(f"/api/projects/{pid}/members", json=roster_of("lead"), headers=ah)

    entries = client.get(f"/api/history/project/{pid}", headers=ah).json()["entries"]
    roster = [e for e in entries if e["action"] == "project.members"]
    assert roster and "artist" in roster[0]["detail"] and "lead" in roster[0]["detail"]


def test_editing_the_roster_never_transfers_ownership(client):
    """Dropping the PM from the roster used to promote whoever was first — an
    artist would come out of it holding producer rights (member.manage,
    series.delete). Ownership moves only through the explicit admin-only
    project update."""
    user_service.create_user("boss", "pw123456", role="admin")
    ah = _h(client, "boss")
    pm = user_service.create_user("pm", "pw123456")
    artist = user_service.create_user("artist", "pw123456")

    # BOD creates the project and sets the PM as owner, per the workflow.
    proj = client.post(
        "/api/projects", json={"name": "MOGU", "owner_user_id": str(pm.id)}, headers=ah
    ).json()
    assert proj["owner_user_id"] == str(pm.id)

    # A roster save that leaves the PM out must not hand the project over.
    out = client.put(
        f"/api/projects/{proj['id']}/members",
        json={"members": [{"user_id": str(artist.id), "role": "artist"}]},
        headers=ah,
    ).json()

    owner_rows = [m for m in out["members"] if m["is_owner"]]
    assert owner_rows and owner_rows[0]["user_id"] == str(pm.id)
    artist_row = [m for m in out["members"] if m["user_id"] == str(artist.id)][0]
    assert artist_row["role"] == "artist" and artist_row["is_owner"] is False


def test_a_project_with_no_owner_yet_adopts_one_from_the_roster(client):
    """An admin may create a project before deciding who runs it; the first
    roster save is then allowed to establish the owner."""
    user_service.create_user("boss", "pw123456", role="admin")
    ah = _h(client, "boss")
    pm = user_service.create_user("pm", "pw123456")
    proj = client.post("/api/projects", json={"name": "MOGU"}, headers=ah).json()
    assert proj["owner_user_id"] is None

    out = client.put(
        f"/api/projects/{proj['id']}/members",
        json={"members": [{"user_id": str(pm.id), "role": "producer"}]},
        headers=ah,
    ).json()
    assert [m for m in out["members"] if m["is_owner"]][0]["user_id"] == str(pm.id)


# ── access ─────────────────────────────────────────────────────────────────


def test_history_is_not_readable_by_outsiders(client):
    user_service.create_user("boss", "pw123456", role="admin")
    ah = _h(client, "boss")
    user_service.create_user("nosy", "pw123456")
    pid = client.post("/api/projects", json={"name": "MOGU"}, headers=ah).json()["id"]

    r = client.get(f"/api/history/project/{pid}", headers=_h(client, "nosy"))
    assert r.status_code == 404  # 404 not 403 — don't confirm the id exists


def test_unknown_object_type_is_rejected(client):
    user_service.create_user("boss", "pw123456", role="admin")
    import uuid as _uuid

    r = client.get(f"/api/history/wombat/{_uuid.uuid4()}", headers=_h(client, "boss"))
    assert r.status_code == 400
