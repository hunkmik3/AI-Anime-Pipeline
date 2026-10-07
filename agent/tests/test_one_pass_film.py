import json
from copy import deepcopy
from pathlib import Path
import pytest

from flowboard.services.video_analyzer import one_pass_film as film, board
from flowboard.services import source_film, prompt_coverage, production_run


def example(tmp_path):
    from PIL import Image
    frames = []
    for n in (1,2):
        own = []
        for k in (1,2,3):
            rel = f'frames/shot{n:03}_{k}.jpg'; p = tmp_path/rel
            p.parent.mkdir(exist_ok=True); Image.new('RGB',(8,8)).save(p); own.append(rel)
        frames.append(own)
    shots = [{'shot':n,'start':(n-1)*2.,'end':n*2.,'frames':frames[n-1],
              'dialogue_lines':['Bonjour.'] if n==1 else [],
              'source':{'shot_size':'MS','camera_crop':'head to waist','camera_elevation':'level',
                        'composition':'single','camera_movement':'static','action':'Holds a cup.'}} for n in (1,2)]
    analysis = {'video':{'duration':4.,'fps':30.,'aspect_ratio':'1:1'},'shots':shots,
                'analysis_mode':'one_pass','transcript':{'segments':[]},
                'dialogue_track':[{'text':'Bonjour.','start':.5,'end':2.5,'first_shot':1,'last_shot':2}],
                'sequences':[{'first_shot':1,'last_shot':2,'title':'Greeting'}]}
    assets = [
        {'id':'woman','kind':'character','name':'Lina','description':'Adult woman, black hair',
         'states':[{'key':'green','label':'Green coat','look':'black hair','wardrobe':'green coat','posture':'upright'}]},
        {'id':'room','kind':'environment','name':'Room','description':'Small apartment'},
        {'id':'cup','kind':'prop','name':'Cup','description':'White cup'}]
    for a in assets: a.update(evidence_ids=['shot-1-frame-2'],member_ids=[],depends_on_asset_ids=[],reference_required=True)
    rows = {}
    for n in (1,2):
        eid = f'shot-{n}-frame-2'
        rows[str(n)] = {'scene_id':'home','environment_key':'room','evidence_ids':[eid],
            'asset_presence':[{'asset_id':a['id'],'visibility':'visible','position':'center','state':'unchanged',
                               'evidence_ids':[eid],**({'holder_id':'woman'} if a['id']=='cup' else {})} for a in assets],
            'character_states':{'woman':'green'},'speakers':[{'asset_id':'woman','label':'Lina','delivery':'on_camera'}] if n==1 else [],
            'transitions':[],'departures':[],'findings':[]}
    data = {'assets':assets,'scenes':[{'id':'home','shot_ids':[1,2],'present_asset_ids':['woman','room','cup']}],
            'shots':rows,'findings':[]}
    return analysis,data


@pytest.mark.asyncio
async def test_catalog_to_board_to_production_has_real_readiness_and_locks(tmp_path,monkeypatch):
    a,data = example(tmp_path); original=deepcopy(a)
    async def call(*args,**kwargs):return deepcopy(data)
    monkeypatch.setattr(film,'_call',call)
    prepared,cast,adapt = await film.prepare(a,tmp_path,'New film',source_film.source_rules('anime_jp_modern'))
    assert a == original
    assert prepared['source_verification']['status']=='observed'
    assert prepared['source_verification']['reviewed_shots']==[]
    source_film.validate_source(prepared)
    source_film.validate_adaptation(prepared,adapt)
    out=await board.build_board(prepared,adapt,cast=cast,source_id='source-1')
    b=source_film.graph(out,source_film.FilmOptions(style='anime_jp_modern').model_dump())
    seq=out['shots']['clip-01']
    refs=[{'asset_id':a['id'],'ref_label':f'@image{i}','ref_url':f'https://example.test/{i}.png'} for i,a in enumerate(out['production_assets'],1)]
    assert not prompt_coverage.validate_source_contract(seq,out['production_assets'],out['source_verification'],refs)
    assert seq[0]['dialogue']==[{'who':'Lina','line':'Bonjour.','delivery':'on_camera'}]
    assert seq[1]['dialogue']==[]
    assert seq[0]['character_states']=={'woman':'green'}
    preview = production_run.preview(b,'test',{'sequence_keys':[]})
    assert preview['ready'],preview['issues']
    for field,value in [('camera','orbit'),('dialogue',[]),('character_states',{'woman':'blue'})]:
        changed=deepcopy(seq);changed[0][field]=value
        assert any('locked '+field in x for x in prompt_coverage.validate_source_contract(changed,out['production_assets'],out['source_verification'],refs))
    changed=deepcopy(prepared);changed['shots'][0]['source']['action']='Invented'
    with pytest.raises(ValueError,match='stale'):source_film.validate_source(changed)


def test_custody_and_offscreen_persistence_do_not_create_new_people(tmp_path):
    a,data=example(tmp_path)
    # Same scene, close-up of room only. Prior woman and cup persist offscreen.
    data['shots']['2']['asset_presence']=[p for p in data['shots']['2']['asset_presence'] if p['asset_id']=='room']
    data['shots']['2']['character_states']={}
    evidence=film.inv._initial_evidence(tmp_path,a['shots'],30,False)
    patch,issues=film.normalize(data,a['shots'],{'assets':[],'shots':{},'scenes':[]},evidence)
    assert not issues
    pres={p['asset_id']:p for p in patch['shots']['2']['asset_presence']}
    assert pres['woman']['visibility']=='offscreen' and pres['cup']['holder_id']=='woman'
    assert patch['shots']['2']['character_states']=={'woman':'green'}
    # Same image overlap must not silently transfer custody.
    other=deepcopy(data['assets'][0]);other.update(id='man',name='Man',description='Adult man')
    data['assets'].append(other)
    data['shots']['2']['asset_presence'].append({'asset_id':'cup','visibility':'visible','evidence_ids':['shot-2-frame-2'],'holder_id':'man'})
    _,issues=film.normalize(data,a['shots'],{'assets':[],'shots':{},'scenes':[]},evidence)
    assert any(i['code']=='prop_teleport' for i in issues)


@pytest.mark.asyncio
async def test_explicit_environment_binding_does_not_need_a_second_model_call(tmp_path, monkeypatch):
    a,data=example(tmp_path)
    for row in data['shots'].values():
        row['asset_presence']=[p for p in row['asset_presence'] if p['asset_id']!='room']
    evidence=film.inv._initial_evidence(tmp_path,a['shots'],30,False)
    patch,issues=film.normalize(data,a['shots'],{'assets':[],'shots':{},'scenes':[]},evidence)
    assert not issues
    assert all(not any(p['asset_id']=='room' for p in r['asset_presence']) for r in patch['shots'].values())
    assert all(not any(p['asset_id']=='room' for p in r['asset_presence']) for r in data['shots'].values())
    unknown=deepcopy(data);unknown['shots']['1']['environment_key']='invented'
    _,issues=film.normalize(unknown,a['shots'],{'assets':[],'shots':{},'scenes':[]},evidence)
    assert any(i['code']=='missing_environment' for i in issues)
    calls=[]
    async def call(*args,**kwargs):calls.append(kwargs);return deepcopy(data)
    monkeypatch.setattr(film,'_call',call)
    prepared,cast,adapt=await film.prepare(a,tmp_path,'Film',source_film.source_rules('anime_jp_modern'))
    out=await board.build_board(prepared,adapt,cast=cast,source_id='source-1')
    graph=source_film.graph(out,source_film.FilmOptions(style='anime_jp_modern').model_dump())
    preview=production_run.preview(graph,'test',{'sequence_keys':[]})
    assert len(calls)==1 and preview['ready'],preview['issues']
    packages=production_run.selected_packages(graph,'test',[])
    assert all(any(material['asset_id']=='room' for material in package['materials'].values()) for package in packages)
    for shots in out['shots'].values():
        assert all(shot['environment_key']=='room' for shot in shots)
        assert all(not any(p['asset_id']=='room' for p in shot['asset_presence']) for shot in shots)
    refs=[{'asset_id':asset['id'],'ref_label':f'@image{i}','ref_url':f'https://example.test/{i}.png'}
          for i,asset in enumerate(out['production_assets'],1)]
    assert not prompt_coverage.validate_source_contract(out['shots']['clip-01'],out['production_assets'],out['source_verification'],refs)


@pytest.mark.parametrize('visibility',['visible','offscreen'])
@pytest.mark.parametrize('separate_batches',[False,True])
def test_explicit_environment_observation_is_preserved_but_not_carried(tmp_path,visibility,separate_batches):
    a,data=example(tmp_path)
    first=next(p for p in data['shots']['1']['asset_presence'] if p['asset_id']=='room')
    first['visibility']=visibility
    data['shots']['2']['asset_presence']=[p for p in data['shots']['2']['asset_presence'] if p['asset_id']!='room']
    evidence=film.inv._initial_evidence(tmp_path,a['shots'],30,False)
    known={'assets':[],'shots':{},'scenes':[]}
    if separate_batches:
        initial={**data,'shots':{'1':data['shots']['1']}}
        patch,issues=film.normalize(initial,a['shots'][:1],known,evidence)
        assert not issues
        film.inv._merge(known,patch)
        later={'assets':[],'scenes':[],'shots':{'2':data['shots']['2']}}
        patch,issues=film.normalize(later,a['shots'][1:],known,evidence)
        film.inv._merge(known,patch)
        patch=known
    else:
        patch,issues=film.normalize(data,a['shots'],known,evidence)
    assert not issues
    kept=next(p for p in patch['shots']['1']['asset_presence'] if p['asset_id']=='room')
    assert kept['visibility']==visibility and kept['state']==first['state'] and kept['evidence_ids']==first['evidence_ids']
    assert not any(p['asset_id']=='room' for p in patch['shots']['2']['asset_presence'])


def test_environment_migration_removes_only_exact_compiler_sentinel():
    sentinel={'asset_id':'room','visibility':'offscreen',
              'state':'Assigned setting; scenery visibility not separately specified.','evidence_ids':[]}
    records=[sentinel,{**sentinel,'visibility':'visible'},
             {**sentinel,'state':'The room is outside this crop.'},
             {**sentinel,'evidence_ids':['own-frame']},{**sentinel,'asset_id':'person'}]
    inventory={'assets':[{'id':'room','kind':'environment'},{'id':'person','kind':'character'}],
               'shots':{str(i):{'asset_presence':[record]} for i,record in enumerate(records,1)}}
    before=deepcopy(inventory)
    cleaned,removed=film.remove_legacy_environment_presence(inventory)
    assert removed==[{'shot':'1','presence':sentinel}]
    assert cleaned['shots']['1']['asset_presence']==[]
    assert all(cleaned['shots'][str(i)]['asset_presence']==[record] for i,record in enumerate(records[1:],2))
    assert inventory==before


@pytest.mark.asyncio
async def test_environment_migration_recompiles_provenance_without_changing_source_facts(tmp_path):
    a,data=example(tmp_path)
    evidence=film.inv._initial_evidence(tmp_path,a['shots'],30,False)
    inventory,issues=film.normalize(data,a['shots'],{'assets':[],'shots':{},'scenes':[]},evidence)
    assert not issues
    for row in inventory['shots'].values():
        for p in row['asset_presence']:
            if p['asset_id']=='room':p.update(visibility='offscreen',state='Assigned setting; scenery visibility not separately specified.',evidence_ids=[])
    original,_,_=film.compile_prepared(a,inventory,'Film',{},evidence=evidence,model='fixture',source_digest=film.digest(a))
    before=deepcopy(original)
    cleaned,removed=film.remove_legacy_environment_presence(original['scene_inventory'])
    prepared,cast,adapt=film.compile_prepared(original,cleaned,'Film',{},evidence=original['source_verification']['evidence'],
        model=original['one_pass_preparation']['model'],source_digest=original['one_pass_preparation']['source_digest'])
    assert len(removed)==2 and original==before
    assert prepared['shots']==original['shots'] and prepared['dialogue_track']==original['dialogue_track']
    assert prepared['source_verification']['locked_shots']==original['source_verification']['locked_shots']
    assert prepared['source_verification']['asset_digests']==original['source_verification']['asset_digests']
    assert prepared['source_verification']['digest']!=original['source_verification']['digest']
    assert prepared['source_verification']['inventory_digest']==film.digest(cleaned)
    assert prepared['one_pass_preparation']['source_digest']==original['one_pass_preparation']['source_digest']
    source_film.validate_source(prepared)
    source_film.validate_adaptation(prepared,adapt)
    out=await board.build_board(prepared,adapt,cast=cast,source_id='source-1')
    refs=[{'asset_id':asset['id'],'ref_label':f'@image{i}','ref_url':f'https://example.test/{i}.png'}
          for i,asset in enumerate(out['production_assets'],1)]
    assert not prompt_coverage.validate_source_contract(out['shots']['clip-01'],out['production_assets'],out['source_verification'],refs)


def test_explicit_offscreen_character_keeps_its_previous_costume(tmp_path):
    a,data=example(tmp_path)
    data['assets'][0]['states'].append({'key':'blue','label':'Blue coat','wardrobe':'blue coat'})
    data['shots']['1']['character_states']={'woman':'blue'}
    data['shots']['2']['character_states']={}
    for p in data['shots']['2']['asset_presence']:
        if p['asset_id']=='woman':p.update(visibility='offscreen',evidence_ids=[])
    evidence=film.inv._initial_evidence(tmp_path,a['shots'],30,False)
    patch,issues=film.normalize(data,a['shots'],{'assets':[],'shots':{},'scenes':[]},evidence)
    assert not issues
    assert patch['shots']['2']['character_states']=={'woman':'blue'}


def test_dialogue_safe_clip_boundaries_and_overlong_speech():
    shots=[{'shot':n+1,'start':n*4.,'end':(n+1)*4.} for n in range(6)]
    seq=[{'first_shot':1,'last_shot':6}]
    clips=board.plan_dialogue_clips(shots,seq,[{'start':19.,'end':22.}],20)
    assert clips[0]['shots']==[1,2,3,4] and clips[1]['shots']==[5,6]
    with pytest.raises(ValueError,match='no dialogue-safe boundary'):
        board.plan_dialogue_clips([{'shot':1,'start':0.,'end':40.}],seq,[],20)


@pytest.mark.asyncio
async def test_local_repair_is_bounded_and_unknown_requests_not_resubmitted(tmp_path,monkeypatch):
    a,data=example(tmp_path);calls=[]
    async def invalid(*args,**kw):
        calls.append(kw);bad=deepcopy(data);bad['shots']['1']['speakers']=[];return bad
    monkeypatch.setattr(film,'_call',invalid)
    with pytest.raises(ValueError,match='needs attention'):await film.prepare(a,tmp_path,'Film',{})
    assert len(calls)==2 and calls[-1]['repair'] is True
    assert (tmp_path/'one-pass-catalog-findings.json').exists()


def test_source_correction_never_changes_audio_or_time(tmp_path):
    a,data=example(tmp_path);evidence=film.inv._initial_evidence(tmp_path,a['shots'],30,False)
    data['shots']['1']['source_correction']={'reason':'test','evidence_ids':['shot-1-frame-2'],'fields':{'dialogue':'new line'}}
    _,issues=film.normalize(data,a['shots'],{'assets':[],'shots':{},'scenes':[]},evidence)
    assert any(i['code']=='invalid_correction' for i in issues)


def test_upload_one_pass_production_queues_durable_film_job(client):
    r=client.post('/api/automation/videos',files={'file':('source.mp4',b'fixture','video/mp4')},
                  data={'analysis_mode':'one_pass','auto_production':json.dumps(source_film.FilmOptions().model_dump())})
    assert r.status_code==200,r.text
    assert r.json()['auto_production']['stage']=='queued'


@pytest.mark.asyncio
async def test_controller_runs_new_catalog_through_materials_video_and_assembly(tmp_path,monkeypatch):
    from sqlmodel import select
    from flowboard.db import get_session
    from flowboard.db.models import VideoAnalysis,AutomationProject,AutomationJob
    from flowboard.routes import video_analysis as routes
    from tests import test_production_run as harness
    a,data=example(tmp_path)
    with get_session() as s:
        p=AutomationProject(name='New source');s.add(p);s.flush()
        v=VideoAnalysis(name='Film',automation_project_id=p.id,status='analysed',analysis=a,
            options={'analysis_mode':'one_pass','auto_production':{'settings':source_film.FilmOptions(review_masters=False).model_dump(),'stage':'analysis'}})
        s.add(v);s.commit();vid=v.id;pid=p.id
    async def call(*args,**kwargs):return deepcopy(data)
    async def no_old_loop(*a,**kw):raise AssertionError('Old review/adaptation loop ran')
    async def design(vid,*args,**kw):
        v=source_film.read(vid);cast=deepcopy(v.cast)
        for bucket in ('characters','environments','props','background_groups'):
            for c in cast.get(bucket,[]):c['design']={'description':c['description']}
        routes._update(vid,cast=cast)
    monkeypatch.setattr(film,'_call',call)
    monkeypatch.setattr(routes,'_work_dir',lambda vid:tmp_path)
    monkeypatch.setattr(routes,'_run_source_refinement',no_old_loop)
    monkeypatch.setattr(routes,'_run_adaptation',no_old_loop)
    monkeypatch.setattr(routes,'_run_cast',no_old_loop)
    monkeypatch.setattr(routes,'_run_design',design)
    await source_film.execute(vid)
    v=source_film.read(vid);rid=v.options['auto_production']['run_id']
    result=harness.drive(pid,rid)
    assert result.status=='succeeded',result.error
    jobs=harness.all_jobs(pid)
    assert {'plate','write','clip','assemble'} <= {j.kind for j in jobs}
    before=len(jobs)
    await source_film.execute(vid)
    assert len(harness.all_jobs(pid))==before
    with get_session() as s:
        b=s.get(AutomationProject,pid).board
    assert b['sourceVerification']['status']=='observed'
    assert b['sourcePipeline']=='one_pass'


@pytest.mark.asyncio
async def test_paid_catalog_reply_checkpoint_prevents_ambiguous_duplicate(tmp_path,monkeypatch):
    from flowboard.services import avis_text
    calls=[]
    async def fail(*a,**kw):calls.append(kw);raise TimeoutError('unknown outcome')
    monkeypatch.setattr(avis_text,'complete',fail)
    with pytest.raises(TimeoutError):await film._call({'shots':[1]},[],tmp_path,'gpt-test')
    with pytest.raises(RuntimeError,match='not resubmitted'):await film._call({'shots':[1]},[],tmp_path,'gpt-test')
    assert len(calls)==1
