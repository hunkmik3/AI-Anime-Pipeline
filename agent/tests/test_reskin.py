import json
"""Provider-free Reskin contracts and durable controller integration."""
from copy import deepcopy
import uuid
import pytest
from flowboard.services import reskin, reskin_source_review as review, reskin_runtime, production_manifest as pm
from flowboard.services import production_run as run, automation
from flowboard.routes import automation as routes
from tests.test_production_run import fixture as base, project, config, start, tick, get, pending, all_jobs, complete, finish


def observations(shots,key='c1'):
    offset=max(0,shots[0]['source_start']-1)
    return {'shots':[{'id':f'{key}:{i+1}','start':s['source_start']-offset,'end':s['source_end']-offset,
        'criteria':{c:{'status':'pass','at_s':s['source_start']-offset+.1,'evidence':'Observed source evidence for this criterion.'} for c in review.CRITERIA}}
        for i,s in enumerate(shots)],'heard_dialogue':[],'findings':[],'limitations':['Automatic review only.']}


def fixture():
    b=base();b.update(motionReference='white_model',sourceVideoId=str(uuid.uuid4()),sourceMediaSha256='test-sha',reskinVersion=reskin.VERSION,aspectRatio='1:1',kyc=True)
    shots=b['nodes'][-1]['data']['shots']
    for i,s in enumerate(shots):s.update(source_start=i*4.,source_end=(i+1)*4.,dialogue=[])
    b['reskinSourceReview']={'status':'reviewed','input_digest':pm.digest(reskin.source_contract(b)),'findings':[],
        'receipts':[{'clip':'c1','input_digest':'segment-one','source_sha256':'test-sha','observations':observations(shots)}]}
    return b


def packages(b):return run.selected_packages(b,'film',[])


@pytest.fixture
def local_ready(monkeypatch):
    monkeypatch.setattr(reskin_runtime,'require_ready',lambda **kw:None)
    monkeypatch.setattr(reskin,'source_path',lambda _:None)
    monkeypatch.setattr(reskin,'source_fingerprint',lambda _:'test-sha')


def test_empty_findings_alone_are_not_approval():
    b=fixture();assert reskin.readiness(b,packages(b))==[]
    b['reskinSourceReview']['receipts'][0]['observations']={'shots':[],'findings':[]}
    assert reskin.readiness(b,packages(b))
    b['reskinSourceReview']={'status':'reviewed','input_digest':pm.digest(reskin.source_contract(b)),'findings':[]}
    assert reskin.readiness(b,packages(b))


@pytest.mark.parametrize('criterion',review.CRITERIA)
def test_an_unknown_speech_or_identity_criterion_blocks_and_motion_carried_ones_warn(criterion):
    # The white-model video carries cuts, camera, positions and the set's geometry; the
    # words, speakers and who wears what must be settled before a clip is generated.
    b=fixture();b['reskinSourceReview']['receipts'][0]['observations']['shots'][0]['criteria'][criterion]['status']='unclear'
    assert run.preview(b,'film',config())['ready'] == (criterion in review.policy.MOTION_CARRIED)


def test_source_changes_invalidate_but_design_ratio_do_not():
    b=fixture();before=pm.digest(reskin.source_contract(b));motion=reskin.motion_payload(b,packages(b)[0])
    b.update(style='anime',aspectRatio='9:16');b['nodes'][2]['data']['asset']['target_description']='Woven basket replacing the box.'
    assert pm.digest(reskin.source_contract(b))==before
    assert reskin.motion_payload(b,packages(b)[0])==motion
    b['nodes'][-1]['data']['shots'][0]['action']=['Changed action']
    assert reskin.readiness(b,packages(b))


@pytest.mark.parametrize('change',['duration','gap','too_long'])
def test_motion_rejects_bad_intervals(change):
    b=fixture();shots=b['nodes'][-1]['data']['shots']
    if change=='duration':shots[0]['duration_s']=5
    if change=='gap':shots[1]['source_start']=5
    if change=='too_long':shots[1].update(source_end=20,duration_s=16)
    with pytest.raises(ValueError):reskin.motion_payload(b,packages(b)[0])


def test_dialogue_and_timestamps_require_original_audio_agreement():
    b=fixture();shots=[{**s,'id':f'c1:{i+1}'} for i,s in enumerate(b['nodes'][-1]['data']['shots'])]
    shots[0]['dialogue']=[{'who':'hero','line':'It is clear.'}];answer=observations(shots)
    assert review.validate(answer,shots,0)[0]['criterion']=='speech_delivery'
    answer['heard_dialogue']=[{'speaker':'hero','speaker_id':'hero','delivery':'on_camera','text':'It is clear.','start':.5,'end':2}]
    assert not review.validate(answer,shots,0)
    answer['heard_dialogue'][0]['end']=90
    with pytest.raises(ValueError):review.validate(answer,shots,0)


@pytest.mark.parametrize('ratio',['1:1','9:16','16:9'])
def test_staging_output_ratios_and_target_binding_independent(ratio):
    b=fixture();b['aspectRatio']=ratio;b['nodes'][2]['data']['asset']['target_description']='Woven basket replacing the box.'
    p=packages(b)[0];payload=reskin.staging_payload(b,p,[{'url':'https://image','description':'Approved cast'}])
    assert payload['aspect_ratio']=='21:9' and p['aspect_ratio']==ratio
    body=run.writing_body(b,'film',p,[],motion_receipt={'media_id':'motion'})
    assert body.aspect_ratio==ratio and body.sequence['motion_receipt']['media_id']=='motion'
    assert 'Woven basket' in next(a for a in body.reference_assets if a['id']=='box')['target_appearance']
    assert body.shots==b['nodes'][-1]['data']['shots']


def test_the_writer_reads_an_assets_observations_of_its_own_shots_only():
    b=fixture();p=packages(b)[0]
    own=b['nodes'][-1]['data']['shots'][0]['source_shot']
    item=b['nodes'][2]['data']['asset']
    item['observed_states']=[{'shot':own,'state':'Held in the box.'},{'shot':999,'state':'STALE OTHER SHOT'}]
    b['sourceVerification']['source_corrections']=[{'changes':[{'before':'REPLACED VALUE'}]}]
    body=run.writing_body(b,'film',p,[],motion_receipt={'media_id':'motion'})
    text=json.dumps(body.model_dump(),default=str)
    assert 'Held in the box.' in text and 'STALE OTHER SHOT' not in text and 'REPLACED VALUE' not in text
    assert item['observed_states'][1]['state']=='STALE OTHER SHOT'


def test_one_clip_can_draw_its_staging_scene_with_another_image_model():
    b=fixture();b['imageModel']='dola-seedream-5-0-pro';p=packages(b)[0];refs=[{'url':'https://image','description':'Approved cast'}]
    assert reskin.staging_payload(b,p,refs)['image_model']=='dola-seedream-5-0-pro'
    before=run.input_version(b)
    next(n for n in b['nodes'] if n['id']=='vid:'+p['sequence_key'])['data']['stagingImageModel']='gemini-3-pro-image'
    assert reskin.staging_payload(b,p,refs)['image_model']=='gemini-3-pro-image'
    assert run.input_version(b)==before and b['imageModel']=='dola-seedream-5-0-pro'


@pytest.mark.asyncio
@pytest.mark.parametrize('ratio',['1:1','9:16','16:9'])
async def test_provider_gets_images_and_motion_in_order(monkeypatch,ratio):
    from flowboard.services.video import avis,registry
    calls=[]
    async def asset(mid,kind,**kw):calls.append((mid,kind));return kind+':'+mid
    monkeypatch.setattr(avis,'ensure_kyc_asset',asset);monkeypatch.setattr(registry,'register_defaults',lambda:None)
    monkeypatch.setattr(registry,'get_video_provider',lambda _:object())
    out=await automation.prepare_clip('test',reference_urls=['one','two'],kyc_media_ids=['a','b'],motion_media_id='motion',duration_seconds=8,aspect_ratio=ratio)
    assert out['aspect_ratio']==ratio and out['kyc_image_asset_ids']==['Image:a','Image:b']
    assert out['kyc_video_asset_id']=='Video:motion' and calls==[('a','Image'),('b','Image'),('motion','Video')]
    assert 'reference_videos' not in out and out['generate_audio']
    with pytest.raises(automation.AutomationError):
        await automation.prepare_clip('test',reference_urls=['one'],kyc_media_ids=['a'],motion_media_id='motion',duration_seconds=8,first_frame_url='first')


def finish_reskin(j,hold=False):
    if j.kind=='motion_reference':complete(j,{'media_id':'motion-media','url':'https://test/motion.mp4','input_digest':pm.digest(j.payload)})
    elif j.kind=='audio_cleanup':complete(j,{'url':j.payload['url']+'?cleaned','native_url':j.payload['url'],'method':'unchanged'})
    elif j.kind=='clip_quality' and hold:complete(j,{'status':'needs_review','findings':[{'code':'extra_speech','message':'Unexpected words remain.'}]})
    else:finish(j)


@pytest.mark.parametrize('ratio',['1:1','9:16','16:9'])
@pytest.mark.parametrize('hold',[False,True])
def test_controller_chain_resume_draft_and_no_duplicate_gen(local_ready,ratio,hold):
    b=fixture();b['aspectRatio']=ratio;pid=project(b);a=start(pid,mode='render',audio_cleanup=True,set_continuity=False)
    for _ in range(40):
        tick(a['id'])
        if get(a['id']).status!='running':break
        for job in pending(pid):finish_reskin(job,hold)
    out=get(a['id']);assert out.status==('blocked' if hold else 'succeeded'),out.error
    created=all_jobs(pid)
    assert {'motion_reference','plate','write','clip','audio_cleanup','clip_quality','assemble'}<={j.kind for j in created}
    assert not {'raccord','extract_frame'}&{j.kind for j in created}
    assert len([j for j in created if j.kind=='plate'])==1
    assert next(j for j in created if j.kind=='plate').payload['aspect_ratio']=='21:9'
    clip=next(j for j in created if j.kind=='clip')
    assert clip.payload['aspect_ratio']==ratio and clip.payload['motion_media_id']=='motion-media'
    assert next(j for j in created if j.kind=='assemble').payload['aspect_ratio']==ratio
    if hold:
        assert out.result['preview_output']['findings_retained'] and out.result['quality_holds'] and 'output' not in out.result
    else:assert out.result['output']['filename']=='film.mp4'
    a=start(pid,mode='render',audio_cleanup=True,set_continuity=False);tick(a['id'])
    assert get(a['id']).status==out.status,get(a['id']).error
    assert len(all_jobs(pid))==len(created)


def test_unknown_motion_not_resubmitted(local_ready):
    pid=project(fixture());a=start(pid,mode='render');tick(a['id'])
    j=next(j for j in pending(pid) if j.kind=='motion_reference')
    from flowboard.db import get_session
    with get_session() as s:
        obj=s.get(type(j),j.id);obj.status='unknown';s.add(obj);s.commit()
    tick(a['id']);assert get(a['id']).status=='blocked'
    assert len([j for j in all_jobs(pid) if j.kind=='motion_reference'])==1


def test_source_receipts_cannot_be_forged_by_save():
    b=fixture();incoming=deepcopy(b);incoming['reskinSourceReview']={'status':'reviewed'}
    assert routes._keep_earned(incoming,b)['reskinSourceReview']==b['reskinSourceReview']
    assert 'reskinSourceReview' not in routes._keep_earned(deepcopy(b),{})


def test_missing_runtime_blocks_before_materials(local_ready,monkeypatch):
    def missing(**kw):raise ValueError('Missing audio runtime')
    monkeypatch.setattr(reskin_runtime,'require_ready',missing);pid=project(fixture())
    with pytest.raises(ValueError,match='Missing audio'):start(pid)
    assert not all_jobs(pid)


def test_reskin_clip_cannot_bypass_motion_by_omitting_package():
    from fastapi import HTTPException
    pid=project(fixture())
    body=routes.ClipBody(project_id=pid,sequence_key='c1',prompt='anything',duration_seconds=8)
    with pytest.raises(HTTPException):
        routes._validate_generation_contract(body)


def test_write_rejects_legacy_without_motion_before_spending():
    from flowboard.services.automation_jobs import validate_package
    from flowboard.routes.automation import VideoWriteBody
    b=fixture()
    with pytest.raises(ValueError,match='Pipeline Reskin'):
        validate_package(b,'film',VideoWriteBody(sequence={'key':'c1'},shots=b['nodes'][-1]['data']['shots'],characters=[]))


@pytest.mark.asyncio
async def test_source_recheck_reuses_identical_intervals_and_retains_unknown(monkeypatch,tmp_path):
    import json
    from flowboard.services import reskin_source_review as mod
    b=fixture();source=tmp_path/'source.mp4';source.write_bytes(b'source')
    monkeypatch.setattr(reskin,'source_path',lambda _:source)
    b['reskinSourceReview']={};b['sourceMediaSha256']=reskin.sha256(source)
    seq=b['nodes'][-1]['data'];rows=[{**deepcopy(s),'id':f'c1:{i+1}'} for i,s in enumerate(seq['shots'])]
    key=pm.digest({'source_sha':b['sourceMediaSha256'],'shots':rows,'catalog':b['productionAssets'],'system':mod.SYSTEM,'model':mod.MODEL})
    folder=tmp_path/'reskin-source-review'/key;folder.mkdir(parents=True)
    journal=folder/'receipt.json'
    journal.write_text(json.dumps({'state':'completed','answer':observations(rows)}))
    report=await mod.inspect(b,tmp_path)
    assert report['status']=='reviewed' and len(report['receipts'])==1
    journal.write_text(json.dumps({'state':'failed_or_unknown'}))
    with pytest.raises(ValueError,match='không tự gửi lại'):await mod.inspect(b,tmp_path)


def test_same_dialogue_wrong_speaker_or_delivery_is_still_a_finding():
    b=fixture();shots=[{**s,'id':f'c1:{i+1}'} for i,s in enumerate(b['nodes'][-1]['data']['shots'])]
    shots[0]['dialogue']=[{'speaker_id':'hero','line':'Hello','delivery':'voice_over'}]
    answer=observations(shots)
    answer['heard_dialogue']=[{'speaker':'Other actor','speaker_id':'other','delivery':'on_camera','text':'Hello','start':1,'end':2}]
    assert any('Speaker' in f.get('message','') for f in review.validate(answer,shots,0))


def test_cleaned_clip_and_its_quality_are_shown_together():
    from flowboard.db.models import AutomationJob
    from flowboard.services import automation_jobs as jobs
    clip=AutomationJob(kind='clip',node_id='vid:c1',slot='',status='succeeded',result={'url':'https://native'})
    audio=AutomationJob(kind='audio_cleanup',node_id='vid:c1',slot='audio',status='succeeded',
        payload={'source_job':str(clip.id)},result={'url':'https://clean','source_job':str(clip.id)})
    quality=AutomationJob(kind='clip_quality',node_id='vid:c1',slot='quality',status='succeeded',
        payload={'source_job':str(clip.id)},result={'url':'https://clean','status':'checks_passed'})
    b=jobs.overlay(fixture(),[clip,audio,quality])
    video=next(n['data'] for n in b['nodes'] if n['id']=='vid:c1')
    assert video['clipUrl']=='https://clean' and video['nativeClipUrl']=='https://native'
    assert video['qualityCheck']['status']=='checks_passed'


def test_grouping_preserves_cuts_and_does_not_truncate_long_dialogue():
    from flowboard.services.video_analyzer.board import plan_dialogue_clips
    shots=[{'shot':i+1,'start':i*4,'end':(i+1)*4} for i in range(6)]
    scenes=[{'first_shot':1,'last_shot':6}]
    clips=plan_dialogue_clips(shots,scenes,[],15,hard_limit_s=15)
    assert [n for c in clips for n in c['shots']]==list(range(1,7))
    assert all(len(c['shots'])*4<=15 for c in clips)
    with pytest.raises(ValueError,match='no dialogue-safe boundary'):
        plan_dialogue_clips(shots,scenes,[{'start':0,'end':17}],15,hard_limit_s=15)


def test_source_fingerprint_cache_invalidates_on_actual_file_change(tmp_path):
    p=tmp_path/'source.mp4';p.write_bytes(b'first')
    before=reskin.source_fingerprint(p)
    assert reskin.source_fingerprint(p)==before
    p.write_bytes(b'other')
    assert reskin.source_fingerprint(p)!=before


def test_resume_retries_only_failed_local_work(local_ready):
    from flowboard.db import get_session
    pid=project(fixture());a=start(pid,mode='render');tick(a['id'])
    j=next(j for j in pending(pid) if j.kind=='motion_reference')
    with get_session() as s:
        obj=s.get(type(j),j.id);obj.status='failed';obj.error='Local disk issue';s.add(obj);s.commit()
    tick(a['id']);assert get(a['id']).status=='blocked'
    routes.control_production_run(pid,uuid.UUID(a['id']),'resume',user=None)
    assert get(j.id).status=='queued'
    assert len([j for j in all_jobs(pid) if j.kind=='motion_reference'])==1


def test_unsupported_resolution_rejected_before_paid_preparation():
    report=run.preview(fixture(),'film',config(resolution='1080p'))
    assert not report['ready']
    assert any(i['code']=='reskin_resolution' for i in report['issues'])
