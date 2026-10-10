from copy import deepcopy

import pytest

from flowboard.services.video_analyzer import dialogue


def segment(start, tokens, duration=.2):
    words = [{'word': word, 'start': start+i*duration, 'end': start+(i+1)*duration}
             for i, word in enumerate(tokens)]
    return {'start': start, 'end': words[-1]['end'], 'language': 'en',
            'text': ''.join(tokens).strip(), 'words': words}


SHOTS = [{'shot': n+1, 'start': float(n), 'end': float(n+1)} for n in range(6)]


@pytest.mark.parametrize('dash', ['-', '–', '—'])
def test_terminal_segment_interruption_keeps_both_turns_and_all_words_and_times(dash):
    transcript = {'segments': [segment(.1, [' The', ' sofa', ' would', ' be', dash]),
                               segment(1.1, [" I'm", ' not', ' asking.'])]}
    original = deepcopy(transcript)
    lines = dialogue.audio_sentences(transcript, SHOTS)
    assert [line.text for line in lines] == ['The sofa would be'+dash, "I'm not asking."]
    assert [(line.start, line.end) for line in lines] == [
        (seg['words'][0]['start'], seg['words'][-1]['end']) for seg in transcript['segments']]
    original_stream = ' '.join(''.join(w['word'] for seg in transcript['segments']
                                    for w in seg['words']).split())
    assert ' '.join(line.text for line in lines) == original_stream
    assert transcript == original


def test_intra_word_hyphen_does_not_split_a_segment_or_its_continuation():
    transcript = {'segments': [segment(.1, [' A', ' well', '-', 'known', ' person']),
                               segment(1.1, [' walked', ' in.'])]}
    lines = dialogue.audio_sentences(transcript, SHOTS)
    assert [line.text for line in lines] == ['A well-known person walked in.']
    assert lines[0].start == .1 and lines[0].end == pytest.approx(1.5)


def test_ordinary_decoder_fragments_crossing_visual_cuts_still_merge():
    transcript = {'segments': [segment(.6, [' I', ' was']),
                               segment(1., [' walking', ' to', ' work.'])]}
    shots = deepcopy(SHOTS)
    lines = dialogue.build_track(shots, transcript, policy='audio_verbatim')
    # Still ONE sentence in the track...
    assert [line.text for line in lines] == ['I was walking to work.']
    assert (lines[0].first_shot, lines[0].last_shot) == (1, 2)
    assert (lines[0].start, lines[0].end) == (.6, 1.6)
    dialogue.attach(shots, lines)
    # ...but each shot holds the words spoken inside it, the second piece continuing the first.
    assert shots[0]['dialogue_lines'] == ['I was']
    assert shots[1]['dialogue_lines'] == ['walking to work.'] and shots[1]['dialogue_continues'] == 1
    assert [p['cont'] for p in shots[0]['dialogue_pieces'] + shots[1]['dialogue_pieces']] == [False, True]
    assert {p['line_id'] for p in shots[0]['dialogue_pieces'] + shots[1]['dialogue_pieces']} == {lines[0].id}


def test_a_single_word_just_before_a_cut_joins_the_rest_of_its_line():
    # "As" said 0.1 s before the cut, the rest after it: one performable piece, not two.
    transcript = {'segments': [segment(.85, [' As', ' of', ' this', ' moment.'], duration=.1)]}
    shots = deepcopy(SHOTS)
    lines = dialogue.build_track(shots, transcript, policy='audio_verbatim')
    dialogue.attach(shots, lines)
    assert shots[0]['dialogue_lines'] == []
    assert shots[1]['dialogue_lines'] == ['As of this moment.']


def test_internal_dash_pause_is_not_a_segment_boundary():
    transcript = {'segments': [segment(.1, [' I', ' meant', '—', ' the', ' other', ' one.'])]}
    assert [line.text for line in dialogue.audio_sentences(transcript, SHOTS)] == [
        'I meant— the other one.']


def test_a_long_unpunctuated_run_breaks_at_its_pauses():
    # "I call it perfection no pain no hesitation" … 0.6 s … "that thing is still suffering"
    first = [{'word': ' ' + w, 'start': .2 + i * .45, 'end': .2 + i * .45 + .4}
             for i, w in enumerate('I call it perfection no pain no hesitation'.split())]
    later = first[-1]['end'] + .6
    second = [{'word': ' ' + w, 'start': later + i * .45, 'end': later + i * .45 + .4}
              for i, w in enumerate('that thing is still suffering'.split())]
    words = first + second
    transcript = {'segments': [{'start': words[0]['start'], 'end': words[-1]['end'], 'language': 'en',
                                'text': ''.join(w['word'] for w in words).strip(), 'words': words}]}
    shots = [{'shot': 1, 'start': 0., 'end': 10.}]
    lines = dialogue.audio_sentences(transcript, shots)
    assert [l.text for l in lines] == ['I call it perfection no pain no hesitation', 'that thing is still suffering']


def test_a_short_pause_inside_a_short_sentence_does_not_break_it():
    words = [{'word': ' I', 'start': .2, 'end': .4}, {'word': ' was', 'start': .45, 'end': .7},
             {'word': ' thinking', 'start': 1.3, 'end': 1.7}, {'word': ' about', 'start': 1.75, 'end': 2.0},
             {'word': ' you.', 'start': 2.05, 'end': 2.3}]
    transcript = {'segments': [{'start': .2, 'end': 2.3, 'language': 'en', 'text': 'I was thinking about you.', 'words': words}]}
    assert [l.text for l in dialogue.audio_sentences(transcript, [{'shot': 1, 'start': 0., 'end': 3.}])] == [
        'I was thinking about you.']


def test_a_recognised_dash_alone_is_never_a_line():
    transcript = {'segments': [segment(.1, [' The', ' sofa.']), segment(.6, [' -']),
                               segment(.8, [" I'm", ' not', ' asking.'])]}
    assert [line.text for line in dialogue.audio_sentences(transcript, SHOTS)] == ['The sofa.', "I'm not asking."]


def test_a_dash_falling_past_the_cut_stays_with_the_words_it_ends():
    words = [{'word': w, 'start': a, 'end': b} for w, a, b in
             [(' nine', 1.5, 1.7), (' months', 1.7, 1.95), ('-', 1.99, 2.03)]]
    line = dialogue.Line(text='nine months-', first_shot=2, last_shot=3, start=1.5, end=2.03, source='asr')
    line.words = words
    spans = dialogue.split_at_cuts(line, SHOTS)
    assert [(s['shot'], s['text'], s['cont']) for s in spans] == [(2, 'nine months-', False)]
