from datetime import datetime, timedelta, timezone

import pytest

from flowboard.db import get_session
from flowboard.db.models import AutomationProject
from flowboard.routes.automation import _summary


@pytest.mark.parametrize("timestamp", [
    datetime(2026, 10, 7, 3, 37),
    datetime(2026, 10, 7, 3, 37, tzinfo=timezone.utc),
    datetime(2026, 10, 7, 10, 37, tzinfo=timezone(timedelta(hours=7))),
])
def test_summary_emits_explicit_utc_without_mutating_timestamp(timestamp):
    project = AutomationProject(name="Timezone check", updated_at=timestamp)
    assert _summary(project)["updated_at"] == "2026-10-07T03:37:00Z"
    assert project.updated_at == timestamp


def test_project_crud_emits_timezone_after_database_roundtrip(client):
    created = client.post("/api/automation/projects", json={"name": "Timezone check"})
    assert created.status_code == 200
    assert created.json()["updated_at"].endswith("Z")
    project_id = created.json()["id"]
    # Model the historic rows that come back from Postgres with no tzinfo.
    with get_session() as session:
        from uuid import UUID
        project = session.get(AutomationProject, UUID(project_id))
        project.updated_at = datetime(2026, 10, 5, 23, 30)
        session.add(project)
        session.commit()
    listed = client.get("/api/automation/projects")
    assert listed.status_code == 200
    row = next(p for p in listed.json() if p["id"] == project_id)
    assert row["updated_at"] == "2026-10-05T23:30:00Z"
    fetched = client.get(f"/api/automation/projects/{project_id}")
    assert fetched.json()["updated_at"] == row["updated_at"]
    saved = client.patch(f"/api/automation/projects/{project_id}", json={"name": "Updated title"})
    assert saved.status_code == 200
    assert saved.json()["updated_at"].endswith("Z")
