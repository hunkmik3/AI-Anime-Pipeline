import asyncio
import copy

import pytest
from fastapi import HTTPException

from flowboard.db import get_session
from flowboard.db.models import VideoAnalysis
from flowboard.routes import video_analysis as routes


def _row():
    analysis={'shots':[{'shot':1,'start':0,'end':1,'source':{'action':'old'},'dialogue':'keep'}],
              'scene_inventory':{'assets':[],'scenes':[],'shots':{'1':{'asset_presence':[],'evidence_ids':['f']}}},
              'source_verification':{'status':'needs_review','findings':[{'code':'original'}]}}
    with get_session() as s:
        row=VideoAnalysis(name='Generic source',filename='source.mp4',status='analysed',analysis=analysis)
        s.add(row);s.commit();s.refresh(row);return row.id,analysis


def test_refinement_route_requires_existing_source_draft():
    with get_session() as s:
        row=VideoAnalysis(name='Not analysed');s.add(row);s.commit();s.refresh(row);key=row.id
    with pytest.raises(HTTPException) as err:asyncio.run(routes.refine_source(key,user=None))
    assert err.value.status_code==409


def test_refinement_worker_commits_saved_result_with_original_dialogue(tmp_path,monkeypatch):
    key,original=_row();video=tmp_path/'source.mp4';video.write_bytes(b'source')
    monkeypatch.setattr(routes,'_source_path',lambda _:video)
    monkeypatch.setattr(routes,'_work_dir',lambda _:tmp_path)
    async def run(source,work,analysis,**kwargs):
        assert analysis==original
        assert kwargs['only_unresolved'] is True and kwargs['selected_shots'] is None
        result=copy.deepcopy(analysis);result['shots'][0]['source']['action']='corrected'
        result['source_verification']={'status':'verified','retryable':False};return result
    monkeypatch.setattr(routes.source_refinement,'refine',run)
    asyncio.run(routes._run_source_refinement(key))
    with get_session() as s:
        row=s.get(VideoAnalysis,key)
        assert row.status=='analysed' and row.progress=={}
        assert row.analysis['shots'][0]['source']['action']=='corrected'
        assert row.analysis['shots'][0]['dialogue']=='keep'


@pytest.mark.parametrize('retryable',[False,True])
def test_failed_or_retryable_work_preserves_original_draft_for_stage_resume(tmp_path,monkeypatch,retryable):
    key,original=_row();video=tmp_path/'source.mp4';video.write_bytes(b'source')
    monkeypatch.setattr(routes,'_source_path',lambda _:video)
    monkeypatch.setattr(routes,'_work_dir',lambda _:tmp_path)
    async def run(*args,**kwargs):
        if not retryable:raise RuntimeError('Provider failed')
        return {'shots':[],'source_verification':{'status':'needs_review','retryable':True}}
    monkeypatch.setattr(routes.source_refinement,'refine',run)
    asyncio.run(routes._run_source_refinement(key))
    with get_session() as s:
        row=s.get(VideoAnalysis,key)
        assert row.status=='failed' and row.error
        assert row.analysis==original


def test_protocol_route_requires_completed_bound_refinement():
    key,_=_row()
    with pytest.raises(HTTPException) as err:
        asyncio.run(routes.recheck_source_protocol(key,user=None))
    assert err.value.status_code==409


def test_protocol_worker_uses_qa_only_and_preserves_source(tmp_path,monkeypatch):
    key,original=_row();video=tmp_path/'source.mp4';video.write_bytes(b'source')
    monkeypatch.setattr(routes,'_source_path',lambda _:video)
    monkeypatch.setattr(routes,'_work_dir',lambda _:tmp_path)
    async def check(source,work,analysis,**kwargs):
        assert analysis==original
        assert 'selected_shots' not in kwargs
        result=copy.deepcopy(analysis)
        result['source_verification']={'status':'verified','retryable':False}
        return result
    async def forbidden(*args,**kwargs):raise AssertionError('QA-only route must not rewrite')
    monkeypatch.setattr(routes.source_protocol_review,'review',check)
    monkeypatch.setattr(routes.source_refinement,'refine',forbidden)
    asyncio.run(routes._run_source_refinement(key,protocol_only=True))
    with get_session() as s:
        row=s.get(VideoAnalysis,key)
        assert row.status=='analysed'
        assert row.analysis['shots']==original['shots']
        assert row.analysis['scene_inventory']==original['scene_inventory']


@pytest.mark.parametrize('body', [None, {}, {'shots': [1]}])
def test_refine_http_keeps_legacy_no_body_and_passes_explicit_selection(client, monkeypatch, body):
    key, _ = _row()
    calls = []
    token = object()
    def worker(video_id, **kwargs):
        calls.append((video_id, kwargs))
        return token
    started = []
    monkeypatch.setattr(routes, '_run_source_refinement', worker)
    monkeypatch.setattr(routes, '_start', lambda video_id, coro, **kwargs: started.append((video_id, coro)))
    url = f'/api/automation/videos/{key}/refine'
    response = client.post(url) if body is None else client.post(url, json=body)
    assert response.status_code == 200
    assert calls == [(key, {'selected_shots': body.get('shots') if body is not None else None})]
    assert started == [(key, token)]


@pytest.mark.parametrize('body', [
    {'shots': []}, {'shots': [True]}, {'shots': [1.0]}, {'shots': ['1']},
    {'shots': [0]}, {'shots': [-1]}, {'shots': [1, 1]}, {'shot': [1]},
])
def test_invalid_selected_shots_are_rejected_before_start(client, monkeypatch, body):
    key, _ = _row()
    def forbidden(*args, **kwargs):
        raise AssertionError('Invalid selection must not start a model job')
    monkeypatch.setattr(routes, '_run_source_refinement', forbidden)
    monkeypatch.setattr(routes, '_start', forbidden)
    response = client.post(f'/api/automation/videos/{key}/refine', json=body)
    assert response.status_code == 422


def test_refine_rejects_shot_not_present_in_this_video(client, monkeypatch):
    key, original = _row()
    def forbidden(*args, **kwargs):
        raise AssertionError('Unknown shot must not start work')
    monkeypatch.setattr(routes, '_run_source_refinement', forbidden)
    response = client.post(f'/api/automation/videos/{key}/refine', json={'shots': [2]})
    assert response.status_code == 422 and '2' in response.json()['detail']
    with get_session() as session:
        row = session.get(VideoAnalysis, key)
        assert row.analysis == original and row.status == 'analysed'


@pytest.mark.parametrize('binding_changed', [False, True])
def test_selected_worker_passes_exact_scope_and_preserves_draft_on_binding_failure(tmp_path, monkeypatch, binding_changed):
    key, original = _row()
    video = tmp_path / 'source.mp4'
    video.write_bytes(b'source')
    monkeypatch.setattr(routes, '_source_path', lambda _: video)
    monkeypatch.setattr(routes, '_work_dir', lambda _: tmp_path)
    async def refine(source, work, analysis, **kwargs):
        assert analysis == original
        assert kwargs['selected_shots'] == [1]
        assert kwargs['only_unresolved'] is True
        if binding_changed:
            raise ValueError('Selected review requires unchanged source binding')
        result = copy.deepcopy(analysis)
        result['shots'][0]['source']['action'] = 'Corrected selected shot'
        result['source_verification'] = {'status': 'verified', 'retryable': False}
        return result
    monkeypatch.setattr(routes.source_refinement, 'refine', refine)
    asyncio.run(routes._run_source_refinement(key, selected_shots=[1]))
    with get_session() as session:
        row = session.get(VideoAnalysis, key)
        if binding_changed:
            assert row.status == 'failed' and 'source binding' in row.error
            assert row.analysis == original
        else:
            assert row.status == 'analysed'
            assert row.analysis['shots'][0]['source']['action'] == 'Corrected selected shot'
            assert row.analysis['shots'][0]['dialogue'] == 'keep'
