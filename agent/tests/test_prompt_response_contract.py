"""Malformed gateway output never bypasses the source/prompt contract."""
import asyncio
from copy import deepcopy
import json

import pytest

from flowboard.services import avis_text, prompt_coverage as coverage, prompt_writer as writer
from flowboard.services.video_analyzer import adapt
from tests.test_prompt_coverage import _film, _draft, _review, _run


def test_complete_outer_json_wins_over_nested_coverage_array():
    obj = {"prompt": "Plain prompt", "end_state": "waiting", "coverage": [{"shot": 1}]}
    assert avis_text.extract_json(json.dumps(obj)) == obj
    assert avis_text.extract_json("```json\n" + json.dumps(obj) + "\n``` trailing prose") == obj
    assert avis_text.extract_json("Here is the response: " + json.dumps(obj)) == obj


def test_truncated_outer_object_cannot_be_accepted_as_its_complete_child_array():
    with pytest.raises(avis_text.AvisTextError, match="top-level JSON"):
        avis_text.extract_json('{"prompt":"Partial", "coverage":[{"shot":1}]')


def test_split_finish_reason_and_usage_both_survive_sse():
    text, metadata, error = avis_text._parse_sse(
        'data: {"delta":"{}"}\ndata: {"finishReason":"length"}\ndata: {"usage":{"completionTokens":16000}}\n')
    assert text == "{}" and error == ""
    assert metadata["finishReason"] == "length"
    assert metadata["usage"]["completionTokens"] == 16000


def test_known_wrapper_unwrap_is_bounded_and_does_not_create_a_prompt():
    response = {"prompt": "Actual text", "coverage": []}
    assert writer._clip_response({"result": response}) == response
    assert writer._clip_response({"data": [response]}) == response
    assert writer._clip_response({"data": response, "result": response}) != response
    assert writer._clip_response({"result": {"data": {"output": response}}}) != response
    assert writer._clip_response([{}, {}]) == [{}, {}]


def test_grouped_coverage_expands_but_keeps_literal_correct_shot_and_completeness_checks():
    requirements = [{"id": "a", "shot": 1}, {"id": "b", "shot": 1}, {"id": "c", "shot": 2}]
    matches, issues = coverage.expand_coverage([
        {"requirement_ids": ["a", "b"], "shot": 1, "quote": "Shared fact."},
        {"requirement_id": "c", "shot": 2, "quote": "Second fact."}])
    prompt = "[SHOT 1 — 00:00–00:02]\nShared fact.\n[SHOT 2 — 00:02–00:04]\nSecond fact."
    assert issues == []
    assert coverage.check_coverage(prompt, requirements, matches) == []
    matches[1]["shot"] = 2
    assert any("wrong shot" in x for x in coverage.check_coverage(prompt, requirements, matches))
    assert any("Missing coverage" in x for x in coverage.check_coverage(prompt, requirements, matches[:2]))


@pytest.mark.parametrize("ids", [[], ["a", "a"], ["a", 2], "a"])
def test_malformed_grouped_coverage_cannot_drop_coverage(ids):
    expanded, issues = coverage.expand_coverage([{"requirement_ids": ids, "shot": 1, "quote": "Fact."}])
    assert issues and not expanded


def test_source_screen_graphics_remain_provenance_without_becoming_render_requirements():
    seq, shots, cast, extra, assets, _ = _film(True)
    shots[0]["source_appearances"] = [{"source_shot": 2, "scene_id": "dock",
        "asset_presence": deepcopy(shots[0]["asset_presence"]),
        "screen_graphics": [{"asset_id": "watermark", "text": "SOURCE CAPTION"}],
        "evidence_ids": ["frame-2"]}]
    original = deepcopy(shots)
    ask = writer._payload(seq, shots, [(0, 3), (3, 6)], 6, characters=cast, environment=None,
        look="cg3d", aspect_ratio=None, previous_state="", unsafe={}, school_age=False,
        reference_assets=extra, strict=True)
    facts = ask["shots"][0]["source_appearances"]
    assert facts[0] == {"source_shot": 2, "asset_presence": shots[0]["asset_presence"]}
    requirements = coverage.build_requirements(ask["shots"], shots, assets)
    assert "SOURCE CAPTION" not in json.dumps(requirements)
    assert "holder_id" in json.dumps(requirements) and "left" in json.dumps(requirements)
    assert shots == original


def test_missing_prompt_uses_only_one_schema_repair_and_reports_metadata(monkeypatch):
    requests = []
    async def fake(system, user, stats, **kwargs):
        requests.append(kwargs)
        stats.last_response = {"finish_reason": "stop", "completion_tokens": 50, "text_chars": 100}
        return {"coverage": []}
    monkeypatch.setattr(adapt, "ask_json", fake)
    with pytest.raises(writer.WriterError, match='response metadata=.*finish_reason.*stop'):
        _run(_film(True))
    assert len(requests) == 2
    assert all(r["attempts"] == 1 for r in requests)


def test_wrapped_compact_output_still_requires_independent_semantic_review(monkeypatch):
    calls = []
    async def fake(system, user, stats, **kwargs):
        ask = json.loads(user)
        calls.append(ask)
        if "requirements" in ask:
            return _review(ask)
        draft = _draft(ask)
        grouped = {}
        for match in draft["coverage"]:
            key = (match["shot"], match["quote"])
            grouped.setdefault(key, []).append(match["requirement_id"])
        draft["coverage"] = [{"shot": shot, "quote": quote, "requirement_ids": ids}
                             for (shot, quote), ids in grouped.items()]
        return {"result": draft}
    monkeypatch.setattr(adapt, "ask_json", fake)
    result = _run(_film(True))
    assert len(calls) == 2
    assert result.coverage["status"] == "verified"
    assert len(result.coverage["matches"]) == len(result.coverage["requirements"])
    assert all("requirement_id" in m for m in result.coverage["matches"])


def test_truncation_gets_one_larger_bounded_retry_and_finish_diagnostics(monkeypatch):
    budgets = []
    async def fake(model, messages, **kwargs):
        budgets.append(kwargs["max_tokens"])
        return avis_text.Completion(text='{"prompt":"Partial", "coverage":[]', model=model,
                                   finish_reason="length", completion_tokens=kwargs["max_tokens"])
    monkeypatch.setattr(avis_text, "complete", fake)
    with pytest.raises(writer.WriterError, match='response metadata=.*finish_reason.*length'):
        _run(_film(True))
    assert budgets == [16000, 32000]


def test_finish_metadata_and_custom_output_budget_reach_adaptation_caller(monkeypatch):
    async def fake(model, messages, **kwargs):
        assert kwargs["max_tokens"] == 12345
        return avis_text.Completion(text='{"ok":true}', model=model, finish_reason="stop", completion_tokens=7)
    monkeypatch.setattr(avis_text, "complete", fake)
    stats = adapt.TextStats()
    assert asyncio.run(adapt.ask_json("s", "u", stats, model="test", fallback="", attempts=1, max_tokens=12345)) == {"ok": True}
    assert stats.last_response == {"model": "test", "finish_reason": "stop", "text_chars": 11,
        "prompt_tokens": 0, "completion_tokens": 7, "max_tokens": 12345,
        "json_type": "dict", "top_level_keys": ["ok"]}


@pytest.mark.parametrize("hand", [None, "uncertain"])
def test_visible_prop_with_unspecified_hand_remains_required_without_blocking_end_state(monkeypatch, hand):
    data = _film(True)
    for shot in data[1]:
        prop = shot["asset_presence"][2]
        if hand is None:
            prop.pop("hand")
        else:
            prop["hand"] = hand
    requests = []
    async def fake(system, user, stats, **kwargs):
        ask = json.loads(user)
        requests.append(ask)
        if "requirements" in ask:
            assert "Unknown detail alone is not a defect" in system
            props = [r for r in ask["requirements"] if r.get("asset_id") == "prop"]
            assert len(props) == 2 and all(r["visibility"] == "visible" for r in props)
            assert all(r["holder_id"] == "person" for r in props)
            assert "left hand" not in ask["prompt"] and "right hand" not in ask["prompt"]
            assert "hand unspecified" in ask["end_state"]
            return _review(ask)
        assert "uncertain VISIBILITY differs from a visible prop" in system
        draft = _draft(ask)
        draft["prompt"] = draft["prompt"].replace("left hand", "grasp, hand unspecified")
        draft["end_state"] = draft["end_state"].replace("left hand", "grasp, hand unspecified")
        for match in draft["coverage"]:
            match["quote"] = match["quote"].replace("left hand", "grasp, hand unspecified")
        return draft
    monkeypatch.setattr(adapt, "ask_json", fake)
    result = _run(data)
    assert result.coverage["status"] == "verified" and len(requests) == 2
    assert len([r for r in result.coverage["requirements"] if r.get("asset_id") == "prop"]) == 2


def test_adult_reference_does_not_disable_conservative_school_content_flag(monkeypatch):
    data = _film()
    data[2][0].update(role="adult student", design={"age_read": "Clearly24-year-old adult"})
    async def fake(system, user, stats, **kwargs):
        ask = json.loads(user)
        if "requirements" in ask:
            assert "not verified age evidence" in system
            assert "fully clothed, nonsexual restrictions still apply" in system
            return _review(ask)
        assert ask["school_age"] is True
        assert ask["references"][0]["age"] == "Clearly24-year-old adult"
        assert "not\na verified age fact" in system
        assert "adult styling never\nrelaxes that rule" in system
        return _draft(ask)
    monkeypatch.setattr(adapt, "ask_json", fake)
    assert _run(data).coverage["status"] == "verified"


@pytest.mark.parametrize("final_review_passes", [True, False])
def test_round_two_semantic_prompt_defect_gets_one_final_bounded_repair(monkeypatch, final_review_passes):
    calls = {"writer": 0, "review": 0}
    async def fake(system, user, stats, **kwargs):
        ask = json.loads(user)
        if "requirements" in ask:
            calls["review"] += 1
            defect = [{"requirement_id": "shot:1:asset:prop:3", "kind": "prompt_issue",
                       "message": "Preserve the approved prop detail in its shot."}]
            return _review(ask, None if final_review_passes and calls["review"] == 2 else defect)
        calls["writer"] += 1
        if calls["writer"] == 3:
            assert "approved prop detail" in ask["fix_exactly_these_problems"][0]
        draft = _draft(ask)
        if calls["writer"] == 1:
            draft["prompt"] = draft["prompt"].replace("DURATION: 6 seconds.", "DURATION: 0 seconds.")
        return draft
    monkeypatch.setattr(adapt, "ask_json", fake)
    if final_review_passes:
        result = _run(_film(True))
        assert result.coverage["writer_rounds"] == 3
        assert result.coverage["status"] == "verified"
    else:
        with pytest.raises(writer.WriterError, match="approved prop detail"):
            _run(_film(True))
    assert calls == {"writer": 3, "review": 2}


def test_repeated_deterministic_format_errors_still_stop_at_two_rounds(monkeypatch):
    calls = []
    async def fake(system, user, stats, **kwargs):
        ask = json.loads(user)
        calls.append(ask)
        assert "requirements" not in ask
        draft = _draft(ask)
        draft["prompt"] = draft["prompt"].replace("DURATION: 6 seconds.", "DURATION: 0 seconds.")
        return draft
    monkeypatch.setattr(adapt, "ask_json", fake)
    with pytest.raises(writer.WriterError, match="header must say"):
        _run(_film(True))
    assert len(calls) == 2


def test_round_two_source_issue_never_gets_a_third_model_repair(monkeypatch):
    calls = {"writer": 0, "review": 0}
    async def fake(system, user, stats, **kwargs):
        ask = json.loads(user)
        if "requirements" in ask:
            calls["review"] += 1
            return _review(ask, [{"requirement_id": "shot:1:asset:prop:3", "kind": "source_issue",
                                  "message": "Contradictory positive source holder claims."}])
        calls["writer"] += 1
        draft = _draft(ask)
        if calls["writer"] == 1:
            draft["prompt"] = draft["prompt"].replace("DURATION: 6 seconds.", "DURATION: 0 seconds.")
        return draft
    monkeypatch.setattr(adapt, "ask_json", fake)
    with pytest.raises(writer.WriterError, match="Source contract needs review"):
        _run(_film(True))
    assert calls == {"writer": 2, "review": 1}


def test_deterministic_full_shot_anchors_cannot_use_a_missing_or_other_shot():
    prompt = "[SHOT 1 — 00:00–00:02]\nThe lead waits.\n[SHOT 2 — 00:02–00:04]\nThe crowd stays."
    requirements = [{"id": "lead", "shot": 1}, {"id": "crowd", "shot": 2}, {"id": "missing", "shot": 3}]
    matches = coverage.full_shot_coverage(prompt, requirements)
    assert matches[0]["quote"] == "The lead waits."
    assert matches[1]["quote"] == "The crowd stays."
    assert matches[2]["quote"] == ""
    assert any("exact non-empty span inside shot 3" in x for x in coverage.check_coverage(prompt, requirements, matches))
    matches[0]["quote"] = matches[1]["quote"]
    assert any("exact non-empty span inside shot 1" in x for x in coverage.check_coverage(prompt, requirements, matches))


def test_full_shot_scope_rejects_forged_short_quote_even_when_it_is_literal(monkeypatch):
    async def fake(system, user, stats, **kwargs):
        ask = json.loads(user)
        if "requirements" in ask:
            return _review(ask)
        draft = _draft(ask)
        draft.pop("coverage")
        return draft
    monkeypatch.setattr(adapt, "ask_json", fake)
    result = _run(_film(True))
    assert result.coverage["evidence_scope"] == "full_shot"
    assert result.coverage["verification_method"] == "deterministic_shot_evidence_and_semantic_review"
    assert result.coverage["matches"] == coverage.full_shot_coverage(result.prompt, result.coverage["requirements"])
    assert coverage.verify_prompt_contract(result.prompt, result.coverage,
        expected_digest=result.contract_digest, actual_digest=result.contract_digest) == []
    altered = deepcopy(result.coverage)
    altered["matches"][0]["quote"] = "Medium shot."
    assert coverage.check_coverage(result.prompt, altered["requirements"], altered["matches"]) == []
    assert any("Full-shot evidence" in x for x in coverage.verify_prompt_contract(result.prompt, altered,
        expected_digest=result.contract_digest, actual_digest=result.contract_digest))


@pytest.mark.parametrize("label,flag,expected", [
    ("THEO, VOICEOVER, CONTINUING", "absent", True),
    ("THEO, CONTINUING", False, False),
    ("THEO", True, True),
    ("THEO, VOICEOVER", "absent", False),
])
def test_speaker_payload_normalizes_qualifiers_and_infers_continuation_only_when_absent(label, flag, expected):
    seq, shots, cast, extra, _, _ = _film(True)
    cast[0]["name"] = "Theo Lambert"
    dialogue = {"who": label, "line": "I was born with it."}
    if flag != "absent":
        dialogue["cont"] = flag
    shots[0]["dialogue"] = [dialogue]
    ask = writer._payload(seq, shots, [(0, 3), (3, 6)], 6, characters=cast, environment=None,
        look="cg3d", aspect_ratio=None, previous_state="", unsafe={}, school_age=False,
        reference_assets=extra, strict=True)
    said = ask["shots"][0]["dialogue"][0]
    assert said["who"] == label and said["continues_from_previous_shot"] is expected
    assert said["speaker_in_frame"] is True


def test_speaker_presence_uses_normalized_full_or_unambiguous_short_name():
    seq, shots, cast, extra, _, _ = _film(True)
    cast[0]["name"] = "Ava Shaw"
    shots[0]["dialogue"] = [{"who": "AVA, CONTINUING", "line": "A line."},
                            {"who": "AVA SHAW, CONTINUING", "line": "Another line."}]
    def payload():
        return writer._payload(seq, shots, [(0, 3), (3, 6)], 6, characters=cast, environment=None,
            look="cg3d", aspect_ratio=None, previous_state="", unsafe={}, school_age=False,
            reference_assets=extra, strict=True)["shots"][0]["dialogue"]
    assert all(d["speaker_in_frame"] is True for d in payload())
    cast.append({"key": "other-ava", "name": "Ava Stone"})
    said = payload()
    assert said[0]["speaker_in_frame"] is None  # ambiguous first name is not guessed
    assert said[1]["speaker_in_frame"] is True


@pytest.mark.parametrize("delivery", ["OFF SCREEN", "VOICEOVER"])
def test_offscreen_delivery_retains_required_partial_body_in_review_contract(delivery):
    seq, shots, cast, extra, assets, _ = _film(True)
    cast[0]["name"] = "Mira Sol"
    shots[0]["asset_presence"][0].update(visibility="partial", state="Shoulder and hand visible; face outside crop.")
    label = f"MIRA SOL, {delivery}, CONTINUING"
    shots[0]["dialogue"] = [{"who": label, "line": "Keep it closed."}]
    before = deepcopy(shots)
    ask = writer._payload(seq, shots, [(0, 3), (3, 6)], 6, characters=cast, environment=None,
        look="cg3d", aspect_ratio=None, previous_state="", unsafe={}, school_age=False,
        reference_assets=extra, strict=True)
    requirements = coverage.build_requirements(ask["shots"], shots, assets)
    spoken = next(r["value"] for r in requirements if r["id"] == "shot:1:dialogue:1")
    presence = next(r for r in requirements if r["id"] == "shot:1:asset:person:1")
    assert spoken["who"] == label and spoken["speaker_in_frame"] is True
    assert spoken["continues_from_previous_shot"] is True
    assert presence["visibility"] == "partial" and presence["state"] == before[0]["asset_presence"][0]["state"]
    assert shots == before  # Do not erase the shoulder to make delivery look consistent.


def test_strict_reference_keeps_all_approved_costume_details_and_extra_accessories_legacy_unchanged():
    seq, shots, cast, extra, _, _ = _film(True)
    cast[0].update(design={"hair": "Short dark hair", "costume": [
        {"piece": "Blazer", "colour": "charcoal and navy", "material": "wool", "detail": "Tailored seams"},
        {"piece": "Blouse", "colour": "white", "material": "opaque cloth", "detail": "Small triangular keyhole at upper sternum only"},
        {"piece": "Tie", "colour": "muted grey and navy plaid", "detail": "Narrow knot"},
        {"piece": "Shoes", "colour": "black", "detail": "Polished leather"},
        {"piece": "Aviator sunglasses", "colour": "black", "detail": "Dark lenses and metal frame"},
        {"piece": "Backpack", "colour": "navy", "detail": "Front full-body sheet view only; omit from rear head study"}],
        "accessories": ["Silver lapel pin", "Plain leather belt"]},
        wardrobe="Tailored suit; black aviator sunglasses; silver watch at left wrist")
    def keep(strict):
        return writer._payload(seq, shots, [(0, 3), (3, 6)], 6, characters=cast, environment=None,
            look="cg3d", aspect_ratio=None, previous_state="", unsafe={}, school_age=False,
            reference_assets=extra, strict=strict)["references"][0]["keep"]
    rich = keep(True)
    for phrase in ("Small triangular keyhole", "muted grey and navy plaid", "Aviator sunglasses",
                   "Dark lenses and metal frame", "Polished leather", "Silver lapel pin", "silver watch at left wrist"):
        assert phrase in rich
    assert keep(False) == writer.automation._preserve_line(cast[0])
    assert "reference sheet only, never video visibility" in writer._CONTRACT_SYSTEM
    assert "do not\noverride video-shot presence" in writer._REVIEW_SYSTEM


def test_semantic_repair_preserves_requirement_id_and_render_shot_target(monkeypatch):
    calls = {"writer": 0, "review": 0}
    async def fake(system, user, stats, **kwargs):
        ask = json.loads(user)
        if "requirements" in ask:
            calls["review"] += 1
            return _review(ask, [{"requirement_id": "shot:2:framing", "kind": "prompt_issue",
                                  "message": "The shot must keep the requested framing."}]
                           if calls["review"] == 1 else None)
        calls["writer"] += 1
        assert "Retain every supplied camera movement, focus change, angle, lens value" in system
        assert "Drop camera jargon and lens numbers" not in system
        if calls["writer"] == 2:
            problem = ask["fix_exactly_these_problems"][0]
            assert problem == "[SHOT 2; requirement_id=shot:2:framing] The shot must keep the requested framing."
        return _draft(ask)
    monkeypatch.setattr(adapt, "ask_json", fake)
    assert _run(_film(True)).coverage["status"] == "verified"
    assert calls == {"writer": 2, "review": 2}


def test_review_problem_keeps_unknown_requirement_id_without_inventing_a_shot():
    problem = writer._review_problem({"requirement_id": "unmapped-global", "message": "A global issue."},
                                     [{"id": "shot:2:framing", "shot": 2}])
    assert problem == "[requirement_id=unmapped-global] A global issue."
