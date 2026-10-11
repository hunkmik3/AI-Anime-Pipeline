"""Voices measured on the audio reach the catalog, settle who says each line, and reach the writer."""
import wave
from copy import deepcopy

import numpy as np
import pytest

from flowboard.services import prompt_writer, source_film
from flowboard.services.video_analyzer import one_pass_film as film, voice_id

LINES = ['Where were you tonight?', 'At the office, working late.', 'All night long, really?',
         'Yes, all night long.', 'You never call me anymore.', 'I know, I am sorry.',
         'Then come home earlier.', 'I will, I promise you.']


def example(tmp_path):
    from PIL import Image
    shots, segments = [], []
    for n in range(1, 9):
        own = []
        for k in (1, 2, 3):
            rel = f'frames/shot{n:03}_{k}.jpg'
            (tmp_path / rel).parent.mkdir(exist_ok=True)
            Image.new('RGB', (8, 8)).save(tmp_path / rel)
            own.append(rel)
        a = (n - 1) * 2.0
        shots.append({'shot': n, 'start': a, 'end': a + 2.0, 'frames': own,
                      'source': {'shot_size': 'MS', 'camera_crop': 'head to waist', 'camera_elevation': 'level',
                                 'composition': 'two-shot', 'camera_movement': 'static', 'action': 'They talk.'}})
        words = [{'word': ' ' + w, 'start': round(a + .2 + i * .3, 3), 'end': round(a + .2 + i * .3 + .26, 3),
                  'probability': .95} for i, w in enumerate(LINES[n - 1].split())]
        segments.append({'start': words[0]['start'], 'end': words[-1]['end'], 'language': 'en',
                         'text': LINES[n - 1], 'words': words})
    with wave.open(str(tmp_path / 'audio.wav'), 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(np.zeros(16000 * 16, dtype='<i2').tobytes())
    analysis = {'video': {'duration': 16., 'fps': 30., 'aspect_ratio': '16:9'}, 'shots': shots,
                'analysis_mode': 'one_pass', 'transcript': {'segments': segments},
                'sequences': [{'first_shot': 1, 'last_shot': 8, 'title': 'Late'}]}
    assets = [{'id': 'lina', 'kind': 'character', 'name': 'Lina', 'description': 'Adult woman, black hair',
               'states': [{'key': 'coat', 'label': 'Coat', 'look': 'black hair', 'wardrobe': 'green coat', 'posture': 'upright'}]},
              {'id': 'marc', 'kind': 'character', 'name': 'Marc', 'description': 'Adult man, grey beard',
               'states': [{'key': 'suit', 'label': 'Suit', 'look': 'grey beard', 'wardrobe': 'navy suit', 'posture': 'upright'}]},
              {'id': 'room', 'kind': 'environment', 'name': 'Kitchen', 'description': 'Small kitchen'}]
    for asset in assets:
        asset.update(evidence_ids=['shot-1-frame-2'], member_ids=[], depends_on_asset_ids=[], reference_required=True)
    rows = {}
    for n in range(1, 9):
        eid = f'shot-{n}-frame-2'
        # She speaks the odd shots, he the even ones; the catalog gives shot 4 to her by mistake.
        who = 'lina' if n % 2 or n == 4 else 'marc'
        rows[str(n)] = {'scene_id': 'kitchen', 'environment_key': 'room', 'evidence_ids': [eid],
                        'asset_presence': [{'asset_id': a['id'], 'visibility': 'visible', 'position': 'center',
                                            'state': 'unchanged', 'evidence_ids': [eid]} for a in assets],
                        'character_states': {'lina': 'coat', 'marc': 'suit'},
                        'speakers': [{'asset_id': who, 'label': who.title(), 'delivery': 'on_camera'}],
                        'transitions': [], 'departures': [], 'findings': []}
    data = {'assets': assets, 'scenes': [{'id': 'kitchen', 'shot_ids': list(range(1, 9)),
                                          'present_asset_ids': ['lina', 'marc', 'room']}],
            'shots': rows, 'findings': []}
    return analysis, data


@pytest.mark.asyncio
async def test_a_line_given_to_the_wrong_person_follows_its_voice_to_the_writer(tmp_path, monkeypatch):
    analysis, data = example(tmp_path)
    monkeypatch.setenv('FLOWBOARD_VOICE_LISTEN', '0')
    rng = np.random.default_rng(1)
    centres = rng.normal(size=(2, 256))

    async def fingerprints(audio, work, jobs):
        out = {}
        for job in jobs:
            n = int(job['id'][1:])                      # L0001 is shot 1's line, and so on
            v = centres[(n - 1) % 2] + rng.normal(size=256) * .4
            out[job['id']] = list(v / np.linalg.norm(v))
        return out

    payloads = []

    async def call(payload, *args, **kwargs):
        payloads.append(payload)
        return deepcopy(data)

    monkeypatch.setattr(voice_id, 'fingerprints', fingerprints)
    monkeypatch.setattr(film, '_call', call)
    prepared, cast, adapt = await film.prepare(analysis, tmp_path, 'Late', source_film.source_rules('anime_jp_modern'))

    pieces = payloads[0]['shots'][3]['dialogue_pieces']
    assert pieces[0]['voice'] == 'V1' or pieces[0]['voice'] == 'V2'
    assert pieces[0]['voice_confidence'] == 'high' and 'known_voices' in payloads[0]
    receipts = prepared['voices']['reconciled']
    assert [(r['shot'], r['from']['asset_id'], r['to']['asset_id']) for r in receipts] == [(4, 'lina', 'marc')]
    assert prepared['voices']['disputes'] == []
    said = {n: [(d['who'], d.get('speaker_id')) for d in s['dialogue']] for n, s in adapt['shots'].items()}
    assert said['4'] == [('Marc', 'marc')] and said['3'] == [('Lina', 'lina')]
    audit = prepared['shotlist_audit']
    assert audit['voices']['speakers_changed_by_voice'] == 1 and audit['voices']['voices_given_to_several_people'] == []
    # The writer prints whatever the board calls that character now.
    renamed = prompt_writer.bind_speakers([adapt['shots']['4']],
                                          [{'key': 'marc', 'source_asset_id': 'marc', 'name': 'Marc Duval'}])
    assert renamed[0]['dialogue'][0]['who'] == 'Marc Duval'
    film.validate_prepared(prepared)


@pytest.mark.asyncio
async def test_an_unsettled_wardrobe_flip_gets_one_repair_then_keeps_the_state(tmp_path, monkeypatch):
    analysis, data = example(tmp_path)
    monkeypatch.setenv('FLOWBOARD_VOICE_LISTEN', '0')
    (tmp_path / 'audio.wav').unlink()                      # no voices needed here
    data['assets'][0]['states'].append({'key': 'coat-open', 'label': 'Open coat', 'look': 'black hair',
                                        'wardrobe': 'green coat worn open over a white blouse', 'posture': 'upright'})
    data['shots']['5']['character_states'] = {'lina': 'coat-open', 'marc': 'suit'}
    calls = []

    async def call(payload, *args, repair=False, **kwargs):
        calls.append(repair)
        if repair:
            assert [f['code'] for f in payload['repair_only_these_findings']] == [
                'unexplained_wardrobe_change', 'unexplained_wardrobe_change']
        return deepcopy(data)

    monkeypatch.setattr(film, '_call', call)
    prepared, cast, adapt = await film.prepare(analysis, tmp_path, 'Late', source_film.source_rules('anime_jp_modern'))
    assert calls == [False, True]
    # Nobody saw a change: she keeps her coat closed (the flip back is gone with it).
    inventory = prepared['scene_inventory']
    assert [(r['shot'], r['asset_id'], r['from'], r['to']) for r in inventory['settled_wardrobe']] == [
        (5, 'lina', 'coat-open', 'coat')]
    assert inventory['shots']['5']['character_states'] == {'lina': 'coat', 'marc': 'suit'}
    assert not inventory.get('catalog_notes')
    assert prepared['shotlist_audit']['wardrobe']['notes_left_by_catalog'] == 0


@pytest.mark.asyncio
async def test_a_recorded_wardrobe_change_needs_no_repair(tmp_path, monkeypatch):
    analysis, data = example(tmp_path)
    monkeypatch.setenv('FLOWBOARD_VOICE_LISTEN', '0')
    (tmp_path / 'audio.wav').unlink()
    data['assets'][0]['states'].append({'key': 'coat-open', 'label': 'Open coat', 'look': 'black hair',
                                        'wardrobe': 'green coat worn open over a white blouse', 'posture': 'upright'})
    for n in range(5, 9):
        data['shots'][str(n)]['character_states'] = {'lina': 'coat-open', 'marc': 'suit'}
    data['shots']['5']['wardrobe_changes'] = [{'asset_id': 'lina', 'from_state': 'coat', 'to_state': 'coat-open',
                                               'basis': 'She unbuttons her coat.'}]
    calls = []

    async def call(payload, *args, repair=False, **kwargs):
        calls.append(repair)
        return deepcopy(data)

    monkeypatch.setattr(film, '_call', call)
    prepared, _, _ = await film.prepare(analysis, tmp_path, 'Late', source_film.source_rules('anime_jp_modern'))
    assert calls == [False] and not prepared['scene_inventory'].get('catalog_notes')
    assert prepared['scene_inventory']['shots']['5']['wardrobe_changes'][0]['basis'] == 'She unbuttons her coat.'


def flip_coat(data):
    data['assets'][0]['states'].append({'key': 'coat-open', 'label': 'Open coat', 'look': 'black hair',
                                        'wardrobe': 'green coat worn open over a white blouse', 'posture': 'upright'})
    data['shots']['5']['character_states'] = {'lina': 'coat-open', 'marc': 'suit'}


@pytest.mark.asyncio
async def test_a_repair_that_restates_only_its_fix_keeps_the_rest_of_the_first_answer(tmp_path, monkeypatch):
    analysis, data = example(tmp_path)
    monkeypatch.setenv('FLOWBOARD_VOICE_LISTEN', '0')
    (tmp_path / 'audio.wav').unlink()
    flip_coat(data)

    async def call(payload, *args, repair=False, **kwargs):
        if not repair:
            return deepcopy(data)
        # The model answers with the two corrected shots only; assets were given before.
        fixed = {n: deepcopy(data['shots'][n]) for n in ('5', '6')}
        fixed['5']['wardrobe_changes'] = [{'asset_id': 'lina', 'from_state': 'coat', 'to_state': 'coat-open',
                                           'basis': 'She unbuttons her coat.'}]
        fixed['6']['wardrobe_changes'] = [{'asset_id': 'lina', 'from_state': 'coat-open', 'to_state': 'coat',
                                           'basis': 'She buttons it again.'}]
        return {'assets': [], 'scenes': [], 'shots': fixed, 'findings': []}

    monkeypatch.setattr(film, '_call', call)
    prepared, _, _ = await film.prepare(analysis, tmp_path, 'Late', source_film.source_rules('anime_jp_modern'))
    assert not prepared['scene_inventory'].get('catalog_notes')
    assert {a['id'] for a in prepared['scene_inventory']['assets']} == {'lina', 'marc', 'room'}
    assert prepared['scene_inventory']['shots']['6']['wardrobe_changes'][0]['basis'] == 'She buttons it again.'


@pytest.mark.asyncio
async def test_a_repair_that_breaks_the_batch_is_dropped_and_the_flip_settled(tmp_path, monkeypatch):
    analysis, data = example(tmp_path)
    monkeypatch.setenv('FLOWBOARD_VOICE_LISTEN', '0')
    (tmp_path / 'audio.wav').unlink()
    flip_coat(data)

    async def call(payload, *args, repair=False, **kwargs):
        if not repair:
            return deepcopy(data)
        broken = deepcopy(data['shots']['5'])
        broken['character_states'] = {'nobody': 'coat'}
        return {'assets': [], 'shots': {'5': broken}, 'findings': []}

    monkeypatch.setattr(film, '_call', call)
    prepared, _, _ = await film.prepare(analysis, tmp_path, 'Late', source_film.source_rules('anime_jp_modern'))
    assert not prepared['scene_inventory'].get('catalog_notes')
    assert prepared['scene_inventory']['shots']['5']['character_states'] == {'lina': 'coat', 'marc': 'suit'}


def test_patching_merges_assets_and_scenes_by_id_and_shots_by_number():
    first = {'assets': [{'id': 'a', 'v': 1}, {'id': 'b', 'v': 1}], 'scenes': [{'id': 's', 'v': 1}],
             'shots': {'1': {'v': 1}, '2': {'v': 1}}, 'findings': [{'code': 'x'}]}
    out = film.patch_output(first, {'assets': [{'id': 'b', 'v': 2}], 'shots': {'2': {'v': 2}}, 'findings': []})
    assert out == {'assets': [{'id': 'a', 'v': 1}, {'id': 'b', 'v': 2}], 'scenes': [{'id': 's', 'v': 1}],
                   'shots': {'1': {'v': 1}, '2': {'v': 2}}, 'findings': []}


@pytest.mark.asyncio
async def test_a_failed_repair_call_leaves_the_first_answer_standing(tmp_path, monkeypatch):
    from flowboard.services import avis_text
    analysis, data = example(tmp_path)
    monkeypatch.setenv('FLOWBOARD_VOICE_LISTEN', '0')
    (tmp_path / 'audio.wav').unlink()
    flip_coat(data)

    async def call(payload, *args, repair=False, **kwargs):
        if repair:
            raise avis_text.AvisEmptyResponse('gpt-6-luna: provider returned empty text')
        return deepcopy(data)

    monkeypatch.setattr(film, '_call', call)
    prepared, _, _ = await film.prepare(analysis, tmp_path, 'Late', source_film.source_rules('anime_jp_modern'))
    inventory = prepared['scene_inventory']
    assert [r['shot'] for r in inventory['settled_wardrobe']] == [5] and not inventory.get('catalog_notes')
    assert inventory['repair_failures'][0]['error'].startswith('AvisEmptyResponse')


@pytest.mark.asyncio
async def test_unproven_on_camera_claim_is_retained_for_review_without_inventing_delivery(tmp_path, monkeypatch):
    analysis, data = example(tmp_path)
    monkeypatch.setenv('FLOWBOARD_VOICE_LISTEN', '0')
    (tmp_path / 'audio.wav').unlink()
    # Shot 4: Marc is only partly in frame, yet his line is claimed on camera with no face evidence.
    for p in data['shots']['4']['asset_presence']:
        if p['asset_id'] == 'marc':
            p['visibility'] = 'partial'
    data['shots']['4']['speakers'] = [{'asset_id': 'marc', 'label': 'Marc', 'delivery': 'on_camera'}]

    async def call(payload, *args, repair=False, **kwargs):
        return deepcopy(data)                     # the repair repeats the same claim

    monkeypatch.setattr(film, '_call', call)
    from flowboard.services.video_analyzer.catalog_recovery import CatalogNeedsReview
    with pytest.raises(CatalogNeedsReview) as caught:
        await film.prepare(analysis, tmp_path, 'Late', source_film.source_rules('anime_jp_modern'))
    assert caught.value.report['pending_shots'] == list(range(1, 9))
    assert data['shots']['4']['speakers'][0]['delivery'] == 'on_camera'
    assert not (tmp_path/'one-pass-production.json').exists()


@pytest.mark.asyncio
async def test_a_batch_the_catalog_model_will_not_answer_goes_to_the_vision_model(tmp_path, monkeypatch):
    from flowboard.services import avis_text
    analysis, data = example(tmp_path)
    monkeypatch.setenv('FLOWBOARD_VOICE_LISTEN', '0')
    monkeypatch.delenv('FLOWBOARD_ONE_PASS_CATALOG_FALLBACK_MODEL', raising=False)
    monkeypatch.delenv('FLOWBOARD_ONE_PASS_MODEL', raising=False)
    (tmp_path / 'audio.wav').unlink()
    models = []

    async def call(payload, cards, work, model, repair=False):
        models.append(model)
        if model == 'gpt-6-luna':
            raise avis_text.AvisEmptyResponse('gpt-6-luna: provider returned empty text')
        return deepcopy(data)

    monkeypatch.setattr(film, '_call', call)
    prepared, _, _ = await film.prepare(analysis, tmp_path, 'Late', source_film.source_rules('anime_jp_modern'))
    assert models == ['gpt-6-luna', 'claude-opus-5-5']
    assert prepared['scene_inventory']['fallback_batches'][0]['model'] == 'claude-opus-5-5'


@pytest.mark.asyncio
async def test_a_batch_neither_model_answers_goes_again_as_two_smaller_requests(tmp_path, monkeypatch):
    from flowboard.services import avis_text
    analysis, data = example(tmp_path)
    monkeypatch.setenv('FLOWBOARD_VOICE_LISTEN', '0')
    monkeypatch.delenv('FLOWBOARD_ONE_PASS_CATALOG_FALLBACK_MODEL', raising=False)
    monkeypatch.delenv('FLOWBOARD_ONE_PASS_MODEL', raising=False)
    (tmp_path / 'audio.wav').unlink()
    asked = []

    async def call(payload, cards, work, model, repair=False):
        numbers = [s['shot'] for s in payload['shots']]
        if not repair:
            asked.append((model, numbers))
        if model == 'gpt-6-luna':
            raise avis_text.AvisEmptyResponse('gpt-6-luna: provider returned empty text')
        if len(numbers) == 8:
            raise avis_text.AvisEmptyResponse('terminal empty response')
        out = deepcopy(data)
        out['shots'] = {k: v for k, v in out['shots'].items() if int(k) in numbers}
        out['scenes'] = [{**sc, 'shot_ids': [n for n in sc['shot_ids'] if n in numbers]} for sc in out['scenes']]
        return out

    monkeypatch.setattr(film, '_call', call)
    prepared, _, _ = await film.prepare(analysis, tmp_path, 'Late', source_film.source_rules('anime_jp_modern'))
    whole, first, second = list(range(1, 9)), [1, 2, 3, 4], [5, 6, 7, 8]
    assert asked == [('gpt-6-luna', whole), ('claude-opus-5-5', whole), ('gpt-6-luna', first),
                     ('claude-opus-5-5', first), ('gpt-6-luna', second), ('claude-opus-5-5', second)]
    inventory = prepared['scene_inventory']
    assert inventory['split_batches'] == [{'shots': whole, 'reason': 'AvisEmptyResponse: terminal empty response'}]
    assert [b['shots'] for b in inventory['fallback_batches']] == [first, second]
    assert sorted(inventory['shots'], key=int) == [str(n) for n in whole]


@pytest.mark.asyncio
async def test_other_catalog_errors_are_not_hidden_by_the_fallback(tmp_path, monkeypatch):
    analysis, data = example(tmp_path)
    monkeypatch.setenv('FLOWBOARD_VOICE_LISTEN', '0')
    (tmp_path / 'audio.wav').unlink()

    async def call(payload, cards, work, model, repair=False):
        raise ValueError('bad key')

    monkeypatch.setattr(film, '_call', call)
    with pytest.raises(ValueError, match='bad key'):
        await film.prepare(analysis, tmp_path, 'Late', source_film.source_rules('anime_jp_modern'))


@pytest.mark.asyncio
async def test_faces_that_are_not_the_person_s_earlier_faces_go_to_the_repair_then_stay_a_note(tmp_path, monkeypatch):
    from flowboard.services.video_analyzer import face_id
    analysis, data = example(tmp_path)
    monkeypatch.setenv('FLOWBOARD_VOICE_LISTEN', '0')
    (tmp_path / 'audio.wav').unlink()
    for n in range(1, 9):                       # Lina alone on screen; shots 6–8 are a new scene
        for p in data['shots'][str(n)]['asset_presence']:
            if p['asset_id'] == 'marc':
                p['visibility'] = 'offscreen'
        data['shots'][str(n)]['speakers'] = [{'asset_id': 'lina', 'label': 'Lina', 'delivery': 'on_camera'}]
        if n >= 6:
            data['shots'][str(n)]['scene_id'] = 'garden'
    data['scenes'] = [{'id': 'kitchen', 'shot_ids': [1, 2, 3, 4, 5], 'present_asset_ids': ['lina', 'marc', 'room']},
                      {'id': 'garden', 'shot_ids': [6, 7, 8], 'present_asset_ids': ['lina', 'marc', 'room']}]
    rng = np.random.default_rng(5)
    lina, stranger = (v / np.linalg.norm(v) for v in rng.normal(size=(2, 128)))

    async def faces_for(work, evidence):
        out = {}
        for e in evidence:
            who = lina if e['shot'] <= 5 else stranger
            v = who + rng.normal(size=128) * .02
            out[e['id']] = [{'box': [0, 0, 120, 120], 'score': .99, 'embedding': list(v / np.linalg.norm(v))}]
        return out

    asked = []

    async def call(payload, *args, repair=False, **kwargs):
        if repair:
            asked.extend(f['code'] for f in payload['repair_only_these_findings'])
        return deepcopy(data)

    monkeypatch.setenv('FLOWBOARD_FACE_CHECK', '1')
    monkeypatch.setattr(face_id, 'faces_for', faces_for)
    monkeypatch.setattr(film, '_call', call)
    prepared, _, _ = await film.prepare(analysis, tmp_path, 'Late', source_film.source_rules('anime_jp_modern'))
    assert 'face_contradicts_identity' in asked
    assert [n['code'] for n in prepared['scene_inventory']['catalog_notes']] == ['face_contradicts_identity']
    # The finished-catalog audit compares every scene with the others (with two, both disagree).
    assert 'garden' in {f['scene'] for f in prepared['identity_check']['faces_not_matching']}
    assert {f['character'] for f in prepared['shotlist_audit']['wardrobe']['faces_not_matching']} == {'lina'}


@pytest.mark.asyncio
async def test_a_person_seen_leaving_and_listed_as_departed_in_the_same_shot_leaves_after_it(tmp_path, monkeypatch):
    analysis, data = example(tmp_path)
    monkeypatch.setenv('FLOWBOARD_VOICE_LISTEN', '0')
    (tmp_path / 'audio.wav').unlink()
    for p in data['shots']['8']['asset_presence']:
        if p['asset_id'] == 'marc':
            p['visibility'] = 'partial'
    data['shots']['8']['departures'] = ['marc']
    data['shots']['8']['speakers'] = [{'asset_id': 'lina', 'label': 'Lina', 'delivery': 'on_camera'}]

    async def call(payload, *args, repair=False, **kwargs):
        return deepcopy(data)                    # the repair repeats the contradiction

    monkeypatch.setattr(film, '_call', call)
    prepared, _, _ = await film.prepare(analysis, tmp_path, 'Late', source_film.source_rules('anime_jp_modern'))
    row = prepared['scene_inventory']['shots']['8']
    assert row['departures'] == [] and 'marc' in {p['asset_id'] for p in row['asset_presence']}
    assert prepared['scene_inventory']['settled_contradictions'][0]['dropped'] == 'departure in the same shot'
