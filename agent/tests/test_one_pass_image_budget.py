import pytest
from flowboard.services.video_analyzer.one_pass_film import catalog_batch


def test_dynamic_batches_keep_every_shot_frame_and_identity_anchor():
    shots=[{'shot':i} for i in range(1,25)]
    evidence=[{'id':f's{i}f{j}','shot':i} for i in range(1,25) for j in range(3)]
    known={'assets':[{'evidence_ids':[e['id']]} for e in evidence[:20]]}
    offset=12
    seen=[]
    while offset<len(shots):
        batch, supplied=catalog_batch(shots,offset,12,known,evidence)
        assert len(supplied)<=50
        assert {e['id'] for e in evidence[:20]} <= {e['id'] for e in supplied}
        assert all(e in supplied for e in evidence if e['shot'] in {s['shot'] for s in batch})
        seen.extend(s['shot'] for s in batch)
        offset+=len(batch)
    assert seen==list(range(13,25))


def test_too_large_single_shot_fails_without_discarding_evidence():
    with pytest.raises(ValueError,match='complete shot'):
        catalog_batch([{'shot':1}],0,12,{'assets':[]},
                      [{'id':str(i),'shot':1} for i in range(51)])


def test_long_film_retrieves_bounded_anchors_without_losing_current_shot_frames():
    evidence=[{'id':f's{i}f{j}','shot':i} for i in range(1,301) for j in range(3)]
    known={'assets':[{'id':f'a{i}','kind':'prop','evidence_ids':[f's{i}f0']} for i in range(1,145)],
           'shots':{'144':{'asset_presence':[{'asset_id':'hero','visibility':'visible'}]}}}
    known['assets'].append({'id':'hero','name':'Hero','kind':'character','evidence_ids':['s1f1']})
    shots=[{'shot':i,'source':{'action':'Hero walks'}} for i in range(1,301)]
    offset=144
    while offset<len(shots):
        batch,supplied=catalog_batch(shots,offset,12,known,evidence)
        ns={s['shot'] for s in batch};sent={e['id'] for e in supplied}
        assert len(supplied)<=50 and 's1f1' in sent
        assert all(e['id'] in sent for e in evidence if e['shot'] in ns)
        assert all(e['shot']<=max(ns) for e in supplied)
        offset+=len(batch)


def test_a_large_complete_shot_takes_priority_over_historical_anchors():
    old=[{'id':f'old{i}','shot':1} for i in range(51)]
    current=[{'id':f'new{i}','shot':2} for i in range(50)]
    known={'assets':[{'evidence_ids':[e['id']]} for e in old]}
    batch,supplied=catalog_batch([{'shot':2}],0,12,known,old+current)
    assert batch==[{'shot':2}] and supplied==current


def test_grid_keeps_source_pixels_at_original_scale_and_all_labels(tmp_path):
    from PIL import Image, ImageChops
    from flowboard.services.video_analyzer.one_pass_film import catalog_frame_grids
    cards=[]
    for n in range(5):
        p=tmp_path/f'f{n}.png'
        Image.new('RGB',(40,60),(n*40,20,200)).save(p)
        cards.append(({'id':f'shot-{n}-frame-1'},p))
    out=catalog_frame_grids(cards,tmp_path/'grid')
    assert [label for labels,_ in out for label in labels]==[e['id'] for e,_ in cards]
    for i,(_,path) in enumerate(cards):
        grid=Image.open(out[i//4][1]);j=i%4
        crop=grid.crop(((j%2)*40,(j//2)*88+28,(j%2)*40+40,(j//2)*88+88))
        assert ImageChops.difference(crop,Image.open(path)).getbbox() is None
