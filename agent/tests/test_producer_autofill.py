"""Producer is recorded from the series creator (no second declaration)."""
from flowboard.services import user_service


def _h(client, u, p="pw123456"):
    return {"Authorization": f"Bearer {client.post('/api/account/login', json={'username': u, 'password': p}).json()['token']}"}


def test_producer_autofilled_from_creator(client):
    user_service.create_user("boss", "pw123456", role="admin")
    ah = _h(client, "boss")
    prod = user_service.create_user("raymond", "pw123456", display_name="Raymond PM")
    pid = client.post("/api/projects", json={"name": "MOGU", "owner_user_id": str(prod.id)}, headers=ah).json()["id"]

    # the producer creates a series and does NOT send a producer field
    ph = _h(client, "raymond")
    s = client.post(f"/api/projects/{pid}/series", json={"name": "S1", "code": "S1"}, headers=ph).json()
    assert s["production"]["producer"] == "Raymond PM"


def test_explicit_producer_still_wins(client):
    user_service.create_user("boss", "pw123456", role="admin")
    ah = _h(client, "boss")
    pid = client.post("/api/projects", json={"name": "MOGU"}, headers=ah).json()["id"]
    s = client.post(
        f"/api/projects/{pid}/series",
        json={"name": "S", "production": {"producer": "Someone Else"}},
        headers=ah,
    ).json()
    assert s["production"]["producer"] == "Someone Else"
