"""Tests for POST /api/requests and GET /api/requests/:id, plus the worker."""
import asyncio

import pytest

from flowboard.worker.processor import WorkerController


from tests.conftest import make_shot as _board  # noqa: F401


def test_create_request_persists_and_returns_row(client):
    b = _board(client)
    n = client.post("/api/nodes", json={"shot_id": b["id"], "type": "image"}).json()

    r = client.post(
        "/api/requests",
        json={
            "node_id": n["id"],
            "type": "proxy",
            "params": {"url": "https://aisandbox-pa.googleapis.com/v1/ping"},
        },
    )
    assert r.status_code == 200
    row = r.json()
    assert row["type"] == "proxy"
    assert row["status"] == "queued"
    assert row["node_id"] == n["id"]
    assert "id" in row


def test_create_request_with_missing_node_returns_404(client):
    r = client.post(
        "/api/requests",
        json={"node_id": 9999, "type": "proxy", "params": {}},
    )
    assert r.status_code == 404


def test_get_request_returns_row(client):
    r = client.post(
        "/api/requests",
        json={"type": "proxy", "params": {"url": "https://aisandbox-pa.googleapis.com/v1/ping"}},
    ).json()
    r2 = client.get(f"/api/requests/{r['id']}")
    assert r2.status_code == 200
    assert r2.json()["id"] == r["id"]


def test_get_missing_request_returns_404(client):
    r = client.get("/api/requests/9999")
    assert r.status_code == 404


def test_cancel_queued_request_marks_failed_with_canceled_error(client):
    r = client.post(
        "/api/requests",
        json={"type": "proxy", "params": {"url": "https://aisandbox-pa.googleapis.com/v1/ping"}},
    ).json()
    res = client.post(f"/api/requests/{r['id']}/cancel")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "failed"
    assert body["error"] == "canceled"
    assert body["finished_at"] is not None


def test_cancel_missing_request_returns_404(client):
    res = client.post("/api/requests/9999/cancel")
    assert res.status_code == 404


def test_cancel_already_canceled_request_returns_409(client):
    r = client.post(
        "/api/requests",
        json={"type": "proxy", "params": {}},
    ).json()
    first = client.post(f"/api/requests/{r['id']}/cancel")
    assert first.status_code == 200
    again = client.post(f"/api/requests/{r['id']}/cancel")
    assert again.status_code == 409


@pytest.mark.asyncio
async def test_worker_skips_canceled_request(client):
    """If the user cancels a queued row before the worker pops it,
    the worker should not run the handler and not flip status away
    from the canceled state."""
    row = client.post(
        "/api/requests",
        json={"type": "proxy", "params": {"marker": "skip-me"}},
    ).json()
    # Cancel BEFORE enqueueing the rid so the row already reads
    # status='failed' when the worker pops it.
    canceled = client.post(f"/api/requests/{row['id']}/cancel").json()
    assert canceled["status"] == "failed"

    handler_calls: list[dict] = []

    async def _spy_handler(params):
        handler_calls.append(params)
        return ({"echo": params}, None)

    w = WorkerController(handlers={"proxy": _spy_handler})
    task = asyncio.create_task(w.start())
    try:
        w.enqueue(row["id"])
        # Give the worker a couple of ticks; if it were going to run,
        # it would do so well under a second.
        await asyncio.sleep(0.3)
        assert handler_calls == []
        current = client.get(f"/api/requests/{row['id']}").json()
        assert current["status"] == "failed"
        assert current["error"] == "canceled"
    finally:
        w.request_shutdown()
        await asyncio.wait_for(task, timeout=2.0)


# ── Worker tests ──────────────────────────────────────────────────────────────


async def _ok_handler(params):
    return ({"echo": params}, None)


async def _fail_handler(_params):
    return ({}, "boom")


@pytest.mark.asyncio
async def test_worker_marks_request_done_on_ok(client):
    # Enqueue via the real API so we get a real DB row.
    row = client.post(
        "/api/requests",
        json={"type": "proxy", "params": {"marker": "abc"}},
    ).json()

    w = WorkerController(handlers={"proxy": _ok_handler})
    task = asyncio.create_task(w.start())
    try:
        w.enqueue(row["id"])
        # Poll the row until status flips, up to ~2s.
        for _ in range(40):
            await asyncio.sleep(0.05)
            current = client.get(f"/api/requests/{row['id']}").json()
            if current["status"] != "queued":
                break
        assert current["status"] == "done"
        assert current["result"] == {"echo": {"marker": "abc"}}
        assert current["error"] is None
    finally:
        w.request_shutdown()
        await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_worker_marks_request_failed_on_error(client):
    row = client.post(
        "/api/requests", json={"type": "proxy", "params": {}}
    ).json()

    w = WorkerController(handlers={"proxy": _fail_handler})
    task = asyncio.create_task(w.start())
    try:
        w.enqueue(row["id"])
        for _ in range(40):
            await asyncio.sleep(0.05)
            current = client.get(f"/api/requests/{row['id']}").json()
            if current["status"] != "queued":
                break
        assert current["status"] == "failed"
        assert current["error"] == "boom"
    finally:
        w.request_shutdown()
        await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_worker_unknown_request_type_fails(client):
    row = client.post(
        "/api/requests", json={"type": "totally_made_up", "params": {}}
    ).json()
    w = WorkerController(handlers={"proxy": _ok_handler})
    task = asyncio.create_task(w.start())
    try:
        w.enqueue(row["id"])
        for _ in range(40):
            await asyncio.sleep(0.05)
            current = client.get(f"/api/requests/{row['id']}").json()
            if current["status"] != "queued":
                break
        assert current["status"] == "failed"
        assert "unknown_request_type" in current["error"]
    finally:
        w.request_shutdown()
        await asyncio.wait_for(task, timeout=2.0)


# ── create_project + gen_image handler tests ──────────────────────────────────


async def _poll_until_settled(client, rid, timeout_s=2.0):
    for _ in range(int(timeout_s / 0.05)):
        await asyncio.sleep(0.05)
        current = client.get(f"/api/requests/{rid}").json()
        if current["status"] not in ("queued", "running"):
            return current
    return current


@pytest.mark.asyncio
async def test_worker_create_project_stores_project_id(client):
    async def stub_create_project(params):
        assert params.get("name") == "Scene 01"
        return {"raw": {"status": 200}, "project_id": "proj-abc"}, None

    row = client.post(
        "/api/requests",
        json={"type": "create_project", "params": {"name": "Scene 01"}},
    ).json()

    w = WorkerController(handlers={"create_project": stub_create_project})
    task = asyncio.create_task(w.start())
    try:
        w.enqueue(row["id"])
        settled = await _poll_until_settled(client, row["id"])
        assert settled["status"] == "done"
        assert settled["result"]["project_id"] == "proj-abc"
    finally:
        w.request_shutdown()
        await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_worker_gen_image_stores_media_ids(client):
    async def stub_gen_image(params):
        assert params["prompt"] == "a cat"
        assert params["project_id"] == "proj-abc"
        return {"raw": {"status": 200}, "media_ids": ["m-1", "m-2"]}, None

    row = client.post(
        "/api/requests",
        json={
            "type": "gen_image",
            "params": {"prompt": "a cat", "project_id": "proj-abc"},
        },
    ).json()

    w = WorkerController(handlers={"gen_image": stub_gen_image})
    task = asyncio.create_task(w.start())
    try:
        w.enqueue(row["id"])
        settled = await _poll_until_settled(client, row["id"])
        assert settled["status"] == "done"
        assert settled["result"]["media_ids"] == ["m-1", "m-2"]
    finally:
        w.request_shutdown()
        await asyncio.wait_for(task, timeout=2.0)


# ── gen_video worker tests ─────────────────────────────────────────────────








@pytest.mark.asyncio
async def test_worker_gen_image_rejects_missing_prompt(client):
    row = client.post(
        "/api/requests",
        json={"type": "gen_image", "params": {"project_id": "p"}},
    ).json()

    from flowboard.worker.processor import _handle_gen_image

    w = WorkerController(handlers={"gen_image": _handle_gen_image})
    task = asyncio.create_task(w.start())
    try:
        w.enqueue(row["id"])
        settled = await _poll_until_settled(client, row["id"])
        assert settled["status"] == "failed"
        assert settled["error"] == "missing_prompt"
    finally:
        w.request_shutdown()
        await asyncio.wait_for(task, timeout=2.0)


def test_recover_orphan_running_requests_marks_them_failed(client):
    """An agent restart while a long-running gen_video poll is mid-flight leaves
    the request in 'running' forever. The startup recovery hook should sweep
    those rows to 'failed' so the frontend stops polling indefinitely."""
    from datetime import datetime, timezone

    from flowboard.db import get_session
    from flowboard.db.models import Request
    from flowboard.main import _recover_orphan_running_requests

    # Two stuck running rows + one already-failed (untouched control).
    with get_session() as s:
        s.add(Request(
            type="gen_video",
            status="running",
            params={},
            created_at=datetime.now(timezone.utc),
        ))
        s.add(Request(
            type="gen_image",
            status="running",
            params={},
            created_at=datetime.now(timezone.utc),
        ))
        s.add(Request(
            type="gen_image",
            status="failed",
            error="prior",
            params={},
            created_at=datetime.now(timezone.utc),
        ))
        s.commit()

    touched = _recover_orphan_running_requests()
    assert touched == 2

    rows = client.get("/api/requests").json() if False else None  # noqa: F841
    from sqlmodel import select as _select
    with get_session() as s:
        rows = s.exec(_select(Request)).all()
        statuses = sorted([(r.type, r.status, r.error) for r in rows])
    assert statuses == [
        ("gen_image", "failed", "agent_restart_lost"),
        ("gen_image", "failed", "prior"),
        ("gen_video", "failed", "agent_restart_lost"),
    ]

    # Idempotent — second call should touch nothing.
    assert _recover_orphan_running_requests() == 0
