from copy import deepcopy

import pytest

from flowboard.services.video_analyzer import one_pass_film as film


CANONICAL_DESCRIPTION = (
    'Adult woman with dark eyebrows, dark hair, and a slim build; hair is gathered up '
    'in the opening room scene and slicked back when wet in the bath.'
)


def catalog(number):
    evidence = f'frame-{number}'
    return {
        'assets': [
            {'id': 'woman', 'kind': 'character', 'name': 'Woman',
             'description': CANONICAL_DESCRIPTION, 'evidence_ids': [evidence],
             'states': [{'key': 'uniform', 'label': 'Uniform', 'look': 'dark hair',
                         'wardrobe': 'black uniform', 'posture': 'upright'}]},
            {'id': 'room', 'kind': 'environment', 'name': 'Room',
             'description': 'A quiet room', 'evidence_ids': [evidence]}],
        'scenes': [{'id': 'hotel', 'shot_ids': [number], 'present_asset_ids': ['woman', 'room']}],
        'shots': {str(number): {
            'scene_id': 'hotel', 'environment_key': 'room', 'evidence_ids': [evidence],
            'asset_presence': [{'asset_id': 'woman', 'visibility': 'visible',
                                'state': 'wearing the uniform', 'evidence_ids': [evidence]}],
            'character_states': {'woman': 'uniform'}, 'speakers': [], 'findings': []}},
        'findings': []}


def first_catalog():
    known = {'assets': [], 'shots': {}, 'scenes': []}
    patch, issues = film.normalize(catalog(1), [{'shot': 1}], known,
                                  [{'id': 'frame-1', 'shot': 1}])
    assert not issues
    film.inv._merge(known, patch)
    return known


def test_known_identity_prose_is_carried_and_discarded_restatement_is_audited():
    known = first_catalog()
    data = catalog(2)
    data['assets'][0].update(name='Woman in the hotel', description=(
        'Adult woman with dark eyebrows, dark hair, and a slim build; '
        'her dark hair is gathered in a bun in the hotel scenes.'))
    originals = deepcopy((known, data))
    patch, issues = film.normalize(data, [{'shot': 2}], known,
                                  [{'id': 'frame-2', 'shot': 2}])
    assert not issues
    woman = next(a for a in patch['assets'] if a['id'] == 'woman')
    assert woman['name'] == 'Woman'
    assert woman['description'] == CANONICAL_DESCRIPTION
    assert patch['discarded_profile_restatements'] == [{
        'asset_id': 'woman', 'shots': [2], 'fields': {
            'name': {'retained': 'Woman', 'discarded': 'Woman in the hotel'},
            'description': {'retained': CANONICAL_DESCRIPTION,
                            'discarded': data['assets'][0]['description']}}}]
    assert (known, data) == originals


def test_partial_return_inherits_existing_state_before_validation():
    known=first_catalog()
    data=catalog(2)
    data['shots']['2']['character_states']={}
    data['shots']['2']['asset_presence'][0]['visibility']='partial'
    patch,issues=film.normalize(data,[{'shot':2}],known,[{'id':'frame-2','shot':2}])
    assert not issues
    assert patch['shots']['2']['character_states']=={'woman':'uniform'}


def test_state_does_not_implicitly_cross_a_scene_change():
    known=first_catalog()
    data=catalog(2)
    data['scenes'][0]['id']='later'
    data['shots']['2']['scene_id']='later'
    data['shots']['2']['character_states']={}
    _,issues=film.normalize(data,[{'shot':2}],known,[{'id':'frame-2','shot':2}])
    assert any(i['code']=='missing_character_state' for i in issues)


def test_identity_kind_conflict_is_still_blocked():
    known = first_catalog()
    data = catalog(2)
    data['assets'][0]['kind'] = 'prop'
    patch, issues = film.normalize(data, [{'shot': 2}], known,
                                  [{'id': 'frame-2', 'shot': 2}])
    assert any(i['code'] == 'identity_conflict' for i in issues)
    assert not any(a['id'] == 'woman' for a in patch['assets'])
    assert known['assets'][0]['kind'] == 'character'


def test_wardrobe_redefinition_is_blocked_and_canonical_state_is_retained():
    known = first_catalog()
    data = catalog(2)
    data['assets'][0]['states'][0]['wardrobe'] = 'red dress'
    patch, issues = film.normalize(data, [{'shot': 2}], known,
                                  [{'id': 'frame-2', 'shot': 2}])
    assert any(i['code'] == 'wardrobe_redefined' for i in issues)
    woman = next(a for a in patch['assets'] if a['id'] == 'woman')
    assert woman['states'] == known['assets'][0]['states']


def test_posture_only_restatement_keeps_canonical_state_and_audits_discarded_text():
    known = first_catalog()
    data = catalog(2)
    data['assets'][0]['states'][0]['posture'] = 'Posture varies by shot.'
    original = deepcopy((known, data))
    patch, issues = film.normalize(data, [{'shot': 2}], known,
                                  [{'id': 'frame-2', 'shot': 2}])
    assert not issues
    woman = next(a for a in patch['assets'] if a['id'] == 'woman')
    assert woman['states'] == known['assets'][0]['states']
    assert patch['discarded_wardrobe_posture_restatements'] == [{
        'asset_id': 'woman', 'state_key': 'uniform', 'shots': [2],
        'fields': {'posture': {'retained': 'upright', 'discarded': 'Posture varies by shot.'}}}]
    assert (known, data) == original


def test_known_state_metadata_is_discarded_without_revising_its_canonical_look():
    known = first_catalog()
    data = catalog(2)
    data['assets'][0]['states'][0].update(
        label='Hotel uniform', look='dark hair and glasses', posture='Standing or walking.')
    original = deepcopy((known, data))
    patch, issues = film.normalize(data, [{'shot': 2}], known,
                                  [{'id': 'frame-2', 'shot': 2}])
    assert not issues
    woman = next(a for a in patch['assets'] if a['id'] == 'woman')
    assert woman['states'] == known['assets'][0]['states']
    assert woman['states'][0]['look'] == 'dark hair'
    assert patch['discarded_wardrobe_metadata_restatements'] == [{
        'asset_id': 'woman', 'state_key': 'uniform', 'shots': [2], 'fields': {
            'label': {'retained': 'Uniform', 'discarded': 'Hotel uniform'},
            'look': {'retained': 'dark hair', 'discarded': 'dark hair and glasses'}}}]
    assert patch['discarded_wardrobe_posture_restatements'][0]['fields'] == {
        'posture': {'retained': 'upright', 'discarded': 'Standing or walking.'}}
    assert (known, data) == original


def test_metadata_restatement_does_not_allow_a_wardrobe_change():
    known = first_catalog()
    data = catalog(2)
    data['assets'][0]['states'][0].update(
        posture='Standing or walking.', look='dark hair and glasses', wardrobe='red dress')
    patch, issues = film.normalize(data, [{'shot': 2}], known,
                                  [{'id': 'frame-2', 'shot': 2}])
    assert any(i['code'] == 'wardrobe_redefined' for i in issues)
    woman = next(a for a in patch['assets'] if a['id'] == 'woman')
    assert woman['states'] == known['assets'][0]['states']
    assert not patch.get('discarded_wardrobe_posture_restatements')
    assert not patch.get('discarded_wardrobe_metadata_restatements')


def test_new_wardrobe_states_remain_available():
    known = first_catalog()
    data = catalog(2)
    new = {'key': 'red', 'label': 'Red dress', 'look': 'dark hair',
           'wardrobe': 'red dress', 'posture': 'Standing.'}
    data['assets'][0]['states'].append(new)
    data['shots']['2']['character_states']['woman'] = 'red'
    patch, issues = film.normalize(data, [{'shot': 2}], known,
                                  [{'id': 'frame-2', 'shot': 2}])
    assert not issues
    woman = next(a for a in patch['assets'] if a['id'] == 'woman')
    assert woman['states'] == known['assets'][0]['states'] + [new]


def test_posture_restatement_does_not_waive_unresolved_source_findings():
    known = first_catalog()
    data = catalog(2)
    data['assets'][0]['states'][0]['posture'] = 'Standing or walking.'
    data['assets'][0]['states'][0]['look'] = 'dark hair and glasses'
    finding = {'shot': 2, 'code': 'foreground_identity_uncertain', 'message': 'Identity unconfirmed.'}
    data['shots']['2']['findings'] = [finding]
    _, issues = film.normalize(data, [{'shot': 2}], known,
                              [{'id': 'frame-2', 'shot': 2}])
    assert finding in issues


@pytest.mark.asyncio
async def test_preparation_keeps_restatement_provenance_without_repair_call(tmp_path, monkeypatch):
    monkeypatch.setenv('FLOWBOARD_ONE_PASS_CATALOG_BATCH', '1')
    monkeypatch.setattr(film.inv, '_initial_evidence', lambda *args: [
        {'id': f'frame-{n}', 'shot': n, 'frame': f'frame-{n}.jpg'} for n in (1, 2)])
    calls = []

    async def call(payload, *args, **kwargs):
        number = payload['shots'][0]['shot']
        calls.append((number, kwargs))
        data = catalog(number)
        if number == 2:
            data['assets'][0]['description'] = 'The same woman with her hair in a bun.'
            data['assets'][0]['states'][0]['posture'] = 'Standing or walking.'
            data['assets'][0]['states'][0]['look'] = 'dark hair and glasses'
        return data

    monkeypatch.setattr(film, '_call', call)
    analysis = {'video': {'duration': 4., 'fps': 30.}, 'shots': [
        {'shot': n, 'start': (n-1)*2., 'end': n*2., 'dialogue_lines': [],
         'source': {'shot_size': 'MS', 'camera_movement': 'static', 'action': 'The woman waits.'}}
        for n in (1, 2)]}
    prepared, _, _ = await film.prepare(analysis, tmp_path, 'Film', {})
    assert calls == [(1, {}), (2, {})]
    assert prepared['scene_inventory']['assets'][0]['description'] == CANONICAL_DESCRIPTION
    provenance = prepared['scene_inventory']['discarded_profile_restatements']
    assert provenance[0]['asset_id'] == 'woman' and provenance[0]['shots'] == [2]
    posture = prepared['scene_inventory']['discarded_wardrobe_posture_restatements']
    assert posture[0]['fields']['posture'] == {'retained': 'upright', 'discarded': 'Standing or walking.'}
    metadata = prepared['scene_inventory']['discarded_wardrobe_metadata_restatements']
    assert metadata[0]['fields']['look'] == {'retained': 'dark hair', 'discarded': 'dark hair and glasses'}
    film.validate_prepared(prepared)
