"""Self-service signup → admin approval → provisioned account + email.

Signup is open to any email; admin approval is the only gate. The public
endpoint must never reveal whether an email already has an account, and the
approval must survive a failing mail server.
"""
from __future__ import annotations

import pytest

from flowboard.services import email_service, registration_service, user_service


@pytest.fixture(autouse=True)
def _no_real_smtp(monkeypatch):
    """Never touch a real mail server in tests. Records what would be sent."""
    sent: list[dict] = []

    def fake_send(to, subject, body):
        sent.append({"to": to, "subject": subject, "body": body})
        return True

    monkeypatch.setattr(email_service, "send", fake_send)
    return sent


@pytest.fixture
def outbox(_no_real_smtp):
    return _no_real_smtp


def _login(client, u, p):
    return client.post("/api/account/login", json={"username": u, "password": p})


def _admin_h(client):
    user_service.create_user("root", "adminpw1", role="admin")
    return {"Authorization": f"Bearer {_login(client, 'root', 'adminpw1').json()['token']}"}


# ── public signup ────────────────────────────────────────────────────────────


def test_register_queues_a_pending_request(client):
    r = client.post(
        "/api/account/register",
        json={"email": "New.Person@Gmail.com", "display_name": "New Person"},
    )
    assert r.status_code == 200 and r.json()["ok"] is True

    rows = registration_service.list_registrations("pending")
    assert len(rows) == 1
    assert rows[0]["email"] == "new.person@gmail.com"  # normalized
    assert rows[0]["display_name"] == "New Person"


def test_register_rejects_malformed_email(client):
    r = client.post("/api/account/register", json={"email": "not-an-email"})
    assert r.status_code == 400
    assert registration_service.list_registrations("pending") == []


def test_register_is_idempotent_and_does_not_enumerate(client):
    """Signing up twice, or for an email that already has an account, returns
    the SAME response and never stacks duplicates in the admin's queue."""
    first = client.post("/api/account/register", json={"email": "dup@x.com"})
    second = client.post("/api/account/register", json={"email": "dup@x.com"})
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert len(registration_service.list_registrations("pending")) == 1

    # an email that already owns an account → same answer, still no new row
    user_service.create_user("taken", "takenpw12", email="taken@x.com")
    r = client.post("/api/account/register", json={"email": "taken@x.com"})
    assert r.status_code == 200 and r.json() == first.json()
    assert len(registration_service.list_registrations("pending")) == 1


def test_register_is_public_without_a_token(client):
    assert client.post("/api/account/register", json={"email": "anon@x.com"}).status_code == 200


# ── admin approval ───────────────────────────────────────────────────────────


def test_approve_creates_account_and_emails_credentials(client, outbox):
    h = _admin_h(client)
    client.post(
        "/api/account/register", json={"email": "maker@x.com", "display_name": "Maker"}
    )
    reg = client.get("/api/admin/registrations?status=pending", headers=h).json()[0]

    r = client.post(
        f"/api/admin/registrations/{reg['id']}/approve", json={"budget_usd": 50}, headers=h
    )
    assert r.status_code == 200
    res = r.json()
    assert res["username"] == "maker@x.com"    # the username IS their email
    assert res["email_sent"] is True
    assert len(res["temp_password"]) >= 8

    # the account is real, must change the temp password, and carries the budget
    u = user_service.get_by_username("maker@x.com")
    assert u is not None and u.email == "maker@x.com"
    assert u.must_change_password is True and u.status == "active"
    assert u.budget_usd == 50

    # the mail actually carries the credentials
    assert len(outbox) == 1
    assert outbox[0]["to"] == "maker@x.com"
    assert "maker@x.com" in outbox[0]["body"] and res["temp_password"] in outbox[0]["body"]

    # request is closed and no longer pending
    assert registration_service.list_registrations("pending") == []


def test_approved_temp_password_logs_in_and_forces_a_change(client):
    h = _admin_h(client)
    client.post("/api/account/register", json={"email": "first@x.com"})
    reg = client.get("/api/admin/registrations?status=pending", headers=h).json()[0]
    res = client.post(
        f"/api/admin/registrations/{reg['id']}/approve", json={}, headers=h
    ).json()

    login = _login(client, res["username"], res["temp_password"])
    assert login.status_code == 200
    assert login.json()["user"]["must_change_password"] is True


def test_approve_survives_a_failing_mail_server(client, monkeypatch):
    """Mail is best-effort: a dead SMTP must NOT lose the approved account."""
    h = _admin_h(client)
    monkeypatch.setattr(email_service, "send", lambda *a, **k: False)
    client.post("/api/account/register", json={"email": "bounce@x.com"})
    reg = client.get("/api/admin/registrations?status=pending", headers=h).json()[0]

    res = client.post(
        f"/api/admin/registrations/{reg['id']}/approve", json={}, headers=h
    ).json()
    assert res["email_sent"] is False           # admin is told to relay by hand
    assert res["temp_password"]                 # ...and given the password
    assert user_service.get_by_username("bounce@x.com") is not None


def test_username_is_the_email_verbatim(client):
    """No derivation/uniquifying: the account name is exactly what they typed,
    so the temp password mail and the login field agree."""
    h = _admin_h(client)
    client.post("/api/account/register", json={"email": "First.Last+tag@Studio.com"})
    reg = client.get("/api/admin/registrations?status=pending", headers=h).json()[0]
    res = client.post(
        f"/api/admin/registrations/{reg['id']}/approve", json={}, headers=h
    ).json()
    assert res["username"] == "first.last+tag@studio.com"   # normalized to lowercase
    assert user_service.get_by_username("first.last+tag@studio.com") is not None


def test_signup_ignored_when_that_username_already_exists(client):
    """An admin-made account named after the email must not be duplicated."""
    user_service.create_user("clash@x.com", "clashpw12")
    client.post("/api/account/register", json={"email": "clash@x.com"})
    assert registration_service.list_registrations("pending") == []


def test_reject_closes_the_request_without_an_account(client):
    h = _admin_h(client)
    client.post("/api/account/register", json={"email": "nope@x.com"})
    reg = client.get("/api/admin/registrations?status=pending", headers=h).json()[0]

    assert client.post(
        f"/api/admin/registrations/{reg['id']}/reject", json={}, headers=h
    ).status_code == 200
    assert user_service.get_by_username("nope@x.com") is None
    assert registration_service.list_registrations("pending") == []
    assert registration_service.list_registrations("rejected")[0]["email"] == "nope@x.com"


def test_cannot_decide_the_same_request_twice(client):
    h = _admin_h(client)
    client.post("/api/account/register", json={"email": "once@x.com"})
    reg = client.get("/api/admin/registrations?status=pending", headers=h).json()[0]
    client.post(f"/api/admin/registrations/{reg['id']}/approve", json={}, headers=h)

    again = client.post(f"/api/admin/registrations/{reg['id']}/approve", json={}, headers=h)
    assert again.status_code == 400 and "already" in again.json()["detail"]


def test_rejected_applicant_can_apply_again(client):
    h = _admin_h(client)
    client.post("/api/account/register", json={"email": "retry@x.com"})
    reg = client.get("/api/admin/registrations?status=pending", headers=h).json()[0]
    client.post(f"/api/admin/registrations/{reg['id']}/reject", json={}, headers=h)

    client.post("/api/account/register", json={"email": "retry@x.com"})
    assert len(registration_service.list_registrations("pending")) == 1


def test_pending_count_for_the_badge(client):
    h = _admin_h(client)
    for e in ("a@x.com", "b@x.com"):
        client.post("/api/account/register", json={"email": e})
    assert client.get("/api/admin/registrations/pending-count", headers=h).json()["count"] == 2


def test_registration_queue_is_admin_only(client):
    user_service.create_user("plain", "plainpw1")
    h = {"Authorization": f"Bearer {_login(client, 'plain', 'plainpw1').json()['token']}"}
    assert client.get("/api/admin/registrations", headers=h).status_code == 403
