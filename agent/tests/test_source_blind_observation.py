import copy
from types import SimpleNamespace

import pytest

from flowboard.services.video_analyzer.source_blind_observation import observe, check_claims


@pytest.mark.asyncio
async def test_blind_observation_only_sees_target_pixels_and_timing():
    shots = [{'shot': 4, 'start': 3.0, 'end': 4.0, 'source': {'action': 'Incorrect draft'}}]
    evidence = [{'id': 'own', 'shot': 4}, {'id': 'neighbor', 'shot': 3}]
    response = {'observations': [{'shot': 4, 'evidence_ids': ['own'], 'people': ['face covered']} ]}
    async def stage(entry, name, system, payload, frames, *args, **kwargs):
        assert name == 'blind_observation'
        assert kwargs['model_override']
        assert payload == {'targets': [{'shot': 4, 'start': 3.0, 'end': 4.0}]}
        assert frames == [evidence[0]]
        assert 'Incorrect draft' not in str(payload)
        return response
    result = await observe(SimpleNamespace(_stage_call=stage), {}, shots, evidence, None, None, None)
    assert result['observations'] == response['observations']
    assert result['observations'] is not response['observations']
    assert result['scope'].endswith('not_verified_facts')


@pytest.mark.asyncio
@pytest.mark.parametrize('rows', [None, [], [{'shot': 4, 'evidence_ids': []}],
    [{'shot': 4, 'evidence_ids': ['foreign']}], [{'shot': 5, 'evidence_ids': ['own']}],
    [{'shot': 4, 'evidence_ids': ['own']}, {'shot': 4, 'evidence_ids': ['own']}], [None]])
async def test_blind_observation_rejects_missing_duplicate_or_foreign_coverage(rows):
    async def stage(*args, **kwargs):
        return {'observations': copy.deepcopy(rows)}
    with pytest.raises(ValueError):
        await observe(SimpleNamespace(_stage_call=stage), {},
            [{'shot': 4, 'start': 3., 'end': 4.}], [{'id': 'own', 'shot': 4}], None, None, None)


@pytest.mark.asyncio
async def test_visual_gate_reports_contradiction_even_without_prior_findings():
    async def stage(entry, name, system, payload, frames, *args, **kwargs):
        assert 'prior_findings' not in payload and 'verdict' not in payload
        assert kwargs['model_override'] and kwargs['verify']
        assert frames == [{'id': 'own', 'shot': 4}]
        return {'checks': [{'shot': 4, 'evidence_ids': ['own'],
                            'findings': ['The supposed person is a broom head.']}]}
    fake = SimpleNamespace(_stage_call=stage, _visual_source_rows=lambda x: x,
                           _finding=lambda code, message, shot: dict(code=code,message=message,shot=shot))
    result = await check_claims(fake, {}, 'visual_check', [{'shot':4}], {'shots':{},'assets':[]},
        [{'id':'own','shot':4},{'id':'foreign','shot':3}], None, None, None)
    assert len(result)==1 and result[0]['code']=='source_mismatch'
    assert result[0]['evidence_ids']==['own']


@pytest.mark.asyncio
@pytest.mark.parametrize('row', [{'shot':4,'evidence_ids':['own']},
    {'shot':4,'evidence_ids':['foreign'],'findings':[]}, {'shot':4,'evidence_ids':['own'],'findings':['']}])
async def test_visual_gate_fails_closed_on_invalid_verdict(row):
    async def stage(*args, **kwargs): return {'checks':[row]}
    fake=SimpleNamespace(_stage_call=stage,_visual_source_rows=lambda x:x)
    with pytest.raises(ValueError):
        await check_claims(fake, {}, 'visual_check', [{'shot':4}], {}, [{'id':'own','shot':4}], None,None,None)
