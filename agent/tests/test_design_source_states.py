from copy import deepcopy

import pytest

from flowboard.services.video_analyzer import design, production


def source():
    kinds = {'lead': 'character', 'box': 'prop', 'crowd': 'background_group', 'room': 'environment'}
    assets = [{'id': key, 'kind': kind, 'name': key, 'description': key,
               'evidence_ids': [], 'member_ids': [], 'depends_on_asset_ids': []}
              for key, kind in kinds.items()]
    rows = {}
    for number in (1, 2):
        presence = [{'asset_id': key, 'visibility': 'visible' if number == 1 else 'offscreen',
                     'position': 'left', 'state': 'Unspecified', 'evidence_ids': [], 'contains_ids': []}
                    for key in kinds]
        box = next(p for p in presence if p['asset_id'] == 'box')
        box.update(state='Lid state is unknown.' if number == 1 else 'Still held offscreen; lid remains unknown.',
                   holder_id='lead', hand='right')
        rows[str(number)] = {'scene_id': 'home', 'asset_presence': presence, 'evidence_ids': []}
    return {'shots': [{'shot': n, 'frames': []} for n in (1, 2)],
            'scene_inventory': {'assets': assets, 'shots': rows, 'scenes': []}}


def test_reference_assets_receive_exact_source_states_without_rewriting_saved_designs():
    analysis = source()
    existing = {'props': [{'key': 'box', 'source_asset_id': 'box',
                          'design': {'states': ['An open-box reference view.']},
                          'plate': {'reference_url': 'https://example.invalid/saved-sheet'}}]}
    before_analysis, before_cast = deepcopy(analysis), deepcopy(existing)
    cast = production.attach_inventory(analysis, existing)
    for kind in ('prop', 'background_group', 'environment'):
        entry = cast[production.BUCKETS[kind]][0]
        expected = [{'shot': int(number), 'scene_id': row['scene_id'], **presence}
                    for number, row in analysis['scene_inventory']['shots'].items()
                    for presence in row['asset_presence'] if presence['asset_id'] == entry['source_asset_id']]
        assert entry['observed_states'] == expected
        assert entry['observed_states'][1]['visibility'] == 'offscreen'
    box = cast['props'][0]
    assert box['design'] == existing['props'][0]['design']
    assert box['plate'] == existing['props'][0]['plate']
    assert box['observed_states'][0]['state'] == 'Lid state is unknown.'
    box['observed_states'][0]['state'] = 'Changed only in the returned copy'
    assert analysis == before_analysis and existing == before_cast


@pytest.mark.parametrize('kind', ['prop', 'background_group', 'environment'])
@pytest.mark.asyncio
async def test_design_request_separates_source_state_from_reference_variants(kind, tmp_path, monkeypatch):
    entry = production.attach_inventory(source(), {})[production.BUCKETS[kind]][0]
    original = deepcopy(entry)
    captured = {}

    async def capture(system, known, frames, stats, **kwargs):
        captured.update(system=system, known=known)
        return {'description': 'Invariant design', 'reference_variants': []}

    monkeypatch.setattr(design, '_design', capture)
    fn = design.design_environment if kind == 'environment' else design.design_asset
    await fn(entry, tmp_path, {'visual_style': 'current project style'}, None)
    assert captured['known']['observed_states'] == original['observed_states']
    assert captured['known']['target_style'] == 'current project style'
    if kind != 'environment':
        assert 'observed_states' not in captured['known']['asset']
        assert '"reference_variants":[]' in captured['system']
        assert '"states":[]' not in captured['system']
    assert 'Do not reinterpret an unknown source' in captured['system']
    assert 'variant never overrides an observed shot state' in captured['system']
    assert entry == original
