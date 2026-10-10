from copy import deepcopy
import hashlib
import json
import pytest
from flowboard.services import production_quality as quality, production_media as media


def package():
    return {'version':'v1','style':'realistic','materials':{},'scene_anchors':{},'dialogue_language':'en',
        'shots':[{'id':'shot1','duration_s':1,'dialogue':[{'who':'Man','line':'Take it anyway.'}]}]}


def audio(text='Take it anyway.',end=.8):
    return {'segments':[{'start':0,'end':end,'text':text}]}


def test_missing_added_and_wrong_dialogue_flagged_in_actual_output():
    p=package()
    assert not quality.audio_check(p['shots'],audio(),1)['findings']
    for heard in ['', 'Take it.', 'Take it anyway. Take it anyway.', 'That is what the contract says.']:
        result=quality.audio_check(p['shots'],audio(heard),1)
        assert any(f['code']=='audio_text_disagreement' for f in result['findings'])
    assert quality.audio_check(p['shots'],{},1,False)['findings'][0]['code']=='missing_audio_stream'


def test_silent_shots_do_not_authorize_invented_dialogue():
    p=package();p['shots'][0]['dialogue']=[]
    assert not quality.audio_check(p['shots'],{},1,False)['findings']
    assert quality.audio_check(p['shots'],audio(),1)['findings']


def test_visual_review_requires_all_shots_and_actual_evidence():
    p=package();frames={'F1':0,'F2':.5}
    row={'status':'pass','note':'Seen in the actual frame.','frames':['F1'],'observed_start_s':0.0}
    answer={'shots':[{'id':'shot1',**row}],'continuity':deepcopy(row)}
    assert not quality.validate_visual(answer,p,frames)
    changed=deepcopy(answer);changed['shots']=[]
    with pytest.raises(ValueError,match='omitted'):quality.validate_visual(changed,p,frames)
    changed=deepcopy(answer);changed['shots'][0]['frames']=['F99']
    with pytest.raises(ValueError,match='foreign'):quality.validate_visual(changed,p,frames)
    answer['shots'][0]['status']='unclear'
    assert quality.validate_visual(answer,p,frames)[0]['code']=='visual_output_unclear'


def test_reused_output_receipt_must_match_actual_job_and_current_package():
    clip={'job_id':'j','url':'https://test/clip'}
    receipt={'version':quality.VERSION,'status':'checks_passed','source_job':'j','url':clip['url'],
        'package_version':'v1','media_sha256':'abc'}
    quality.require_pass(receipt,clip,'v1')
    for key,value in [('source_job','other'),('package_version','v2'),('status','needs_review'),('media_sha256','')]:
        with pytest.raises(ValueError):quality.require_pass({**receipt,key:value},clip,'v1')


def test_finding_correct_shot_later_does_not_pass_locked_cut_timing():
    p=package();p['shots'].append({'id':'shot2','duration_s':1,'dialogue':[]})
    frames={'F1':0.,'F2':.5,'F3':1.5,'F4':1.9}
    row={'status':'pass','note':'The requested angle appears.','frames':['F1'],'observed_start_s':0.}
    answer={'shots':[{'id':'shot1',**row},{'id':'shot2',**row,'frames':['F3'],'observed_start_s':1.5}],
        'continuity':row}
    findings=quality.validate_visual(answer,p,frames)
    assert findings==[{'code':'generated_cut_timing','id':'shot2',
        'message':'Observed shot starts at a different time than the locked cut.',
        'expected_start_s':1.,'observed_start_s':1.5}]


@pytest.mark.asyncio
async def test_audio_failure_saves_receipt_and_spends_no_visual_call(tmp_path,monkeypatch):
    from flowboard.services.video_analyzer import asr
    monkeypatch.setattr(quality,'STORAGE_DIR',tmp_path)
    async def fetch(url,path):path.write_bytes(b'test clip bytes')
    async def probe(path):return {'streams':[{'codec_type':'video'},{'codec_type':'audio'}], 'format':{'duration':'1'}}
    async def command(*args):return b''
    async def transcribe(*args,**kwargs):return audio('Take it.')
    async def no_review(*args,**kwargs):raise AssertionError('Unnecessary paid review')
    monkeypatch.setattr(media,'fetch',fetch);monkeypatch.setattr(media,'probe',probe);monkeypatch.setattr(media,'command',command)
    monkeypatch.setattr(asr,'transcribe',transcribe);monkeypatch.setattr(quality,'visual_review',no_review)
    payload={'source_job':'job','url':'https://test/clip','package':package()}
    out=await quality.inspect(payload)
    assert out['status']=='needs_review' and out['visual'] is None
    assert out['media_sha256']==hashlib.sha256(b'test clip bytes').hexdigest()
    assert await quality.inspect(payload)==out


@pytest.mark.asyncio
async def test_trim_cannot_cut_a_spoken_tail(tmp_path,monkeypatch):
    from flowboard.services.video_analyzer import asr
    monkeypatch.setattr(quality,'STORAGE_DIR',tmp_path)
    async def fetch(url,path):path.write_bytes(b'media')
    async def probe(path):return {'streams':[{'codec_type':'video'},{'codec_type':'audio'}], 'format':{'duration':'2'}}
    async def command(*args):return b''
    async def transcribe(*args,**kwargs):return audio(end=1.8)
    monkeypatch.setattr(media,'fetch',fetch);monkeypatch.setattr(media,'probe',probe);monkeypatch.setattr(media,'command',command)
    monkeypatch.setattr(asr,'transcribe',transcribe)
    result=await quality.inspect({'source_job':'j','url':'https://test/clip','package':package(),'timing_policy':'source_duration'})
    assert any(f['code']=='assembly_would_cut_speech' for f in result['findings'])


def test_failed_quality_blocks_assembly_and_does_not_resubmit_video():
    from tests import test_production_run as h
    pid=h.project();run=h.start(pid,mode='render')
    for _ in range(15):
        h.tick(run['id'])
        waiting=h.pending(pid)
        q=next((j for j in waiting if j.kind=='clip_quality'),None)
        if q:break
        for job in waiting:h.finish(job)
    assert q
    h.complete(q,{'status':'needs_review','findings':[{'code':'audio_text_disagreement','message':'Reply missing.'}]})
    h.tick(run['id'])
    result=h.get(run['id'])
    assert result.status=='blocked' and 'Reply missing' in result.error
    assert not any(j.kind=='assemble' for j in h.all_jobs(pid))
    clips=[j for j in h.all_jobs(pid) if j.kind=='clip']
    assert len(clips)==1 and clips[0].status=='succeeded' and clips[0].result['url']
    # Starting another run must reuse this same media and failed inspection.
    retry=h.start(pid,mode='render');h.tick(retry['id'])
    assert h.get(retry['id']).status=='blocked'
    assert len([j for j in h.all_jobs(pid) if j.kind=='clip'])==1


@pytest.mark.asyncio
@pytest.mark.parametrize('listening_result', ['present', 'unavailable'])
async def test_correct_words_do_not_clear_music_or_missing_listening(tmp_path,monkeypatch,listening_result):
    from flowboard.services import production_audio
    from flowboard.services.video_analyzer import asr
    monkeypatch.setattr(quality,'STORAGE_DIR',tmp_path)
    async def fetch(url,path):path.write_bytes(b'generated media')
    async def probe(path):return {'streams':[{'codec_type':'video'},{'codec_type':'audio'}], 'format':{'duration':'1'}}
    async def command(*args):return b''
    async def transcribe(*args,**kwargs):return audio()
    async def listen(*args):
        if listening_result=='unavailable':raise ValueError('Provider did not complete')
        return {'speech':[{'speaker':'voice1','text':'Take it anyway.'}],
                'background_music':'present','uncertain_spans':[], 'notes':'Instrumental score.'}
    async def no_visual(*args):raise AssertionError('Do not spend on visual checks when audio failed')
    monkeypatch.setattr(media,'fetch',fetch);monkeypatch.setattr(media,'probe',probe);monkeypatch.setattr(media,'command',command)
    monkeypatch.setattr(asr,'transcribe',transcribe);monkeypatch.setattr(production_audio,'review',listen)
    monkeypatch.setattr(quality,'visual_review',no_visual)
    out=await quality.inspect({'source_job':'new','url':'https://test/clip','package':package()})
    assert out['status']=='needs_review'
    code='background_music_present' if listening_result=='present' else 'audio_listening_unavailable'
    assert any(f['code']==code for f in out['findings'])
    with pytest.raises(ValueError):quality.require_pass(out,{'job_id':'new','url':'https://test/clip'},'v1')
