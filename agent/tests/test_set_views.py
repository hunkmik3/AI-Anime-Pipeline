"""One set per scene, drawn as two reference views: its master and the camera turned around.

A prop built into the set (a lift door) is pictured by the set's master view, not by a sheet
of its own; each view is anchored on the source frames that look its way.
"""
from copy import deepcopy

import pytest
from PIL import Image

from flowboard.services import production_run, prompt_coverage, prompt_writer, raccord, set_anchor, shot_package as pack

VIEWS = {'master': 'Facing the lift bank.', 'reverse': 'Facing back toward the lobby arch.'}


def plate(name):
    return {'referenceUrl': 'https://r2.test/' + name, 'mediaId': 'media-' + name, 'prompt': ''}


def board(reverse=True, views=True):
    corridor = {'key': 'corridor', 'source_asset_id': 'corridor', 'name': 'Hotel corridor', 'kind': 'environment',
                'summary': 'Marble lift corridor.',
                'design': {'architecture': 'A bank of brass lifts.', 'camera': 'Looking down the lift bank.',
                           'depth': 'Lifts recede to the arch.', 'light': 'Warm sconces.'}}
    if views:
        corridor.update(views=dict(VIEWS), set_states='focal lift open, its cabin lit')
    shots = [{'n': 1, 'source_shot': 1, 'scene_id': 'talk', 'duration_s': 3, 'environment_key': 'corridor',
              'set_view': 'master', 'framing': 'WIDE', 'character_keys': ['ann'], 'character_states': {'ann': 'uniform'},
              'asset_presence': [{'asset_id': 'ann', 'visibility': 'visible'},
                                 {'asset_id': 'lift-door', 'visibility': 'visible'},
                                 {'asset_id': 'papers', 'visibility': 'visible', 'holder_id': 'ann'}]},
             {'n': 2, 'source_shot': 2, 'scene_id': 'talk', 'duration_s': 3, 'environment_key': 'corridor',
              'set_view': 'reverse', 'framing': 'OTS', 'character_keys': ['ann'], 'character_states': {'ann': 'uniform'},
              'asset_presence': [{'asset_id': 'ann', 'visibility': 'visible'}]}]
    return {'style': 'live_action_feature', 'imageModel': 'dola-seedream-5-0-pro', 'sourceVideoId': 'vid-1',
            'sourceVerification': {'method': 'one_pass_production'},
            'productionAssets': [{'id': 'ann', 'kind': 'character'}, {'id': 'corridor', 'kind': 'environment'},
                                 {'id': 'lift-door', 'kind': 'prop'}, {'id': 'papers', 'kind': 'prop'}],
            'nodes': [
                {'id': 'character:ann', 'data': {'kind': 'character', 'character': {
                    'key': 'ann', 'source_asset_id': 'ann', 'name': 'Ann', 'kind': 'character',
                    'states': [{'key': 'uniform', 'wardrobe': 'Maid uniform.'}]},
                    'identity': plate('ann'), 'states': {'uniform': plate('ann-uniform')}, 'activeState': 'uniform'}},
                {'id': 'environment:corridor', 'data': {'kind': 'environment', 'environment': corridor,
                                                        'plate': plate('corridor'),
                                                        **({'reverse': plate('corridor-reverse')} if reverse else {})}},
                {'id': 'asset:lift-door', 'data': {'kind': 'asset', 'asset': {
                    'key': 'lift-door', 'source_asset_id': 'lift-door', 'name': 'Lift door', 'kind': 'prop',
                    'description': 'Brass lift door.', 'fixture_of': ['corridor']}, 'plate': {}}},
                {'id': 'asset:papers', 'data': {'kind': 'asset', 'asset': {
                    'key': 'papers', 'source_asset_id': 'papers', 'name': 'Papers', 'kind': 'prop',
                    'description': 'A contract.'}, 'plate': plate('papers')}},
                {'id': 'vid:c1', 'data': {'kind': 'video', 'prompt': ''}},
                {'id': 'seq:c1', 'data': {'kind': 'sequence', 'sequence': {'key': 'c1', 'environment_key': 'corridor'},
                                          'shots': shots}}]}


def test_a_set_with_two_views_binds_both_and_its_lift_door_shares_the_master_image():
    p = pack.build(board(), 'film', 'c1')
    m = p['materials']
    assert set(m) == {'ann:uniform', 'corridor', 'corridor:reverse', 'lift-door', 'papers'} and p['ready']
    assert m['corridor']['view'] == 'master: ' + VIEWS['master']
    assert (m['corridor:reverse']['state_key'], m['corridor:reverse']['reference_url'], m['corridor:reverse']['view']) == (
        'reverse', 'https://r2.test/corridor-reverse', 'reverse: ' + VIEWS['reverse'])
    assert (m['lift-door']['reference_url'], m['lift-door']['fixture_of']) == ('https://r2.test/corridor', 'corridor')
    assert m['papers']['reference_url'] == 'https://r2.test/papers' and 'fixture_of' not in m['papers']
    anchor = p['scene_anchors'][p['shots'][0]['continuity_scope']]
    assert anchor['environment_materials'] == ['corridor'] and list(anchor['layout_locks']) == ['corridor']


def test_a_missing_reverse_view_blocks_like_any_missing_material_and_old_sets_stay_one_plate():
    p = pack.build(board(reverse=False), 'film', 'c1')
    assert not p['ready'] and {'asset': 'corridor', 'code': 'missing_material'} in [
        {k: i.get(k) for k in ('asset', 'code')} for i in p['issues']]
    old = pack.build(board(reverse=False, views=False), 'film', 'c1')
    assert 'corridor:reverse' not in old['materials'] and 'view' not in old['materials']['corridor']


def test_the_writer_gets_one_location_in_two_views_and_the_bindings_check():
    b = board()
    p = pack.build(b, 'film', 'c1')
    body = production_run.writing_body(b, 'film', p, [])
    env = body.environment
    assert (env['ref_label'], env['name'], env['view']) == ('@image2', 'Hotel corridor — master view', 'master: ' + VIEWS['master'])
    by_id = {(r['id'], r.get('state_key')): r for r in body.reference_assets}
    assert by_id[('corridor', 'reverse')]['ref_label'] == '@image3'
    assert by_id[('corridor', 'reverse')]['name'] == 'Hotel corridor — reverse view'
    assert by_id[('lift-door', None)]['ref_label'] == '@image2'          # pictured by the master view
    refs = [*body.characters, env, *body.reference_assets]
    assert pack.check_bindings(p, refs) == []
    slots, issues = prompt_coverage.reference_slots(refs)
    assert issues == [] and [s['ref_label'] for s in slots] == ['@image1', '@image2', '@image3', '@image4']
    ask = prompt_writer._payload({'key': 'c1', 'label': 'CLIP 1'}, deepcopy(b['nodes'][-1]['data']['shots']),
                                 [(0, 3), (3, 6)], 6, characters=body.characters, environment=env, look='live_action_feature',
                                 aspect_ratio='1:1', previous_state='', unsafe={}, school_age=False,
                                 reference_assets=body.reference_assets)
    tags = {r['tag']: r for r in ask['references']}
    assert tags['@image3']['kind'] == 'location' and tags['@image3']['view'] == 'reverse: ' + VIEWS['reverse']
    assert tags['@image2']['kind'] == 'asset_atlas'                    # the corridor and its built-in door
    assert [r.get('set_view') for r in ask['shots']] == ['master', 'reverse']


def test_a_reverse_view_beside_its_master_is_no_duplicate_but_identity_and_costume_still_are():
    views = [{'ref_label': '@image1', 'ref_url': 'u1', 'id': 'corridor'},
             {'ref_label': '@image2', 'ref_url': 'u2', 'id': 'corridor', 'state_key': 'reverse'}]
    assert prompt_coverage.reference_slots(views)[1] == []
    assert prompt_coverage.reference_slots(list(reversed(views)))[1] == []
    costume = [{'ref_label': '@image1', 'ref_url': 'u1', 'id': 'ann'},
               {'ref_label': '@image2', 'ref_url': 'u2', 'id': 'ann', 'state_key': 'wet'}]
    assert prompt_coverage.reference_slots(costume)[1]


def test_the_reverse_is_drawn_after_the_master_and_the_built_in_door_gets_no_sheet():
    b = board(reverse=False)
    specs = production_run.material_specs(b, [pack.build(b, 'film', 'c1')])
    assert 'environment:corridor:reverse' in specs and 'asset:lift-door:plate' not in specs
    assert specs['environment:corridor:reverse']['deps'] == ['environment:corridor:plate']
    from flowboard.services.production_progress import make_plan
    groups = {m['key']: m['group'] for m in make_plan(specs, [])['materials']}
    assert (groups['environment:corridor:plate'], groups['environment:corridor:reverse']) == ('masters', 'variants')


@pytest.fixture
def frames(tmp_path, monkeypatch):
    import flowboard.config as config
    work = tmp_path / 'storage' / 'video_analysis' / 'vid-1' / 'frames'
    work.mkdir(parents=True)
    for n in (1, 2):
        for k in (1, 2, 3):
            Image.new('RGB', (160, 90), (n, k, 0)).save(work / f'shot{n:03d}_{k}.jpg')
    monkeypatch.setattr(config, 'STORAGE_DIR', tmp_path / 'storage')
    monkeypatch.setattr(set_anchor, '_captioned', lambda board: set())
    monkeypatch.setattr(set_anchor, 'publish_stable', lambda path: 'https://r2.test/' + path.name)


def test_each_view_is_anchored_on_the_source_frames_that_look_its_way(frames):
    b = board(reverse=False)
    for shot in b['nodes'][-1]['data']['shots']:
        shot['asset_presence'] = [p for p in shot['asset_presence'] if p['asset_id'] != 'ann']
    b['nodes'][-1]['data']['shots'][1]['asset_presence'] = [{'asset_id': 'ann', 'visibility': 'visible'}]
    assert set_anchor.candidate_shots(b, 'corridor', 'master') == [(50, 1)]
    assert set_anchor.candidate_shots(b, 'corridor', 'reverse') == []            # over a shoulder: below the plate bar
    assert set_anchor.view_candidates(b, 'corridor', 'reverse') == [(8, 2)]      # ...but a reverse anchor
    specs = production_run.material_specs(b, [pack.build(b, 'film', 'c1')])
    master = production_run.material_payload(specs['environment:corridor:plate'], [], b)
    assert master['reference_urls'] == ['https://r2.test/shot001_2.jpg']
    for text in ('MASTER VIEW', VIEWS['master'], 'LANDMARK STATES: focal lift open, its cabin lit',
                 'BUILT INTO THIS SET', 'Lift door — Brass lift door.', 'LOCATION ANCHOR FRAMES'):
        assert text in master['prompt']
    ready = {'reference_url': 'https://r2.test/corridor', 'media_id': '', 'asset_id': 'corridor', 'name': 'Hotel corridor'}
    reverse = production_run.material_payload(specs['environment:corridor:reverse'], [ready], b)
    assert reverse['reference_urls'] == ['https://r2.test/corridor', 'https://r2.test/shot002_2.jpg']
    assert "@image1 is this location's approved master view" in reverse['prompt'] and VIEWS['reverse'] in reverse['prompt']
    assert 'The later images are frames of the source film looking this way' in reverse['prompt']
    assert 'Looking down the lift bank.' not in reverse['prompt'] and 'LOCATION ANCHOR FRAMES' not in reverse['prompt']
    assert 'is behind this camera and out of view' in reverse['prompt']
    # The view sets the direction for the master too: the design's own camera would compete with it.
    assert 'Looking down the lift bank.' not in master['prompt'] and 'Lifts recede to the arch.' not in master['prompt']


def test_the_frames_the_staging_chose_for_a_view_anchor_it_first(frames):
    b = board(reverse=False)
    shots = b['nodes'][-1]['data']['shots']
    shots.append({**deepcopy(shots[1]), 'n': 3, 'source_shot': 3, 'framing': 'MS'})
    import flowboard.config as config
    work = config.STORAGE_DIR / 'video_analysis' / 'vid-1' / 'frames'
    for k in (1, 2, 3):
        Image.new('RGB', (160, 90), (3, k, 0)).save(work / f'shot003_{k}.jpg')
    env = b['nodes'][1]['data']['environment']
    assert set_anchor.anchor_urls(b, env, view='reverse') == ['https://r2.test/shot003_2.jpg', 'https://r2.test/shot002_2.jpg']
    env['views']['reverse_shots'] = [2, 1]          # 1 looks the master's way: never a reverse anchor
    assert set_anchor.anchor_urls(b, env, view='reverse') == ['https://r2.test/shot002_2.jpg', 'https://r2.test/shot003_2.jpg']


def test_a_donghua_reverse_names_the_master_after_the_bundled_style_image(frames):
    from flowboard.services import film_styles
    b = board(reverse=False)
    b['style'] = film_styles.KEY
    specs = production_run.material_specs(b, [pack.build(b, 'film', 'c1')])
    ready = {'reference_url': 'https://r2.test/corridor', 'media_id': '', 'asset_id': 'corridor', 'name': 'Hotel corridor'}
    reverse = production_run.material_payload(specs['environment:corridor:reverse'], [ready], b)
    assert "@image2 is this location's approved master view" in reverse['prompt']


def test_a_generated_reverse_does_not_change_the_run_inputs_and_raccord_sees_both_views():
    before = board(reverse=False)
    after = deepcopy(before)
    after['nodes'][1]['data']['reverse'] = {**plate('corridor-reverse'), 'prompt': 'P', 'runtimePrompt': 'P',
                                            'baseMaterialPrompt': ''}
    assert production_run.input_version(before) == production_run.input_version(after)
    scene = next(iter(raccord.scene_inputs(board(), 'film').values()))
    assert scene['environment_references'] == {'corridor': 'https://r2.test/corridor'}
    assert scene['environment_views'] == {'corridor': 'https://r2.test/corridor-reverse'}
    assert 'environment_views' not in next(iter(raccord.scene_inputs(board(reverse=False), 'film').values()))
