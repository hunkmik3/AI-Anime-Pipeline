"""Fresh, bounded protocol QA preserves facts, independent evidence and history."""
import asyncio
import copy
import json

import pytest

from flowboard.services import avis_text
from flowboard.services.video_analyzer import source_inventory as inv, source_protocol_review as protocol
from tests.test_source_inventory import _source, _draft


def _case(tmp_path, count=3):
    video, work, shots = _source(tmp_path, count=count)
    shots[0]["dialogue"] = "Original spoken words"
    inventory = {**_draft(shots), "schema_version": inv.SCHEMA_VERSION}
    evidence = [{**e, "sha256": inv._hash_file(work / e["frame"])}
                for e in inv._initial_evidence(work, shots, 30, False)]
    binding = inv._digest({"video": inv._hash_file(video), "models": [inv.MODEL, inv.VERIFY_MODEL],
        "evidence": sorted((e["id"], e["sha256"], e.get("timestamp_s"), e.get("shot")) for e in evidence)})
    prior = {"method": "source_frames", "status": "needs_review", "retryable": False,
        "reviewed_shots": list(range(1, count + 1)), "unresolved_shots": [1, 2],
        "inventory_digest": inv.inventory_digest(inventory), "evidence": evidence,
        "findings": [{"code": "resolution_incomplete", "shot": 1,
                      "message": "No valid disposition", "prior_finding_id": "old-question"},
                     {"code": "source_mismatch", "shot": 2, "message": "A real visible discrepancy"}],
        "refinement": {"source_binding": binding, "source_shots_digest": inv._digest(shots),
            "policy": "original-refinement-policy", "resolutions": [{"finding": {
                "finding_id": "old-question", "code": "source_description_mismatch", "shot": 1,
                "message": "The subtitle description omits a visible graphic"}, "valid": False}]}}
    analysis = {"video": {"fps": 30}, "shots": shots, "scene_inventory": inventory,
                "source_verification": prior, "dialogue_track": [{"text": "Original spoken words"}],
                "transcript": {"segments": [{"text": "Original spoken words"}]}}
    return video, work, analysis


def _reply(payload, decision="resolved"):
    return {"checks": [{"shot": s["shot"], "status": "verified", "findings": [],
        "source_description_findings": [], "evidence_ids": [f"shot-{s['shot']}-frame-1"],
        "resolutions": [{"finding_id": f["finding_id"], "decision": decision,
                         "reason": "Current source frames establish this disposition",
                         "evidence_ids": [f"shot-{s['shot']}-frame-1"]}
                        for f in payload["prior_findings"] if f["shot"] == s["shot"]]}
                        for s in payload["proposed_source_shots"]], "review_requests": []}


def _install(monkeypatch, responder=None):
    calls = []
    async def complete(model, messages, **kwargs):
        payload = json.loads(messages[1]["content"][0]["text"])
        calls.append(copy.deepcopy(payload))
        value = responder(payload, len(calls)) if responder else _reply(payload)
        if isinstance(value, Exception): raise value
        return avis_text.Completion(text=json.dumps(value), model=model, prompt_tokens=12, completion_tokens=8)
    monkeypatch.setattr(avis_text, "complete", complete)
    return calls


def _run(case, **kwargs):
    return asyncio.run(protocol.review(*case, **kwargs))


def test_model_ledger_has_only_current_ids_and_preserves_history_facts():
    full = [{"finding_id": "current", "prior_finding_id": "old", "asset_id": "person",
             "prior_question": {"finding_id": "old", "message": "Missing person", "asset_id": "person"},
             "resolution": {"finding_id": "older", "decision": "unresolved", "reason": "Face obscured",
                            "evidence_ids": ["frame-1"]}, "allowed_decisions": ["resolved", "unresolved"]}]
    original = copy.deepcopy(full)
    clean = protocol.model_ledger(full)
    assert full == original
    assert clean[0]["finding_id"] == "current"
    assert "prior_finding_id" not in clean[0]
    assert "finding_id" not in clean[0]["prior_question"]
    assert "finding_id" not in clean[0]["resolution"]
    assert clean[0]["prior_question"]["message"] == "Missing person"
    assert clean[0]["resolution"]["evidence_ids"] == ["frame-1"]


def test_fresh_qa_preserves_facts_audio_unaffected_shots_and_main_provenance(tmp_path, monkeypatch):
    case = _case(tmp_path)
    original = copy.deepcopy(case[2])
    calls = _install(monkeypatch)
    result = _run(case)
    report = result["source_verification"]
    assert report["unresolved_shots"] == [2]
    assert len(report["findings"]) == 1
    assert all(report["findings"][0][key] == value
               for key, value in original["source_verification"]["findings"][1].items())
    assert report["findings"][0]["category"] == "visual"
    assert report["protocol_review"]["selected_shots"] == [1]
    assert report["refinement"] == original["source_verification"]["refinement"]
    assert result["source_protocol_review_history"][0]["prior_report"] == original["source_verification"]
    for field in ("shots", "scene_inventory", "dialogue_track", "transcript"):
        assert result[field] == original[field]
    assert case[2] == original
    assert len(calls) == 1
    assert [s["shot"] for s in calls[0]["proposed_source_shots"]] == [1]
    assert "draft_inventory" not in calls[0] and "source_shots" not in calls[0]
    question = calls[0]["prior_findings"][0]
    assert question["message"] == "The subtitle description omits a visible graphic"
    assert question["code"] == "source_description_mismatch"
    assert "prior_question" not in question
    assert _run(case) == result
    assert _run((case[0], case[1], result)) == result
    assert len(calls) == 1


@pytest.mark.parametrize("malformed", ["missing_check", "bad_checks", "foreign_id", "missing_disposition"])
def test_invalid_or_incomplete_fresh_reply_cannot_clear_protocol_shot(tmp_path, monkeypatch, malformed):
    case = _case(tmp_path)
    def responder(payload, _):
        reply = _reply(payload)
        if malformed == "missing_check": reply["checks"] = []
        elif malformed == "bad_checks": reply["checks"] = "verified"
        elif malformed == "foreign_id": reply["checks"][0]["resolutions"][0]["finding_id"] = "old-question"
        else: reply["checks"][0]["resolutions"] = []
        return reply
    calls = _install(monkeypatch, responder)
    result = _run(case)
    assert result["source_verification"]["status"] == "needs_review"
    assert {1, 2} <= set(result["source_verification"]["unresolved_shots"])
    assert not result["source_verification"]["protocol_review"]["retryable"]
    assert _run((case[0], case[1], result)) == result
    assert len(calls) == 1


def test_real_finding_remains_when_fresh_qa_cannot_resolve_it(tmp_path, monkeypatch):
    case = _case(tmp_path)
    case[2]["source_verification"]["findings"].append({"code": "source_mismatch", "shot": 1,
                                                      "message": "Person identity remains uncertain"})
    _install(monkeypatch, lambda payload, _: _reply(payload, "unresolved"))
    report = _run(case)["source_verification"]
    assert {1, 2} <= set(report["unresolved_shots"])
    assert any(f.get("message") == "Person identity remains uncertain" for f in report["findings"])


def test_unchanged_structural_uncertainty_cannot_be_cleared_by_clean_verdict(tmp_path, monkeypatch):
    case = _case(tmp_path)
    case[2]["scene_inventory"]["shots"]["1"]["asset_presence"][0]["visibility"] = "uncertain"
    case[2]["source_verification"]["inventory_digest"] = inv.inventory_digest(case[2]["scene_inventory"])
    _install(monkeypatch)
    report = _run(case)["source_verification"]
    assert 1 in report["unresolved_shots"]
    assert any(f["code"] == "uncertain_presence" for f in report["findings"])


@pytest.mark.parametrize("changed", ["video", "frame", "shot", "inventory", "model"])
def test_source_binding_changes_fail_before_api_calls(tmp_path, monkeypatch, changed):
    case = _case(tmp_path)
    calls = _install(monkeypatch)
    if changed == "video": case[0].write_bytes(b"changed source")
    elif changed == "frame": (case[1] / case[2]["shots"][0]["frames"][0]).write_bytes(b"changed frame")
    elif changed == "shot": case[2]["shots"][0]["source"]["action"] = "Changed action"
    elif changed == "inventory": case[2]["scene_inventory"]["shots"]["1"]["asset_presence"] = []
    else: monkeypatch.setattr(inv, "VERIFY_MODEL", "changed-model")
    with pytest.raises(ValueError, match="unchanged, source-bound"):
        _run(case)
    assert calls == []


def test_transient_failure_retries_without_accepting_or_losing_prior_findings(tmp_path, monkeypatch):
    case = _case(tmp_path)
    calls = _install(monkeypatch, lambda payload, number: RuntimeError("provider unavailable") if number == 1 else _reply(payload))
    failed = _run(case)
    assert failed["source_verification"]["protocol_review"]["retryable"]
    assert any(f.get("message") == "The subtitle description omits a visible graphic"
               for f in failed["source_verification"]["findings"])
    assert failed["source_protocol_review_history"][0]["prior_report"]["findings"][0]["prior_finding_id"] == "old-question"
    result = _run(case)
    assert len(calls) == 2
    assert result["source_verification"]["unresolved_shots"] == [2]
    assert not result["source_verification"]["retryable"]


def test_additional_frame_request_stays_unresolved_without_extraction(tmp_path, monkeypatch):
    case = _case(tmp_path)
    def responder(payload, _):
        reply = _reply(payload)
        reply["review_requests"] = [{"shot": 1, "timestamp_s": 0.4, "crop": None,
                                     "reason": "The occluded hand needs another source view"}]
        return reply
    async def forbidden(*args, **kwargs):
        raise AssertionError("Bounded protocol QA must not extract frames")
    monkeypatch.setattr(inv, "_stage_frames", forbidden)
    _install(monkeypatch, responder)
    report = _run(case)["source_verification"]
    assert 1 in report["unresolved_shots"]
    assert any(f["code"] == "source_detail_unresolved" for f in report["findings"])


def test_clean_report_without_protocol_errors_is_a_noop(tmp_path, monkeypatch):
    case = _case(tmp_path)
    case[2]["source_verification"]["findings"] = [{"code": "source_mismatch", "shot": 2, "message": "Actual question"}]
    calls = _install(monkeypatch)
    assert _run(case) == case[2]
    assert calls == []


def test_new_machine_report_does_not_inherit_old_human_acceptance(tmp_path, monkeypatch):
    case = _case(tmp_path)
    prior = case[2]["source_verification"]
    prior["review"] = {"accepted_by": "reviewer", "accepted_shots": [1, 2], "note": "Old review"}
    for finding in prior["findings"]: finding["accepted"] = True
    _install(monkeypatch)
    result = _run(case)
    assert "review" not in result["source_verification"]
    assert all("accepted" not in f for f in result["source_verification"]["findings"])
    assert result["source_protocol_review_history"][0]["prior_report"]["review"] == prior["review"]


def test_protocol_context_uses_full_timeline_and_relevant_canonical_profiles(tmp_path, monkeypatch):
    case = _case(tmp_path)
    calls = _install(monkeypatch)
    result = _run(case)
    payload = calls[0]
    temporal = payload["temporal_context"]
    assert [row["shot"] for row in payload["proposed_source_shots"]] == [1]
    assert {row["shot"] for row in temporal["neighbors"]} == {2, 3}
    assert temporal["policy"]["presence_from_neighbor"] is False
    assert "current_frame" in temporal["evidence_roles"]["shot-1-frame-1"]["roles"]
    assert "temporal_neighbor" in temporal["evidence_roles"]["shot-2-frame-1"]["roles"]
    for neighbor in temporal["neighbors"]:
        assert "dialogue" not in neighbor["source_shot"]
    assert "dialogue" not in payload["proposed_source_shots"][0]
    definitions = {a["id"] for a in payload["proposed_inventory"]["assets"]}
    assert definitions == {a["id"] for a in payload["identity_catalog"]}
    assert {p["asset_id"] for p in payload["proposed_inventory"]["shots"]["1"]["asset_presence"]} <= definitions
    assert result["shots"] == case[2]["shots"]


@pytest.mark.parametrize("mode", ["valid", "foreign_current", "missing"])
def test_contextual_identity_requires_fresh_valid_link_without_changing_presence(tmp_path, monkeypatch, mode):
    case = _case(tmp_path)
    link = {"shot": 1, "asset_id": "market-lead", "current_evidence_ids": ["shot-1-frame-1"],
            "context_evidence_ids": ["shot-2-frame-1"],
            "reason": "The distinctive physical features and adjacent reverse angle establish continuity."}
    case[2]["source_verification"]["refinement"]["context_links"] = [
        {"stage": "checker_0", "links": [{"link": link, "valid": True}]}]
    original = copy.deepcopy(case[2])

    def responder(payload, _):
        assert payload["required_context_reviews"] == [{"shot": 1, "asset_id": "market-lead"}]
        reply = _reply(payload)
        if mode != "missing":
            reply["context_links"] = [copy.deepcopy(link)]
            if mode == "foreign_current":
                reply["context_links"][0]["current_evidence_ids"] = ["shot-2-frame-1"]
        return reply

    _install(monkeypatch, responder)
    result = _run(case)
    report = result["source_verification"]
    assert (1 in report["unresolved_shots"]) == (mode != "valid")
    if mode == "foreign_current":
        assert any(f["code"] == "context_link_current_evidence" for f in report["findings"])
    if mode == "missing":
        assert any(f["code"] == "context_identity_unconfirmed" for f in report["findings"])
    assert result["scene_inventory"] == original["scene_inventory"]
    assert result["shots"] == original["shots"]
    assert case[2] == original


def test_summary_refreshes_verdicts_but_never_invents_a_source_correction(tmp_path, monkeypatch):
    case = _case(tmp_path)
    case[2]["source_verification"]["issue_summary"] = {
        "input_findings": 12, "active_issues": 2, "duplicates_collapsed": 7,
        "by_category": {"technical": 1, "visual": 1, "uncertainty": 0},
        "processed_shots": [1, 2], "retained_verified_shots": [3],
        "corrected_shots": [1, 2], "corrected_and_verified_shots": [],
        "verified_shots": [3], "unresolved_shots": [1, 2], "technical_actions": 4}
    _install(monkeypatch)
    report = _run(case)["source_verification"]
    summary = report["issue_summary"]
    assert summary["active_issues"] == len(report["findings"]) == 1
    assert summary["by_category"] == {"technical": 0, "visual": 1, "uncertainty": 0}
    assert summary["corrected_shots"] == [1, 2]
    assert summary["corrected_and_verified_shots"] == [1]
    assert summary["verified_shots"] == [1, 3]
    assert summary["unresolved_shots"] == report["unresolved_shots"] == [2]
    assert summary["retained_verified_shots"] == [3]
    assert summary["technical_actions"] == 4
    assert summary["duplicates_collapsed"] == 7


def test_repeated_protocol_wrappers_are_one_active_question_with_preserved_audit(tmp_path, monkeypatch):
    case = _case(tmp_path)
    case[2]["source_verification"]["issue_audit"] = {"input": [{"original": "input audit"}],
                                                   "final": [{"original": "final audit"}]}
    case[2]["source_verification"]["findings"].append(
        copy.deepcopy(case[2]["source_verification"]["findings"][0]))
    calls = _install(monkeypatch)
    result = _run(case)
    assert len(calls[0]["prior_findings"]) == 1
    report = result["source_verification"]
    # The displayed count belongs to the original input cohort, not later QA.
    assert report["issue_summary"]["duplicates_collapsed"] == 0
    assert report["issue_summary"]["input_findings"] == 1
    assert report["protocol_review"]["issue_ledger"]["input"]["duplicates_collapsed"] == 1
    assert report["issue_summary"]["active_issues"] == 1
    assert report["issue_summary"]["corrected_shots"] == []
    assert len(result["source_protocol_review_history"][0]["prior_report"]["findings"]) == 3
    journal = json.loads((case[1] / "source_protocol_review.v1.json").read_text())
    assert len(journal["input_issue_ledger"]["audit"]) == 3
    assert report["issue_audit"]["input"] == [{"original": "input audit"}]
    assert report["issue_audit"]["final"] == [{"original": "final audit"}]
    assert len(report["issue_audit"]["protocol"][0]["input"]) == 3


def test_changed_temporal_policy_invalidates_completed_protocol_checkpoint(tmp_path, monkeypatch):
    case = _case(tmp_path)
    calls = _install(monkeypatch)
    before = _run(case)
    original_hash = inv._hash_file

    def changed(path):
        value = original_hash(path)
        return value + "new-context-policy" if path.name == "source_temporal_context.py" else value

    monkeypatch.setattr(inv, "_hash_file", changed)
    after = _run(case)
    assert len(calls) == 2
    assert after["source_verification"]["protocol_review"]["policy"] != before["source_verification"]["protocol_review"]["policy"]


@pytest.mark.parametrize("still_active", [True, False])
def test_compacted_protocol_audit_schedules_only_current_active_issues(tmp_path, monkeypatch, still_active):
    from flowboard.services.video_analyzer.source_issue_ledger import build_issue_ledger

    case = _case(tmp_path)
    ledger = build_issue_ledger(case[2])
    report = case[2]["source_verification"]
    report["findings"] = [f for f in ledger["issues"] if still_active or f["shot"] != 1]
    report["issue_audit"] = {"final": ledger["audit"]}
    assert not any(f["code"] in protocol.PROTOCOL_CODES for f in report["findings"])
    calls = _install(monkeypatch)
    result = _run(case)
    assert len(calls) == int(still_active)
    if still_active:
        assert result["source_verification"]["protocol_review"]["selected_shots"] == [1]
        assert result["source_verification"]["unresolved_shots"] == [2]
    else:
        assert result == case[2]


@pytest.mark.parametrize("code", ["invalid_evidence", "source_mismatch"])
def test_explicit_selection_can_review_nonprotocol_issue_without_rewriting_facts(tmp_path, monkeypatch, code):
    case = _case(tmp_path)
    prior = case[2]["source_verification"]
    prior["findings"] = [{"code": code, "shot": 2, "message": "Check the current committed source facts"}]
    prior["unresolved_shots"] = [2]
    original = copy.deepcopy(case[2])
    calls = _install(monkeypatch)
    result = _run(case, selected_shots=[2])
    report = result["source_verification"]
    assert len(calls) == 1
    assert [shot["shot"] for shot in calls[0]["proposed_source_shots"]] == [2]
    assert report["protocol_review"]["selected_shots"] == [2]
    assert report["status"] == "verified" and report["unresolved_shots"] == []
    assert report["findings"] == []
    assert "review" not in report
    assert report["issue_summary"]["corrected_shots"] == []
    for key in ("shots", "scene_inventory", "dialogue_track", "transcript"):
        assert result[key] == original[key]
    assert case[2] == original


def test_explicit_selection_retains_all_unselected_issues(tmp_path, monkeypatch):
    case = _case(tmp_path)
    case[2]["source_verification"]["findings"].append({
        "code": "source_mismatch", "shot": 3, "message": "Unselected shot still has a wrong holder"})
    case[2]["source_verification"]["unresolved_shots"] = [1, 2, 3]
    original = copy.deepcopy(case[2])
    calls = _install(monkeypatch)
    result = _run(case, selected_shots=[2])
    report = result["source_verification"]
    assert [shot["shot"] for shot in calls[0]["proposed_source_shots"]] == [2]
    assert report["status"] == "needs_review"
    assert report["unresolved_shots"] == [1, 3]
    assert {f["shot"] for f in report["findings"]} == {1, 3}
    assert {f["message"] for f in report["findings"]} == {
        "The subtitle description omits a visible graphic", "Unselected shot still has a wrong holder"}
    assert result["source_protocol_review_history"][-1]["prior_report"] == original["source_verification"]
    assert result["scene_inventory"] == original["scene_inventory"]
    assert result["shots"] == original["shots"]


def test_selected_clean_verdict_cannot_clear_global_blocker(tmp_path, monkeypatch):
    case = _case(tmp_path)
    case[2]["source_verification"]["findings"].append({
        "code": "source_mismatch", "shot": None, "message": "Whole-film identity conflict remains unresolved"})
    calls = _install(monkeypatch)
    report = _run(case, selected_shots=[2])["source_verification"]
    assert [shot["shot"] for shot in calls[0]["proposed_source_shots"]] == [2]
    assert report["status"] == "needs_review"
    assert report["unresolved_shots"] == [1, 2, 3]
    assert any(f.get("shot") is None and f["message"] == "Whole-film identity conflict remains unresolved"
               for f in report["findings"])
    assert report["issue_summary"]["verified_shots"] == []


def test_completed_default_review_does_not_skip_different_explicit_selection(tmp_path, monkeypatch):
    case = _case(tmp_path)
    calls = _install(monkeypatch)
    first = _run(case)
    assert first["source_verification"]["protocol_review"]["selected_shots"] == [1]
    assert first["source_verification"]["unresolved_shots"] == [2]
    continued = (case[0], case[1], first)
    second = _run(continued, selected_shots=[2])
    assert len(calls) == 2
    assert [[shot["shot"] for shot in payload["proposed_source_shots"]] for payload in calls] == [[1], [2]]
    assert second["source_verification"]["protocol_review"]["selected_shots"] == [2]
    assert second["source_verification"]["status"] == "verified"
    assert second["shots"] == first["shots"]
    assert second["scene_inventory"] == first["scene_inventory"]
    replay = _run((case[0], case[1], second), selected_shots=[2])
    assert replay == second and len(calls) == 2


@pytest.mark.parametrize("selection", [[], [0], [-1], [True], [1.0], ["1"], [1, 1], [4], "1"])
def test_invalid_explicit_review_selection_is_rejected_before_model_call(tmp_path, monkeypatch, selection):
    case = _case(tmp_path)
    original = copy.deepcopy(case[2])
    calls = _install(monkeypatch)
    with pytest.raises(ValueError):
        _run(case, selected_shots=selection)
    assert calls == []
    assert case[2] == original
    assert not (case[1] / "source_protocol_review.v1.json").exists()


def test_explicit_review_still_requires_unchanged_bound_source(tmp_path, monkeypatch):
    case = _case(tmp_path)
    case[2]["shots"][1]["source"]["action"] = "Edited after source verification"
    calls = _install(monkeypatch)
    with pytest.raises(ValueError, match="unchanged, source-bound"):
        _run(case, selected_shots=[2])
    assert calls == []
