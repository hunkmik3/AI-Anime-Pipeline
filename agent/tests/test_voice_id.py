"""Who is speaking: voice groups from fingerprints, their confidence, and one person per voice."""
import json
import wave

import numpy as np
import pytest

from flowboard.services.video_analyzer import dialogue, voice_id, voice_id_worker


def voices(seed=7, dim=256):
    rng = np.random.default_rng(seed)
    centres = rng.normal(size=(3, dim))
    return rng, centres / np.linalg.norm(centres, axis=1, keepdims=True)


def fingerprint(rng, centre, noise):
    v = centre + rng.normal(size=centre.shape) * noise
    return list(v / np.linalg.norm(v))


def film(n_per_voice=6, short=2):
    """Three people; each has long clear lines and a few short ones."""
    rng, centres = voices()
    vectors, meta, truth = {}, {}, {}
    k = 0
    for person, centre in enumerate(centres):
        for i in range(n_per_voice + short):
            k += 1
            lid = f'L{k:04d}'
            long = i < n_per_voice
            vectors[lid] = fingerprint(rng, centre, 0.035 if long else 0.06)
            meta[lid] = {'speech_s': 2.0 if long else 0.6, 'words': 6 if long else 2, 'guided': False}
            truth[lid] = person
    return vectors, meta, truth


def test_long_lines_form_one_voice_per_person_and_are_high_confidence():
    vectors, meta, truth = film()
    evidence, groups = voice_id.assign(vectors, meta)
    assert sorted(g['anchors'] for g in groups.values()) == [6, 6, 6]
    voice_of_person = {}
    for lid, e in evidence.items():
        if meta[lid]['speech_s'] >= 1:
            assert e['voice_confidence'] == 'high'
        voice_of_person.setdefault(truth[lid], set()).add(e['voice'])
    # Every line of a person, short ones included, lands on that person's voice.
    assert all(len(v) == 1 for v in voice_of_person.values())
    assert len({next(iter(v)) for v in voice_of_person.values()}) == 3


def test_short_lines_are_hints_and_noise_is_not_placed():
    vectors, meta, _ = film()
    rng = np.random.default_rng(3)
    vectors['L9999'] = list((lambda v: v / np.linalg.norm(v))(rng.normal(size=256)))
    meta['L9999'] = {'speech_s': 0.5, 'words': 1, 'guided': False}
    evidence, _ = voice_id.assign(vectors, meta)
    assert {e['voice_confidence'] for lid, e in evidence.items() if meta[lid]['speech_s'] < 1 and lid != 'L9999'} == {'low'}
    assert evidence['L9999']['voice_confidence'] == 'none' and evidence['L9999']['voice'] is None


def test_lines_recovered_from_captions_never_anchor_a_voice():
    vectors, meta, _ = film()
    rng, centres = voices()
    # Two quiet recovered lines that happen to sound alike would otherwise make a voice.
    odd = rng.normal(size=256)
    for lid in ('L0901', 'L0902', 'L0903'):
        vectors[lid] = fingerprint(rng, odd / np.linalg.norm(odd), 0.02)
        meta[lid] = {'speech_s': 2.0, 'words': 6, 'guided': True}
    evidence, groups = voice_id.assign(vectors, meta)
    assert len(groups) == 3
    assert all(evidence[lid]['voice_confidence'] != 'high' for lid in ('L0901', 'L0902', 'L0903'))


def test_a_group_of_two_cannot_vouch_for_itself():
    rng, centres = voices()
    vectors = {'L1': fingerprint(rng, centres[0], .03), 'L2': fingerprint(rng, centres[0], .03)}
    meta = {k: {'speech_s': 2.0, 'words': 6, 'guided': False} for k in vectors}
    evidence, groups = voice_id.assign(vectors, meta)
    assert groups['V1']['anchors'] == 2 and {e['voice_confidence'] for e in evidence.values()} != {'high'}


# ── one person per voice ─────────────────────────────────────────────────────

def catalog(assignments, visible=('lily', 'damian')):
    """One shot per line; ``assignments`` is [(line id, voice, confidence, asset id)]."""
    shots, rows = [], {}
    for n, (lid, voice, confidence, aid) in enumerate(assignments, start=1):
        shots.append({'shot': n, 'dialogue_lines': ['x'], 'dialogue_pieces': [
            {'line_id': lid, 'text': 'x', 'cont': False, 'voice': voice, 'voice_confidence': confidence}]})
        rows[str(n)] = {'speakers': [{'asset_id': aid, 'label': aid.title(), 'delivery': 'on_camera'}],
                        'asset_presence': [{'asset_id': a, 'visibility': 'visible'} for a in visible]}
    return shots, rows


def test_a_high_confidence_line_given_to_the_wrong_person_follows_its_voice():
    shots, rows = catalog([('L1', 'V1', 'high', 'lily'), ('L2', 'V1', 'high', 'lily'), ('L3', 'V1', 'high', 'lily'),
                           ('L4', 'V1', 'high', 'damian'), ('L5', 'V1', 'low', 'damian')])
    receipts = voice_id.reconcile(rows, shots)
    assert [(r['line_id'], r['from']['asset_id'], r['to']['asset_id']) for r in receipts] == [('L4', 'damian', 'lily')]
    assert rows['4']['speakers'][0] == {'asset_id': 'lily', 'label': 'Lily', 'delivery': 'on_camera'}
    assert rows['5']['speakers'][0]['asset_id'] == 'damian'        # a hint is never enforced
    assert voice_id.disputes(rows, shots) == []


def test_the_new_speaker_is_offscreen_when_not_in_the_shot():
    shots, rows = catalog([('L1', 'V1', 'high', 'lily'), ('L2', 'V1', 'high', 'lily'), ('L3', 'V1', 'high', 'lily'),
                           ('L4', 'V1', 'high', 'damian')], visible=('damian',))
    voice_id.reconcile(rows, shots)
    assert rows['4']['speakers'][0]['delivery'] == 'offscreen'


def test_a_voice_split_between_people_is_left_alone_and_reported():
    shots, rows = catalog([('L1', 'V1', 'high', 'lily'), ('L2', 'V1', 'high', 'lily'),
                           ('L3', 'V1', 'high', 'damian'), ('L4', 'V1', 'high', 'damian')])
    assert voice_id.reconcile(rows, shots) == []
    assert voice_id.disputes(rows, shots) == [{'voice': 'V1', 'speakers': {'lily': 2, 'damian': 2}}]


def test_a_line_cut_at_a_shot_edge_moves_as_a_whole():
    shots, rows = catalog([('L1', 'V1', 'high', 'lily'), ('L2', 'V1', 'high', 'lily'), ('L3', 'V1', 'high', 'lily'),
                           ('L4', 'V1', 'high', 'damian')])
    shots.append({'shot': 5, 'dialogue_lines': ['y'], 'dialogue_pieces': [
        {'line_id': 'L4', 'text': 'y', 'cont': True, 'voice': 'V1', 'voice_confidence': 'high'}]})
    rows['5'] = {'speakers': [{'asset_id': 'damian', 'label': 'Damian', 'delivery': 'offscreen'}], 'asset_presence': []}
    receipts = voice_id.reconcile(rows, shots)
    assert {(r['shot'], r['to']['asset_id']) for r in receipts} == {(4, 'lily'), (5, 'lily')}
    assert rows['5']['speakers'][0]['delivery'] == 'offscreen'


def test_bindings_name_each_voices_majority_character():
    track = [{'id': f'L{i}', 'voice': 'V1', 'voice_confidence': 'high'} for i in range(1, 5)]
    track.append({'id': 'L9', 'voice': 'V2', 'voice_confidence': 'low'})
    speakers = {'L1': {'asset_id': 'lily', 'label': 'Lily'}, 'L2': {'asset_id': 'lily', 'label': 'Lily'},
                'L3': {'asset_id': 'damian', 'label': 'Damian'}, 'L9': {'asset_id': 'maria', 'label': 'Maria'}}
    assert voice_id.bindings(speakers, track) == {'V1': {'asset_id': 'lily', 'label': 'Lily', 'lines': 2, 'of': 3}}


# ── the pipeline step ────────────────────────────────────────────────────────

def write_wav(path, seconds=8.0, rate=16000):
    t = np.arange(int(seconds * rate)) / rate
    tone = (3000 * np.sin(2 * np.pi * 180 * t)).astype('<i2')
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(tone.tobytes())


def analysis_with_lines():
    words = lambda a, toks: [{'word': ' ' + t, 'start': a + i * .3, 'end': a + i * .3 + .25} for i, t in enumerate(toks)]
    segs = [{'start': 0.2, 'end': 1.4, 'words': words(.2, ['one', 'two', 'three', 'four'])},
            {'start': 3.0, 'end': 3.3, 'words': words(3.0, ['hi']), 'source': 'guided_caption'}]
    track = [{'id': 'L0001', 'text': 'one two three four', 'start': 0.2, 'end': 1.35, 'source': 'asr'},
             {'id': 'L0002', 'text': 'hi', 'start': 3.0, 'end': 3.25, 'source': 'asr'},
             {'id': 'L0003', 'text': 'unheard', 'start': 5.0, 'end': 6.0, 'source': 'caption'}]
    shots = [{'shot': 1, 'start': 0., 'end': 4., 'dialogue_pieces': [
                 {'line_id': 'L0001', 'text': 'one two three four', 'cont': False},
                 {'line_id': 'L0002', 'text': 'hi', 'cont': False}]},
             {'shot': 2, 'start': 4., 'end': 8., 'dialogue_pieces': [{'line_id': 'L0003', 'text': 'unheard', 'cont': False}]}]
    return {'shots': shots, 'dialogue_track': track, 'transcript': {'segments': segs}}


def test_jobs_cover_spoken_words_only_and_mark_caption_recoveries():
    a = analysis_with_lines()
    jobs, meta = voice_id.jobs_for(a['dialogue_track'], a['transcript'])
    assert [j['id'] for j in jobs] == ['L0001', 'L0002']           # a caption-only line has no audio of its own
    assert jobs[0]['spans'] == [[0.15, 1.4]] and meta['L0002']['guided'] is True


@pytest.mark.asyncio
async def test_identify_marks_every_line_and_piece(tmp_path, monkeypatch):
    write_wav(tmp_path / 'audio.wav')
    rng, centres = voices()

    async def fake(audio, work, jobs):
        return {j['id']: fingerprint(rng, centres[0], .03) for j in jobs}

    monkeypatch.setattr(voice_id, 'fingerprints', fake)
    out = await voice_id.identify(analysis_with_lines(), tmp_path, listening=False)
    assert out['voices']['status'] == 'measured' and out['voices']['measured'] == 2
    track = {l['id']: l for l in out['dialogue_track']}
    assert track['L0003']['voice'] is None and track['L0003']['voice_confidence'] == 'none'
    pieces = [p for s in out['shots'] for p in s['dialogue_pieces']]
    assert all('voice' in p and 'voice_confidence' in p for p in pieces)


@pytest.mark.asyncio
async def test_no_audio_is_reported_not_guessed(tmp_path):
    out = await voice_id.identify(analysis_with_lines(), tmp_path, listening=False)
    assert out['voices']['status'] == 'no_audio' and 'voice' not in out['dialogue_track'][0]


def test_listening_reel_holds_references_then_unsure_lines(tmp_path):
    write_wav(tmp_path / 'audio.wav', seconds=20)
    evidence = {f'L{i}': {'voice': 'V1', 'voice_confidence': 'high', 'voice_similarity': .8} for i in range(3)}
    evidence['L9'] = {'voice': None, 'voice_confidence': 'none', 'voice_similarity': .1}
    meta = {k: {'speech_s': 1.5} for k in evidence}
    spans = {f'L{i}': [[i * 2.0, i * 2.0 + 1.5]] for i in range(3)}
    spans['L9'] = [[10.0, 10.6]]
    reel, rate, table = voice_id.listening_reel(tmp_path / 'audio.wav', evidence, meta, {'V1': {'anchors': 3}}, spans)
    assert [(r['clip'], r['id']) for r in table] == [('REFERENCE V1', 'V1'), ('LINE', 'L9')]
    assert len(reel) / rate == pytest.approx(table[-1]['end'] + 1.0, abs=.01)
    voice_id._apply_listening(evidence, {'lines': [{'id': 'L9', 'voice': 'V1', 'confidence': 'high'}]}, table)
    assert evidence['L9'] == {'voice': 'V1', 'voice_confidence': 'low', 'voice_similarity': .1, 'voice_basis': 'listening'}


@pytest.mark.asyncio
async def test_listening_never_repeats_an_unfinished_paid_call(tmp_path, monkeypatch):
    monkeypatch.setenv('ATRIUM_CLIENT_ID', 'x')
    monkeypatch.setenv('ATRIUM_CLIENT_SECRET', 'y')
    from flowboard.services.flowstudio import r2
    monkeypatch.setattr(r2, 'is_configured', lambda: True)
    write_wav(tmp_path / 'audio.wav', seconds=20)
    a = analysis_with_lines()
    a['dialogue_track'][0].update(start=0.2, end=1.35)
    evidence = {'L0001': {'voice': 'V1', 'voice_confidence': 'high', 'voice_similarity': .8},
                'L0002': {'voice': None, 'voice_confidence': 'none', 'voice_similarity': .1}}
    meta = {'L0001': {'speech_s': 1.25}, 'L0002': {'speech_s': .35}}
    groups = {'V1': {'anchors': 3}}
    reel, rate, table = voice_id.listening_reel(tmp_path / 'audio.wav', evidence, meta, groups,
                                                {'L0001': [[0.15, 1.4]], 'L0002': [[2.95, 3.3]]})
    import hashlib
    sha = hashlib.sha256(reel.astype('<i2').tobytes()).hexdigest()
    sig = voice_id._digest({'pcm': sha, 'model': voice_id.LISTEN_MODEL, 'prompt': voice_id.LISTEN_PROMPT, 'table': table})[:24]
    folder = tmp_path / 'voice-id' / f'listening-{sig}'
    folder.mkdir(parents=True)
    (folder / 'receipt.json').write_text(json.dumps({'state': 'in_flight'}))
    out = await voice_id.listen(a, tmp_path, evidence, meta, groups, tmp_path / 'audio.wav')
    assert (out['status'], out['reels'], out['not_repeated']) == ('partial', 1, 1)
    assert evidence['L0002']['voice'] is None


# ── the fingerprint worker ───────────────────────────────────────────────────

def test_filterbank_is_mean_normalised_with_kaldi_framing():
    rate = 16000
    t = np.arange(rate) / rate
    x = (8000 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
    feats = voice_id_worker.fbank(x)
    assert feats.shape == (1 + (rate - 400) // 160, 80)
    assert np.allclose(feats.mean(axis=0), 0, atol=1e-4)
    raw = voice_id_worker.fbank(np.concatenate([x, np.zeros(rate, dtype=np.float32)]))
    assert raw.shape[0] == 1 + (2 * rate - 400) // 160
    assert voice_id_worker.fbank(np.zeros(100, dtype=np.float32)).shape == (0, 80)


# ── two voices run together in one line ──────────────────────────────────────

def two_voice_film(tmp_path):
    """Six clear lines each from A and B, then one long line: A's words, a short pause, B's."""
    words, track, t = [], [], 0.0
    def say(lid, tokens):
        nonlocal t
        ws = [{'word': ' ' + w, 'start': round(t + i * .4, 3), 'end': round(t + i * .4 + .35, 3)} for i, w in enumerate(tokens)]
        t = ws[-1]['end'] + 1.0
        words.extend(ws)
        track.append({'id': lid, 'text': ' '.join(tokens), 'start': ws[0]['start'], 'end': ws[-1]['end'], 'source': 'asr'})
        return ws
    for i in range(12):
        say(f'L{i + 1:04d}', ['line', 'number', 'one', 'two', 'three'])
    long_a = say('L0013', ['you', 'never', 'told', 'me', 'that'])
    long_b = [{'word': ' ' + w, 'start': round(long_a[-1]['end'] + .2 + i * .4, 3),
               'end': round(long_a[-1]['end'] + .2 + i * .4 + .35, 3)} for i, w in enumerate(['because', 'you', 'never', 'asked', 'me'])]
    words.extend(long_b)
    track[-1].update(text=track[-1]['text'] + ' because you never asked me', end=long_b[-1]['end'])
    write_wav(tmp_path / 'audio.wav', seconds=t + 10)
    return {'shots': [{'shot': 1, 'start': 0., 'end': t + 10}], 'dialogue_track': track,
            'transcript': {'segments': [{'start': words[0]['start'], 'end': words[-1]['end'], 'text': 'x', 'words': words}]}}


@pytest.mark.asyncio
@pytest.mark.parametrize('second_voice, expected', [(1, ['V1', 'V2']), (0, None)])
async def test_a_line_whose_halves_are_two_voices_is_reported_as_a_turn(tmp_path, monkeypatch, second_voice, expected):
    a = two_voice_film(tmp_path)
    rng, centres = voices()

    async def fake(audio, work, jobs):
        out = {}
        for j in jobs:
            lid, _, half = j['id'].partition('#')
            n = int(lid[1:])
            who = (n - 1) % 2 if n <= 12 else (0 if half != 'b' else second_voice)
            out[j['id']] = fingerprint(rng, centres[who], .03)
        return out

    monkeypatch.setattr(voice_id, 'fingerprints', fake)
    out = await voice_id.identify(a, tmp_path, listening=False, breaks=True)
    turns = out['voices']['breaks']
    if expected is None:
        assert turns == []
        return
    assert [(t['line_id'], t['after']['word']) for t in turns] == [('L0013', ' that')]
    assert sorted(turns[0]['voices']) == sorted(expected)
    marked = voice_id.mark_breaks(a['transcript'], turns)
    lines = dialogue.audio_sentences(marked, a['shots'])
    assert [l.text for l in lines[-2:]] == ['you never told me that', 'because you never asked me']
