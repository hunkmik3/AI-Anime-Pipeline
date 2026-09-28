"""Retraction belongs to the publishing batch, not its speculative seed."""
from __future__ import annotations

import asyncio
import copy
import json

import pytest

from flowboard.services import avis_text
from flowboard.services.video_analyzer import source_identity, source_inventory as inv
from flowboard.services.video_analyzer import source_parallel
from tests.test_source_inventory import _draft, _source, _verdict


def _asset(asset_id="gift", **kwargs):
    return {"id": asset_id, "kind": "prop", "name": asset_id,
            "description": "A distinct box", "evidence_ids": ["frame-7"],
            "member_ids": [], "depends_on_asset_ids": [], **kwargs}


def _observation(asset=None, shot=7):
    asset = asset or _asset()
    return {"inventory": {"assets": [copy.deepcopy(asset)], "scenes": [],
                           "shots": {str(shot): {"asset_presence": [{"asset_id": asset["id"]}]}}},
            "issues": [], "identity_changes": [], "retryable": False}


def _removed_patch(shot=7):
    return {"assets": [], "scenes": [], "shots": {str(shot): {"asset_presence": []}}}


def _payload(published):
    return {"known_inventory": inv._empty(), "newly_published_asset_ids": published,
            "asset_registration_origins": {
                "gift": {"batch_key": "7-12", "first_shot": 7, "origin_shots": [7]}}}


def _old_change(asset=None):
    return {"code": "registry_conflict", "reason": "retracted_observation",
            "asset_id": "gift", "first_shot": 7, "origin_shots": [7],
            "current": copy.deepcopy(asset or _asset()), "proposed": None}


def test_ordered_registration_records_first_publisher_only():
    catalog, origins = inv._empty(), {}
    first = source_parallel._register_with_provenance(
        inv, catalog, _observation(), "7-12", [{"shot": 7}], origins)
    later = source_parallel._register_with_provenance(
        inv, catalog, _observation(shot=13), "13-18", [{"shot": 13}], origins)
    assert first["newly_published_asset_ids"] == ["gift"]
    assert later["newly_published_asset_ids"] == []
    assert origins == {"gift": {"batch_key": "7-12", "first_shot": 7, "origin_shots": [7]}}
    assert later["asset_registration_origins"] == origins
    later["asset_registration_origins"]["gift"]["first_shot"] = 999
    assert origins["gift"]["first_shot"] == 7
    assert len(catalog["assets"]) == 1


def test_previously_registered_identity_absent_from_dispatch_seed_is_not_retracted():
    observation = _observation()
    entry = {"calls": {"repair": {"output": _removed_patch()}}}
    changes = inv._retracted_observations(entry, observation, _removed_patch(),
                                         [{"shot": 7}], observation["inventory"], _payload([]))
    assert changes == []


def test_true_publisher_retraction_preserves_profile_and_origin():
    observation = _observation()
    entry = {"calls": {"repair": {"output": _removed_patch()}}}
    changes = inv._retracted_observations(entry, observation, _removed_patch(),
                                         [{"shot": 7}], observation["inventory"], _payload(["gift"]))
    assert len(changes) == 1
    assert changes[0]["current"] == observation["inventory"]["assets"][0]
    assert changes[0]["proposed"] is None
    assert changes[0]["registration_origin"]["batch_key"] == "7-12"
    assert changes[0]["origin_shots"] == [7]


@pytest.mark.parametrize("support", ["definition", "presence", "holder", "contains", "member", "dependency"])
def test_delta_omission_with_surviving_support_is_not_a_retraction(support):
    observation = _observation()
    patch = _removed_patch()
    frozen = copy.deepcopy(observation["inventory"])
    if support == "definition":
        patch["assets"].append(_asset())
    elif support == "presence":
        patch["shots"]["7"]["asset_presence"].append({"asset_id": "gift"})
    else:
        presence = {"asset_id": "container"}
        container = _asset("container")
        if support == "holder":
            presence["holder_id"] = "gift"
        elif support == "contains":
            presence["contains_ids"] = ["gift"]
        elif support == "member":
            container["member_ids"] = ["gift"]
        else:
            container["depends_on_asset_ids"] = ["gift"]
        frozen["assets"].append(container)
        patch["shots"]["7"]["asset_presence"].append(presence)
    entry = {"calls": {"repair": {"output": patch}}}
    assert inv._retracted_observations(entry, observation, patch, [{"shot": 7}],
                                       frozen, _payload(["gift"])) == []


@pytest.mark.parametrize("published", [[], ["gift"]])
def test_cached_result_recomputes_only_retraction_without_model_calls(tmp_path, monkeypatch, published):
    observation = _observation()
    old = _old_change()
    genuine = {"code": "registry_conflict", "asset_id": "gift", "first_shot": 7,
               "current": _asset(), "proposed": _asset(description="A contradictory profile")}
    visual = {"code": "source_description_mismatch", "shot": 7,
              "message": "The source says left hand, but the right hand is visible."}
    raw = {"inventory": _removed_patch(), "findings": [inv._identity_finding(old),
            inv._identity_finding(genuine), visual], "identity_changes": [old, genuine],
           "reviewed_shots": [7], "evidence": [{"id": "frame-7", "shot": 7}],
           "status": "needs_review", "retryable": False}
    entry = {"result": copy.deepcopy(raw), "calls": {"repair": {"output": _removed_patch(),
             "responses": [{"text": "preserved raw model response"}]}}, "supplied": []}
    original_calls = copy.deepcopy(entry["calls"])

    async def forbidden(*args, **kwargs):
        raise AssertionError("A completed paid stage must not run again")

    monkeypatch.setattr(inv, "_stage_call", forbidden)
    result = asyncio.run(inv._verify_batch(entry, observation, [{"shot": 7}], observation["inventory"],
                        _payload(published), tmp_path / "video.mp4", tmp_path, 30,
                        {"remaining": 0}, inv._CallLimiter(1), lambda: None))
    assert result["cached"] is True
    assert result["status"] == "needs_review"
    assert result["inventory"] == raw["inventory"]
    assert result["evidence"] == raw["evidence"]
    assert result["reviewed_shots"] == [7]
    assert visual in result["findings"]
    assert genuine in result["identity_changes"]
    assert inv._identity_finding(genuine) in result["findings"]
    retractions = [c for c in result["identity_changes"] if c.get("reason") == "retracted_observation"]
    assert bool(retractions) == bool(published)
    assert entry["calls"] == original_calls
    assert entry["host_retraction_history"][-1]["previous"] == [old]
    history_count = len(entry["host_retraction_history"])
    inv._refresh_retraction_result(entry, entry["result"], observation, [{"shot": 7}],
                                   observation["inventory"], _payload(published))
    assert len(entry["host_retraction_history"]) == history_count


def test_missing_completed_repair_does_not_erase_cached_retraction():
    change = _old_change()
    result = {"inventory": _removed_patch(), "identity_changes": [change],
              "findings": [inv._identity_finding(change)], "status": "needs_review"}
    original = copy.deepcopy(result)
    inv._refresh_retraction_result({}, result, _observation(), [{"shot": 7}],
                                   _observation()["inventory"], _payload([]))
    assert result == original


def test_cached_false_retraction_alone_can_clear_without_altering_source_facts():
    change = _old_change()
    result = {"inventory": _removed_patch(), "identity_changes": [change],
              "findings": [inv._identity_finding(change)], "status": "needs_review"}
    entry = {"calls": {"repair": {"output": _removed_patch()}}}
    inv._refresh_retraction_result(entry, result, _observation(), [{"shot": 7}],
                                   _observation()["inventory"], _payload([]))
    assert result["status"] == "verified"
    assert result["findings"] == []
    assert result["identity_changes"] == []
    assert result["inventory"] == _removed_patch()


def test_parallel_registration_upgrade_reuses_paid_verdicts(tmp_path, monkeypatch):
    """Same-wave canonical IDs predate later repair despite an older shared seed."""
    video, work, shots = _source(tmp_path, count=18)
    monkeypatch.setattr(inv, "SOURCE_OBSERVATION_CONCURRENCY", 4)
    monkeypatch.setattr(inv, "SOURCE_CONCURRENCY", 4)
    calls = []
    checks = {}

    def reply(model, data):
        return avis_text.Completion(text=json.dumps(data), model=model,
                                    prompt_tokens=12, completion_tokens=8)

    async def complete(model, messages, **kwargs):
        payload = json.loads(messages[1]["content"][0]["text"])
        system = messages[0]["content"]
        calls.append(system)
        if system == source_identity.SYSTEM:
            candidates = payload["candidates"]
            first = candidates[0]
            return reply(model, {"mappings": [
                {"candidate_id": c["candidate_id"], "decision": "new" if i == 0 else "match",
                 "target_id": None if i == 0 else first["candidate_id"],
                 "candidate_evidence_ids": c["candidate_evidence_ids"][:1],
                 "target_evidence_ids": [] if i == 0 else first["candidate_evidence_ids"][:1],
                 "reason": "Matching source object"} for i, c in enumerate(candidates)]})
        batch = payload["source_shots"]
        first = batch[0]["shot"]
        if system == inv._VERIFY:
            checks[first] = checks.get(first, 0) + 1
            verdict = _verdict(batch)
            if first == 13 and checks[first] == 1:
                verdict["checks"][0].update(status="needs_review", findings=["The gift is absent here."])
            return reply(model, verdict)
        draft = _draft(batch)
        if first > 6 and "previous_draft" not in payload:
            prop = copy.deepcopy(next(a for a in draft["assets"] if a["id"] == "market-prop"))
            prop.update(id="new-gift", name="Gift box", description="A distinct white gift box")
            draft["assets"].append(prop)
            for row in draft["shots"].values():
                presence = copy.deepcopy(next(p for p in row["asset_presence"] if p["asset_id"] == "market-prop"))
                presence["asset_id"] = "new-gift"
                row["asset_presence"].append(presence)
            draft["scenes"][0]["present_asset_ids"].append("new-gift")
        return reply(model, draft)

    monkeypatch.setattr(avis_text, "complete", complete)
    inventory, report = asyncio.run(inv.analyze(video, work, shots, fps=30))
    assert report["status"] == "verified"
    gift = next(a for a in inventory["assets"] if a["name"] == "Gift box")
    path = work / "source_inventory.v1.json"
    cache = json.loads(path.read_text())
    assert cache["asset_registration_origins"][gift["id"]]["batch_key"] == "7-12"
    assert cache["batches"]["7-12"]["registration_provenance"]["newly_published_asset_ids"] == [gift["id"]]
    later = cache["batches"]["13-18"]
    assert later["registration_provenance"]["newly_published_asset_ids"] == []
    paid_stages = copy.deepcopy(later["calls"])
    paid_context = later["verification_context_digest"]
    false = {"code": "registry_conflict", "reason": "retracted_observation", "asset_id": gift["id"],
             "first_shot": 13, "origin_shots": [13], "current": gift, "proposed": None}
    later["result"]["identity_changes"].append(false)
    later["result"]["findings"].append(inv._identity_finding(false))
    later["result"]["status"] = "needs_review"
    # Emulate a pre-fix checkpoint: stages and their exact context survive.
    cache.pop("asset_registration_origins", None)
    for entry in cache["batches"].values():
        entry.pop("registration_provenance", None)
    path.write_text(json.dumps(cache))
    paid_count = len(calls)
    restored, again = asyncio.run(inv.analyze(video, work, shots, fps=30))
    assert len(calls) == paid_count
    assert restored == inventory
    assert again["status"] == "verified"
    upgraded = json.loads(path.read_text())["batches"]["13-18"]
    assert upgraded["calls"] == paid_stages
    assert upgraded["verification_context_digest"] == paid_context
    assert upgraded["host_retraction_history"][-1]["previous"] == [false]
    assert not upgraded["result"]["identity_changes"]
