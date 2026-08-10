"""Phase 11.3 — the Drive proxy that lets reviewers watch a Restricted file.

Only the pure logic + the route's guards are covered here; the suite never talks
to Google (conftest points the credential paths at nothing, so
``drive.is_configured()`` is False — same as CI).
"""
from __future__ import annotations

import pytest

from flowboard.services import drive
from flowboard.services import user_service

DRIVE = "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUv/view"


def _h(client, u, p="pw123456"):
    tok = client.post("/api/account/login", json={"username": u, "password": p}).json()
    return {"Authorization": f"Bearer {tok['token']}"}


def test_suite_is_isolated_from_real_drive():
    """Guard rail: a dev machine has real credentials in agent/ — the tests must
    never pick them up, or they'd hit Google with fixture ids."""
    assert drive.is_configured() is False


def test_scopes_are_read_only():
    """The robot account must never be able to change studio files."""
    assert drive.SCOPES == ["https://www.googleapis.com/auth/drive.readonly"]


def test_access_token_without_credentials_is_a_clear_error():
    with pytest.raises(drive.DriveError) as exc:
        drive.access_token(force=True)
    assert exc.value.code in ("not_configured", "not_authorized")


# ── the route ──────────────────────────────────────────────────────────────


@pytest.fixture()
def submitted(client):
    """A series with one submission, ready to stream."""
    user_service.create_user("boss", "pw123456", role="admin")
    ah = _h(client, "boss")
    emp = user_service.create_user("emp", "pw123456")
    outsider = user_service.create_user("nosy", "pw123456")

    pid = client.post("/api/projects", json={"name": "MOGU"}, headers=ah).json()["id"]
    sid = client.post(
        f"/api/projects/{pid}/series", json={"name": "S1", "code": "S1"}, headers=ah
    ).json()["id"]
    ep = client.post(
        f"/api/projects/{pid}/scenes", json={"name": "EP1", "series_id": sid}, headers=ah
    ).json()["id"]
    client.patch(f"/api/scenes/{ep}/assignee", json={"user_id": str(emp.id)}, headers=ah)
    # The series is what gets handed in, so it is what carries the delivered file.
    client.patch(f"/api/series/{sid}/assignee", json={"user_id": str(emp.id)}, headers=ah)
    sub = client.post(
        f"/api/series/{sid}/submissions", json={"drive_url": DRIVE}, headers=_h(client, "emp")
    ).json()
    return {"sub": sub, "ah": ah, "emp": emp, "outsider": outsider}


def test_submission_exposes_a_proxy_url(submitted):
    """The reviewer plays OUR url, not Drive's — that's what keeps the file
    Restricted and the reviewer Google-free."""
    s = submitted["sub"]
    assert s["stream_url"] == f"/api/submissions/{s['id']}/video"


def test_stream_requires_project_access(client, submitted):
    """Someone with no role on the project can't watch the cut, even with the id."""
    r = client.get(
        f"/api/submissions/{submitted['sub']['id']}/video", headers=_h(client, "nosy")
    )
    assert r.status_code == 404  # 404, not 403 — don't leak that it exists


def test_stream_unknown_submission_404(client, submitted):
    import uuid as _uuid

    r = client.get(f"/api/submissions/{_uuid.uuid4()}/video", headers=submitted["ah"])
    assert r.status_code == 404


def test_stream_reports_drive_not_configured(client, submitted):
    """With no Drive identity the route must fail as a *configuration* problem
    (501/503) with an actionable message — never a bare 500."""
    r = client.get(f"/api/submissions/{submitted['sub']['id']}/video", headers=submitted["ah"])
    assert r.status_code in (501, 503)
    detail = r.json()["detail"].lower()
    assert "oauth client" in detail or "not authorized" in detail
