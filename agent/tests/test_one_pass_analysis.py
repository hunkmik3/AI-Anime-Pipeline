from copy import deepcopy
from types import SimpleNamespace
import json

import pytest

from flowboard.services import avis_text
from flowboard.services.video_analyzer import one_pass, dialogue, pipeline, vision, adapt, asr
from flowboard.services.video_analyzer.asr_multilingual_worker import language_runs, owned_segments
from flowboard.services.video_analyzer.frames import ShotSpan


def observation(n=1):
    return {'shot':n,'shot_size':'MS','camera_angle':'high angle','camera_movement':'unclear',
        'camera_crop':'head to thighs','camera_elevation':'looking down','composition':'OTS',
        'action':'looks back','subtitle':'last line only',
        'subtitle_events':[{'text':'Câu thứ nhất.','first_frame':f'S{n}_F1','last_frame':f'S{n}_F1'},
                           {'text':'Câu thứ hai.','first_frame':f'S{n}_F2','last_frame':f'S{n}_F2'}]}


def test_complete_captions_are_kept_and_foreign_evidence_fails():
    span=ShotSpan(1,0,2,[None,None]);data=observation();before=deepcopy(data)
    result=one_pass.normalize([data],[span])[1]
    assert result['subtitle']=='Câu thứ nhất. | Câu thứ hai.'
    assert result['camera_crop']=='head to thighs' and data==before
    data['subtitle_events'][0]['first_frame']='S2_F1'
    with pytest.raises(ValueError,match='caption evidence'):one_pass.normalize([data],[span])
    with pytest.raises(ValueError,match='omitted'):one_pass.normalize([],[span])


def test_audio_wins_over_translated_caption_and_repetition_survives():
    shots=[{'shot':i,'start':i-1,'end':i,'source':{'subtitle':'Chia tay đi.'}} for i in range(1,4)]
    words=[{'start':.1,'end':.4,'word':" Let's"},{'start':.5,'end':1.2,'word':' go.'},
           {'start':1.5,'end':1.7,'word':" Let's"},{'start':1.8,'end':2.2,'word':' go.'}]
    transcript={'segments':[{'start':.1,'end':2.2,'text':"Let's go. Let's go.",'words':words}]}
    track=dialogue.build_track(shots,transcript,policy='audio_verbatim')
    assert [(l.text,l.first_shot,l.last_shot) for l in track]==[("Let's go.",1,2),("Let's go.",2,3)]
    analysis={'shots':shots,'transcript':transcript,'dialogue_policy':'audio_verbatim'}
    pipeline.ensure_dialogue(analysis)
    assert all(l['source']=='asr' for l in analysis['dialogue_track'])
    assert dialogue.build_track(shots,{},policy='audio_verbatim')==[]


def test_audio_decoder_window_does_not_drop_or_repeat_sentence():
    shots=[{'shot':1,'start':0,'end':4},{'shot':2,'start':4,'end':8}]
    data={'segments':[{'language':'en','words':[{'start':3,'end':3.7,'word':' Go'}]},
                      {'language':'en','words':[{'start':4,'end':4.3,'word':' now.'}]}]}
    track=dialogue.build_track(shots,data,policy='audio_verbatim')
    assert len(track)==1 and track[0].text=='Go now.' and track[0].last_shot==2


def test_language_switch_is_not_locked_to_intro_and_low_confidence_is_ignored():
    probes=[{'center':2.5,'language':'vi','probability':.99},
            {'center':7.5,'language':'vi','probability':.99},
            {'center':9,'language':'fr','probability':.3},
            {'center':12.5,'language':'en','probability':.98}]
    assert language_runs(probes,30)==[{'start':0.,'end':10.,'language':'vi'}, {'start':10.,'end':30,'language':'en'}]


def test_overlap_word_ownership_preserves_every_word_once():
    words=[SimpleNamespace(start=1.8,end=2.2,word=' one',probability=.9),
           SimpleNamespace(start=2.3,end=2.5,word=' two.',probability=.9)]
    seg=SimpleNamespace(words=words,avg_logprob=-.1,no_speech_prob=.01)
    first=owned_segments([seg],0,2,0,'en');second=owned_segments([seg],2,4,0,'en')
    assert first==[] and ''.join(w['word'] for s in second for w in s['words'])==' one two.'


@pytest.mark.asyncio
async def test_successful_batch_reused_and_unknown_request_not_repeated(tmp_path,monkeypatch):
    from PIL import Image
    p=tmp_path/'a.png';Image.new('RGB',(8,8)).save(p)
    span=ShotSpan(1,0,2,[p,p]);calls=[]
    async def complete(model,messages,**kw):
        calls.append(kw)
        return avis_text.Completion(text=json.dumps([observation()]),model=model)
    monkeypatch.setattr(avis_text,'complete',complete)
    _,stats,_=await one_pass.analyze([span],tmp_path)
    _,_,metadata=await one_pass.analyze([span],tmp_path)
    assert len(calls)==1 and calls[0]['attempts']==1 and metadata['cache_hits']==1
    assert stats.escalated==0
    journal=next((tmp_path/'one-pass-vision').glob('*.json'))
    data=json.loads(journal.read_text());data['state']='in_flight';journal.write_text(json.dumps(data))
    with pytest.raises(RuntimeError,match='no automatic paid retry'):await one_pass.analyze([span],tmp_path)
    assert len(calls)==1


@pytest.mark.asyncio
async def test_one_pass_pipeline_has_no_hidden_review_and_legacy_paths_unchanged(tmp_path,monkeypatch):
    async def measure(*a,**kw):return {'video':{'duration':2,'fps':30,'width':480,'height':480,'has_audio':True},
        'spans':[{'index':1,'start':0,'end':2,'frames':[]}],'cuts':{},'timings_s':{}}
    seen=[]
    async def speech(*a,**kw):seen.append(kw);return {'segments':[{'start':.2,'end':1.2,'text':'Original English.'}]}
    async def observe(*a,**kw):return {1:{'shot_size':'MS','action':'speaks','subtitle':'Tiếng Việt.'}},vision.VisionStats(),{}
    async def story(*a,**kw):return [{'first_shot':1,'last_shot':1}],{},adapt.TextStats(),[]
    async def forbidden(*a,**kw):raise AssertionError('Hidden verifier/tier fallback')
    monkeypatch.setattr(pipeline,'_measure',measure);monkeypatch.setattr(pipeline,'_speech',speech)
    monkeypatch.setattr(one_pass,'analyze',observe);monkeypatch.setattr(pipeline,'_story',story)
    monkeypatch.setattr(pipeline.source_inventory,'analyze',forbidden);monkeypatch.setattr(vision,'analyze',forbidden)
    result=await pipeline.analyze(tmp_path/'source.mp4',tmp_path,analysis_mode='one_pass')
    assert seen==[{'multilingual':True}]
    assert result['dialogue_policy']=='audio_verbatim' and result['shots'][0]['dialogue']=='Original English.'
    assert result['source_verification']['status']!='verified'
    assert not result.get('source_refinement_history')


def test_one_pass_exposes_explicit_production_capability():
    from flowboard.routes.video_analysis import analysis_capabilities
    caps=analysis_capabilities()
    assert 'one_pass' in caps['analysis_modes'] and not caps['one_pass_analysis_only']
    assert caps['one_pass_auto_production']


@pytest.mark.asyncio
async def test_multilingual_cache_bound_to_video_bytes_and_language(tmp_path,monkeypatch):
    video=tmp_path/'source.mp4';video.write_bytes(b'original')
    calls=[]
    monkeypatch.setattr(asr,'extract_audio',lambda video,out:out)
    async def transcribe(audio,work,**kwargs):
        calls.append(kwargs);return {'segments':[],'method':'language_runs_v1'}
    monkeypatch.setattr(asr,'transcribe',transcribe)
    first=await pipeline._speech(video,tmp_path,None,multilingual=True)
    second=await pipeline._speech(video,tmp_path,None,multilingual=True)
    assert not first['cache_reused'] and second['cache_reused'] and len(calls)==1
    await pipeline._speech(video,tmp_path,'en',multilingual=True)
    video.write_bytes(b'different')
    await pipeline._speech(video,tmp_path,'en',multilingual=True)
    assert len(calls)==3
    assert not (tmp_path/'transcript.json').exists()


@pytest.mark.asyncio
async def test_one_pass_upload_still_validates_film_settings_before_file_or_db_write():
    from flowboard.routes.video_analysis import upload
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await upload(file=SimpleNamespace(filename='source.mp4'),analysis_mode='one_pass',auto_production='{"style":"invalid"}', rules='', user=None)
    assert exc.value.status_code==422
