"""Owner set restyles: a new look per place, never a new layout (like target casting)."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from flowboard.services import avis_text, production_run, set_restyle
from tests.test_set_views import board

CAST = {'environments': [{'key': 'corridor', 'source_asset_id': 'corridor', 'name': 'Hotel corridor',
                          'description': 'Cream marble lift corridor.'},
                         {'key': 'kitchen', 'source_asset_id': 'kitchen', 'name': 'Kitchen', 'description': 'Steel kitchen.'}]}
LOOK = 'Dark green marble walls, walnut panels and blackened brass fixtures; warm, low evening light.'


def answer(looks):
    return SimpleNamespace(text=json.dumps({'looks': looks}), prompt_tokens=1, completion_tokens=1)


def test_a_restyle_resolves_once_falls_back_when_declined_and_keeps_the_source_description(tmp_path, monkeypatch):
    calls = []

    async def complete(model, *a, **kw):
        calls.append(model)
        if model == set_restyle.MODEL:
            raise avis_text.AvisContentRefusal('declined')
        return answer([{'asset_id': 'corridor', 'target_look': LOOK}])

    monkeypatch.setattr(avis_text, 'complete', complete)
    receipt = asyncio.run(set_restyle.resolve('make the hotel darker', CAST, tmp_path))
    assert asyncio.run(set_restyle.resolve('make the hotel darker', CAST, tmp_path)) == receipt
    assert calls == [set_restyle.MODEL, set_restyle.FALLBACK]
    out = set_restyle.apply_cast(CAST, receipt)
    corridor, kitchen = out['environments']
    assert corridor['target_look'] == LOOK and corridor['source_description']['description'] == 'Cream marble lift corridor.'
    assert 'target_look' not in kitchen and CAST['environments'][0].get('target_look') is None


def test_an_unknown_place_is_refused(tmp_path, monkeypatch):
    async def complete(model, *a, **kw):
        return answer([{'asset_id': 'ballroom', 'target_look': LOOK}])

    monkeypatch.setattr(avis_text, 'complete', complete)
    with pytest.raises(ValueError, match='existing environments'):
        asyncio.run(set_restyle.resolve('ballroom gold', CAST, tmp_path))


def test_the_set_views_are_drawn_with_the_look_and_the_writer_is_told(monkeypatch):
    from flowboard.services import set_anchor, shot_package as pack, prompt_writer
    from copy import deepcopy
    b = board(reverse=False)
    b['nodes'][1]['data']['environment']['target_look'] = LOOK
    monkeypatch.setattr(set_anchor, 'anchor_urls', lambda *a, **kw: [])
    specs = production_run.material_specs(b, [pack.build(b, 'film', 'c1')])
    master = production_run.material_payload(specs['environment:corridor:plate'], [], b)
    assert 'TARGET SET LOOK' in master['prompt'] and LOOK in master['prompt']
    b['nodes'][1]['data']['reverse'] = {'referenceUrl': 'https://r2.test/corridor-reverse', 'mediaId': 'm', 'prompt': ''}
    p = pack.build(b, 'film', 'c1')
    assert p['materials']['corridor']['description'].startswith('Approved target look: ' + LOOK)
    body = production_run.writing_body(b, 'film', p, [])
    ask = prompt_writer._payload({'key': 'c1'}, deepcopy(b['nodes'][-1]['data']['shots']), [(0, 3), (3, 6)], 6,
                                 characters=body.characters, environment=body.environment, look='live_action_feature',
                                 aspect_ratio='1:1', previous_state='', unsafe={}, school_age=False,
                                 reference_assets=body.reference_assets)
    looks = [r.get('target_look') or [x.get('target_look') for x in r.get('asset_bindings', [])] for r in ask['references']]
    assert LOOK in json.dumps(looks)


def test_old_colour_words_follow_the_restyle_in_the_writers_copy_only():
    shots = [{'environment_key': 'corridor', 'action': ['He opens the gold-framed lift door'],
              'asset_presence': [{'asset_id': 'door', 'state': 'Gold frame; cream marble panel'}]},
             {'environment_key': 'kitchen', 'action': ['Gold pan']}]
    envs = [{'source_asset_id': 'corridor', 'restyle_terms': {'gold': 'matte brass', 'cream marble': 'deep green marble'}}]
    out = set_restyle.project_shots(shots, envs)
    assert out[0]['action'] == ['He opens the matte brass-framed lift door']
    assert out[0]['asset_presence'][0]['state'] == 'matte brass frame; deep green marble panel'
    assert out[1] is shots[1] and shots[0]['action'] == ['He opens the gold-framed lift door']


def test_a_white_model_reskin_writes_no_positions_or_backgrounds_and_says_why():
    from flowboard.services import prompt_writer
    shots = [{'background': 'cream walls', 'staging': 'She stands outside the lift.', 'framing_note': 'left',
              'action': ['She waits.']}]
    view = prompt_writer.white_model_view(shots)
    assert view[0]['background'] == view[0]['staging'] == view[0]['framing_note'] == ''
    assert view[0]['action'] == ['She waits.'] and shots[0]['staging'] == 'She stands outside the lift.'
    assert '@video1' in prompt_writer.WHITE_MODEL_POLICY


def test_a_state_sheet_states_its_condition_first():
    from flowboard.services import film_styles
    item = {'key': 'w', 'kind': 'character', 'name': 'Woman', 'states': [{'key': 'wet', 'label': 'Wet uniform',
            'wardrobe': 'The uniform visibly wet and stained.'}]}
    prompt = film_styles.sheet_prompt('character', item, state=item['states'][0], has_reference=True, style='live_action_feature')
    assert prompt.startswith('SELECTED STATE — THIS SHEET SHOWS EXACTLY THIS\nWet uniform: The uniform visibly wet and stained.')


def test_the_reskin_package_copy_carries_no_layout_text():
    from flowboard.services import prompt_writer
    package = {'shots': [{'background': 'sofa', 'staging': 'outside', 'composition': 'left', 'action': ['waits'],
                          'raccord': {'direction': 'his left arm on the door', 'set_layouts': [{'anchors': 'lifts at left'}],
                                      'scene_rule': 'one corridor'}}],
               'scene_anchors': {'s': {'source_staging': 'lift layout', 'sheet_geometry': [{'anchors': 'x'}], 'policy': 'keep'}}}
    out = prompt_writer.white_model_package(package)
    assert out['shots'][0] == {'background': '', 'staging': '', 'composition': '', 'action': ['waits'],
                               'raccord': {'direction': '', 'set_layouts': [], 'scene_rule': 'one corridor'}}
    assert out['scene_anchors']['s'] == {'source_staging': '', 'sheet_geometry': [], 'policy': 'keep'}
    assert package['shots'][0]['background'] == 'sofa' and package['shots'][0]['raccord']['direction'].startswith('his')


def test_a_reskin_prompt_is_not_asked_to_account_for_an_offscreen_prop():
    from flowboard.services import prompt_coverage
    rows = [{'shot': 1, 'action': ['He reads.']}]
    shots = [{'asset_presence': [{'asset_id': 'booklet', 'visibility': 'offscreen', 'state': 'open on the podium'},
                                 {'asset_id': 'man', 'visibility': 'visible'}]}]
    ids = lambda **kw: [r['id'] for r in prompt_coverage.build_requirements(rows, shots, [], **kw) if r['kind'] == 'presence']
    assert ids() == ['shot:1:asset:booklet:1', 'shot:1:asset:man:2']
    assert ids(skip_offscreen=True) == ['shot:1:asset:man:2']
