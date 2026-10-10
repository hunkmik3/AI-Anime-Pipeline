"""A later batch rebinds earlier shots of the scene it continues when its frames prove a
state already held there (a wet uniform a wide shot could not show), with a receipt."""
import pytest

from flowboard.services.video_analyzer import one_pass_film as film

DRY = {'key': 'uniform', 'label': 'Uniform', 'look': 'dark hair', 'wardrobe': 'black uniform', 'posture': 'upright'}
WET = {'key': 'wet-uniform', 'label': 'Wet uniform', 'look': 'dark hair', 'wardrobe': 'black uniform, soaked',
       'posture': 'upright'}


def answer(scenes, state, **extra):
    """A catalog answer for ``scenes`` = {shot: scene id}, the woman bound to ``state``."""
    first = min(scenes)
    order = list(dict.fromkeys(scenes.values()))
    return {
        'assets': [{'id': 'woman', 'kind': 'character', 'name': 'Woman', 'description': 'Adult woman with dark hair.',
                    'evidence_ids': [f'frame-{first}'], 'states': [DRY, WET]},
                   {'id': 'lift', 'kind': 'environment', 'name': 'Lift', 'description': 'A gold lift.',
                    'evidence_ids': [f'frame-{first}']}],
        'scenes': [{'id': sc, 'shot_ids': [n for n, s in scenes.items() if s == sc],
                    'present_asset_ids': ['woman', 'lift']} for sc in order],
        'shots': {str(n): {'scene_id': sc, 'environment_key': 'lift', 'evidence_ids': [f'frame-{n}'],
                           'asset_presence': [{'asset_id': 'woman', 'visibility': 'visible',
                                               'state': 'in her uniform', 'evidence_ids': [f'frame-{n}']}],
                           'character_states': {'woman': state}, 'speakers': [], 'findings': []}
                  for n, sc in scenes.items()},
        'findings': [], **extra}


def run(data, known):
    numbers = sorted(int(n) for n in data['shots'])
    return film.normalize(data, [{'shot': n} for n in numbers], known,
                          [{'id': f'frame-{n}', 'shot': n} for n in numbers])


def first_batch():
    known = {'schema_version': 1, 'assets': [], 'scenes': [], 'shots': {}}
    patch, issues = run(answer({1: 'corridor', 2: 'lift', 3: 'lift'}, 'uniform'), known)
    assert not issues
    film.inv._merge(known, patch)
    return known


def codes(issues):
    return [(i['code'], i.get('shot')) for i in issues]


def test_the_scene_a_batch_continues_runs_back_to_where_it_began():
    known = first_batch()
    assert film.continuing_scene(known) == {'scene_id': 'lift', 'shots': {'2': {'woman': 'uniform'},
                                                                           '3': {'woman': 'uniform'}}}
    assert film.continuing_scene({'shots': {}}) == {}


def test_later_frames_rebind_the_earlier_shots_of_the_same_scene():
    known = first_batch()
    # Without a rebinding the wet close-up reads as an unexplained change (a reviewer note).
    _, issues = run(answer({4: 'lift', 5: 'lift'}, 'wet-uniform'), known)
    assert codes(issues) == [('unexplained_wardrobe_change', 4)]
    assert "earlier_states {'3': {'woman': 'wet-uniform'}}" in issues[0]['message']
    data = answer({4: 'lift', 5: 'lift'}, 'wet-uniform',
                  earlier_states={'2': {'woman': 'wet-uniform'}, '3': {'woman': 'wet-uniform'}})
    patch, issues = run(data, known)
    assert issues == []
    assert patch['earlier_states'] == {'2': {'woman': 'wet-uniform'}, '3': {'woman': 'wet-uniform'}}
    assert known['shots']['3']['character_states'] == {'woman': 'uniform'}      # checked on a copy
    film.inv._merge(known, patch)
    film.apply_earlier_states(known, patch, {4, 5})
    assert [known['shots'][n]['character_states']['woman'] for n in '12345'] == \
        ['uniform', 'wet-uniform', 'wet-uniform', 'wet-uniform', 'wet-uniform']
    assert known['rebound_earlier_states'] == [
        {'shot': 2, 'asset_id': 'woman', 'from': 'uniform', 'to': 'wet-uniform', 'by_shots': [4, 5]},
        {'shot': 3, 'asset_id': 'woman', 'from': 'uniform', 'to': 'wet-uniform', 'by_shots': [4, 5]}]


def test_a_rebinding_outside_the_continuing_scene_is_ignored_with_a_note():
    known = first_batch()
    data = answer({4: 'lift'}, 'wet-uniform', earlier_states={
        '1': {'woman': 'wet-uniform'},          # another scene
        '3': {'woman': 'bath'},                  # not one of her states
        '9': {'woman': 'wet-uniform'}})          # not an earlier shot
    patch, issues = run(data, known)
    assert 'earlier_states' not in patch
    assert codes(issues).count(('invalid_earlier_state', 4)) == 3
    assert film.blocking(issues) == []
    # A batch that starts a new scene continues nothing.
    patch, issues = run(answer({4: 'kitchen'}, 'wet-uniform', earlier_states={'3': {'woman': 'wet-uniform'}}), known)
    assert 'earlier_states' not in patch and codes(issues) == [('invalid_earlier_state', 4)]


def test_wardrobe_and_identity_findings_of_the_model_are_notes_others_block():
    known = first_batch()
    data = answer({4: 'lift'}, 'uniform')
    data['shots']['4']['findings'] = [
        {'shot': 4, 'code': 'wardrobe-state-inconsistency', 'message': 'Shot 3 should be wet.'},
        {'shot': 4, 'code': 'identity-unresolved', 'message': 'The blurred figure may be her.'},
        {'shot': 4, 'code': 'speaker-identity-unclear', 'message': 'Who says it?'},
        {'shot': 4, 'code': 'prop_custody', 'message': 'The cup moves hands.'}]
    _, issues = run(data, known)
    assert [i['code'] for i in film.blocking(issues)] == ['speaker-identity-unclear', 'prop_custody']
    assert len(issues) == 4


def test_a_repair_restates_the_rebinding_or_leaves_the_first_answer_s():
    first = answer({4: 'lift'}, 'wet-uniform', earlier_states={'3': {'woman': 'wet-uniform'}})
    assert film.patch_output(first, {'findings': []})['earlier_states'] == {'3': {'woman': 'wet-uniform'}}
    assert film.patch_output(first, {'earlier_states': {}, 'findings': []})['earlier_states'] == {}


@pytest.mark.asyncio
async def test_preparation_applies_the_rebinding_to_the_film(tmp_path, monkeypatch):
    monkeypatch.setenv('FLOWBOARD_ONE_PASS_CATALOG_BATCH', '2')
    monkeypatch.setattr(film.inv, '_initial_evidence', lambda *args: [
        {'id': f'frame-{n}', 'shot': n, 'frame': f'frame-{n}.jpg'} for n in (1, 2, 3, 4)])
    calls = []

    async def call(payload, *args, **kwargs):
        numbers = [s['shot'] for s in payload['shots']]
        calls.append((numbers, payload['continuing_scene'], kwargs))
        if numbers == [1, 2]:
            return answer({1: 'lift', 2: 'lift'}, 'uniform')
        return answer({3: 'lift', 4: 'lift'}, 'wet-uniform', earlier_states={'1': {'woman': 'wet-uniform'},
                                                                             '2': {'woman': 'wet-uniform'}})

    monkeypatch.setattr(film, '_call', call)
    analysis = {'video': {'duration': 8., 'fps': 30.}, 'shots': [
        {'shot': n, 'start': (n-1)*2., 'end': n*2., 'dialogue_lines': [],
         'source': {'shot_size': 'MS', 'camera_movement': 'static', 'action': 'She rides the lift.'}}
        for n in (1, 2, 3, 4)]}
    prepared, _, adaptation = await film.prepare(analysis, tmp_path, 'Film', {})
    assert [c[0] for c in calls] == [[1, 2], [3, 4]] and all(c[2] == {} for c in calls)    # no repair
    assert calls[1][1] == {'scene_id': 'lift', 'shots': {'1': {'woman': 'uniform'}, '2': {'woman': 'uniform'}}}
    inventory = prepared['scene_inventory']
    assert [inventory['shots'][n]['character_states']['woman'] for n in '1234'] == ['wet-uniform'] * 4
    assert [r['shot'] for r in inventory['rebound_earlier_states']] == [1, 2]
    assert adaptation['shots']['1']['character_states'] == {'woman': 'wet-uniform'}
    film.validate_prepared(prepared)


def test_an_unexplained_change_left_by_the_repair_keeps_the_earlier_state():
    known = first_batch()
    data = answer({4: 'lift', 5: 'lift'}, 'wet-uniform')
    _, issues = run(data, known)
    [issue] = issues
    assert (issue['code'], issue['asset_id'], issue['from_state'], issue['to_state']) == \
        ('unexplained_wardrobe_change', 'woman', 'uniform', 'wet-uniform')
    settled, receipts = film.settle_wardrobe(data, issues)
    assert [settled['shots'][n]['character_states']['woman'] for n in '45'] == ['uniform', 'uniform']
    assert [r['shot'] for r in receipts] == [4, 5]
    assert run(settled, known)[1] == []
    # A change the frames record is kept.
    data['shots']['4']['wardrobe_changes'] = [{'asset_id': 'woman', 'from_state': 'uniform',
                                               'to_state': 'wet-uniform', 'basis': 'Wine spills on her.'}]
    assert run(data, known)[1] == []


def test_the_final_catalog_never_changes_a_state_inside_a_scene_without_a_record():
    known = first_batch()
    later = answer({4: 'lift', 5: 'lift'}, 'wet-uniform')
    patch, _ = run(later, known)
    film.inv._merge(known, patch)           # e.g. two batches that judged the costume differently
    receipts = film.settle_scene_wardrobe(known)
    assert [(r['shot'], r['from'], r['to']) for r in receipts] == [(4, 'wet-uniform', 'uniform'), (5, 'wet-uniform', 'uniform')]
    assert [known['shots'][n]['character_states']['woman'] for n in '2345'] == ['uniform'] * 4
    assert known['shots']['4']['asset_presence'][0]['state'] == 'In Uniform (kept through this scene; no change is visible).'
    assert film.settle_scene_wardrobe(known) == []          # idempotent
    # A recorded change stands, and a new scene starts fresh.
    known2 = first_batch()
    later['shots']['4']['wardrobe_changes'] = [{'asset_id': 'woman', 'from_state': 'uniform',
                                                'to_state': 'wet-uniform', 'basis': 'Wine spills on her.'}]
    film.inv._merge(known2, run(later, known2)[0])
    assert film.settle_scene_wardrobe(known2) == []
