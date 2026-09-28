"""Explicit production adaptations preserve verified source evidence and coverage."""
import asyncio
from copy import deepcopy
import json

import pytest

from flowboard.services import production_adaptation as adaptation
from flowboard.services import prompt_coverage as coverage
from flowboard.services import prompt_writer as writer
from flowboard.services.video_analyzer import adapt


def _case():
    assets = [{"id": "person", "kind": "character", "name": "Source person", "reference_required": True},
              {"id": "box", "kind": "prop", "name": "Closed box", "reference_required": True}]
    presence = [{"asset_id": "person", "visibility": "visible", "state": "Uniform dissolves to reveal lingerie.",
                 "position": "foreground", "evidence_ids": ["f2"]},
                {"asset_id": "box", "visibility": "partial", "state": "closed", "position": "waist",
                 "holder_id": "person", "hand": "left", "contains_ids": [], "evidence_ids": ["f2"]}]
    observation = {"scene_id": "hall", "asset_presence": deepcopy(presence), "evidence_ids": ["f2"]}
    shot = {"source_shots": [2], "source_appearances": [{"source_shot": 2, **observation}],
            "source_evidence": ["f2"], "asset_presence": [{**p, "source_shot": 2} for p in presence],
            "duration_s": 3, "framing": "MS", "character_keys": ["person"],
            "action": ["The uniform dissolves to reveal lingerie."], "dialogue": []}
    shot["production_adaptation"] = {"schema_version": 1, "reason": "Replace source lingerie effect with a fully clothed object scan.",
        "source_digest": adaptation.source_digest(shot),
        "shot_overrides": {"action": ["A soft scan passes over the closed box while the adult remains fully clothed."],
                           "vfx": ["A cyan outline traces the box exterior."]},
        "asset_presence": [{"asset_id": "person", "source_shot": 2,
                            "state": "Adult wearing an opaque tailored suit, unchanged throughout."}]}
    report = {"status": "verified", "method": "source_frames", "digest": "inventory-digest",
              "reviewed_shots": [2], "unresolved_shots": [], "findings": [],
              "evidence": [{"id": "f2", "shot": 2}],
              "shot_digests": {"2": coverage._source_digest(observation)},
              "asset_digests": {a["id"]: coverage._source_digest(a) for a in assets}}
    cast = [{"key": "person", "source_asset_id": "person", "name": "Adult Lead", "role": "adult student",
             "ref_label": "@image1", "ref_url": "https://test/lead.png"}]
    refs = [{"id": "box", "kind": "prop", "name": "Closed Box", "ref_label": "@image2", "ref_url": "https://test/box.png"}]
    return shot, assets, report, cast, refs


def test_explicit_target_states_preserve_raw_source_and_immutable_relations():
    shot, assets, report, cast, refs = _case()
    before = deepcopy(shot)
    assert coverage.validate_source_contract([shot], assets, report, cast + refs) == []
    target = adaptation.apply(shot)
    assert target["asset_presence"][0]["state"].startswith("Adult wearing")
    assert target["source_appearances"] == shot["source_appearances"]
    assert target["asset_presence"][1] == shot["asset_presence"][1]
    assert target["production_appearances"][0]["asset_presence"][0]["state"].startswith("Adult wearing")
    assert shot == before
    assert adaptation.apply(target) == target


@pytest.mark.parametrize("change", [
    {"visibility": "offscreen"}, {"holder_id": "someone"}, {"hand": "right"},
    {"contains_ids": ["extra"]}, {"evidence_ids": []}, {"asset_id": "new-person"},
    {"source_shot": 99},
])
def test_overlay_cannot_hide_add_reidentify_or_reassign_source_presence(change):
    shot, assets, report, cast, refs = _case()
    shot["production_adaptation"]["asset_presence"][0].update(change)
    assert coverage.validate_source_contract([shot], assets, report, cast + refs)


def test_anchor_cannot_replace_source_digest_validation():
    shot, assets, report, cast, refs = _case()
    shot["source_appearances"][0]["asset_presence"][0]["state"] = "edited source"
    assert any("stale" in x for x in coverage.validate_source_contract([shot], assets, report, cast + refs))
    shot["production_adaptation"]["source_digest"] = adaptation.source_digest(shot)
    assert any("changed after verification" in x for x in coverage.validate_source_contract([shot], assets, report, cast + refs))
    assets[0]["description"] = "edited identity"
    assert any("Source asset 'person' changed" in x for x in coverage.validate_source_contract([shot], assets, report, cast + refs))


def test_overlay_requires_reason_verified_anchor_and_disallows_unknown_controls():
    shot, assets, report, cast, refs = _case()
    del report["shot_digests"]
    shot["production_adaptation"]["reason"] = " "
    shot["production_adaptation"]["shot_overrides"]["source_shots"] = [99]
    shot["production_adaptation"]["approved_by"] = "invented owner"
    issues = coverage.validate_source_contract([shot], assets, report, cast + refs)
    assert any("reason" in x for x in issues)
    assert any("verified source shot digests" in x for x in issues)
    assert any("source_shots" in x for x in issues)
    assert any("unsupported fields" in x for x in issues)


def test_existing_camera_and_lighting_string_or_structured_forms_are_supported():
    shot, *_ = _case()
    for camera, lighting in [("Static eye-level", "Soft window light"),
                             ({"movement": "Static", "angle": "eye-level"}, {"key": "Window light"})]:
        shot["production_adaptation"]["shot_overrides"].update(camera=camera, lighting=lighting)
        assert adaptation.validate(shot) == []
        target = adaptation.apply(shot)
        assert target["camera"] == camera and target["lighting"] == lighting


def test_overlay_cannot_certify_unresolved_source_or_legacy_input(monkeypatch):
    shot, assets, report, cast, refs = _case()
    report["unresolved_shots"] = [2]
    assert any("not verified" in x for x in coverage.validate_source_contract([shot], assets, report, cast + refs))
    async def forbidden(*args, **kwargs):
        pytest.fail("Unverified adaptation must not reach a model")
    monkeypatch.setattr(adapt, "ask_json", forbidden)
    with pytest.raises(writer.WriterError, match="verified-source contract"):
        asyncio.run(writer.write_clip_prompt({}, [shot], characters=cast, environment=None))


def test_stale_review_metadata_cannot_hide_uncertainty_in_partially_verified_report():
    shot, assets, report, cast, refs = _case()
    shot["source_appearances"][0]["asset_presence"][0]["visibility"] = "uncertain"
    shot["asset_presence"][0]["visibility"] = "uncertain"
    observation = {k: v for k, v in shot["source_appearances"][0].items() if k != "source_shot"}
    report.update(status="needs_review", scope="sampled_visual", review={"accepted_by": "old-reviewer"})
    report["shot_digests"]["2"] = coverage._source_digest(observation)
    shot["production_adaptation"]["source_digest"] = adaptation.source_digest(shot)
    assert coverage.source_readiness_issues([shot], report) == []
    issues = coverage.validate_source_contract([shot], assets, report, cast + refs)
    assert any("resolve uncertain visibility" in x for x in issues)


def test_merged_source_occurrences_remain_distinct_and_are_not_silently_dropped():
    shot, *_ = _case()
    shot["source_shots"] = [2, 3]
    second = deepcopy(shot["source_appearances"][0])
    second["source_shot"] = 3
    second["asset_presence"][0]["state"] = "different later pose"
    shot["source_appearances"].append(second)
    shot["asset_presence"] += [{**p, "source_shot": 3} for p in second["asset_presence"]]
    shot["production_adaptation"]["source_digest"] = adaptation.source_digest(shot)
    target = adaptation.apply(shot)
    assert len(target["asset_presence"]) == 4
    assert target["asset_presence"][2]["state"] == "different later pose"
    assert target["production_appearances"][1]["asset_presence"][0]["state"] == "different later pose"
    shot["production_adaptation"]["asset_presence"] *= 2
    assert any("unique" in x for x in adaptation.validate(shot))


def test_writer_payload_and_semantic_requirements_use_explicit_target_states():
    shot, assets, _, cast, refs = _case()
    ask = writer._payload({"label": "CLIP 01"}, [shot], [(0, 3)], 3, characters=cast,
                          environment=None, look="cg3d", aspect_ratio="9:16", previous_state="",
                          unsafe={1: "source uniform dissolves"}, school_age=True, reference_assets=refs, strict=True)
    row = ask["shots"][0]
    assert "rewrite_required" not in row
    assert "source_appearances" not in row
    assert row["production_appearances"][0]["asset_presence"][0]["state"].startswith("Adult wearing")
    requirements = coverage.build_requirements(ask["shots"], [shot], assets)
    assert "lingerie" not in json.dumps(requirements)
    assert next(x for x in requirements if x["kind"] == "presence")["visibility"] == "visible"
    assert any(x["kind"] == "production_appearances" for x in requirements)


def test_unsafe_target_overlay_still_stops_before_any_model(monkeypatch):
    shot, assets, report, cast, refs = _case()
    shot["production_adaptation"]["shot_overrides"]["action"] = ["The uniform dissolves to reveal lingerie."]
    async def forbidden(*args, **kwargs):
        pytest.fail("Unsafe target must not reach model")
    monkeypatch.setattr(adapt, "ask_json", forbidden)
    with pytest.raises(writer.WriterError, match="approved adaptation"):
        asyncio.run(writer.write_clip_prompt({"label": "CLIP 01", "duration_s": 3}, [shot],
            characters=cast, environment=None, production_assets=assets,
            reference_assets=refs, source_verification=report))


def test_overlay_flows_to_writer_and_reviewer_without_approving_source(monkeypatch):
    shot, assets, report, cast, refs = _case()
    original = deepcopy((shot, report))
    calls = []
    async def fake(system, user, stats, **kwargs):
        ask = json.loads(user)
        calls.append(ask)
        if "requirements" in ask:
            assert "lingerie" not in json.dumps(ask["requirements"])
            return {"status": "verified", "checked_requirement_ids": [r["id"] for r in ask["requirements"]], "findings": []}
        body = "Adult Lead stays in an opaque tailored suit in the foreground, holding the closed box at waist level in the left hand. A cyan scan traces its exterior. Medium shot."
        prompt = f"CLIP 01 — TEST\nDURATION: {ask['clip']['duration_seconds']} seconds. SHOT COUNT: 1.\n[CREATIVES DESCRIPTION]\n"
        prompt += "\n".join(f"{r['tag']} — {r['name']}. Reference." for r in ask['references'])
        prompt += f"\n[SPECIFIC TIMELINE]\n[SHOT 1 — {ask['shots'][0]['time']}]\n{body}\n[OVERALL SUPPLEMENT]\nNo music."
        return {"prompt": prompt, "coverage": [{"requirement_id": r['id'], "shot": 1, "quote": body} for r in ask['coverage_requirements']]}
    monkeypatch.setattr(adapt, "ask_json", fake)
    result = asyncio.run(writer.write_clip_prompt({"label": "CLIP 01", "title": "TEST", "duration_s": 3}, [shot],
        characters=cast, environment=None, production_assets=assets, reference_assets=refs, source_verification=report,
        unsafe=[(1, "source clothing dissolve")]))
    assert result.coverage["status"] == "verified"
    assert len(calls) == 2
    assert (shot, report) == original
    assert "review" not in report
    changed = deepcopy(shot)
    changed["production_adaptation"]["asset_presence"][0]["state"] = "Different target costume."
    assert coverage.contract_digest({}, [shot], cast, None) != coverage.contract_digest({}, [changed], cast, None)
