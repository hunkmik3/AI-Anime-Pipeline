import copy

import pytest

from flowboard.services.video_analyzer import source_inventory as inv
from flowboard.services.video_analyzer import source_temporal_context as temporal


def _fixture():
    shots = [{"shot": number, "start": 2.0 * (number - 1), "end": 2.0 * number,
              "dialogue": "Do not treat speech as visual evidence", "dialogue_heard": "ASR text",
              "source": {"action": f"Visible pose {number}", "subtitle": "Visible caption",
                         "dialogue": "Nested speech", "custom": "Untrusted unrelated metadata"}}
             for number in range(1, 9)]
    evidence = [{"id": f"e{number}{suffix}", "shot": number,
                 "timestamp_s": 2.0 * (number - 1) + offset, "frame": f"{number}{suffix}.jpg"}
                for number in range(1, 9) for suffix, offset in (("a", .1), ("b", 1.0), ("c", 1.9))]
    inventory = {"assets": [
        {"id": "person", "kind": "character", "name": "Recurring person", "evidence_ids": ["e1a", "e8c"]},
        {"id": "other", "kind": "character", "name": "Other person", "evidence_ids": ["e3a"]}],
        "scenes": [{"id": scene, "shot_ids": list(range(1, 9)), "present_asset_ids": ["person", "other"]}
                   for scene in ("hall", "room")],
        "identity_aliases": {"old-person": "older-person", "older-person": "person", "loop": "loop"},
        "shots": {str(number): {"scene_id": "hall" if number <= 4 else "room",
                  "asset_presence": [{"asset_id": "person", "visibility": "visible",
                                      "evidence_ids": [f"e{number}{suffix}" for suffix in "abc"]}]}
                  for number in range(1, 9)}}
    inventory["shots"]["3"]["asset_presence"].append(
        {"asset_id": "other", "visibility": "visible", "evidence_ids": ["e3a"]})
    return shots, inventory, evidence


def _build():
    shots, inventory, evidence = _fixture()
    batch = [shots[3]]
    context, supplied = temporal.build_temporal_context(inv, batch, shots, inventory, evidence)
    return batch, inventory, context, supplied


def _link(**updates):
    return {"shot": 4, "asset_id": "person", "current_evidence_ids": ["e4a"],
            "context_evidence_ids": ["e3a"], "reason": "Distinctive face and continuous body position across these views.", **updates}


def test_neighbors_use_full_timeline_with_explicit_scene_boundaries_and_no_audio():
    shots, inventory, evidence = _fixture()
    original = copy.deepcopy((shots, inventory, evidence))
    context, supplied = temporal.build_temporal_context(inv, [shots[3]], shots, inventory, evidence)
    details = context["temporal_context"]
    assert [row["shot"] for row in details["neighbors"]] == [2, 3, 5, 6]
    assert [row["offset"] for row in details["neighbors"]] == [-2, -1, 1, 2]
    assert [row["boundary_label"] for row in details["neighbors"]] == ["same_scene", "same_scene", "scene_boundary", "scene_boundary"]
    assert details["preferred_neighbor_shots"]["4"] == [3, 2, 5, 6]
    for row in details["neighbors"]:
        assert len(row["evidence_ids"]) == 2
        assert "dialogue" not in row["source_shot"]
        assert "dialogue" not in row["source_shot"]["source"]
        assert "custom" not in row["source_shot"]["source"]
    assert set(context["proposed_inventory"]["shots"]) == {"4"}
    assert {p["asset_id"] for p in context["proposed_inventory"]["shots"]["4"]["asset_presence"]} == {"person"}
    assert details["policy"]["presence_from_neighbor"] is False
    assert (shots, inventory, evidence) == original
    assert len({frame["id"] for frame in supplied}) == len(supplied)


def test_evidence_roles_keep_current_frames_anchors_and_neighbors_distinct():
    _, _, context, supplied = _build()
    details = context["temporal_context"]
    assert details["evidence_roles"]["e4b"]["roles"] == ["current_frame"]
    assert details["evidence_roles"]["e1a"]["roles"] == ["identity_anchor"]
    assert details["evidence_roles"]["e1a"]["anchor_asset_ids"] == ["person"]
    assert "temporal_neighbor" in details["evidence_roles"]["e3a"]["roles"]
    assert details["evidence_roles"]["e3a"]["neighbor_for_shots"] == [4]
    assert {"e4a", "e4b", "e4c"} <= {frame["id"] for frame in supplied}
    assert details["canonical_aliases"]["old-person"] == "person"
    assert details["alias_paths"]["old-person"] == ["old-person", "older-person", "person"]
    assert details["unresolved_alias_ids"] == ["loop"]


def test_gap_limit_rejects_distant_neighbors_and_out_of_window_frames():
    shots, inventory, evidence = _fixture()
    for row in shots[4:]:
        row["start"] += 20; row["end"] += 20
    for frame in evidence:
        if frame["shot"] >= 5:
            frame["timestamp_s"] += 20
    context, _ = temporal.build_temporal_context(inv, [shots[3]], shots, inventory, evidence)
    assert [row["shot"] for row in context["temporal_context"]["neighbors"]] == [2, 3]
    assert not any("temporal_neighbor" in row["roles"] and row["shot"] >= 5
                   for row in context["temporal_context"]["evidence_roles"].values())


def test_noncontiguous_review_batch_does_not_make_distant_targets_neighbors():
    shots, inventory, evidence = _fixture()
    context, _ = temporal.build_temporal_context(inv, [shots[3], shots[7]], shots, inventory, evidence)
    rows = context["temporal_context"]["neighbors"]
    assert [row["shot"] for row in rows if row["for_shot"] == 4] == [2, 3, 5, 6]
    assert [row["shot"] for row in rows if row["for_shot"] == 8] == [6, 7]
    assert len({ref for row in rows if row["shot"] == 6 for ref in row["evidence_ids"]}) <= 2


def test_supplied_batch_middle_frames_can_anchor_same_identity_without_becoming_neighbors():
    shots, inventory, evidence = _fixture()
    batch = [shots[3], shots[7]]
    context, supplied = temporal.build_temporal_context(inv, batch, shots, inventory, evidence)
    issues, audit = temporal.validate_context_links(
        [_link(context_evidence_ids=["e8b"])], batch, inventory, supplied, context)
    assert not issues and audit[0]["valid"]
    assert not any(row["shot"] == 8 and row["for_shot"] == 4
                   for row in context["temporal_context"]["neighbors"])
    assert audit[0]["authorizes_presence_or_merge"] is False


@pytest.mark.parametrize("visibility", ["offscreen", "uncertain", "occluded"])
def test_supplied_frame_does_not_authorize_an_unobserved_identity(visibility):
    shots, inventory, evidence = _fixture()
    inventory["shots"]["8"]["asset_presence"][0]["visibility"] = visibility
    batch = [shots[3], shots[7]]
    context, supplied = temporal.build_temporal_context(inv, batch, shots, inventory, evidence)
    issues, _ = temporal.validate_context_links(
        [_link(context_evidence_ids=["e8b"])], batch, inventory, supplied, context)
    assert [issue["code"] for issue in issues] == ["context_link_context_evidence"]


def test_other_asset_or_unsupplied_middle_frame_remains_disallowed():
    batch, inventory, context, supplied = _build()
    for ref in ("e3b", "e8b"):
        issues, _ = temporal.validate_context_links(
            [_link(context_evidence_ids=[ref])], batch, inventory, supplied, context)
        assert any(issue["code"] == "context_link_context_evidence" for issue in issues)


def test_reviewer_keeps_original_reveal_anchor_when_proposal_resamples_identity():
    batch, inventory, original, supplied = _build()
    review = copy.deepcopy(original)
    review['temporal_context']['allowed_identity_context']['4']['person'] = []
    # A bad/foreign/new ID cannot be smuggled into this retained whitelist.
    original['temporal_context']['allowed_identity_context']['4']['person'] += ['missing', 'e4a']
    original['temporal_context']['allowed_identity_context']['4']['new-person'] = ['e3a']
    temporal.retain_supplied_identity_context(original, review, supplied)
    issues, _ = temporal.validate_context_links([_link()], batch, inventory, supplied, review)
    assert not issues
    refs = review['temporal_context']['allowed_identity_context']['4']['person']
    assert 'e4a' not in refs and 'missing' not in refs
    assert 'new-person' not in review['temporal_context']['allowed_identity_context']['4']


@pytest.mark.parametrize("reference", ["e3a", "e1a"])
def test_valid_contextual_identity_requires_both_current_and_same_asset_support(reference):
    batch, inventory, context, supplied = _build()
    original = copy.deepcopy(inventory)
    issues, audit = temporal.validate_context_links([_link(context_evidence_ids=[reference])], batch, inventory, supplied, context)
    assert not issues and audit[0]["valid"]
    assert audit[0]["validation_scope"] == "citation_structure_only"
    assert audit[0]["authorizes_presence_or_merge"] is False
    assert inventory == original


@pytest.mark.parametrize("change,code", [
    ({"shot": 99}, "invalid_context_link"),
    ({"shot": True}, "invalid_context_link"),
    ({"asset_id": "new-person"}, "context_link_unknown_asset"),
    ({"asset_id": "other"}, "context_link_presence"),
    ({"current_evidence_ids": []}, "context_link_current_evidence"),
    ({"current_evidence_ids": ["e3a"]}, "context_link_current_evidence"),
    ({"current_evidence_ids": ["e4a", "e3a"]}, "context_link_current_evidence"),
    ({"context_evidence_ids": []}, "context_link_context_evidence"),
    ({"context_evidence_ids": ["e4a"]}, "context_link_context_evidence"),
    ({"context_evidence_ids": ["e7b"]}, "context_link_context_evidence"),
    ({"reason": " "}, "context_link_reason"),
])
def test_invalid_links_cannot_create_identity_or_foreign_frame_presence(change, code):
    batch, inventory, context, supplied = _build()
    issues, audit = temporal.validate_context_links([_link(**change)], batch, inventory, supplied, context)
    assert code in {row["code"] for row in issues} and not audit[0]["valid"]


@pytest.mark.parametrize("visibility", ["offscreen", "uncertain", "occluded"])
def test_context_cannot_promote_nonvisible_current_asset(visibility):
    batch, inventory, context, supplied = _build()
    inventory["shots"]["4"]["asset_presence"][0]["visibility"] = visibility
    issues, _ = temporal.validate_context_links([_link()], batch, inventory, supplied, context)
    assert any(row["code"] == "context_link_presence" for row in issues)


def test_same_scene_union_and_other_people_frames_do_not_grant_context_support():
    batch, inventory, context, supplied = _build()
    # This known character has no current presence initially. A repair can add
    # a grounded partial presence, but its context must still belong to itself.
    inventory["shots"]["4"]["asset_presence"].append(
        {"asset_id": "other", "visibility": "partial", "evidence_ids": ["e4a"]})
    wrong = _link(asset_id="other", context_evidence_ids=["e2a"])
    issues, _ = temporal.validate_context_links([wrong], batch, inventory, supplied, context)
    assert any(row["code"] == "context_link_context_evidence" for row in issues)
    correct = _link(asset_id="other", context_evidence_ids=["e3a"])
    issues, audit = temporal.validate_context_links([correct], batch, inventory, supplied, context)
    assert not issues and audit[0]["valid"]


def test_removed_context_image_and_malformed_links_stay_invalid():
    batch, inventory, context, supplied = _build()
    issues, _ = temporal.validate_context_links([_link()], batch, inventory,
                                               [frame for frame in supplied if frame["id"] != "e3a"], context)
    assert any(row["code"] == "context_link_context_evidence" for row in issues)
    issues, _ = temporal.validate_context_links({"shot": 4}, batch, inventory, supplied, context)
    assert issues[0]["code"] == "invalid_context_link"
    issues, audit = temporal.validate_context_links([None], batch, inventory, supplied, context)
    assert issues and not audit[0]["valid"]
    assert temporal.validate_context_links(None, batch, inventory, supplied, context) == ([], [])
