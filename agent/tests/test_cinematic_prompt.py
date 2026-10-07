import asyncio
import copy
import json

import pytest
from PIL import Image

from flowboard.services import cinematic_prompt as cp, prompt_writer as writer, prompt_coverage
from flowboard.services.video_analyzer import adapt


def inputs():
    sequence = {
        "label": "CLIP 01",
        "title": "The parcel",
        "duration_s": 4,
        "preserve_source_shots": True,
    }
    shots = [
        {
            "duration_s": 4,
            "framing": "MS",
            "camera": "Static",
            "character_keys": ["rhea"],
            "action": ["Rhea holds a closed parcel."],
            "dialogue": [{"who": "RHEA", "line": "Here it is."}],
        }
    ]
    chars = [
        {
            "key": "rhea",
            "name": "Rhea",
            "ref_label": "@image1",
            "ref_url": "https://example.invalid/rhea.png",
            "media_id": "abc",
            "design": {"costume": [{"piece": "coat", "colour": "green"}]},
        }
    ]
    return sequence, shots, chars


def good():
    return """Create a **4-second square 1:1 cinematic 3D sequence** in a station.
## REFERENCE CONTROL
- **@image1 = RHEA — CHARACTER REFERENCE.**
  Keep her face and green coat.
## STYLE
Cinematic 3D, quiet daylight.
**Dramatic intention:** deliver the parcel.
**Opening state:** Rhea holds the closed parcel.
# SHOT 1 | 00:00.000–00:04.000
**Duration:** 4 seconds
**Framing:** Medium shot.
**Camera:** Static.
Rhea holds the closed parcel and looks up.
## DIALOGUE
RHEA — SPOKEN ON CAMERA:
“Here it is.”
**Delivery / audio:** Quiet English.
**End state:** Rhea retains the closed parcel.
## AUDIO
No music. English dialogue only. Room tone.
## CONTINUITY / NEGATIVE CONSTRAINTS
Keep the closed parcel and green coat. No subtitles.
"""


def ask():
    sequence, shots, chars = inputs()
    return writer._payload(
        sequence,
        shots,
        [(0, 4)],
        4,
        characters=chars,
        environment=None,
        look="cg3d",
        aspect_ratio="1:1",
        previous_state="",
        unsafe={},
        school_age=False,
        strict=True,
    )


def test_modern_prompt_passes_same_fixed_fact_checks():
    kw = dict(
        refs=[("@image1", "RHEA")],
        duration=4,
        slots=[(0, 4)],
        lines=[(1, "Here it is.")],
        speakers=[(1, "RHEA", "Here it is.")],
        exempt_shots=set(),
        school_age=False,
    )
    assert writer.check_clip_prompt(good(), **kw) == []
    assert cp.validate(good(), ask(), [(0, 4)]) == []
    assert "## AUDIO" not in prompt_coverage.shot_blocks(good())[1]
    assert writer.check_clip_prompt(good().replace("RHEA — SPOKEN", "SOMEONE — SPOKEN"), **kw)
    assert cp.validate(good().replace("square 1:1", "horizontal 16:9"), ask(), [(0, 4)])
    assert cp.validate(good().replace("“Here it is.”", "“Here it is.”\n“你好”"), ask(), [(0, 4)])
    inline = good().replace("REFERENCE.**\n  Keep", "REFERENCE.** Keep")
    assert writer.check_clip_prompt(inline, **kw) == []


def test_modern_delivery_cannot_lose_offscreen_or_continuation():
    payload = ask()
    payload["shots"][0]["dialogue"][0]["who"] = "RHEA, OFF SCREEN, CONTINUING"
    assert any("OFF SCREEN" in x for x in cp.validate(good(), payload, [(0, 4)]))
    assert (
        cp.validate(good().replace("SPOKEN ON CAMERA", "OFF SCREEN, CONTINUING"), payload, [(0, 4)])
        == []
    )
    payload = ask()
    payload["shots"][0]["dialogue"][0]["continues_from_previous_shot"] = True
    assert any("CONTINUING" in x for x in cp.validate(good(), payload, [(0, 4)]))


def test_slash_delivery_keeps_exact_speaker_and_continuation_checks():
    prompt = good().replace("SPOKEN ON CAMERA", "OFF SCREEN / CONTINUING")
    payload = ask()
    payload["shots"][0]["dialogue"][0]["who"] = "RHEA, OFF SCREEN, CONTINUING"
    checks = dict(refs=[("@image1", "RHEA")], duration=4, slots=[(0, 4)],
                  lines=[(1, "Here it is.")], speakers=[(1, "RHEA, OFF SCREEN, CONTINUING", "Here it is.")],
                  exempt_shots=set(), school_age=False)
    assert writer.check_clip_prompt(prompt, **checks) == []
    assert cp.validate(prompt, payload, [(0, 4)]) == []
    assert writer.check_clip_prompt(prompt.replace("RHEA — OFF", "SOMEONE — OFF"), **checks)
    assert cp.validate(prompt.replace(" / CONTINUING", ""), payload, [(0, 4)])
    assert "OFF SCREEN / CONTINUING" in prompt


def test_actual_approved_example_is_readable_without_rewriting():
    p = writer._DOCS / "cinematic-prompts" / "approved-example.txt"
    prompt = p.read_text()
    assert len(prompt_coverage.shot_blocks(prompt)) == 9
    assert "[SHOT 1" in cp.canonical(prompt, 20)
    assert p.read_text() == prompt


@pytest.mark.parametrize("source_speaker", ["OFFSCREEN NARRATOR", "OFF-SCREEN NARRATOR", "NARRATOR, OFF SCREEN"])
def test_narrator_prefix_and_suffix_delivery_preserve_identity(source_speaker):
    prompt = good().replace("RHEA — SPOKEN ON CAMERA", "NARRATOR — OFF SCREEN")
    payload = ask()
    payload["shots"][0]["dialogue"][0]["who"] = source_speaker
    checks = dict(refs=[("@image1", "RHEA")], duration=4, slots=[(0, 4)],
                  lines=[(1, "Here it is.")], speakers=[(1, source_speaker, "Here it is.")],
                  exempt_shots=set(), school_age=False)
    assert writer.check_clip_prompt(prompt, **checks) == []
    assert cp.validate(prompt, payload, [(0, 4)]) == []
    assert writer.check_clip_prompt(prompt.replace("NARRATOR —", "RHEA —"), **checks)
    assert cp.validate(prompt.replace("OFF SCREEN", "SPOKEN ON CAMERA"), payload, [(0, 4)])


def test_reference_images_are_deduplicated_and_kept_in_tag_order(monkeypatch, tmp_path):
    path = tmp_path / "ref.png"
    Image.new("RGB", (12, 12), "green").save(path)
    monkeypatch.setattr(cp.media, "cached_path", lambda _: path)
    refs = [
        {
            "id": "box",
            "name": "Box",
            "ref_label": "@image2",
            "ref_url": "https://cdn/b",
            "media_id": "b",
        },
        {
            "id": "rhea",
            "name": "Rhea",
            "ref_label": "@image1",
            "ref_url": "https://cdn/a",
            "media_id": "a",
        },
        {
            "id": "coat",
            "name": "Coat",
            "ref_label": "@image1",
            "ref_url": "https://cdn/a",
            "media_id": "a",
        },
    ]
    parts, records = asyncio.run(cp.reference_images(refs))
    assert [r["tag"] for r in records] == ["@image1", "@image2"]
    assert [p["type"] for p in parts] == ["text", "imageBase64", "text", "imageBase64"]
    assert all("data" not in r for r in records)
    refs[2]["media_id"] = "different"
    with pytest.raises(ValueError, match="conflicting"):
        asyncio.run(cp.reference_images(refs))


def test_missing_or_untrusted_image_does_not_silently_become_text_only(monkeypatch):
    monkeypatch.setattr(cp.media, "cached_path", lambda _: None)
    monkeypatch.setattr(cp.media, "_url_allowed", lambda _: False)
    with pytest.raises(ValueError, match="configured media storage"):
        asyncio.run(cp.reference_images(inputs()[2]))


def test_seedance25_accepts_ten_references_but_checks_upper_limit():
    refs = [
        {"id": str(i), "ref_label": f"@image{i}", "ref_url": f"https://cdn/{i}"}
        for i in range(1, 32)
    ]
    assert prompt_coverage.reference_slots(refs[:10])[1] == []
    assert prompt_coverage.reference_slots(refs[:30])[1] == []
    assert "30 reference" in " ".join(prompt_coverage.reference_slots(refs)[1])


def test_cinematic_writer_sends_images_reviews_and_keeps_source_unchanged(monkeypatch):
    sequence, shots, chars = inputs()
    original = copy.deepcopy(shots)
    seen = []

    async def images(refs):
        return [{"type": "text", "text": "image evidence"}], [
            {"tag": "@image1", "sha256": "digest"}
        ]

    async def fake(system, user, stats, **kw):
        seen.append((system, json.loads(user), kw))
        stats.answered_by[writer.WRITER_MODEL] = 1
        return {
            "prompt": good(),
            "end_state": "Rhea retains the closed parcel.",
            "staging_decisions": [],
        }

    async def review(prompt, req, refs, assets, **kw):
        assert req and "staging_decisions" in kw
        return {
            "status": "verified",
            "checked_requirement_ids": [r["id"] for r in req],
            "findings": [],
        }

    monkeypatch.setattr(cp, "reference_images", images)
    monkeypatch.setattr(adapt, "ask_json", fake)
    monkeypatch.setattr(writer, "review_prompt", review)
    result = asyncio.run(
        writer.write_clip_prompt(
            sequence,
            shots,
            characters=chars,
            environment=None,
            aspect_ratio="1:1",
            look="cg3d",
            cinematic=True,
        )
    )
    assert result.engine == "cinematic-v1" and len(result.reference_images) == 1
    assert seen[0][2]["image_parts"] and seen[0][2]["model"] == writer.WRITER_MODEL
    assert shots == original and result.prompt == cp.enforce_audio_policy(good())
    assert result.coverage["semantic_review"]["status"] == "verified"
    assert "AVA SHAW" not in seen[0][0]  # Approved fixture is never model story context.
    assert any(
        r["kind"] == "presence" and r["name"].upper() == "RHEA"
        for r in result.coverage["requirements"]
    )


def test_image_parts_use_avis_native_shape(monkeypatch):
    captured = []

    async def complete(model, messages, **kw):
        captured.append(messages)
        return cp.avis_text.Completion(text='{"ok":true}', model=model)

    monkeypatch.setattr(cp.avis_text, "complete", complete)
    image = {"type": "imageBase64", "data": "abc", "mediaType": "image/jpeg"}
    result = asyncio.run(
        adapt.ask_json(
            "sys",
            "facts",
            adapt.TextStats(),
            model="gpt-6-luna",
            fallback="",
            attempts=1,
            image_parts=[image],
        )
    )
    assert result == {"ok": True}
    assert captured[0][1]["content"] == [{"type": "text", "text": "facts"}, image]


def test_missing_carry_forward_state_cannot_be_accepted(monkeypatch):
    seq, shots, chars = inputs()
    calls = []

    async def images(refs):
        return [], []

    async def model(system, user, stats, **kwargs):
        calls.append(json.loads(user))
        return {"prompt": good(), "staging_decisions": []}

    monkeypatch.setattr(cp, "reference_images", images)
    monkeypatch.setattr(adapt, "ask_json", model)
    with pytest.raises(writer.WriterError, match="non-empty end_state"):
        asyncio.run(
            writer.write_clip_prompt(
                seq, shots, characters=chars, environment=None, aspect_ratio="1:1", cinematic=True
            )
        )
    assert len(calls) == 2
    assert "end_state" in " ".join(calls[1]["fix_exactly_these_problems"])


@pytest.mark.parametrize(
    "style,hero,setting",
    [
        ("2D watercolor anime", "LIN", "an ancient mountain shrine"),
        ("photorealistic period drama", "ELENA", "a nineteenth-century railway station"),
        ("cinematic 3D science fiction", "KAEL", "an orbital docking bay"),
    ],
)
def test_default_writer_uses_current_film_profiles_even_without_optional_images(
    monkeypatch, style, hero, setting
):
    seq, shots, chars = inputs()
    chars[0].update(name=hero, wardrobe="Green travelling coat")
    chars[0].pop("ref_label")
    chars[0].pop("ref_url")
    chars[0].pop("media_id")
    shots[0]["dialogue"][0]["who"] = hero
    shots[0]["action"] = [f"{hero} holds a closed parcel."]
    env = {"name": setting, "summary": "A quiet covered walkway.", "lighting": "Soft dawn light"}
    output = good().replace(
        "- **@image1 = RHEA — CHARACTER REFERENCE.**\n  Keep her face and green coat.",
        "No images are attached. Follow the supplied design profiles.",
    )
    output = (
        output.replace("RHEA", hero)
        .replace("Rhea", hero)
        .replace("cinematic 3D", style)
        .replace("in a station", "in " + setting)
    )
    calls = []

    async def model(system, user, stats, **kwargs):
        payload = json.loads(user)
        calls.append(payload)
        if "requirements" in payload:
            return {
                "status": "verified",
                "checked_requirement_ids": [r["id"] for r in payload["requirements"]],
                "findings": [],
            }
        assert payload["clip"]["style"] == style
        profiles = payload["unreferenced_assets"]
        assert profiles[0]["name"] == hero and profiles[0]["wardrobe"] == "Green travelling coat"
        assert profiles[1]["name"] == setting
        assert payload["references"] == [] and kwargs["image_parts"] == []
        assert "AVA SHAW" not in system and "CAMPUS HALLWAY" not in system
        return {
            "prompt": output,
            "end_state": f"{hero} retains the parcel.",
            "staging_decisions": [],
        }

    monkeypatch.setattr(adapt, "ask_json", model)
    result = asyncio.run(
        writer.write_clip_prompt(
            seq, shots, characters=chars, environment=env, aspect_ratio="1:1", style_note=style
        )
    )
    assert result.engine == cp.ENGINE and len(calls) == 2
    assert style in result.prompt and setting in result.prompt


@pytest.mark.parametrize("path", ["/video/write", "/video/prompt"])
def test_both_api_paths_use_cinematic_and_never_translate_dialogue(client, monkeypatch, path):
    seq, shots, chars = inputs()
    calls = []

    async def model(*args, **kwargs):
        calls.append(kwargs)
        assert kwargs["cinematic"] is True
        return writer.Written(prompt=good(), duration=4, model="gpt-6-luna", engine=cp.ENGINE)

    async def no_translation(*args, **kwargs):
        pytest.fail("The old language parameter must not translate locked dialogue.")

    monkeypatch.setattr(writer, "write_clip_prompt", model)
    monkeypatch.setattr(writer, "WRITER_ON", True)
    monkeypatch.setattr(writer.automation, "translate_prompt", no_translation)
    response = client.post(
        "/api/automation" + path,
        json={"sequence": seq, "shots": shots, "characters": chars, "language": "zh"},
    )
    assert response.status_code == 200 and response.json()["engine"] == cp.ENGINE
    assert response.json()["prompt"] == good() and len(calls) == 1
    monkeypatch.setattr(writer, "WRITER_ON", False)
    blocked = client.post(
        "/api/automation" + path, json={"sequence": seq, "shots": shots, "characters": chars}
    )
    assert blocked.status_code == 422 and len(calls) == 1


def test_cinematic_route_is_default_and_does_not_fall_back_on_failure(monkeypatch):
    from flowboard.routes import automation as routes
    from fastapi import HTTPException

    sequence, shots, chars = inputs()

    async def broken(*args, **kwargs):
        assert kwargs["cinematic"] is True
        raise writer.WriterError("Image missing")

    monkeypatch.setattr(writer, "CLIP_ENGINE", "cinematic")
    monkeypatch.setattr(writer, "write_clip_prompt", broken)
    with pytest.raises(HTTPException, match="Image missing"):
        asyncio.run(
            routes.write_video_prompt(
                routes.VideoWriteBody(sequence=sequence, shots=shots, characters=chars)
            )
        )


def test_cinematic_strict_route_and_provided_review_keep_receipts_and_staging(monkeypatch):
    from flowboard.routes import automation as routes
    from fastapi import HTTPException
    from tests.test_prompt_coverage import _film, _review

    seq, shots, chars, extras, assets, report = _film(True)
    seq["production_context"] = {"scene_id": "dock", "continuity": "workers remain at the dock"}
    original = copy.deepcopy(shots)
    decisions = [
        {
            "shot": 1,
            "kind": "framing",
            "description": "Keep the waiting workers partly visible.",
            "basis": "Source presence says workers are partial behind Mira.",
        }
    ]
    checked = []

    async def images(refs):
        return [{"type": "text", "text": "actual image input"}], [
            {"tag": r["ref_label"], "sha256": "hash"} for r in refs
        ]

    async def model(system, user, stats, **kwargs):
        payload = json.loads(user)
        if "requirements" in payload:
            checked.append(payload)
            assert payload["production_context"] == seq["production_context"]
            assert payload["staging_decisions"] == decisions
            return _review(payload)
        stats.answered_by[writer.WRITER_MODEL] = 1
        text = "Create a **6-second square 1:1 cinematic 3D sequence** at the dock.\n## REFERENCE CONTROL\n"
        text += "\n".join(
            f"- **{r['tag']} = {r['name']} — REFERENCE.** Keep this design."
            for r in payload["references"]
        )
        text += "\n## STYLE\nCinematic 3D.\n**Dramatic intention:** Wait for pickup.\n**Opening state:** Mira waits at the dock.\n"
        for r in payload["shots"]:
            text += f"\n# SHOT {r['shot']} | {r['time']}\n**Duration:** 3 seconds\n**Framing:** Medium shot.\n**Camera:** Static.\n"
            text += "Mira Sol waits in the foreground, Navigation Core closed in her left hand at waist height. Dock Workers remain stationary, partly visible behind her.\n**End state:** Mira retains the closed core, workers stay behind her.\n"
        text += "## AUDIO\nNo speech or music.\n## CONTINUITY / NEGATIVE CONSTRAINTS\nPreserve the closed core in Mira’s left hand and the dock workers.\n"
        return {
            "prompt": text,
            "end_state": "Mira waits with the core in her left hand.",
            "staging_decisions": decisions,
        }

    monkeypatch.setattr(cp, "reference_images", images)
    monkeypatch.setattr(adapt, "ask_json", model)
    monkeypatch.setattr(writer, "WRITER_ON", True)
    monkeypatch.setattr(writer, "CLIP_ENGINE", "cinematic")
    monkeypatch.setattr(routes.auth, "_server_secret", lambda: b"cinematic-test-only")
    body = routes.VideoWriteBody(
        sequence=seq,
        shots=shots,
        characters=chars,
        reference_assets=extras,
        production_assets=assets,
        source_verification=report,
        style="cg3d",
        aspect_ratio="1:1",
    )
    out = asyncio.run(routes.write_video_prompt(body))
    assert out.engine == cp.ENGINE and len(out.reference_images) == 3
    slots, _ = prompt_coverage.reference_slots([*chars, *extras])
    request = routes.ClipBody(
        prompt=out.prompt,
        prompt_contract=body,
        duration_seconds=out.duration_seconds,
        aspect_ratio="1:1",
        reference_urls=[r["ref_url"] for r in slots],
        coverage=out.coverage,
        contract_digest=out.contract_digest,
        coverage_token=out.coverage_token,
    )
    routes._validate_generation_contract(request)
    reread = asyncio.run(
        routes.verify_video_prompt(
            routes.VerifyPromptBody(
                prompt_contract=body,
                prompt=out.prompt,
                end_state=out.end_state,
                staging_decisions=out.staging_decisions,
            )
        )
    )
    assert reread.prompt == out.prompt and reread.staging_decisions == decisions
    assert len(checked) == 2 and shots == original
    request.coverage = copy.deepcopy(request.coverage)
    request.coverage["staging_decisions"][0]["description"] = "Move the core to the right hand."
    with pytest.raises(HTTPException):
        routes._validate_generation_contract(request)


def test_chunked_review_retains_scene_context_and_staging(monkeypatch):
    calls = []

    async def model(system, user, stats, **kwargs):
        payload = json.loads(user)
        calls.append(payload)
        return {
            "status": "verified",
            "findings": [],
            "checked_requirement_ids": [r["id"] for r in payload["requirements"]],
        }

    monkeypatch.setattr(adapt, "ask_json", model)
    req = [
        {"id": f"shot:1:action:{i}", "shot": 1, "kind": "action", "value": "Rhea waits."}
        for i in range(65)
    ]
    context = {"scene_id": "station"}
    choices = [
        {
            "shot": 1,
            "kind": "hand_assignment",
            "description": "Use left hand.",
            "basis": "Both hands free.",
        }
    ]
    out = asyncio.run(
        writer.review_prompt(
            good(), req, [], [], production_context=context, staging_decisions=choices
        )
    )
    assert out["status"] == "verified" and len(calls) == 2
    assert all(
        c["production_context"] == context and c["staging_decisions"] == choices for c in calls
    )
def test_source_serializer_keeps_camera_and_verbatim_foreign_audio():
    from flowboard.services.cinematic_prompt import serialize_source_locks
    prompt = '''Create a 4-second sequence.
## REFERENCE CONTROL
No images attached.
# SHOT 1 | 00:00–00:02
**Framing:** invented close-up
**Camera:** invented orbit
**Action:** She responds.
**DIALOGUE**
Lina — SPOKEN ON CAMERA:
“Hello!”
**Delivery / audio:** calm
**End state:** She holds still.
# SHOT 2 | 00:02–00:04
**Framing:** wrong
**Camera:** wrong
## DIALOGUE
Lina:
“Hello again!”
**End state:** She listens.
## AUDIO
Room tone.
'''
    shots=[{'framing':'MS','framing_note':'head to waist','camera':'static',
            'dialogue':[{'who':'Lina','line':'Bonjour, ça va ?','delivery':'offscreen'}],
            'performance':['Clip audio 0.5–2.5s: continue across the cut.']},
           {'framing':'CU','camera':'static','dialogue':[]}]
    result=serialize_source_locks(prompt,shots)
    assert 'invented' not in result and 'Hello' not in result
    assert result.count('Bonjour, ça va ?')==1
    assert 'LINA — OFF SCREEN:' in result
    assert '**Camera:** static' in result and '**Framing:** MS. head to waist' in result
    assert 'Clip audio 0.5–2.5s' in result
    assert serialize_source_locks(result,shots)==result
    problems = writer.check_clip_prompt(result, refs=[], duration=4, slots=[(0,2),(2,4)],
        lines=[(1,'Bonjour, ça va ?')], speakers=[(1,'LINA','Bonjour, ça va ?')],
        exempt_shots=set(),school_age=False)
    assert not any('correct speaker' in p for p in problems)


def test_source_timing_serializer_repairs_fractional_headers_without_rewriting_actions():
    prompt = good().replace('# SHOT 1 | 00:00.000–00:04.000', '# SHOT 1 | 00:01–00:05.')
    prompt = prompt.replace('**Duration:** 4 seconds', '**Duration:** 5 seconds')
    shots = inputs()[1]
    result = cp.serialize_source_locks(prompt, shots, [(0, 4)])
    assert '# SHOT 1 | 00:00–00:04\n' in result
    assert '**Duration:** 4.000 seconds' in result
    assert 'Rhea holds the closed parcel and looks up.' in result
    assert cp.serialize_source_locks(result, shots, [(0, 4)]) == result
    problems = writer.check_clip_prompt(result, refs=[('@image1', 'RHEA')], duration=4,
        slots=[(0,4)], lines=[(1,'Here it is.')], exempt_shots=set(), school_age=False)
    assert not problems


def test_timing_serializer_does_not_create_or_renumber_missing_shots():
    prompt = good().replace('# SHOT 1 |', '# SHOT 2 |')
    result = cp.serialize_source_locks(prompt, inputs()[1], [(0,4)])
    assert '# SHOT 2 |' in result and '# SHOT 1 |' not in result
    assert any('shot headers' in p for p in writer.check_clip_prompt(result,
        refs=[('@image1', 'RHEA')], duration=4, slots=[(0,4)], lines=[],
        exempt_shots=set(), school_age=False))


def test_saved_draft_is_bound_to_inputs_and_keeps_staging(tmp_path, monkeypatch):
    from flowboard import config
    monkeypatch.setattr(config, 'STORAGE_DIR', tmp_path)
    key='a'*64
    choices=[{'shot':1,'kind':'hand_assignment','description':'Use left hand.','basis':'Both hands free.'}]
    writer._save_writer_draft(key,good(),'Parcel retained.',2,staging_decisions=choices)
    restored=writer._load_writer_draft(key)
    assert restored['staging_decisions']==choices and restored['prompt']==good()
    assert writer._load_writer_draft('b'*64) is None
    path=tmp_path/'prompt_writer_drafts'/(key+'.json')
    d=json.loads(path.read_text());d['contract_digest']='b'*64;path.write_text(json.dumps(d))
    assert writer._load_writer_draft(key) is None


def test_one_pass_saved_draft_still_requires_independent_review(monkeypatch):
    sequence,shots,chars=inputs()
    review_calls=[]
    async def forbidden(*a,**k):raise AssertionError('Must reuse the paid draft')
    async def images(*a,**k):return [],[]
    async def review(prompt,req,*a,**k):
        review_calls.append(prompt)
        assert k['observed_source'] is True
        return {'status':'verified','findings':[],'checked_requirement_ids':[r['id'] for r in req]}
    # Source contract validation is covered by integration tests; this isolates
    # recovery after a known local serialization failure, not a network timeout.
    monkeypatch.setattr(prompt_coverage,'validate_source_contract',lambda *a,**k:[])
    monkeypatch.setattr(writer,'_load_writer_draft',lambda d:{'prompt':good().replace('\nRHEA —','\nRhea —'),'end_state':'Parcel retained.','staging_decisions':[]})
    monkeypatch.setattr(writer,'_save_writer_draft',lambda *a,**k:None)
    monkeypatch.setattr(cp,'reference_images',images)
    monkeypatch.setattr(adapt,'ask_json',forbidden)
    monkeypatch.setattr(writer,'review_prompt',review)
    args=dict(characters=chars,environment=None,aspect_ratio='1:1',look='cg3d',cinematic=True,
              source_verification={'method':'one_pass_production','status':'observed'},production_assets=[])
    result=asyncio.run(writer.write_clip_prompt(sequence,shots,**args))
    assert len(review_calls)==1 and 'RHEA — SPOKEN ON CAMERA:' in result.prompt
    assert result.coverage['resumed_saved_draft'] is True and result.coverage['writer_rounds']==0
    async def reject(*a,**k):return {'status':'needs_review','findings':[{'kind':'source_issue','message':'Speaker contradicts source'}]}
    monkeypatch.setattr(writer,'review_prompt',reject)
    with pytest.raises(writer.WriterError,match='Source contract needs review'):
        asyncio.run(writer.write_clip_prompt(sequence,shots,**args))


def test_source_action_beats_keep_order_and_gpt_staging_after_repeated_serialization():
    shots = inputs()[1]
    shots[0]["action"] = [
        "Rhea looks up with a serious face.",
        "Rhea tilts her head toward the recipient.",
        "Rhea has an eager look while holding the closed parcel.",
    ]
    result = cp.serialize_source_locks(good(), shots, [(0, 4)])
    block = prompt_coverage.shot_blocks(result)[1]
    positions = [block.index(action) for action in shots[0]["action"]]
    assert positions == sorted(positions)
    assert all(block.count(action) == 1 for action in shots[0]["action"])
    assert "Rhea holds the closed parcel and looks up." in block
    assert block.count("**Locked action beats:**") == 1
    assert cp.serialize_source_locks(result, shots, [(0, 4)]) == result


def test_source_action_beats_replace_stale_cached_lock_without_erasing_staging():
    shots = inputs()[1]
    cached = good().replace(
        "**Framing:**", "**Locked action beats:** Rhea drops the parcel and leaves.\n**Framing:**"
    )
    result = cp.serialize_source_locks(cached, shots, [(0, 4)])
    block = prompt_coverage.shot_blocks(result)[1]
    assert "Rhea drops the parcel and leaves." not in result
    assert shots[0]["action"][0] in block
    assert "Rhea holds the closed parcel and looks up." in block
    assert block.count("**Locked action beats:**") == 1
    assert cp.serialize_source_locks(result, shots, [(0, 4)]) == result


@pytest.mark.parametrize("finding_kind", ["prompt_issue", "source_issue"])
def test_serialized_actions_do_not_bypass_independent_contradiction_review(monkeypatch, finding_kind):
    sequence, shots, chars = inputs()
    conflicting = good().replace(
        "Rhea holds the closed parcel and looks up.", "Rhea opens the parcel and exposes its contents."
    )
    draft = {"prompt": conflicting, "end_state": "Rhea retains the closed parcel.", "staging_decisions": []}
    reviews = []

    async def fake_provider(system, user, *args, **kwargs):
        payload = json.loads(user)
        if "shot_texts" not in payload:
            return copy.deepcopy(draft)
        reviews.append(payload)
        shot_text = payload["shot_texts"]["1"]
        assert "Rhea holds a closed parcel." in shot_text
        assert "Rhea opens the parcel and exposes its contents." in shot_text
        requirement = next(r for r in payload["requirements"] if r["kind"] == "action")
        return {
            "status": "needs_revision",
            "checked_requirement_ids": [r["id"] for r in payload["requirements"]],
            "findings": [{"requirement_id": requirement["id"], "kind": finding_kind,
                          "message": "Opening the parcel contradicts the locked closed state."}],
        }

    async def images(*args, **kwargs):
        return [], []

    monkeypatch.setattr(prompt_coverage, "validate_source_contract", lambda *a, **k: [])
    monkeypatch.setattr(writer, "_load_writer_draft", lambda _: copy.deepcopy(draft))
    monkeypatch.setattr(writer, "_save_writer_draft", lambda *a, **k: None)
    monkeypatch.setattr(cp, "reference_images", images)
    monkeypatch.setattr(adapt, "ask_json", fake_provider)
    with pytest.raises(writer.WriterError, match="Opening the parcel contradicts"):
        asyncio.run(writer.write_clip_prompt(
            sequence, shots, characters=chars, environment=None, aspect_ratio="1:1", look="cg3d",
            cinematic=True, production_assets=[],
            source_verification={"method": "one_pass_production", "status": "observed"},
        ))
    assert len(reviews) == (1 if finding_kind == "source_issue" else 3)


def test_one_pass_serializer_uses_approved_target_adaptation_and_preserves_source(monkeypatch):
    from flowboard.services import production_adaptation

    sequence, shots, chars = inputs()
    shot = shots[0]
    shot.update(source_shots=[1], source_appearances=[{"source_shot": 1, "asset_presence": []}])
    shot["action"] = ["Rhea looks down sadly while holding the parcel."]
    shot["camera"] = "Slow orbit"
    shot["production_adaptation"] = {
        "schema_version": 1,
        "reason": "Use the approved hopeful performance and static composition.",
        "source_digest": production_adaptation.source_digest(shot),
        "shot_overrides": {
            "action": ["Rhea has an eager look while holding the closed parcel."],
            "camera": "Static",
            "framing": "MCU",
            "framing_note": "Face and parcel remain visible.",
            "dialogue": [{"who": "RHEA", "line": "A gift for you.", "delivery": "on_camera"}],
        },
        "asset_presence": [],
    }
    original = copy.deepcopy(shots)
    reviewed = []

    async def forbidden(*args, **kwargs):
        raise AssertionError("The cached draft must be repaired locally before review")

    async def images(*args, **kwargs):
        return [], []

    async def review(prompt, requirements, *args, **kwargs):
        reviewed.append(prompt)
        assert "Rhea has an eager look while holding the closed parcel." in prompt
        assert "Rhea looks down sadly" not in prompt
        assert "**Camera:** Static" in prompt and "Slow orbit" not in prompt
        assert "**Framing:** MCU. Face and parcel remain visible." in prompt
        assert "A gift for you." in prompt and "Here it is." not in prompt
        assert [r["value"] for r in requirements if r["kind"] == "action"] == [
            "Rhea has an eager look while holding the closed parcel."
        ]
        return {"status": "verified", "findings": [],
                "checked_requirement_ids": [r["id"] for r in requirements]}

    # Adaptation anchoring is exercised by production_adaptation.apply; the
    # independent source-observation validation has separate integration tests.
    monkeypatch.setattr(prompt_coverage, "validate_source_contract", lambda *a, **k: [])
    monkeypatch.setattr(writer, "_load_writer_draft", lambda _: {
        "prompt": good(), "end_state": "Rhea retains the closed parcel.", "staging_decisions": []
    })
    monkeypatch.setattr(writer, "_save_writer_draft", lambda *a, **k: None)
    monkeypatch.setattr(cp, "reference_images", images)
    monkeypatch.setattr(adapt, "ask_json", forbidden)
    monkeypatch.setattr(writer, "review_prompt", review)
    result = asyncio.run(writer.write_clip_prompt(
        sequence, shots, characters=chars, environment=None, aspect_ratio="1:1", look="cg3d",
        cinematic=True, production_assets=[],
        source_verification={"method": "one_pass_production", "status": "observed"},
    ))
    assert reviewed == [result.prompt.rstrip()]
    assert shots == original


@pytest.mark.parametrize("kind_source", ["design", "definition"])
@pytest.mark.parametrize("asset_kind", ["prop", "environment", "background_group"])
def test_source_model_context_removes_sheet_prompts_without_changing_film_facts(kind_source, asset_kind):
    observation = {
        "asset_id": "red_box", "visibility": "visible", "holder_id": "rhea",
        "state": "Small red box; contents and lid state are not clearly visible.",
        "position": "In Rhea's extended hand", "evidence_ids": ["shot-1-frame-1"],
    }
    wardrobe = {"key": "green_coat", "wardrobe": "Green wool coat, brass buttons.",
                "look": "Adult with dark curly hair."}
    asset = {
        "definition": {"id": "red_box", "description": "A small deep-red box.",
                       "states": ["Observed lid state remains unknown."]},
        "design": {
            "name": "Small red box", "shots": [1],
            "design": {
                "states": ["Shot 1: the lid is fully raised; shot 2: close it."],
                "identity_details": ["Palm-sized, deep red, softly rounded corners."],
                "materials": ["Matte exterior; exact covering is unknown."],
                "render_notes": "Premium modern 3D donghua; restrained highlights.",
            },
        },
        "references": {
            "plate": {"prompt": "SHEET ONLY: Show the box open in shot 1.",
                      "mediaId": "box-plate", "referenceUrl": "https://example.invalid/box.png"},
        },
    }
    asset[kind_source]["kind"] = asset_kind
    context = {
        "assets": {
            "red_box": asset,
            "rhea": {
                "definition": {"id": "rhea", "kind": "character", "states": [wardrobe]},
                "design": {"kind": "character", "states": [wardrobe],
                           "design": {"states": [wardrobe], "hair": "Dark curls",
                                      "render_notes": "Refined 3D facial construction."}},
                "references": {
                    "identity": {"prompt": "SHEET ONLY: Neutral turnaround of Rhea.",
                                 "mediaId": "rhea-identity", "referenceUrl": "https://example.invalid/rhea.png"},
                    "states": {
                        "green_coat": {"prompt": "SHEET ONLY: Green coat front and back.",
                                       "mediaId": "rhea-coat", "referenceUrl": "https://example.invalid/coat.png"},
                    },
                },
            },
        },
        "shots": [{"id": "shot-1", "observed": [observation],
                   "start_state": {"red_box": observation}, "end_state": {"red_box": observation}}],
        "scene_rule": "Rhea keeps the box; no visible transfer is established.",
        "notes": {"prompt": "Preserve this film direction; it is not an image-sheet prompt."},
    }
    original = copy.deepcopy(context)
    projected = cp.source_model_context(context)
    expected = copy.deepcopy(original)
    del expected["assets"]["red_box"]["design"]["design"]["states"]
    del expected["assets"]["red_box"]["references"]["plate"]["prompt"]
    del expected["assets"]["rhea"]["references"]["identity"]["prompt"]
    del expected["assets"]["rhea"]["references"]["states"]["green_coat"]["prompt"]
    assert projected == expected
    assert "SHEET ONLY" not in json.dumps(projected)
    assert context == original
    # The projection is a detached model payload, including the records retained
    # verbatim; later prompt assembly cannot mutate source observations or design.
    projected["shots"][0]["observed"][0]["state"] = "changed only in model payload"
    projected["assets"]["rhea"]["design"]["states"][0]["wardrobe"] = "changed only in model payload"
    assert context == original
