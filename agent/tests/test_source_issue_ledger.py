"""Canonical active questions and evidence-preserving reference repair."""
import copy
import json

import pytest

from flowboard.services.video_analyzer.source_issue_ledger import (
    build_issue_ledger, repair_inventory_references,
)


def _analysis(*findings, history=None):
    return {"source_verification": {"findings": list(findings), "review": {"accepted": True}},
            "source_refinement_history": history or [],
            "dialogue_track": [{"text": "Untouched spoken words"}]}


def _question(**changes):
    return {"finding_id": "original-id", "shot": 2, "code": "source_description_mismatch",
            "message": "The visible hand holds an open case.", "asset_id": "case", **changes}


def _wrapped(question, finding_id="later-id"):
    return {"finding_id": finding_id, "shot": question["shot"],
            "code": "resolution_incomplete", "message": "No valid cited disposition",
            "prior_finding_id": question["finding_id"]}


def test_nested_history_unwraps_concrete_active_question_only_and_preserves_input():
    original = _question()
    middle = {"finding_id": "middle-id", "shot": 2, "code": "source_refinement_unresolved",
              "original_code": "source_description_mismatch", "message": "Generic later wrapper",
              "resolution": {"finding_id": "original-id", "decision": "unresolved"}}
    final = _wrapped(middle)
    analysis = _analysis(final, history=[{"prior_report": {"refinement": {"resolutions": [
        {"finding": original, "resolution": {"decision": "unresolved"}},
        {"finding": middle}, {"finding": _question(finding_id="resolved-history", shot=7,
                                                    message="An old resolved question")} ]}}}])
    before = copy.deepcopy(analysis)
    ledger = build_issue_ledger(analysis)
    assert analysis == before
    assert len(ledger["issues"]) == 1
    issue = ledger["issues"][0]
    assert issue["shot"] == 2 and issue["code"] == original["code"]
    assert issue["message"] == original["message"] and issue["category"] == "visual"
    assert issue["asset_id"] == "case"
    assert ledger["audit"][0]["active_finding"] == final
    assert ledger["audit"][0]["root_finding"] == original
    assert "old resolved" not in json.dumps(ledger["issues"])
    for stale_id in ("original-id", "middle-id", "later-id"):
        assert stale_id not in json.dumps(ledger["issues"])


def test_exact_duplicate_roots_merge_provenance_without_merging_other_assets_or_fields():
    first = _question(message="  The visible\nhand holds an open case.  ")
    second = _question(finding_id="another-id")
    wrapper = {**_wrapped(second), "prior_question": second}
    analysis = _analysis(first, wrapper, _question(asset_id="other-case"),
                         _question(field="subtitle"), _question(field="title"))
    ledger = build_issue_ledger(analysis)
    assert len(ledger["issues"]) == 4
    merged = next(row for row in ledger["issues"] if row["provenance"]["count"] == 2)
    assert merged["message"] == second["message"]
    assert ledger["stats"]["duplicates_collapsed"] == ledger["stats"]["merged_duplicates"] == 1
    assert {row.get("field") for row in ledger["issues"]} == {None, "title", "subtitle"}
    assert {row["asset_id"] for row in ledger["issues"]} == {"case", "other-case"}


def test_stable_ids_ignore_wrapper_and_unicode_composition_but_not_question_content():
    question = _question(message="Café has an open case.")
    equivalent = _question(finding_id="new-id", message=" Cafe\u0301  has an open case. ", shot="2")
    wrapped = {**_wrapped(equivalent), "prior_question": equivalent}
    first = build_issue_ledger(_analysis(question))["issues"][0]
    second = build_issue_ledger(_analysis(wrapped))["issues"][0]
    assert first["finding_id"] == second["finding_id"]
    changed = build_issue_ledger(_analysis(_question(message="Café has a closed case.")))["issues"][0]
    assert first["finding_id"] != changed["finding_id"]


@pytest.mark.parametrize("code,category", [("invalid_resolution", "technical"),
    ("uncertain_presence", "uncertainty"), ("source_detail_unresolved", "uncertainty"),
    ("context_identity_unconfirmed", "uncertainty"), ("missing_person", "visual"),
    ("audio_not_checked", "technical")])
def test_categories_never_remove_active_questions(code, category):
    ledger = build_issue_ledger(_analysis(_question(code=code)))
    assert len(ledger["issues"]) == 1
    assert ledger["issues"][0]["category"] == category
    assert ledger["issues"][0]["code"] == code


@pytest.mark.parametrize("kind", ["missing", "foreign_shot", "ambiguous", "cyclic"])
def test_unavailable_ambiguous_or_cyclic_history_stays_a_technical_question(kind):
    active = _wrapped(_question())
    history = []
    if kind == "foreign_shot":
        history = [{"finding": _question(shot=99)}]
    if kind == "ambiguous":
        history = [{"finding": _question()}, {"finding": _question(message="The case is closed.")}]
    if kind == "cyclic":
        active["prior_question"] = active
    analysis = _analysis(active, history=history)
    ledger = build_issue_ledger(analysis)
    assert len(ledger["issues"]) == 1
    assert ledger["issues"][0]["category"] == "technical"
    assert ledger["issues"][0]["history_problem"]
    json.dumps(ledger)  # Cyclic input history must not produce a cyclic audit.
    if kind == "cyclic":
        assert analysis["source_verification"]["findings"][0]["prior_question"] is active


@pytest.mark.parametrize("findings", [{"bad": True}, [None], [{"code": [], "shot": {}, "message": "Malformed"}]])
def test_malformed_active_records_fail_closed(findings):
    ledger = build_issue_ledger({"source_verification": {"findings": findings}})
    assert ledger["issues"] and ledger["issues"][0]["category"] == "technical"
    assert ledger["issues"][0]["code"] == "invalid_issue_record"


def _inventory():
    return {"assets": [{"id": "person", "kind": "character", "name": "Person"},
                       {"id": "case", "kind": "prop", "description": "Physical case"},
                       {"id": "spectators", "kind": "group", "member_ids": ["person"]}],
            "scenes": [{"id": "hall", "shot_ids": [1, 2], "present_asset_ids": ["person", "case"],
                        "design_note": "Keep user scene design"}],
            "shots": {"1": {"scene_id": "hall", "asset_presence": [
                {"asset_id": "person", "visibility": "visible", "evidence_ids": ["one"]},
                {"asset_id": "case", "visibility": "partial", "holder_id": "person", "evidence_ids": ["one"]}],
                "user_note": "Do not change"},
                "2": {"scene_id": "hall", "asset_presence": [], "continuity_note": "Keep"}},
            "user_assets": {"paid_plate": "opaque-user-media-ref"}}


def _evidence():
    return [{"id": "one", "shot": 1, "frame": "one.jpg"},
            {"id": "two", "shot": 2, "frame": "two.jpg"}]


def test_alias_repair_is_transitive_and_preserves_original_defs_user_media_and_absent_fields():
    inventory = _inventory()
    inventory["assets"].extend([{"id": "old-person", "kind": "character", "portrait": "paid-portrait"},
                                {"id": "mid-person", "kind": "character"}])
    inventory["identity_aliases"] = {"old-person": "mid-person", "mid-person": "person"}
    inventory["shots"]["1"]["asset_presence"][0]["asset_id"] = "old-person"
    inventory["shots"]["1"]["asset_presence"][1].update(holder_id="mid-person", contains_ids=["old-person"])
    inventory["assets"][2]["member_ids"] = ["old-person"]
    inventory["assets"][1]["depends_on_asset_ids"] = ["mid-person"]
    before = copy.deepcopy(inventory)
    repaired, actions, findings = repair_inventory_references(inventory, _evidence())
    assert inventory == before and not findings
    assert repaired["user_assets"] == inventory["user_assets"]
    assert repaired["shots"]["2"] == inventory["shots"]["2"]
    assert repaired["shots"]["1"]["asset_presence"][0]["asset_id"] == "person"
    assert "contains_ids" not in repaired["shots"]["1"]["asset_presence"][0]
    assert repaired["shots"]["1"]["asset_presence"][1]["holder_id"] == "person"
    assert repaired["shots"]["1"]["asset_presence"][1]["contains_ids"] == ["person"]
    assert repaired["assets"][2]["member_ids"] == ["person"]
    assert repaired["assets"][1]["depends_on_asset_ids"] == ["person"]
    assert "member_ids" not in repaired["assets"][0]
    assert "depends_on_asset_ids" not in repaired["assets"][0]
    assert repaired["identity_alias_originals"]["old-person"]["portrait"] == "paid-portrait"
    assert {a["id"] for a in repaired["assets"]} == {"person", "case", "spectators"}
    assert any(a["action"] == "canonical_alias" for a in actions)
    twice, new_actions, new_findings = repair_inventory_references(repaired, _evidence())
    assert twice == repaired and not new_actions and not new_findings


def test_foreign_presence_citation_moves_only_when_own_evidence_survives_and_keeps_audit():
    inventory = _inventory()
    inventory["shots"]["1"]["asset_presence"][0]["evidence_ids"] = ["two", "one"]
    before = copy.deepcopy(inventory)
    repaired, actions, findings = repair_inventory_references(inventory, _evidence())
    assert inventory == before and not findings
    p = repaired["shots"]["1"]["asset_presence"][0]
    assert p["evidence_ids"] == ["one"] and p["identity_anchor_evidence_ids"] == ["two"]
    assert repaired["assets"][0]["identity_anchor_evidence_ids"] == ["two"]
    action = next(a for a in actions if a["action"] == "separate_identity_anchor_citations")
    assert action["before"] == ["two", "one"] and action["shot"] == 1
    assert repaired["shots"]["1"]["user_note"] == before["shots"]["1"]["user_note"]
    twice, new_actions, new_findings = repair_inventory_references(repaired, _evidence())
    assert twice == repaired and not new_actions and not new_findings


@pytest.mark.parametrize("citations", [["two"], ["one", "two", "unknown"]])
def test_missing_or_unknown_evidence_never_becomes_verified_by_reference_repair(citations):
    inventory = _inventory()
    inventory["shots"]["1"]["asset_presence"][0]["evidence_ids"] = citations
    repaired, _, findings = repair_inventory_references(inventory, _evidence())
    p = repaired["shots"]["1"]["asset_presence"][0]
    if "one" in citations:
        assert p["evidence_ids"] == ["one", "unknown"]
        assert any(f["code"] == "unknown_evidence_reference" for f in findings)
    else:
        assert p["evidence_ids"] == citations and "identity_anchor_evidence_ids" not in p
        assert any(f["code"] == "missing_own_shot_evidence" for f in findings)
    assert {f["shot"] for f in findings} == {1}


def test_alias_cycles_unknown_targets_and_cross_layer_aliases_remain_blocking():
    inventory = _inventory()
    inventory["identity_aliases"] = {"person": "case", "case": "person", "missing": "nonexistent"}
    inventory["screen_graphics"] = [{"id": "graphic", "kind": "character"}]
    inventory["identity_aliases"]["graphic"] = "person"
    repaired, _, findings = repair_inventory_references(inventory, _evidence())
    assert repaired["assets"] == inventory["assets"]
    assert repaired["screen_graphics"] == inventory["screen_graphics"]
    assert {"identity_alias_cycle", "unknown_alias_target"} <= {f["code"] for f in findings}
    assert repaired["shots"]["1"]["asset_presence"][0]["asset_id"] == "person"


def test_unknown_identity_and_scene_references_are_retained_and_scoped():
    inventory = _inventory()
    inventory["shots"]["1"]["scene_id"] = "unknown-scene"
    p = inventory["shots"]["1"]["asset_presence"][1]
    p.update(asset_id="unknown-prop", holder_id="unknown-holder", contains_ids=["unknown-content"])
    repaired, _, findings = repair_inventory_references(inventory, _evidence())
    assert repaired["shots"]["1"] == inventory["shots"]["1"]
    assert {"unknown_scene", "unknown_asset", "unknown_holder", "unknown_contents"} <= {f["code"] for f in findings}
    assert all(f["shot"] == 1 for f in findings)
    assert repaired["scenes"][0]["shot_ids"] == [2]
    assert repaired["scenes"][0]["present_asset_ids"] == []
    assert repaired["scenes"][0]["design_note"] == "Keep user scene design"


def test_scene_membership_rebuild_uses_actual_timeline_and_keeps_graphics_separate():
    inventory = _inventory()
    inventory["scenes"][0]["shot_ids"] = [99]
    inventory["scenes"][0]["present_asset_ids"] = ["ghost"]
    inventory["screen_graphics"] = [{"id": "overlay", "kind": "prop"}]
    inventory["shots"]["2"]["screen_graphics"] = [{"asset_id": "overlay", "visibility": "visible", "evidence_ids": ["two"]}]
    repaired, actions, findings = repair_inventory_references(inventory, _evidence())
    scene = repaired["scenes"][0]
    assert scene["shot_ids"] == [1, 2]
    assert scene["present_asset_ids"] == ["person", "case"]
    assert scene["screen_graphic_ids"] == ["overlay"]
    assert any(f["code"] == "unknown_scene_member" for f in findings)
    audit = next(a for a in actions if a["action"] == "rebuild_scene_membership")
    assert audit["before"]["present_asset_ids"] == ["ghost"]


def test_malformed_relationships_and_ambiguous_frames_are_not_silently_repaired():
    inventory = _inventory()
    inventory["assets"][2]["member_ids"] = [{"not": "an ID"}]
    p = inventory["shots"]["1"]["asset_presence"][0]
    p.update(holder_id={"bad": "ID"}, contains_ids=[{}])
    duplicate = {"id": "one", "shot": 2, "frame": "different.jpg"}
    repaired, _, findings = repair_inventory_references(inventory, _evidence() + [duplicate])
    assert repaired["assets"][2]["member_ids"] == [{"not": "an ID"}]
    assert repaired["shots"]["1"]["asset_presence"][0] == p
    assert {"invalid_relationship", "unknown_holder", "unknown_contents", "unknown_evidence_reference", "missing_own_shot_evidence"} <= {f["code"] for f in findings}


def test_clean_inventory_is_unchanged_and_does_not_grow_optional_empty_fields():
    inventory = _inventory()
    repaired, actions, findings = repair_inventory_references(inventory, _evidence())
    assert repaired == inventory and not actions and not findings


def test_recanonicalizing_array_only_asset_ids_keeps_id_distinctions_and_original_count():
    first = _question(asset_id=None, current={"id": "before-person"}, proposed={"id": "after-person"})
    ledger = build_issue_ledger(_analysis(first, {**first, "finding_id": "second-occurrence"}))
    canonical = ledger["issues"][0]
    assert canonical["asset_ids"] == ["after-person", "before-person"]
    assert canonical["provenance"]["count"] == 2
    second = build_issue_ledger(_analysis(*copy.deepcopy(ledger["issues"])))
    third = build_issue_ledger(_analysis(*copy.deepcopy(second["issues"])))
    assert third["issues"] == second["issues"] == ledger["issues"]
    assert second["audit"][0]["active_finding"] == canonical
    other = copy.deepcopy(canonical)
    other["asset_ids"] = ["different-person"]
    fourth = build_issue_ledger(_analysis(canonical, other))
    assert len(fourth["issues"]) == 2


def test_canonical_copy_and_wrapper_of_same_occurrences_do_not_inflate_provenance():
    question = _question()
    canonical = build_issue_ledger(_analysis(question, {**question, "finding_id": "another-id"}))["issues"][0]
    wrapped = {**_wrapped(canonical), "prior_question": copy.deepcopy(canonical)}
    ledger = build_issue_ledger(_analysis(copy.deepcopy(canonical), copy.deepcopy(canonical), wrapped))
    assert ledger["issues"] == [canonical]
    assert len(ledger["audit"]) == 3  # Audit each current carrier without counting new source occurrences.
    assert ledger["audit"][2]["active_finding"] == wrapped


def test_canonical_provenance_can_gain_a_new_distinct_occurrence_without_recounting_old_ones():
    question = _question()
    canonical = build_issue_ledger(_analysis(question, {**question, "finding_id": "another-id"}))["issues"][0]
    ledger = build_issue_ledger(_analysis(canonical, copy.deepcopy(canonical), _question(finding_id="third-occurrence")))
    assert ledger["issues"][0]["provenance"]["count"] == 3
    repeated = build_issue_ledger(_analysis(*ledger["issues"]))
    assert repeated["issues"] == ledger["issues"]


def test_malformed_asset_id_array_does_not_remove_valid_named_identity_or_crash():
    canonical = build_issue_ledger(_analysis(_question(asset_ids=["additional", None, {}, ""])))["issues"][0]
    assert canonical["asset_ids"] == ["additional", "case"]
    assert build_issue_ledger(_analysis(canonical))["issues"] == [canonical]


@pytest.mark.parametrize("code", [
    "unknown_scene_asset", "unknown_holder", "unknown_asset", "invalid_evidence",
    "invalid_visual_update", "missing_own_shot_evidence", "invalid_asset", "invalid_relationship",
    "invalid_presence", "invalid_visibility", "invented_shot", "missing_shot", "missing_scene",
    "missing_evidence", "duplicate_presence", "unknown_contents", "foreign_evidence",
    "invalid_graphic_evidence", "invalid_context_link", "context_link_unknown_asset",
    "context_link_presence", "context_link_current_evidence", "context_link_context_evidence", "context_link_reason",
])
def test_explicit_structural_and_citation_errors_are_reporting_technical_only(code):
    from flowboard.services.video_analyzer.source_issue_ledger import refresh_report_metadata
    issue = build_issue_ledger(_analysis(_question(code=code)))["issues"][0]
    assert issue["category"] == "technical"
    original = {"findings": [{**issue, "category": "visual"}], "status": "needs_review",
                "unresolved_shots": [2], "digest": "unchanged-verdict-binding"}
    refreshed = refresh_report_metadata(original)
    assert refreshed["findings"] == [issue]
    assert refreshed["status"] == "needs_review"
    assert refreshed["unresolved_shots"] == [2]
    assert refreshed["digest"] == original["digest"]


@pytest.mark.parametrize("code", ["source_mismatch", "registry_conflict", "invalid_unknown_future_code"])
def test_visual_categories_are_not_changed_by_message_text_or_code_prefix(code):
    from flowboard.services.video_analyzer.source_issue_ledger import refresh_report_metadata
    finding = _question(code=code, message="Protocol invalid evidence unknown asset: the hand is wrong.")
    report = refresh_report_metadata({"findings": [finding]})
    assert report["findings"][0]["category"] == "visual"
    assert report["findings"][0]["message"] == finding["message"]


def test_metadata_refresh_preserves_every_verdict_fact_review_and_digest_and_audits_changes():
    from flowboard.services.video_analyzer.source_issue_ledger import refresh_report_metadata
    report = {"status": "needs_review", "findings": [
        _question(code="unknown_holder", category="visual", evidence_ids=["frame-own"],
                  prior_question={"finding_id": "old", "message": "Keep full historical fact"}),
        _question(finding_id="real-fact", code="source_mismatch", category="technical"),
        _question(finding_id="uncertain-fact", code="uncertain_presence", category="visual"),
        _question(finding_id="scope", code="audio_not_checked", category="technical")],
        "issue_summary": {"by_category": {"technical": 2, "visual": 2, "uncertainty": 0},
                          "visual_claims": 99, "protocol_issues": 99, "uncertainty_issues": 99,
                          "scope_notes": 99, "duplicates_collapsed": 103, "merged_duplicates": 5, "input_findings": 452,
                          "active_issues": 4, "corrected_shots": [3], "technical_actions": 8},
        "issue_audit": {"input": [{"finding_id": "q1"}, {"finding_id": "q1"}, {"finding_id": "q2"}],
                        "protocol": [{"input": [{"finding_id": "q1"}] * 100}]},
        "review": {"accepted": True, "note": "Original user review", "digest": "review-binding"},
        "digest": "report-digest", "inventory_digest": "inventory-digest", "asset_digests": {"a": "d"},
        "shot_digests": {"2": "shot-digest"}, "evidence": [{"id": "frame-own", "shot": 2}],
        "reviewed_shots": [1, 2, 3], "unresolved_shots": [2], "retryable": False,
        "usage": {"model": {"calls": 20}}, "refinement": {"resolutions": [{"decision": "unresolved"}]}}
    original = copy.deepcopy(report)
    refreshed = refresh_report_metadata(report)
    assert report == original
    for before, after in zip(original["findings"], refreshed["findings"]):
        assert {k: v for k, v in after.items() if k != "category"} == {k: v for k, v in before.items() if k != "category"}
    assert [r["category"] for r in refreshed["findings"]] == ["technical", "visual", "uncertainty", "technical"]
    for key in original.keys() - {"findings", "issue_summary"}:
        assert refreshed[key] == original[key]
    summary = refreshed["issue_summary"]
    assert summary["by_category"] == {"technical": 2, "visual": 1, "uncertainty": 1}
    assert summary["duplicates_collapsed"] == summary["merged_duplicates"] == 1
    assert summary["input_findings"] == len(original["issue_audit"]["input"]) == 3
    assert (summary["visual_claims"], summary["protocol_issues"], summary["uncertainty_issues"], summary["scope_notes"]) == (2, 1, 1, 1)
    assert (summary["active_issues"], summary["corrected_shots"], summary["technical_actions"]) == (4, [3], 8)
    changes = {row["path"]: row for row in refreshed["metadata_audit"][-1]["changes"]}
    assert changes["findings[0].category"]["before"] == "visual"
    assert changes["issue_summary.duplicates_collapsed"]["before"] == 103
    assert changes["issue_summary.duplicates_collapsed"]["after"] == 1
    assert changes["issue_summary.input_findings"]["before"] == 452
    assert changes["issue_summary.input_findings"]["after"] == 3
    assert refresh_report_metadata(refreshed) == refreshed


def test_duplicate_reporting_ignores_later_phases_and_never_merges_missing_audit_ids():
    from flowboard.services.video_analyzer.source_issue_ledger import refresh_report_metadata
    report = {"findings": [], "issue_summary": {"duplicates_collapsed": 80},
              "issue_audit": {"input": [{"finding_id": "first"}, {"finding_id": "first"}, {}, None],
                              "protocol": [{"input": [{"finding_id": "first"}] * 50}]}}
    refreshed = refresh_report_metadata(report)
    assert refreshed["issue_summary"]["duplicates_collapsed"] == 1
    without_initial = {"findings": [], "issue_summary": {"duplicates_collapsed": 7},
                       "issue_audit": {"protocol": report["issue_audit"]["protocol"]}}
    assert refresh_report_metadata(without_initial)["issue_summary"]["duplicates_collapsed"] == 7
