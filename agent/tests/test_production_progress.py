"""Actual work counts, source history, review checkpoint, and read-only access."""
from copy import deepcopy
from types import SimpleNamespace
import uuid
import pytest
from fastapi import HTTPException
from flowboard.db import get_session
from flowboard.db.models import AutomationJob, AutomationProject, VideoAnalysis, User
from flowboard.services import production_progress as progress
from flowboard.routes import automation as routes, video_analysis
from tests.test_production_run import project, start, get, tick, pending, finish, drive, all_jobs, two_clips
from tests.test_primary_materials import fixture


def report(pid):
    with get_session() as s:
        return progress.snapshot(s, s.get(AutomationProject, pid))


def stages(r):
    return {s['key']:s for s in r['stages']}


def test_review_shows_masters_and_approval_separately_and_future_clip_total(client):
    pid=project(two_clips()); start(pid,review_masters=True,mode='render')
    r=client.get(f'/api/automation/projects/{pid}/progress'); assert r.status_code==200, r.text
    d=r.json(); rows=stages(d)
    assert rows['masters']['percent']==100 and rows['masters']['status']=='succeeded'
    assert rows['approval']['done']==0 and rows['approval']['status']=='waiting_user'
    assert rows['variants']['done']==0
    assert rows['clip']['total']==2 and rows['clip']['done']==0
    assert d['active_jobs']==0 and not all_jobs(pid)


def test_missing_master_provider_failure_then_upload_supersedes_old_job():
    b=fixture(); b['nodes'][0]['data']['identity']={}
    pid=project(b); start(pid,review_masters=True)
    with get_session() as s:
        j=AutomationJob(project_id=pid,kind='plate',node_id='character:hero',slot='identity',request_key='failed-master',status='failed',error='Provider down')
        s.add(j);s.commit();s.refresh(j);jid=str(j.id)
    r=report(pid); assert stages(r)['masters']['failed']==1 and r['jobs'][0]['error']=='Provider down'
    with get_session() as s:
        p=s.get(AutomationProject,pid);b=deepcopy(p.board)
        b['nodes'][0]['data']['identity']={'referenceUrl':'https://test/new-sheet','uploaded':True,'ignoredRuntimeJobIds':[jid]}
        p.board=b;s.add(p);s.commit()
    r=report(pid); assert stages(r)['masters']['percent']==100 and not r['jobs']


def test_plan_counts_unscheduled_clips_and_ignores_unrelated_finished_job():
    pid=project(two_clips()); started=start(pid,mode='render'); rid=started['id']
    tick(rid)
    for j in pending(pid): finish(j)
    tick(rid)
    writers=[j for j in pending(pid) if j.kind=='write']; assert writers
    finish(writers[0])
    r=report(pid); row=stages(r)['write']
    assert row['done']==1 and row['total']==2 and row['percent']==50
    with get_session() as s:
        s.add(AutomationJob(project_id=pid,kind='write',node_id='vid:unrelated',slot='',request_key='unrelated',status='succeeded'));s.commit()
    assert stages(report(pid))['write']['done']==1
    assert stages(report(pid))['clip']['total']==2


@pytest.mark.parametrize('status',['failed','unknown'])
def test_unsuccessful_work_never_counts_as_completed(status):
    pid=project(); r=start(pid);tick(r['id'])
    task=pending(pid)[0]
    with get_session() as s:
        j=s.get(AutomationJob,task.id);j.status=status;j.error='test failure';s.add(j);s.commit()
    row=stages(report(pid))['raccord']
    assert row['done']==0 and row['status']==status


def test_success_and_reuse_counts_completed_without_new_video_jobs():
    pid=project(); a=start(pid);assert drive(pid,a['id']).status=='succeeded'
    before=len(all_jobs(pid)); b=start(pid);assert drive(pid,b['id']).status=='succeeded'
    r=report(pid);rows=stages(r)
    assert r['completed_stages']==r['total_stages']
    assert rows['write']['percent']==100 and rows['masters']['percent']==100
    assert 'clip' not in rows and len(all_jobs(pid))==before


def test_source_history_survives_stage_change_completion_and_has_no_fake_percent():
    with get_session() as s:
        v=VideoAnalysis(name='source',status='analysing');s.add(v);s.commit();s.refresh(v);vid=v.id
    write=video_analysis._progress_writer(vid)
    write('speech',0,1)
    with get_session() as s:
        v=s.get(VideoAnalysis,vid);row=progress.source_stages(v,True)[0]
        assert row['percent'] is None and row['status']=='running'
    write('speech',1,1);write('vision',35,100)
    video_analysis._update(vid,progress={},status='analysed')
    with get_session() as s:
        rows={x['key']:x for x in progress.source_stages(s.get(VideoAnalysis,vid),True)}
        assert rows['source:speech']['percent']==100 and rows['source:vision']['percent']==35
    progress.record_source(vid,'vision',0,100)
    with get_session() as s:
        assert s.get(VideoAnalysis,vid).progress['steps']['vision']['done']==0


def test_speech_error_is_not_success_despite_callback_complete():
    v=VideoAnalysis(name='source',status='failed',analysis={'speech_error':'ASR unavailable'},progress={'steps':{'speech':{'done':1,'total':1}}})
    row=progress.source_stages(v)[0]
    assert row['done']==0 and row['status']=='failed'


def test_progress_api_is_readonly_and_private(client):
    pid=project(); r=start(pid,review_masters=True)
    with get_session() as s:
        before=deepcopy(s.get(AutomationProject,pid).board)
    for _ in range(2): assert client.get(f'/api/automation/projects/{pid}/progress').status_code==200
    assert not all_jobs(pid) and get(r['id']).status=='paused'
    with get_session() as s:
        p=s.get(AutomationProject,pid);assert p.board==before and p.revision==0
    assert client.get(f'/api/automation/projects/{uuid.uuid4()}/progress').status_code==404
    with get_session() as s:
        owner=User(username='progress-owner',password_hash='unused');s.add(owner);s.flush()
        p=s.get(AutomationProject,pid);p.owner_user_id=owner.id;s.add(p);s.commit()
    with pytest.raises(HTTPException) as error:
        routes.production_progress(pid, user=SimpleNamespace(id=uuid.uuid4()))
    assert error.value.status_code==404


def test_empty_board_has_no_fabricated_progress():
    r=report(project({'nodes':[],'edges':[]}))
    assert r['stages']==[] and r['active_jobs']==0 and r['status']=='idle'


def test_source_phase_history_ends_prior_stage_and_retains_failure():
    from flowboard.services import source_film
    with get_session() as s:
        v=VideoAnalysis(name='source',options={'auto_production':{'stage':'queued'}})
        s.add(v);s.commit();s.refresh(v);vid=v.id
    source_film.state(vid,stage='analysis')
    source_film.state(vid,stage='profiles')
    source_film.state(vid,stage='blocked')
    with get_session() as s:
        rows={r['key']:r for r in progress.source_stages(s.get(VideoAnalysis,vid))}
    assert rows['source-phase:analysis']['percent']==100
    assert rows['source-phase:profiles']['status']=='failed'


def test_source_order_survives_jsonb_key_reordering():
    v=VideoAnalysis(name='source',progress={'steps':{
        'vision':{'done':5,'total':10,'started_at':'2026-10-07T01:03:00+00:00'},
        'probe':{'done':1,'total':1,'started_at':'2026-10-07T01:01:00+00:00'},
        'keyframes':{'done':10,'total':10,'started_at':'2026-10-07T01:02:00+00:00'}}})
    assert [r['key'] for r in progress.source_stages(v,True)]==['source:probe','source:keyframes','source:vision']
