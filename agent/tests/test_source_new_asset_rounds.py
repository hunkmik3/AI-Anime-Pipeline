"""New source definitions survive bounded writer/checker repair rounds."""
import copy

import pytest

from tests.test_source_context_refinement import _install, _run
from tests.test_source_refinement import _case


@pytest.mark.parametrize('echo_definition', [False, True])
def test_new_definition_survives_empty_or_echoed_repair_delta(tmp_path, monkeypatch, echo_definition):
    case = _case(tmp_path)
    established = {a['id']: copy.deepcopy(a) for a in case[2]['scene_inventory']['assets']}
    rounds = {'writer': 0, 'checker': 0}
    proposed_ids = []

    def writer(data, payload):
        rounds['writer'] += 1
        if rounds['writer'] == 1:
            data['assets'] = [{'id': 'new-brooch', 'kind': 'prop', 'name': 'Observed brooch',
                'description': 'A separate small silver brooch.', 'reference_required': True,
                'evidence_ids': ['shot-1-frame-1'], 'member_ids': [], 'depends_on_asset_ids': []}]
            data['shots']['1']['asset_presence'].append({'asset_id': 'new-brooch', 'visibility': 'visible',
                'position': 'Foreground', 'state': 'Resting on the counter.',
                'contains_ids': [], 'evidence_ids': ['shot-1-frame-1']})
        else:
            definition = next(a for a in payload['draft_inventory']['assets'] if a['name'] == 'Observed brooch')
            data['assets'] = [copy.deepcopy(definition)] if echo_definition else []
            frozen = copy.deepcopy(established['market-lead'])
            frozen['name'] = 'Unexpected replacement identity'
            data['assets'].append(frozen)

    def checker(data, payload):
        rounds['checker'] += 1
        definition = next(a for a in payload['proposed_inventory']['assets'] if a['name'] == 'Observed brooch')
        proposed_ids.append(definition['id'])
        assert definition['id'] not in payload['temporal_context']['canonical_asset_ids']
        assert not any(f['code'] == 'unknown_asset' for f in payload['structural_findings'])
        if rounds['checker'] == 1:
            row = next(x for x in data['checks'] if x['shot'] == 1)
            row['status'] = 'needs_review'
            row['findings'] = ['The visual description needs one bounded repair.']

    _install(monkeypatch, writer=writer, checker=checker)
    result = _run(case)
    report = result['source_verification']
    assert report['status'] == 'verified' and not report['retryable']
    assert rounds == {'writer': 2, 'checker': 2}
    assert len(set(proposed_ids)) == 1
    new = [a for a in result['scene_inventory']['assets'] if a['id'] not in established]
    assert len(new) == 1 and new[0]['id'] == proposed_ids[0]
    assert any(p['asset_id'] == new[0]['id'] for p in result['scene_inventory']['shots']['1']['asset_presence'])
    assert next(a for a in result['scene_inventory']['assets'] if a['id'] == 'market-lead') == established['market-lead']
