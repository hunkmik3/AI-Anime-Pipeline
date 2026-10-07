import asyncio
import json
import pytest
from flowboard.services import avis_text
from flowboard.services.video_analyzer import adapt, pipeline, source_inventory as inv


@pytest.mark.parametrize('text,finish', [
    ("I'm sorry, but I cannot assist with that request.", 'stop'),
    ('I cannot help with that request.', 'stop'),
    ('', 'content_filter'), ('{}', 'safety'),
])
def test_content_refusal_never_retries_or_switches_models(monkeypatch,text,finish):
    calls=[]
    async def complete(model,*args,**kwargs):
        calls.append(model)
        return avis_text.Completion(text=text,model=model,finish_reason=finish)
    monkeypatch.setattr(avis_text,'complete',complete)
    with pytest.raises(avis_text.AvisContentRefusal):
        asyncio.run(adapt.ask_json('s','u',adapt.TextStats(),model='primary',fallback='backup'))
    assert calls==['primary']


def test_refusal_inside_dialogue_json_is_not_a_provider_refusal():
    avis_text.check_content_response('{"dialogue":"I cannot help with that request."}','stop')


def test_story_refusal_is_checkpointed_and_blocks_downstream_on_resume(tmp_path,monkeypatch):
    calls=[]
    async def entities(*args):
        calls.append('entities')
        raise avis_text.AvisContentRefusal('provider refused')
    async def sequences(*args):
        calls.append('sequences');return []
    monkeypatch.setattr(adapt,'build_sequences',sequences)
    monkeypatch.setattr(adapt,'extract_entities',entities)
    for _ in range(2):
        with pytest.raises(avis_text.AvisContentRefusal):
            asyncio.run(pipeline._story([],tmp_path))
    assert sorted(calls)==['entities','sequences']


def test_empty_inventory_response_retry_has_no_empty_assistant_message(tmp_path,monkeypatch):
    calls=[]
    async def complete(model,messages,**kw):
        calls.append(messages)
        assert all(m['content'] for m in messages)
        return avis_text.Completion(text='' if len(calls)==1 else '{}',model=model)
    monkeypatch.setattr(avis_text,'complete',complete)
    assert asyncio.run(inv._ask('s',{},[],tmp_path,{}))=={}
    assert len(calls)==2


@pytest.mark.parametrize('mode',['standard','fast'])
def test_upload_persists_analysis_mode_for_recovery(client,mode):
    import uuid
    from flowboard.db import get_session
    from flowboard.db.models import VideoAnalysis
    response=client.post('/api/automation/videos',
        files={'file':('new.mp4',b'fixture','video/mp4')},
        data={'analysis_mode':mode,'auto_production':'{}'})
    assert response.status_code==200,response.text
    with get_session() as session:
        row=session.get(VideoAnalysis,uuid.UUID(response.json()['id']))
        assert row.options['analysis_mode']==mode


def test_unknown_analysis_mode_rejected_before_creating_jobs(client):
    from sqlmodel import select
    from flowboard.db import get_session
    from flowboard.db.models import VideoAnalysis,AutomationJob
    response=client.post('/api/automation/videos',
        files={'file':('new.mp4',b'fixture','video/mp4')},data={'analysis_mode':'invalid'})
    assert response.status_code==422
    with get_session() as session:
        assert not session.exec(select(VideoAnalysis)).all()
        assert not session.exec(select(AutomationJob)).all()


def test_fast_analysis_capability_is_advertised(client):
    response=client.get('/api/automation/videos/capabilities')
    assert response.status_code==200
    assert response.json()=={'analysis_modes':['standard','fast','one_pass'],
                            'fast_mode_experimental':True,
                            'one_pass_analysis_only':False,'one_pass_auto_production':True}


def test_source_refusal_is_terminal_across_resumes(tmp_path,monkeypatch):
    calls=[]
    async def ask(*args,**kwargs):
        calls.append(True)
        raise avis_text.AvisContentRefusal('declined')
    monkeypatch.setattr(inv,'_ask',ask)
    entry={'usage':{}}
    async def run():
        for _ in range(2):
            with pytest.raises(avis_text.AvisContentRefusal):
                await inv._stage_call(entry,'observe','s',{},[],tmp_path,asyncio.Semaphore(1),lambda:None)
    asyncio.run(run())
    assert len(calls)==1
