from copy import deepcopy
from pathlib import Path
import pytest
from flowboard.services.video_analyzer import joint_observation as joint, source_inventory as inv


def reply(n=1):
    return {'source_shots':[{'shot':n,'start':999,'end':1000,'shot_size':'CU','camera_angle':'eye-level',
        'camera_movement':'static','action':'Woman holds a cup.'}],
      'assets':[{'id':'woman','kind':'character','name':'Woman','description':'brown hair',
        'evidence_ids':[f'shot-{n}-frame-1']},{'id':'cup','kind':'prop','name':'Cup',
        'description':'white cup','evidence_ids':[f'shot-{n}-frame-1']}],
      'scenes':[{'id':'room','shot_ids':[n],'present_asset_ids':['woman','cup']}],
      'shots':{str(n):{'scene_id':'room','evidence_ids':[f'shot-{n}-frame-1'],
        'asset_presence':[{'asset_id':'woman','visibility':'visible','evidence_ids':[f'shot-{n}-frame-1']},
          {'asset_id':'cup','visibility':'visible','holder_id':'woman','contains_ids':[],
           'evidence_ids':[f'shot-{n}-frame-1']}]}},'review_requests':[]}


def test_reader_cannot_overwrite_timeline_or_omit_shots():
    got=joint.validate_sources(reply(),[{'shot':1}])
    assert 'start' not in got[1] and 'end' not in got[1]
    with pytest.raises(ValueError):joint.validate_sources(reply(),[{'shot':1},{'shot':2}])
    broken=reply();broken['source_shots']*=2
    with pytest.raises(ValueError):joint.validate_sources(broken,[{'shot':1}])
    qualified=reply();qualified['source_shots'][0]['camera_angle']='slightly low angle'
    assert joint.validate_sources(qualified,[{'shot':1}])[1]['camera_angle']=='slightly low angle'


def test_local_names_do_not_assert_identity_across_batches():
    original=reply();before=deepcopy(original)
    first=joint.namespace_draft(original,1);second=joint.namespace_draft(original,7)
    assert first['assets'][0]['id'] != second['assets'][0]['id']
    assert first['shots']['1']['asset_presence'][1]['holder_id']=='j1-woman'
    assert first['shots']['1']['scene_id']=='j1-room'
    assert original==before


def test_namespaced_long_ids_remain_valid_without_collisions():
    a='a'*96;b='a'*95+'b'
    got=joint.namespace_draft({'assets':[{'id':a},{'id':b}], 'shots':{'1':{'asset_presence':[
        {'asset_id':a,'holder_id':b}]}},'scenes':[]},123)
    ids=[x['id'] for x in got['assets']]
    assert len(set(ids))==2 and all(len(x)<=96 for x in ids)
    assert got['shots']['1']['asset_presence'][0]['holder_id']==ids[1]


@pytest.mark.asyncio
async def test_failed_batch_does_not_cancel_successful_siblings(tmp_path,monkeypatch):
    import asyncio
    from tests.test_source_inventory import _source
    _,work,shots=_source(tmp_path)
    monkeypatch.setattr(inv,'BATCH_SIZE',1)
    saved=[]
    async def stage(entry,name,system,payload,*args,**kw):
        n=payload['source_shots'][0]['shot']
        if n==1:raise RuntimeError('one failed batch')
        await asyncio.sleep(.01)
        saved.append(n)
        return reply(n)
    monkeypatch.setattr(inv,'_stage_call',stage)
    with pytest.raises(ValueError,match='successful sibling batches saved'):
        await joint.analyze(shots,work)
    assert saved==[2]


@pytest.mark.asyncio
async def test_reuse_skips_extraction_but_not_validation_or_review(tmp_path,monkeypatch):
    calls=[]
    async def no_call(*a,**kw):calls.append(a);raise AssertionError('Repeated extraction')
    monkeypatch.setattr(inv,'_stage_call',no_call)
    batch=[{'shot':1,'start':0,'end':1}];cards=[{'id':'shot-1-frame-1','shot':1}]
    entry={'trace':[],'usage':{},'calls':{},'frame_calls':{}}
    draft=joint.namespace_draft(reply(),1)
    result=await inv._observe_batch(entry,batch,inv._empty(),{},cards,Path('video'),tmp_path,30,
        {'remaining':0},None,lambda:None,preobserved={'draft':draft,'fingerprint':'f','evidence_ids':['shot-1-frame-1']})
    assert not calls and not result['issues']
    assert 'result' not in entry and 'reviewed_shots' not in result
    broken=deepcopy(draft);broken['shots']['1']['asset_presence'][0]['evidence_ids']=['foreign']
    entry={'trace':[],'usage':{},'calls':{},'frame_calls':{}}
    result=await inv._observe_batch(entry,batch,inv._empty(),{},cards,Path('video'),tmp_path,30,
        {'remaining':0},None,lambda:None,preobserved={'draft':broken,'fingerprint':'g','evidence_ids':['shot-1-frame-1']})
    assert result['issues']


@pytest.mark.asyncio
async def test_batch_cache_reuses_siblings_when_one_input_changes(tmp_path,monkeypatch):
    from PIL import Image
    (tmp_path/'frames').mkdir()
    shots=[]
    for n in (1,7):
        for i in (1,2,3):Image.new('RGB',(10,10),'blue').save(tmp_path/f'frames/shot{n:03}_{i}.jpg')
        shots.append({'shot':n,'start':float(n),'end':float(n+2),'dialogue':'Hello',
                      'frames':[f'frames/shot{n:03}_{i}.jpg' for i in (1,2,3)]})
    monkeypatch.setattr(inv,'BATCH_SIZE',1)
    calls=[]
    async def fake(entry,stage,system,payload,cards,work,limiter,save,**kwargs):
        cached=entry.setdefault('calls',{}).setdefault(stage,{})
        if 'output' not in cached:
            n=payload['source_shots'][0]['shot'];calls.append(n);cached['output']=reply(n);save()
        return cached['output']
    monkeypatch.setattr(inv,'_stage_call',fake)
    await joint.analyze(shots,tmp_path,fps=30)
    shots[1]['dialogue']='Changed'
    await joint.analyze(shots,tmp_path,fps=30)
    assert calls.count(1)==1 and calls.count(7)==2
