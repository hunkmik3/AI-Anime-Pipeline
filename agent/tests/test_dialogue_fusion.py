"""Same-language caption cues fused with speech recognition, and lines split at cuts."""
from copy import deepcopy

import pytest

from flowboard.services.video_analyzer import dialogue, dialogue_fusion, source_integrity, subtitle_cues


def words(start, tokens, step=.25, prob=.95):
    return [{'word': w, 'start': round(start + i * step, 3), 'end': round(start + (i + 1) * step - .02, 3),
             'probability': prob} for i, w in enumerate(tokens)]


def segment(start, tokens, **kw):
    ws = words(start, tokens, **kw)
    return {'start': ws[0]['start'], 'end': ws[-1]['end'], 'text': ''.join(w['word'] for w in ws).strip(),
            'words': ws, 'language': 'en'}


def shot(n, start, end, *captions):
    events = [{'text': text, 'first_frame': f'S{n}_F{a}', 'last_frame': f'S{n}_F{b}'} for text, a, b in captions]
    return {'shot': n, 'start': start, 'end': end, 'source': {'subtitle_events': events, 'action': 'x', 'shot_size': 'MS'}}


# ── cues ─────────────────────────────────────────────────────────────────────

def test_caption_frames_become_seconds_and_a_held_caption_is_one_cue():
    shots = [shot(1, 0., 2., ('Hello there.', 2, 3)), shot(2, 2., 3., ('Hello there.', 1, 3))]
    cues = subtitle_cues.build(shots)
    assert len(cues) == 1 and cues[0]['shots'] == [1, 2]
    # Samples of a 2 s shot sit at .1/.5/.9 → frame 2 at 1.0 s, widened halfway to frame 1 (0.6 s).
    assert cues[0]['start'] == pytest.approx(.6) and cues[0]['end'] == pytest.approx(3.)


def test_dash_led_turns_in_one_caption_are_two_cues():
    cues = subtitle_cues.build([shot(1, 0., 4., ('- Oh no. - Ha, okay!', 1, 5))])
    assert [c['text'] for c in cues] == ['Oh no.', 'Ha, okay!']
    assert cues[0]['end'] <= cues[1]['start'] + 1e-6


# ── classification and coverage ──────────────────────────────────────────────

LINES = ['Three seconds left.', 'Make your choice now.', 'I am leaving now.', 'Do not follow me.',
         'We are both adults here.', 'Then act like one.', 'Fine, have it your way.']
FOREIGN = ['Còn ba giây.', 'Chọn ngay đi.', 'Tôi đi đây.', 'Đừng theo tôi.',
           'Chúng ta đều lớn rồi.', 'Vậy cư xử như người lớn đi.', 'Được, tuỳ anh.']


def film(captions=LINES, skip=()):
    """One 3 s shot per caption; each English line spoken inside its shot unless skipped."""
    shots = [shot(i + 1, i * 3., i * 3. + 3., (c, 1, 3)) for i, c in enumerate(captions)]
    segs = [segment(i * 3. + .2, [' ' + w for w in line.split()]) for i, line in enumerate(LINES) if i not in skip]
    return shots, {'segments': segs}


def test_same_language_captions_are_verbatim_and_translated_ones_are_not():
    shots, transcript = film()
    cues = subtitle_cues.build(shots)
    assert dialogue_fusion.classify(cues, transcript)['mode'] == 'verbatim'
    shots, transcript = film(captions=FOREIGN)
    assert dialogue_fusion.classify(subtitle_cues.build(shots), transcript)['mode'] == 'translated'


def test_one_heard_utterance_covers_only_the_caption_on_screen_when_it_was_said():
    # Shots 129/130 of the sample: she asks, he repeats it; only one utterance was recognised.
    shots = [shot(1, 0., 2., ('Damian, what are you doing in here?', 1, 3)),
             shot(2, 2., 4., ('What are you doing in here?', 1, 3))]
    cues = subtitle_cues.build(shots)
    assert len(cues) == 2
    his = segment(2.2, [' What', ' are', ' you', ' doing', ' in', ' here?'])
    share = dialogue_fusion.cue_coverage(cues, his['words'])
    assert share[0] < .5 and share[1] == 1.0
    hers = segment(.3, [' What', ' are', ' you', ' doing', ' in', ' here?'])
    share = dialogue_fusion.cue_coverage(cues, hers['words'])
    assert share[0] == pytest.approx(6 / 7) and share[1] < .5


def test_coverage_holds_across_alignment_windows():
    shots, transcript = film()
    words = [w for seg in transcript['segments'] for w in seg['words']]
    cues = subtitle_cues.build(shots)
    assert dialogue_fusion.cue_coverage(cues, words, budget=5) == [1.0] * len(cues)


# ── recovery ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unheard_verbatim_caption_comes_back_as_decoded_speech(tmp_path, monkeypatch):
    shots, transcript = film(skip={2})          # line 3 was never transcribed
    (tmp_path / 'audio.wav').write_bytes(b'')
    calls = []

    async def windows(audio, out, jobs):
        calls.append(jobs)
        job = jobs[0]
        decoded = [segment(6.3, [' I', ' am', ' leaving', ' now.'])]
        return {'results': {job['id']: {'plain': decoded, 'prompted': []}}}

    monkeypatch.setattr(dialogue_fusion.asr, 'transcribe_windows', windows)
    analysis = {'shots': shots, 'transcript': transcript, 'video': {'duration': 21.}}
    out = await dialogue_fusion.recover(analysis, tmp_path)
    rec = out['transcript']['caption_recovery']
    assert rec['mode'] == 'verbatim' and len(calls) == 1 and len(calls[0]) == 1
    assert [a['heard'] for a in rec['accepted']] == ['I am leaving now.'] and not rec['fallbacks']
    assert any(s.get('source') == 'guided_caption' for s in out['transcript']['segments'])
    # Cached: a second run decodes nothing.
    calls.clear()
    again = await dialogue_fusion.recover(analysis, tmp_path)
    assert not calls and again['transcript']['caption_recovery']['accepted'] == rec['accepted']


@pytest.mark.asyncio
async def test_a_prompted_decode_that_only_echoes_the_caption_is_refused(tmp_path, monkeypatch):
    shots, transcript = film(skip={2})
    (tmp_path / 'audio.wav').write_bytes(b'')

    async def windows(audio, out, jobs):
        mumble = [segment(6.4, [' uh', ' leave'], prob=.5)]                 # someone speaks, unreadably
        echo = [segment(6.3, [' I', ' am', ' leaving', ' now.'], prob=.2)]   # low-confidence echo
        return {'results': {jobs[0]['id']: {'plain': mumble, 'prompted': echo}}}

    monkeypatch.setattr(dialogue_fusion.asr, 'transcribe_windows', windows)
    out = await dialogue_fusion.recover({'shots': shots, 'transcript': transcript, 'video': {'duration': 21.}}, tmp_path)
    rec = out['transcript']['caption_recovery']
    assert not rec['accepted'] and [f['text'] for f in rec['fallbacks']] == ['I am leaving now.']
    track = dialogue.build_track(shots, out['transcript'], policy='audio_verbatim')
    kept = [l for l in track if l.source == 'caption']
    assert [l.text for l in kept] == ['I am leaving now.'] and kept[0].flags == ['audio_unconfirmed']
    dialogue.attach(shots, track)
    assert shots[2]['dialogue_lines'] == ['I am leaving now.'] and shots[2]['dialogue_heard'] == ''


@pytest.mark.asyncio
async def test_the_caption_prompted_decode_wins_only_when_it_hears_more(tmp_path, monkeypatch):
    shots, transcript = film(skip={2})
    (tmp_path / 'audio.wav').write_bytes(b'')

    async def windows(audio, out, jobs):
        plain = [segment(6.55, [' am', ' leaving', ' now.'])]
        prompted = [segment(6.3, [' I', ' am', ' leaving', ' now.'], prob=.8)]
        return {'results': {jobs[0]['id']: {'plain': plain, 'prompted': prompted}}}

    monkeypatch.setattr(dialogue_fusion.asr, 'transcribe_windows', windows)
    out = await dialogue_fusion.recover({'shots': shots, 'transcript': transcript, 'video': {'duration': 21.}}, tmp_path)
    accepted = out['transcript']['caption_recovery']['accepted']
    assert [(a['heard'], a['prompted'], a['match']) for a in accepted] == [('I am leaving now.', True, 1.0)]


@pytest.mark.asyncio
async def test_a_name_heard_differently_stays_in_its_line_with_the_caption_spelling(tmp_path, monkeypatch):
    # Shot 129 of the sample: the decode heard "Damien!" where the caption reads "Damian,".
    shots, transcript = film(captions=LINES + ['Damian, what are you doing in here?'])
    (tmp_path / 'audio.wav').write_bytes(b'')

    async def windows(audio, out, jobs):
        plain = [segment(21.2, [' Damien!'], prob=.39),
                 segment(21.8, [' What', ' are', ' you', ' doing', ' in', ' here?'])]
        return {'results': {jobs[0]['id']: {'plain': plain, 'prompted': []}}}

    monkeypatch.setattr(dialogue_fusion.asr, 'transcribe_windows', windows)
    out = await dialogue_fusion.recover({'shots': shots, 'transcript': transcript, 'video': {'duration': 24.}}, tmp_path)
    assert [a['heard'] for a in out['transcript']['caption_recovery']['accepted']] == [
        'Damien! What are you doing in here?']
    # Unknown, the name keeps the spelling it was heard with...
    track = dialogue.build_track(shots, out['transcript'], policy='audio_verbatim')
    assert [(l.text, l.flags) for l in track if l.first_shot == 8] == [('Damien! What are you doing in here?', [])]
    # ...and takes the caption's once the film names him.
    names = dialogue_fusion.known_names({'characters': [{'source_name': 'Damian', 'aliases': ['Mr. Cross']}]})
    assert names == {'damian', 'mr', 'cross'}
    track = dialogue.build_track(shots, out['transcript'], policy='audio_verbatim', names=names)
    assert [(l.text, l.flags) for l in track if l.first_shot == 8] == [
        ('Damian! What are you doing in here?', ['caption_corrected'])]


@pytest.mark.asyncio
async def test_translated_captions_never_become_dialogue(tmp_path, monkeypatch):
    shots, transcript = film(captions=FOREIGN, skip={2})
    (tmp_path / 'audio.wav').write_bytes(b'')
    asked = []

    async def windows(audio, out, jobs):
        asked.extend(jobs)
        return {'results': {j['id']: {'plain': [], 'prompted': []} for j in jobs}}

    monkeypatch.setattr(dialogue_fusion.asr, 'transcribe_windows', windows)
    out = await dialogue_fusion.recover({'shots': shots, 'transcript': transcript}, tmp_path)
    rec = out['transcript']['caption_recovery']
    # Only the caption with nothing heard under it is decoded again — unprompted, in English.
    assert rec['mode'] == 'translated' and [(j['prompt'], j['language']) for j in asked] == [('', 'en')]
    assert [(u['text'], u['reason']) for u in rec['unplaced']] == [('Tôi đi đây.', 'no_speech_heard')]
    track = dialogue.build_track(shots, out['transcript'], policy='audio_verbatim')
    assert all(l.source == 'asr' for l in track) and 'Tôi đi đây.' not in [l.text for l in track]


@pytest.mark.asyncio
async def test_speech_found_under_a_translated_caption_is_kept_in_its_own_language(tmp_path, monkeypatch):
    shots, transcript = film(captions=FOREIGN, skip={2})
    (tmp_path / 'audio.wav').write_bytes(b'')
    heard = {**segment(6.3, [' Jace!', ' Over', ' here!'], prob=.85), 'no_speech_prob': .1, 'avg_logprob': -.3}
    music = {**segment(6.3, [' Thank', ' you.'], prob=.9), 'no_speech_prob': .9, 'avg_logprob': -.2}

    async def windows(audio, out, jobs):
        return {'results': {j['id']: {'plain': [music, heard], 'prompted': []} for j in jobs}}

    monkeypatch.setattr(dialogue_fusion.asr, 'transcribe_windows', windows)
    out = await dialogue_fusion.recover({'shots': shots, 'transcript': transcript}, tmp_path)
    rec = out['transcript']['caption_recovery']
    assert [a['heard'] for a in rec['accepted']] == ['Jace! Over here!']
    track = dialogue.build_track(shots, out['transcript'], policy='audio_verbatim')
    assert 'Jace! Over here!' in [l.text for l in track]


# ── corrections ──────────────────────────────────────────────────────────────

def test_misheard_word_takes_the_caption_spelling_but_contractions_stay_spoken():
    cues = subtitle_cues.build([shot(1, 0., 3., ('You are such a sweet girl.', 1, 3))])
    heard = words(.2, [" You're", ' such', ' a', ' tweet', ' girl.'])
    fixed, receipts = dialogue_fusion.correct_words(heard, cues)
    assert ''.join(w['word'] for w in fixed).strip() == "You're such a sweet girl."
    assert receipts == [{'heard': 'tweet', 'caption': 'sweet', 'start': heard[3]['start']}]
    assert [w['start'] for w in fixed] == [w['start'] for w in heard]


def test_a_caption_typo_or_slip_never_replaces_what_was_said():
    # Sample shots 31 and 149: the captions read "monthes" and "the old fashion way".
    cues = subtitle_cues.build([shot(1, 0., 3., ('For nine monthes and the old fashion way.', 1, 3))])
    heard = words(.2, [' For', ' nine', ' months', ' and', ' the', ' old', '-fashioned', ' way.'])
    film_words = {'for', 'nine', 'months', 'and', 'the', 'old', 'fashioned', 'way', 'sweet'}
    assert dialogue_fusion.correct_words(heard, cues, heard=film_words, names=set()) == (heard, [])
    # A word the film's speech does use elsewhere is taken.
    cues = subtitle_cues.build([shot(1, 0., 3., ('You are such a sweet girl.', 1, 3))])
    heard = words(.2, [" You're", ' such', ' a', ' tweet', ' girl.'])
    fixed, receipts = dialogue_fusion.correct_words(heard, cues, heard=film_words, names=set())
    assert [r['caption'] for r in receipts] == ['sweet']


def test_a_different_word_or_a_paraphrase_is_never_rewritten():
    cues = subtitle_cues.build([shot(1, 0., 3., ('You are such a sweet girl.', 1, 3))])
    for spoken in ([" You're", ' such', ' a', ' kind', ' girl.'],            # not a mishearing
                   [' Such', ' a', ' sweet', ' kid.'],                       # too little agrees
                   [" You're", ' a', ' good', ' kid,', ' really.']):         # a paraphrase
        heard = words(.2, spoken)
        assert dialogue_fusion.correct_words(heard, cues) == (heard, [])


# ── integrity ────────────────────────────────────────────────────────────────

def test_resolved_caption_checks_are_notes_only_with_verbatim_captions():
    shots, transcript = film()
    transcript['segments'][1]['words'][1]['word'] = ' yer'      # a disagreement with its caption
    transcript['segments'][1]['text'] = 'Make yer choice.'
    analysis = {'shots': shots, 'transcript': transcript, 'video': {'duration': 21.}}
    blocking = source_integrity.report(analysis)['findings']
    assert any(f['code'] == 'caption_audio_disagreement' for f in blocking)
    analysis['transcript']['caption_recovery'] = {'mode': 'verbatim'}
    report = source_integrity.report(analysis)
    assert not any(f['code'] == 'caption_audio_disagreement' for f in report['findings'])
    assert any(f['code'] == 'caption_audio_disagreement' for f in report['notes'])


def test_a_zero_length_repeat_at_a_decoder_edge_is_one_word():
    first = {'start': 0., 'end': 1.0, 'text': 'In here?', 'words': [
        {'word': ' In', 'start': .6, 'end': .8}, {'word': ' here?', 'start': 1.0, 'end': 1.0}]}
    second = {'start': 1.1, 'end': 1.3, 'text': 'here?', 'words': [{'word': ' here?', 'start': 1.1, 'end': 1.3}]}
    out = source_integrity.align_audio({'segments': [first, second]}, [])
    kept = [w for s in out['segments'] for w in s['words']]
    assert [w['word'] for w in kept] == [' In', ' here?'] and kept[-1]['end'] == 1.3


# ── caption turns as sentence boundaries ─────────────────────────────────────

def test_two_turns_heard_as_one_sentence_are_split_where_the_captions_change():
    shots = [shot(1, 0., 2., ('We should leave right now', 1, 3)), shot(2, 2., 4., ('No we are staying here tonight', 1, 3))]
    heard = segment(.2, [' we', ' should', ' leave', ' right', ' now', ' no', ' we', ' are', ' staying', ' here', ' tonight'],
                    step=.3)
    track = dialogue.build_track(shots, {'segments': [heard]}, policy='audio_verbatim')
    assert [l.text for l in track] == ['we should leave right now', 'no we are staying here tonight']
    assert all('split_at_caption_turn' in l.flags for l in track)
    assert (track[1].start, track[1].first_shot) == (heard['words'][5]['start'], 2)


def test_translated_captions_and_one_held_caption_never_split_a_sentence():
    heard = segment(.2, [' we', ' should', ' leave', ' right', ' now', ' and', ' never', ' come', ' back'], step=.3)
    foreign = [shot(1, 0., 2., ('Chúng ta nên đi ngay', 1, 3)), shot(2, 2., 4., ('và đừng bao giờ quay lại', 1, 3))]
    assert len(dialogue.build_track(foreign, {'segments': [deepcopy(heard)]}, policy='audio_verbatim')) == 1
    held = [shot(1, 0., 2., ('We should leave right now and never come back', 1, 3)),
            shot(2, 2., 4., ('We should leave right now and never come back', 1, 3))]
    assert len(dialogue.build_track(held, {'segments': [deepcopy(heard)]}, policy='audio_verbatim')) == 1


def test_older_readings_joined_with_semicolons_are_separate_captions():
    shots = [{'shot': 1, 'start': 0., 'end': 1., 'source': {'subtitle': 'a moving; car and'}},
             {'shot': 2, 'start': 1., 'end': 2., 'source': {'subtitle': 'car and; felt along'}}]
    assert [(c['text'], c['shots']) for c in subtitle_cues.build(shots)] == [
        ('a moving', [1]), ('car and', [1, 2]), ('felt along', [2])]
    assert source_integrity.caption_pieces(shots[1]) == ['car and', 'felt along']


# ── what is never inserted ───────────────────────────────────────────────────

async def recover_with(tmp_path, monkeypatch, captions, plain, skip={2}, prompted=()):
    shots, transcript = film(captions=captions, skip=skip)
    (tmp_path / 'audio.wav').write_bytes(b'')

    async def windows(audio, out, jobs):
        return {'results': {j['id']: {'plain': list(plain), 'prompted': list(prompted)} for j in jobs}}

    monkeypatch.setattr(dialogue_fusion.asr, 'transcribe_windows', windows)
    out = await dialogue_fusion.recover({'shots': shots, 'transcript': transcript, 'video': {'duration': 30.}}, tmp_path)
    return out['transcript']['caption_recovery']


@pytest.mark.asyncio
async def test_a_caption_with_no_voice_under_it_is_reported_not_spoken(tmp_path, monkeypatch):
    rec = await recover_with(tmp_path, monkeypatch, LINES[:2] + ['Free for a limited time'] + LINES[3:], plain=[])
    assert not rec['fallbacks'] and [(u['text'], u['reason']) for u in rec['unplaced']] == [
        ('Free for a limited time', 'no_speech_heard')]


@pytest.mark.asyncio
async def test_a_misread_repeat_of_a_line_already_said_is_not_said_twice(tmp_path, monkeypatch):
    mumble = [segment(6.4, [' uh', ' huh'], prob=.6)]
    rec = await recover_with(tmp_path, monkeypatch, LINES[:2] + [LINES[0]] + LINES[3:], plain=mumble)
    assert not rec['fallbacks'] and [u['reason'] for u in rec['unplaced']] == ['repeated_caption']


@pytest.mark.asyncio
async def test_a_caption_over_another_language_is_a_translation_not_a_missing_line(tmp_path, monkeypatch):
    spanish = [{**segment(6.3, [' Me', ' voy', ' ahora.'], prob=.9), 'language': 'es'}]
    rec = await recover_with(tmp_path, monkeypatch, LINES, plain=spanish)
    assert not rec['fallbacks'] and [u['reason'] for u in rec['unplaced']] == ['other_language']


def test_a_few_misread_captions_do_not_turn_verbatim_captions_into_mixed():
    noisy = LINES + ['Free for a limited time', 'and there is only one person', 'Stop give it back now']
    shots, transcript = film(captions=noisy)
    cls = dialogue_fusion.classify(subtitle_cues.build(shots), transcript)
    assert cls['match'] < .8 and cls['cue_median'] == 1.0 and cls['mode'] == 'verbatim'


@pytest.mark.asyncio
async def test_in_a_two_language_film_a_caption_over_the_other_language_is_still_a_translation(tmp_path, monkeypatch):
    shots, transcript = film(skip={2})
    transcript['segments'].append({**segment(25.0, [' Hola', ' amigo', ' mío.']), 'language': 'es'})
    (tmp_path / 'audio.wav').write_bytes(b'')

    async def windows(audio, out, jobs):
        spanish = [{**segment(6.3, [' Me', ' voy', ' ahora.'], prob=.9), 'language': 'es'}]
        return {'results': {j['id']: {'plain': spanish, 'prompted': []} for j in jobs}}

    monkeypatch.setattr(dialogue_fusion.asr, 'transcribe_windows', windows)
    out = await dialogue_fusion.recover({'shots': shots, 'transcript': transcript, 'video': {'duration': 30.}}, tmp_path)
    rec = out['transcript']['caption_recovery']
    assert not rec['fallbacks'] and [u['reason'] for u in rec['unplaced']] == ['other_language']


@pytest.mark.asyncio
async def test_speech_heard_otherwise_under_a_caption_is_a_disagreement_not_a_missing_line(tmp_path, monkeypatch):
    shots, transcript = film(skip={2})
    # The recognition heard other words while "I am leaving now." was on screen.
    transcript['segments'].append(segment(6.3, [' I', ' won', 't', ' be', ' finding']))
    transcript['segments'].sort(key=lambda s: s['start'])
    (tmp_path / 'audio.wav').write_bytes(b'')

    async def windows(audio, out, jobs):
        return {'results': {j['id']: {'plain': [segment(6.3, [' I', ' won', 't'], prob=.8)], 'prompted': []} for j in jobs}}

    monkeypatch.setattr(dialogue_fusion.asr, 'transcribe_windows', windows)
    out = await dialogue_fusion.recover({'shots': shots, 'transcript': transcript, 'video': {'duration': 30.}}, tmp_path)
    rec = out['transcript']['caption_recovery']
    assert not rec['fallbacks'] and [u['reason'] for u in rec['unplaced']] == ['transcribed_differently']


def test_a_rolling_two_line_caption_is_its_two_lines():
    rows = [("I can't hide it anymore.",), ("I can't hide it anymore.\nYou're the one for me.",), ("You're the one for me.",)]
    shots = [{'shot': n, 'start': n * 1.5, 'end': n * 1.5 + 1.5, 'source': {'subtitle': text}} for n, (text,) in enumerate(rows, 1)]
    assert [(c['text'], c['shots']) for c in subtitle_cues.build(shots)] == [
        ("I can't hide it anymore.", [1, 2]), ("You're the one for me.", [2, 3])]
    shots = [{'shot': 1, 'start': 0., 'end': 2., 'source': {'subtitle': 'Ethan Sinclair,\nmy best friend since forever.'}},
             {'shot': 2, 'start': 2., 'end': 4., 'source': {'subtitle': 'my best friend since forever.'}}]
    cues = subtitle_cues.build(shots)
    assert [(c['text'], c['shots'], c['start'], c['end']) for c in cues] == [
        ('Ethan Sinclair,', [1], 0., 2.), ('my best friend since forever.', [1, 2], 0., 4.)]


def test_a_word_the_decoder_gave_no_duration_gets_some_of_the_silence_around_it():
    lone = {'start': 308.7, 'end': 308.7, 'text': 'Michael.', 'words': [{'word': ' Michael.', 'start': 308.7, 'end': 308.7}]}
    before = {'start': 306.5, 'end': 307.8, 'text': 'world,', 'words': [{'word': ' world,', 'start': 307.5, 'end': 307.8}]}
    out = source_integrity.align_audio({'segments': [before, lone]}, [])
    w = out['segments'][1]['words'][0]
    assert (w['start'], w['end']) == (308.7, 308.9) and out['segments'][1]['end'] == 308.9
    # No room after it (the next word starts at once): it takes the silence before instead.
    squeezed = {'start': 47.0, 'end': 48.2, 'text': 'Gullwing, and I', 'words': [
        {'word': ' Gullwing,', 'start': 46.6, 'end': 47.08}, {'word': ' and', 'start': 47.88, 'end': 47.88},
        {'word': ' I', 'start': 47.88, 'end': 48.16}]}
    out = source_integrity.align_audio({'segments': [squeezed]}, [])
    assert [(x['start'], x['end']) for x in out['segments'][0]['words']][1] == (47.68, 47.88)
    assert len(out['integrity_alignment']['timed_zero_length_words']) == 1


def test_a_caption_boundary_never_leaves_a_one_word_line():
    words = [{'word': ' I', 'start': 45.76, 'end': 45.94}, {'word': ' drive', 'start': 45.94, 'end': 46.12},
             {'word': ' a', 'start': 46.12, 'end': 46.24}, {'word': ' Mercedes', 'start': 46.24, 'end': 46.6},
             {'word': ' Gullwing,', 'start': 46.6, 'end': 47.08, 'source_sentence_end': True},
             {'word': ' and', 'start': 47.68, 'end': 47.88, 'source_sentence_end': True},
             {'word': ' I', 'start': 47.88, 'end': 48.16}, {'word': ' also', 'start': 48.16, 'end': 48.44},
             {'word': ' have', 'start': 48.44, 'end': 48.6, 'source_sentence_end': True}]
    shots = [{'shot': 1, 'start': 45., 'end': 49.}]
    lines = dialogue.audio_sentences({'segments': [{'start': 45.76, 'end': 48.6, 'text': 'x', 'words': words}]}, shots)
    assert [l.text for l in lines] == ['I drive a Mercedes Gullwing,', 'and I also have']


def test_an_unheard_caption_is_reported_not_blocking_once_recovery_has_looked_for_it():
    shots, transcript = film(captions=FOREIGN, skip={2, 3})
    analysis = {'shots': shots, 'transcript': transcript, 'video': {'duration': 21.}}
    assert any(f['code'] == 'caption_without_audio' for f in source_integrity.report(analysis)['findings'])
    analysis['transcript']['caption_recovery'] = {'mode': 'translated'}
    report = source_integrity.report(analysis)
    assert not any(f['code'] == 'caption_without_audio' for f in report['findings'])
    assert any(f['code'] == 'caption_without_audio' for f in report['notes'])


def test_a_caption_whose_last_words_open_the_next_is_split_where_they_overlap():
    shots = [{'shot': 1, 'start': 0., 'end': 2., 'source': {'subtitle': "It's been almost ten minutes. I can't believe I let him freak me out"}},
             {'shot': 2, 'start': 2., 'end': 5., 'source': {'subtitle': "I can't believe I let him freak me out enough to call you here."}}]
    assert [(c['text'], c['shots'], c['start']) for c in subtitle_cues.build(shots)] == [
        ("It's been almost ten minutes.", [1], 0.),
        ("I can't believe I let him freak me out enough to call you here.", [1, 2], 0.)]
