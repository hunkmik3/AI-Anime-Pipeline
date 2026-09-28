from copy import deepcopy
from io import BytesIO
import uuid
import pytest
from PIL import Image
from flowboard.services import reference_atlas as atlas, production_run as run, automation_jobs as jobs
from flowboard.db import get_session
from flowboard.db.models import AutomationJob
from tests.test_production_run import fixture,project,start,tick,pending,finish,complete,get,config


def large_board():
    b=fixture()
    for i in range(10):
        aid=f'prop-{i}'
        b['productionAssets'].append({'id':aid,'kind':'prop'})
        b['nodes'].append({'id':'asset:'+aid,'data':{'kind':'asset','asset':{'key':aid,'id':aid,'kind':'prop','name':aid},'plate':{'referenceUrl':'https://test/'+aid,'mediaId':aid}}})
        b['nodes'][5]['data']['shots'][0]['asset_presence'].append({'asset_id':aid,'visibility':'visible'})
    return b


def test_plan_respects_slot_budget_and_preserves_character_sheet():
    p=run.shot_package.build(large_board(),'film','c1');packing=atlas.plan(p['materials'],2)
    assert packing['source_images']==14 and packing['transport_images']<=7
    keys=[]
    for page in packing['pages']:
        assert 2<=len(page['cells'])<=4
        for cell in page['cells']:
            assert not any(b['kind']=='character' for b in cell['bindings'])
            keys += [b['material_key'] for b in cell['bindings']]
    assert len(keys)==len(set(keys))
    assert atlas.plan(p['materials'],2)==packing
    with pytest.raises(ValueError):atlas.plan(p['materials'],8)


def test_atlas_geometry_preserves_complete_sources_without_cropping():
    materials={f'm{i}':{'asset_id':f'a{i}','name':f'A{i}','kind':'prop','reference_url':f'https://{i}'} for i in range(10)}
    page=atlas.plan(materials)['pages'][0]
    blobs=[]
    for color in ('red','blue')[:len(page['cells'])]:
        image=Image.new('RGB',(200,400),color);blob=BytesIO();image.save(blob,format='PNG');blobs.append(blob.getvalue())
    assert len(page['cells'])==2
    result=Image.open(BytesIO(atlas.compose(page,blobs)))
    assert result.size==(3072,1632)
    assert result.getpixel((768,864))==(255,0,0)
    assert result.getpixel((2304,864))==(0,0,255)
    assert result.getpixel((50,864))==(246,246,246)


def test_ready_coreference_cycle_is_not_a_generation_prerequisite():
    b=fixture();b['productionAssets'][1]['depends_on_asset_ids']=['box'];b['productionAssets'][2]['depends_on_asset_ids']=['crowd']
    preview=run.preview(b,'film',config());assert preview['ready']
    assert preview['dependency_notes'][0]['code']=='existing_coreference_group'
    specs=run.material_specs(b,run.selected_packages(b,'film',[]))
    assert specs['asset:box:plate']['deps']==[] and specs['asset:crowd:plate']['deps']==[]
    b['productionAssets'][1]['generation_depends_on_asset_ids']=['box']
    assert not run.preview(b,'film',config())['ready']


def test_auto_atlas_run_receipts_and_tampering():
    pid=project(large_board());a=start(pid)
    for _ in range(12):
        tick(a['id'])
        for j in pending(pid):
            if j.kind=='atlas':complete(j,{'version':j.payload['version'],'cells':j.payload['cells'],'url':'https://atlas/'+str(j.id),'media_id':str(j.id)})
            elif j.kind!='write':finish(j)
        writers=[j for j in pending(pid) if j.kind=='write']
        if writers:break
    else:raise AssertionError(get(a['id']).error)
    writer=writers[0]
    from flowboard.routes.automation import VideoWriteBody
    body=VideoWriteBody.model_validate(writer.payload)
    assert body.sequence['reference_atlases']
    refs=[*body.characters,body.environment,*body.reference_assets]
    assert len(run.prompt_coverage.reference_slots(refs)[0])<=9
    jobs.validate_writing(pid,body)
    changed=body.model_copy(deep=True);changed.sequence['reference_atlases'][0]['result']['url']='https://fake'
    with pytest.raises(ValueError,match='modified'):jobs.validate_writing(pid,changed)
    changed=body.model_copy(deep=True)
    cell=next(r for r in changed.reference_assets if r.get('atlas_cell'));cell['atlas_cell']='CELL 99'
    with pytest.raises(ValueError,match='label changed'):jobs.validate_writing(pid,changed)
    other=project(large_board())
    with pytest.raises(ValueError,match='this project'):atlas.verify(other,body.sequence['shot_package'],body.sequence['reference_atlases'],refs)
    finish(writer);tick(a['id']);assert get(a['id']).status=='succeeded',get(a['id']).error


def test_redundant_reference_tags_are_normalized_without_changing_binding():
    from flowboard.services.prompt_writer import normalize_reference_mentions
    refs=[('@image1','AVA'),('@image2','HALL')]
    prompt='@image1 — AVA. Same identity as @image1.\n@image2 — HALL.\nKeep her wardrobe from @image1 in @image2.'
    normalized=normalize_reference_mentions(prompt,refs)
    assert normalized.count('@image1')==1 and normalized.count('@image2')==1
    assert 'Same identity as reference image 1.' in normalized
    assert normalized.endswith('Keep her wardrobe from reference image 1 in reference image 2.')
    duplicate='@image1 — AVA.\n@image1 — AVA.'
    assert normalize_reference_mentions(duplicate,refs)==duplicate
    assert normalize_reference_mentions('@image3 — UNKNOWN',refs)=='@image3 — UNKNOWN'


def test_nested_sheet_coordinates_survive_outer_atlas_binding():
    b=fixture()
    box=next(n for n in b['nodes'] if n['id']=='asset:box')
    box['data']['asset']['target_description']='CELL 4: small green box, distinct from the other objects on this sheet.'
    package=run.shot_package.build(b,'film','c1')
    key,m=next((k,m) for k,m in package['materials'].items() if m['asset_id']=='box')
    before=deepcopy(package)
    receipt={'result':{'url':'https://outer-atlas','media_id':'outer-media','cells':[
        {'label':'CELL 2','position':'top right','url':m['reference_url'],
         'bindings':[{'material_key':key,'asset_id':'box'}]}]}}
    body=run.writing_body(b,'film',package,[],[receipt])
    ref=next(r for r in body.reference_assets if r['id']=='box')
    assert ref['atlas_cell']=='CELL 2'
    assert 'CELL 4: small green box' in ref['description']
    assert 'INNER sheet coordinates' in ref['description']
    assert 'OUTER TRANSPORT ATLAS: CELL 2' in ref['description']
    assert ref['ref_url']=='https://outer-atlas'
    assert body.sequence['shot_package']==before==package
