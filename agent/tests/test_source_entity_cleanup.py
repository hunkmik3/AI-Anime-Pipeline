"""Grounded duplicate cleanup with real host guards and mocked model calls."""
import asyncio
import copy
import json

import pytest
from PIL import Image

from flowboard.services.video_analyzer import source_entity_cleanup as cleanup
from flowboard.services.video_analyzer import source_inventory as inv


def _asset(key, refs, kind="character", **extra):
    return {"id": key, "kind": kind, "name": key, "description": "stable source appearance",
            "role": "supporting", "reference_required": True, "member_ids": [],
            "depends_on_asset_ids": [], "evidence_ids": refs, **extra}


def _fixture(tmp_path):
    evidence = []
    for n in range(1, 7):
        path = tmp_path / f"frame-{n}.png"
        Image.new("RGB", (8, 8), (n * 20, 10, 30)).save(path)
        evidence.append({"id": f"e{n}", "shot": n, "timestamp_s": n + .1, "frame": path.name})
    data = {"schema_version": 1,
            "assets": [_asset("casual", ["e1"], description="Light jacket, hair loose; later VFX changes clothes."),
                       _asset("formal", ["e2", "e3", "e4"], description="Same recurring character in dark formal clothes.")],
            "scenes": [{"id": "room", "shot_ids": [1, 2, 3, 4], "present_asset_ids": ["casual", "formal"]}],
            "shots": {str(n): {"scene_id": "room", "evidence_ids": [f"e{n}"], "asset_presence": [
                {"asset_id": "casual" if n == 1 else "formal", "visibility": "visible",
                 "state": "wearing light clothes" if n == 1 else "wearing dark clothes",
                 "evidence_ids": [f"e{n}"], "contains_ids": []}]} for n in range(1, 5)}}
    return data, evidence


def _install(monkeypatch, pairs=None, decide=None):
    calls = []

    async def ask(system, payload, supplied, work_dir, usage, **kwargs):
        calls.append({"system": system, "payload": copy.deepcopy(payload),
                      "evidence": [e["id"] for e in supplied]})
        if system == cleanup.PROPOSE_SYSTEM:
            assert not supplied
            return {"pairs": pairs if pairs is not None else [{"asset_a": "casual", "asset_b": "formal"}]}
        assert system in (cleanup.VISUAL_SYSTEM, cleanup.REVIEW_SYSTEM)
        # Each pass has raw evidence, not the proposal reason or another verdict.
        assert "decision" not in payload and "previous_decision" not in payload
        row = {"asset_a": payload["asset_a"], "asset_b": payload["asset_b"],
               "decision": "same_identity", "reason": "Distinctive appearance supports the same source character.",
               "evidence_a_ids": payload["candidate_a"]["anchor_evidence_ids"][:1],
               "evidence_b_ids": payload["candidate_b"]["anchor_evidence_ids"][:1]}
        return decide(system, payload, row) if decide else row

    monkeypatch.setattr(inv, "_ask", ask)
    return calls


def _run(data, evidence, tmp_path, journal=None, **kwargs):
    return asyncio.run(cleanup.reconcile_entities(inv, data, evidence, journal if journal is not None else {},
                                                  tmp_path, asyncio.Semaphore(4), lambda: None, **kwargs))


def test_cross_wardrobe_match_needs_two_passes_and_remaps_relationships(tmp_path, monkeypatch):
    data, evidence = _fixture(tmp_path)
    data["assets"].append(_asset("portrait", ["e1"], "prop", depends_on_asset_ids=["casual"]))
    data["assets"].append(_asset("friends", ["e1"], "background_group", member_ids=["casual"]))
    data["shots"]["1"]["asset_presence"].append({"asset_id": "portrait", "visibility": "visible",
                                               "holder_id": "casual", "contains_ids": ["casual"], "evidence_ids": ["e1"]})
    original = copy.deepcopy(data)
    calls = _install(monkeypatch)
    result, findings, audit = _run(data, evidence, tmp_path)
    assert data == original and not findings
    assert audit["aliases"] == {"casual": "formal"}
    assert len(calls) == 3
    assert {call["system"] for call in calls[1:]} == {cleanup.VISUAL_SYSTEM, cleanup.REVIEW_SYSTEM}
    assets = {a["id"]: a for a in result["assets"]}
    assert "casual" not in assets
    assert assets["formal"]["description"] == original["assets"][1]["description"]
    assert set(assets["formal"]["evidence_ids"]) == {"e1", "e2", "e3", "e4"}
    assert assets["portrait"]["depends_on_asset_ids"] == ["formal"]
    assert assets["friends"]["member_ids"] == ["formal"]
    assert result["shots"]["1"]["asset_presence"][0]["state"] == "wearing light clothes"
    assert result["shots"]["1"]["asset_presence"][1]["holder_id"] == "formal"
    assert result["shots"]["1"]["asset_presence"][1]["contains_ids"] == ["formal"]
    assert result["scenes"][0]["present_asset_ids"] == ["formal"]
    assert audit["originals"]["shots"]["1"] == original["shots"]["1"]
    assert {a["id"] for a in audit["originals"]["assets"]} >= {"casual", "formal", "portrait", "friends"}


@pytest.mark.parametrize("guard", ["kind", "cooccur", "partial_cooccur", "dependency", "group_unknown", "holder"])
def test_host_rejects_unsafe_pairs_without_visual_calls(tmp_path, monkeypatch, guard):
    data, evidence = _fixture(tmp_path)
    if guard == "kind":
        data["assets"][1]["kind"] = "prop"
    elif guard in {"cooccur", "partial_cooccur"}:
        data["shots"]["1"]["asset_presence"].append({"asset_id": "formal", "visibility": "partial" if guard.startswith("partial") else "visible",
                                                   "evidence_ids": ["e1"]})
    elif guard == "dependency":
        data["assets"][0]["depends_on_asset_ids"] = ["different-person"]
    elif guard == "group_unknown":
        for a in data["assets"]: a["kind"] = "background_group"
    elif guard == "holder":
        data["shots"]["1"]["asset_presence"][0]["holder_id"] = "formal"
    calls = _install(monkeypatch)
    result, _, audit = _run(data, evidence, tmp_path)
    assert result == data and not audit["aliases"]
    assert audit["pairs"][0]["status"] == "host_blocked"
    assert len(calls) == 1


def test_disagreement_retains_both_as_pending_without_invalidating_existing_shots(tmp_path, monkeypatch):
    data, evidence = _fixture(tmp_path)
    data["shots"]["99"] = {"scene_id": "room", "asset_presence": []}
    _install(monkeypatch, decide=lambda system, payload, row: {
        **row, "decision": "different_identity" if system == cleanup.REVIEW_SYSTEM else "same_identity"})
    result, findings, audit = _run(data, evidence, tmp_path)
    assert result == data and audit["status"] == "needs_review"
    assert not findings
    assert len(audit["pending_pairs"]) == len(audit["informational_findings"]) == 1
    assert audit["pending_pairs"][0]["pair"] == ["casual", "formal"]
    assert set(audit["pending_pairs"][0]["passes"]) == {"compare", "review"}
    information = audit["informational_findings"][0]
    assert information["code"] == "entity_alias_pending" and information["blocking"] is False
    assert information["original_code"] == "entity_identity_unresolved"
    assert "shot" not in information
    assert not audit["aliases"]


def test_missing_evidence_never_becomes_text_only_merge(tmp_path, monkeypatch):
    data, evidence = _fixture(tmp_path)
    (tmp_path / "frame-1.png").unlink()
    calls = _install(monkeypatch)
    result, findings, audit = _run(data, evidence, tmp_path)
    assert result == data and not findings and not audit["aliases"]
    assert audit["status"] == "needs_review" and len(audit["pending_pairs"]) == 1
    assert len(calls) == 1
    assert "no readable" in audit["pairs"][0]["reason"]


def test_each_pass_must_cite_own_evidence_on_both_sides(tmp_path, monkeypatch):
    data, evidence = _fixture(tmp_path)
    _install(monkeypatch, decide=lambda system, payload, row: {
        **row, "evidence_b_ids": row["evidence_a_ids"]} if system == cleanup.REVIEW_SYSTEM else row)
    result, findings, audit = _run(data, evidence, tmp_path)
    assert result == data and not findings and not audit["aliases"]
    assert audit["status"] == "needs_review" and len(audit["pending_pairs"]) == 1
    assert "belonging to each asset" in audit["pairs"][0]["reason"]


def test_resume_reuses_both_completed_visual_checks(tmp_path, monkeypatch):
    data, evidence = _fixture(tmp_path)
    calls = _install(monkeypatch)
    journal = {}
    first = _run(data, evidence, tmp_path, journal)
    second = _run(data, evidence, tmp_path, journal)
    assert first == second and len(calls) == 3


def test_resume_retries_only_missing_or_malformed_pass(tmp_path, monkeypatch):
    data, evidence = _fixture(tmp_path)
    review_calls = 0

    def response(system, payload, row):
        nonlocal review_calls
        if system == cleanup.REVIEW_SYSTEM:
            review_calls += 1
            if review_calls == 1: return {"reason": "missing decision"}
        return row

    calls = _install(monkeypatch, decide=response)
    journal = {}
    _, first_findings, first_audit = _run(data, evidence, tmp_path, journal)
    _, second_findings, second_audit = _run(data, evidence, tmp_path, journal)
    assert not first_findings and not second_findings
    assert first_audit["pending_pairs"] and not second_audit["pending_pairs"]
    assert first_audit["status"] == "needs_review" and second_audit["status"] == "complete"
    assert second_audit["aliases"] == {"casual": "formal"}
    assert len(calls) == 4 and review_calls == 2


def test_image_change_invalidates_cached_identity_agreement(tmp_path, monkeypatch):
    data, evidence = _fixture(tmp_path)
    calls = _install(monkeypatch)
    journal = {}
    _run(data, evidence, tmp_path, journal)
    Image.new("RGB", (8, 8), "white").save(tmp_path / "frame-1.png")
    _run(data, evidence, tmp_path, journal)
    assert len(calls) == 6 and len(journal["runs"]) == 2


def test_anchor_selection_has_first_last_and_clear_actual_appearance(tmp_path):
    data, evidence = _fixture(tmp_path)
    readable = {e["id"]: e for e in evidence}
    candidate = cleanup._anchor_candidate(data["assets"][1], data, readable)
    assert candidate["anchor_evidence_ids"] == ["e2", "e4", "e3"]
    assert len(candidate["anchor_evidence_ids"]) <= 3


def test_transitive_merge_cannot_bypass_cooccurrence(tmp_path, monkeypatch):
    data, evidence = _fixture(tmp_path)
    data["assets"].append(_asset("third", ["e5"]))
    data["shots"]["1"]["asset_presence"].append({"asset_id": "third", "visibility": "partial", "evidence_ids": ["e1"]})
    data["shots"]["5"] = {"scene_id": "room", "asset_presence": [{"asset_id": "third", "visibility": "visible", "evidence_ids": ["e5"]}]}
    _install(monkeypatch, pairs=[{"asset_a": "casual", "asset_b": "formal"}, {"asset_a": "formal", "asset_b": "third"}])
    result, _, audit = _run(data, evidence, tmp_path)
    assert audit["aliases"] == {"casual": "formal"}
    assert {a["id"] for a in result["assets"]} == {"formal", "third"}
    assert audit["pairs"][1]["status"] == "host_blocked"


def test_transitive_merge_cannot_bypass_independent_distinct_verdict(tmp_path, monkeypatch):
    data, evidence = _fixture(tmp_path)
    data["assets"].append(_asset("third", ["e5"]))
    data["shots"]["5"] = {"scene_id": "room", "asset_presence": [
        {"asset_id": "third", "visibility": "visible", "evidence_ids": ["e5"]}]}

    def response(system, payload, row):
        if (payload["asset_a"], payload["asset_b"]) == ("casual", "third"):
            return {**row, "decision": "different_identity"}
        return row

    _install(monkeypatch, pairs=[{"asset_a": "casual", "asset_b": "formal"},
                                 {"asset_a": "casual", "asset_b": "third"},
                                 {"asset_a": "formal", "asset_b": "third"}], decide=response)
    result, _, audit = _run(data, evidence, tmp_path)
    assert audit["aliases"] == {"casual": "formal"}
    assert {a["id"] for a in result["assets"]} == {"formal", "third"}
    assert "transitive" in audit["pairs"][2]["reason"]


def test_group_match_requires_exact_established_members(tmp_path, monkeypatch):
    data, evidence = _fixture(tmp_path)
    for a in data["assets"]:
        a.update(kind="background_group", member_ids=["member-one", "member-two"])
    data["assets"].extend([_asset("member-one", ["e5"]), _asset("member-two", ["e6"])])
    _install(monkeypatch)
    _, findings, audit = _run(data, evidence, tmp_path)
    assert not findings and audit["aliases"] == {"casual": "formal"}
    data["assets"][1]["member_ids"] = ["member-one", "other-member"]
    _, _, rejected = _run(data, evidence, tmp_path)
    assert not rejected["aliases"] and rejected["pairs"][0]["status"] == "host_blocked"


def test_unresolved_group_member_ids_do_not_authorize_a_merge(tmp_path, monkeypatch):
    data, evidence = _fixture(tmp_path)
    for asset in data["assets"]:
        asset.update(kind="background_group", member_ids=["unknown-member"])
    calls = _install(monkeypatch)
    result, _, audit = _run(data, evidence, tmp_path)
    assert result == data and len(calls) == 1 and not audit["aliases"]


def test_pair_proposal_limits_are_enforced_by_host():
    assets = {str(n): {} for n in range(30)}
    response = {"pairs": [{"asset_a": "0", "asset_b": str(n)} for n in range(1, 30)]}
    pairs, limited, _ = cleanup._pairs(response, assets)
    assert len(pairs) == cleanup.MAX_PAIRS_PER_ASSET and len(limited) == 25


def test_missing_proposal_is_unavailable_without_inventing_global_shot_flags(tmp_path, monkeypatch):
    data, evidence = _fixture(tmp_path)

    async def ask(*args, **kwargs): return {"not_pairs": []}

    monkeypatch.setattr(inv, "_ask", ask)
    result, findings, audit = _run(data, evidence, tmp_path)
    assert result == data and not findings and audit["status"] == "unavailable"
    assert not audit["aliases"]


def _omissions(key="casual", shots=(2, 3, 4)):
    return [{"code": "identity_coverage_omitted", "asset_id": key, "shot": number}
            for number in shots]


@pytest.mark.parametrize("decision", ["same_identity", "different_identity", "uncertain"])
def test_coverage_hint_checks_pair_missed_by_proposal_and_never_authorizes_merge(tmp_path, monkeypatch, decision):
    data, evidence = _fixture(tmp_path)
    calls = _install(monkeypatch, pairs=[], decide=lambda system, payload, row: {**row, "decision": decision})
    result, findings, audit = _run(data, evidence, tmp_path, prior_findings=_omissions())
    assert len(calls) == 3
    assert {call["system"] for call in calls[1:]} == {cleanup.VISUAL_SYSTEM, cleanup.REVIEW_SYSTEM}
    assert audit["candidate_hints"][0]["omitted_shots"] == [2, 3, 4]
    assert audit["aliases"] == ({"casual": "formal"} if decision == "same_identity" else {})
    assert not findings
    assert bool(audit["pending_pairs"]) is (decision == "uncertain")
    assert (audit["status"] == "needs_review") is (decision == "uncertain")
    if decision != "same_identity":
        assert result == data
    # Independent visual reviewers receive images/profiles, not the coverage
    # claim or another model's proposed match as purported supporting evidence.
    assert all(set(call["payload"]) == {"asset_a", "asset_b", "candidate_a", "candidate_b"}
               for call in calls[1:])


def test_coverage_hints_count_distinct_shots_and_keep_only_top_two_same_kind(tmp_path):
    data, _ = _fixture(tmp_path)
    data["assets"].extend([_asset("third", ["e5"]), _asset("fourth", ["e6"]),
                           _asset("offscreen", ["e6"]), _asset("object", ["e5"], "prop")])
    for number in (2, 3, 4):
        rows = data["shots"][str(number)]["asset_presence"]
        rows.extend([{"asset_id": "object", "visibility": "visible"},
                     {"asset_id": "offscreen", "visibility": "offscreen"},
                     {"asset_id": "unknown", "visibility": "visible"}])
        if number <= 3:
            rows.extend([{"asset_id": "third", "visibility": "partial"}] * 2)
        if number == 2:
            rows.append({"asset_id": "fourth", "visibility": "visible"})
    findings = _omissions(shots=(1, 2, 2, 3, 4, 999)) + _omissions("unknown") + [
        {"code": "source_mismatch", "asset_id": "casual", "shot": 2},
        {"code": "identity_coverage_omitted", "asset_id": "casual", "shot": True}]
    hints = cleanup._coverage_hints(data, findings)
    assert [(hint["asset_a"], hint["asset_b"], hint["omitted_shots"]) for hint in hints] == [
        ("casual", "formal", [2, 3, 4]), ("casual", "third", [2, 3])]
    assert cleanup._coverage_hints(data, list(reversed(findings))) == hints


def test_coverage_hint_cannot_bypass_host_cooccurrence_guard(tmp_path, monkeypatch):
    data, evidence = _fixture(tmp_path)
    data["shots"]["1"]["asset_presence"].append({"asset_id": "formal", "visibility": "partial", "evidence_ids": ["e1"]})
    calls = _install(monkeypatch, pairs=[])
    result, _, audit = _run(data, evidence, tmp_path, prior_findings=_omissions())
    assert result == data and not audit["aliases"] and len(calls) == 1
    assert audit["pairs"][0]["status"] == "host_blocked"


def test_coverage_hints_share_existing_pair_budget_and_deduplicate_model_pairs():
    assets = {str(n): {} for n in range(10)}
    response = {"pairs": [{"asset_a": "0", "asset_b": str(n)} for n in range(1, 10)]}
    hints = [{"asset_a": "0", "asset_b": "9", "reason": "Coverage candidate"}]
    pairs, limited, ignored = cleanup._pairs(response, assets, candidate_hints=hints)
    assert pairs[0][0] == ("0", "9")
    assert len(pairs) == cleanup.MAX_PAIRS_PER_ASSET
    assert len({pair for pair, _ in pairs}) == len(pairs)
    assert len(limited) == 5 and not ignored


def test_no_usable_hints_keep_legacy_digest_payload_and_resume(tmp_path, monkeypatch):
    data, evidence = _fixture(tmp_path)
    calls = _install(monkeypatch)
    journal = {}
    first = _run(data, evidence, tmp_path, journal)
    readable = cleanup._readable_evidence(inv, evidence, tmp_path)
    candidates = {a["id"]: cleanup._anchor_candidate(a, data, readable) for a in data["assets"]}
    selected = {ref for candidate in candidates.values() for ref in candidate["anchor_evidence_ids"]}
    legacy_digest = inv._digest({"version": cleanup.VERSION,
        "systems": [cleanup.PROPOSE_SYSTEM, cleanup.VISUAL_SYSTEM, cleanup.REVIEW_SYSTEM],
        "models": [inv.MODEL, inv.VERIFY_MODEL], "inventory": data,
        "evidence": [readable[ref] for ref in sorted(selected)],
        "limits": [cleanup.MAX_PAIRS, cleanup.MAX_PAIRS_PER_ASSET, cleanup.MAX_ANCHORS_PER_ASSET]})
    assert first[2]["input_digest"] == legacy_digest
    assert "candidate_hints" not in first[2]
    assert set(calls[0]["payload"]) == {"catalog"}
    assert _run(data, evidence, tmp_path, journal, prior_findings=[]) == first
    assert _run(data, evidence, tmp_path, journal, prior_findings=_omissions("unknown")) == first
    assert len(calls) == 3 and len(journal["runs"]) == 1


def test_relevant_coverage_hints_invalidate_empty_proposal_cache_and_resume(tmp_path, monkeypatch):
    data, evidence = _fixture(tmp_path)
    calls = _install(monkeypatch, pairs=[])
    journal = {}
    first = _run(data, evidence, tmp_path, journal)
    assert not first[2]["pairs"] and len(calls) == 1
    second = _run(data, evidence, tmp_path, journal, prior_findings=_omissions())
    assert second[2]["aliases"] == {"casual": "formal"}
    assert len(calls) == 4 and len(journal["runs"]) == 2
    assert _run(data, evidence, tmp_path, journal, prior_findings=list(reversed(_omissions()))) == second
    assert len(calls) == 4


def test_optional_comparison_limit_is_pending_without_creating_shot_defects(tmp_path, monkeypatch):
    data, evidence = _fixture(tmp_path)
    monkeypatch.setattr(cleanup, "MAX_PAIRS", 0)
    calls = _install(monkeypatch)
    result, findings, audit = _run(data, evidence, tmp_path)
    assert result == data and not findings and not audit["aliases"]
    assert len(calls) == 1 and audit["status"] == "needs_review"
    assert audit["pending_pairs"][0]["status"] == "limit_reached"
    assert audit["informational_findings"][0]["original_code"] == "entity_cleanup_limit"
    assert audit["informational_findings"][0]["blocking"] is False


def test_pending_alias_preserves_real_per_shot_visibility_and_evidence_defects(tmp_path, monkeypatch):
    data, evidence = _fixture(tmp_path)
    data["shots"]["2"]["asset_presence"][0]["visibility"] = "uncertain"
    data["shots"]["3"]["evidence_ids"] = []
    data["shots"]["3"]["asset_presence"][0]["evidence_ids"] = []
    batch = [{"shot": number} for number in range(1, 5)]
    _, original_issues = inv._normalise(data, batch, data, evidence)
    calls = _install(monkeypatch, decide=lambda system, payload, row: {**row, "decision": "uncertain"})
    journal = {}
    first = _run(data, evidence, tmp_path, journal)
    result, findings, audit = first
    _, retained_issues = inv._normalise(result, batch, result, evidence)
    assert result == data and not findings and audit["pending_pairs"]
    assert retained_issues == original_issues
    assert {issue["code"] for issue in retained_issues} >= {"uncertain_presence", "missing_evidence", "foreign_evidence"}
    assert _run(data, evidence, tmp_path, journal) == first and len(calls) == 3


def test_physical_alias_keeps_unrelated_graphic_bytes_and_shot_change_scope(tmp_path):
    data, _ = _fixture(tmp_path)
    data["screen_graphics"] = [{"id": "watermark", "description": "Source overlay", "custom": {"keep": True}}]
    for number, row in data["shots"].items():
        row["screen_graphics"] = [{"asset_id": "watermark", "evidence_ids": [f"e{number}"],
                                    "position": "lower center", "custom": {"keep": number}}]
    data["shots"]["2"]["screen_graphics"][0]["contains_ids"] = None
    data["shots"]["3"]["screen_graphics"][0]["contains_ids"] = ["unrelated", "unrelated"]
    data["shots"]["4"]["screen_graphics"] = None
    original = copy.deepcopy(data)
    result = cleanup._remap(data, {"casual": "formal"})
    assert data == original
    assert json.dumps(result["screen_graphics"]) == json.dumps(original["screen_graphics"])
    for number in data["shots"]:
        assert json.dumps(result["shots"][number]["screen_graphics"]) == json.dumps(original["shots"][number]["screen_graphics"])
    for number in ("2", "3", "4"):
        assert json.dumps(result["shots"][number]) == json.dumps(original["shots"][number])
    assert set(cleanup._originals(data, result)["shots"]) == {"1"}


def test_graphics_remap_only_explicit_changed_alias_references(tmp_path):
    data, _ = _fixture(tmp_path)
    data["screen_graphics"] = [
        {"id": "annotation", "member_ids": ["casual"], "depends_on_asset_ids": ["casual", "formal"],
         "description": "Do not rewrite this description or add fields"},
        {"id": "casual", "custom": "retained"},
    ]
    data["shots"]["1"]["screen_graphics"] = [
        {"asset_id": "casual", "position": "left", "custom": {"keep": 1}},
        {"asset_id": "annotation", "holder_id": "casual", "position": "right"},
        {"asset_id": "annotation", "contains_ids": ["casual", "formal", "unrelated"]},
    ]
    original = copy.deepcopy(data)
    result = cleanup._remap(data, {"casual": "formal"})
    expected_graphics = copy.deepcopy(original["screen_graphics"])
    expected_graphics[0].update(member_ids=["formal"], depends_on_asset_ids=["formal"])
    expected_graphics[1]["id"] = "formal"
    assert result["screen_graphics"] == expected_graphics
    expected_rows = copy.deepcopy(original["shots"]["1"]["screen_graphics"])
    expected_rows[0]["asset_id"] = "formal"
    expected_rows[1]["holder_id"] = "formal"
    expected_rows[2]["contains_ids"] = ["formal", "unrelated"]
    assert result["shots"]["1"]["screen_graphics"] == expected_rows
    assert "contains_ids" not in result["shots"]["1"]["screen_graphics"][0]
    assert "screen_graphics" not in result["shots"]["2"]
    assert data == original
