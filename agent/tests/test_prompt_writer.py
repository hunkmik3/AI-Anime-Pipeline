"""The prompt writers: what code fixes, what it checks, and what it falls back to."""
from __future__ import annotations

import asyncio

import pytest

from flowboard.routes import automation as automation_routes
from flowboard.services import automation, prompt_writer
from flowboard.services.video_analyzer import adapt as adapt_mod


def _clip():
    sequence = {"label": "CLIP 07", "title": "The Spider in Her Throat", "summary": "He warns her. Beats: A",
                "duration_s": 5.0, "editing": "cut"}
    shots = [
        {"duration_s": 1.2, "framing": "MCU", "character_keys": ["sienna", "theo"],
         "dialogue": [{"who": "SIENNA", "line": "Sorry about that."}], "action": ["She turns away."]},
        {"duration_s": 1.3, "framing": "MCU", "character_keys": ["theo"],
         "dialogue": [{"who": "THEO", "line": "Promise me you'll go see the nurse."}], "action": ["He waits."]},
    ]
    cast = [{"key": "sienna", "name": "Sienna Rothwell", "ref_label": "@image1", "role": "co-lead",
             "design": {"hair": "Platinum bob.", "costume": [{"piece": "Cropped blazer", "colour": "Ivory"}]}},
            {"key": "theo", "name": "Theo Lambert", "ref_label": "@image2", "role": "lead",
             "summary": "A scholarship junior at Hallwood High.", "design": {}}]
    env = {"name": "Hallwood High Main Locker Corridor", "ref_label": "@image3"}
    return sequence, shots, cast, env


def _good(duration: int, slots) -> str:
    lines = {1: 'SIENNA:\n"Sorry about that."', 2: 'THEO:\n"Promise me you\'ll go see the nurse."'}
    heads = "\n\n".join(f"[SHOT {i} — {prompt_writer._mmss(a)}–{prompt_writer._mmss(b)}]\nBeat.\n{lines.get(i, '')}"
                        for i, (a, b) in enumerate(slots, start=1))
    return (f"CLIP 07 — THE SPIDER IN HER THROAT\nDURATION: {duration} seconds. SHOT COUNT: {len(slots)}.\n\n"
            "[CREATIVES DESCRIPTION]\n@image1 — SIENNA ROTHWELL. Co-lead.\n@image2 — THEO LAMBERT. Lead.\n"
            "@image3 — HALLWOOD HIGH MAIN LOCKER CORRIDOR. Location.\n\n[SPECIFIC TIMELINE]\n\n"
            + heads + '\n\n[OVERALL SUPPLEMENT]\nNo music.')


def test_the_timeline_is_whole_seconds_and_ends_on_the_clip_length():
    shots = [{"duration_s": d} for d in (1.37, 1.23, 2.7, 3.51)]
    slots, duration = prompt_writer.whole_second_timeline(shots, 9)
    assert duration == 9 and slots[-1][1] == 9 and slots[0][0] == 0
    assert all(b - a >= 1 for a, b in slots)
    assert all(slots[i][1] == slots[i + 1][0] for i in range(len(slots) - 1))
    # Shots too short to all get a second push the length out, not a shot away.
    slots, duration = prompt_writer.whole_second_timeline([{"duration_s": 0.4}] * 6, 4)
    assert len(slots) == 6 and duration == 6


def test_the_checks_name_every_fixed_fact_that_is_wrong():
    refs = [("@image1", "SIENNA ROTHWELL"), ("@image2", "THEO LAMBERT")]
    slots = [(0, 2), (2, 4)]
    lines = [(1, "Sorry about that."), (2, "Promise me you'll go see the nurse.")]
    good = ("CLIP 07 — X\nDURATION: 4 seconds. SHOT COUNT: 2.\n@image1 — SIENNA ROTHWELL. a\n"
            "@image2 — THEO LAMBERT. b\n[SHOT 1 — 00:00–00:02]\nSIENNA:\n“Sorry about that.”\n"
            "[SHOT 2 — 00:02–00:04]\nTHEO:\n\"Promise me you’ll go see the nurse.\"\n")
    ok = prompt_writer.check_clip_prompt(good, refs=refs, duration=4, slots=slots, lines=lines,
                                         exempt_shots=set(), school_age=True)
    assert ok == []                                    # curly quotes and apostrophes are the same words
    bad = (good.replace("@image2 — THEO LAMBERT", "@image2 — THEO").replace("DURATION: 4", "DURATION: 5")
           .replace("00:02–00:04", "00:02–00:05").replace("Sorry about that.", "Sorry about his.") + "@image4")
    problems = prompt_writer.check_clip_prompt(bad, refs=refs, duration=4, slots=slots, lines=lines,
                                               exempt_shots=set(), school_age=True)
    text = " ".join(problems)
    assert "@image2 must be declared" in text and "@image4" in text and "DURATION: 4 seconds" in text
    assert "[SHOT 2 — 00:02–00:04]" in text and "Sorry about that." in text


def test_an_undressing_beat_fails_but_a_rule_against_it_does_not():
    base = ("DURATION: 2 seconds.\n@image1 — THEO LAMBERT.\n[SPECIFIC TIMELINE]\n[SHOT 1 — 00:00–00:02]\n{}\n"
            "[OVERALL SUPPLEMENT]\nNo undressing, clothing transparency or nudity.")
    kw = dict(refs=[("@image1", "THEO LAMBERT")], duration=2, slots=[(0, 2)], lines=[],
              exempt_shots=set(), school_age=True)
    assert prompt_writer.check_clip_prompt(base.format("Her clothing stays unchanged. Do not undress anyone."), **kw) == []
    problems = prompt_writer.check_clip_prompt(base.format("Her uniform dissolves into particles."), **kw)
    assert problems and "adapt that beat" in problems[0]


def test_the_writer_is_handed_the_fixed_facts_and_asked_once_more_with_the_problems(monkeypatch):
    sequence, shots, cast, env = _clip()
    asked = []

    async def fake(system, user, stats, **kw):
        asked.append((system, user, kw))
        import json
        ask = json.loads(user)
        slots = [tuple(int(x[:2]) * 60 + int(x[3:]) for x in row["time"].split("–")) for row in ask["shots"]]
        duration = ask["clip"]["duration_seconds"]
        prompt = _good(duration, slots)
        if len(asked) == 1:
            prompt = prompt.replace("Sorry about that.", "Sorry.")       # the first answer loses a line
        stats.answered_by["gpt-6-astra"] = 1
        return {"prompt": prompt, "end_state": "Theo holds the box in his left hand."}

    monkeypatch.setattr(adapt_mod, "ask_json", fake)
    out = asyncio.run(prompt_writer.write_clip_prompt(sequence, shots, characters=cast, environment=env,
                                                      look="cg3d", aspect_ratio="9:16",
                                                      previous_state="She holds the box."))
    assert len(asked) == 2
    import json
    first = json.loads(asked[0][1])
    assert [r["tag"] for r in first["references"]] == ["@image1", "@image2", "@image3"]
    assert first["opening_state"] == "She holds the box." and first["school_age"] is True
    assert first["shots"][1]["time"].endswith(f"00:{out.duration:02d}")
    assert "fix_exactly_these_problems" in json.loads(asked[1][1])
    assert asked[0][2]["model"] == prompt_writer.WRITER_MODEL
    assert out.end_state.startswith("Theo holds") and out.model == "gpt-6-astra"
    assert "THE SPIDER IN HER THROAT" in asked[0][0]            # the header line is spelled out


def test_a_shot_to_rewrite_is_marked_and_its_lines_are_not_held_to_the_words(monkeypatch):
    sequence, shots, cast, env = _clip()
    shots[0]["action"] = ["Her camisole fabric breaks into particles."]

    async def fake(system, user, stats, **kw):
        import json
        ask = json.loads(user)
        assert ask["shots"][0]["rewrite_required"]
        slots = [tuple(int(x[:2]) * 60 + int(x[3:]) for x in row["time"].split("–")) for row in ask["shots"]]
        prompt = _good(ask["clip"]["duration_seconds"], slots).replace("Sorry about that.", "What pen is in my case?")
        return {"prompt": prompt, "end_state": ""}

    monkeypatch.setattr(adapt_mod, "ask_json", fake)
    unsafe = automation.unsafe_shots(shots, cast, env)
    out = asyncio.run(prompt_writer.write_clip_prompt(sequence, shots, characters=cast, environment=env,
                                                      unsafe=unsafe))
    assert "What pen" in out.prompt


def test_the_route_falls_back_to_the_template_and_says_why(monkeypatch):
    sequence, shots, cast, env = _clip()

    async def broken(*a, **kw):
        raise prompt_writer.WriterError("Shot 1 must contain this line")

    monkeypatch.setattr(prompt_writer, "write_clip_prompt", broken)
    body = automation_routes.VideoWriteBody(sequence=sequence, shots=shots, characters=cast, environment=env,
                                            style="cg3d", aspect_ratio="9:16")
    out = asyncio.run(automation_routes.write_video_prompt(body))
    assert out.writer == "template" and "[SPECIFIC TIMELINE]" in out.prompt
    assert out.warnings and "template" in out.warnings[0]
    # An unsafe clip the writer could not adapt is refused, as before.
    shots[0]["action"] = ["Her camisole fabric breaks into particles."]
    body = automation_routes.VideoWriteBody(sequence=sequence, shots=shots, characters=cast, environment=env)
    with pytest.raises(Exception) as err:
        asyncio.run(automation_routes.write_video_prompt(body))
    assert getattr(err.value, "status_code", None) == 422


def test_the_image_writer_rewrites_descriptions_and_keeps_the_contract(monkeypatch):
    draft = ("Create a sheet for THEO.\n\nFORMAT:\n16:9 landscape.\n\nFACE:\nHeart-shaped face.\n\n"
             "HAIR:\nTextured crop.\n\nPOSE:\nStand upright.\n\nNO:\n- watermarks\n")

    async def fake(system, user, stats, **kw):
        import json
        assert set(json.loads(user)["sections"]) == {"FACE", "HAIR"}
        return {"sections": {"FACE": "Heart-shaped face, high cheekbones.", "HAIR": "Chestnut textured crop."}}

    monkeypatch.setattr(adapt_mod, "ask_json", fake)
    out, by = asyncio.run(prompt_writer.write_image_prompt(draft, kind="character", subject="Theo",
                                                           design={"hair": "x"}, style="cg3d"))
    assert "high cheekbones" in out and "Chestnut textured crop." in out
    assert "FORMAT:\n16:9 landscape." in out and "POSE:\nStand upright." in out and "- watermarks" in out
    assert by == prompt_writer.WRITER_MODEL


def test_the_image_writer_keeps_the_draft_when_the_answer_does_not_fit(monkeypatch):
    draft = "FACE:\nHeart-shaped face.\n\nHAIR:\nTextured crop.\n"

    async def fake(system, user, stats, **kw):
        return {"sections": {"FACE": "New face."}}                       # HAIR dropped

    monkeypatch.setattr(adapt_mod, "ask_json", fake)
    out, by = asyncio.run(prompt_writer.write_image_prompt(draft, kind="character", subject="Theo",
                                                           design={"hair": "x"}, style="cg3d"))
    assert (out, by) == (draft, "template")
    # No brief: nothing to write from, no call.
    assert asyncio.run(prompt_writer.write_image_prompt(draft, kind="character", subject="Theo",
                                                        design=None, style="cg3d")) == (draft, "template")


def test_a_shot_with_a_line_is_never_rounded_below_what_the_line_needs():
    shots = [{"duration_s": 1.37, "dialogue": [{"who": "S", "line": "Sorry about that."}]},
             {"duration_s": 2.6}, {"duration_s": 2.6}]
    slots, duration = prompt_writer.whole_second_timeline(shots, 7)
    assert slots[0] == (0, 2)                     # 1.4 s of speech is two whole seconds, not one
    assert duration == 7 and slots[-1][1] == 7


def test_the_examples_are_never_the_clip_being_written_nor_its_neighbours():
    names = lambda label: [p.name for p in prompt_writer.examples_for(label)]
    assert names("CLIP 02") == ["clip-07.txt", "clip-08.txt"]
    assert names("CLIP 07") == ["clip-03.txt", "clip-02.txt"]
    assert names("CLIP 01") == ["clip-03.txt", "clip-07.txt"]
    assert all(p.exists() for p in prompt_writer.examples_for("CLIP 05"))


def test_underwear_in_a_school_age_shot_is_handed_over_to_be_rewritten(monkeypatch):
    sequence, shots, cast, env = _clip()
    shots[0]["action"] = ["The boy in black lingerie walks past."]
    seen = {}

    async def fake(system, user, stats, **kw):
        import json
        ask = json.loads(user)
        seen["rewrite"] = ask["shots"][0].get("rewrite_required")
        slots = [tuple(int(x[:2]) * 60 + int(x[3:]) for x in row["time"].split("–")) for row in ask["shots"]]
        prompt = _good(ask["clip"]["duration_seconds"], slots).replace("Sorry about that.", "Nice bag.")
        return {"prompt": prompt, "end_state": ""}

    monkeypatch.setattr(adapt_mod, "ask_json", fake)
    out = asyncio.run(prompt_writer.write_clip_prompt(sequence, shots, characters=cast, environment=env))
    assert seen["rewrite"] == "lingerie" and "Nice bag." in out.prompt


def test_the_sheets_style_note_becomes_the_clips_style_and_a_look_names_the_costume():
    sequence, shots, cast, env = _clip()
    cast[1]["design"] = {}
    cast[1]["wardrobe"] = "Light grey school suit jacket, white shirt, plaid tie, khaki trousers"
    fitted = automation.fit_shots_to_lines(shots)
    slots, duration = prompt_writer.whole_second_timeline(fitted, 5)
    ask = prompt_writer._payload(sequence, fitted, slots, duration, characters=cast, environment=env,
                                 look="cg3d", aspect_ratio="9:16", previous_state="", unsafe={},
                                 school_age=True, style_note="Premium stylized 3D donghua.")
    assert ask["clip"]["style"] == "Premium stylized 3D donghua."
    assert ask["references"][1]["keep"] == "Light grey school suit jacket, white shirt"
