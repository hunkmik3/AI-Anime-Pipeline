from copy import deepcopy
from types import SimpleNamespace
from datetime import timedelta
import uuid
import pytest
from flowboard.services import avis_text,automation_jobs as jobs
from flowboard.services.video_analyzer import catalog_speaker_review as review,one_pass_film as film
from tests.test_one_pass_voices import example


def fixture():
    data={'assets':[{'id':'speaker','name':'Speaker','kind':'character'},{'id':'listener','name':'Listener','kind':'character'}],
          'shots':{'1':{'speakers':[{'asset_id':'listener','label':'Listener','delivery':'on_camera'}],
                       'findings':[{'shot':1,'code':'voice_conflict','message':'Wrong voice'}]}},'findings':[]}
    batch=[{'shot':1,'start':10,'end':12,'dialogue_pieces':[{'line_id':'L1','text':'Hello there.','start':10.4,'end':11.4}]}]
    issues=[{'shot':1,'code':'voice_conflict','message':'Wrong voice'}]
    answer={'bindings':[{'shot':1,'index':0,'text':'Hello there.','speaker_id':'speaker','delivery':'offscreen',
                         'status':'resolved','at_s':1,'basis':'The same male voice continues over the silent listener reverse angle.'}],
            'resolutions':[{'index':0,'status':'resolved','at_s':1,'basis':'Visible listener is silent while the previous man continues speaking.'}],
            'limitations':[]}
    return data,batch,issues,answer


def test_exact_audio_evidence_corrects_only_binding_and_retains_receipt():
    data,batch,issues,answer=fixture()
    after,changes=review.apply(answer,data,batch,{'assets':[]},issues,10)
    assert after['shots']['1']['speakers'][0]['asset_id']=='speaker'
    assert after['shots']['1']['speakers'][0]['delivery']=='offscreen'
    assert not after['shots']['1']['findings']
    assert data['shots']['1']['speakers'][0]['asset_id']=='listener'
    assert changes[0]['evidence']==answer['bindings'][0]


@pytest.mark.parametrize('defect',['words','unknown','time','unclear','duplicate','mouth'])
def test_missing_or_conflicting_audio_evidence_never_silently_clears_findings(defect):
    data,batch,issues,answer=fixture()
    r=answer['bindings'][0]
    if defect=='words':r['text']='Another line.'
    if defect=='unknown':r['speaker_id']='invented'
    if defect=='time':r['at_s']=20
    if defect=='unclear':answer['resolutions'][0]['status']='unclear'
    if defect=='duplicate':answer['bindings'].append(deepcopy(r))
    if defect=='mouth':r['delivery']='on_camera';r['mouth_visible']=False
    with pytest.raises(ValueError):review.apply(answer,data,batch,{'assets':[]},issues,10)


@pytest.mark.asyncio
async def test_catalog_invokes_audiovisual_recovery_before_deferring_batch(tmp_path,monkeypatch):
    analysis,data=example(tmp_path);(tmp_path/'audio.wav').unlink()
    issue={'shot':1,'code':'speaker_conflict','message':'Source needs listening'}
    data['findings']=[issue];called=[]
    async def call(*args,**kwargs):return deepcopy(data)
    async def recover(raw,*args,**kwargs):
        called.append(1);out=deepcopy(raw);out['findings']=[]
        return out,{'status':'corrected','path':'original-av-evidence','resolved_findings':[issue]}
    monkeypatch.setattr(film,'_call',call);monkeypatch.setattr(review,'recover',recover)
    prepared,_,_=await film.prepare(analysis,tmp_path,'Film',{})
    assert called==[1]
    assert prepared['scene_inventory']['audiovisual_speaker_repairs'][0]['path']=='original-av-evidence'
    assert prepared['source_verification']['independent_review'] is False


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',[avis_text.AvisContentRefusal('refused'),TimeoutError('unknown')])
async def test_audiovisual_recovery_is_never_used_to_resend_a_refused_or_unknown_repair(tmp_path,monkeypatch,failure):
    analysis,data=example(tmp_path);(tmp_path/'audio.wav').unlink()
    data['findings']=[{'shot':1,'code':'speaker_conflict','message':'Ambiguous'}]
    async def call(*args,repair=False,**kwargs):
        if repair:raise failure
        return deepcopy(data)
    async def forbidden(*args,**kwargs):raise AssertionError('Refused content must not be routed')
    monkeypatch.setattr(film,'_call',call);monkeypatch.setattr(review,'recover',forbidden)
    with pytest.raises(film.recovery.CatalogNeedsReview):await film.prepare(analysis,tmp_path,'Film',{})
    saved=__import__('json').loads((tmp_path/'one-pass-catalog-progress.json').read_text())
    assert not saved['inventory'].get('audiovisual_speaker_repairs')


@pytest.mark.asyncio
async def test_rate_rejection_can_resume_but_does_not_reset_paid_receipts(tmp_path,monkeypatch):
    calls=[]
    async def complete(*args,**kwargs):
        calls.append(1)
        if len(calls)==1:raise avis_text.AvisRateLimited('429')
        return SimpleNamespace(text='{"ok":true}',prompt_tokens=1,completion_tokens=1)
    monkeypatch.setattr(avis_text,'complete',complete)
    with pytest.raises(avis_text.AvisRateLimited):await film._call({},[],tmp_path,'test')
    assert await film._call({},[],tmp_path,'test')=={'ok':True}
    assert await film._call({},[],tmp_path,'test')=={'ok':True}
    assert len(calls)==2


@pytest.mark.asyncio
async def test_rate_limited_source_is_scheduled_and_recovered_without_user(monkeypatch):
    from flowboard.db import get_session
    from flowboard.db.models import AutomationJob,VideoAnalysis
    from flowboard.services import source_film
    with get_session() as s:
        v=VideoAnalysis(name='Auto');s.add(v);s.commit();s.refresh(v);vid=v.id
    job=jobs.enqueue_source(vid,{'task':'film'})
    async def limited(_):raise avis_text.AvisRateLimited('429')
    monkeypatch.setattr(source_film,'execute',limited)
    await jobs.execute(jobs.claim())
    with get_session() as s:
        row=s.get(AutomationJob,uuid.UUID(job['id']))
        assert row.status=='queued' and row.prepared['rate_retries']==1
        assert row.lease_until>jobs.now()+timedelta(seconds=40)
        row.lease_until=jobs.now()-timedelta(seconds=1);s.add(row);s.commit()
    async def succeeds(_):pass
    monkeypatch.setattr(source_film,'execute',succeeds)
    await jobs.execute(jobs.claim())
    with get_session() as s:assert s.get(AutomationJob,uuid.UUID(job['id'])).status=='succeeded'


def test_neighboring_audio_differences_are_retained_without_overwriting_words_or_target_evidence():
    data,batch,issues,answer=fixture()
    data['shots']['2']=deepcopy(data['shots']['1']);data['shots']['2']['findings']=[]
    batch.append({'shot':2,'start':12,'end':14,'dialogue_pieces':[{'line_id':'L2','text':'Wait.','start':12.4,'end':13.4}]})
    answer['bindings'].append({**deepcopy(answer['bindings'][0]),'shot':2,'text':'Wait, wait.','at_s':3})
    revised,changes=review.apply(answer,data,batch,{'assets':[]},issues,10)
    assert revised['shots']['1']['speakers'][0]['asset_id']=='speaker'
    assert revised['shots']['2']==data['shots']['2']
    assert changes[-1]['status']=='context_audio_difference' and changes[-1]['source_fidelity_approved'] is False


@pytest.mark.asyncio
async def test_changing_context_does_not_resubmit_a_refused_interval(tmp_path,monkeypatch):
    film.atomic(tmp_path/'catalog-request-outcomes.json',{'source_identity':'same-media',
        'terminal':[{'shots':[5,6],'kind':'content_refusal','reason':'Provider declined'}]})
    async def forbidden(*a,**kw):raise AssertionError('Must not resend')
    monkeypatch.setattr(avis_text,'complete',forbidden)
    with pytest.raises(avis_text.AvisContentRefusal):
        await film._call({'shots':[{'shot':5}],'catalog':{'corrected_earlier_identity':True}},[],tmp_path,'test')


@pytest.mark.asyncio
async def test_malformed_known_response_gets_one_checkpointed_schema_repair(tmp_path,monkeypatch):
    analysis,data=example(tmp_path);(tmp_path/'audio.wav').unlink();calls=[]
    async def call(payload,*args,repair=False,**kwargs):
        calls.append(repair)
        if not repair:raise film.recovery.CatalogMalformedResponse('{"shots":')
        assert payload['malformed_response']=='{"shots":'
        return deepcopy(data)
    monkeypatch.setattr(film,'_call',call)
    await film.prepare(analysis,tmp_path,'Film',{})
    assert calls==[False,True]


@pytest.mark.asyncio
async def test_completed_catalog_drafts_resume_without_repaying_even_with_new_request_context(tmp_path,monkeypatch):
    analysis,data=example(tmp_path);(tmp_path/'audio.wav').unlink()
    async def call(*a,**kw):return deepcopy(data)
    monkeypatch.setattr(film,'_call',call)
    await film.prepare(analysis,tmp_path,'Film',{})
    async def forbidden(*a,**kw):raise AssertionError('Accepted source drafts should be revalidated locally')
    monkeypatch.setattr(film,'_call',forbidden)
    result,_,_=await film.prepare(analysis,tmp_path,'Film',{})
    assert result['scene_inventory']['reused_catalog_shots']==list(range(1,9))


@pytest.mark.asyncio
async def test_exhausted_rate_budget_is_one_actionable_failure_with_source_state(monkeypatch):
    from flowboard.db import get_session
    from flowboard.db.models import AutomationJob,VideoAnalysis
    from flowboard.services import source_film
    with get_session() as s:
        v=VideoAnalysis(name='Limited',status='queued',options={'auto_production':{}});s.add(v);s.commit();s.refresh(v);vid=v.id
    job=jobs.enqueue_source(vid,{'task':'film'})
    with get_session() as s:
        j=s.get(AutomationJob,uuid.UUID(job['id']));j.prepared={'rate_retries':8};s.add(j);s.commit()
    async def limited(_):raise avis_text.AvisRateLimited('429')
    monkeypatch.setattr(source_film,'execute',limited)
    await jobs.execute(jobs.claim())
    with get_session() as s:
        assert s.get(AutomationJob,uuid.UUID(job['id'])).status=='failed'
        row=s.get(VideoAnalysis,vid)
        assert row.status=='failed' and '8' in row.error


def test_saved_answer_echoing_exact_finding_keys_reuses_its_own_timed_binding():
    data,batch,issues,answer=fixture()
    resolution=answer['resolutions'][0]
    resolution.pop('index');resolution.pop('at_s');resolution.update(shot=1,code='voice_conflict')
    out,changes=review.apply(answer,data,batch,{},issues,10)
    assert out['shots']['1']['speakers'][0]['asset_id']=='speaker'
    assert changes[0]['status']=='schema_mapping'
    assert changes[0]['normalized']['at_s']==answer['bindings'][0]['at_s']
    assert changes[0]['normalized']['time_from_binding']=={'shot':1,'index':0}


@pytest.mark.parametrize('defect',['wrong_shot','wrong_code','ambiguous_finding','ambiguous_time','no_evidence','missing_finding'])
def test_schema_mapping_never_fills_missing_or_ambiguous_source_evidence(defect):
    data,batch,issues,answer=fixture()
    r=answer['resolutions'][0];r.pop('index');r.pop('at_s');r.update(shot=1,code='voice_conflict')
    if defect=='wrong_shot':r['shot']=2
    if defect=='wrong_code':r['code']='another_problem'
    if defect=='ambiguous_finding':issues.append({**issues[0],'message':'Another claim'})
    if defect=='ambiguous_time':answer['bindings'].append(deepcopy(answer['bindings'][0]))
    if defect=='no_evidence':answer['bindings'][0].pop('at_s')
    if defect=='missing_finding':issues.append({**issues[0],'code':'different_problem'})
    with pytest.raises(ValueError):review.apply(answer,data,batch,{},issues,10)
