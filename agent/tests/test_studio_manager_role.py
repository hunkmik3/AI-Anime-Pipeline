"""A third system role: the studio manager.

There were two — ``admin`` and ``user`` — and ``project.manage`` was admin-only,
so one person was the sole route to a new project. That is a bottleneck the
moment two branches run at once, and the fix is a second staff role rather than
handing out the one that also controls the money.

The whole role is defined by where it STOPS, so that is what most of these test:

    manager can    open projects, lay them out, provision the people who work
    manager cannot touch a budget, or make anyone an admin

The second is the one that makes the split real rather than decorative — anyone
who can create an admin has every right an admin has, one step removed.
"""
from __future__ import annotations

import pytest

from flowboard.services import user_service


def _h(client, u, p="pw123456"):
    r = client.post("/api/account/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture()
def staff(client):
    user_service.create_user("sm_owner", "pw123456", role="admin")
    user_service.create_user("sm_mgr", "pw123456", role="manager")
    worker = user_service.create_user("sm_worker", "pw123456")
    return {
        "owner": _h(client, "sm_owner"),
        "mgr": _h(client, "sm_mgr"),
        "worker": _h(client, "sm_worker"),
        "worker_id": str(worker.id),
    }


# ── what the role is for ────────────────────────────────────────────────────


def test_a_manager_opens_a_project_and_lays_it_out(client, staff):
    """The bottleneck this role exists to remove."""
    proj = client.post("/api/projects", json={"name": "Branch two"}, headers=staff["mgr"])
    assert proj.status_code == 200, proj.text
    pid = proj.json()["id"]

    series = client.post(
        f"/api/projects/{pid}/series", json={"name": "S1"}, headers=staff["mgr"]
    )
    assert series.status_code == 200, series.text
    ep = client.post(
        f"/api/projects/{pid}/scenes", json={"name": "EP1"}, headers=staff["mgr"]
    )
    assert ep.status_code == 200, ep.text


def test_a_manager_provisions_the_people_who_do_the_work(client, staff):
    r = client.post(
        "/api/admin/users",
        json={"username": "sm_new", "password": "pw123456", "role": "user"},
        headers=staff["mgr"],
    )
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "user"


def test_a_manager_sees_every_project_not_just_their_own(client, staff):
    """Unscoped inside the studio, like an admin — a manager who could open a
    project but not then see it is not running anything."""
    client.post("/api/projects", json={"name": "Owner's"}, headers=staff["owner"])
    names = {p["name"] for p in client.get("/api/projects", headers=staff["mgr"]).json()}
    assert "Owner's" in names


# ── where it stops ──────────────────────────────────────────────────────────


def test_a_manager_cannot_make_someone_an_admin(client, staff):
    """The one that makes the split real. Anyone who can create an admin has
    every right an admin has, one step removed."""
    r = client.post(
        "/api/admin/users",
        json={"username": "sm_sneaky", "password": "pw123456", "role": "admin"},
        headers=staff["mgr"],
    )
    assert r.status_code == 403, r.text

    # …nor promote an existing account.
    r = client.patch(
        f"/api/admin/users/{staff['worker_id']}", json={"role": "admin"}, headers=staff["mgr"]
    )
    assert r.status_code == 403, r.text

    # And the account really is not an admin.
    assert user_service.get_by_username("sm_worker").role == "user"


def test_a_manager_cannot_change_a_budget(client, staff):
    r = client.patch(
        f"/api/admin/users/{staff['worker_id']}",
        json={"budget_usd": 500},
        headers=staff["mgr"],
    )
    assert r.status_code == 403, r.text

    r = client.patch(
        f"/api/admin/users/{staff['worker_id']}",
        json={"add_budget_usd": 50},
        headers=staff["mgr"],
    )
    assert r.status_code == 403, r.text


def test_a_manager_cannot_touch_the_shared_pool(client, staff):
    assert client.patch(
        "/api/admin/pool", json={"add_usd": 100}, headers=staff["mgr"]
    ).status_code == 403


def test_the_budget_guard_does_not_block_the_harmless_fields_beside_it(client, staff):
    """Budget shares an endpoint with a dozen ordinary settings. Refusing the
    whole request when no budget was named would take the role's actual job with
    it."""
    r = client.patch(
        f"/api/admin/users/{staff['worker_id']}",
        json={"display_name": "Renamed"},
        headers=staff["mgr"],
    )
    assert r.status_code == 200, r.text


# ── and the owner still can ─────────────────────────────────────────────────


def test_an_owner_keeps_everything(client, staff):
    assert client.patch(
        f"/api/admin/users/{staff['worker_id']}",
        json={"budget_usd": 500},
        headers=staff["owner"],
    ).status_code == 200
    assert client.post(
        "/api/admin/users",
        json={"username": "sm_admin2", "password": "pw123456", "role": "admin"},
        headers=staff["owner"],
    ).status_code == 200


# ── and a plain member still cannot ─────────────────────────────────────────


def test_a_member_is_unchanged_by_any_of_this(client, staff):
    """The new role must not have widened the old one on its way in."""
    assert client.post(
        "/api/projects", json={"name": "Nope"}, headers=staff["worker"]
    ).status_code == 403
    assert client.get("/api/admin/users", headers=staff["worker"]).status_code == 403


# ── the seal that must not drift ────────────────────────────────────────────


def test_giantflow_and_giantstudio_agree_on_who_is_staff():
    """`flow_permissions` is deliberately sealed from `permissions` — its own
    module, its own table, so a change to one cannot quietly widen the other.
    The cost of that seal is a duplicated list of system roles, and a duplicate
    that drifts is worse than no seal: a studio manager unscoped on one side and
    a plain viewer on the other is one person with two jobs depending on which
    tab they opened."""
    from flowboard.routes.deps import STAFF_ROLES
    from flowboard.services.flow_permissions import STAFF_SYSTEM_ROLES

    assert set(STAFF_ROLES) == set(STAFF_SYSTEM_ROLES)


def test_a_manager_runs_giantflow_too(client, staff):
    """Every comic, not the viewer's read-only crumbs."""
    me = client.get("/api/flowstudio/me", headers=staff["mgr"]).json()
    assert me["true_role"] == "admin"
    assert me["capabilities"]["panel.review"] is True
    assert me["capabilities"]["batch.manage"] is True
