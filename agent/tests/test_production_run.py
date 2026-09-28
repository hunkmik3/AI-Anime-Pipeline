from copy import deepcopy
import uuid
import pytest
from sqlmodel import select
from flowboard.db import get_session
from flowboard.db.models import AutomationProject,AutomationJob
from flowboard.services import automation_jobs as jobs, production_run as run, raccord
from flowboard.routes import automation as routes
from tests.test_shot_package import fixture as base_fixture

def fixture():
    b=base_fixture()
    b["sourceVerification"]={"method":"source_frames","status":"verified","digest":"test-source","reviewed_shots":[1,2],"evidence":[{"id":"f1","shot":1},{"id":"f2","shot":2}]}
    for shot in b["nodes"][-1]["data"]["shots"]:
        shot["source_evidence"]=["f"+str(shot["source_shot"])]
        if not any(p["asset_id"]=="hero" for p in shot["asset_presence"]):shot["asset_presence"].append({"asset_id":"hero","visibility":"visible"})
    return b


def project(board=None):
    with get_session() as s:
        p=AutomationProject(name='Production test',board=board or fixture());s.add(p);s.commit();s.refresh(p);return p.id

def config(**kw):return routes.ProductionRunConfig(**kw).model_dump()
def start(pid,**kw):return run.start(pid,0,config(**kw),'run-'+str(uuid.uuid4()))
def get(jid):
    with get_session() as s:return s.get(AutomationJob,uuid.UUID(str(jid)))
def tick(jid):run.tick(uuid.UUID(str(jid)))
def complete(j,result):
    with get_session() as s:
        obj=s.get(AutomationJob,j.id);obj.status='succeeded';obj.result=result;s.add(obj);s.commit()
def pending(pid):
    with get_session() as s:return s.exec(select(AutomationJob).where(AutomationJob.project_id==pid,AutomationJob.kind!=run.KIND,AutomationJob.status=='queued')).all()
def all_jobs(pid):
    with get_session() as s:return s.exec(select(AutomationJob).where(AutomationJob.project_id==pid,AutomationJob.kind!=run.KIND)).all()
def finish(j):
    if j.kind=='raccord':
        scene=j.payload;out=raccord.fallback(scene,'test');out.update(scope=scene['scope'],input_version=scene['version'],schema_version=raccord.SCHEMA_VERSION)
        out['version']=run.pm.digest(out);complete(j,out)
    elif j.kind=='write':complete(j,{'prompt':'A complete English film prompt.','duration_seconds':8,'writer':'test','source_shots':j.payload['shots']})
    elif j.kind=='clip':complete(j,{'url':'https://test/'+str(j.id)+'.mp4','persisted':True})
    elif j.kind in ('plate','extract_frame'):complete(j,{'images':[{'reference_url':'https://test/'+str(j.id)+'.png','url':'https://test/'+str(j.id)+'.png','media_id':str(j.id)}]})
    elif j.kind=='ingest':complete(j,{'media_ids':{url:'media-ingested' for url in j.payload['urls']}})
    elif j.kind=='assemble':complete(j,{'filename':'film.mp4'})
    else:raise AssertionError(j.kind)
def drive(pid,rid):
    for _ in range(30):
        tick(rid)
        if get(rid).status!='running':return get(rid)
        for j in pending(pid):finish(j)
    raise AssertionError(get(rid).result)


def test_preview_no_calls_and_invalid_source_cuts():
    b=fixture();r=run.preview(b,'film',config());assert r['ready'] and r['missing_materials']==0
    b['nodes'][-1]['data']['shots'][0]['source_shots']=[1,2]
    assert not run.preview(b,'film',config())['ready']

def test_cycle_missing_nodes_block_before_spending():
    b=fixture();b['productionAssets'][1]['depends_on_asset_ids']=['box'];b['productionAssets'][2]['depends_on_asset_ids']=['crowd']
    b['nodes'][1]['data']['plate']={}
    assert not run.preview(b,'film',config())['ready']
    pid=project(b)
    with pytest.raises(ValueError,match='Cyclic'):start(pid)
    assert not pending(pid)

def test_prepare_persisted_and_reused_without_video_generation():
    pid=project();a=start(pid);out=drive(pid,a['id']);assert out.status=='succeeded',out.error
    before=all_jobs(pid);assert {j.kind for j in before}=={'material_binding','raccord','write'}
    b=start(pid);assert drive(pid,b['id']).status=='succeeded';assert len(all_jobs(pid))==len(before)

def test_material_dependency_order_and_no_repeat_regen():
    b=fixture();b['productionAssets'][1]['member_ids']=['hero']
    for n in b['nodes'][:4]:n['data']['identity']={};n['data']['plate']={};n['data']['states']={'day':{}}
    pid=project(b);a=start(pid,image_parallel=2);tick(a['id']);first=pending(pid)
    assert len(first)==2 and not any(j.node_id=='asset:crowd' for j in first)
    for j in first:finish(j)
    out=drive(pid,a['id']);assert out.status=='succeeded',out.error
    images=[j for j in all_jobs(pid) if j.kind=='plate'];assert len(images)<=5
    assert len({(j.node_id,j.slot) for j in images})==len(images)
    assert next(j for j in images if j.node_id=='asset:crowd').payload['reference_urls']


def test_target_generation_order_does_not_mutate_verified_source_catalog():
    b=fixture()
    b['productionAssets'][1]['depends_on_asset_ids']=['box']
    b['productionAssets'][2]['depends_on_asset_ids']=['crowd']
    original=deepcopy(b['productionAssets'])
    b['nodes'][1]['data']['plate']={}
    b['nodes'][2]['data']['plate']={}
    with pytest.raises(ValueError,match='Cyclic'):
        run.material_specs(b,run.selected_packages(b,'film',[]))
    b['nodes'][1]['data']['asset']['generation_depends_on_asset_ids']=['hero']
    b['nodes'][2]['data']['asset']['generation_depends_on_asset_ids']=[]
    specs=run.material_specs(b,run.selected_packages(b,'film',[]))
    assert specs['asset:crowd:plate']['deps']==['character:hero:day']
    assert specs['asset:box:plate']['deps']==[]
    assert b['productionAssets']==original

def test_image_budget_stops_before_first_paid_job():
    b=fixture();b['nodes'][1]['data']['plate']={};pid=project(b);a=start(pid,max_images=0)
    tick(a['id']);assert get(a['id']).status=='blocked';assert not pending(pid)

def test_input_change_blocks_active_run_and_layout_does_not():
    b=fixture();before=run.input_version(b);b['nodes'][0]['position']={'x':7};assert run.input_version(b)==before
    pid=project(b);a=start(pid)
    with get_session() as s:
        p=s.get(AutomationProject,pid);board=deepcopy(p.board);board['nodes'][-1]['data']['shots'][0]['action']=['Changed'];p.board=board;s.add(p);s.commit()
    tick(a['id']);assert get(a['id']).status=='blocked';assert not pending(pid)

def test_unknown_job_blocks_without_resubmit():
    b=fixture();b['nodes'][1]['data']['plate']={};pid=project(b);a=start(pid);tick(a['id']);j=pending(pid)[0]
    with get_session() as s:
        item=s.get(AutomationJob,j.id);item.status='unknown';s.add(item);s.commit()
    tick(a['id']);assert get(a['id']).status=='blocked';assert not pending(pid)

def test_render_order_trim_config_and_settings_retained():
    b=fixture();b['kyc']=True;b['unmoderated']=True;pid=project(b);a=start(pid,mode='render');out=drive(pid,a['id'])
    assert out.status=='succeeded',out.error
    clip=next(j for j in all_jobs(pid) if j.kind=='clip');edit=next(j for j in all_jobs(pid) if j.kind=='assemble')
    assert len(clip.payload['kyc_media_ids'])==4 and clip.payload['unmoderated'] is True
    assert clip.payload['prompt_contract']['sequence']['preserve_source_shots'] is True
    assert edit.payload['clips'][0]['duration_s']==8 and edit.payload['fps']==24
    assert out.result['output']['filename']=='film.mp4'

def test_ingest_old_materials_does_not_regenerate():
    b=fixture();b['kyc']=True
    for n in b['nodes'][:4]:
        for p in [n['data'].get('identity',{}),n['data'].get('plate',{}),*n['data'].get('states',{}).values()]:p.pop('mediaId',None)
    pid=project(b);a=start(pid);out=drive(pid,a['id']);assert out.status=='succeeded',out.error
    kinds=[j.kind for j in all_jobs(pid)];assert 'ingest' in kinds and 'plate' not in kinds

def test_claim_never_takes_controller():
    pid=project();a=start(pid);assert jobs.claim() is None
    tick(a['id']);claimed=jobs.claim();assert claimed['kind']=='raccord'

def test_pause_resume_and_owner_routes(client):
    pid=project();a=start(pid)
    assert client.post(f'/api/automation/projects/{pid}/production-runs/{a["id"]}/pause').status_code==200
    tick(a['id']);assert not pending(pid)
    assert client.post(f'/api/automation/projects/{pid}/production-runs/{a["id"]}/resume').status_code==200
    other=project();assert client.post(f'/api/automation/projects/{other}/production-runs/{a["id"]}/pause').status_code==404
    assert client.get(f'/api/automation/projects/{pid}/production-runs/{a["id"]}/film').status_code==404

def test_idempotency_and_single_active_run():
    pid=project();a=run.start(pid,0,config(),'same');assert run.start(pid,0,config(),'same')['id']==a['id']
    with pytest.raises(ValueError):run.start(pid,0,config(mode='render'),'same')
    with pytest.raises(ValueError,match='already active'):start(pid)


def two_clips():
    b=fixture();seq=b['nodes'][-1];second=deepcopy(seq);second['id']='seq:c2';second['data']['sequence']['key']='c2'
    second['data']['shots']=[second['data']['shots'][1]];second['data']['shots'][0].update(source_shot=3,source_evidence=['f3'])
    b['nodes'].extend([second,{'id':'vid:c2','data':{'kind':'video','prompt':''}}])
    b['sourceVerification']['reviewed_shots'].append(3);b['sourceVerification']['evidence'].append({'id':'f3','shot':3})
    return b


def test_cross_clip_dependency_extracts_at_editorial_boundary_before_next_writer():
    pid=project(two_clips());a=start(pid,mode='render');tick(a['id']);plan=pending(pid)[0]
    finish(plan);j=get(plan.id);out=deepcopy(j.result);out['shots'][-1]['depends_on_previous']=True
    out['shots'][-1]['predecessor_shot_id']=out['shots'][-2]['shot_id'];out['version']=run.pm.digest(out);complete(j,out)
    tick(a['id']);assert [j.node_id for j in pending(pid)]==['vid:c1']
    finish(pending(pid)[0]);tick(a['id']);first_clip=next(j for j in pending(pid) if j.kind=='clip');finish(first_clip)
    tick(a['id']);extract=next(j for j in pending(pid) if j.kind=='extract_frame')
    assert extract.payload['time_s']==pytest.approx(8-1/24)
    assert not any(j.kind=='write' and j.node_id=='vid:c2' for j in all_jobs(pid))
    finish(extract);tick(a['id']);second=next(j for j in pending(pid) if j.kind=='write')
    assert second.payload['reference_assets'][-1]['kind']=='continuity_frame'
    assert second.payload['sequence']['continuity_references'][0]['url'].endswith('.png')
    assert drive(pid,a['id']).status=='succeeded'


def test_independent_clips_queue_together_and_paused_run_stays_paused():
    b=two_clips();b['nodes'][-2]['data']['shots'][0]['scene_id']='other'
    pid=project(b);a=start(pid,mode='render');tick(a['id'])
    for j in pending(pid):finish(j)
    tick(a['id']);assert len([j for j in pending(pid) if j.kind=='write'])==2


def test_failed_task_counts_and_links_survive_blocked_controller():
    pid=project(two_clips());a=start(pid);tick(a['id']);plan=pending(pid)[0];finish(plan)
    tick(a['id']);writers=pending(pid)
    with get_session() as s:
        item=s.get(AutomationJob,writers[0].id);item.status='failed';item.error='test error';s.add(item);s.commit()
    tick(a['id']);out=get(a['id']);assert out.status=='blocked'
    assert out.result['created_counts']['text']>=2
    assert out.result['tasks']['write:c1']['status']=='failed'


def test_new_run_reuses_unaffected_clip_after_target_design_change():
    b=two_clips();second=b['nodes'][-2]['data']['shots'][0];second['scene_id']='elsewhere';second['character_keys']=[]
    second['asset_presence']=[{'asset_id':'hall','visibility':'visible'}]
    pid=project(b);a=start(pid);assert drive(pid,a['id']).status=='succeeded'
    before=[j.id for j in all_jobs(pid) if j.kind=='write' and j.node_id=='vid:c2']
    with get_session() as s:
        p=s.get(AutomationProject,pid);changed=deepcopy(p.board);changed['nodes'][0]['data']['character']['summary']='New hero face';p.board=changed;s.add(p);s.commit()
    new=start(pid);assert drive(pid,new['id']).status=='succeeded'
    after=[j.id for j in all_jobs(pid) if j.kind=='write' and j.node_id=='vid:c2'];assert after==before


@pytest.mark.asyncio
async def test_real_ffmpeg_assembly_trims_padding_and_normalizes_audio(tmp_path, monkeypatch):
    from flowboard.services import production_media as media
    import shutil
    if not shutil.which('ffmpeg'):pytest.skip('ffmpeg unavailable')
    one=tmp_path/'one.mp4';two=tmp_path/'two.mp4'
    await media.command('ffmpeg','-v','error','-y','-f','lavfi','-i','color=c=blue:s=160x90:r=24:d=1','-f','lavfi','-i','sine=frequency=440:duration=1','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',one)
    await media.command('ffmpeg','-v','error','-y','-f','lavfi','-i','color=c=red:s=90x160:r=30:d=1','-c:v','libx264','-pix_fmt','yuv420p',two)
    async def fetch(url,path):shutil.copyfile({'one':one,'two':two}[url],path)
    monkeypatch.setattr(media,'fetch',fetch);monkeypatch.setattr(media,'STORAGE_DIR',tmp_path)
    out=await media.assemble({'fps':24,'aspect_ratio':'16:9','resolution':'720p','clips':[
        {'url':'one','duration_s':.5,'sequence_key':'c1','job_id':'1'},
        {'url':'two','duration_s':.75,'sequence_key':'c2','job_id':'2'}]})
    info=await media.probe(tmp_path/'production-renders'/out['filename'])
    assert abs(float(info['format']['duration'])-1.25)<.15
    video=next(s for s in info['streams'] if s['codec_type']=='video');assert video['width']==1280 and video['r_frame_rate']=='24/1'
    assert any(s['codec_type']=='audio' for s in info['streams'])
    with pytest.raises(ValueError,match='shorter'):
        await media.assemble({'fps':24,'clips':[{'url':'one','duration_s':10,'sequence_key':'c1','job_id':'1'}]})


def test_paid_dispatch_checks_original_run_inputs_before_call():
    pid=project();a=start(pid);tick(a['id']);job=pending(pid)[0]
    run.validate_run_input(pid,job.prepared)
    with get_session() as s:
        p=s.get(AutomationProject,pid);b=deepcopy(p.board);b['kyc']=True;p.board=b;s.add(p);s.commit()
    with pytest.raises(ValueError,match='changed before dispatch'):run.validate_run_input(pid,job.prepared)


def test_prompt_reference_order_and_frame_are_preserved_in_writer_contract():
    b=fixture();pack=run.shot_package.build(b,'film','c1')
    contract=run.writing_body(b,'film',pack,[{'id':'continuity:one','name':'Closing frame','url':'https://test/last','media_id':'last-id','description':'Do not invent a handoff.'}])
    ordered,errors=run.prompt_coverage.reference_slots([*contract.characters,contract.environment,*contract.reference_assets])
    assert not errors and len(ordered)==5 and ordered[-1]['media_id']=='last-id'
    assert not run.shot_package.check_bindings(pack,[*contract.characters,contract.environment,*contract.reference_assets])
    assert contract.sequence['continuity_references'][0]['id']=='continuity:one'


@pytest.mark.asyncio
async def test_changed_inputs_are_rejected_before_provider_and_reused_safely(monkeypatch):
    from flowboard.services import automation
    b=fixture();b['nodes'][1]['data']['plate']={};pid=project(b);a=start(pid);tick(a['id'])
    queued=pending(pid)[0];claimed=jobs.claim();assert claimed['id']==queued.id
    with get_session() as s:
        p=s.get(AutomationProject,pid);board=deepcopy(p.board);board['nodes'][-1]['data']['shots'][1]['action']=['A changed gesture'];p.board=board;s.add(p);s.commit()
    async def forbidden(**kw):raise AssertionError('No provider request allowed')
    monkeypatch.setattr(automation,'generate_plate',forbidden)
    await jobs.execute(claimed)
    failed=get(queued.id);assert failed.status=='failed' and failed.prepared['validation_rejected']
    tick(a['id']);assert get(a['id']).status=='blocked'
    new=start(pid);tick(new['id'])
    recycled=get(queued.id);assert recycled.status=='queued' and recycled.prepared['run_id']==new['id']
    assert len([j for j in all_jobs(pid) if j.kind=='plate'])==1


def test_material_prompt_shared_dependency_atlas_keeps_slot_numbers():
    b=fixture();spec={'kind':'asset','item':{'kind':'background_group','name':'Crowd','id':'crowd'},'plate':{}}
    deps=[{'reference_url':'https://shared','asset_id':'a','name':'A'},{'reference_url':'https://shared','asset_id':'b','name':'B'}]
    out=run.material_payload(spec,deps,b)
    assert out['reference_urls']==['https://shared']
    assert '@image2' not in out['prompt']


def test_concurrent_controller_ticks_schedule_each_request_once():
    from concurrent.futures import ThreadPoolExecutor
    pid=project();a=start(pid)
    with ThreadPoolExecutor(max_workers=4) as executor:list(executor.map(lambda _:tick(a['id']),range(8)))
    tasks=all_jobs(pid)
    assert len([j for j in tasks if j.kind=='raccord'])==1
    assert len({j.request_key for j in tasks})==len(tasks)
