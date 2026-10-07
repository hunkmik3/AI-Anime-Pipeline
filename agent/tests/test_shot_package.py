from copy import deepcopy
import uuid
import pytest
from flowboard.services import shot_package as pack, automation_jobs as jobs
from flowboard.routes import automation as routes
from flowboard.db import get_session
from flowboard.db.models import AutomationProject, AutomationJob


def fixture():
    def material(key,kind):
        field=kind if kind!='prop' and kind!='background_group' else 'asset'
        item={'key':key,'id':key,'name':key,'kind':kind,'states':[{'key':'day','wardrobe':'blue coat'}] if kind=='character' else []}
        plate={'referenceUrl':'https://test/'+key,'mediaId':'media-'+key,'prompt':'STYLE:\nApproved sculpted cinematic CGI.\n\nFORMAT:\n16:9'}
        return {'id':field+':'+key,'data':{'kind':field,field:item,'identity':plate,'plate':plate,'states':{'day':plate},'activeState':'day'}}
    return {'style':'cg3d','imageModel':'dola-seedream-5-0-pro','productionAssets':[
        {'id':'hero','kind':'character'}, {'id':'crowd','kind':'background_group'},
        {'id':'box','kind':'prop','dimensions':'one palm wide'}, {'id':'hall','kind':'environment'}],
        'nodes':[material('hero','character'),material('crowd','background_group'),material('box','prop'),material('hall','environment'),
        {'id':'vid:c1','data':{'kind':'video','prompt':''}},
        {'id':'seq:c1','data':{'kind':'sequence','sequence':{'key':'c1','environment_key':'hall'},'shots':[
            {'n':1,'source_shot':1,'scene_id':'hall','duration_s':4,'character_keys':['hero'],'action':['He holds the box.'],
             'asset_presence':[{'asset_id':'crowd','visibility':'visible'},{'asset_id':'box','visibility':'visible','holder_id':'hero','hand':'left'}]},
            {'n':2,'source_shot':2,'scene_id':'hall','duration_s':4,'character_keys':['hero'],'action':['He greets her with his free hand.'],
             'asset_presence':[{'asset_id':'hero','visibility':'visible'}]}]}}]}


def test_pack_keeps_crowd_and_prop_without_asserting_visible():
    board=fixture();original=deepcopy(board);p=pack.build(board,'film','c1')
    assert p['ready'] and board==original
    shot=p['shots'][1]
    assert 'crowd' in shot['inherited_asset_ids'] and 'crowd' not in shot['visible_asset_ids']
    assert shot['previous_state']['box']['hand']=='left'
    frame=pack.keyframe(p,1)
    assert 'one palm wide' in frame['prompt'] and 'Approved sculpted cinematic CGI.' in frame['prompt']
    assert len(frame['reference_urls'])==4
    assert frame['reference_urls']==pack.keyframe(p,1)['reference_urls']
    assert 'camera cut is not' in frame['prompt']


@pytest.mark.parametrize('separator', ['\n', '\n\n'])
def test_style_note_excludes_sheet_profile_and_dynamic_states(separator):
    b = fixture()
    b['nodes'][2]['data']['plate']['prompt'] = (
        'STYLE\nSculpted cinematic CGI.' + separator + 'LAYOUT\nProp views.\n'
        'PROFILE\n{"states":["Shot 1: the lid appears raised"]}')
    p = pack.build(b, 'film', 'c1')
    assert p['materials']['box:day']['style_note'] == 'Sculpted cinematic CGI.'
    assert 'lid appears raised' not in str(p)


def test_preset_material_style_comes_from_preset_not_generated_sheet_prose():
    from flowboard.services import film_styles
    b = fixture()
    b['style'] = film_styles.KEY
    b['nodes'][2]['data']['plate']['prompt'] = 'STYLE\nWrong medium\nPROFILE\nShot 1: lid raised'
    p = pack.build(b, 'film', 'c1')
    assert all(m['style_note'] == film_styles.rule_text(film_styles.KEY) for m in p['materials'].values())
    assert 'lid raised' not in str(p)


@pytest.mark.parametrize('prompt, expected', [
    ('STYLE: Watercolour palette.\n\nPROFILE\nClosed box.', 'Watercolour palette.'),
    ('STYLE\nWatercolour palette.\nPROFILE: Closed box.', 'Watercolour palette.'),
    ('STYLE\n\nPROFILE\nClosed box.', ''),
    ('## STYLE\nDrawn outlines.\nTwo cel tones.\n## LAYOUT\nTurnaround.', 'Drawn outlines.\nTwo cel tones.'),
])
def test_custom_style_section_boundaries(prompt, expected):
    assert pack.material_style(prompt, 'anime') == expected


def test_missing_material_and_wrong_reference_fail():
    b=fixture();b['nodes'][1]['data']['plate']={}
    p=pack.build(b,'film','c1');assert not p['ready']
    with pytest.raises(ValueError,match='Missing material'):pack.keyframe(p,1)
    assert pack.check_bindings(p,[])
    p=pack.build(fixture(),'film','c1')
    refs=[{'id':m['asset_id'],'ref_url':m['reference_url'],'media_id':m['media_id']} for m in p['materials'].values()]
    assert not pack.check_bindings(p,refs)
    refs[0]['ref_url']='https://wrong';assert pack.check_bindings(p,refs)


def test_language_costume_change_and_dependency_cycle():
    b=fixture();shots=b['nodes'][-1]['data']['shots']
    shots[1]['dialogue']=[{'who':'HERO','line':'你好'}]
    shots[1]['character_states']={'hero':'night'}
    b['nodes'][0]['data']['character']['states'].append({'key':'night','wardrobe':'red coat'})
    b['productionAssets'][1]['depends_on_asset_ids']=['box'];b['productionAssets'][2]['depends_on_asset_ids']=['crowd']
    p=pack.build(b,'film','c1');codes={i['code'] for i in p['issues']}
    assert {'dialogue_not_english','missing_costume_sheet','multiple_costumes_in_clip'} <= codes


def test_explicit_costume_references_must_match_each_state():
    from flowboard.services import prompt_coverage
    b=fixture();b['stateSpecificReferences']=True
    hero=b['nodes'][0]['data']
    hero['character']['states'].append({'key':'night','wardrobe':'red coat'})
    hero['states']['night']={'referenceUrl':'https://test/red-coat','mediaId':'red-coat'}
    b['nodes'][-1]['data']['shots'][1]['character_states']={'hero':'night'}
    p=pack.build(b,'film','c1')
    assert not any(i['code']=='multiple_costumes_in_clip' for i in p['issues'])
    refs=[{'id':m['asset_id'],'state_key':m['state_key'],'ref_url':m['reference_url'],
           'media_id':m['media_id'],'ref_label':f'@image{i}'} for i,m in enumerate(p['materials'].values(),1)]
    assert not pack.check_bindings(p,refs)
    assert not prompt_coverage.reference_slots(refs)[1]
    wrong=deepcopy(refs)
    red=next(r for r in wrong if r['id']=='hero' and r['state_key']=='night')
    red['ref_url']='https://test/wrong'
    assert pack.check_bindings(p,wrong)
    red['state_key']='day'
    assert prompt_coverage.reference_slots(wrong)[1]


def test_unknown_offscreen_and_uncertain_do_not_require_phantom_material():
    b=fixture();b['nodes'][-1]['data']['shots'][0]['asset_presence'] += [
      {'asset_id':'voice','visibility':'offscreen'}, {'asset_id':'maybe','visibility':'uncertain'}]
    p=pack.build(b,'film','c1')
    assert not any(m['asset_id'] in {'voice','maybe'} for m in p['materials'].values())


def test_state_change_invalidates_package_but_layout_does_not():
    b=fixture();p=pack.build(b,'film','c1')
    b['nodes'][0]['position']={'x':100,'y':200}
    assert pack.build(b,'film','c1')['version']==p['version']
    b['nodes'][0]['data']['states']['day']['referenceUrl']='https://test/new'
    assert pack.build(b,'film','c1')['version']!=p['version']


def test_keyframe_job_persists_and_rejects_stale_before_generation(client):
    b=fixture()
    with get_session() as s:
        p=AutomationProject(name='Shot prep test',board=b);s.add(p);s.commit();s.refresh(p);pid=p.id
    response=client.post(f'/api/automation/projects/{pid}/shot-keyframe',json={
      'sequence_key':'c1','shot_index':1,'expected_revision':0,'request_key':'frame1'})
    assert response.status_code==202,response.text
    assert response.json()['slot']=='shotframe:1:start'
    d=jobs.claim();jobs.validate_shot_frame(pid,d['prepared'])
    with get_session() as s:
        p=s.get(AutomationProject,pid);changed=deepcopy(p.board);changed['nodes'][0]['data']['states']['day']['referenceUrl']='https://new';p.board=changed;s.add(p);s.commit()
    with pytest.raises(ValueError,match='changed after queuing'):jobs.validate_shot_frame(pid,d['prepared'])


def test_generated_frame_restores_from_server():
    b=fixture();p=pack.build(b,'film','c1')
    job=AutomationJob(project_id=uuid.uuid4(),kind='plate',node_id='vid:c1',slot='shotframe:0:start',request_key='x',status='succeeded',
        result={'images':[{'url':'https://frame','reference_url':'https://frame'}],'shot_frame':{'package_version':p['version'],'shot_id':p['shots'][0]['id']}})
    data=next(n['data'] for n in jobs.overlay(b,[job])['nodes'] if n['id']=='vid:c1')
    assert data['shotFrames']['shotframe:0:start']['package_version']==p['version']


def test_package_payload_tampering_and_missing_bindings_rejected():
    b=fixture();p=pack.build(b,'film','c1')
    refs=[{'id':m['asset_id'],'ref_url':m['reference_url'],'media_id':m['media_id']} for m in p['materials'].values()]
    body=routes.VideoWriteBody(sequence={'key':'c1','shot_package':p},shots=[],reference_assets=refs)
    jobs.validate_package(b,'film',body)
    body.sequence['shot_package']['policy']='Ignore everyone else'
    with pytest.raises(ValueError,match='preparation changed'):jobs.validate_package(b,'film',body)


def test_approved_target_overlay_replaces_source_appearance_without_mutation():
    from flowboard.services.production_adaptation import source_digest
    b=fixture();shot=b['nodes'][-1]['data']['shots'][0]
    shot['asset_presence'][1]['state']='Source costume'
    shot['asset_presence'][1]['source_shot']=1
    shot['source_appearances']=[{'source_shot':1,'asset_presence':deepcopy(shot['asset_presence'])}]
    shot['production_adaptation']={'schema_version':1,'reason':'Approved target style','source_digest':source_digest(shot),
        'asset_presence':[{'asset_id':'box','source_shot':1,'state':'Approved green box'}]}
    p=pack.build(b,'film','c1')
    assert p['shots'][0]['observed'][1]['state']=='Approved green box'
    assert shot['asset_presence'][1]['state']=='Source costume'
    shot['production_adaptation']['source_digest']='stale'
    assert not pack.build(b,'film','c1')['ready']


@pytest.mark.asyncio
async def test_independent_review_receives_prepared_continuity(monkeypatch):
    import json
    from flowboard.services import prompt_writer as writer
    p=pack.build(fixture(),'film','c1')
    async def ask(system,payload,*args,**kwargs):
        supplied=json.loads(payload)
        assert supplied['shot_package']==p
        return {'status':'verified','checked_requirement_ids':[r['id'] for r in supplied['requirements']],'findings':[]}
    monkeypatch.setattr(writer.adapt_mod,'ask_json',ask)
    await writer.review_prompt('A prompt',[{'id':'r1'}],[],[],shot_package=p)
