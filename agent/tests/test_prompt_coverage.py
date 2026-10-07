"""Regressions for the source-to-prompt agent, with no paid or live model calls."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json

import pytest

from flowboard.services import prompt_coverage as coverage
from flowboard.services import prompt_writer as writer
from flowboard.services.video_analyzer import adapt


def _film(space=False):
    names = ("Mira Sol", "Dock Workers", "Navigation Core") if space else ("Theo Lambert", "Student Group A", "Gold Watch")
    assets = [
        {"id": "person", "kind": "character", "name": "Original Actor", "production_name": names[0],
         "production_key": "adapted-lead", "reference_required": True},
        {"id": "group", "kind": "background_group", "name": names[1], "reference_required": True},
        {"id": "prop", "kind": "prop", "name": names[2], "reference_required": True},
    ]
    cast = [{"key": "adapted-lead", "source_asset_id": "person", "name": names[0],
             "ref_label": "@image1", "ref_url": "https://example.test/person.png"}]
    extra = [{"id": a["id"], "name": a["name"], "kind": a["kind"],
              "ref_label": f"@image{i}", "ref_url": f"https://example.test/{a['id']}.png"}
             for i, a in enumerate(assets[1:], 2)]
    sequence = {"label": "CLIP 01", "title": "A precise exchange", "duration_s": 6}
    shots = [{"source_shots": [n], "source_evidence": [f"frame-{n}"], "duration_s": 3,
              "character_keys": ["adapted-lead"], "framing": "MS",
              "scene_present_asset_ids": ["person", "group", "prop"],
              "asset_presence": [
                  {"asset_id": "person", "visibility": "visible", "position": "foreground", "state": "waiting"},
                  {"asset_id": "group", "visibility": "partial", "position": "behind the lead", "state": "stationary"},
                  {"asset_id": "prop", "visibility": "visible", "position": "at waist height", "state": "closed",
                   "holder_id": "person", "hand": "left", "contains_ids": []}],
              "action": ["The lead waits."], "dialogue": []} for n in (2, 3)]
    report = {"status": "verified", "method": "source_frames", "reviewed_shots": [2, 3],
              "unresolved_shots": [], "digest": "source-digest", "findings": [],
              "evidence": [{"id": f"frame-{n}", "shot": n, "frame": f"shot-{n}.jpg", "timestamp_s": n * 2.0}
                           for n in (2, 3)]}
    return sequence, shots, cast, extra, assets, report


def _draft(ask, *, wrong_hand=False, omit_group=False):
    lead = next(a for a in ask["production_assets"] if a["id"] == "person")["production_name"]
    group = next(a for a in ask["production_assets"] if a["id"] == "group")["name"]
    prop = next(a for a in ask["production_assets"] if a["id"] == "prop")["name"]
    prompt = (f"CLIP 01 — A PRECISE EXCHANGE\nDURATION: {ask['clip']['duration_seconds']} seconds. "
              f"SHOT COUNT: {len(ask['shots'])}.\n[CREATIVES DESCRIPTION]\n")
    prompt += "\n".join(f"{r['tag']} — {r['name']}. Reference." for r in ask["references"])
    prompt += "\n[SPECIFIC TIMELINE]\n"
    matches = []
    for row in ask["shots"]:
        body = (f"Medium shot. {lead} waits in the foreground. {prop} stays closed at waist height "
                f"in {lead}'s {'right' if wrong_hand else 'left'} hand. "
                + ("" if omit_group else f"{group} remain partly visible, stationary behind {lead}. ")
                + "The lead waits.")
        prompt += f"[SHOT {row['shot']} — {row['time']}]\n{body}\n"
        for item in ask["coverage_requirements"]:
            if item["shot"] == row["shot"]:
                if omit_group and item.get("asset_id") == "group":
                    continue
                matches.append({"requirement_id": item["id"], "shot": row["shot"], "quote": body})
    prompt += "[OVERALL SUPPLEMENT]\nNo music."
    return {"prompt": prompt, "end_state": f"{lead} holds {prop} in the left hand.", "coverage": matches}


def _review(ask, findings=None):
    return {"status": "needs_revision" if findings else "verified",
            "checked_requirement_ids": [r["id"] for r in ask["requirements"]], "findings": findings or []}


def _run(data):
    seq, shots, cast, extra, assets, report = data
    return asyncio.run(writer.write_clip_prompt(seq, shots, characters=cast, environment=None,
                        production_assets=assets, reference_assets=extra, source_verification=report, cinematic=False))


@pytest.mark.parametrize("space", [False, True])
def test_missing_background_is_repaired_for_unrelated_films(monkeypatch, space):
    data = _film(space)
    calls = []

    async def fake(system, user, stats, **kwargs):
        ask = json.loads(user)
        calls.append(ask)
        if "requirements" in ask:
            group = data[4][1]["name"]
            in_each_shot = all(group in block for block in coverage.shot_blocks(ask["prompt"]).values())
            findings = [] if in_each_shot else [{
                "requirement_id": "shot:1:asset:group:2", "kind": "prompt_issue",
                "message": "Missing shot:1:asset:group:2 background presence in the actual shot text."}]
            return _review(ask, findings)
        # The host supplies literal shot evidence. A semantic reviewer must
        # still reject a missing group even without writer coverage claims.
        draft = _draft(ask, omit_group=len(calls) == 1)
        draft.pop("coverage")
        return draft

    monkeypatch.setattr(adapt, "ask_json", fake)
    result = _run(data)
    assert len(calls) == 4
    assert any("asset:group" in p for p in calls[2]["fix_exactly_these_problems"])
    assert result.coverage["status"] == "verified"
    assert data[4][1]["name"] in result.prompt
    if space:
        assert all(x not in result.prompt for x in ("Theo", "Student", "Watch", "Sienna"))
        assert calls[0]["production_assets"][0]["production_name"] == "Mira Sol"


def test_wrong_holder_hand_passes_quotes_but_independent_review_repairs_it(monkeypatch):
    calls = {"writer": 0, "review": 0}

    async def fake(system, user, stats, **kwargs):
        ask = json.loads(user)
        if "requirements" in ask:
            calls["review"] += 1
            findings = [{"requirement_id": "shot:1:asset:prop:3", "kind": "prompt_issue",
                         "message": "The prop must remain in the left hand, not the right hand."}]
            return _review(ask, findings if "right hand" in ask["prompt"] else None)
        calls["writer"] += 1
        if calls["writer"] == 2:
            assert "left hand" in ask["fix_exactly_these_problems"][0]
        return _draft(ask, wrong_hand=calls["writer"] == 1)

    monkeypatch.setattr(adapt, "ask_json", fake)
    result = _run(_film(True))
    assert calls == {"writer": 2, "review": 2}
    assert "right hand" not in result.prompt


def test_missing_reference_and_unrelated_evidence_stop_before_model(monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("Incomplete source data must not reach a model")

    monkeypatch.setattr(adapt, "ask_json", forbidden)
    data = list(_film())
    data[3] = data[3][:1]
    with pytest.raises(writer.WriterError, match="Required reference image is missing.*prop"):
        _run(data)
    data = list(_film())
    data[1][0]["source_evidence"] = ["frame-3"]
    with pytest.raises(writer.WriterError, match="does not belong"):
        _run(data)


def test_source_uncertainty_is_returned_upstream_instead_of_invented(monkeypatch):
    calls = []

    async def fake(system, user, stats, **kwargs):
        ask = json.loads(user)
        calls.append(ask)
        if "requirements" in ask:
            return _review(ask, [{"requirement_id": "shot:1:asset:prop:3", "kind": "source_issue",
                                  "message": "Source records conflict on which hand holds the object."}])
        return _draft(ask)

    monkeypatch.setattr(adapt, "ask_json", fake)
    with pytest.raises(writer.WriterError, match="Source contract needs review.*conflict"):
        _run(_film())
    assert len(calls) == 2


def test_wrong_shot_dialogue_and_duplicate_reference_are_rejected():
    prompt = ('DURATION: 4 seconds.\n@image1 — MIRA SOL.\n[SPECIFIC TIMELINE]\n'
              '[SHOT 1 — 00:00–00:02]\nMIRA:\n"Stand by."\n'
              '[SHOT 2 — 00:02–00:04]\nMIRA:\n"Open the hatch."\n'
              '[OVERALL SUPPLEMENT]\n@image1')
    issues = writer.check_clip_prompt(prompt, refs=[("@image1", "MIRA SOL")], duration=4,
                                     slots=[(0, 2), (2, 4)], lines=[(1, "Open the hatch.")],
                                     exempt_shots=set(), school_age=False)
    assert any("exactly once" in p for p in issues)
    assert any("inside that shot" in p for p in issues)
    wrong_speaker = prompt.replace('MIRA:\n"Open', 'PILOT:\n"Open')
    issues = writer.check_clip_prompt(wrong_speaker, refs=[], duration=4, slots=[(0, 2), (2, 4)],
                                     lines=[], speakers=[(2, "MIRA SOL", "Open the hatch.")],
                                     exempt_shots=set(), school_age=False)
    assert any("correct speaker" in p for p in issues)


def test_coverage_cannot_quote_another_shot_or_claim_absent_text():
    prompt = "[SHOT 1 — 00:00–00:02]\nEmpty corridor.\n[SHOT 2 — 00:02–00:04]\nWorkers wait."
    requirement = [{"id": "crowd", "shot": 1, "kind": "presence"}]
    problems = coverage.check_coverage(prompt, requirement,
                                      [{"requirement_id": "crowd", "shot": 2, "quote": "Workers wait."}])
    assert any("wrong shot" in p for p in problems)
    assert any("exact non-empty span" in p for p in problems)


def test_stale_inputs_or_manual_prompt_edit_invalidate_verified_draft(monkeypatch):
    async def fake(system, user, stats, **kwargs):
        ask = json.loads(user)
        return _review(ask) if "requirements" in ask else _draft(ask)

    monkeypatch.setattr(adapt, "ask_json", fake)
    seq, shots, cast, extra, assets, report = data = _film(True)
    result = _run(data)
    digest = coverage.contract_digest(seq, shots, cast, None, assets, extra, report,
                                      look="realistic", aspect_ratio=None, previous_state="", style_note="")
    assert coverage.verify_prompt_contract(result.prompt, result.coverage,
                                            expected_digest=result.contract_digest, actual_digest=digest) == []
    changed = deepcopy(shots)
    changed[0]["asset_presence"][2]["hand"] = "right"
    other = coverage.contract_digest(seq, changed, cast, None, assets, extra, report,
                                     look="realistic", aspect_ratio=None, previous_state="", style_note="")
    assert any("inputs changed" in p for p in coverage.verify_prompt_contract(
        result.prompt, result.coverage, expected_digest=result.contract_digest, actual_digest=other))
    assert any("text changed" in p for p in coverage.verify_prompt_contract(
        result.prompt + "\nAdd a stranger.", result.coverage,
        expected_digest=digest, actual_digest=digest))


def test_merged_shots_keep_both_source_states_and_adapted_reference_identity():
    seq, shots, cast, extra, assets, report = _film(True)
    first, second = shots
    first["source_shots"] = [2, 3]
    first["source_evidence"] = ["frame-2", "frame-3"]
    first["asset_presence"] = [{**p, "source_shot": 2} for p in first["asset_presence"]]
    first["asset_presence"] += [{**p, "source_shot": 3} for p in second["asset_presence"]]
    first["source_appearances"] = [{"source_shot": 2, "asset_presence": first["asset_presence"][:3]},
                                   {"source_shot": 3, "asset_presence": first["asset_presence"][3:]}]
    first["action"] = [f"Action detail {i}." for i in range(7)]
    assert coverage.validate_source_contract([first], assets, report, cast + extra) == []
    ask = writer._payload(seq, [first], [(0, 6)], 6, characters=cast, environment=None,
                           look="cg3d", aspect_ratio=None, previous_state="", unsafe={},
                           school_age=False, reference_assets=extra, strict=True)
    requirements = coverage.build_requirements(ask["shots"], [first], assets)
    assert len(ask["shots"][0]["action"]) == 7
    assert ask["shots"][0]["source_appearances"] == first["source_appearances"]
    assert len({r["id"] for r in requirements}) == len(requirements)
    assert next(r for r in requirements if r.get("asset_id") == "person")["name"] == "Mira Sol"


def test_incomplete_semantic_review_cannot_self_certify(monkeypatch):
    async def fake(system, user, stats, **kwargs):
        ask = json.loads(user)
        if "requirements" in ask:
            return {"status": "verified", "checked_requirement_ids": [], "findings": []}
        return _draft(ask)

    monkeypatch.setattr(adapt, "ask_json", fake)
    with pytest.raises(writer.WriterError, match="did not check every requirement"):
        _run(_film())


def test_large_semantic_review_preserves_context_and_checks_every_batch(monkeypatch):
    requirements=[{'id':f'req-{i}'} for i in range(130)]
    seen=[]
    async def fake(system,user,stats,**kwargs):
        data=json.loads(user);seen.append(data)
        return {'status':'verified','checked_requirement_ids':[r['id'] for r in data['requirements']],'findings':[]}
    monkeypatch.setattr(adapt,'ask_json',fake)
    result=asyncio.run(writer.review_prompt('Full cross-shot prompt',requirements,[],[],
                                           shot_package={'all_shots':True},end_state='Final state'))
    assert result['status']=='verified' and result['review_batches']==3
    assert result['checked_requirement_ids']==[r['id'] for r in requirements]
    assert all(len(d['requirements'])<=64 and d['prompt']=='Full cross-shot prompt'
               and d['shot_package']=={'all_shots':True} and d['end_state']=='Final state' for d in seen)
    async def incomplete(system,user,stats,**kwargs):
        data=json.loads(user);ids=[r['id'] for r in data['requirements']]
        return {'status':'verified','checked_requirement_ids':ids[:-1],'findings':[]}
    monkeypatch.setattr(adapt,'ask_json',incomplete)
    with pytest.raises(writer.WriterError,match='did not check every requirement'):
        asyncio.run(writer.review_prompt('Full cross-shot prompt',requirements,[],[]))


def test_decomposed_unicode_speaker_label_is_same_name_not_an_alias():
    import unicodedata
    text=unicodedata.normalize('NFD','DURATION: 2 seconds.\n[SHOT 1 — 00:00–00:02]\nHUYỀN TIÊU:\n"Hello."')
    kwargs=dict(refs=[],duration=2,slots=[(0,2)],lines=[(1,'Hello.')],exempt_shots=set(),school_age=False)
    assert not writer.check_clip_prompt(text,**kwargs,speakers=[(1,'HUYỀN TIÊU','Hello.')])
    assert writer.check_clip_prompt(text,**kwargs,speakers=[(1,'MA TÔN','Hello.')])


def test_host_renders_locked_shot_times_without_changing_or_inventing_shots():
    draft='[SHOT 1 — 00:00–00:01]\nHe lifts the box.\n[SHOT 2 — 00:01–00:02]\nShe reacts.'
    rendered=writer.normalize_shot_headers(draft,[(0,.966),(.966,2.133)])
    assert '[SHOT 1 — 00:00–00:00.966]' in rendered
    assert '[SHOT 2 — 00:00.966–00:02.133]' in rendered
    assert 'He lifts the box.\n' in rendered and rendered.endswith('She reacts.')
    missing=draft.split('[SHOT 2')[0]
    assert writer.normalize_shot_headers(missing,[(0,1),(1,2)])==missing
    wrong_order=draft.replace('SHOT 2','SHOT 3')
    assert writer.normalize_shot_headers(wrong_order,[(0,1),(1,2)])==wrong_order


def test_verified_source_digests_reject_stale_observations_but_allow_target_names():
    seq, shots, cast, extra, assets, report = _film(True)
    report["asset_digests"] = {a["id"]: coverage._source_digest(
        {k: v for k, v in a.items() if k not in {"production_key", "production_name"}}) for a in assets}
    report["shot_digests"] = {}
    for shot in shots:
        source = shot["source_shots"][0]
        observation = {"scene_id": "dock", "asset_presence": deepcopy(shot["asset_presence"]),
                       "evidence_ids": list(shot["source_evidence"])}
        report["shot_digests"][str(source)] = coverage._source_digest(observation)
        shot["source_appearances"] = [{"source_shot": source, **observation}]
        shot["asset_presence"] = [{**p, "source_shot": source} for p in shot["asset_presence"]]
    assets[0]["production_name"] = "Captain Mira"
    assert coverage.validate_source_contract(shots, assets, report, cast + extra) == []
    shots[0]["asset_presence"][2]["hand"] = "right"
    issues = coverage.validate_source_contract(shots, assets, report, cast + extra)
    assert any("changed from its verified source appearances" in p for p in issues)
    assets[2]["description"] = "A different object"
    assert any("Source asset 'prop' changed" in p for p in coverage.validate_source_contract(shots, assets, report, cast + extra))


def test_offscreen_continuity_may_cite_adjacent_verified_frame():
    _, shots, cast, extra, assets, report = _film(True)
    shots[1]["asset_presence"][1].update(visibility="offscreen", evidence_ids=["frame-2"])
    assert coverage.validate_source_contract(shots, assets, report, cast + extra) == []
    shots[1]["asset_presence"][1]["visibility"] = "visible"
    assert any("unrelated evidence" in p for p in coverage.validate_source_contract(shots, assets, report, cast + extra))


def test_an_accepted_review_settles_uncertain_presence_without_making_it_a_requirement():
    """The verifier could not tell whether the group is there. Until a person
    accepts that, the writer must not start; after, it is not asked to cover it."""
    _, shots, cast, extra, assets, report = _film()
    shots[0]["asset_presence"][1]["visibility"] = "uncertain"
    issues = coverage.validate_source_contract(shots, assets, report, cast + extra)
    assert any("resolve uncertain visibility for 'group'" in p for p in issues)
    report["review"] = {"accepted_by": "reviewer", "accepted_at": "2026-09-25T00:00:00Z"}
    assert coverage.validate_source_contract(shots, assets, report, cast + extra) == []
    rows = [{"shot": n, "dialogue": []} for n in (1, 2)]
    presence = [(r["shot"], r["asset_id"]) for r in coverage.build_requirements(rows, shots, assets)
                if r["kind"] == "presence"]
    assert (1, "group") not in presence and (2, "group") in presence
    assert (1, "person") in presence and (1, "prop") in presence


def test_strict_requirements_retain_full_camera_light_effects_and_long_actions():
    sequence, shots, cast, extra, assets, _ = _film(True)
    shot = shots[0]
    shot.update(camera={"movement": "Crane down, then track sideways after the hatch closes.",
                        "angle": "Start overhead; finish at the pilot's eye height.",
                        "focus": "Rack from distant workers to the navigation core."},
                lens_mm=35,
                lighting={"key": "Cold skylight from port", "practical": "Amber warning beacon pulses twice."},
                vfx=["Steam vents after the seal opens.", "Beacon reflection crosses the core casing."],
                action=[f"Separate physical action {i}." for i in range(8)])
    payload = writer._payload(sequence, shots, [(0, 3), (3, 6)], 6, characters=cast,
                              environment=None, look="cg3d", aspect_ratio=None,
                              previous_state="", unsafe={}, school_age=False,
                              reference_assets=extra, strict=True)
    requirements = coverage.build_requirements(payload["shots"], shots, assets)
    by_id = {item["id"]: item for item in requirements}
    for field in ("camera", "lens_mm", "lighting", "vfx"):
        assert payload["shots"][0][field] == shot[field]
        assert by_id[f"shot:1:{field}"]["value"] == shot[field]
        assert f"shot:2:{field}" not in by_id  # Absent facts create no invented requirement.
    assert by_id["shot:1:action:8"]["value"] == "Separate physical action 7."
    quotes = [{"requirement_id": r["id"], "shot": r["shot"], "quote": "Placeholder."}
              for r in requirements if r["kind"] != "lighting"]
    prompt = "[SHOT 1 — 00:00–00:03]\nPlaceholder.\n[SHOT 2 — 00:03–00:06]\nPlaceholder."
    assert any("Missing coverage for shot:1:lighting" in error
               for error in coverage.check_coverage(prompt, requirements, quotes))


def test_model_context_scopes_other_scenes_but_keeps_dependency_closure_and_full_digest(monkeypatch):
    sequence, shots, cast, extra, assets, report = data = _film(True)
    assets[2]["depends_on_asset_ids"] = ["pilot-portrait"]
    assets += [{"id": "pilot-portrait", "kind": "prop", "name": "Pilot portrait",
                "reference_required": False, "depends_on_asset_ids": ["portrait-person"]},
               {"id": "portrait-person", "kind": "character", "name": "Retired pilot", "reference_required": False},
               {"id": "other-scene-group", "kind": "background_group", "name": "Desert caravan", "reference_required": False},
               {"id": "other-scene-prop", "kind": "prop", "name": "Sundial", "reference_required": False}]
    report["evidence"].append({"id": "far-frame", "shot": 900, "frame": "desert.jpg", "timestamp_s": 400})
    report["trace"] = [{"irrelevant": "large film-wide trace"}]
    report["usage"] = {"tokens": 123456}
    requests = []

    async def fake(system, user, stats, **kwargs):
        ask = json.loads(user)
        requests.append(ask)
        return _review(ask) if "requirements" in ask else _draft(ask)

    monkeypatch.setattr(adapt, "ask_json", fake)
    result = _run(data)
    expected = {"person", "group", "prop", "pilot-portrait", "portrait-person"}
    assert all({a["id"] for a in ask["production_assets"]} == expected for ask in requests)
    sent_report = requests[0]["source_verification"]
    assert {e["id"] for e in sent_report["evidence"]} == {"frame-2", "frame-3"}
    assert "trace" not in sent_report and "usage" not in sent_report
    full_digest = coverage.contract_digest(sequence, shots, cast, None, assets, extra, report,
                                            look="realistic", aspect_ratio=None, previous_state="", style_note="")
    assert result.contract_digest == full_digest
    assets[-1]["description"] = "Modified elsewhere"
    assert coverage.contract_digest(sequence, shots, cast, None, assets, extra, report,
                                    look="realistic", aspect_ratio=None, previous_state="", style_note="") != full_digest


def test_semantic_review_extracts_complete_current_shot_including_appended_sound(monkeypatch):
    prompt='Shared wardrobe lock.\n[SHOT 1 — 00:00–00:01]\nHero crouches.\nSound design: wind through leaves.\n[SHOT 2 — 00:01–00:02]\nThe box remains closed.\n[OVERALL SUPPLEMENT]\nNo extra speech.'
    seen=[]
    async def fake(system,user,stats,**kwargs):
        d=json.loads(user);seen.append(d)
        return {'status':'verified','checked_requirement_ids':['R1'],'findings':[]}
    monkeypatch.setattr(adapt,'ask_json',fake)
    asyncio.run(writer.review_prompt(prompt,[{'id':'sound-1','shot':1}],[],[]))
    assert seen[0]['shot_texts']=={'1':coverage.shot_blocks(prompt)[1]}
    assert 'Sound design: wind through leaves.' in seen[0]['shot_texts']['1']
    assert 'box remains closed' not in seen[0]['shot_texts']['1']
    assert seen[0]['prompt']==prompt
