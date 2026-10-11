from datetime import timedelta
import uuid
from flowboard.db import get_session
from flowboard.db.models import AutomationProject,AutomationJob,User
from flowboard.services.production_notifications import inbox
from flowboard.services.automation_jobs import now


def test_inbox_is_persistent_owner_scoped_and_superseded_failures_do_not_alert():
    with get_session() as s:
        owner=User(username='inbox-owner',password_hash='unused');other=User(username='other-owner',password_hash='unused')
        s.add(owner);s.add(other);s.flush()
        p=AutomationProject(name='My film',owner_user_id=owner.id);hidden=AutomationProject(name='Private film',owner_user_id=other.id)
        s.add(p);s.add(hidden);s.flush()
        s.add(AutomationJob(project_id=p.id,kind='source',node_id='source',request_key='first',status='failed',created_at=now()-timedelta(minutes=2)))
        live=AutomationJob(project_id=p.id,kind='source',node_id='source',request_key='second',status='preparing')
        s.add(live)
        s.add(AutomationJob(project_id=hidden.id,kind='source',node_id='private',request_key='secret',status='failed'))
        s.commit()
        assert inbox(s,owner.id)=={'events':[],'active_projects':1}
        live.status='blocked';s.add(live);s.commit()
        first=inbox(s,owner.id);second=inbox(s,owner.id)
        assert first==second and first['active_projects']==0 and len(first['events'])==1
        assert first['events'][0]['project_name']=='My film'


def test_manual_pause_is_quiet_but_opted_in_master_review_is_actionable():
    with get_session() as s:
        p=AutomationProject(name='Review');s.add(p);s.flush()
        run=AutomationJob(project_id=p.id,kind='production_run',node_id='run',request_key='run',status='paused')
        s.add(run);s.commit()
        assert not inbox(s)['events']
        run.result={'stage':'master_review'};s.add(run);s.commit()
        event=inbox(s)['events'][0]
        assert 'sheet' in event['message']


def test_inbox_endpoint_exposes_saved_outcome_after_browser_reconnect(client):
    with get_session() as s:
        p=AutomationProject(name='Finished');s.add(p);s.flush()
        s.add(AutomationJob(project_id=p.id,kind='production_run',node_id='run',request_key='done',status='succeeded',payload={'config':{'mode':'render'}}));s.commit()
    response=client.get('/api/automation/production-inbox')
    assert response.status_code==200
    assert response.json()['events'][0]['message']=='Phim đã ghép xong, bạn có thể mở để xem.'
