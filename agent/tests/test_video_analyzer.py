"""Video-to-shotlist analyzer — the parts that must never need a model.

The analyzer's whole design rests on one split: the machine measures the
timeline, models only describe what is inside it. So the tests here cover the
measured side and the checks that defend it — cut spans, clip planning,
validation, export — and never call a model or decode a video.

Measured behaviour that IS covered by a live run rather than here (cut F1,
ASR model choice, vision escalation) is recorded in the module docstrings of
cuts.py, asr.py and vision.py.
"""
from __future__ import annotations

import pytest

from flowboard.routes import automation as automation_routes
from flowboard.services import automation
from flowboard.services.video_analyzer import board, dialogue, export, validate
from flowboard.services.video_analyzer.adapt import tile_sequences
from flowboard.services.video_analyzer.frames import sample_points, spans_from_cuts
from flowboard.services.video_analyzer.probe import VideoMeta, timecode


# ── timeline ───────────────────────────────────────────────────────────────


def test_spans_cover_the_whole_video_without_gaps():
    spans = spans_from_cuts([1.5, 4.25], 10.0)
    assert [(s.index, s.start, s.end) for s in spans] == [
        (1, 0.0, 1.5),
        (2, 1.5, 4.25),
        (3, 4.25, 10.0),
    ]
    # Contiguity is the property everything downstream assumes.
    assert all(a.end == b.start for a, b in zip(spans, spans[1:]))


def test_cuts_outside_the_video_are_ignored():
    assert len(spans_from_cuts([-2.0, 3.0, 99.0], 10.0)) == 2


def test_sample_points_follow_the_shot_length_bands():
    assert len(sample_points(0.4)) == 2
    assert len(sample_points(2.0)) == 3
    assert len(sample_points(9.0)) == 5


def test_timecode_is_mm_ss_cc():
    assert timecode(0) == "00:00.00"
    assert timecode(75.5) == "01:15.50"


# ── validation ─────────────────────────────────────────────────────────────


def _shots(*spans, source=True):
    return [
        {
            "shot": i,
            "start": a,
            "end": b,
            "dialogue": "",
            "source": {"confidence": 0.9, "action": "x"} if source else None,
        }
        for i, (a, b) in enumerate(spans, start=1)
    ]


FRAME = 1 / 30


def test_validate_passes_a_contiguous_shotlist():
    report = validate.validate(_shots((0.0, 2.0), (2.0, 5.0)), duration=5.0, frame=FRAME)
    assert report.ok and not report.findings


def test_validate_catches_a_gap_an_overlap_and_a_short_video():
    gap = validate.validate(_shots((0.0, 2.0), (2.5, 5.0)), duration=5.0, frame=FRAME)
    assert not gap.ok and any(f.code == "gap" for f in gap.findings)

    overlap = validate.validate(_shots((0.0, 2.6), (2.5, 5.0)), duration=5.0, frame=FRAME)
    assert any(f.code == "overlap" for f in overlap.findings)

    short = validate.validate(_shots((0.0, 2.0), (2.0, 5.0)), duration=9.0, frame=FRAME)
    assert any(f.code == "end_mismatch" for f in short.findings)


def test_validate_flags_an_impact_flash_and_an_unseen_shot_without_failing():
    shots = _shots((0.0, 0.2), (0.2, 5.0))
    shots[1]["source"] = None
    report = validate.validate(shots, duration=5.0, frame=FRAME)
    codes = {f.code for f in report.findings}
    assert codes == {"suspicious_cut", "not_analysed"}
    # Both are for a person to look at — neither is a structural error.
    assert report.ok


def test_validate_catches_a_dropped_shot_and_a_name_the_glossary_missed():
    shots = _shots((0.0, 2.0), (2.0, 5.0))
    glossary = {"characters": {"Dương Viêm": "Kagari Ren"}}
    adapted = {1: {"title": "A", "action": ["Dương Viêm steps forward."]}}
    report = validate.validate(
        shots, duration=5.0, frame=FRAME, glossary=glossary, adapted=adapted
    )
    codes = {f.code for f in report.findings}
    assert "not_adapted" in codes      # shot 2 never came back
    assert "glossary_leak" in codes    # shot 1 kept the source name
    assert not report.ok


def test_validate_does_not_call_a_kept_name_a_leak():
    shots = _shots((0.0, 5.0))
    report = validate.validate(
        shots,
        duration=5.0,
        frame=FRAME,
        glossary={"locations": {"Tiên Minh": "Tiên Minh"}},
        adapted={1: {"action": ["Tiên Minh burns."]}},
    )
    assert not [f for f in report.findings if f.code == "glossary_leak"]


# ── sequences and clips ────────────────────────────────────────────────────


def test_tile_sequences_repairs_a_grouping_that_leaves_shots_out():
    tiled = tile_sequences(
        [{"first_shot": 1, "last_shot": 3, "title": "a"}, {"first_shot": 6, "last_shot": 8, "title": "b"}],
        10,
    )
    assert [(q["first_shot"], q["last_shot"]) for q in tiled] == [(1, 5), (6, 10)]


def test_tile_sequences_falls_back_to_one_sequence():
    assert tile_sequences([], 7) == [
        {"first_shot": 1, "last_shot": 7, "title": "Full video", "goal": "", "conflict": "", "beats": []}
    ]


def _even_shots(count: int, each: float):
    return [
        {"shot": i, "start": round((i - 1) * each, 3), "end": round(i * each, 3)}
        for i in range(1, count + 1)
    ]


def test_clips_stay_inside_seedance_limits_and_keep_every_shot():
    shots = _even_shots(40, 1.0)  # 40s of one-second shots
    clips = board.plan_clips(shots, [{"first_shot": 1, "last_shot": 40}])
    numbers = [n for c in clips for n in c["shots"]]
    assert numbers == list(range(1, 41))  # nothing dropped, nothing twice
    for c in clips:
        length = sum(shots[n - 1]["end"] - shots[n - 1]["start"] for n in c["shots"])
        assert board.MIN_CLIP_S <= length <= board.HARD_MAX_S


def test_a_short_tail_joins_the_clip_before_it_rather_than_standing_alone():
    # 16s of shots: a 15s clip closes, and the 1s tail must not become its own clip.
    shots = _even_shots(16, 1.0)
    clips = board.plan_clips(shots, [{"first_shot": 1, "last_shot": 16}])
    assert len(clips) == 1 and len(clips[0]["shots"]) == 16


def test_a_sequence_too_short_for_a_clip_joins_its_neighbour():
    shots = _even_shots(12, 1.0)
    clips = board.plan_clips(
        shots, [{"first_shot": 1, "last_shot": 10}, {"first_shot": 11, "last_shot": 12}]
    )
    assert len(clips) == 1 and len(clips[0]["shots"]) == 12


def test_board_shot_carries_the_reference_timecode_and_maps_the_framing():
    shot = {"shot": 7, "start": 12.5, "end": 13.1, "source": {"shot_size": "EWS", "camera_angle": "low angle"}}
    out = board.board_shot(1, shot, {"title": "FALL", "action": ["He drops."]}, {})
    assert out["framing"] == "EWS"                   # preserve the source framing distinction
    assert out["duration_s"] == 0.6
    assert out["source_shot"] == 7 and out["source_tc"] == "00:12.50–00:13.10"
    for framing in ("MWS", "POV"):
        shot['source']['shot_size']=framing
        assert board.board_shot(1,shot,{}, {})['framing']==framing


def _shot(n, dur, **extra):
    return {"n": n, "duration_s": dur, "title": f"S{n}", "action": [f"a{n}"],
            "dialogue": [], "performance": [], "avoid": [], "sfx": [],
            "character_keys": [], "source_shot": n, "source_tc": f"00:0{n}.00–00:0{n}.50",
            **extra}


def test_shots_the_model_cannot_cut_are_folded_into_a_shootable_one():
    # Seedance floors a delivered shot near a second and silently drops the
    # rest, so a run of inserts becomes one shot of its own rather than five
    # that vanish. Beats and lines survive the fold.
    shots = [_shot(1, 0.3, dialogue=[{"who": "A", "line": "hi"}]), _shot(2, 0.3),
             _shot(3, 0.5), _shot(4, 2.0), _shot(5, 0.2)]
    out = board._merge_unshootable(shots, floor=1.0)
    assert [s["duration_s"] for s in out] == [1.1, 2.2]
    assert [s["n"] for s in out] == [1, 2]
    assert out[0]["source_shots"] == [1, 2, 3]       # what it now plays
    assert out[0]["action"] == ["a1", "a2", "a3"]    # nothing thrown away
    assert out[0]["dialogue"] == [{"who": "A", "line": "hi"}]
    assert out[1]["source_shots"] == [4, 5]          # a short tail joins the shot before it


def test_a_shot_already_long_enough_is_left_exactly_as_it_was():
    shots = [_shot(1, 2.0), _shot(2, 1.5)]
    out = board._merge_unshootable(shots, floor=1.0)
    assert [s["duration_s"] for s in out] == [2.0, 1.5]
    assert [s["title"] for s in out] == ["S1", "S2"]


# ── what the board's prompts do with a re-made clip ────────────────────────


def test_a_reference_clip_is_generated_long_enough_to_hold_its_last_shot():
    # Rounding to nearest would return 12 and cut the final shot off.
    assert automation.video_duration_for(12.4) == 13
    assert automation.video_duration_for(12.0) == 12
    assert automation.video_duration_for(0.5) == automation.VIDEO_MIN_S


def test_cut_mode_keeps_every_shot_in_order_at_its_timestamp():
    sequence = {"title": "Clash", "summary": "They fight.", "duration_s": 6.4, "editing": "cut"}
    shots = [
        {"title": "STRIKE", "duration_s": 0.4, "framing": "CU", "action": ["He strikes."]},
        {"title": "BLAST", "duration_s": 6.0, "framing": "WIDE", "action": ["A blast lands."]},
    ]
    prompt = automation.build_video_prompt(sequence, shots, characters=[], environment=None, look="anime")
    assert "[SHOT 1 — 00:00–00:00.4]" in prompt      # sub-second shots keep their decimal
    assert "[SHOT 2 — 00:00.4–00:06.4]" in prompt
    assert "Do not add, remove or reorder shots." in prompt
    assert "One continuous scene" not in prompt      # that is the drama mode's rule


def test_drama_mode_is_one_continuous_scene():
    sequence = {"title": "Kitchen", "summary": "She waits.", "duration_s": 8}
    prompt = automation.build_video_prompt(
        sequence, [{"title": "WAIT", "duration_s": 8, "framing": "MS", "action": ["She waits."]}],
        characters=[], environment=None,
    )
    assert "One continuous scene with no fast cuts." in prompt
    assert "Cinematic realistic" in prompt
    assert "Do not add, remove or reorder shots." not in prompt


# ── the section format the user's hand-written prompts use ────────────────


def _office_clip():
    sequence = {"title": "The Price", "summary": "Marika names her price. Beats: A; B",
                "duration_s": 5.0, "editing": "cut"}
    shots = [
        {"duration_s": 2.4, "framing": "CU", "camera": "Close-up on Marika's face.",
         "character_keys": ["marika"], "action": ["She looks up from the laptop."],
         "dialogue": [{"who": "MARIKA", "line": "There's only one way to settle this."}],
         "sfx": ["laptop fan", "clock tick"]},
        {"duration_s": 2.6, "framing": "MCU", "character_keys": ["voss"],
         "action": ["He straightens."], "avoid": ["turn his head"]},
    ]
    cast = [
        {"key": "marika", "name": "Marika", "ref_label": "@image1",
         "design": {"hair": "Long wavy purple hair", "costume": [
             {"piece": "halter dress", "colour": "Deep purple"}]}},
        {"key": "voss", "name": "Voss", "ref_label": "@image2", "design": {}},
    ]
    env = {"name": "President Office", "ref_label": "@image3"}
    return automation.build_video_prompt(sequence, shots, characters=cast, environment=env,
                                         look="anime", aspect_ratio="1:1")


def test_the_six_sections_come_in_the_working_order():
    prompt = _office_clip()
    order = ["[CREATIVES DESCRIPTION]", "FORMAT: 1:1.", "[ONE-SENTENCE SUMMARY]",
             "[SPECIFIC TIMELINE]", "[OVERALL SUPPLEMENT]"]
    at = [prompt.index(x) for x in order]
    assert at == sorted(at)
    # The standing rules close the prompt — nothing after them.
    assert prompt.rstrip().endswith("No AI artifacts.")


def test_a_character_is_a_hard_reference_and_a_location_is_reference_only():
    # The old template told Seedance to lock the location to its plate, which
    # pastes the plate behind every shot. The working prompts do the opposite.
    prompt = _office_clip()
    assert "@image1 — MARIKA, HARD CHARACTER REFERENCE." in prompt
    assert "long wavy purple hair" in prompt and "deep purple halter dress" in prompt
    assert "@image3 — PRESIDENT OFFICE, ENVIRONMENT REFERENCE ONLY." in prompt
    assert "DO NOT reproduce @image3 as an exact background" in prompt
    assert "newly generated angle" in prompt


def test_a_line_sits_once_inside_its_own_shot_under_the_speakers_name():
    prompt = _office_clip()
    assert 'MARIKA:\n"There\'s only one way to settle this."' in prompt
    assert prompt.count("There's only one way to settle this.") == 1
    # A silent shot in a talking clip says so, so the model does not fill it.
    shot2 = prompt[prompt.index("[SHOT 2"):prompt.index("[OVERALL SUPPLEMENT]")]
    assert "There is NO dialogue in this shot." in shot2
    assert "Only the specified English dialogue. No additional dialogue." in prompt


def test_sound_is_ordered_and_a_negative_stays_inside_its_shot():
    prompt = _office_clip()
    assert "SFX: laptop fan → clock tick." in prompt
    shot2 = prompt[prompt.index("[SHOT 2"):prompt.index("[OVERALL SUPPLEMENT]")]
    assert "Do NOT turn his head." in shot2


def test_two_speakers_get_a_blocking_section_only_when_a_shot_places_them():
    prompt = _office_clip()
    assert "[BLOCKING]" not in prompt      # no shot here holds both of them


def test_a_school_cast_carries_the_guard_the_working_prompts_carry():
    sequence = {"title": "Hall", "summary": "They meet.", "duration_s": 4, "editing": "cut"}
    cast = [{"key": "t", "name": "Theo", "ref_label": "@image1",
             "role": "lead", "summary": "A scholarship junior at Hallwood High.", "design": {}}]
    prompt = automation.build_video_prompt(
        sequence, [{"duration_s": 4, "framing": "MS", "character_keys": ["t"], "action": ["He walks."]}],
        characters=cast, environment=None, look="cg3d")
    assert "No nudity, no underwear, no clothing removal" in prompt


def test_a_shot_grows_to_fit_its_line_and_the_clip_grows_with_it():
    # Seedance speaks ~2.2 words a second; a reference actor fired this line
    # into 1.37 seconds, and a 17s generation would have cut it off.
    sequence = {"duration_s": 3.37}
    shots = [
        {"duration_s": 2.0, "dialogue": [{"who": "A", "line": "Wait."}]},
        {"duration_s": 1.37, "dialogue": [{"who": "A", "line": "Promise me you'll go see the nurse."}]},
    ]
    fitted = automation.fit_shots_to_lines(shots)
    assert fitted[0]["duration_s"] == 2.0                    # the line fits: left as measured
    assert fitted[1]["duration_s"] >= 7 / automation.SPEECH_WPS
    assert shots[1]["duration_s"] == 1.37                    # copies, not the caller's shots
    assert automation.clip_seconds(sequence, shots) == automation.video_duration_for(
        2.0 + fitted[1]["duration_s"])
    prompt = automation.build_video_prompt(sequence, shots, characters=[], environment=None)
    assert f"Total runtime: about {automation.clip_seconds(sequence, shots)} seconds." in prompt


def test_a_line_heard_over_someone_elses_shot_is_marked_off_screen_and_continuing():
    sequence = {"duration_s": 4.7, "editing": "cut"}
    cast = [{"key": "theo", "name": "Theo Lambert", "ref_label": "@image1", "design": {}},
            {"key": "sienna", "name": "Sienna Rothwell", "ref_label": "@image2", "design": {}}]
    shots = [
        {"duration_s": 2.3, "framing": "MCU", "character_keys": ["theo"],
         "dialogue": [{"who": "THEO", "line": "No, listen, in ten minutes,"}]},
        {"duration_s": 2.4, "framing": "MCU", "character_keys": ["sienna"],
         "dialogue": [{"who": "THEO", "line": "if you don't see the nurse, you will die.", "cont": True}]},
    ]
    prompt = automation.build_video_prompt(sequence, shots, characters=cast, environment=None)
    assert 'THEO:\n"No, listen, in ten minutes,"' in prompt
    assert 'THEO (continuing, off-screen):\n"if you don\'t see the nurse, you will die."' in prompt
    assert "There is NO dialogue in this shot." not in prompt


def test_a_shot_keeps_the_working_prompts_density():
    sequence = {"duration_s": 3, "editing": "cut"}
    cast = [{"key": "theo", "name": "Theo", "ref_label": "@image1", "design": {}}]
    shot = {
        "duration_s": 3, "framing": "OTS", "character_keys": ["theo"],
        "camera": "Over-the-shoulder, eye-level, static, shallow depth of field (~T2.2) holding his eyes",
        "framing_note": "A grey suit shoulder fills the left third; Theo sits in the right two-thirds; lockers behind",
        "action": ["He leans in.", "He speaks without blinking.", "His jaw sets.", "He holds the stare."],
        "performance": ["Brows low."],
        "dialogue": [{"who": "THEO", "line": "You're going to die."}],
        "avoid": ["Do not move the camera", "Do not have him blink"],
        "sfx": ["his low urgent line", "crowd murmur", "sneaker squeak", "locker slam", "bell"],
    }
    prompt = automation.build_video_prompt(sequence, [shot], characters=cast, environment=None)
    body = prompt[prompt.index("[SHOT 1"):prompt.index("[OVERALL SUPPLEMENT]")]
    assert "Over-the-shoulder shot. Over-the-shoulder, eye-level, static." in body   # lens notes gone
    assert "Theo sits in the right two-thirds." in body          # the clause that names him
    assert "grey suit shoulder" not in body and "lockers behind" not in body
    assert "speaks without blinking" not in body                 # the NAME: block says that
    assert body.count("\nHe ") + body.count("\nHis ") == 3      # three acting lines, no more
    assert "Do not have him blink." in body and "move the camera" not in body
    assert "SFX: crowd murmur → sneaker squeak → locker slam." in body


def test_blocking_comes_from_a_shot_that_places_both_characters():
    sequence = {"duration_s": 5, "editing": "cut"}
    cast = [{"key": "theo", "name": "Theo", "ref_label": "@image1", "design": {}},
            {"key": "sienna", "name": "Sienna", "ref_label": "@image2", "design": {}}]
    shots = [
        {"duration_s": 2, "framing": "OTS", "character_keys": ["theo", "sienna"],
         "framing_note": "Out-of-focus shoulder fills the left third; Sienna sharp beyond"},
        {"duration_s": 3, "framing": "MS", "character_keys": ["theo", "sienna"],
         "framing_note": "Sienna frame left, Theo frame right facing her; lockers behind"},
    ]
    prompt = automation.build_video_prompt(sequence, shots, characters=cast, environment=None)
    blocking = prompt[prompt.index("[BLOCKING]"):prompt.index("[ONE-SENTENCE SUMMARY]")]
    assert "Sienna frame left, Theo frame right facing her." in blocking
    assert "shoulder" not in blocking


def test_anime_style_never_asks_for_photoreal_skin():
    prompt = automation.build_character_prompt(
        {"name": "Kagari Ren", "role": "lead"}, {"look": "lean", "wardrobe": "grey robe"},
        has_reference=False, style="anime",
    )
    assert "anime character model sheet" in prompt
    assert "photorealistic" not in prompt


# ── export ─────────────────────────────────────────────────────────────────


def _analysis():
    return {
        "video": VideoMeta(duration=5.0, fps=30, width=1080, height=1920, has_audio=True).as_dict(),
        "sequences": [{"first_shot": 1, "last_shot": 2, "title": "Open", "goal": "start"}],
        "shots": [
            {"shot": 1, "start": 0.0, "end": 2.0, "dialogue": "Chào", "frames": [],
             "source": {"shot_size": "WS", "camera_angle": "low angle", "camera_movement": "static",
                        "action": "He enters.", "title_card": "Ma Tôn", "confidence": 0.9}},
            {"shot": 2, "start": 2.0, "end": 5.0, "dialogue": "", "frames": [],
             "source": {"shot_size": "CU", "camera_angle": "eye-level", "camera_movement": "push-in",
                        "action": "He turns.", "confidence": 0.8}},
        ],
        "validation": {"ok": True, "errors": 0, "warnings": 0, "findings": []},
    }


def test_json_export_keeps_source_and_adaptation_apart():
    data = export.to_json(_analysis(), {"glossary": {"characters": {"Ma Tôn": "Kuroda Ryūma"}},
                                        "shots": {"1": {"title": "ENTRY"}}})
    first = data["shots"][0]
    assert first["timeline"] == {
        "start": 0.0, "end": 2.0, "duration": 2.0,
        "start_tc": "00:00.00", "end_tc": "00:02.00",
        # Frames as well as seconds: a cut is conformed in frames.
        "start_frame": 0, "end_frame": 59, "frames": 60,
    }
    assert first["source"]["action"] == "He enters."       # the reference, untouched
    assert first["adaptation"]["title"] == "ENTRY"          # the re-telling, beside it
    assert data["shots"][1]["adaptation"] is None


def test_markdown_export_groups_by_sequence_and_escapes_pipes():
    analysis = _analysis()
    analysis["shots"][0]["source"]["action"] = "He enters | quickly"
    md = export.to_markdown(analysis, None)
    assert "## Sequence 01 — Open · shots 001–002" in md
    assert "| 001 | 00:00.00–00:02.00<br>f0–59 |" in md
    assert "He enters \\| quickly" in md                    # a pipe must not break the table


def test_markdown_export_shows_the_glossary_when_there_is_one():
    md = export.to_markdown(_analysis(), {"glossary": {"characters": {"Ma Tôn": "Kuroda Ryūma"}},
                                          "shots": {}})
    assert "| characters | Ma Tôn | Kuroda Ryūma |" in md


@pytest.mark.parametrize("mode", ["short", "detailed", "generation"])
def test_every_export_mode_renders(mode):
    md = export.to_markdown(
        _analysis(),
        {"glossary": {}, "shots": {"1": {"title": "ENTRY", "action": ["He enters."],
                                         "avoid": ["no smile"], "sfx": ["footstep"]}}},
        mode=mode,
    )
    assert "001" in md


# ── dialogue: one line, once ───────────────────────────────────────────────


def _subtitled(*pairs):
    """(shot, subtitle) → shots carrying that subtitle, 1s each."""
    return [
        {"shot": n, "start": float(n - 1), "end": float(n), "dialogue": "",
         "source": {"subtitle": sub}}
        for n, sub in pairs
    ]


def test_a_subtitle_held_across_cuts_becomes_one_line_on_the_shot_it_starts_in():
    shots = _subtitled(
        (1, "Consider today's training\ncomplete."),
        (2, "Consider today's training\ncomplete."),
        (3, None),
    )
    track = dialogue.from_subtitles(shots)
    assert [(l.first_shot, l.last_shot, l.text) for l in track] == [
        (1, 1, "Consider today's training complete.")
    ]

    dialogue.attach(shots, track)
    assert shots[0]["dialogue_lines"] == ["Consider today's training complete."]
    assert shots[1]["dialogue_lines"] == [] and shots[2]["dialogue_lines"] == []


def test_a_scrolling_caption_is_stitched_rather_than_repeated():
    # The second reading repeats the tail of the first — the classic two-line
    # caption moving up as the next line appears.
    shots = _subtitled(
        (1, "If today we cannot kill him,"),
        (2, "If today we cannot kill him, every living soul"),
        (3, "every living soul will fall into danger."),
    )
    track = dialogue.from_subtitles(shots)
    assert len(track) == 1
    assert track[0].text == "If today we cannot kill him, every living soul will fall into danger."
    assert (track[0].first_shot, track[0].last_shot) == (1, 3)


def test_a_shot_in_the_middle_of_a_line_says_which_shot_it_continues():
    shots = _subtitled((1, "I will ask you"), (2, "one last time."), (3, "Silence."))
    dialogue.attach(shots, dialogue.from_subtitles(shots))
    assert shots[1]["dialogue_continues"] == 1
    assert shots[2]["dialogue_continues"] is None
    assert shots[2]["dialogue_lines"] == ["Silence."]


def test_both_lines_of_one_caption_survive_even_when_one_repeats_the_other():
    # "Mau đi đi. / Đi đi." is one person saying both, in one caption.
    shots = _subtitled((1, "Get going.\nGo."))
    assert [l.text for l in dialogue.from_subtitles(shots)] == ["Get going.", "Go."]


def test_the_same_caption_a_shot_later_is_the_same_line_not_a_new_one():
    shots = _subtitled((1, "Go."), (2, "Go."))
    assert len(dialogue.from_subtitles(shots)) == 1


def test_speech_carries_the_track_when_nothing_is_burned_in():
    shots = [{"shot": 1, "start": 0.0, "end": 2.0, "dialogue": "", "source": {}},
             {"shot": 2, "start": 2.0, "end": 4.0, "dialogue": "", "source": {}}]
    transcript = {"segments": [{"start": 0.5, "end": 3.0, "text": "One sentence across a cut."}]}
    track = dialogue.build_track(shots, transcript)
    assert [(l.source, l.first_shot, l.last_shot) for l in track] == [("asr", 1, 2)]
    dialogue.attach(shots, track)
    assert shots[0]["dialogue"] == "One sentence across a cut."
    assert shots[1]["dialogue"] == "" and shots[1]["dialogue_continues"] == 1


def test_validation_notices_a_line_written_in_two_shots():
    report = validate.validate(
        _shots((0.0, 2.0), (2.0, 5.0)),
        duration=5.0,
        frame=FRAME,
        adapted={
            1: {"dialogue": [{"who": "A", "line": "Consider today's training complete."}]},
            2: {"dialogue": [{"who": "B", "line": "Consider today's training complete."}]},
        },
    )
    assert [f.code for f in report.findings if f.code == "dialogue_repeat"] == ["dialogue_repeat"]


# ── the three ways a clip is kept consistent ───────────────────────────────
#
# Probed live against Seedance 2.5 on the B2B endpoint (2026-09-18):
#   · a first/last frame of PHOTOREAL people is refused outright
#     ("input image may contain real person"), and cannot be combined with a
#     KYC identity ("first/last frame content cannot be mixed with reference
#     media content") — so keyframes are for drawn styles;
#   · a reference VIDEO of the same people IS accepted, which is what makes
#     "extend" the live-action answer;
#   · extension takes its ratio from the video it continues ("adaptive"), and
#     any other ratio is refused.
# These tests pin the request each mode builds, so a refactor cannot silently
# send the combination the provider rejects.


class _FakeProvider:
    def __init__(self):
        self.params = None

    async def run_to_completion(self, params):
        self.params = params
        return {}, {"status": "succeeded", "video_bytes": b"mp4", "video_url": None}


@pytest.fixture
def clip_call(monkeypatch):
    """Call generate_clip against a fake provider and return the params sent."""
    from flowboard.services.video import registry as video_registry

    provider = _FakeProvider()
    monkeypatch.setattr(video_registry, "register_defaults", lambda: None)
    monkeypatch.setattr(video_registry, "get_video_provider", lambda _model: provider)
    monkeypatch.setattr(automation, "_publish_bytes", lambda blob, ext: "https://r2/clip.mp4")

    def run(**kwargs):
        import asyncio

        asyncio.run(automation.generate_clip("PROMPT", **kwargs))
        return provider.params

    return run


def test_keyframe_mode_sends_the_two_frames_and_nothing_else(clip_call):
    params = clip_call(
        reference_urls=["https://r2/sheet.png"],
        duration_seconds=12,
        first_frame_url="https://r2/first.png",
        last_frame_url="https://r2/last.png",
        kyc_media_ids=["media-1"],
    )
    assert params["first_frame_url"] == "https://r2/first.png"
    assert params["last_frame_url"] == "https://r2/last.png"
    # Both would make the provider drop the last frame, or refuse the request.
    assert "reference_images" not in params
    assert "kyc_image_asset_ids" not in params


def test_chained_mode_extends_the_previous_clip_and_follows_its_shape(clip_call):
    params = clip_call(
        reference_urls=["https://r2/sheet.png"],
        duration_seconds=9,
        aspect_ratio="16:9",
        previous_clip_url="https://r2/prev.mp4",
        chain="extend",
    )
    assert params["reference_videos"] == ["https://r2/prev.mp4"]
    assert params["omni_reference_task_type"] == "extend"
    # Extension is refused with any ratio but the source's own.
    assert params["aspect_ratio"] == "adaptive"
    assert "reference_images" not in params


def test_chained_reference_mode_keeps_the_boards_own_ratio(clip_call):
    params = clip_call(
        reference_urls=[],
        duration_seconds=9,
        aspect_ratio="9:16",
        previous_clip_url="https://r2/prev.mp4",
        chain="reference",
    )
    assert params["omni_reference_task_type"] == "reference"
    assert params["aspect_ratio"] == "9:16"


def test_reference_mode_is_unchanged(clip_call):
    params = clip_call(reference_urls=["https://r2/a.png", "https://r2/b.png"], duration_seconds=10)
    assert params["reference_images"] == ["https://r2/a.png", "https://r2/b.png"]
    assert params["omni_reference_task_type"] == "reference"
    assert "first_frame_url" not in params


def test_a_clip_with_no_input_at_all_is_refused(clip_call):
    with pytest.raises(automation.AutomationError):
        clip_call(reference_urls=[], duration_seconds=10)


def test_clip_planning_follows_the_length_the_user_picked():
    shots = _even_shots(60, 1.0)
    short = board.plan_clips(shots, [{"first_shot": 1, "last_shot": 60}], max_clip_s=10)
    long = board.plan_clips(shots, [{"first_shot": 1, "last_shot": 60}], max_clip_s=30)
    assert len(short) > len(long)          # fewer, longer clips = fewer seams
    for c in long:                          # and never past Seedance's own limit
        assert sum(shots[n - 1]["end"] - shots[n - 1]["start"] for n in c["shots"]) <= board.HARD_MAX_S


# ── the veto that separates a cut from an invisible seam ───────────────────
#
# Measured on a 233s CGI trailer assembled from 4-second generated segments:
# 21 of 89 shipped "cuts" had identical pictures either side and landed on exact
# multiples of four seconds — the seam between two generated segments, invisible
# to a viewer and a spike to a threshold detector. Comparing the picture 8
# frames (0.27s) apart tells the two cases apart; adjacent frames do not,
# because a cut hidden in a whip-pan has blur on both sides.


def _scene(value: int, n: int = 40):
    import numpy as np
    return np.full((n, 114, 64), value, dtype=np.uint8)


def test_the_picture_either_side_of_a_real_boundary_stays_different():
    import numpy as np
    from flowboard.services.video_analyzer import cuts as cuts_mod

    rng = np.random.default_rng(7)
    before = rng.integers(0, 255, (20, 114, 64), dtype=np.uint8)
    after = rng.integers(0, 255, (20, 114, 64), dtype=np.uint8)
    thumbs = np.concatenate([before, after])
    # frame 20 is the first of the new scene
    assert cuts_mod._unchanged(thumbs, 20) < cuts_mod._VETO_SIM


def test_an_invisible_seam_reads_as_the_same_picture():
    import numpy as np
    from flowboard.services.video_analyzer import cuts as cuts_mod

    rng = np.random.default_rng(11)
    base = rng.integers(0, 255, (114, 64), dtype=np.uint8)
    # one shot throughout, plus the pixel-level jitter an encoder seam makes
    thumbs = np.stack([np.clip(base + rng.integers(-2, 3, base.shape), 0, 255).astype(np.uint8)
                       for _ in range(40)])
    assert cuts_mod._unchanged(thumbs, 20) > cuts_mod._VETO_SIM


def test_strong_change_outranks_the_motion_referee():
    from flowboard.services.video_analyzer import cuts as cuts_mod

    # The rule the referee must yield to: a candidate whose picture is no longer
    # the same picture is a cut even when similarity decays like motion.
    assert cuts_mod._CHANGED_SIM < cuts_mod._VETO_SIM


def test_the_cast_bible_owns_the_names():
    """The glossary follows the sheets, not the other way round.

    Measured failure this guards: a Vietnamese drama whose glossary romanised
    "Dương Viêm" to "Yang Yan" while the character sheet said "Dương Viêm" —
    the adapted shotlist then used the romanisation 230 times and the sheet's
    name 20, for one person.
    """
    from flowboard.services.video_analyzer.pipeline import _reconcile_names

    glossary = {"characters": {
        "Dương Viêm": "Yang Yan",
        "Viêm nhi": "Yang Yan",          # an alias, same target
        "Ma Tôn": "Demon Sovereign",
    }}
    cast = {"characters": [{"source_name": "Dương Viêm", "name": "Dương Viêm"}]}
    out = _reconcile_names(glossary, cast)["characters"]
    assert out["Dương Viêm"] == "Dương Viêm"
    assert out["Viêm nhi"] == "Dương Viêm"      # the alias follows its group
    assert out["Ma Tôn"] == "Demon Sovereign"   # untouched


# ── a stale save must not delete generated work ────────────────────────────


def test_a_save_missing_a_clip_keeps_the_one_already_generated():
    # The board page autosaves the whole document from its own memory, so a tab
    # open since before a batch ran writes a board with no clip on it. That is a
    # stale save, not a request to throw the clip away.
    stored = {"nodes": [{"id": "vid:clip-02", "data": {
        "kind": "video", "status": "done", "prompt": "镜头1：0秒-2秒",
        "clipUrl": "https://r2/clip.mp4", "refs": [{"url": "https://r2/a.png"}]}}]}
    incoming = {"nodes": [{"id": "vid:clip-02", "data": {
        "kind": "video", "status": "idle", "prompt": "", "refs": []}}]}
    out = automation_routes._keep_earned(incoming, stored)
    d = out["nodes"][0]["data"]
    assert d["clipUrl"] == "https://r2/clip.mp4"
    assert d["prompt"] == "镜头1：0秒-2秒"
    assert d["refs"] == [{"url": "https://r2/a.png"}]
    assert d["status"] == "done"          # and it stops claiming to be waiting


def test_a_save_carrying_a_new_clip_replaces_the_old_one():
    stored = {"nodes": [{"id": "v", "data": {"kind": "video", "clipUrl": "https://r2/old.mp4"}}]}
    incoming = {"nodes": [{"id": "v", "data": {"kind": "video", "clipUrl": "https://r2/new.mp4"}}]}
    out = automation_routes._keep_earned(incoming, stored)
    assert out["nodes"][0]["data"]["clipUrl"] == "https://r2/new.mp4"


def test_a_stale_save_keeps_a_generated_sheet_on_a_character_node():
    stored = {"nodes": [{"id": "char:a", "data": {
        "kind": "character", "identity": {"image": "https://r2/sheet.png", "status": "done"}}}]}
    incoming = {"nodes": [{"id": "char:a", "data": {
        "kind": "character", "identity": {"image": "", "status": "idle"}}}]}
    out = automation_routes._keep_earned(incoming, stored)
    assert out["nodes"][0]["data"]["identity"]["image"] == "https://r2/sheet.png"


# ── the zip a designer opens ────────────────────────────────────────────────


def test_a_plate_is_filed_under_its_subjects_own_name():
    taken: set[str] = set()
    assert automation.plate_filename("Sienna Rothwell", "character", taken) == \
        "nhan-vat/Sienna-Rothwell.png"
    assert automation.plate_filename("Cedarcrest House Party Hall", "environment", taken) == \
        "boi-canh/Cedarcrest-House-Party-Hall.png"


def test_vietnamese_names_keep_their_letters_rather_than_losing_them():
    # Folding the accents leaves a readable name; dropping the characters
    # outright would file "Dương Viêm" as "ng-im".
    taken: set[str] = set()
    assert automation.plate_filename("Dương Viêm", "character", taken) == "nhan-vat/Duong-Viem.png"
    assert automation.plate_filename("Đại Chưởng Giáo", "character", taken) == \
        "nhan-vat/Dai-Chuong-Giao.png"


def test_two_characters_sharing_a_name_do_not_overwrite_each_other():
    taken: set[str] = set()
    first = automation.plate_filename("Alex", "character", taken)
    second = automation.plate_filename("Alex", "character", taken)
    assert first == "nhan-vat/Alex.png"
    assert second == "nhan-vat/Alex-2.png"


def test_a_nameless_subject_still_gets_a_file():
    assert automation.plate_filename("", "environment", set()) == "boi-canh/untitled.png"


# ── the guard on a school-age cast ─────────────────────────────────────────


_SCHOOL = [{"key": "t", "name": "Theo", "role": "lead", "summary": "A junior at Hallwood High."}]


def test_a_shot_that_dissolves_a_students_clothes_is_refused():
    shots = [{"action": ["The camisole fabric breaks into drifting particles and vanishes."]},
             {"action": ["A dissolve strips away her jacket and shirt."]},
             {"action": ["The glow sweeps down, revealing a black lace bra beneath."]}]
    assert [n for n, _ in automation.unsafe_shots(shots, _SCHOOL, None)] == [1, 2, 3]


def test_the_words_that_only_look_like_it_are_left_alone():
    # Every one of these is on the real board and is nothing.
    shots = [{"framing_note": "ceiling strip lights pull back over her head"},
             {"framing_note": "students in navy blazers and striped ties"},
             {"action": ["Long nude-polish nails clear the button."]},
             {"action": ["He looks at the girl in the pink bra top."]},
             {"title": "HER CLOTHES? HER PANTIES?"}]
    assert automation.unsafe_shots(shots, _SCHOOL, None) == []


def test_the_guard_only_applies_to_a_school_age_cast():
    adult = [{"key": "a", "name": "Mara", "role": "detective", "summary": "A night-shift detective."}]
    shots = [{"action": ["She undresses and steps into the shower."]}]
    assert automation.unsafe_shots(shots, adult, {"name": "Apartment"}) == []
    assert automation.unsafe_shots(shots, _SCHOOL, None) == [(1, "undresses")]


# ── lines across cuts, names, and a cast redesigned after the shots ────────


def test_a_line_the_reference_says_across_a_cut_is_split_at_that_cut():
    shots = [
        _shot(1, 2.33, source_shot=63, heard="No, listen. In ten minutes,",
              dialogue=[{"who": "THEO", "line": "No, listen, in ten minutes, if you don't see the nurse, you will die."}]),
        _shot(2, 2.33, source_shot=64, continues_from=63, heard="if you don't see the nurse, you will die."),
    ]
    for sh in shots:
        sh["source_shots"] = [sh["source_shot"]]
    out = board._carry_lines(shots)
    assert out[0]["dialogue"] == [{"who": "THEO", "line": "No, listen, in ten minutes,"}]
    assert out[1]["dialogue"] == [{"who": "THEO", "line": "if you don't see the nurse, you will die.", "cont": True}]


def test_a_reworded_line_splits_at_the_same_share_on_punctuation():
    head, tail = board._split_at_heard(
        "No, listen, in ten minutes, unless the nurse sees you, you die.",
        "if you don't see the nurse, you will die.", "No, listen. In ten minutes,")
    assert (head, tail) == ("No, listen, in ten minutes,", "unless the nurse sees you, you die.")


def test_a_line_begun_in_the_clip_before_is_left_where_it_is():
    shots = [_shot(1, 2.0, source_shot=64, continues_from=63, heard="you will die.")]
    shots[0]["source_shots"] = [64]
    assert board._carry_lines(shots)[0]["dialogue"] == []


def test_source_names_become_cast_names_like_for_like():
    rename = board._renamer([
        {"name": "Theo Lambert", "source_name": "Michael"},
        {"name": "Sienna Rothwell", "source_name": "Ava Shaw"},
        {"name": "Noelle Pryce", "source_name": "Dr. Skylar"},
    ])
    assert rename("Michael sees the spider inside Ava; Dr. Skylar calls Ava Shaw.") == (
        "Theo sees the spider inside Sienna; Dr. Pryce calls Sienna Rothwell.")
    assert board._renamer([])("Michael") == "Michael"


def _redesigned():
    cast = {
        "characters": [{"key": "sienna", "name": "Sienna", "looks_like": "long dark wavy hair, blue satin dress",
                        "design": {"hair": "Platinum blunt bob. Straight.",
                                   "costume": [{"piece": "Cropped blazer", "colour": "Ivory"}]}},
                       {"key": "extra", "name": "Guard"}],
        "shots": {"1": {"character_keys": ["sienna"]}, "2": {"character_keys": ["extra"]}},
    }
    adaptation = {"shots": {
        "1": {"framing_note": "Sienna's dark wavy hair and blue satin shoulder fill the left third",
              "sfx": ["satin rustle", "crowd"], "dialogue": [{"who": "SIENNA", "line": "Wait."}]},
        "2": {"framing_note": "A guard in a grey suit"},
    }}
    return cast, adaptation


def test_conform_asks_only_about_redesigned_people_and_keeps_only_real_changes(monkeypatch):
    from flowboard.services.video_analyzer import adapt as adapt_mod, conform
    cast, adaptation = _redesigned()
    asked = []

    async def fake(system, user, stats, **kw):
        asked.append(user)
        return [{"shot": 1, "framing_note": "Sienna's shoulder fills the left third",   # fields beside "shot"
                 "sfx": ["fabric rustle", "crowd"], "dialogue": "hacked", "action": "not a list"},
                {"shot": 2, "changes": {"framing_note": "x"}}]         # not in the batch

    monkeypatch.setattr(adapt_mod, "ask_json", fake)
    import asyncio
    out = asyncio.run(conform.conform_shots(adaptation, cast, adapt_mod.TextStats()))
    assert len(asked) == 1 and '"shot": 2' not in asked[0]              # the guard has no new look
    assert "Wait." not in asked[0]                                      # lines are never sent
    assert out == {1: {"framing_note": "Sienna's shoulder fills the left third",
                       "sfx": ["fabric rustle", "crowd"]}}


def test_a_conform_can_be_undone_to_the_words_first_written():
    from flowboard.services.video_analyzer import conform
    _, adaptation = _redesigned()
    once = conform.apply(adaptation, {1: {"framing_note": "Sienna's shoulder fills the left third"}}, note={"at": "t1"})
    twice = conform.apply(once, {1: {"framing_note": "Her shoulder fills the left third"}}, note={"at": "t2"})
    assert twice["shots"]["1"]["framing_note"] == "Her shoulder fills the left third"
    assert adaptation["shots"]["1"]["framing_note"].startswith("Sienna's dark wavy hair")   # input untouched
    back = conform.undo(twice)
    assert back["shots"]["1"] == adaptation["shots"]["1"]
    assert "conform" not in back


def test_a_model_that_sends_nothing_back_is_replaced_by_the_fallback(monkeypatch):
    import asyncio
    from flowboard.services import avis_text
    from flowboard.services.video_analyzer import adapt as adapt_mod
    asked = []

    async def fake(model, messages, **kw):
        asked.append(model)
        if model == adapt_mod.TEXT_MODEL:
            if len(asked) == 2:
                raise avis_text.AvisTextError("HTTP 520")
            return avis_text.Completion(text="", model=model)
        return avis_text.Completion(text='{"ok": true}', model=model)

    async def no_wait(_s):
        return None

    monkeypatch.setattr(adapt_mod, "FALLBACK_MODEL", "fallback-model")
    monkeypatch.setattr(adapt_mod, "TEXT_MODEL", "primary-model")
    monkeypatch.setattr(avis_text, "complete", fake)
    monkeypatch.setattr(adapt_mod.asyncio, "sleep", no_wait)
    stats = adapt_mod.TextStats()
    assert asyncio.run(adapt_mod.ask_json("s", "u", stats)) == {"ok": True}
    assert asked == ["primary-model"] * 3 + ["fallback-model"]
    assert stats.answered_by == {"fallback-model": 1}


def test_a_model_that_answers_badly_is_asked_again_not_replaced(monkeypatch):
    import asyncio
    from flowboard.services import avis_text
    from flowboard.services.video_analyzer import adapt as adapt_mod
    asked = []

    async def fake(model, messages, **kw):
        asked.append(model)
        return avis_text.Completion(text="Sure! Here it is", model=model)

    async def no_wait(_s):
        return None

    monkeypatch.setattr(adapt_mod, "FALLBACK_MODEL", "fallback-model")
    monkeypatch.setattr(adapt_mod, "TEXT_MODEL", "primary-model")
    monkeypatch.setattr(avis_text, "complete", fake)
    monkeypatch.setattr(adapt_mod.asyncio, "sleep", no_wait)
    with pytest.raises(avis_text.AvisTextError):
        asyncio.run(adapt_mod.ask_json("s", "u", adapt_mod.TextStats()))
    assert asked == ["primary-model"] * 3


def test_the_guard_reads_a_school_cast_from_what_the_board_actually_sends():
    # The board sends name, look and design — no role, no summary. On X-Ray the
    # only "school" in that payload was in the costume, and the guard missed it.
    cast = [{"key": "sienna", "name": "Sienna", "look": "", "wardrobe": "",
             "design": {"costume": [{"piece": "Cropped blazer"}]}},
            {"key": "theo", "name": "Theo", "design": {"costume": [{"piece": "Charcoal navy school blazer"}]}}]
    shots = [{"action": ["The camisole fabric breaks into drifting particles."]}]
    assert automation.unsafe_shots(shots, cast, {"name": "Hallwood High Main Locker Corridor"}) == [
        (1, "camisole fabric breaks into")]
    adults = [{"key": "a", "name": "Ana", "design": {"costume": [{"piece": "Office blazer"}]}}]
    assert automation.unsafe_shots(shots, adults, {"name": "Downtown Loft"}) == []


def test_an_action_said_while_speaking_is_kept_and_a_line_about_speaking_goes():
    sequence = {"duration_s": 3, "editing": "cut"}
    shot = {"duration_s": 3, "framing": "MCU", "character_keys": [],
            "action": ["Theo lifts the green box a few inches as he speaks.",
                       "He locks his eyes on hers and speaks without blinking.",
                       "Theo speaks, holding her eyeline."],
            "dialogue": [{"who": "THEO", "line": "Promise me."}]}
    prompt = automation.build_video_prompt(sequence, [shot], characters=[], environment=None)
    assert "Theo lifts the green box a few inches as he speaks." in prompt
    assert "He locks his eyes on hers and speaks without blinking." in prompt
    assert "holding her eyeline" not in prompt


def test_conform_finds_a_cast_member_the_shot_map_missed_by_name(monkeypatch):
    from flowboard.services.video_analyzer import adapt as adapt_mod, conform
    cast, adaptation = _redesigned()
    adaptation["shots"]["3"] = {"framing_note": "Sienna's wavy hair edges the frame"}   # no key in the map
    asked = []

    async def fake(system, user, stats, **kw):
        asked.append(user)
        return []

    monkeypatch.setattr(adapt_mod, "ask_json", fake)
    import asyncio
    asyncio.run(conform.conform_shots(adaptation, cast, adapt_mod.TextStats()))
    assert '"shot": 3' in asked[0] and '"shot": 2' not in asked[0]


def test_a_shot_nobody_is_placed_in_is_asked_about_with_the_whole_cast(monkeypatch):
    from flowboard.services.video_analyzer import adapt as adapt_mod, conform
    cast, adaptation = _redesigned()
    cast["characters"].append({"key": "theo", "name": "Theo", "design": {"hair": "Crop."}})
    asked = []

    async def fake(system, user, stats, **kw):
        asked.append(user)
        return []

    monkeypatch.setattr(adapt_mod, "ask_json", fake)
    import asyncio, json
    asyncio.run(conform.conform_shots(adaptation, cast, adapt_mod.TextStats(), only={2}))
    names = [c["name"] for c in json.loads(asked[0])["cast"]]
    assert names == ["Sienna", "Theo"]


def test_an_old_costume_is_recognised_in_a_shot_that_names_nobody():
    from flowboard.services.video_analyzer import conform
    cast, adaptation = _redesigned()
    adaptation["shots"]["4"] = {"framing_note": "A blue satin hem crosses the bottom of frame"}
    adaptation["shots"]["5"] = {"framing_note": "A guard in a grey suit waits by the door"}
    assert "satin" in conform.old_look_words(conform.cast_sheet(cast))
    assert conform.suspect_shots(adaptation, cast) == {1, 4}


def test_an_error_inside_the_stream_is_read_not_taken_for_silence():
    from flowboard.services import avis_text
    text, final, error = avis_text._parse_sse('data: {"error":"temperature is not supported by this model"}\n')
    assert (text, error) == ("", "temperature is not supported by this model")
    text, final, error = avis_text._parse_sse('data: {"delta":"hi"}\ndata: {"delta":"","finishReason":"stop"}\n')
    assert (text, final.get("finishReason"), error) == ("hi", "stop", "")
    assert isinstance(avis_text._refusal("claude-opus-5", "Invalid or missing API key."), avis_text.AvisModelError)
    assert isinstance(avis_text._refusal("gpt-6", 'HTTP 400 [\'Pricing not configured for model "gpt-6"\']'),
                      avis_text.AvisModelError)
    assert not isinstance(avis_text._refusal("x", "upstream timeout"), avis_text.AvisModelError)


def test_a_refused_model_goes_straight_to_the_fallback_and_stays_skipped(monkeypatch):
    import asyncio
    from flowboard.services import avis_text
    from flowboard.services.video_analyzer import adapt as adapt_mod
    asked = []

    async def fake(model, messages, **kw):
        asked.append(model)
        if model == "primary-model":
            raise avis_text.AvisModelError("primary-model: Invalid or missing API key.")
        return avis_text.Completion(text='{"ok": true}', model=model)

    async def no_wait(_s):
        return None

    monkeypatch.setattr(adapt_mod, "FALLBACK_MODEL", "fallback-model")
    monkeypatch.setattr(adapt_mod, "TEXT_MODEL", "primary-model")
    monkeypatch.setattr(adapt_mod, "_REFUSED", {})
    monkeypatch.setattr(avis_text, "complete", fake)
    monkeypatch.setattr(adapt_mod.asyncio, "sleep", no_wait)
    assert asyncio.run(adapt_mod.ask_json("s", "u", adapt_mod.TextStats())) == {"ok": True}
    assert asked == ["primary-model", "fallback-model"]          # one refusal, no retries
    asked.clear()
    asyncio.run(adapt_mod.ask_json("s", "u", adapt_mod.TextStats()))
    assert asked == ["fallback-model"]                             # remembered for the process


def test_a_school_age_cast_puts_the_content_rule_in_the_shot_brief():
    from flowboard.services.video_analyzer import adapt as adapt_mod
    rules = adapt_mod.AdaptationRules()
    plain = adapt_mod._shot_system(rules, {}, [])
    school = adapt_mod._shot_system(rules, {}, [], minors=True)
    assert "SCHOOL-AGE" not in plain
    assert "SCHOOL-AGE" in school and "OBJECT" in school


def test_a_gap_fill_after_a_cast_rename_keeps_the_shots_already_adapted(monkeypatch):
    import asyncio
    from flowboard.services.video_analyzer import adapt as adapt_mod, pipeline
    shots = [{"shot": 1, "start": 0.0, "end": 1.0, "source": {}}, {"shot": 2, "start": 1.0, "end": 2.0, "source": {}}]
    analysis = {"shots": shots, "video": {"duration": 2.0, "fps": 24, "width": 1, "height": 1, "has_audio": True, "path": "x"}}
    rules = adapt_mod.AdaptationRules()
    cast = {"characters": [{"name": "Sienna Rothwell", "source_name": "Ava"}]}
    previous = {"rules": rules.as_dict(), "shots": {"1": {"title": "KEPT"}},
                "glossary": {"characters": {"Ava": "Sienna Rothwell — Queen of the Senior Class"}},
                "conform": {"before": {"1": {"title": "OLD"}}}}
    asked = []

    async def fake_adapt(shots, sequences, glossary, rules, stats, *, only=None, **kw):
        asked.append(set(only or ()))
        return {n: {"title": "NEW"} for n in only}

    monkeypatch.setattr(adapt_mod, "adapt_shots", fake_adapt)
    monkeypatch.setattr(pipeline, "ensure_dialogue", lambda a: None)
    out = asyncio.run(pipeline.adapt(analysis, rules, glossary=previous["glossary"], previous=previous, cast=cast))
    assert asked == [{2}]                                   # only the gap, not both shots
    assert out["shots"]["1"] == {"title": "KEPT"} and out["shots"]["2"] == {"title": "NEW"}
    assert out["conform"] == previous["conform"]            # the undo record survives
