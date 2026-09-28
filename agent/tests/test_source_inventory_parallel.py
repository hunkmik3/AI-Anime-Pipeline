"""Concurrent source review must preserve identities, evidence and durable work."""
from __future__ import annotations

import asyncio
import copy
import json

from flowboard.services import avis_text
from flowboard.services.video_analyzer import source_inventory as inv
from tests.test_source_inventory import _source, _draft, _verdict


def _completion(model, data):
    return avis_text.Completion(text=json.dumps(data), model=model,
                               prompt_tokens=12, completion_tokens=8)


def test_reviews_overlap_with_bounded_calls_and_ordered_results(tmp_path, monkeypatch):
    video, work, shots = _source(tmp_path, count=30)
    monkeypatch.setattr(inv, "SOURCE_CONCURRENCY", 4)
    active = peak = 0
    finished = []
    snapshots = []

    async def complete(model, messages, **kwargs):
        nonlocal active, peak
        payload = json.loads(messages[1]["content"][0]["text"])
        batch = payload["source_shots"]
        verify = messages[0]["content"] == inv._VERIFY
        first = batch[0]["shot"]
        active += 1
        peak = max(peak, active)
        try:
            # Later reviews finish first. Results must still commit by source order.
            await asyncio.sleep((0.08 if first == 1 else 0.02) if verify else 0.003)
            if verify:
                finished.append(first)
                return _completion(model, _verdict(batch))
            snapshots.append(copy.deepcopy(payload))
            draft = _draft(batch)
            for row in draft["shots"].values():
                row["asset_presence"][0]["state"] = "PENDING_MUTABLE_STATE"
            return _completion(model, draft)
        finally:
            active -= 1

    monkeypatch.setattr(avis_text, "complete", complete)
    inventory, report = asyncio.run(inv.analyze(video, work, shots, fps=30))
    assert 2 <= peak <= 4
    assert finished != sorted(finished)
    assert report["status"] == "verified"
    assert report["reviewed_shots"] == list(range(1, 31))
    assert list(inventory["shots"]) == [str(n) for n in range(1, 31)]
    for payload in snapshots[1:]:
        assert "PENDING_MUTABLE_STATE" not in json.dumps(payload)
        assert not (payload.get("known_inventory") or {}).get("shots")
    assert active == 0


def test_concurrency_one_remains_serial(tmp_path, monkeypatch):
    video, work, shots = _source(tmp_path, count=12)
    monkeypatch.setattr(inv, "SOURCE_CONCURRENCY", 1)
    active = peak = 0

    async def complete(model, messages, **kwargs):
        nonlocal active, peak
        batch = json.loads(messages[1]["content"][0]["text"])["source_shots"]
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.003)
            reply = _verdict(batch) if messages[0]["content"] == inv._VERIFY else _draft(batch)
            return _completion(model, reply)
        finally:
            active -= 1

    monkeypatch.setattr(avis_text, "complete", complete)
    inventory, report = asyncio.run(inv.analyze(video, work, shots, fps=30))
    assert peak == 1
    assert report["status"] == "verified"
    assert len(inventory["shots"]) == 12


def test_completed_review_findings_resume_without_new_calls_or_approval(tmp_path, monkeypatch):
    video, work, shots = _source(tmp_path, count=12)
    monkeypatch.setattr(inv, "SOURCE_CONCURRENCY", 4)
    calls = []

    async def complete(model, messages, **kwargs):
        batch = json.loads(messages[1]["content"][0]["text"])["source_shots"]
        verify = messages[0]["content"] == inv._VERIFY
        calls.append((verify, batch[0]["shot"]))
        if not verify:
            return _completion(model, _draft(batch))
        verdict = _verdict(batch)
        for row in verdict["checks"]:
            row["status"] = "needs_review"
            row["source_description_findings"] = ["Visible framing contradicts source camera size."]
        return _completion(model, verdict)

    monkeypatch.setattr(avis_text, "complete", complete)
    original_inventory, original = asyncio.run(inv.analyze(video, work, shots, fps=30))
    count = len(calls)
    restored_inventory, restored = asyncio.run(inv.analyze(video, work, shots, fps=30))
    assert len(calls) == count
    assert restored_inventory == original_inventory
    assert restored["status"] == original["status"] == "needs_review"
    assert restored["findings"] == original["findings"]
    assert restored["unresolved_shots"] == list(range(1, 13))
    assert not restored.get("human_review")


def test_worker_identity_change_cannot_silently_rewrite_published_profile(tmp_path, monkeypatch):
    video, work, shots = _source(tmp_path, count=12)
    monkeypatch.setattr(inv, "SOURCE_CONCURRENCY", 4)
    checks = {}

    async def complete(model, messages, **kwargs):
        payload = json.loads(messages[1]["content"][0]["text"])
        batch = payload["source_shots"]
        first = batch[0]["shot"]
        verify = messages[0]["content"] == inv._VERIFY
        await asyncio.sleep(0.015 if verify else 0.002)
        if not verify:
            draft = _draft(batch)
            if first == 1 and "previous_draft" in payload:
                draft["assets"][0]["description"] = "A different person with a contradictory identity."
            return _completion(model, draft)
        checks[first] = checks.get(first, 0) + 1
        verdict = _verdict(batch)
        if first == 1 and checks[first] == 1:
            verdict["checks"][0]["status"] = "needs_review"
            verdict["checks"][0]["findings"] = ["The lead identity description is incorrect."]
        return _completion(model, verdict)

    monkeypatch.setattr(avis_text, "complete", complete)
    inventory, report = asyncio.run(inv.analyze(video, work, shots, fps=30))
    lead = next(a for a in inventory["assets"] if a["id"] == "market-lead")
    assert lead["description"] == "distinct face"
    assert report["status"] == "needs_review"
    assert set(range(1, 13)) <= set(report["unresolved_shots"])
    assert any(f["code"] == "registry_conflict" for f in report["findings"])
    assert "contradictory identity" in json.dumps(report)


def test_transient_verifier_failure_resumes_without_repeating_observation(tmp_path, monkeypatch):
    video, work, shots = _source(tmp_path, count=2)
    monkeypatch.setattr(inv, "SOURCE_CONCURRENCY", 4)
    calls = []
    fail = True

    async def complete(model, messages, **kwargs):
        batch = json.loads(messages[1]["content"][0]["text"])["source_shots"]
        verify = messages[0]["content"] == inv._VERIFY
        calls.append(verify)
        if verify and fail:
            raise avis_text.AvisTextError("temporary gateway failure")
        return _completion(model, _verdict(batch) if verify else _draft(batch))

    monkeypatch.setattr(avis_text, "complete", complete)
    _, original = asyncio.run(inv.analyze(video, work, shots, fps=30))
    assert original["status"] != "verified"
    observation_calls = calls.count(False)
    fail = False
    _, restored = asyncio.run(inv.analyze(video, work, shots, fps=30))
    assert calls.count(False) == observation_calls == 1
    assert restored["status"] == "verified"


def test_cancelled_analysis_awaits_workers_and_resumes_completed_observations(tmp_path, monkeypatch):
    video, work, shots = _source(tmp_path, count=18)
    monkeypatch.setattr(inv, "SOURCE_CONCURRENCY", 4)
    calls = []
    completed_observations = []
    active = 0
    block = True
    started = None

    async def complete(model, messages, **kwargs):
        nonlocal active
        batch = json.loads(messages[1]["content"][0]["text"])["source_shots"]
        verify = messages[0]["content"] == inv._VERIFY
        calls.append((verify, batch[0]["shot"]))
        active += 1
        try:
            if verify and block:
                started.set()
                await asyncio.Event().wait()
            else:
                await asyncio.sleep(0.002)
            if not verify:
                completed_observations.append(batch[0]["shot"])
            return _completion(model, _verdict(batch) if verify else _draft(batch))
        finally:
            active -= 1

    monkeypatch.setattr(avis_text, "complete", complete)

    async def interrupt():
        nonlocal started
        started = asyncio.Event()
        task = asyncio.create_task(inv.analyze(video, work, shots, fps=30))
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.sleep(0.02)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert active == 0

    asyncio.run(interrupt())
    # A started call cancelled before its response is available must be retried.
    # Only completed responses can be checkpointed and reused. Wall-clock sleeps
    # do not guarantee every started observation completed on a loaded machine.
    prior_observations = list(completed_observations)
    assert prior_observations
    block = False
    _, report = asyncio.run(inv.analyze(video, work, shots, fps=30))
    assert report["status"] == "verified"
    for n in prior_observations:
        assert calls.count((False, n)) == 1


def test_retracted_observed_identity_cannot_remain_a_verified_phantom(tmp_path, monkeypatch):
    video, work, shots = _source(tmp_path, count=12)
    monkeypatch.setattr(inv, "SOURCE_CONCURRENCY", 4)
    checks = {}

    async def complete(model, messages, **kwargs):
        payload = json.loads(messages[1]["content"][0]["text"])
        batch = payload["source_shots"]
        first = batch[0]["shot"]
        verify = messages[0]["content"] == inv._VERIFY
        await asyncio.sleep(0.01 if verify else 0.001)
        if verify:
            checks[first] = checks.get(first, 0) + 1
            verdict = _verdict(batch)
            if first == 1 and checks[first] == 1:
                verdict["checks"][0]["status"] = "needs_review"
                verdict["checks"][0]["findings"] = ["No evidence supports the ghost character."]
            return _completion(model, verdict)
        draft = _draft(batch)
        if first == 1 and "previous_draft" not in payload:
            ghost = copy.deepcopy(draft["assets"][0])
            ghost.update(id="ghost", name="Ghost", description="Unsupported extra person")
            draft["assets"].append(ghost)
            presence = copy.deepcopy(draft["shots"]["1"]["asset_presence"][0])
            presence["asset_id"] = "ghost"
            draft["shots"]["1"]["asset_presence"].append(presence)
            draft["scenes"][0]["present_asset_ids"].append("ghost")
        return _completion(model, draft)

    monkeypatch.setattr(avis_text, "complete", complete)
    inventory, report = asyncio.run(inv.analyze(video, work, shots, fps=30))
    assert report["status"] != "verified"
    assert 1 in report["unresolved_shots"]
    assert any(c.get("reason") == "retracted_observation" and c["asset_id"] == "ghost"
               for c in report["registry_conflicts"])
    assert not any(p["asset_id"] == "ghost" for p in inventory["shots"]["1"]["asset_presence"])


def test_anchor_ranking_uses_host_observation_counts_with_catalog_only_context():
    catalog = {"shots": {}, "assets": [
        {"id": f"person-{n}", "kind": "character", "evidence_ids": [f"frame-{n}"]}
        for n in range(inv.MAX_ANCHORS + 2)
    ]}
    late_lead = f"person-{inv.MAX_ANCHORS + 1}"
    anchors = inv._anchor_ids(catalog, {late_lead: 20})
    assert anchors[0] == f"frame-{inv.MAX_ANCHORS + 1}"
    assert len(anchors) == inv.MAX_ANCHORS
    assert catalog["shots"] == {}
