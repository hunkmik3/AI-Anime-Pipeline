import asyncio
import copy
import pytest
from flowboard.services.video_analyzer import source_inventory as inv, source_refinement as refine
from tests.test_source_refinement import _case, _mock


def bound_case(tmp_path):
    video, work, analysis = _case(tmp_path)
    report = analysis['source_verification']
    for e in report['evidence']:
        e['sha256'] = inv._hash_file(work / e['frame'])
    report.update(method='source_frames', inventory_digest=inv.inventory_digest(analysis['scene_inventory']))
    report['input_binding'] = inv.verification_binding(video, analysis['shots'], report['evidence'])
    return video, work, analysis


def test_first_pass_verified_sibling_is_retained_by_focused_repair(tmp_path, monkeypatch):
    case=bound_case(tmp_path)
    _,checks=_mock(monkeypatch)
    result=asyncio.run(refine.refine(*case, only_unresolved=True, strategy='focused'))
    assert [s['shot'] for s in checks[0]['proposed_source_shots']]==[1]
    assert result['source_verification']['refinement']['scope']['retained_verified_shots']==[2]
    assert result['shots'][1]==case[2]['shots'][1]


@pytest.mark.parametrize('changed', ['video','frame','description','inventory','review_prompt'])
def test_changed_inputs_cannot_retain_first_pass_approval(tmp_path, monkeypatch, changed):
    video,work,analysis=bound_case(tmp_path)
    if changed=='video': video.write_bytes(b'changed movie')
    elif changed=='frame': (work/analysis['shots'][1]['frames'][0]).write_bytes(b'changed pixels')
    elif changed=='description': analysis['shots'][1]['source']['action']='changed action'
    elif changed=='inventory': analysis['scene_inventory']['assets'][0]['description']='changed identity'
    else: monkeypatch.setattr(inv,'_VERIFY',inv._VERIFY+' changed policy')
    with pytest.raises(ValueError,match='unchanged source-bound'):
        asyncio.run(refine.refine(video,work,analysis,selected_shots=[1],strategy='focused'))
