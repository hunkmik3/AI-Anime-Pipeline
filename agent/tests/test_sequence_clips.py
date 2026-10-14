from copy import deepcopy

import pytest

from flowboard.services.video_analyzer.sequence_clips import plan, boundary_context


def shots(n):
    return [{'shot':i+1, 'start':float(i), 'end':float(i+1)} for i in range(n)]


def ends(clips):
    return [c['shots'][-1] for c in clips]


def test_complete_exchange_wins_over_filling_fifteen_seconds():
    ss = shots(30)
    semantic = {'boundaries':[{'after_shot':12,'kind':'beat_end','reason':'answer and reaction finish'},
                              {'after_shot':21,'kind':'beat_end','reason':'second exchange complete'}]}
    out = plan(ss, [{'first_shot':1,'last_shot':30,'goal':'phone exchange'}], [], 15, semantic=semantic)
    assert ends(out) == [12,21,30]
    assert all(c['sequence_context'][0]['goal']=='phone exchange' for c in out)


def test_sentence_overrides_model_claim_of_sequence_end():
    ss=shots(26)
    out=plan(ss, [], [{'start':10.5,'end':16.5}],15,
             semantic={'boundaries':[{'after_shot':13,'kind':'sequence_end','reason':'incorrect label'}]})
    assert not any(10.5 < e < 16.5 for e in ends(out))
    assert [n for c in out for n in c['shots']]==list(range(1,27))


def test_short_sequence_is_kept_and_long_sequence_uses_shared_context():
    ss=shots(30)
    qs=[{'first_shot':1,'last_shot':6,'goal':'farewell'}, {'first_shot':7,'last_shot':30,'goal':'new arrival'}]
    out=plan(ss,qs,[],15,min_clip_s=12)
    assert out[0]['shots']==list(range(1,7))
    assert all(c['sequence_context'][0]['goal']=='new arrival' for c in out[1:])


def test_no_retiming_no_drop_and_no_impossible_speech_truncation():
    ss=shots(31); original=deepcopy(ss)
    with pytest.raises(ValueError, match='dialogue-safe'):
        plan(ss,[],[{'start':0,'end':31}],15)
    assert ss==original
    with pytest.raises(ValueError, match='scope cuts'):
        plan(ss,[],[{'start':-1,'end':1}],15)


def test_flashback_with_continuous_voice_stays_in_same_request():
    ss=shots(12)
    out=plan(ss,[{'first_shot':1,'last_shot':12}], [{'start':1,'end':11}],15,
             character_states={n:{'hero':'past' if 5<=n<=7 else 'present'} for n in range(1,13)})
    assert ends(out)==[12]


def test_asr_pause_after_comma_does_not_cut_one_question_in_half():
    ss=shots(27)
    lines=[{'text':'In all of DC,','start':10.2,'end':12.4},
           {'text':'besides Lex, who else could match you?','start':13.2,'end':17.8}]
    out=plan(ss,[],lines,15)
    assert not any(10.2 < e < 17.8 for e in ends(out))


def test_missing_sequence_labels_do_not_make_every_shot_a_new_scene():
    out=plan(shots(12),[{'first_shot':i,'last_shot':i,'_coverage_fallback':True} for i in range(1,13)],[],15)
    assert len(out)==1


@pytest.mark.asyncio
async def test_unknown_semantic_call_is_retained_not_billed_again(tmp_path,monkeypatch):
    calls=[]
    async def fail(*args,**kwargs):
        calls.append(1)
        raise TimeoutError()
    monkeypatch.setattr('flowboard.services.video_analyzer.sequence_clips.avis_text.complete',fail)
    a={'shots':shots(12),'sequences':[],'dialogue_track':[]}
    assert (await boundary_context(a,tmp_path))['degraded']
    assert (await boundary_context(a,tmp_path))['degraded']
    assert len(calls)==1


@pytest.mark.asyncio
async def test_board_and_graph_carry_sequence_context_without_changing_shots():
    from flowboard.services.video_analyzer import board
    from flowboard.services import source_film
    ss=[{**s,'source':{'shot_size':'MS','action':'Waits.'}} for s in shots(24)]
    a={'shots':ss,'video':{'duration':24},'dialogue_track':[],
       'source_verification':{'method':'one_pass_production'},
       'sequences':[{'first_shot':1,'last_shot':24,'title':'One conversation','goal':'Resolve disagreement'}]}
    sem={'boundaries':[{'after_shot':12,'kind':'beat_end','reason':'Question and answer finish'}]}
    out=await board.build_board(a,{},cast={},max_clip_s=15,hard_limit_s=15,sequence_boundaries=sem)
    graph=source_film.graph(out,source_film.FilmOptions().model_dump())
    nodes=[n['data']['sequence'] for n in graph['nodes'] if n['id'].startswith('seq:')]
    assert len(nodes)==2
    assert all(n['sequence_context'][0]['goal']=='Resolve disagreement' for n in nodes)
    assert nodes[0]['boundary_plan']['cut_reason']=='Question and answer finish'
    assert [s['source_shot'] for group in out['shots'].values() for s in group]==list(range(1,25))
