"""One room per location: each shot's own background and the location's layout reach the writer."""
from copy import deepcopy

import pytest

from flowboard.services import cinematic_prompt, prompt_writer, shot_package as pack
from flowboard.services.video_analyzer import board as board_mod

DESIGN = {'architecture': 'Long vaulted bathroom; tall arched windows run along the left side.',
          'depth': 'Foreground marble steps; midground oval tub; the mirrored vanity backs it on the right.',
          'camera': 'Entrance-side view with windows frame left and the vanity frame right.',
          'light': 'Daylight enters from the left-hand windows; warm sconces by the mirrors.'}


def test_the_geography_is_the_design_layout_depth_and_reference_view():
    geo = pack.environment_geography(DESIGN)
    assert geo.startswith('Layout: Long vaulted bathroom') and 'Depth: Foreground marble steps' in geo
    assert 'Reference view: Entrance-side view' in geo and 'Daylight' not in geo
    assert pack.environment_geography({'lock': 'Door on the north wall.'}) == 'Door on the north wall.'
    assert pack.environment_geography({}) == ''


def test_a_board_shot_keeps_what_the_source_shows_behind_the_people():
    shot = {'shot': 7, 'start': 1.0, 'end': 3.0, 'source': {
        'shot_size': 'MS', 'setting': 'tall arched windows with city view, gilded mirror, daylight'}}
    out = board_mod.board_shot(1, shot, {'framing': 'MS'}, {})
    assert out['background'] == 'tall arched windows with city view, gilded mirror, daylight'
    assert board_mod.board_shot(1, shot, {'background': 'from the adaptation'}, {})['background'] == 'from the adaptation'


def environment_board():
    board = {'style': 'cg3d', 'productionAssets': [{'id': 'bath', 'kind': 'environment'}], 'nodes': [
        {'id': 'environment:bath', 'data': {'kind': 'environment', 'environment': {
            'key': 'bath', 'id': 'bath', 'name': 'Bathroom', 'kind': 'environment', 'design': DESIGN},
            'plate': {'referenceUrl': 'https://test/bath', 'mediaId': 'm-bath', 'prompt': 'STYLE:\nFilm.'}}},
        {'id': 'vid:c1', 'data': {'kind': 'video', 'prompt': ''}},
        {'id': 'seq:c1', 'data': {'kind': 'sequence', 'sequence': {'key': 'c1', 'environment_key': 'bath'}, 'shots': [
            {'n': 1, 'source_shot': 1, 'scene_id': 'bath', 'duration_s': 3, 'environment_key': 'bath',
             'background': 'arched windows and a chandelier', 'action': ['She waits.'], 'asset_presence': []}]}}]}
    return board


def test_the_package_carries_the_room_layout_light_and_each_shot_background():
    p = pack.build(environment_board(), 'film', 'c1')
    bath = p['materials']['bath']
    assert bath['layout_lock'] == pack.environment_geography(DESIGN)
    assert bath['lighting'] == DESIGN['light']
    scope = p['shots'][0]['continuity_scope']
    assert p['scene_anchors'][scope]['layout_locks']['bath'] == pack.environment_geography(DESIGN)
    assert p['shots'][0]['background'] == 'arched windows and a chandelier'
    assert '"background": "arched windows and a chandelier"' in pack.keyframe(p, 0)['prompt']


def test_the_writer_is_given_each_shot_background_and_the_image_as_the_location_layout():
    shots = [{'framing': 'MS', 'camera': 'static', 'background': 'arched windows and a chandelier',
              'action': ['She waits.'], 'dialogue': [], 'duration_s': 3}]
    environment = {'key': 'bath', 'source_asset_id': 'bath', 'name': 'Bathroom', 'design': DESIGN,
                   'ref_label': '@image1', 'ref_url': 'https://test/bath'}
    ask = prompt_writer._payload({'key': 'c1', 'label': 'CLIP 1'}, shots, [(0, 3)], 3, characters=[],
                                 environment=environment, look='realistic', aspect_ratio='16:9',
                                 previous_state='', unsafe={}, school_age=False)
    assert ask['shots'][0]['background'] == 'arched windows and a chandelier'
    location = next(r for r in ask['references'] if r['kind'] == 'location')
    # The attached image is the layout; the text that drew it is not a second lock to reconcile.
    assert 'geography' not in location and location['light'] == DESIGN['light']


def test_the_source_background_is_locked_into_each_shot_after_its_camera():
    prompt = ('# SHOT 1 | 00:00–00:03.000\n**Duration:** 3 seconds\n**Framing:** wide\n**Camera:** pan\n'
              '**Background:** a garden (made up)\nShe waits by the windows.\n**End state:** still.\n'
              '# SHOT 2 | 00:03.000–00:05.000\n**Duration:** 2 seconds\n**Framing:** close\n**Camera:** static\n'
              'Her face.\n**End state:** still.\n')
    shots = [{'framing': 'WS', 'framing_note': '', 'camera': 'static', 'background': 'arched windows, chandelier'},
             {'framing': 'CU', 'framing_note': '', 'camera': 'static', 'background': ''}]
    out = cinematic_prompt.serialize_source_locks(prompt, shots, [(0, 3), (3, 5)])
    first, second = out.split('# SHOT 2')
    assert '**Camera:** static\n**Background:** arched windows, chandelier\n' in first
    assert 'garden' not in first and '**Background:**' not in second


def test_older_locks_without_a_background_or_staging_still_validate():
    from flowboard.services.video_analyzer import one_pass_film as film
    shot = {'source_shot': 1, 'duration_s': 2, 'framing': 'CU', 'camera': 'static', 'framing_note': '',
            'action': [], 'dialogue': [], 'character_states': {}}
    locked = {k: deepcopy(shot.get(k, [])) for k in film.LOCK_FIELDS if k not in ('background', 'staging')}
    report = {'locked_shots': {'1': {**locked, 'duration_s': 2}}}
    assert film.validate_locked([shot], report) == []


def test_an_observed_action_loses_its_words_about_burned_in_captions():
    from flowboard.services.video_analyzer import one_pass_film as film
    assert film.without_captions('lips part while caption shows, then smiles broadly') == 'lips part, then smiles broadly'
    assert film.without_captions('woman listens without speaking; caption plays over her') == 'woman listens without speaking'
    assert film.without_captions('A subtitle appears') == ''
    assert film.without_captions('She folds the towel.') == 'She folds the towel.'


def test_a_layout_read_from_the_approved_image_replaces_the_design_text_lock():
    board = environment_board()
    plain = pack.build(deepcopy(board), 'film', 'c1')
    scope = plain['shots'][0]['continuity_scope']
    assert plain['scene_anchors'][scope]['layout_locks'] == {'bath': pack.environment_geography(DESIGN)}
    layout = {'asset_id': 'bath', 'reference_url': 'https://test/bath', 'anchors': 'Windows left, vanity right.',
              'unknowns': 'Reverse wall.'}
    board['raccordPlans'] = {scope: {'scope': scope, 'version': 'v1', 'mode': 'ai', 'scene_rule': 'One bathroom.',
                                     'set_layouts': [layout], 'shots': [
        {'shot_id': plain['shots'][0]['id'], 'direction': 'She waits.', 'resolutions': [], 'depends_on_previous': False}]}}
    imaged = pack.build(board, 'film', 'c1')
    anchor = imaged['scene_anchors'][scope]
    assert anchor['sheet_geometry'] == [layout] and anchor['layout_locks'] == {}
