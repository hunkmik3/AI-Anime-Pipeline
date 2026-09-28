"""Speaker qualifiers must not create false errors or hide attribution errors."""
import pytest

from flowboard.services.prompt_writer import check_clip_prompt


def _check(expected, body):
    line = "You have the wrong person."
    prompt = ("DURATION: 4 seconds.\n[SPECIFIC TIMELINE]\n"
              "[SHOT 1 — 00:00–00:04]\n" + body + "\n[OVERALL SUPPLEMENT]\n")
    return check_clip_prompt(prompt, refs=[], duration=4, slots=[(0, 4)], lines=[(1, line)],
                             speakers=[(1, expected, line)], exempt_shots=set(), school_age=False)


@pytest.mark.parametrize("expected,label", [
    ("AVA, OFF SCREEN, CONTINUING", "AVA, OFF SCREEN, CONTINUING"),
    ("AVA, OFF SCREEN, CONTINUING", "AVA"),
    ("AVA", "AVA, OFF SCREEN, CONTINUING"),
    ("AVA SHAW (OFF SCREEN, CONTINUING)", "AVA"),
    ("Ava Shaw, off screen, continuing", "AVA SHAW (CONTINUING)"),
    ("AVA — OFF-SCREEN", "AVA (OFF SCREEN)"),
    ("AVA", "AVA SHAW"),
])
def test_delivery_qualifiers_do_not_change_speaker_identity(expected, label):
    assert _check(expected, f'{label}:\n"You have the wrong person."') == []


@pytest.mark.parametrize("expected,body", [
    ("AVA, OFF SCREEN, CONTINUING", 'THEO:\n"You have the wrong person."'),
    ("AVA SHAW, CONTINUING", 'AVA STONE:\n"You have the wrong person."'),
    ("AVA, OFF SCREEN", 'AVA:\n"Hello."\nTHEO:\n"You have the wrong person."'),
    ("AVA, OFF SCREEN", 'Ava watches Theo.\nTHEO:\n"You have the wrong person."'),
])
def test_qualifiers_do_not_let_another_character_take_the_line(expected, body):
    issues = _check(expected, body)
    assert any("correct speaker" in issue for issue in issues)
