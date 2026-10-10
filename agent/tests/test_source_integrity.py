from copy import deepcopy
import pytest
from flowboard.services.video_analyzer import dialogue, source_integrity as integrity, board


def shot(n,start,end,caption=''):
    return {'shot':n,'start':start,'end':end,'source':{'action':'A conversation.','shot_size':'CU','subtitle':caption}}


def transcript(text,start=0,step=.2):
    words=[{'word':' '+s,'start':round(start+i*step,3),'end':round(start+(i+1)*step,3)} for i,s in enumerate(text.split())]
    return {'segments':[{'start':words[0]['start'],'end':words[-1]['end'],'text':text,'words':words}]}


def test_missing_punctuation_no_longer_swallows_lilys_reply():
    # Real regression: S99 assigned all three turns to Maria because ASR had no periods.
    captions=['And then you are going to make him understand exactly why he chose you',
        'Oh no Maria it is not like that', 'If it is really not like that give him my number']
    source=transcript(' '.join(captions))
    shots=[];cursor=0
    for n,text in enumerate(captions,1):
        end=round(cursor+len(text.split())*.2,3);shots.append(shot(n,cursor,end,text));cursor=end
    aligned=integrity.align_audio(source,shots)
    lines=dialogue.build_track(shots,aligned,policy='audio_verbatim')
    assert [l.text for l in lines]==captions
    assert [l.first_shot for l in lines]==[1,2,3]
    assert not any(w.get('source_sentence_end') for s in source['segments'] for w in s['words'])


def test_only_overlapping_duplicate_word_removed_real_repetition_survives():
    source=transcript("I'll take care of everything.")
    word=deepcopy(source['segments'][0]['words'][-1])
    source['segments'].append({'start':word['start'],'end':word['end'],'text':word['word'],'words':[word]})
    later=transcript('everything.',2)['segments'][0];source['segments'].append(later)
    out=integrity.align_audio(source,[])
    assert [l.text for l in dialogue.audio_sentences(out,[shot(1,0,5)])]==["I'll take care of everything.",'everything.']
    assert len(out['integrity_alignment']['removed_overlap_tokens'])==1
    assert integrity.align_audio(out,[])==out  # receipts survive a cached recompile


def test_translation_cannot_invent_speech_or_replace_audio():
    source=transcript('Xin chao ban.')
    out=integrity.align_audio(source,[shot(1,0,3,'Hello my friend.')])
    assert out['segments']==source['segments'] and not out['integrity_alignment']['boundaries']
    silent={'shots':[shot(1,0,3,'I am so happy for you.')],'transcript':{'segments':[]}}
    assert 'caption_without_audio' in {f['code'] for f in integrity.findings(silent)}
    assert not dialogue.audio_sentences(silent['transcript'],silent['shots'])


def test_sweet_tweet_conflict_not_silently_corrected_but_3_three_is_not_error():
    shots=[shot(1,0,4,'Sweet of her to run me a bath.')]
    audio=transcript('Tweet of her to run me a bath.')
    assert 'caption_audio_disagreement' in {f['code'] for f in integrity.findings({'shots':shots,'transcript':audio})}
    a={'shots':[shot(1,0,3,'3 seconds. Make your choice.')], 'transcript':transcript('Three seconds. Make your choice.')}
    assert not integrity.findings(a)


def test_old_cut_cache_and_partial_speaker_do_not_pass_as_verified():
    a={'shots':[shot(1,0,3)],'cuts':{'kept':[1]},'transcript':{}}
    inv={'shots':{'1':{'speakers':[{'asset_id':'man','delivery':'on_camera'}],
        'asset_presence':[{'asset_id':'man','visibility':'partial','position':'Shoulder at edge'}]}}}
    codes={f['code'] for f in integrity.findings(a,inv)}
    assert {'cut_measurement_outdated','speaker_mouth_not_established','speaker_coverage'}<=codes


def test_four_shot_target_reduces_complex_requests_without_dropping_speech():
    shots=[shot(i+1,i,i+1) for i in range(12)]
    seq=[{'first_shot':1,'last_shot':12}]
    assert [r['shots'] for r in board.plan_dialogue_clips(shots,seq,[],20,max_shots=4)]==[list(range(1,5)),list(range(5,9)),list(range(9,13))]
    line={'start':3.2,'end':5.8}
    result=board.plan_dialogue_clips(shots,seq,[line],20,max_shots=4)
    assert result[0]['shots']==[1,2,3]
    assert [n for r in result for n in r['shots']]==list(range(1,13))
    assert not any(line['start']<shots[r['shots'][-1]-1]['end']<line['end'] for r in result)


def test_registered_pan_is_distinct_from_an_edit_and_flash_fade(tmp_path):
    import cv2
    import numpy as np
    from flowboard.services.video_analyzer import cuts
    rng=np.random.default_rng(42)
    source=rng.integers(20,230,(320,350),dtype=np.uint8)
    source=cv2.GaussianBlur(source,(5,5),0)
    source=cv2.normalize(source,None,0,255,cv2.NORM_MINMAX)
    # A large moving textured wall/room followed by an unrelated reverse angle.
    frames=[source[:,i*4:i*4+240] for i in range(20)]
    other=rng.integers(0,255,(320,240),dtype=np.uint8)
    frames += [other]*8
    path=tmp_path/'pan.avi'
    writer=cv2.VideoWriter(str(path),cv2.VideoWriter_fourcc(*'MJPG'),30,(240,320))
    for frame in frames:writer.write(cv2.cvtColor(frame,cv2.COLOR_GRAY2BGR))
    writer.release()
    assert cuts.registered_motion(path,10)>.94
    assert cuts.registered_motion(path,20)<.94
    thumbs=np.stack([cv2.resize(f,(64,114)) for f in frames])
    assert not cuts.flash_transition(thumbs,10)
    thumbs[12]=250  # A white transition just after the candidate must survive.
    assert cuts.flash_transition(thumbs,10)


def test_held_caption_is_not_missing_dialogue_on_every_reaction_cut():
    captions='Three seconds. Make your choice.'
    shots=[shot(1,0,2,captions),shot(2,2,3,captions),shot(3,3,4,captions)]
    a={'shots':shots,'transcript':transcript(captions,0,.18)}
    assert not integrity.findings(a)


def test_new_cut_receipt_cannot_bless_old_shot_boundaries():
    a={'shots':[shot(1,0,2),shot(2,2,3)],'cuts':{'measurement_version':3,'kept':[]}}
    assert 'cut_shot_disagreement' in {f['code'] for f in integrity.findings(a)}
