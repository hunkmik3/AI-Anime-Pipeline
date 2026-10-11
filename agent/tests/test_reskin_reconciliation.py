from copy import deepcopy
import pytest
from flowboard.services import reskin_source_review as review, reskin_reconciliation as repair
from tests.test_reskin import observations


def source_shots():
    return [dict(id='c1:1',source_start=0.,source_end=1.,dialogue=[dict(speaker_id='a',delivery='offscreen',line='Hello')]),
            dict(id='c1:2',source_start=1.,source_end=2.,dialogue=[dict(speaker_id='a',delivery='on_camera',line='there')])]


def test_cut_split_delivery_is_checked_per_shot_not_per_whole_utterance():
    rows=source_shots();answer=observations(rows)
    answer['heard_dialogue']=[dict(shot_id=rows[0]['id'],speaker='A',speaker_id='a',delivery='offscreen',text='Hello',start=.2,end=.9),
                              dict(shot_id=rows[1]['id'],speaker='A',speaker_id='a',delivery='on_camera',text='there',start=1.,end=1.5)]
    assert not review.validate(answer,rows,0)
    answer['heard_dialogue'][0]['speaker_id']='b'
    assert review.validate(answer,rows,0)[0]['shot']=='c1:1'


def test_wrong_words_in_another_shot_never_hide_a_speaker_error():
    rows=source_shots();answer=observations(rows)
    answer['heard_dialogue']=[dict(shot_id=rows[0]['id'],speaker='B',speaker_id='b',delivery='offscreen',text='Hello',start=.2,end=.9),
                              dict(shot_id=rows[1]['id'],speaker='A',speaker_id='a',delivery='on_camera',text='you',start=1.,end=1.5)]
    assert {f['shot'] for f in review.validate(answer,rows,0)}=={'c1:1','c1:2'}


def test_rebind_merges_aliases_without_adding_a_second_actor():
    assets={'wrong':{'id':'wrong','kind':'character','name':'Vivian Mason','states':[{'key':'old'}]},
            'right':{'id':'right','kind':'character','name':'Claudia Leech','states':[{'key':'dress'}]}}
    original={'character_keys':['wrong','right'],'character_states':{'wrong':'old','right':'dress'},
              'staging':"Vivian's hand holds the bracelet.",
              'source_appearances':[{'character_states':{'wrong':'old','right':'dress'},'asset_presence':[
                  {'asset_id':'wrong','visibility':'visible','holder_id':'wrong'},
                  {'asset_id':'right','visibility':'offscreen'}]}]}
    before=deepcopy(original)
    revised=repair._rebind(original,{'from':'wrong','to':'right','state':'dress'},assets)
    assert original==before
    assert revised['character_keys']==['right']
    assert revised['character_states']=={'right':'dress'}
    assert len(revised['source_appearances'][0]['asset_presence'])==1
    assert revised['source_appearances'][0]['asset_presence'][0]['visibility']=='visible'
    assert revised['staging']=="Claudia's hand holds the bracelet."
    with pytest.raises(ValueError,match='costume'):
        repair._rebind(original,{'from':'wrong','to':'right','state':'invented'},assets)


def test_unknown_or_different_source_receipt_cannot_apply():
    for record in ({'state':'in_flight','source_sha256':'sha'}, {'state':'completed','source_sha256':'changed'}):
        with pytest.raises(ValueError,match='completed receipt'):
            repair.apply_group({'sourceMediaSha256':'sha'},record,path='proof',expected_findings=[])


def test_review_aggregate_rejects_missing_receipt_instead_of_accepting_empty_findings():
    board={'nodes':[{'data':{'kind':'sequence','sequence':{'key':'clip-1'},'shots':[]}}]}
    with pytest.raises(ValueError,match='no source review receipt'):
        repair.aggregate(board,[],previous={},history=[])


@pytest.mark.asyncio
async def test_successful_source_handoff_clears_old_failure(monkeypatch):
    import uuid
    from flowboard.services import source_film,production_run
    from flowboard.routes import video_analysis
    from flowboard.db import get_session
    from flowboard.db.models import AutomationProject,VideoAnalysis
    with get_session() as s:
        p=AutomationProject(name='retry',title='retry',board={});s.add(p);s.commit();s.refresh(p)
        v=VideoAnalysis(name='source',automation_project_id=p.id,status='failed',error='old review error',
            options={'auto_production':{'settings':source_film.FilmOptions().model_dump()}})
        s.add(v);s.commit();s.refresh(v);vid=v.id
    monkeypatch.setattr(production_run,'start',lambda *a,**k:{'id':str(uuid.uuid4())})
    await source_film.finish_board(source_film.read(vid),source_film.FilmOptions(),{})
    assert source_film.read(vid).status=='adapted'
    assert source_film.read(vid).error is None
    assert source_film.read(vid).options['auto_production']['stage']=='production'
