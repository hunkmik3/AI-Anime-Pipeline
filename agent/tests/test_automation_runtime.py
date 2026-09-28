import asyncio
import uuid
from datetime import timedelta
from copy import deepcopy
import pytest
from sqlmodel import select
from flowboard.db import get_session
from flowboard.db.models import AutomationProject,AutomationJob
from flowboard.services import automation_jobs as jobs, production_manifest as pm
from flowboard.routes import automation as routes


def make_project():
    p=AutomationProject(name='Test film',board={'nodes':[{'id':'vid:c1','data':{'kind':'video','prompt':'test'}},{'id':'seq:c1','data':{'kind':'sequence','sequence':{'key':'c1'},'shots':[]}}]})
    with get_session() as s:s.add(p);s.commit();s.refresh(p);return p.id

def queue(pid,**kwargs):
    return jobs.enqueue(pid,'clip','vid:c1','',{'prompt':'test','project_id':str(pid),'sequence_key':'c1','duration_seconds':4,'reference_urls':['https://example.test/ref']},kwargs.get('key','first'))

def test_dedup_and_conflicting_request():
    pid=make_project();a=queue(pid);b=queue(pid)
    assert a['id']==b['id']
    assert queue(pid,key='different')['id']==a['id']
    with pytest.raises(ValueError):jobs.enqueue(pid,'clip','vid:c1','',{'prompt':'changed'},'first')

def test_claim_exclusive_and_resume_known_provider():
    pid=make_project();queue(pid);a=jobs.claim();assert a and jobs.claim() is None
    jobs.checkpoint(a['id'],a['lease_token'],status='running',provider_job_id='b2b:known')
    with get_session() as s:
        j=s.get(AutomationJob,a['id']);j.lease_until=jobs.now()-timedelta(seconds=1);s.add(j);s.commit()
    resumed=jobs.claim();assert resumed['provider_job_id']=='b2b:known'
    with pytest.raises(RuntimeError):jobs.checkpoint(a['id'],a['lease_token'],status='failed')

def test_ambiguous_submit_is_never_retried():
    pid=make_project();queue(pid);a=jobs.claim()
    with get_session() as s:
        j=s.get(AutomationJob,a['id']);j.status='submitting';j.lease_until=jobs.now()-timedelta(seconds=1);s.add(j);s.commit()
    assert jobs.claim() is None
    with get_session() as s:assert s.get(AutomationJob,a['id']).status=='unknown'
    assert queue(pid,key='another')['status']=='unknown'

def test_runtime_overlay_does_not_lose_edits():
    board={'nodes':[{'id':'vid:c1','data':{'kind':'video','prompt':'new edit','status':'error','error':'interrupted'}}]}
    job=AutomationJob(project_id=uuid.uuid4(),kind='clip',node_id='vid:c1',request_key='x',status='succeeded',provider_job_id='remote',result={'url':'https://video.test/v.mp4','persisted':True})
    out=jobs.overlay(board,[job]);d=out['nodes'][0]['data']
    assert d['status']=='done' and d['clipUrl'] and d['prompt']=='new edit' and 'error' not in d
    assert board['nodes'][0]['data']['status']=='error'

def test_revision_conflict_and_archive():
    from fastapi import HTTPException
    pid=make_project()
    routes.save_project(pid,routes.ProjectSave(board={'nodes':[]},expected_revision=0),None)
    with pytest.raises(HTTPException) as exc:routes.save_project(pid,routes.ProjectSave(board={'nodes':[]},expected_revision=0),None)
    assert exc.value.status_code==409
    with pytest.raises(HTTPException) as exc:routes.save_project(pid,routes.ProjectSave(board={'nodes':[]}),None)
    assert exc.value.status_code==428
    assert len(routes.production_revisions(pid,None))==1
    assert routes.production_revision(pid,1,None)['schema_version']==1


def fixture_board():
    return {'productionAssets':[{'id':'crowd','kind':'background_group'},{'id':'box','kind':'prop'}], 'nodes':[
      {'id':'seq:c1','data':{'kind':'sequence','sequence':{'key':'c1'},'shots':[
       {'source_shot':1,'scene_id':'hall','duration_s':.4,'asset_presence':[{'asset_id':'crowd','visibility':'visible'},{'asset_id':'box','visibility':'visible','holder_id':'hero','hand':'left'}]},
       {'source_shot':2,'scene_id':'hall','duration_s':.6,'asset_presence':[{'asset_id':'box','visibility':'visible','holder_id':'hero','hand':'right'}]},
       {'source_shot':3,'scene_id':'garden','duration_s':2,'asset_presence':[]},
       {'source_shot':4,'scene_id':'hall','duration_s':2,'asset_presence':[]}]}}]}

def test_crowd_survives_cut_but_not_time_scene_change():
    m=pm.build(fixture_board(),'movie');a,b,c,d=m['shots']
    assert b['end_state']['crowd']['visibility']=='not_observed'
    assert not c['start_state'] and not d['start_state']
    assert any(x['code']=='state_transition_needs_review' for x in m['issues'])
    assert len(m['shots'])==4 and a['duration_s']==.4

def test_explicit_transition_and_exit():
    b=fixture_board();shots=b['nodes'][0]['data']['shots'];shots[1]['continuity_events']=[{'type':'exit','asset_id':'crowd'},{'asset_id':'box','field':'hand','to':'right','reason':'Visible transfer during shot'}]
    m=pm.build(b);assert 'crowd' not in m['shots'][1]['end_state']
    assert not any(x['code']=='state_transition_needs_review' for x in m['issues'])


def test_current_visible_prop_does_not_inherit_unobserved_holder():
    b=fixture_board();shots=b['nodes'][0]['data']['shots']
    shots[1]['asset_presence']=[{'asset_id':'box','visibility':'visible','state':'Resting on the table.'}]
    row=pm.build(b)['shots'][1]
    assert row['start_state']['box']['holder_id']=='hero'
    assert 'holder_id' not in row['end_state']['box']
    assert 'hand' not in row['end_state']['box']
    shots[1]['asset_presence'][0]['visibility']='offscreen'
    assert pm.build(b)['shots'][1]['end_state']['box']['holder_id']=='hero'

def test_asset_versions_and_context_change_only_affected_clips():
    b=fixture_board();before=pm.build(b,'movie');b['productionAssets'][1]['description']='small box';after=pm.build(b,'movie')
    assert before['assets']['box']['version']!=after['assets']['box']['version']
    assert before['assets']['crowd']['version']==after['assets']['crowd']['version']
    assert pm.context(before,'c1')['version']!=pm.context(after,'c1')['version']
    assert pm.context(before,'other')['version']==pm.context(after,'other')['version']

@pytest.mark.asyncio
async def test_resume_polls_without_submitting(monkeypatch):
    from flowboard.services import automation
    from flowboard.services.video import registry
    pid=make_project();queue(pid);d=jobs.claim();d['provider_job_id']='b2b:known';d['prepared']={'external_job_id':'b2b:known'}
    class Fake:
        async def submit(self,*args):raise AssertionError('double charge')
        async def poll(self,jid):assert jid=='b2b:known';return {'status':'succeeded'}
    monkeypatch.setattr(registry,'register_defaults',lambda:None);monkeypatch.setattr(registry,'get_video_provider',lambda _:Fake())
    async def publish(*a):return {'url':'https://example.test/result','persisted':True}
    monkeypatch.setattr(automation,'publish_clip',publish)
    await jobs.execute(d)
    with get_session() as s:assert s.get(AutomationJob,d['id']).status=='succeeded'

@pytest.mark.asyncio
async def test_paid_submission_receipt_saved_then_success(monkeypatch):
    from flowboard.services import automation
    from flowboard.services.video import registry
    pid=make_project();queue(pid);d=jobs.claim();calls=[]
    async def prepare(**kwargs):assert kwargs['unmoderated'] is True;return {'motion_prompt':'test'}
    class Fake:
        async def submit(self,p):calls.append('submit');return {'external_job_id':'b2b:receipt','warnings':[]}
        async def poll(self,jid):
            with get_session() as s:assert s.get(AutomationJob,d['id']).provider_job_id=='b2b:receipt'
            return {'status':'succeeded'}
    async def publish(*args):return {'url':'https://example.test/video','persisted':True}
    monkeypatch.setattr(registry,'register_defaults',lambda:None);monkeypatch.setattr(registry,'get_video_provider',lambda _:Fake())
    monkeypatch.setattr(automation,'prepare_clip',prepare);monkeypatch.setattr(automation,'publish_clip',publish)
    await jobs.execute(d)
    assert calls==['submit']
    with get_session() as s:
        assert s.get(AutomationJob,d['id']).status=='succeeded'
        board=jobs.project_board(s,s.get(AutomationProject,pid))
        assert board['nodes'][0]['data']['status']=='done'

@pytest.mark.asyncio
async def test_transport_timeout_after_submit_stays_unknown(monkeypatch):
    from flowboard.services import automation
    from flowboard.services.video import registry
    pid=make_project();queue(pid);d=jobs.claim()
    async def prepare(**kwargs):return {}
    class Fake:
        async def submit(self,p):raise TimeoutError('accepted?')
    monkeypatch.setattr(automation,'prepare_clip',prepare);monkeypatch.setattr(registry,'register_defaults',lambda:None)
    monkeypatch.setattr(registry,'get_video_provider',lambda _:Fake())
    await jobs.execute(d)
    with get_session() as s:assert s.get(AutomationJob,d['id']).status=='unknown'
    assert jobs.claim() is None


def test_job_project_scope_and_provider_reconciliation():
    from fastapi import HTTPException
    pid=make_project()
    jid=uuid.UUID(queue(pid)['id'])
    other=make_project()
    with pytest.raises(HTTPException) as exc:routes.get_job(other,jid,None)
    assert exc.value.status_code==404
    with get_session() as s:
        j=s.get(AutomationJob,jid);j.status='unknown';s.add(j);s.commit()
    r=routes.resume_job(pid,jid,routes.ReconcileJob(provider_job_id='b2b:existing'),None)
    assert r['status']=='running' and r['provider_job_id']=='b2b:existing'
    assert queue(pid,key='same')['id']==str(jid)


def test_preserved_timeline_does_not_round_or_stretch_shots():
    from flowboard.services.prompt_writer import production_timeline,_mmss
    shots=[{'duration_s':.4,'dialogue':[]},{'duration_s':.6,'dialogue':[]}]
    fitted,slots,duration=production_timeline({'preserve_source_shots':True},shots)
    assert slots==[(0,.4),(.4,1)] and duration==4 and fitted==shots
    assert _mmss(.4)=='00:00.400'

@pytest.mark.asyncio
async def test_reference_board_preserves_all_short_shots(monkeypatch):
    from flowboard.services.video_analyzer import board as builder
    monkeypatch.setattr(builder,'attach_inventory',lambda analysis,cast:cast)
    analysis={'shots':[{'shot':1,'start':0.,'end':.4,'source':{}},{'shot':2,'start':.4,'end':1.,'source':{}}],
      'sequences':[{'first_shot':1,'last_shot':2}], 'video':{'duration':1.,'aspect_ratio':'16:9'}}
    b=await builder.build_board(analysis,{'shots':{}},cast={'characters':[],'environments':[]},source_id='movie')
    shots=b['shots']['clip-01'];assert len(shots)==2
    assert [x['duration_s'] for x in shots]==[.4,.6]
    assert shots[0]['shot_uid']=='movie:shot:1' and shots[1]['source_start']==.4
    merged=await builder.build_board(analysis,{'shots':{}},cast={'characters':[],'environments':[]},preserve_source_shots=False)
    assert len(merged['shots']['clip-01'])==1

@pytest.mark.asyncio
async def test_board_covers_shots_when_story_omits_or_overlaps_ranges(monkeypatch):
    from flowboard.services.video_analyzer import board as builder
    monkeypatch.setattr(builder,'attach_inventory',lambda a,c:c)
    a={'shots':[{'shot':n,'start':n-1,'end':n,'source':{}} for n in range(1,7)],
       'sequences':[{'first_shot':1,'last_shot':3},{'first_shot':3,'last_shot':4}], 'video':{'duration':6}}
    b=await builder.build_board(a,{},cast={})
    assert [s['source_shot'] for shots in b['shots'].values() for s in shots]==list(range(1,7))


def test_prompt_payload_includes_scene_state_and_fractional_policy():
    from flowboard.services import prompt_writer as w
    import inspect
    # Full call exercises the formatter rather than checking prompt code strings.
    args={'sequence':{'preserve_source_shots':True,'production_context':{'version':'v1','shots':[]}},
          'shots':[],'slots':[],'duration':4,'characters':[],'environment':None,'previous_state':'','aspect_ratio':'16:9','style_note':''}
    sig=inspect.signature(w._payload)
    for key,param in sig.parameters.items():
        if key not in args and param.default is inspect.Parameter.empty:
            args[key]={'look':'cg3d','strict':False,'unsafe_map':{},'school_age':False}.get(key,[])
    payload=w._payload(**args)
    assert payload['production_context']['version']=='v1'
    assert 'fractional' in payload['source_timing_policy']

@pytest.mark.asyncio
async def test_source_task_persists_and_resumes_without_browser(monkeypatch):
    from flowboard.db.models import VideoAnalysis
    from flowboard.routes import video_analysis as source_routes
    with get_session() as s:
        source=VideoAnalysis(name='Source');s.add(source);s.commit();s.refresh(source);vid=source.id
    job=jobs.enqueue_source(vid,{'task':'analyze'})
    assert jobs.source_running(vid)
    with pytest.raises(ValueError):jobs.enqueue_source(vid,{'task':'refine'})
    async def analyze(video_id,then_adapt):source_routes._update(video_id,status='analysed')
    monkeypatch.setattr(source_routes,'_run_analysis',analyze)
    claimed=jobs.claim();await jobs.execute(claimed)
    assert not jobs.source_running(vid)
    with get_session() as s:assert s.get(AutomationJob,uuid.UUID(job['id'])).status=='succeeded'

@pytest.mark.asyncio
async def test_writer_job_keeps_signed_result_and_manual_draft(monkeypatch):
    pid=make_project()
    payload={'sequence':{'key':'c1'},'shots':[],'characters':[]}
    job=jobs.enqueue(pid,'write','vid:c1','prompt',payload,'write1',metadata={'base_prompt':'test','input_fingerprint':'h1:test'})
    from flowboard.routes import automation as routes
    async def fake_write(body):return routes.VideoWriteResponse(prompt='Written prompt',duration_seconds=4,coverage_token='receipt',contract_digest='digest')
    monkeypatch.setattr(routes,'write_video_prompt',fake_write)
    d=jobs.claim();await jobs.execute(d)
    with get_session() as s:
        row=s.get(AutomationProject,pid);out=jobs.project_board(s,row)
        assert out['nodes'][0]['data']['prompt']=='Written prompt'
        assert out['nodes'][0]['data']['inputFingerprint']=='h1:test'
        board=deepcopy(row.board);board['nodes'][0]['data']['prompt']='user edit';row.board=board;s.add(row);s.commit()
        assert jobs.project_board(s,row)['nodes'][0]['data']['prompt']=='user edit'


def test_http_job_creation_stale_board_and_current_completion(client):
    pid=make_project()
    body={'kind':'clip','node_id':'vid:c1','payload':{'project_id':str(pid),'sequence_key':'c1','prompt':'test','duration_seconds':4,'reference_urls':['https://example.test/ref']},'request_key':'http1','expected_revision':0}
    r=client.post(f'/api/automation/projects/{pid}/jobs',json=body);assert r.status_code==202,r.text
    duplicate=client.post(f'/api/automation/projects/{pid}/jobs',json=body);assert duplicate.json()['id']==r.json()['id']
    body['expected_revision']=3
    assert client.post(f'/api/automation/projects/{pid}/jobs',json=body).status_code==409
    with get_session() as s:
        j=s.get(AutomationJob,uuid.UUID(r.json()['id']));j.status='succeeded';j.result={'url':'https://example.test/video','persisted':True};s.add(j);s.commit()
    board=client.get(f'/api/automation/projects/{pid}').json()['board'];assert board['nodes'][0]['data']['status']=='done'


def test_unknown_requires_explicit_provider_absence_before_new_take():
    pid=make_project();r=queue(pid);jid=uuid.UUID(r['id'])
    with get_session() as s:
        j=s.get(AutomationJob,jid);j.status='unknown';s.add(j);s.commit()
    from fastapi import HTTPException
    with pytest.raises(HTTPException):routes.resolve_absent_job(pid,jid,routes.ResolveAbsentJob(provider_checked_no_submission=False,note='Checked account'),None)
    out=routes.resolve_absent_job(pid,jid,routes.ResolveAbsentJob(provider_checked_no_submission=True,note='Checked Avis account: no generation accepted.'),None)
    assert out['status']=='failed' and queue(pid,key='intentional-new-take')['id']!=r['id']


def test_concurrent_enqueue_and_claim_are_exclusive():
    from concurrent.futures import ThreadPoolExecutor
    pid=make_project()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda n:queue(pid,key=f'click-{n}'),range(4)))
    assert len({r['id'] for r in results})==1
    with ThreadPoolExecutor(max_workers=4) as pool:
        claimed=list(pool.map(lambda _:jobs.claim(),range(4)))
    assert len([x for x in claimed if x is not None])==1


@pytest.mark.asyncio
async def test_changed_shotlist_rejected_before_paid_submit(monkeypatch):
    from flowboard.services import automation
    from flowboard.services.video import registry
    pid=make_project()
    payload={'project_id':str(pid),'sequence_key':'c1','prompt':'test','duration_seconds':4,
             'reference_urls':['https://example.test/ref'],'prompt_contract':{'sequence':{'key':'c1'},'shots':[],'characters':[]}}
    jobs.enqueue(pid,'clip','vid:c1','',payload,'stale')
    with get_session() as s:
        p=s.get(AutomationProject,pid);board=deepcopy(p.board);board['nodes'][1]['data']['shots']=[{'id':1,'duration_s':4}];p.board=board;s.add(p);s.commit()
    monkeypatch.setattr(routes,'_validate_generation_contract',lambda _:None)
    monkeypatch.setattr(registry,'register_defaults',lambda:None)
    monkeypatch.setattr(registry,'get_video_provider',lambda _:object())
    async def forbidden(**kwargs):raise AssertionError('Must reject before preparing/submitting')
    monkeypatch.setattr(automation,'prepare_clip',forbidden)
    data=jobs.claim();await jobs.execute(data)
    with get_session() as s:
        job=s.get(AutomationJob,data['id']);assert job.status=='failed' and 'Shotlist changed' in job.error


def test_verified_writer_result_clears_stale_writer_error():
    board={'nodes':[{'id':'seq:c1','data':{'kind':'sequence','shots':[]}},
                    {'id':'vid:c1','data':{'kind':'video','prompt':'','writerError':'Old draft failed'}}]}
    job=AutomationJob(project_id=uuid.uuid4(),kind='write',node_id='vid:c1',slot='prompt',request_key='verified',status='succeeded',
                      payload={'sequence':{'key':'c1'},'shots':[]},prepared={'base_prompt':''},result={'prompt':'Reviewed current draft'})
    result=jobs.overlay(board,[job])['nodes'][1]['data']
    assert result['prompt']=='Reviewed current draft' and 'writerError' not in result
    assert board['nodes'][1]['data']['writerError']=='Old draft failed'


def test_superseded_text_attempt_does_not_replace_used_verified_prompt():
    from datetime import datetime,timezone
    board={'nodes':[{'id':'seq:c1','data':{'kind':'sequence','shots':[]}},
                    {'id':'vid:c1','data':{'kind':'video','prompt':''}}]}
    good=AutomationJob(project_id=uuid.uuid4(),kind='write',node_id='vid:c1',slot='prompt',request_key='good',status='succeeded',
        created_at=datetime(2026,1,1,tzinfo=timezone.utc),payload={'sequence':{'key':'c1'},'shots':[]},prepared={'base_prompt':''},result={'prompt':'Used reviewed draft'})
    obsolete=AutomationJob(project_id=good.project_id,kind='write',node_id='vid:c1',slot='prompt',request_key='obsolete',status='failed',
        created_at=datetime(2026,1,2,tzinfo=timezone.utc),error='Obsolete rewrite failed',prepared={'superseded_by_generation':'clip-job'})
    result=jobs.overlay(board,[good,obsolete])['nodes'][1]['data']
    assert result['prompt']=='Used reviewed draft' and result['writerJobStatus']=='succeeded'
    assert obsolete.status=='failed' and obsolete.error=='Obsolete rewrite failed'
