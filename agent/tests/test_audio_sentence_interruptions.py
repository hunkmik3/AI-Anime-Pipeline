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
    assert [line.text for line in lines] == ['I was walking to work.']
    assert (lines[0].first_shot, lines[0].last_shot) == (1, 2)
    assert (lines[0].start, lines[0].end) == (.6, 1.6)
    dialogue.attach(shots, lines)
    assert shots[0]['dialogue_lines'] == ['I was walking to work.']
    assert shots[1]['dialogue_lines'] == [] and shots[1]['dialogue_continues'] == 1


def test_internal_dash_pause_is_not_a_segment_boundary():
    transcript = {'segments': [segment(.1, [' I', ' meant', '—', ' the', ' other', ' one.'])]}
    assert [line.text for line in dialogue.audio_sentences(transcript, SHOTS)] == [
        'I meant— the other one.']
