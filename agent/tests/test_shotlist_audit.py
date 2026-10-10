"""Deterministic shotlist measurements: what a person checking it against the video would count."""
from flowboard.services.video_analyzer import shotlist_audit

LINES = ['Three seconds left.', 'Make your choice now.', 'I am leaving now.', 'Do not follow me.',
         'We are both adults here.', 'Then act like one.', 'Fine, have it your way.']


def shot(n, caption, lines=(), start=None, dur=3.):
    start = (n - 1) * dur if start is None else start
    return {'shot': n, 'start': start, 'end': start + dur, 'dialogue_lines': list(lines),
            'source': {'subtitle_events': [{'text': caption}] if caption else []}}


def transcript(lines):
    segs = []
    for i, line in enumerate(lines):
        if line:
            segs.append({'start': i * 3., 'end': i * 3. + 2., 'text': line})
    return {'segments': segs}


def test_every_caption_line_said_in_its_shot_is_complete():
    shots = [shot(i + 1, c, [c]) for i, c in enumerate(LINES)]
    d = shotlist_audit.dialogue_metrics({'shots': shots, 'transcript': transcript(LINES)})
    assert d['captions_verbatim'] and d['caption_coverage'] == 1.0
    assert d['missing_caption_lines'] == [] and d['speech_shots_without_lines'] == []


def test_a_caption_line_nobody_says_is_missing_and_its_shot_silent():
    shots = [shot(i + 1, c, [] if i == 2 else [c]) for i, c in enumerate(LINES)]
    d = shotlist_audit.dialogue_metrics({'shots': shots, 'transcript': transcript(LINES)})
    assert [m['text'] for m in d['missing_caption_lines']] == ['I am leaving now.']
    assert d['speech_shots_without_lines'] == [3]


def test_a_line_spoken_over_two_shots_continues_rather_than_going_missing():
    shots = [shot(i + 1, c, [c]) for i, c in enumerate(LINES)]
    shots[3]['dialogue_lines'] = ['Do not']
    shots.insert(4, {**shot(5, 'follow me.', ['follow me.']), 'dialogue_continues': 4})
    for i, s in enumerate(shots):
        s['shot'] = i + 1
    d = shotlist_audit.dialogue_metrics({'shots': shots, 'transcript': transcript(LINES)})
    assert d['speech_shots_without_lines'] == []


def test_one_line_holding_two_speakers_turns_is_reported_but_a_repeated_question_is_not():
    shots = [shot(i + 1, c, [c]) for i, c in enumerate(LINES)]
    track = [{'text': 'Do not follow me. We are both adults here.', 'first_shot': 4, 'last_shot': 5}]
    d = shotlist_audit.dialogue_metrics({'shots': shots, 'transcript': transcript(LINES), 'dialogue_track': track})
    assert [m['caption_shots'] for m in d['merged_turn_lines']] == [[4, 5]]
    # She asks, he repeats the question: one line holding her words is one turn.
    shots[3]['source']['subtitle_events'] = [{'text': 'Damian, what are you doing in here?'}]
    shots[4]['source']['subtitle_events'] = [{'text': 'What are you doing in here?'}]
    track = [{'text': 'Damian! What are you doing in here?', 'first_shot': 4, 'last_shot': 4}]
    d = shotlist_audit.dialogue_metrics({'shots': shots, 'transcript': transcript(LINES), 'dialogue_track': track})
    assert d['merged_turn_lines'] == []


def test_pieces_longer_than_their_shot_are_overlong_with_real_timing():
    shots = [shot(1, '', ['Three seconds left.'], dur=1.), shot(2, '', ['Fine.'], start=1., dur=1.)]
    shots[0]['dialogue_pieces'] = [{'text': 'Three seconds left.', 'start': 0., 'end': 1.6}]
    shots[1]['dialogue_pieces'] = [{'text': 'Fine.', 'start': 1.1, 'end': 1.5}]
    d = shotlist_audit.dialogue_metrics({'shots': shots, 'transcript': {}})
    assert d['overlong_shots'] == [{'shot': 1, 'speech_s': 1.6, 'shot_s': 1.0}]


def test_speakers_are_counted_by_identity_not_by_label():
    adaptation = {'shots': {'1': {'dialogue': [{'who': 'Lily', 'speaker_id': 'a1'},
                                               {'who': 'the maid', 'speaker_id': 'a1'}]},
                            '2': {'dialogue': [{'who': 'Damian'}]}}}
    sp = shotlist_audit.speaker_metrics(adaptation, {'characters': [{'source_asset_id': 'a1'}]})
    assert sp['lines'] == 3 and sp['lines_with_speaker_id'] == 2
    assert sp['labels_per_speaker'] == {'a1': ['Lily', 'the maid']} and sp['unknown_speaker_ids'] == []
