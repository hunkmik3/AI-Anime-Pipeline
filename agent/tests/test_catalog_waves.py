"""Independent story segments catalogued side by side merge in film order, on evidence only."""
from copy import deepcopy

import numpy as np

from flowboard.services.video_analyzer import catalog_waves as waves


def vec(seed):
    v = np.random.default_rng(seed).standard_normal(128).astype(np.float32)
    return (v / np.linalg.norm(v)).tolist()


def near(base, seed, noise=0.03):
    v = np.array(base) + noise * np.random.default_rng(seed).standard_normal(128)
    return (v / np.linalg.norm(v)).tolist()


def row(scene, *people, env='room', states=None, speaker=None):
    return {'scene_id': scene, 'environment_key': env,
            'asset_presence': [{'asset_id': p, 'visibility': 'visible'} for p in people],
            'character_states': states or {p: 'day' for p in people},
            'speakers': [{'asset_id': speaker, 'label': speaker}] if speaker else []}


def person(aid, name='woman', states=(('day', 'blue dress'),)):
    return {'id': aid, 'kind': 'character', 'name': name, 'description': name,
            'states': [{'key': k, 'wardrobe': w} for k, w in states]}


ROOM = {'id': 'room', 'kind': 'environment', 'name': 'Office', 'description': 'An office.'}


def seed_catalog():
    return {'assets': [ROOM, person('anna', 'Anna')], 'scenes': [{'id': 'sc1', 'shot_ids': [1, 2]}],
            'shots': {'1': row('sc1', 'anna'), '2': row('sc1', 'anna')}}


def faces_for(rows_people):
    return {f'shot-{n}-frame-1': [{'embedding': v}] for n, v in rows_people.items()}


def segment(seed, n0, new_id='woman', name='woman', states=(('day', 'blue dress'),)):
    seg = deepcopy(seed)
    seg['assets'].append(person(new_id, name, states))
    seg['scenes'].append({'id': 'sc2', 'shot_ids': [n0, n0 + 1]})
    seg['shots'].update({str(n0): row('sc2', new_id, speaker=new_id), str(n0 + 1): row('sc2', new_id)})
    return seg


def test_segments_split_at_sequence_starts_that_do_not_cut_an_utterance():
    shots = [{'shot': n, 'dialogue_pieces': [{'cont': n == 7}]} for n in range(1, 11)]
    sequences = [{'first_shot': 1}, {'first_shot': 4}, {'first_shot': 7}, {'first_shot': 9}]
    assert waves.segments(shots, sequences) == [(0, 3), (3, 8), (8, 10)]
    assert waves.waves([(0, 3), (3, 8), (8, 10), (10, 12), (12, 13)], 2) == [[(0, 3)], [(3, 8), (8, 10)], [(10, 12), (12, 13)]]


def test_the_same_new_id_with_another_face_is_renamed_and_the_same_face_is_merged():
    seed = seed_catalog()
    a, b = vec(1), vec(2)
    first, second = segment(seed, 10), segment(seed, 20)
    faces = faces_for({10: a, 11: near(a, 3), 20: b, 21: near(b, 4)})
    merged = deepcopy(seed)
    waves.absorb(merged, seed, first, 's10', faces, {}, {}, {}, [])
    receipt = waves.absorb(merged, seed, second, 's20', faces, {}, {}, {}, [])
    assert receipt['renamed'] == {'woman': 'woman-s20'} and not receipt['merged']
    assert merged['shots']['20']['asset_presence'][0]['asset_id'] == 'woman-s20'
    assert merged['shots']['20']['speakers'][0] == {'asset_id': 'woman-s20', 'label': 'woman'}   # free text kept
    assert waves.dangling(merged) == []
    # The same woman, met again in a parallel segment under another id: one person.
    third = segment(seed, 30, new_id='lady-in-blue', name='lady in blue')
    faces.update(faces_for({30: near(a, 5), 31: near(a, 6)}))
    receipt = waves.absorb(merged, seed, third, 's30', faces, {}, {}, {}, [])
    assert receipt['merged'] == {'lady-in-blue': 'woman'}
    assert merged['shots']['30']['asset_presence'][0]['asset_id'] == 'woman'
    assert not any(x['id'] == 'lady-in-blue' for x in merged['assets'])


def test_a_known_voice_merges_a_faceless_newcomer_and_a_new_outfit_key_clash_is_renamed():
    seed = seed_catalog()
    track = [{'id': f'L{i}', 'voice': 'V1', 'voice_confidence': 'high'} for i in range(5)]
    seed_speakers = {f'L{i}': {'asset_id': 'anna'} for i in range(3)}
    seg = segment(seed, 40, new_id='caller', name='caller', states=(('day', 'a red coat'),))
    seg_speakers = {**seed_speakers, 'L3': {'asset_id': 'caller'}, 'L4': {'asset_id': 'caller'}}
    merged, speakers = deepcopy(seed), dict(seed_speakers)
    receipt = waves.absorb(merged, seed, seg, 's40', {}, speakers, seg_speakers, seed_speakers, track)
    assert receipt['merged'] == {'caller': 'anna'} and speakers['L3'] == {'asset_id': 'anna'}
    anna = next(x for x in merged['assets'] if x['id'] == 'anna')
    assert {s['key'] for s in anna['states']} == {'day', 'day-s40'}           # a different outfit, own key
    assert merged['shots']['40']['character_states'] == {'anna': 'day-s40'}


def test_a_same_named_stranger_without_evidence_stays_separate_with_a_note():
    seed = seed_catalog()
    seg = segment(seed, 50, new_id='anna-2', name='Anna')
    merged = deepcopy(seed)
    receipt = waves.absorb(merged, seed, seg, 's50', {}, {}, {}, {}, [])
    assert not receipt['merged'] and receipt['notes'][0]['code'] == 'possible_duplicate_identity'
    assert any(x['id'] == 'anna-2' for x in merged['assets'])


def test_a_same_named_place_is_one_place_and_scenes_never_merge():
    seed = seed_catalog()
    seg = deepcopy(seed)
    seg['assets'].append({'id': 'office-2', 'kind': 'environment', 'name': 'office', 'description': 'Office again.'})
    seg['scenes'].append({'id': 'sc1-b', 'shot_ids': [60]})
    seg['shots']['60'] = row('sc1-b', 'anna', env='office-2')
    other = deepcopy(seed)
    other['scenes'].append({'id': 'sc1-b', 'shot_ids': [70]})
    other['shots']['70'] = row('sc1-b', 'anna')
    merged = deepcopy(seed)
    first = waves.absorb(merged, seed, seg, 's60', {}, {}, {}, {}, [])
    second = waves.absorb(merged, seed, other, 's70', {}, {}, {}, {}, [])
    assert first['merged'] == {'office-2': 'room'} and merged['shots']['60']['environment_key'] == 'room'
    assert second['scenes_renamed'] == {'sc1-b': 'sc1-b-s70'} and merged['shots']['70']['scene_id'] == 'sc1-b-s70'


async def _prepared(tmp_path, monkeypatch, parallel):
    import pytest  # noqa: F401
    from flowboard.services.video_analyzer import one_pass_film as film
    from tests.test_one_pass_voices import example
    tmp_path.mkdir(parents=True, exist_ok=True)
    analysis, data = example(tmp_path)
    (tmp_path / 'audio.wav').unlink()
    monkeypatch.setenv('FLOWBOARD_ONE_PASS_CATALOG_BATCH', '2')
    analysis['sequences'] = [{'first_shot': n, 'last_shot': n + 1} for n in (1, 3, 5, 7)]
    calls = []

    async def call(payload, cards, work, model, repair=False):
        ids = [s['shot'] for s in payload['shots']]
        calls.append((ids, [n for n, _ in payload['previous_shots']]))
        out = deepcopy(data)
        out['shots'] = {k: v for k, v in out['shots'].items() if int(k) in ids}
        out['scenes'] = [{**sc, 'shot_ids': ids} for sc in out['scenes']]
        return out
    monkeypatch.setattr(film, '_call', call)
    prepared, _, _ = await film.prepare(analysis, tmp_path, 'Film', {}, parallel=parallel)
    return prepared, calls


import pytest


@pytest.mark.asyncio
async def test_a_parallel_catalog_reads_each_segment_from_its_own_start_and_agrees_with_one_pass(tmp_path, monkeypatch):
    seq, seq_calls = await _prepared(tmp_path / 'seq', monkeypatch, 1)
    par, par_calls = await _prepared(tmp_path / 'par', monkeypatch, 2)
    assert sorted(c[0] for c in par_calls) == sorted(c[0] for c in seq_calls) == [[1, 2], [3, 4], [5, 6], [7, 8]]
    starts = {tuple(ids): previous for ids, previous in par_calls}
    assert starts[(3, 4)] == [] and starts[(5, 6)] == [] and starts[(7, 8)] == []
    strip = lambda shots: [{k: s.get(k) for k in ('shot', 'character_states', 'asset_presence', 'dialogue')} for s in shots]
    assert strip(par['shots']) == strip(seq['shots'])


def test_a_reskin_catalog_keeps_the_models_voice_notes_as_reviewer_notes():
    from flowboard.services.video_analyzer import one_pass_film as film
    notes = [{'shot': 61, 'code': 'voice_cluster_conflict', 'message': 'kept the visible speaker'},
             {'shot': 87, 'code': 'prop_continuity_conflict', 'message': 'wristband'},
             {'shot': 5, 'code': 'missing_environment', 'message': 'needs a place'}]
    assert [f['shot'] for f in film.blocking(film._model_findings(notes))] == [61, 87, 5]
    token = film.SOFT_SPEECH_NOTES.set(True)
    try:
        assert [f['shot'] for f in film.blocking(film._model_findings(notes))] == [5]
    finally:
        film.SOFT_SPEECH_NOTES.reset(token)


@pytest.mark.parametrize('text, kept', [
    ('She turns to him, name cards fade in beside each guest', 'She turns to him'),
    ('Title card reads the date; the car pulls up', 'the car pulls up'),
    ('He adjusts the name tag on his jacket', 'He adjusts the name tag on his jacket'),     # a prop
    ('He speaks. A name card fades in at upper left and right-center, then the upper-left card fades.', 'He speaks.'),
    ('Lips part while the caption shows', 'Lips part'),
    ('A title card appears. She opens the card he gave her.', 'She opens the card he gave her.'),  # a new sentence
])
def test_text_laid_over_the_picture_leaves_the_action_but_a_worn_name_tag_stays(text, kept):
    from flowboard.services.video_analyzer import one_pass_film as film
    assert film.without_captions(text) == kept
