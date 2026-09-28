import asyncio
import copy

import pytest

from flowboard.services.video_analyzer import source_identity_coverage as coverage
from flowboard.services.video_analyzer import source_inventory as inv


def _case(tmp_path, count=13):
    shots = [{"shot": n, "start": n - 1, "end": n} for n in range(1, count + 1)]
    evidence = []
    for shot in shots:
        n = shot["shot"]
        name = f"frame-{n}.jpg"
        (tmp_path / name).write_bytes(f"frame {n}".encode())
        evidence.append({"id": f"e{n}", "shot": n, "frame": name, "sha256": "untrusted old hash"})
    inventory = {"assets": [{"id": "late", "kind": "prop", "description": "distinct object", "evidence_ids": ["e1"]}],
                 "scenes": [], "shots": {str(s["shot"]): {"asset_presence": []} for s in shots}}
    inventory["shots"]["1"]["asset_presence"] = [{"asset_id": "late", "visibility": "visible"}]
    return inventory, shots, evidence


def _install(monkeypatch, responder=None):
    calls = []

    async def ask(system, payload, supplied, work_dir, usage, **kwargs):
        assert system == coverage.SYSTEM and kwargs["verify"] is True
        calls.append(copy.deepcopy(payload))
        total = usage.setdefault("mock", {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
        total["calls"] += 1
        if responder:
            return responder(payload)
        return {"checks": [{**pair, "status": "absent", "evidence_ids": [f"e{pair['shot']}"]}
                           for pair in payload["expected_checks"]]}

    monkeypatch.setattr(inv, "_ask", ask)
    return calls


def _run(case, tmp_path, journal=None):
    inventory, shots, evidence = case
    return asyncio.run(coverage.audit_coverage(inv, [{"asset_id": "late", "first_shot": 1}],
        inventory, shots, evidence, journal if journal is not None else {}, tmp_path,
        inv._CallLimiter(4), lambda: None))


def test_all_later_missing_pairs_checked_and_cached_without_inventory_mutation(tmp_path, monkeypatch):
    case = _case(tmp_path)
    case[0]["shots"]["4"]["asset_presence"] = [{"asset_id": "late", "visibility": "uncertain"}]
    original = copy.deepcopy(case)
    calls, journal = _install(monkeypatch), {}
    findings, completed = _run(case, tmp_path, journal)
    pairs = [pair for call in calls for pair in call["expected_checks"]]
    assert {p["shot"] for p in pairs} == set(range(2, 14)) - {4}
    assert len(calls) == 3 and completed == {"late"} and findings == []
    assert journal["usage"]["mock"]["calls"] == 3
    assert case == original
    assert _run(case, tmp_path, journal) == ([], {"late"})
    assert len(calls) == 3 and journal["usage"]["mock"]["calls"] == 3


@pytest.mark.parametrize("status", ["omitted", "uncertain"])
def test_positive_or_uncertain_appearance_stays_blocking_with_exact_shot(tmp_path, monkeypatch, status):
    case = _case(tmp_path, 2)
    _install(monkeypatch, lambda payload: {"checks": [{"shot": 2, "asset_id": "late", "status": status,
                                                       "evidence_ids": ["e2"], "reason": "visual detail"}]})
    findings, completed = _run(case, tmp_path)
    assert completed == {"late"}
    assert findings[0]["shot"] == 2 and findings[0]["asset_id"] == "late"
    assert findings[0]["code"] == "identity_coverage_" + status
    assert case[0]["shots"]["2"]["asset_presence"] == []


@pytest.mark.parametrize("bad", ["missing", "duplicate", "foreign_anchor", "wrong_status"])
def test_invalid_verdict_never_becomes_an_absence_pass(tmp_path, monkeypatch, bad):
    case = _case(tmp_path, 2)

    def response(_):
        row = {"shot": 2, "asset_id": "late", "status": "absent", "evidence_ids": ["e2"]}
        if bad == "foreign_anchor":
            row["evidence_ids"] = ["e1"]
        if bad == "wrong_status":
            row["status"] = "verified"
        return {"checks": [] if bad == "missing" else [row, row] if bad == "duplicate" else [row]}

    _install(monkeypatch, response)
    findings, completed = _run(case, tmp_path)
    assert completed == {"late"} and findings[0]["code"] == "identity_coverage_invalid"
    assert findings[0]["shot"] == 2


def test_provider_failure_is_scoped_to_every_affected_pair(tmp_path, monkeypatch):
    case = _case(tmp_path, 4)

    def fail(_):
        raise RuntimeError("provider unavailable")

    _install(monkeypatch, fail)
    findings, completed = _run(case, tmp_path)
    assert completed == {"late"}
    assert {f["shot"] for f in findings} == {2, 3, 4}
    assert all(f["code"] == "identity_coverage_failed" for f in findings)


def test_missing_frames_or_anchor_are_scoped_errors_without_paid_calls(tmp_path, monkeypatch):
    case = _case(tmp_path, 3)
    (tmp_path / "frame-1.jpg").unlink()
    (tmp_path / "frame-3.jpg").unlink()
    calls = _install(monkeypatch)
    findings, completed = _run(case, tmp_path)
    assert calls == [] and completed == {"late"}
    assert {f["shot"] for f in findings} == {2, 3}
    assert all(f["code"] == "identity_coverage_unavailable" for f in findings)


def test_changed_frame_bytes_only_invalidates_affected_batch(tmp_path, monkeypatch):
    case = _case(tmp_path, 13)
    calls, journal = _install(monkeypatch), {}
    _run(case, tmp_path, journal)
    (tmp_path / "frame-8.jpg").write_bytes(b"changed frame")
    _run(case, tmp_path, journal)
    assert len(calls) == 4
    assert {p["shot"] for p in calls[-1]["expected_checks"]} == set(range(7, 13))
    assert journal["usage"]["mock"]["calls"] == 4


def test_checks_run_concurrently_and_cancellation_awaits_workers(tmp_path, monkeypatch):
    case = _case(tmp_path, 13)

    async def scenario():
        entered, finished = [], []
        ready = asyncio.Event()

        async def ask(system, payload, supplied, work_dir, usage, **kwargs):
            first = payload["expected_checks"][0]["shot"]
            entered.append(first)
            if len(entered) == 3:
                ready.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.append(first)

        monkeypatch.setattr(inv, "_ask", ask)
        inventory, shots, evidence = case
        task = asyncio.create_task(coverage.audit_coverage(inv, [{"asset_id": "late", "first_shot": 1}],
            inventory, shots, evidence, {}, tmp_path, inv._CallLimiter(4), lambda: None))
        await asyncio.wait_for(ready.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert sorted(entered) == sorted(finished) and len(finished) == 3

    asyncio.run(scenario())
