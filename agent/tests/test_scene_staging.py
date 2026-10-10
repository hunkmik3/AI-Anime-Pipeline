"""Scene staging: where each person is relative to the set, read once per scene from the
source frames and locked into every shot's prompt like the camera."""
import asyncio
import json

from PIL import Image

from flowboard.services import cinematic_prompt, prompt_writer
from flowboard.services.video_analyzer import board as board_mod, scene_staging

ANSWER = {
    'layout': 'Hotel corridor with a lift whose doors stand open; a cream pillar to the left of the opening.',
    'shots': {
        '1': {'staging': 'He stands in the corridor outside the open lift, at its threshold, right hand on the pillar.',
              'landmark_states': 'lift doors open'},
        '2': {'staging': 'She faces him in the corridor, outside the lift.', 'landmark_states': 'lift doors open'}}}


def scene(tmp_path, numbers=(1, 2, 3)):
    (tmp_path / 'frames').mkdir()
    evidence = []
    for n in numbers:
        for k in (1, 2, 3):
            rel = f'frames/shot{n:03d}_{k}.jpg'
            Image.new('RGB', (16, 16), (n, k, 0)).save(tmp_path / rel)
            evidence.append({'id': f'shot-{n}-frame-{k}', 'shot': n, 'frame': rel})
    shots = [{'shot': n, 'source': {'shot_size': 'MS', 'blocking': 'man center-right', 'setting': 'corridor'}}
             for n in numbers]
    inventory = {'assets': [{'id': 'man', 'kind': 'character', 'name': 'Man'},
                            {'id': 'corridor', 'kind': 'environment', 'name': 'Corridor'}],
                 'shots': {str(n): {'scene_id': 'lift-talk', 'asset_presence': [
                     {'asset_id': 'man', 'visibility': 'visible', 'position': 'frame right'},
                     {'asset_id': 'corridor', 'visibility': 'visible'}]} for n in numbers}}
    return shots, inventory, evidence


def test_a_scene_is_read_once_from_its_frames_and_reused_from_the_journal(tmp_path, monkeypatch):
    shots, inventory, evidence = scene(tmp_path)
    asked = []

    async def complete(name, content):
        asked.append([c for c in content if c.get('type') == 'text' and c['text'].startswith('Shot ')])
        return json.dumps(ANSWER)

    monkeypatch.setattr(scene_staging, '_complete', complete)
    out = asyncio.run(scene_staging.extract(shots, inventory, evidence, tmp_path))
    assert len(asked) == 1 and [c['text'] for c in asked[0]] == ['Shot 1 source frame', 'Shot 2 source frame',
                                                                'Shot 3 source frame']
    assert out['scenes'] == {'lift-talk': ANSWER['layout']}
    assert out['shots']['1']['staging'].startswith('He stands in the corridor outside the open lift')
    assert out['missing'] == [3]                       # not answered: no staging, nothing invented
    assert asyncio.run(scene_staging.extract(shots, inventory, evidence, tmp_path)) == out and len(asked) == 1


def test_an_unanswered_scene_leaves_no_staging_and_is_not_resent(tmp_path, monkeypatch):
    shots, inventory, evidence = scene(tmp_path)

    async def refused(name, content):
        raise TimeoutError()

    monkeypatch.setattr(scene_staging, '_complete', refused)
    out = asyncio.run(scene_staging.extract(shots, inventory, evidence, tmp_path))
    assert out['shots'] == {} and out['missing'] == [1, 2, 3]

    async def never(name, content):
        raise AssertionError('an unfinished request is not resent')

    monkeypatch.setattr(scene_staging, '_complete', never)
    assert asyncio.run(scene_staging.extract(shots, inventory, evidence, tmp_path))['missing'] == [1, 2, 3]


def test_the_staging_line_is_locked_after_the_background_and_reaches_the_writer():
    line = scene_staging.staging_line(ANSWER['shots']['1'])
    assert line.endswith('(lift doors open)')
    prompt = ('# SHOT 1 | 00:00–00:03.000\n**Duration:** 3 seconds\n**Framing:** wide\n**Camera:** pan\n'
              '**Staging:** he is inside the lift (made up)\nHe talks.\n**End state:** still.\n'
              '# SHOT 2 | 00:03.000–00:05.000\n**Duration:** 2 seconds\n**Framing:** close\n**Camera:** static\n'
              'Her face.\n**End state:** still.\n')
    shots = [{'framing': 'WS', 'framing_note': '', 'camera': 'static', 'background': 'open lift, pillar',
              'staging': line},
             {'framing': 'CU', 'framing_note': '', 'camera': 'static', 'background': '', 'staging': 'She faces him.'}]
    out = cinematic_prompt.serialize_source_locks(prompt, shots, [(0, 3), (3, 5)])
    first, second = out.split('# SHOT 2')
    assert f'**Background:** open lift, pillar\n**Staging:** {line}\n' in first and 'made up' not in first
    assert '**Camera:** static\n**Staging:** She faces him.\n' in second
    rows = [{'framing': 'MS', 'camera': 'static', 'staging': line, 'action': ['He talks.'], 'dialogue': [],
             'duration_s': 3}]
    ask = prompt_writer._payload({'key': 'c1', 'label': 'CLIP 1'}, rows, [(0, 3)], 3, characters=[],
                                 environment=None, look='realistic', aspect_ratio='16:9', previous_state='',
                                 unsafe={}, school_age=False)
    assert ask['shots'][0]['staging'] == line


def test_a_board_shot_carries_its_staging_and_the_scene_layout():
    shot = {'shot': 7, 'start': 1.0, 'end': 3.0, 'source': {'shot_size': 'MS'}}
    out = board_mod.board_shot(1, shot, {'staging': 'Outside the open lift.', 'scene_layout': 'Lift corridor.'}, {})
    assert out['staging'] == 'Outside the open lift.' and out['scene_layout'] == 'Lift corridor.'


def test_the_preparation_locks_each_shot_staging_into_the_shotlist(tmp_path, monkeypatch):
    import pytest
    from flowboard.services.video_analyzer import one_pass_film as film
    from tests.test_one_pass_earlier_states import answer
    monkeypatch.setenv('FLOWBOARD_SCENE_STAGING', '1')
    monkeypatch.setenv('FLOWBOARD_ONE_PASS_CATALOG_BATCH', '2')
    (tmp_path / 'frames').mkdir()
    for n in (1, 2):
        Image.new('RGB', (8, 8)).save(tmp_path / f'frames/shot{n:03d}_1.jpg')
    monkeypatch.setattr(film.inv, '_initial_evidence', lambda *args: [
        {'id': f'frame-{n}', 'shot': n, 'frame': f'frames/shot{n:03d}_1.jpg'} for n in (1, 2)])

    async def call(payload, *args, **kwargs):
        return answer({1: 'lift', 2: 'lift'}, 'uniform')

    async def complete(name, content):
        return json.dumps({'layout': 'Lift corridor, doors open.', 'shots': {
            '1': {'staging': 'She waits outside the open lift.', 'landmark_states': 'doors open'},
            '2': {'staging': 'She still waits outside it.', 'landmark_states': 'doors open'}}})

    monkeypatch.setattr(film, '_call', call)
    monkeypatch.setattr(scene_staging, '_complete', complete)
    analysis = {'video': {'duration': 4., 'fps': 30.}, 'shots': [
        {'shot': n, 'start': (n-1)*2., 'end': n*2., 'dialogue_lines': [],
         'source': {'shot_size': 'MS', 'camera_movement': 'static', 'action': 'She waits.'}} for n in (1, 2)]}
    prepared, _, adaptation = asyncio.run(film.prepare(analysis, tmp_path, 'Film', {}))
    assert adaptation['shots']['1']['staging'] == 'She waits outside the open lift. (doors open)'
    assert adaptation['shots']['2']['scene_layout'] == 'Lift corridor, doors open.'
    locked = prepared['source_verification']['locked_shots']['1']
    assert locked['staging'] == adaptation['shots']['1']['staging']
    film.validate_prepared(prepared)


def places(tmp_path):
    """The lift-talk scene with the catalog's lobby on shot 2 and two props in every shot."""
    shots, inventory, evidence = scene(tmp_path)
    inventory['assets'] += [{'id': 'lobby', 'kind': 'environment', 'name': 'Lobby'},
                            {'id': 'lift-door', 'kind': 'prop', 'name': 'Lift door'},
                            {'id': 'papers', 'kind': 'prop', 'name': 'Papers'}]
    for n in (1, 2, 3):
        row = inventory['shots'][str(n)]
        row['environment_key'] = 'lobby' if n == 2 else 'corridor'
        row['asset_presence'] = [p for p in row['asset_presence'] if p['asset_id'] != 'corridor'] + [
            {'asset_id': 'lift-door', 'visibility': 'visible'}, {'asset_id': 'papers', 'visibility': 'visible'}]
    return shots, inventory, evidence


VIEWS = {'master': 'Facing the lift bank.', 'reverse': 'Facing back toward the lobby arch.'}


def test_a_scene_names_where_its_people_stand_which_way_each_shot_looks_and_its_built_in_props(tmp_path, monkeypatch):
    shots, inventory, evidence = places(tmp_path)
    asked = []

    async def complete(name, content):
        asked.append(json.loads(content[0]['text']))
        return json.dumps({'layout': 'A lift bank; the lobby beyond the arch.',
                           'views': {'corridor': {**VIEWS, 'master_shots': [1, 3], 'reverse_shots': [2, 9]},
                                     'mars': VIEWS},
                           'set_states': {'corridor': 'focal lift open'},
                           'fixtures': ['lift-door', 'ghost'],
                           'shots': {'1': {'staging': 'He waits outside the lift.', 'set': 'corridor', 'view': 'master'},
                                     '2': {'staging': 'She stands outside the lift, in the corridor.', 'set': 'corridor',
                                           'view': 'reverse', 'behind': 'the opposite wall and the lobby arch'},
                                     '3': {'staging': 'He turns.', 'set': 'mars', 'view': 'sideways'}}})

    monkeypatch.setattr(scene_staging, '_complete', complete)
    out = asyncio.run(scene_staging.extract(shots, inventory, evidence, tmp_path))
    assert asked[0]['environments'] == {'corridor': 'Corridor', 'lobby': 'Lobby'}
    assert asked[0]['props'] == {'lift-door': 'Lift door', 'papers': 'Papers'}
    assert [asked[0]['shots'][i]['catalog_environment'] for i in range(3)] == ['corridor', 'lobby', 'corridor']
    assert (out['shots']['2']['set'], out['shots']['2']['view']) == ('corridor', 'reverse')   # in the corridor, looking out
    assert (out['shots']['3']['set'], out['shots']['3']['view']) == ('', '')                  # nothing invented
    # Anchor shots only where the answer itself puts that shot in the place, looking that way.
    assert out['views'] == {'corridor': {**VIEWS, 'master_shots': [1], 'reverse_shots': [2]}}
    assert out['set_states'] == {'corridor': 'focal lift open'} and out['fixtures'] == {'lift-door': ['corridor']}
    assert scene_staging.staging_line(out['shots']['2']) == (
        'She stands outside the lift, in the corridor. Behind them: the opposite wall and the lobby arch. '
        'Location view: reverse.')


def test_a_place_keeps_the_views_an_earlier_scene_named(tmp_path, monkeypatch):
    shots, inventory, evidence = places(tmp_path)
    inventory['shots']['3']['scene_id'] = 'return'
    asked = []

    async def complete(name, content):
        asked.append(json.loads(content[0]['text']))
        views = VIEWS if len(asked) == 1 else {'master': 'Facing the arch.', 'reverse': 'Facing the lifts.'}
        numbers = [s['shot'] for s in asked[-1]['shots']]
        return json.dumps({'layout': 'Lifts.', 'views': {'corridor': views},
                           'shots': {str(n): {'staging': 'Here.', 'set': 'corridor', 'view': 'master'} for n in numbers}})

    monkeypatch.setattr(scene_staging, '_complete', complete)
    out = asyncio.run(scene_staging.extract(shots, inventory, evidence, tmp_path))
    assert asked[0]['known_views'] == {} and asked[1]['known_views'] == {'corridor': VIEWS}
    assert out['views'] == {'corridor': {**VIEWS, 'master_shots': [], 'reverse_shots': []}}


def test_the_preparation_puts_a_shot_looking_into_the_lobby_in_the_corridor_set(tmp_path, monkeypatch):
    from flowboard.services.video_analyzer import one_pass_film as film
    from tests.test_one_pass_earlier_states import answer
    monkeypatch.setenv('FLOWBOARD_SCENE_STAGING', '1')
    monkeypatch.setenv('FLOWBOARD_ONE_PASS_CATALOG_BATCH', '2')
    (tmp_path / 'frames').mkdir()
    for n in (1, 2):
        Image.new('RGB', (8, 8)).save(tmp_path / f'frames/shot{n:03d}_1.jpg')
    monkeypatch.setattr(film.inv, '_initial_evidence', lambda *args: [
        {'id': f'frame-{n}', 'shot': n, 'frame': f'frames/shot{n:03d}_1.jpg'} for n in (1, 2)])

    async def call(payload, *args, **kwargs):
        data = answer({1: 'lift', 2: 'lift'}, 'uniform')
        data['assets'] += [{'id': 'lobby', 'kind': 'environment', 'name': 'Lobby', 'description': 'A grand lobby.',
                            'evidence_ids': ['frame-2']},
                           {'id': 'lift-door', 'kind': 'prop', 'name': 'Lift door', 'description': 'A brass lift door.',
                            'evidence_ids': ['frame-1']}]
        data['shots']['2']['environment_key'] = 'lobby'
        for n in ('1', '2'):
            data['shots'][n]['asset_presence'].append({'asset_id': 'lift-door', 'visibility': 'visible',
                                                       'state': 'open', 'evidence_ids': [f'frame-{n}']})
        data['scenes'][0]['present_asset_ids'] += ['lift-door']
        return data

    async def complete(name, content):
        return json.dumps({'layout': 'A lift bank; the lobby beyond the arch.', 'views': {'lift': VIEWS},
                           'set_states': {'lift': 'focal lift open'}, 'fixtures': ['lift-door'],
                           'shots': {'1': {'staging': 'She waits outside the open lift.', 'set': 'lift', 'view': 'master'},
                                     '2': {'staging': 'She faces him, the lobby behind her.', 'set': 'lift',
                                           'view': 'reverse'}}})

    monkeypatch.setattr(film, '_call', call)
    monkeypatch.setattr(scene_staging, '_complete', complete)
    analysis = {'video': {'duration': 4., 'fps': 30.}, 'shots': [
        {'shot': n, 'start': (n-1)*2., 'end': n*2., 'dialogue_lines': [],
         'source': {'shot_size': 'MS', 'camera_movement': 'static', 'action': 'She waits.'}} for n in (1, 2)]}
    prepared, cast, adaptation = asyncio.run(film.prepare(analysis, tmp_path, 'Film', {}))
    assert prepared['scene_inventory']['shots']['2']['environment_key'] == 'lobby'     # the source catalog stays
    assert cast['shots']['2']['environment_key'] == 'lift'
    assert adaptation['setting_rebinds'] == [{'shot': 2, 'from': 'lobby', 'to': 'lift',
                                              'basis': 'scene staging: the shot looks from this place into the other one'}]
    assert [adaptation['shots'][n]['set_view'] for n in ('1', '2')] == ['master', 'reverse']
    lift = next(e for e in cast['environments'] if e['source_asset_id'] == 'lift')
    assert (lift['views'], lift['set_states']) == ({**VIEWS, 'master_shots': [], 'reverse_shots': []}, 'focal lift open')
    door = next(e for e in cast['props'] if e['source_asset_id'] == 'lift-door')
    assert door['fixture_of'] == ['lift']
    film.validate_prepared(prepared)
    board = asyncio.run(board_mod.build_board(prepared, adaptation, cast=cast, preserve_source_shots=True))
    shots = [s for clip in board['shots'].values() for s in clip]
    assert [(s['environment_key'], s['set_view']) for s in shots] == [('lift', 'master'), ('lift', 'reverse')]
