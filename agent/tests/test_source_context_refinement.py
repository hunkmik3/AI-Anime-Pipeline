"""Mocked provider integration through real temporal/refinement host validators."""
import asyncio
import copy
import json

import pytest

from flowboard.services import avis_text
from flowboard.services.video_analyzer import source_refinement as refine
from tests.test_source_refinement import _case


def _link(asset="market-lead", *, reason="Distinctive face and body position continue across the cut."):
    return {"shot": 1, "asset_id": asset, "current_evidence_ids": ["shot-1-frame-1"],
            "context_evidence_ids": ["shot-2-frame-1"], "reason": reason}


def _install(monkeypatch, *, writer=None, checker=None, keep_first_issue=False):
    from flowboard.services.video_analyzer import source_asset_layers, source_entity_cleanup, source_protocol_review
    calls = {"writer": [], "checker": [], "layers": [], "entities": []}

    async def layers(inv, inventory, *args, **kwargs):
        calls["layers"].append(copy.deepcopy(inventory))
        return copy.deepcopy(inventory), [], {"moved_asset_ids": []}

    async def entities(inv, inventory, *args, **kwargs):
        calls["entities"].append(copy.deepcopy(inventory))
        return copy.deepcopy(inventory), [], {"aliases": {}, "status": "complete"}

    # This suite exercises source refinement, not the separate protocol-review
    # stage. Do not let another mocked model waive a deliberately invalid link.
    async def protocol(video, work_dir, analysis, **kwargs):
        return analysis

    async def complete(model, messages, **kwargs):
        payload = json.loads(messages[1]["content"][0]["text"])
        if "proposed_source_shots" in payload:
            calls["checker"].append(copy.deepcopy(payload))
            checks = []
            for shot in payload["proposed_source_shots"]:
                number = shot["shot"]
                notes = ["Visible hand remains unresolved."] if keep_first_issue and number == 1 else []
                checks.append({"shot": number, "status": "needs_review" if notes else "verified",
                    "evidence_ids": [f"shot-{number}-frame-1"], "findings": notes,
                    "source_description_findings": [], "screen_graphics_seen": [],
                    "resolutions": [{"finding_id": finding["finding_id"], "decision": "resolved",
                        "reason": "Current source frames support the corrected visible fact.",
                        "evidence_ids": [f"shot-{number}-frame-1"]}
                        for finding in payload["prior_findings"] if finding["shot"] == number],
                    "confirmed_text_updates": payload.get("proposed_text_updates", {}).get(str(number), {})})
            data = {"checks": checks, "review_requests": [], "context_links": []}
            if checker:
                checker(data, payload)
        else:
            calls["writer"].append(copy.deepcopy(payload))
            data = copy.deepcopy(payload["draft_inventory"])
            data["assets"] = []  # A valid empty DELTA, not an empty canonical catalog.
            data["review_requests"] = []
            data["context_links"] = []
            data["visual_updates"] = [{"shot": 1, "source": {"action": "Corrected visible action"}}] if "1" in data["shots"] else []
            if writer:
                writer(data, payload)
        return avis_text.Completion(text=json.dumps(data), model=model)

    monkeypatch.setattr(source_asset_layers, "classify_layers", layers)
    monkeypatch.setattr(source_entity_cleanup, "reconcile_entities", entities)
    monkeypatch.setattr(source_protocol_review, "review", protocol)
    monkeypatch.setattr(avis_text, "complete", complete)
    return calls


def _run(case, **kwargs):
    return asyncio.run(refine.refine(*case, **kwargs))


def test_empty_asset_delta_keeps_full_relevant_canonical_catalog_for_checker(tmp_path, monkeypatch):
    case = _case(tmp_path)
    original = copy.deepcopy(case[2])
    calls = _install(monkeypatch)
    result = _run(case)
    assert result["source_verification"]["status"] == "verified"
    expected = {asset["id"]: asset for asset in original["scene_inventory"]["assets"]}
    assert calls["checker"]
    for payload in calls["checker"]:
        actual = {asset["id"]: asset for asset in payload["proposed_inventory"]["assets"]}
        assert actual == expected
        assert payload["identity_catalog"] == payload["proposed_inventory"]["assets"]
        assert set(payload["proposed_inventory"]["shots"]) == {"1", "2"}
    assert result["shots"][0]["dialogue"] == original["shots"][0]["dialogue"]
    assert case[2] == original


@pytest.mark.parametrize("review", ["valid", "missing", "foreign_current"])
def test_contextual_writer_link_requires_its_own_independent_valid_checker_link(tmp_path, monkeypatch, review):
    case = _case(tmp_path)

    def writer(data, payload):
        data["context_links"] = [_link(reason="Writer-only distinctive continuity explanation.")]

    def checker(data, payload):
        if review != "missing":
            link = _link(reason="Independent reviewer observes the same distinctive face and continuing body orientation.")
            if review == "foreign_current":
                link["current_evidence_ids"] = ["shot-2-frame-1"]
            data["context_links"] = [link]

    calls = _install(monkeypatch, writer=writer, checker=checker)
    result = _run(case)
    report = result["source_verification"]
    assert (report["status"] == "verified") is (review == "valid")
    for payload in calls["checker"]:
        assert payload["required_context_reviews"] == [{"shot": 1, "asset_id": "market-lead"}]
        assert "Writer-only distinctive continuity explanation." not in json.dumps(payload)
        assert "shot-2-frame-1" in payload["temporal_context"]["allowed_identity_context"]["1"]["market-lead"]
    audit = report["refinement"]["context_links"]
    assert any(row["stage"].startswith("writer_") and row["links"][0]["valid"] for row in audit)
    if review != "valid":
        assert 1 in report["unresolved_shots"]
        assert any(finding["code"] == "context_identity_unconfirmed" for finding in report["findings"])
    else:
        assert any(row["stage"].startswith("checker_") and row["links"][0]["valid"] for row in audit)


@pytest.mark.parametrize("malformed", [{"shot": 1, "asset_id": "market-lead"}, [None], ["not a link"]])
def test_malformed_writer_context_links_reach_validator_and_cannot_silently_pass(tmp_path, monkeypatch, malformed):
    case = _case(tmp_path)

    def writer(data, payload):
        data["context_links"] = copy.deepcopy(malformed)

    calls = _install(monkeypatch, writer=writer)
    result = _run(case)
    report = result["source_verification"]
    assert report["status"] == "needs_review" and not report["retryable"]
    assert any(finding["code"] == "invalid_context_link" for finding in report["findings"])
    for payload in calls["checker"]:
        assert any(finding["code"] == "invalid_context_link" for finding in payload["structural_findings"])
        assert payload["required_context_reviews"] == []


def test_new_definition_cannot_gain_established_identity_through_context(tmp_path, monkeypatch):
    case = _case(tmp_path)

    def writer(data, payload):
        data["assets"] = [{"id": "new-person", "kind": "character", "name": "Unestablished person",
                           "description": "A proposed identity borrowing an existing person's context.",
                           "member_ids": [], "depends_on_asset_ids": [],
                           "evidence_ids": ["shot-1-frame-1", "shot-2-frame-1"]}]
        row = data["shots"]["1"]
        row["asset_presence"] = [presence for presence in row["asset_presence"]
                                  if presence["asset_id"] in {asset["id"] for asset in case[2]["scene_inventory"]["assets"]}]
        row["asset_presence"].append({"asset_id": "new-person", "visibility": "visible",
                                      "position": "foreground", "state": "Current proposed appearance",
                                      "contains_ids": [], "evidence_ids": ["shot-1-frame-1"]})
        data["context_links"] = [_link("new-person")]

    calls = _install(monkeypatch, writer=writer)
    result = _run(case)
    report = result["source_verification"]
    assert report["status"] == "needs_review" and not report["retryable"]
    assert any(finding["code"] == "context_link_unknown_asset" for finding in report["findings"])
    established = {asset["id"] for asset in case[2]["scene_inventory"]["assets"]}
    for payload in calls["checker"]:
        new_ids = {asset["id"] for asset in payload["proposed_inventory"]["assets"]} - established
        assert new_ids  # The definition exists, so this tests eligibility rather than a missing asset.
        temporal = payload["temporal_context"]
        assert new_ids.isdisjoint(temporal["canonical_asset_ids"])
        assert all(new_ids.isdisjoint(by_asset) for by_asset in temporal["allowed_identity_context"].values())
        assert payload["required_context_reviews"] == []


def test_targeted_context_preserves_verified_neighbor_and_graphic_metadata_shape(tmp_path, monkeypatch):
    case = _case(tmp_path)
    inventory = case[2]["scene_inventory"]
    inventory["identity_aliases"] = {"old-lead": "market-lead"}
    inventory["screen_graphics"] = [{"id": "branding", "description": "Overlay", "evidence_ids": ["shot-1-frame-1"]}]
    for number, row in inventory["shots"].items():
        row["screen_graphics"] = [{"asset_id": "branding", "position": "lower center",
                                    "visibility": "visible", "evidence_ids": [f"shot-{number}-frame-1"]}]
    _install(monkeypatch, keep_first_issue=True)
    first = _run(case)
    assert first["source_verification"]["unresolved_shots"] == [1]
    prior_shot = copy.deepcopy(first["shots"][1])
    prior_inventory_row = copy.deepcopy(first["scene_inventory"]["shots"]["2"])
    calls = _install(monkeypatch)
    result = _run((case[0], case[1], first), only_unresolved=True)
    report = result["source_verification"]
    assert report["status"] == "verified"
    assert report["refinement"]["scope"]["processed_shots"] == [1]
    assert report["refinement"]["scope"]["retained_verified_shots"] == [2]
    assert not calls["layers"] and not calls["entities"]
    assert result["shots"][1] == prior_shot
    assert json.dumps(result["scene_inventory"]["shots"]["2"]) == json.dumps(prior_inventory_row)
    assert "contains_ids" not in result["scene_inventory"]["shots"]["2"]["screen_graphics"][0]
    for payload in calls["checker"]:
        assert [shot["shot"] for shot in payload["proposed_source_shots"]] == [1]
        assert set(payload["proposed_inventory"]["shots"]) == {"1"}
        assert [row["shot"] for row in payload["temporal_context"]["neighbors"]] == [2]


def test_explicit_selection_rechecks_verified_shot_without_touching_other_unresolved_work(tmp_path, monkeypatch):
    case = _case(tmp_path)
    _install(monkeypatch, keep_first_issue=True)
    first = _run(case)
    assert first["source_verification"]["unresolved_shots"] == [1]
    before = copy.deepcopy(first)

    def writer(data, payload):
        assert set(data["shots"]) == {"2"}
        data["visual_updates"] = [{"shot": 2, "source": {"action": "Explicit selected-shot correction"}}]

    calls = _install(monkeypatch, writer=writer)
    result = _run((case[0], case[1], first), selected_shots=[2])
    report = result["source_verification"]
    assert report["refinement"]["scope"]["mode"] == "selected"
    assert report["refinement"]["scope"]["processed_shots"] == [2]
    assert report["status"] == "needs_review" and report["unresolved_shots"] == [1]
    assert result["shots"][1]["source"]["action"] == "Explicit selected-shot correction"
    assert result["shots"][0] == before["shots"][0]
    assert result["scene_inventory"]["shots"]["1"] == before["scene_inventory"]["shots"]["1"]
    fields = ("finding_id", "code", "shot", "message", "asset_id")
    old_issues = [tuple(f.get(field) for field in fields) for f in before["source_verification"]["findings"] if f.get("shot") == 1]
    retained = [tuple(f.get(field) for field in fields) for f in report["findings"] if f.get("shot") == 1]
    assert retained == old_issues and retained
    assert all([shot["shot"] for shot in payload["proposed_source_shots"]] == [2] for payload in calls["checker"])
    assert not calls["layers"] and not calls["entities"]
    assert first == before


@pytest.mark.parametrize("changed", ["inventory", "source_shot", "source_frame", "source_video"])
def test_explicit_selection_rejects_changed_source_binding_before_any_model_call(tmp_path, monkeypatch, changed):
    case = _case(tmp_path)
    _install(monkeypatch)
    first = _run(case)
    if changed == "inventory":
        first["scene_inventory"]["shots"]["1"]["asset_presence"][0]["state"] = "Changed outside source verification"
    elif changed == "source_shot":
        first["shots"][0]["source"]["action"] = "Changed source description"
    elif changed == "source_frame":
        (case[1] / first["shots"][0]["frames"][0]).write_bytes(b"different source pixels")
    else:
        case[0].write_bytes(b"different source video")
    calls = _install(monkeypatch)
    with pytest.raises(ValueError, match="unchanged source-bound"):
        _run((case[0], case[1], first), selected_shots=[2])
    assert all(not values for values in calls.values())


@pytest.mark.parametrize("selection", [[], [999], [True], ["1"], (1,)])
def test_explicit_selection_rejects_empty_unknown_or_invalid_shot_numbers(tmp_path, monkeypatch, selection):
    case = _case(tmp_path)
    calls = _install(monkeypatch)
    with pytest.raises(ValueError, match="nonempty list"):
        _run(case, selected_shots=selection)
    assert all(not values for values in calls.values())


def test_subset_verification_cannot_clear_a_global_blocker(tmp_path, monkeypatch):
    case = _case(tmp_path)
    _install(monkeypatch)
    first = _run(case)
    global_issue = {"code": "registry_conflict", "shot": None,
                    "message": "A global source registry question remains unverified across the film."}
    first["source_verification"].update(status="needs_review", unresolved_shots=[1, 2], findings=[global_issue])
    old_shot = copy.deepcopy(first["shots"][0])
    old_row = copy.deepcopy(first["scene_inventory"]["shots"]["1"])
    calls = _install(monkeypatch)
    result = _run((case[0], case[1], first), selected_shots=[2])
    report = result["source_verification"]
    assert report["refinement"]["scope"]["processed_shots"] == [2]
    assert report["status"] == "needs_review" and report["unresolved_shots"] == [1, 2]
    assert any(f.get("shot") is None and f["code"] == global_issue["code"] and
               f["message"] == global_issue["message"] for f in report["findings"])
    assert result["shots"][0] == old_shot and result["scene_inventory"]["shots"]["1"] == old_row
    assert all([shot["shot"] for shot in payload["proposed_source_shots"]] == [2] for payload in calls["checker"])


@pytest.mark.parametrize("before_visibility", ["visible", "partial"])
@pytest.mark.parametrize("after_visibility", [None, "offscreen", "occluded"])
def test_presence_removal_or_out_of_view_reclassification_asks_current_shot_question(before_visibility, after_visibility):
    before = {"shots": {"1": {"asset_presence": [{"asset_id": "object", "visibility": before_visibility,
                                                  "evidence_ids": ["own-1"]}]},
                        "2": {"asset_presence": [{"asset_id": "outside-batch", "visibility": "visible"}]}}}
    after = copy.deepcopy(before)
    after["shots"]["2"]["asset_presence"] = []
    if after_visibility is None:
        after["shots"]["1"]["asset_presence"] = []
    else:
        after["shots"]["1"]["asset_presence"][0]["visibility"] = after_visibility
    originals = copy.deepcopy((before, after))
    questions = refine._presence_change_questions(before, after, [{"shot": 1}])
    assert len(questions) == 1
    assert questions[0]["code"] == "presence_change_review"
    assert questions[0]["shot"] == 1 and questions[0]["asset_id"] == "object"
    assert "own-shot" in questions[0]["message"]
    assert (before, after) == originals  # Asking a question never restores old presence.


@pytest.mark.parametrize("before_visibility,after_visibility", [
    ("visible", "visible"), ("partial", "partial"), ("partial", "visible"),
    ("offscreen", "offscreen"), ("offscreen", None), ("visible", "uncertain"),
])
def test_unchanged_offscreen_and_explicit_uncertainty_do_not_create_removal_questions(before_visibility, after_visibility):
    before = {"shots": {"1": {"asset_presence": [{"asset_id": "person", "visibility": before_visibility}]}}}
    after = copy.deepcopy(before)
    if after_visibility is None:
        after["shots"]["1"]["asset_presence"] = []
    else:
        after["shots"]["1"]["asset_presence"][0]["visibility"] = after_visibility
    assert refine._presence_change_questions(before, after, [{"shot": 1}]) == []


@pytest.mark.parametrize("confirm_removal", [True, False])
def test_removed_visible_asset_requires_explicit_independent_disposition_without_forcing_presence(tmp_path, monkeypatch, confirm_removal):
    case = _case(tmp_path)

    def writer(data, payload):
        data["shots"]["1"]["asset_presence"] = [presence for presence in data["shots"]["1"]["asset_presence"]
                                                  if presence["asset_id"] != "market-prop"]

    def checker(data, payload):
        questions = [finding for finding in payload["prior_findings"] if finding["code"] == "presence_change_review"]
        assert any(finding["shot"] == 1 and finding["asset_id"] == "market-prop" for finding in questions)
        if not confirm_removal:
            removal_ids = {finding["finding_id"] for finding in questions}
            for check in data["checks"]:
                check["resolutions"] = [row for row in check["resolutions"] if row["finding_id"] not in removal_ids]

    calls = _install(monkeypatch, writer=writer, checker=checker)
    result = _run(case)
    report = result["source_verification"]
    assert (report["status"] == "verified") is confirm_removal
    assert not any(presence["asset_id"] == "market-prop" for presence in result["scene_inventory"]["shots"]["1"]["asset_presence"])
    assert calls["checker"]
    if not confirm_removal:
        assert 1 in report["unresolved_shots"]
        assert any(finding["code"] in {"presence_change_review", "resolution_incomplete"} for finding in report["findings"])
