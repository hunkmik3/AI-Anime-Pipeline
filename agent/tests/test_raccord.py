from copy import deepcopy
from datetime import timedelta
from uuid import uuid4, UUID
import pytest
from flowboard.services import raccord, automation_jobs as jobs, shot_package
from flowboard.db import get_session
from flowboard.db.models import AutomationProject, AutomationJob
from tests.test_shot_package import fixture


def cross_clip():
    board = fixture()
    seq = board['nodes'][-1]['data']
    last = seq['shots'].pop()
    board['nodes'].extend([{'id':'seq:c2','data':{'kind':'sequence','sequence':{'key':'c2','environment_key':'hall'},'shots':[last]}},
                          {'id':'vid:c2','data':{'kind':'video','prompt':''}}])
    return board


def response(scene):
    return {'scene_rule':'Retain crowd and box across reverse angles.', 'shots':[
        {'shot_id':s['shot_id'],'direction':'Keep the box in the observed left hand; use the free hand for greeting.',
         'resolutions':[], 'depends_on_previous': i>0} for i,s in enumerate(scene['shots'])]}


def test_scene_spans_clips_and_cache_ignores_layout_but_tracks_materials():
    board = cross_clip();scenes = raccord.scene_inputs(board,'film')
    assert len(scenes)==1
    scene=next(iter(scenes.values()))
    assert [s['sequence_key'] for s in scene['shots']]==['c1','c2']
    before=scene['version'];board['nodes'][0]['position']={'x':400,'y':1}
    assert next(iter(raccord.scene_inputs(board,'film').values()))['version']==before
    board['nodes'][0]['data']['identity']['referenceUrl']='https://changed'
    assert next(iter(raccord.scene_inputs(board,'film').values()))['version']!=before


@pytest.mark.asyncio
async def test_luna_avis_plan_validated_cached_and_attached(monkeypatch):
    from flowboard.services.video_analyzer import adapt
    board = cross_clip();pid=uuid4();scene=next(iter(raccord.scene_inputs(board,str(pid)).values()))
    async def ask(system,user,*args,**kwargs):
        assert kwargs['model']=='gpt-6-luna' and kwargs['fallback']==''
        return response(scene)
    monkeypatch.setattr(adapt,'ask_json',ask)
    plan=await raccord.plan(scene)
    assert plan['mode']=='ai' and plan['shots'][1]['predecessor_shot_id']==scene['shots'][0]['shot_id']
    job=AutomationJob(project_id=pid,kind='raccord',node_id='raccord:x',request_key='x',status='succeeded',result=plan)
    attached=jobs.overlay(board,[job]);p=shot_package.build(attached,str(pid),'c2')
    assert p['shots'][0]['raccord']['direction'].startswith('Keep the box')
    assert p['raccord_versions']
    assert 'raccordPlans' not in board
    board['nodes'][-2]['data']['shots'][0]['action']=['Changed action']
    assert not jobs.overlay(board,[job]).get('raccordPlans')


@pytest.mark.asyncio
async def test_malformed_model_result_repairs_once_then_falls_back_without_approval(monkeypatch):
    from flowboard.services.video_analyzer import adapt
    scene=next(iter(raccord.scene_inputs(fixture(),'film').values()));calls=[]
    async def bad(*args,**kwargs):calls.append(1);return {'shots':[]}
    monkeypatch.setattr(adapt,'ask_json',bad)
    plan=await raccord.plan(scene)
    assert len(calls)==2 and plan['mode']=='rules_fallback' and len(plan['shots'])==2
    assert all(s['basis']=='production_plan_not_source_evidence' for s in plan['shots'])
    assert plan['shots'][0]['predecessor_shot_id'] is None


def test_unknown_asset_and_invented_transfer_rejected():
    scene=next(iter(raccord.scene_inputs(fixture(),'film').values()));answer=response(scene)
    answer['shots'][0]['resolutions']=[{'asset_id':'intruder','strategy':'keep_last_known','reason':'invented'}]
    with pytest.raises(ValueError):raccord.validate(answer,scene)
    answer['shots'][0]['resolutions']=[{'asset_id':'box','strategy':'supported_transition','reason':'I guessed a transfer'}]
    with pytest.raises(ValueError):raccord.validate(answer,scene)
    answer['shots'][0]['resolutions']=[];answer['shots'][0]['depends_on_previous']=True
    with pytest.raises(ValueError):raccord.validate(answer,scene)


def test_scene_reset_and_untrusted_client_plan():
    board=fixture();board['nodes'][-1]['data']['shots'][1]['continuity_break']=True
    assert len(raccord.scene_inputs(board,'film'))==2
    board['raccordPlans']={'hall':{'fake':True}}
    assert 'raccordPlans' not in jobs.overlay(board,[])


@pytest.mark.asyncio
async def test_queue_deduplicates_and_worker_runs_without_ui_approval(client, monkeypatch):
    from flowboard.services.video_analyzer import adapt
    with get_session() as s:
        p=AutomationProject(name='Automatic raccord',board=cross_clip());s.add(p);s.commit();s.refresh(p);pid=p.id
    request={'sequence_key':'c2','expected_revision':0}
    a=client.post(f'/api/automation/projects/{pid}/raccord',json=request)
    assert a.status_code==202,a.text
    b=client.post(f'/api/automation/projects/{pid}/raccord',json=request)
    assert a.json()[0]['id']==b.json()[0]['id']
    data=jobs.claim()
    async def ask(*args,**kwargs):return response(data['payload'])
    monkeypatch.setattr(adapt,'ask_json',ask)
    await jobs.execute(data)
    c=client.post(f'/api/automation/projects/{pid}/raccord',json=request)
    assert c.json()[0]['status']=='succeeded'
    package=client.get(f'/api/automation/projects/{pid}/shot-packages?sequence_key=c2').json()
    assert package['shots'][0]['raccord']['mode']=='ai'
    assert client.get(f'/api/automation/projects/{uuid4()}/shot-packages').status_code==404


def test_compaction_keeps_state_values_without_repeating_them():
    scene=next(iter(raccord.scene_inputs(fixture(),'film').values()))
    packed=raccord.compact(scene)
    for full,short in zip(scene['shots'],packed['shots']):
        restored=[packed['state_facts'][ref] for ref in short['observed']]
        wanted=[{k:v for k,v in p.items() if k not in {'evidence_ids','source_shot','last_observed_shot','inherited'}} for p in full['observed']]
        assert restored==wanted
    assert scene['shots'][0]['observed'][0]['asset_id']=='crowd'


def test_cancelled_planning_job_can_resume_on_next_automatic_request(client):
    with get_session() as s:
        p=AutomationProject(name='Retry planner',board=fixture());s.add(p);s.commit();s.refresh(p);pid=p.id
    url=f'/api/automation/projects/{pid}/raccord';body={'expected_revision':0}
    first=client.post(url,json=body).json()[0]
    assert client.post(f'/api/automation/projects/{pid}/jobs/{first["id"]}/cancel').status_code==200
    second=client.post(url,json=body).json()[0]
    assert first['id']!=second['id'] and second['status']=='queued'
    assert client.post(url,json=body).json()[0]['id']==second['id']


@pytest.mark.asyncio
async def test_unsupported_transition_repairs_only_affected_shot(monkeypatch):
    from flowboard.services.video_analyzer import adapt
    scene=next(iter(raccord.scene_inputs(fixture(),'film').values()))
    async def ask(*args,**kwargs):
        answer=response(scene)
        answer['shots'][0]['resolutions']=[{'asset_id':'box','strategy':'supported_transition','reason':'guessed'}]
        return answer
    monkeypatch.setattr(adapt,'ask_json',ask)
    result=await raccord.plan(scene)
    assert result['mode']=='ai_with_rules' and len(result['rule_repairs'])==1
    assert result['shots'][0]['direction'].startswith('Stage the supplied action')
    assert result['shots'][1]['direction'].startswith('Keep the box')
