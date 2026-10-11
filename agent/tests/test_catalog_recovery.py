import json
from copy import deepcopy

import pytest

from flowboard.services import avis_text
from flowboard.services.video_analyzer import one_pass_film as film, catalog_recovery as recovery
from tests.test_one_pass_voices import example


def speaking_example():
    assets = [{'id':'woman','kind':'character','name':'Woman in the white dress'},
              {'id':'other','kind':'character','name':'Other woman'}]
    shots, rows, evidence = [], {}, []
    for n in (51,52):
        eid=f'shot-{n}-frame-1'
        evidence.append({'id':eid,'shot':n})
        shots.append({'shot':n,'source':{'action':'Turns her head.' if n==51 else 'Her mouth moves as she speaks.'},
                      'dialogue_pieces':[{'line_id':'L35','cont':n==52}]})
        rows[str(n)]={'asset_presence':[{'asset_id':'woman','visibility':'visible','evidence_ids':[eid]}],
                     'speakers':[{'asset_id':'other','label':'Other woman','delivery':'on_camera',
                                  'mouth_visible':True,'evidence_ids':[eid]}]}
    return {'assets':[],'shots':rows},shots,{'assets':assets},evidence


def test_source_mouth_and_continued_line_repair_wrong_id_without_changing_delivery():
    data,shots,known,evidence=speaking_example()
    fixed,receipts=recovery.speaker_links(data,shots,known,evidence)
    assert len(receipts)==2
    assert all(r['speakers'][0]['asset_id']=='woman' for r in fixed['shots'].values())
    assert all(r['speakers'][0]['delivery']=='on_camera' for r in fixed['shots'].values())
    assert all(r['source_fidelity_approved'] is False for r in receipts)
    assert data['shots']['51']['speakers'][0]['asset_id']=='other'


@pytest.mark.parametrize('uncertainty',['silent','two_people','foreign_evidence','voice_over'])
def test_presence_or_captions_alone_never_resolve_a_speaker(uncertainty):
    data,shots,known,evidence=speaking_example()
    if uncertainty=='silent':
        shots[-1]['source']['action']='She is a silent listener. Caption shows her line.'
    for n,row in data['shots'].items():
        if uncertainty=='two_people':
            row['asset_presence'].append({'asset_id':'other','visibility':'visible','evidence_ids':[f'shot-{n}-frame-1']})
        if uncertainty=='foreign_evidence':row['speakers'][0]['evidence_ids']=['shot-9-frame-1']
        if uncertainty=='voice_over':row['speakers'][0]['delivery']='voice_over'
    fixed,receipts=recovery.speaker_links(data,shots,known,evidence)
    assert not receipts and fixed==data


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['content','refusal','timeout'])
async def test_bad_interval_is_isolated_and_later_sequence_finishes_without_production(tmp_path,monkeypatch,failure):
    analysis,data=example(tmp_path)
    (tmp_path/'audio.wav').unlink()
    monkeypatch.setenv('FLOWBOARD_ONE_PASS_CATALOG_BATCH','2')
    analysis['sequences']=[{'first_shot':1,'last_shot':4},{'first_shot':5,'last_shot':8}]
    calls=[];reports=[]
    async def call(payload,cards,work,model,repair=False):
        ids=[s['shot'] for s in payload['shots']];calls.append((ids,model,repair))
        if ids[0]==1:
            if failure=='refusal':raise avis_text.AvisContentRefusal('provider declined')
            if failure=='timeout':raise TimeoutError('unknown outcome')
        out=deepcopy(data)
        out['shots']={k:v for k,v in out['shots'].items() if int(k) in ids}
        out['scenes']=[{**sc,'shot_ids':ids} for sc in out['scenes']]
        if ids[0]>=5:
            for asset in out['assets']: asset['evidence_ids']=['shot-5-frame-2']
        if ids[0]==1:out['shots']['1']['speakers'][0]['asset_id']='nonexistent'
        if ids[0]==5:
            assert payload['previous_shots']==[] and payload['continuing_scene']=={}
        return out
    monkeypatch.setattr(film,'_call',call)
    def progress(*args):pass
    progress.catalog_detail=lambda r:reports.append(deepcopy(r))
    with pytest.raises(recovery.CatalogNeedsReview) as caught:
        await film.prepare(analysis,tmp_path,'Film',{},on_progress=progress)
    report=caught.value.report
    assert report['completed']==4 and report['processed']==8
    assert report['pending_shots']==[1,2,3,4]
    assert {tuple(c[0]) for c in calls}=={(1,2),(5,6),(7,8)}
    assert all(c[1]=='gpt-6-luna' for c in calls)
    assert not (tmp_path/'one-pass-production.json').exists()
    assert reports[-1]['state']=='needs_review'
    checkpoint=json.loads((tmp_path/'one-pass-catalog-progress.json').read_text())
    assert sorted(checkpoint['inventory']['shots'])==['5','6','7','8']


@pytest.mark.asyncio
async def test_content_refusal_receipt_is_never_retried_after_prompt_update(tmp_path,monkeypatch):
    payload={'shots':[]};cards=[];model='gpt-6-luna'
    key=film.catalog_cache_key(payload,cards,model,system=film.LEGACY_SYSTEM)
    film.atomic(tmp_path/'one-pass-catalog'/f'{key}.json',{
        'state':'failed_or_unknown','error':'Provider declined this content request; automatic retries and fallback stopped.'})
    async def forbidden(*args,**kwargs):raise AssertionError('must not send a new request')
    monkeypatch.setattr(avis_text,'complete',forbidden)
    with pytest.raises(avis_text.AvisContentRefusal):
        await film._call(payload,cards,tmp_path,model)


@pytest.mark.asyncio
async def test_completed_exact_input_legacy_checkpoint_is_reused_with_provenance(tmp_path,monkeypatch):
    payload={'shots':[]};model='gpt-6-luna'
    old=film.catalog_cache_key(payload,[],model,system=film.LEGACY_SYSTEM)
    film.atomic(tmp_path/'one-pass-catalog'/f'{old}.json',{'state':'completed','output':{'kept':True}})
    async def forbidden(*args,**kwargs):raise AssertionError('must reuse result')
    monkeypatch.setattr(avis_text,'complete',forbidden)
    assert await film._call(payload,[],tmp_path,model)=={'kept':True}
    new=film.catalog_cache_key(payload,[],model)
    saved=json.loads((tmp_path/'one-pass-catalog'/f'{new}.json').read_text())
    assert saved['reused_prior_contract']['revalidate'] is True


def test_sequence_boundary_cannot_split_a_continuing_line():
    shots=[{'shot':n,'dialogue_pieces':[{'cont':n==3}]} for n in range(1,7)]
    seq=[{'first_shot':3},{'first_shot':5}]
    assert recovery.next_independent_offset(shots,seq,2)==4


@pytest.mark.asyncio
async def test_completed_old_fallback_can_be_retained_without_resending_a_refusal(tmp_path,monkeypatch):
    analysis,data=example(tmp_path)
    (tmp_path/'audio.wav').unlink()
    calls=[]
    async def replay(payload,cards,work,model,repair=False):
        calls.append(model)
        assert model=='gpt-6-luna' and not repair
        key=film.catalog_cache_key(payload,cards,'claude-opus-5-5',system=film.LEGACY_SYSTEM)
        film.atomic(work/'one-pass-catalog'/f'{key}.json',{'state':'completed','output':deepcopy(data)})
        raise avis_text.AvisContentRefusal('provider declined')
    monkeypatch.setattr(film,'_call',replay)
    prepared,_,_=await film.prepare(analysis,tmp_path,'Film',{})
    assert calls==['gpt-6-luna']
    assert prepared['scene_inventory']['fallback_batches'][0]['reused_completed_receipt']


def test_uncertainty_in_one_piece_prevents_partial_rebinding_of_a_line():
    data,shots,known,evidence=speaking_example()
    data['shots']['52']['asset_presence'].append({'asset_id':'other','visibility':'partial','evidence_ids':['shot-52-frame-1']})
    fixed,receipts=recovery.speaker_links(data,shots,known,evidence)
    assert fixed==data and not receipts


@pytest.mark.asyncio
async def test_unresolved_source_job_waits_for_review_without_an_automatic_resubmit(monkeypatch):
    from uuid import UUID
    from flowboard.db import get_session
    from flowboard.db.models import VideoAnalysis,AutomationJob
    from flowboard.services import source_film,automation_jobs as jobs
    with get_session() as s:
        source=VideoAnalysis(name='Review');s.add(source);s.commit();s.refresh(source);vid=source.id
    job=jobs.enqueue_source(vid,{'task':'film'})
    async def needs_review(video_id):
        raise recovery.CatalogNeedsReview({'completed':7,'total':8,'pending_shots':[4],
            'pending':[{'error':'Ambiguous speaker'}]})
    monkeypatch.setattr(source_film,'execute',needs_review)
    await jobs.execute(jobs.claim())
    with get_session() as s:
        saved=s.get(AutomationJob,UUID(job['id']))
        assert saved.status=='blocked' and '7/8' in saved.error
    assert jobs.claim() is None


def test_retained_prefix_preserves_later_states_but_does_not_import_future_scene_members():
    inventory={'assets':[{'id':'woman','kind':'character','states':[{'key':'coat'},{'key':'dress'}]},
                         {'id':'later-person','kind':'character','states':[{'key':'suit'}]}],
               'scenes':[{'id':'room','shot_ids':[1,2],'present_asset_ids':['woman','later-person']}],
               'shots':{'1':{'scene_id':'room','asset_presence':[{'asset_id':'woman'}],'character_states':{'woman':'coat'}},
                        '2':{'scene_id':'room','asset_presence':[{'asset_id':'woman'},{'asset_id':'later-person'}],
                             'character_states':{'woman':'dress','later-person':'suit'}}}}
    recovery.retain_drafts(inventory,{'assets':[{'id':'woman','kind':'character','states':[{'key':'coat'}]}],'scenes':[],'shots':{}})
    assert [s['key'] for s in inventory['assets'][0]['states']]==['coat','dress']
    patch=recovery.reusable_patch(inventory,[{'shot':1}])
    assert [a['id'] for a in patch['assets']]==['woman']
    assert patch['assets'][0]['states']==[{'key':'coat'}]
    assert patch['scenes'][0]['present_asset_ids']==['woman']


@pytest.mark.asyncio
async def test_retained_provider_hold_does_not_swallow_preceding_unread_shots(tmp_path,monkeypatch):
    analysis,data=example(tmp_path);(tmp_path/'audio.wav').unlink()
    analysis['sequences']=[{'first_shot':1,'last_shot':4},{'first_shot':5,'last_shot':8}]
    film.atomic(tmp_path/'catalog-request-outcomes.json',{'source_identity':film.source_fingerprint(analysis),
        'terminal':[{'shots':[3,4],'kind':'content_refusal','reason':'Previously declined'}]})
    monkeypatch.setenv('FLOWBOARD_ONE_PASS_CATALOG_BATCH','4')
    calls=[]
    async def call(payload,*args,**kw):
        ns=[s['shot'] for s in payload['shots']];calls.append(ns)
        assert not (set(ns)&{3,4} and set(ns)-{3,4})
        if ns==[3,4]:raise avis_text.AvisContentRefusal('Previously declined')
        out=deepcopy(data);out['shots']={k:v for k,v in out['shots'].items() if int(k) in ns}
        out['scenes']=[{**s,'shot_ids':ns} for s in out['scenes']]
        return out
    monkeypatch.setattr(film,'_call',call)
    with pytest.raises(recovery.CatalogNeedsReview) as caught:
        await film.prepare(analysis,tmp_path,'Film',{})
    assert caught.value.report['completed']==6
    assert caught.value.report['pending_shots']==[3,4]
    assert calls==[[1,2],[3,4],[5,6,7,8]]
