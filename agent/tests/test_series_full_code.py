"""Phase 10 CRM: series full_code is auto-generated like the Series_Master sheet
formula — <BRAND>_<YY><NNN>_<CODE>_<PascalName>, with NNN a per-project,
per-year running number."""
from __future__ import annotations


def _series(client, pid, **body):
    return client.post(f"/api/projects/{pid}/series", json=body).json()


def test_full_code_matches_sheet_format(client):
    pid = client.post("/api/projects", json={"name": "MOGU"}).json()["id"]

    a = _series(
        client,
        pid,
        name="Husband Died 100 Times",
        code="HUSB",
        production={"start_date": "2026-05-19"},
    )
    assert a["production"]["full_code"] == "MOGU_26001_HUSB_HusbandDied100Times"

    b = _series(
        client,
        pid,
        name="Only One Of Us Can Live",
        code="LUCI",
        production={"start_date": "2026-06-03"},
    )
    # second 2026 series → running number 002
    assert b["production"]["full_code"] == "MOGU_26002_LUCI_OnlyOneOfUsCanLive"


def test_running_number_resets_per_year(client):
    pid = client.post("/api/projects", json={"name": "MOGU"}).json()["id"]
    _series(client, pid, name="A", code="AA", production={"start_date": "2026-01-01"})
    c = _series(client, pid, name="B", code="BB", production={"start_date": "2027-01-01"})
    assert c["production"]["full_code"] == "MOGU_27001_BB_B"


def test_diacritics_stripped_and_brand_from_project(client):
    pid = client.post("/api/projects", json={"name": "Sleepy Giant"}).json()["id"]
    s = _series(
        client, pid, name="Trường Học Bí Ẩn", code="tha", production={"start_date": "2026-02-02"}
    )
    # brand from project name (ASCII, upper, no spaces); name → PascalCase ASCII
    assert s["production"]["full_code"] == "SLEEPYGIANT_26001_THA_TruongHocBiAn"


def test_missing_start_date_falls_back_to_current_year(client):
    pid = client.post("/api/projects", json={"name": "MOGU"}).json()["id"]
    s = _series(client, pid, name="No Date", code="ND")
    # shape is right even without a date (YY is the current year, 001 running no.)
    fc = s["production"]["full_code"]
    assert fc.startswith("MOGU_") and fc.endswith("_ND_NoDate")
    assert len(fc.split("_")[1]) == 5  # YY + NNN


def test_full_code_regenerates_when_name_changes(client):
    pid = client.post("/api/projects", json={"name": "MOGU"}).json()["id"]
    s = _series(client, pid, name="Old Name", code="ON", production={"start_date": "2026-03-03"})
    assert s["production"]["full_code"] == "MOGU_26001_ON_OldName"
    upd = client.patch(f"/api/series/{s['id']}", json={"name": "New Title"}).json()
    assert upd["production"]["full_code"] == "MOGU_26001_ON_NewTitle"
