from copy import deepcopy
import json
from types import SimpleNamespace
import pytest
from flowboard.services import avis_text, cinematic_prompt, target_casting, source_film
from flowboard.services.video_analyzer import one_pass_film as film, board as board_mod
from tests.test_one_pass_film import example
from tests.test_production_run import (project,start,drive,all_jobs,two_clips,run,
                                      get_session,AutomationProject)


@pytest.mark.asyncio
async def test_catalog_terminal_empty_retries_once_then_reuses_checkpoint(tmp_path,monkeypatch):
    calls=[]
    async def complete(*a,**kw):
        calls.append(1)
        if len(calls)==1:raise avis_text.AvisEmptyResponse('HTTP 200 terminal empty')
        return SimpleNamespace(text='{"assets":[]}',prompt_tokens=1,completion_tokens=2)
    monkeypatch.setattr(avis_text,'complete',complete)
    assert await film._call({},[],tmp_path,'test')=={'assets':[]}
    assert await film._call({},[],tmp_path,'test')=={'assets':[]}
    assert len(calls)==2
    receipt=json.loads(next((tmp_path/'one-pass-catalog').glob('*.json')).read_text())
    assert receipt['attempts'][0]['state']=='terminal_empty'


@pytest.mark.asyncio
@pytest.mark.parametrize('error',[avis_text.AvisContentRefusal('content refused'),TimeoutError('unknown'),ValueError('bad response')])
async def test_catalog_never_retries_refusal_or_unknown(tmp_path,monkeypatch,error):
    calls=[]
    async def complete(*a,**kw):calls.append(1);raise error
    monkeypatch.setattr(avis_text,'complete',complete)
    with pytest.raises(type(error)):await film._call({},[],tmp_path,'test')
    with pytest.raises(RuntimeError,match='not resubmitted'):await film._call({},[],tmp_path,'test')
    assert len(calls)==1


@pytest.mark.asyncio
async def test_empty_retry_budget_survives_resume(tmp_path,monkeypatch):
    calls=[]
    async def complete(*a,**kw):calls.append(1);raise avis_text.AvisEmptyResponse('empty')
    monkeypatch.setattr(avis_text,'complete',complete)
    with pytest.raises(avis_text.AvisEmptyResponse):await film._call({},[],tmp_path,'test')
    with pytest.raises(RuntimeError):await film._call({},[],tmp_path,'test')
    assert len(calls)==2


@pytest.mark.asyncio
async def test_casting_reaches_board_shots_without_changing_source(tmp_path,monkeypatch):
    analysis,data=example(tmp_path)
    data['shots']['1']['asset_presence'][0]['state']='Black hair in a bun; holding the white cup.'
    async def call(*a,**kw):return deepcopy(data)
    monkeypatch.setattr(film,'_call',call)
    source,cast,adapt=await film.prepare(analysis,tmp_path,'Test',source_film.source_rules('live_action_feature'))
    original=deepcopy(source)
    receipt={'request':'Lina has blonde hair and blue eyes','input_digest':'test',
             'changes':[{'asset_id':'woman','appearance':{'hair_colour':'blonde','eye_colour':'blue'},'basis':'Named Lina'}]}
    target=target_casting.apply_cast(cast,receipt)
    assert target['characters'][0]['states'][0]['wardrobe']==cast['characters'][0]['states'][0]['wardrobe']
    out=await board_mod.build_board(source,adapt,cast=target,source_id='source')
    graph=target_casting.apply_board(source_film.graph(out,{}),receipt)
    c=next(n['data']['character'] for n in graph['nodes'] if n['data']['kind']=='character')
    assert c['target_appearance']['hair_colour']=='blonde'
    shot=next(n['data']['shots'][0] for n in graph['nodes'] if n['data']['kind']=='sequence')
    assert 'blonde hair in a bun' in shot['production_adaptation']['asset_presence'][0]['state']
    assert not film.validate_locked([shot],source['source_verification'])
    assert source==original
    assert shot['dialogue'][0]['line']=='Bonjour.'
    assert shot['dialogue'][0]['who']=='Lina'


@pytest.mark.asyncio
async def test_casting_selection_checkpoint_and_unknown_identity(tmp_path,monkeypatch):
    cast={'characters':[{'key':'hero','source_asset_id':'person-1','role':'lead'}]}
    calls=[]
    async def complete(*a,**kw):
        calls.append(1)
        return SimpleNamespace(text=json.dumps({'changes':[{'asset_id':'invented',
            'appearance':{'hair_colour':'blonde'},'basis':'guess'}]}),prompt_tokens=2,completion_tokens=3)
    monkeypatch.setattr(avis_text,'complete',complete)
    for _ in range(2):
        with pytest.raises(ValueError,match='existing'):await target_casting.resolve('lead blonde',cast,tmp_path)
    assert len(calls)==1


def test_owned_appearance_projection_leaves_wardrobe_objects_and_unspecified_hair_shape():
    text='Dark wavy hair; brown eyes; fair skin; black dress, white cup.'
    out=target_casting.project_text(text,{'hair_colour':'blonde','eye_colour':'blue','skin_tone':'dark brown'})
    assert out=='blonde wavy hair; blue eyes; dark brown skin; black dress, white cup.'


def test_future_clip_edit_reuses_completed_earlier_video_but_changed_clip_renders():
    b=two_clips();pid=project(b);first=start(pid,mode='render')
    assert drive(pid,first['id']).status=='succeeded'
    before=[j for j in all_jobs(pid) if j.kind=='clip']
    with get_session() as s:
        p=s.get(AutomationProject,pid);changed=deepcopy(p.board)
        changed['nodes'][-2]['data']['shots'][0]['action']=['A changed future gesture.']
        p.board=changed;s.add(p);s.commit()
    second=start(pid,mode='render');result=drive(pid,second['id'])
    assert result.status=='succeeded',result.error
    clips=[j for j in all_jobs(pid) if j.kind=='clip']
    assert len([j for j in clips if j.node_id=='vid:c1'])==1
    assert len([j for j in clips if j.node_id=='vid:c2'])==2
    assert result.result['tasks']['clip:c1']['reused']
    assert result.result['tasks']['clip:c1']['id']==str(next(j.id for j in before if j.node_id=='vid:c1'))


def test_own_material_change_does_not_reuse_completed_video():
    pid=project();a=start(pid,mode='render');assert drive(pid,a['id']).status=='succeeded'
    with get_session() as s:
        p=s.get(AutomationProject,pid);b=deepcopy(p.board)
        b['nodes'][0]['data']['character']['summary']='Different approved face'
        p.board=b;s.add(p);s.commit()
    second=start(pid,mode='render');assert drive(pid,second['id']).status=='succeeded'
    assert len([j for j in all_jobs(pid) if j.kind=='clip'])==2


def test_new_audio_policy_is_idempotent_and_inside_audio():
    draft='## AUDIO\nEnglish speech and room tone.\n## CONTINUITY / NEGATIVE CONSTRAINTS\nSame cast.'
    prompt=cinematic_prompt.enforce_audio_policy(draft)
    assert prompt.startswith('## AUDIO\n'+cinematic_prompt.NO_MUSIC)
    assert cinematic_prompt.enforce_audio_policy(prompt)==prompt


def test_cropped_reference_scope_reaches_writer_and_changes_contract():
    from tests.test_production_run import fixture
    b=fixture();old=run.shot_package.build(b,'film','c1')
    data=b['nodes'][0]['data']
    for plate in [data.get('identity',{}),*data.get('states',{}).values()]:
        if plate.get('referenceUrl'):plate['referenceScope']='Face and hair only. No wardrobe or feet reference.'
    package=run.shot_package.build(b,'film','c1')
    body=run.writing_body(b,'film',package,[])
    assert body.characters[0]['reference_scope'].startswith('Face and hair only')
    assert old['version']!=package['version']


@pytest.mark.asyncio
async def test_inherited_offscreen_crop_does_not_claim_previous_screen_visibility(tmp_path,monkeypatch):
    analysis,data=example(tmp_path)
    data['shots']['1']['asset_presence'][2]['state']='Cup rim visible beside the face.'
    data['shots']['1']['asset_presence'][2]['position']='screen left'
    data['shots']['2']['asset_presence']=[p for p in data['shots']['2']['asset_presence'] if p['asset_id']!='cup']
    async def call(*a,**kw):return deepcopy(data)
    monkeypatch.setattr(film,'_call',call)
    source,_,_=await film.prepare(analysis,tmp_path,'Test',source_film.source_rules('anime_jp_modern'))
    cup=next(p for p in source['scene_inventory']['shots']['2']['asset_presence'] if p['asset_id']=='cup')
    assert cup['visibility']=='offscreen' and cup['holder_id']=='woman'
    assert 'visible beside' not in cup['state'] and 'screen left' not in cup['position']
    assert cup['last_visible_description']['state']=='Cup rim visible beside the face.'


@pytest.mark.asyncio
async def test_real_full_take_assembly_keeps_tail_audio_and_reports_actual_duration(tmp_path,monkeypatch):
    import shutil
    from flowboard.services import production_media as media
    if not shutil.which('ffmpeg'):pytest.skip('ffmpeg unavailable')
    path=tmp_path/'late-speech.mp4'
    # Sound only AFTER the editorial cut: trimming .4 seconds would discard it.
    await media.command('ffmpeg','-v','error','-y','-f','lavfi','-i','color=c=blue:s=640x640:r=30:d=1.2',
        '-f','lavfi','-i','sine=frequency=440:duration=1.2','-af',"volume=enable='lt(t,0.6)':volume=0",
        '-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',path)
    async def fetch(url,dest):shutil.copyfile(path,dest)
    monkeypatch.setattr(media,'fetch',fetch);monkeypatch.setattr(media,'STORAGE_DIR',tmp_path)
    result=await media.assemble({'fps':24,'aspect_ratio':'1:1','resolution':'480p','timing_policy':'full_take',
        'clips':[{'sequence_key':'c1','job_id':'paid-1','url':'one','duration_s':.4}]})
    output=tmp_path/'production-renders'/result['filename'];info=await media.probe(output)
    stream=next(s for s in info['streams'] if s['codec_type']=='video')
    assert (stream['width'],stream['height'],stream['r_frame_rate'])==(480,480,'24/1')
    assert result['duration_s']==float(info['format']['duration']) and result['duration_s']>=1.2
    assert result['editorial_duration_s']==.4
    samples=await media.command('ffmpeg','-v','error','-i',output,'-ss','0.8','-t','0.2','-f','s16le','-ac','1','-ar','8000','-')
    import array
    audio=array.array('h',samples)
    assert audio and max(abs(s) for s in audio)>500


def test_full_take_controller_uses_actual_closing_frame_and_new_assembly_policy(monkeypatch):
    from flowboard.services import raccord
    original=raccord.fallback
    def dependent(scene,diagnostic):
        plan=original(scene,diagnostic);plan['shots'][-1]['depends_on_previous']=True
        plan['shots'][-1]['predecessor_shot_id']=plan['shots'][-2]['shot_id']
        return plan
    monkeypatch.setattr(raccord,'fallback',dependent)
    b=two_clips();pid=project(b);a=start(pid,mode='render',timing_policy='full_take')
    assert drive(pid,a['id']).status=='succeeded'
    edit=next(j for j in all_jobs(pid) if j.kind=='assemble')
    assert edit.payload['timing_policy']=='full_take'
    frame=next(j for j in all_jobs(pid) if j.kind=='extract_frame')
    assert frame.payload['at_end'] is True and frame.payload['fps']==24


@pytest.mark.asyncio
async def test_casting_then_design_then_board_then_single_production_handoff(tmp_path,monkeypatch):
    import uuid
    from flowboard.db.models import VideoAnalysis
    from flowboard.routes import video_analysis as routes
    from tests.test_source_film import source,output
    from flowboard.services import production_run
    with get_session() as s:
        p=AutomationProject(name='new auto film');s.add(p);s.flush()
        row=VideoAnalysis(name='source',automation_project_id=p.id,status='analysed',analysis=source(),
            adaptation={'shots':{'1':{'dialogue':[{'who':'LINA','line':'Hello.'}]}}},
            cast={'characters':[{'key':'hero','source_asset_id':'hero','design':{'hair':'black'},'states':[]}],
                  'environments':[{'key':'room','design':{'walls':'pale'}}]},
            options={'auto_production':{'settings':source_film.FilmOptions(casting_request='hero blonde hair').model_dump()}})
        s.add(row);s.commit();vid=row.id
    async def no_adapt(*a,**k):pass
    async def selection(*a,**kw):return {'request':'hero blonde hair','input_digest':'test',
        'changes':[{'asset_id':'hero','appearance':{'hair_colour':'blonde'},'basis':'Named hero'}]}
    steps=[]
    async def design(video_id,pending,**kw):
        row=source_film.read(video_id)
        c=deepcopy(row.cast);assert c['characters'][0]['target_appearance']['hair_colour']=='blonde'
        assert 'design' not in c['characters'][0]
        c['characters'][0]['design']={'hair':'blonde'};routes._update(video_id,cast=c);steps.append('design')
    async def board(*a,**kw):
        assert kw['cast']['characters'][0]['design']['hair']=='blonde';steps.append('board')
        return {**output(),'source_video_id':str(vid)}
    def start_run(*a,**kw):
        steps.append('production');assert a[2]['timing_policy']=='full_take';return {'id':str(uuid.uuid4())}
    monkeypatch.setattr(routes,'_run_adaptation',no_adapt)
    monkeypatch.setattr(routes,'_run_design',design)
    monkeypatch.setattr(target_casting,'resolve',selection)
    monkeypatch.setattr(board_mod,'build_board',board)
    monkeypatch.setattr(production_run,'start',start_run)
    await source_film.execute(vid);await source_film.execute(vid)
    assert steps==['design','board','production']
