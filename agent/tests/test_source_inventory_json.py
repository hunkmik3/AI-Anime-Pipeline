"""Bounded source JSON syntax recovery never substitutes for source validation."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json

import pytest

from flowboard.services import avis_text
from flowboard.services.video_analyzer import inventory_json, source_inventory as inv


def _wrong_shots_closer(value):
    correct = json.dumps(value, ensure_ascii=False)
    assert correct.endswith("}}")
    return correct[:-2] + "]}"


@pytest.mark.parametrize("wrapper", ["{}", "  {}\n", "```json\n{}\n```", "Response:\n{}"])
def test_one_mismatched_closer_has_exact_auditable_repair(wrapper):
    expected = {"assets": [], "scenes": [], "shots": {"1": {"state": 'A ] and } inside "quoted" text.'}}}
    raw = wrapper.format(_wrong_shots_closer(expected))

    parsed, correction = inventory_json.parse_object(raw)

    assert parsed == expected and correction is not None
    index = correction["offset"]
    assert raw[index] == correction["from"] == "]"
    assert correction["to"] == "}"
    assert correction["raw_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    fixed = raw[:index] + "}" + raw[index + 1:]
    assert correction["repaired_sha256"] == hashlib.sha256(fixed.encode()).hexdigest()
    assert sum(a != b for a, b in zip(raw, fixed)) == 1


def test_valid_json_is_returned_unchanged_without_a_repair_record():
    value = {"literal": "\\\"}]", "shots": {"2": {"evidence_ids": []}}}
    parsed, correction = inventory_json.parse_object(json.dumps(value))
    assert parsed == value and correction is None


@pytest.mark.parametrize("raw", [
    '{"shots":{"1":{}}',                         # Missing root delimiter.
    '{"shots":{"1":{"state":"unfinished ]}',  # Unterminated string.
    '{"a":[1,2},"b":{"c":3]}',                # Two wrong closers.
    '{"a":1 "b":2}',                          # Missing comma.
    '{"a":{1,2]}',                             # Wrong opener/invalid object contents.
    '{"a":1]]',                                # Extra closer, not a substitution.
    '{"a":1] trailing text',                   # Repaired candidate must be complete JSON.
    '[{"a":1}]',                              # Root must be an object.
])
def test_other_json_damage_is_rejected_without_inventing_content(raw):
    with pytest.raises(ValueError):
        inventory_json.parse_object(raw)


def test_live_response_is_corrected_once_without_a_second_model_call(tmp_path, monkeypatch):
    expected = {"shots": {"1": {"asset_presence": []}}}
    raw = _wrong_shots_closer(expected)
    calls = []
    journal, usage = {}, {}

    async def complete(model, messages, **kwargs):
        calls.append(model)
        return avis_text.Completion(text=raw, model=model, prompt_tokens=10, completion_tokens=5)

    monkeypatch.setattr(avis_text, "complete", complete)
    result = asyncio.run(inv._ask("system", {}, [], tmp_path, usage, journal=journal))

    assert result == expected and len(calls) == 1
    assert journal["responses"][0]["text"] == raw
    assert journal["responses"][0]["syntax_repair"]["policy"] == "single_mismatched_closer_v1"
    assert usage[inv.MODEL]["calls"] == 1


def test_failed_stage_reuses_latest_complete_saved_reply_without_spending_again(tmp_path, monkeypatch):
    earlier = {"shots": {"1": {"state": "earlier answer"}}}
    latest = {"shots": {"1": {"state": "latest answer"}}}
    raw = [_wrong_shots_closer(earlier), _wrong_shots_closer(latest)]
    entry = {
        "calls": {"repair": {"error": "ValueError: invalid JSON after repair turn",
                              "responses": [{"text": text} for text in raw]}},
        "trace": [], "usage": {inv.MODEL: {"calls": 2, "prompt_tokens": 20, "completion_tokens": 10}},
    }
    original_usage = copy.deepcopy(entry["usage"])
    saves = []

    async def forbidden(*args, **kwargs):
        raise AssertionError("Cached complete text must be reparsed before any provider call")

    monkeypatch.setattr(avis_text, "complete", forbidden)
    result = asyncio.run(inv._stage_call(
        entry, "repair", inv._EXTRACT, {}, [], tmp_path,
        asyncio.Semaphore(1), lambda: saves.append(True),
    ))

    state = entry["calls"]["repair"]
    assert result == latest and state["output"] == latest
    assert state["recovered_response_index"] == 1 and "error" not in state
    assert [r["text"] for r in state["responses"]] == raw
    assert state["history"][0]["error"].startswith("ValueError:")
    assert entry["usage"] == original_usage and saves
    assert any(t["stage"] == "json_syntax_repair" for t in entry["trace"])
    assert any(t["stage"] == "json_cached_response_recovered" for t in entry["trace"])
    assert "verified" not in state  # Syntax recovery is not a verification verdict.


def test_unrepairable_saved_reply_still_retries_only_its_failed_stage(tmp_path, monkeypatch):
    state = {"error": "ValueError: truncated JSON", "responses": [{"text": '{"shots":'}]}
    entry = {"calls": {"repair": state, "observe": {"output": {"already_paid": True}}},
             "usage": {}, "trace": []}
    calls = []

    async def complete(model, messages, **kwargs):
        calls.append(model)
        return avis_text.Completion(text='{"shots": {}}', model=model)

    monkeypatch.setattr(avis_text, "complete", complete)
    result = asyncio.run(inv._stage_call(
        entry, "repair", inv._EXTRACT, {}, [], tmp_path, asyncio.Semaphore(1), lambda: None,
    ))

    assert result == {"shots": {}} and len(calls) == 1
    assert entry["calls"]["observe"]["output"] == {"already_paid": True}
    assert state["history"][0]["responses"][0]["text"] == '{"shots":'
    assert "error" not in state


def test_syntax_recovery_does_not_approve_foreign_evidence():
    raw = '{"checks":[{"shot":1,"status":"verified","evidence_ids":["foreign"],"findings":[]}] ]'
    parsed = inv._parse_object(raw)

    reviewed, findings = inv._checks(
        parsed, [{"shot": 1}], [{"id": "own-frame", "shot": 1}], [],
    )

    assert reviewed == []
    assert any(f["code"] == "verification_missing" for f in findings)
