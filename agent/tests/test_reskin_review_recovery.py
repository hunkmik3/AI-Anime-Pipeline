from copy import deepcopy
import json

import httpx
import pytest

from flowboard.services import reskin, reskin_source_review as review, production_manifest as pm
from tests.test_reskin import fixture, observations, packages, project


def setup(monkeypatch, tmp_path):
    b=fixture();source=tmp_path/'source.mp4';source.write_bytes(b'source')
    monkeypatch.setattr(reskin,'source_path',lambda _:source)
    b['reskinSourceReview']={};b['sourceMediaSha256']=reskin.sha256(source)
    rows=[{**deepcopy(s),'id':f'c1:{i+1}'} for i,s in enumerate(b['nodes'][-1]['data']['shots'])]
    key=pm.digest({'source_sha':b['sourceMediaSha256'],'shots':rows,'catalog':b['productionAssets'],
                   'system':review.SYSTEM,'model':review.MODEL})
    root=tmp_path/'reskin-source-review'/key;root.mkdir(parents=True)
    return b,rows,root/'receipt.json'


@pytest.mark.asyncio
async def test_missing_summary_keeps_observations_without_fabricated_approval(monkeypatch,tmp_path):
    b,rows,journal=setup(monkeypatch,tmp_path)
    answer=observations(rows);answer.pop('limitations');answer.pop('findings')
    journal.write_text(json.dumps({'state':'completed','answer':answer}))
    before=journal.read_bytes()
    with pytest.raises(review.ReviewIncomplete) as caught:
        await review.inspect(b,tmp_path)
    report=caught.value.report
    assert {k:report['counts'][k] for k in ('received','validated','total')}=={'received':1,'validated':0,'total':1}
    assert report['counts']['blocked_clips']==['c1'] and report['status']=='needs_review'
    assert report['receipts'][0]['observations']==answer
    assert report['errors'][0]['kind']=='invalid_response'
    assert journal.read_bytes()==before
    b['reskinSourceReview']=report
    assert reskin.readiness(b,packages(b))


@pytest.mark.asyncio
async def test_partial_report_is_saved_on_project_even_when_review_fails(monkeypatch,tmp_path):
    from flowboard.routes import video_analysis
    from flowboard.db import get_session
    from flowboard.db.models import AutomationProject
    b,_,journal=setup(monkeypatch,tmp_path)
    journal.write_text(json.dumps({'state':'failed_or_unknown','error':'ReadTimeout'}))
    monkeypatch.setattr(video_analysis,'_work_dir',lambda _:tmp_path)
    pid=project(b)
    with pytest.raises(review.ReviewIncomplete,match='thời gian chờ'):
        await review.review_project(pid,{'input_digest':pm.digest(reskin.source_contract(b))})
    with get_session() as s:
        saved=s.get(AutomationProject,pid).board
    assert saved['nodes']==b['nodes']
    assert saved['reskinSourceReview']['errors'][0]['clip']=='c1'
    assert reskin.readiness(saved,packages(saved))


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['bad_request','refusal','timeout'])
async def test_provider_failure_diagnostics_survive_without_resubmission(monkeypatch,tmp_path,failure):
    from flowboard.services.flowstudio import r2
    b,_,journal=setup(monkeypatch,tmp_path)
    async def command(*args):args[-1].write_bytes(b'excerpt')
    monkeypatch.setattr(review.media,'command',command)
    monkeypatch.setattr(r2,'upload_file',lambda _: 'https://test/excerpt.mp4')
    monkeypatch.setenv('ATRIUM_CLIENT_ID','test-client-id')
    monkeypatch.setenv('ATRIUM_CLIENT_SECRET','test-secret-value')
    calls=[]
    def handler(request):
        if request.method=='GET':return httpx.Response(200,content=b'excerpt')
        calls.append(request)
        if failure=='timeout':raise httpx.ReadTimeout('Timed out',request=request)
        detail='SAFETY content policy violation' if failure=='refusal' else 'invalid mimeType test-secret-value https://private/ref?token=secret'
        return httpx.Response(400,json={'error':detail},headers={'x-request-id':'provider-request-123'})
    client=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:client(transport=httpx.MockTransport(handler),**kw))
    for _ in range(2):
        with pytest.raises(review.ReviewIncomplete):await review.inspect(b,tmp_path)
    record=json.loads(journal.read_text())
    assert len(calls)==1
    assert record['state']=='failed_or_unknown'
    if failure=='timeout':assert record['failure']['kind']=='transport_unknown'
    else:
        assert record['http_status']==400 and record['request_id']=='provider-request-123'
        assert 'test-secret-value' not in journal.read_text()
        assert 'https://private/' not in journal.read_text()
        if failure=='refusal':assert record['failure']['kind']=='content_refused'


def test_review_progress_separates_received_validated_and_source_readiness():
    from flowboard.services import production_progress as progress
    from flowboard.db import get_session
    from flowboard.db.models import VideoAnalysis
    with get_session() as s:
        row=VideoAnalysis(name='source',status='failed',options={'auto_production':{'phase_history':{
            'reskin_review':{'status':'failed'}}}})
        s.add(row);s.commit();vid=row.id
    progress.record_source_review(vid,{'status':'needs_review','counts':{'received':24,'validated':23,'total':26},
        'errors':[{'clip':'clip-04','message':'HTTP 400'}],'findings':[]})
    with get_session() as s:
        rows=progress.source_stages(s.get(VideoAnalysis,vid))
    assert len(rows)==1 and rows[0]['status']=='blocked' and rows[0]['done']==23
    assert '24/26' in rows[0]['note'] and 'clip-04' in rows[0]['note']


@pytest.mark.asyncio
async def test_source_resume_starts_at_saved_review_without_rebuilding_profiles(monkeypatch,tmp_path):
    import uuid
    from flowboard.services import source_film, production_run
    from flowboard.services.video_analyzer import one_pass_film, board as builder
    from flowboard.routes import video_analysis
    from flowboard.db import get_session
    from flowboard.db.models import VideoAnalysis, AutomationProject
    b,_,journal=setup(monkeypatch,tmp_path)
    journal.write_text(json.dumps({'state':'failed_or_unknown','error':'ReadTimeout'}))
    pid=project(b)
    with get_session() as s:
        v=VideoAnalysis(id=uuid.UUID(b['sourceVideoId']),name='source',automation_project_id=pid,status='failed',
            analysis={'preserved':'full source'},cast={'preserved':'all paid designs'},
            options={'auto_production':{'board_ready':True,'settings':source_film.FilmOptions(pipeline='reskin_v1').model_dump()}})
        v.options={**v.options,'auto_production':{**v.options['auto_production'],'source_board_input_digest':source_film.board_source_digest(v)}}
        s.add(v);s.commit();vid=v.id
    def forbidden(*a,**kw):raise AssertionError('Upstream preparation or production must not run')
    monkeypatch.setattr(one_pass_film,'prepare',forbidden)
    monkeypatch.setattr(builder,'build_board',forbidden)
    monkeypatch.setattr(video_analysis,'_run_design',forbidden)
    monkeypatch.setattr(production_run,'start',forbidden)
    monkeypatch.setattr(video_analysis,'_work_dir',lambda _:tmp_path)
    with pytest.raises(review.ReviewIncomplete):await source_film.execute(vid)
    with get_session() as s:
        v=s.get(VideoAnalysis,vid);p=s.get(AutomationProject,pid)
        assert v.cast=={'preserved':'all paid designs'}
        assert v.analysis=={'preserved':'full source'}
        assert p.board['reskinSourceReview']['counts']['total']==1
        options=deepcopy(v.options);options['auto_production']['generation_scope']={'source_end_seconds':9}
        v.options=options;s.add(v);s.commit()
    with pytest.raises(ValueError,match='phạm vi đã đổi'):await source_film.execute(vid)
