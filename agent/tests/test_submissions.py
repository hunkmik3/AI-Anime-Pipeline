"""Phase 11 — deliverable submission + review.

Covers the three workflow rules: only the assignee submits, nobody reviews
their own work (approver chain falls through), and history is append-only.
"""
from __future__ import annotations

import pytest

from flowboard.services import submission_service as subs
from flowboard.services import user_service

DRIVE = "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUv/view?usp=sharing"


def _h(client, u, p="pw123456"):
    tok = client.post("/api/account/login", json={"username": u, "password": p}).json()
    return {"Authorization": f"Bearer {tok['token']}"}


@pytest.fixture()
def world(client):
    """Admin(BOD) + PM + Series Producer + Employee, one episode assigned."""
    user_service.create_user("boss", "pw123456", role="admin")
    ah = _h(client, "boss")
    pm = user_service.create_user("pm", "pw123456", display_name="PM Anh A")
    sp = user_service.create_user("sp", "pw123456", display_name="SP Chi B")
    emp = user_service.create_user("emp", "pw123456", display_name="Employee C")

    pid = client.post(
        "/api/projects", json={"name": "MOGU", "owner_user_id": str(pm.id)}, headers=ah
    ).json()["id"]
    client.put(
        f"/api/projects/{pid}/members",
        json={
            "members": [
                {"user_id": str(pm.id), "role": "producer"},
                {"user_id": str(sp.id), "role": "lead"},
                {"user_id": str(emp.id), "role": "artist"},
            ]
        },
        headers=ah,
    )
    sid = client.post(
        f"/api/projects/{pid}/series", json={"name": "S1", "code": "HUSB"}, headers=ah
    ).json()["id"]
    # PM names the Series Producer + assigns the episode to the employee
    assert client.patch(
        f"/api/series/{sid}/producer", json={"user_id": str(sp.id)}, headers=_h(client, "pm")
    ).status_code == 200
    ep = client.post(
        f"/api/projects/{pid}/scenes", json={"name": "EP1", "series_id": sid}, headers=ah
    ).json()["id"]
    # The series is the deliverable, so it is the series that is handed over.
    # The episode assignment stays too: it is what decides who works in EP1.
    assert client.patch(
        f"/api/scenes/{ep}/assignee", json={"user_id": str(emp.id)}, headers=_h(client, "pm")
    ).status_code == 200
    assert client.patch(
        f"/api/series/{sid}/assignee", json={"user_id": str(emp.id)}, headers=_h(client, "pm")
    ).status_code == 200

    return {
        "pid": pid, "sid": sid, "ep": ep,
        "ah": ah, "pm": pm, "sp": sp, "emp": emp,
        "h": {n: _h(client, n) for n in ("pm", "sp", "emp")},
    }


# ── Drive link parsing ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://drive.google.com/file/d/ABC123xyz789/view?usp=sharing", "ABC123xyz789"),
        ("https://drive.google.com/open?id=ABC123xyz789", "ABC123xyz789"),
        ("https://drive.google.com/uc?id=ABC123xyz789&export=download", "ABC123xyz789"),
        ("https://youtube.com/watch?v=abc", None),
        ("not a url", None),
        ("", None),
    ],
)
def test_parse_drive_file_id(url, expected):
    assert subs.parse_drive_file_id(url) == expected


def test_preview_url_is_embeddable():
    assert subs.drive_preview_url("XYZ") == "https://drive.google.com/file/d/XYZ/preview"


# ── Only the assignee submits ──────────────────────────────────────────────


def test_only_assignee_can_submit(client, world):
    w_sid = world["sid"]
    # the SP (not the assignee) cannot submit
    r = client.post(
        f"/api/series/{w_sid}/submissions", json={"drive_url": DRIVE}, headers=world["h"]["sp"]
    )
    assert r.status_code == 403
    # the assignee can
    ok = client.post(
        f"/api/series/{w_sid}/submissions", json={"drive_url": DRIVE}, headers=world["h"]["emp"]
    )
    assert ok.status_code == 200
    body = ok.json()
    assert body["version"] == 1 and body["status"] == "submitted"
    assert body["preview_url"].endswith("/preview")
    # chain routed it to the Series Producer
    assert body["approver_name"] == "SP Chi B"


def test_bad_drive_link_rejected(client, world):
    r = client.post(
        f"/api/series/{world['sid']}/submissions",
        json={"drive_url": "https://youtube.com/watch?v=x"},
        headers=world["h"]["emp"],
    )
    assert r.status_code == 400


# ── Review + history ───────────────────────────────────────────────────────


def _submit(client, world):
    return client.post(
        f"/api/series/{world['sid']}/submissions",
        json={"drive_url": DRIVE},
        headers=world["h"]["emp"],
    ).json()


def test_reject_returns_to_draft_and_keeps_history(client, world):
    v1 = _submit(client, world)
    r = client.post(
        f"/api/submissions/{v1['id']}/reject",
        json={"note": "âm thanh lệch ở 0:42"},
        headers=world["h"]["sp"],
    )
    assert r.status_code == 200 and r.json()["status"] == "rejected"

    detail = client.get(f"/api/series/{world['sid']}/submissions", headers=world["ah"]).json()
    assert detail["series"]["deliverable_status"] == "draft"  # back to draft
    assert detail["submissions"][0]["review_note"] == "âm thanh lệch ở 0:42"

    # resubmit → v2, v1 still on record
    v2 = _submit(client, world)
    assert v2["version"] == 2
    hist = client.get(f"/api/series/{world['sid']}/submissions", headers=world["ah"]).json()
    assert [s["version"] for s in hist["submissions"]] == [2, 1]
    assert [s["status"] for s in hist["submissions"]] == ["submitted", "rejected"]


def test_reject_requires_a_reason(client, world):
    v1 = _submit(client, world)
    r = client.post(f"/api/submissions/{v1['id']}/reject", json={}, headers=world["h"]["sp"])
    assert r.status_code == 400


def test_approve_locks_the_series(client, world):
    v1 = _submit(client, world)
    assert client.post(
        f"/api/submissions/{v1['id']}/approve", json={}, headers=world["h"]["sp"]
    ).status_code == 200
    detail = client.get(f"/api/series/{world['sid']}/submissions", headers=world["ah"]).json()
    assert detail["series"]["deliverable_status"] == "approved"
    # no further submissions once approved
    again = client.post(
        f"/api/series/{world['sid']}/submissions",
        json={"drive_url": DRIVE},
        headers=world["h"]["emp"],
    )
    assert again.status_code == 409


def test_cannot_review_twice(client, world):
    v1 = _submit(client, world)
    client.post(f"/api/submissions/{v1['id']}/approve", json={}, headers=world["h"]["sp"])
    r = client.post(f"/api/submissions/{v1['id']}/approve", json={}, headers=world["h"]["sp"])
    assert r.status_code == 409


# ── Approver chain: no self-review ─────────────────────────────────────────


def test_chain_skips_the_submitter(client, world):
    """When the Series Producer is also the assignee, review must fall through
    to the PM instead of letting them approve their own cut."""
    w_sid = world["sid"]
    # hand the SERIES to the SP themselves — they are its reviewer AND now the
    # person delivering it, which is the collision the chain exists for.
    assert client.patch(
        f"/api/series/{w_sid}/assignee",
        json={"user_id": str(world["sp"].id)},
        headers=world["h"]["pm"],
    ).status_code == 200
    body = client.post(
        f"/api/series/{w_sid}/submissions", json={"drive_url": DRIVE}, headers=world["h"]["sp"]
    ).json()
    assert body["approver_name"] == "PM Anh A"  # fell through to the PM
    # and the SP cannot review it
    assert client.post(
        f"/api/submissions/{body['id']}/approve", json={}, headers=world["h"]["sp"]
    ).status_code == 403


def test_employee_cannot_review(client, world):
    v1 = _submit(client, world)
    assert client.post(
        f"/api/submissions/{v1['id']}/approve", json={}, headers=world["h"]["emp"]
    ).status_code == 403


# ── Listings ───────────────────────────────────────────────────────────────


def test_my_work_lists_the_series_they_hand_in(client, world):
    """One row for the series, not one per episode. The page is a list of things
    to deliver, and the series is delivered once."""
    mine = client.get("/api/my/series", headers=world["h"]["emp"]).json()["series"]
    assert [e["id"] for e in mine] == [world["sid"]]
    assert mine[0]["episode_count"] == 1
    # the SP reviews this series; they do not hand it in
    assert client.get("/api/my/series", headers=world["h"]["sp"]).json()["series"] == []


def test_review_queue_routes_to_the_approver(client, world):
    _submit(client, world)
    sp_queue = client.get("/api/review/queue", headers=world["h"]["sp"]).json()["items"]
    assert len(sp_queue) == 1
    assert sp_queue[0]["series"]["assignee_name"] == "Employee C"
    # the employee sees nothing to review
    assert client.get("/api/review/queue", headers=world["h"]["emp"]).json()["items"] == []


def test_only_one_attempt_can_await_review(client, world):
    """`submitted` is left only by the approver, so the assignee cannot stack a
    second attempt on top of a pending one.

    Without this an artist could submit v2, v3, v4 while v1 waited, and the
    reviewer's queue showed several rows for one episode with nothing to say which
    one counted.
    """
    w_sid, eh = world["sid"], world["h"]["emp"]
    first = client.post(
        f"/api/series/{w_sid}/submissions", json={"drive_url": DRIVE}, headers=eh
    )
    assert first.status_code == 200

    again = client.post(
        f"/api/series/{w_sid}/submissions", json={"drive_url": DRIVE}, headers=eh
    )
    assert again.status_code == 409
    assert "waiting for review" in again.json()["detail"]

    # Sending it back reopens the episode, and only then does a v2 land.
    sid = first.json()["id"]
    client.post(
        f"/api/submissions/{sid}/reject",
        json={"note": "grade is off"},
        headers=world["ah"],
    )
    retry = client.post(
        f"/api/series/{w_sid}/submissions", json={"drive_url": DRIVE}, headers=eh
    )
    assert retry.status_code == 200
    assert retry.json()["version"] == 2


def test_the_review_queue_holds_one_row_per_series(client, world):
    """The consequence the guard above protects: a reviewer's inbox must never show
    the same series twice, because only one attempt can be open at a time."""
    w_sid, eh = world["sid"], world["h"]["emp"]
    client.post(f"/api/series/{w_sid}/submissions", json={"drive_url": DRIVE}, headers=eh)
    # A refused second attempt must not add a row either.
    client.post(f"/api/series/{w_sid}/submissions", json={"drive_url": DRIVE}, headers=eh)

    items = client.get("/api/review/queue", headers=world["ah"]).json()["items"]
    ids = [i["submission"]["series_id"] for i in items]
    assert ids, "the queue should hold the one open attempt"
    assert len(ids) == len(set(ids)), "one series appeared twice"


def test_a_folder_link_says_so(client, world):
    """People paste where they *put* the cut, not the cut. Repeating the required
    format doesn't help someone who thinks they already followed it."""
    r = client.post(
        f"/api/series/{world['sid']}/submissions",
        json={"drive_url": "https://drive.google.com/drive/folders/1o77ivF7hqQNRJz2o"},
        headers=world["h"]["emp"],
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "folder" in detail and "Copy link" in detail
