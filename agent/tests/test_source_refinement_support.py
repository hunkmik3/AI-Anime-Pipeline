import copy

import pytest

from flowboard.services.video_analyzer import source_inventory as inv
from flowboard.services.video_analyzer import source_refinement_support as support


def _shots():
    return [{"shot": 1, "start": 10.0, "end": 11.0, "dialogue": "Original speech",
             "dialogue_heard": "ASR original", "dialogue_track": [{"text": "line"}],
             "adaptation": {"action": "creative"}, "frames": ["frame.jpg"],
             "source": {"action": "old action", "subjects": ["person"], "subtitle": "Original subtitle",
                        "title_card": "Name card", "dialogue": "nested speech", "_model": "old-model",
                        "confidence": 0.6, "uncertain": ["action"], "custom_metadata": {"keep": True}}}]


def test_visual_updates_cannot_change_timing_speech_or_unrelated_metadata():
    shots = _shots()
    original = copy.deepcopy(shots)
    updates = [{"shot": 1, "start": 0, "end": 999, "dialogue": "replacement",
                "source": {"action": "visible corrected action", "subjects": ["person", "background group"],
                           "dialogue": "replacement nested speech", "_model": "invented",
                           "confidence": 1.0, "custom_metadata": {"keep": False}, "unknown": "ignored"}}]
    result, issues = support.apply_visual_updates(shots, updates)
    assert not issues
    expected = copy.deepcopy(original)
    expected[0]["source"].update(action="visible corrected action", subjects=["person", "background group"])
    assert result == expected and shots == original


def test_subtitle_and_title_corrections_require_exact_host_confirmation():
    shots = _shots()
    updates = [{"shot": 1, "source": {"subtitle": "Corrected visible dialogue", "title_card": None,
                                        "subtitle_confirmed": True}}]
    result, issues = support.apply_visual_updates(shots, updates)
    assert result[0]["source"]["subtitle"] == "Original subtitle"
    assert result[0]["source"]["title_card"] == "Name card"
    assert len(issues) == 2
    result, issues = support.apply_visual_updates(shots, updates,
        confirmed_text_updates={1: {"subtitle": "Corrected visible dialogue", "title_card": None}})
    assert not issues and result[0]["source"]["subtitle"] == "Corrected visible dialogue"
    assert result[0]["source"]["title_card"] is None
    assert result[0]["dialogue"] == "Original speech"
    result, issues = support.apply_visual_updates(shots, updates,
        confirmed_text_updates={1: {"subtitle": "Some other text"}})
    assert len(issues) == 2 and result[0]["source"]["subtitle"] == "Original subtitle"


@pytest.mark.parametrize("bad", [[{"shot": 1, "source": {}}, {"shot": 1, "source": {"action": "new"}}],
                                  [{"shot": True, "source": {"action": "new"}}],
                                  [{"shot": 2, "source": {"action": "new"}}],
                                  [{"shot": 1, "source": "invalid"}]])
def test_ambiguous_or_foreign_updates_do_not_overwrite_shots(bad):
    shots = _shots()
    result, issues = support.apply_visual_updates(shots, bad)
    assert result == shots and issues


def test_invalid_visual_values_are_reported_without_dropping_uncertainty():
    shots = _shots()
    result, issues = support.apply_visual_updates(shots, [{"shot": 1, "source": {
        "subjects": "not a list", "uncertain": None, "action": None, "vfx": None}}])
    assert len(issues) == 3
    assert result[0]["source"]["uncertain"] == ["action"]
    assert result[0]["source"]["vfx"] is None


@pytest.mark.parametrize("crop", [[100, 100, 500, 500], [0, 0, 0, 1], [0, 0, 1.2, 1],
                                  "pixels", [0, 0, True, 1], [0, 0, float("nan"), 1]])
def test_invalid_crop_falls_back_to_full_frame_without_guessing_coordinates(crop):
    requests = [{"shot": 1, "timestamp_s": 10.5, "crop": crop, "reason": "Read object"}]
    original = copy.deepcopy(requests)
    safe, notes = support.prepare_frame_requests(inv, requests, _shots(), 30)
    assert safe == [{"shot": 1, "timestamp_s": 10.5, "crop": None, "reason": "Read object"}]
    assert notes[0]["code"] == "review_crop_full_frame_fallback"
    assert notes[0]["shot"] == 1
    assert requests[0]["crop"] is crop


@pytest.mark.parametrize("shot,at", [(2, 10.5), (1, 0.5), (1, 11.0), (True, 10.5), (1, float("inf"))])
def test_invalid_shot_or_time_never_uses_crop_fallback(shot, at):
    safe, notes = support.prepare_frame_requests(inv, [{"shot": shot, "timestamp_s": at, "crop": [100, 100, 20, 20]}], _shots(), 30)
    assert safe == [] and notes[0]["code"] == "invalid_review_request"


def test_valid_crop_is_preserved_and_identical_requests_are_deduplicated():
    row = {"shot": 1, "timestamp_s": 10.5, "crop": [0.1, 0.2, 0.3, 0.4], "reason": "Read label"}
    safe, notes = support.prepare_frame_requests(inv, [row, copy.deepcopy(row)], _shots(), 30)
    assert safe == [row] and notes == []


def test_compact_context_keeps_related_profiles_both_proposal_anchors_and_all_own_frames():
    inventory = {"assets": [
        {"id": "person", "kind": "character", "name": "Canonical", "member_ids": [], "depends_on_asset_ids": [], "evidence_ids": ["e0"]},
        {"id": "photo", "kind": "prop", "depends_on_asset_ids": ["person"], "evidence_ids": ["e1"]},
        {"id": "extra", "kind": "character", "name": "Unrelated", "evidence_ids": ["e3"]}],
        "scenes": [{"id": "room", "shot_ids": [1, 3], "present_asset_ids": ["person", "photo", "extra"]}],
        "shots": {"1": {"scene_id": "room", "asset_presence": [{"asset_id": "photo", "holder_id": "person", "contains_ids": []}]}}}
    evidence = [{"id": f"e{n}", "shot": n, "frame": f"{n}.jpg"} for n in (0, 1, 2, 3)]
    evidence.append({"id": "e1-extra", "shot": 1, "frame": "1-extra.jpg"})
    proposals = [{"id": "person", "name": "Proposed refinement", "evidence_ids": ["e2"]}]
    context, frames = support.build_visual_context(inv, _shots(), inventory, evidence, candidate_profiles=proposals)
    assert {a["id"] for a in context["proposed_inventory"]["assets"]} == {"person", "photo", "extra"}
    assert next(a for a in context["proposed_inventory"]["assets"] if a["id"] == "person")["name"] == "Canonical"
    assert context["candidate_profiles"][0]["name"] == "Proposed refinement"
    assert {f["id"] for f in frames} == {"e0", "e1", "e1-extra", "e2", "e3"}
    assert not context["other_asset_index"]
    assert context["proposed_inventory"]["shots"]["1"]["asset_presence"] == inventory["shots"]["1"]["asset_presence"]
    assert context["proposed_inventory"]["scenes"][0]["shot_ids"] == [1]
    assert context["proposed_inventory"]["scenes"][0]["present_asset_ids"] == ["photo"]
    assert context["scene_context"][0]["shot_ids"] == [1, 3]
    assert context["scene_context"][0]["present_asset_ids"] == ["person", "photo", "extra"]
    assert inventory["scenes"][0]["shot_ids"] == [1, 3]
    assert "dialogue" not in context["source_shots"][0]
    assert context["source_shots"][0]["source"]["subtitle"] == "Original subtitle"


def test_repair_batches_split_scene_boundaries_and_noncontiguous_targets():
    shots = [{"shot": n} for n in [10, 11, 12, 43, 44, 45, 46, 47]]
    inventory = {"shots": {str(n): {"scene_id": "a" if n < 44 else "b"} for n in [10, 11, 12, 43, 44, 45, 46, 47]}}
    result = support.contiguous_batches(shots, inventory, 3)
    assert [[s['shot'] for s in batch] for batch in result] == [[10, 11, 12], [43], [44, 45, 46], [47]]
    assert support.contiguous_batches([], inventory, 3) == []
