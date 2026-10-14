"""Automatic repair must retain evidence and never repeat ambiguous paid work."""
from copy import deepcopy
import json
import httpx
import pytest
from flowboard.services import reskin, reskin_source_repair as repair
from flowboard.services.reskin_reconciliation import LOCK_FIELDS
from tests.test_reskin import fixture, observations


def source_board(monkeypatch,tmp_path):
    b=fixture();source=tmp_path/'source.mp4';source.write_bytes(b'original-source')
    monkeypatch.setattr(reskin,'source_path',lambda _:source)
    b['sourceMediaSha256']=reskin.sha256(source)
    seq=b['nodes'][-1]['data'];seq['sequence']['key']='c1'
    b['sourceVerification'].update(locked_shots={},shot_digests={})
    for i,s in enumerate(seq['shots']):
        s['source_shot']=i+1
        for f in LOCK_FIELDS:
            default = {} if f == 'character_states' else [] if f in ('action','dialogue','performance') else ''
            s.setdefault(f, default)
        s['source_appearances']=[{'source_shot':i+1,'asset_presence':deepcopy(s['asset_presence']), 'speakers':[]}]
        s['dialogue']=[];s['performance']=[]
    seq['shots'][0]['dialogue']=[dict(who='HERO',speaker_id='hero',delivery='offscreen',line='Wrong words')]
    obs=observations(seq['shots']);obs['heard_dialogue']=[dict(shot_id='c1:1',speaker_id='hero',speaker='HERO',delivery='offscreen',text='Hello there',start=.2,end=1.2)]
    b['reskinSourceReview']={'status':'needs_review','receipts':[{'clip':'c1','source_sha256':b['sourceMediaSha256'],'observations':obs}],
        'findings':[dict(clip='c1',shot='c1:1',criterion='speech_delivery',origin='speech_words',message='Original words differ') ]}
    answer={'shots':[],'resolutions':[dict(finding_id='c1:speech:1',status='corrected',shots=[1],at_s=.5,basis='Original audio clearly says Hello there during the first shot.')],'findings':[],'limitations':['Automatic audiovisual observation.']}
    for i,s in enumerate(seq['shots']):
        answer['shots'].append(dict(id=f'c1:{i+1}',source_shot=i+1,start=s['source_start'],end=s['source_end'],fields={},
            dialogue=([dict(speaker_id='hero',speaker='HERO',text='Hello there',delivery='offscreen',start=.2,end=1.2,at_s=.5,basis='Original source voice is heard continuously over this shot.')] if i==0 else []),
            criteria={k:dict(status='pass',at_s=s['source_start']+.2,evidence='Original image and audio establish this source criterion.') for k in repair.review.CRITERIA}))
    return b,answer


def provider(monkeypatch,answer,status=200):
    from flowboard.services.flowstudio import r2
    async def command(*args):args[-1].write_bytes(b'excerpt')
    monkeypatch.setattr(repair.media,'command',command)
    monkeypatch.setattr(r2,'upload_file',lambda _: 'https://test/excerpt.mp4')
    monkeypatch.setenv('ATRIUM_CLIENT_ID','test');monkeypatch.setenv('ATRIUM_CLIENT_SECRET','test')
    calls=[]
    def handle(request):
        if request.method=='GET':return httpx.Response(200,content=b'excerpt')
        calls.append(request)
        return httpx.Response(status,json={'candidates':[{'finishReason':'STOP','content':{'parts':[{'text':json.dumps(answer)}]}}]})
    client=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:client(transport=httpx.MockTransport(handle),**kw))
    return calls


@pytest.mark.asyncio
async def test_evidence_correction_changes_source_revision_and_reuses_paid_receipt(monkeypatch,tmp_path):
    b,answer=source_board(monkeypatch,tmp_path);before=deepcopy(b);calls=provider(monkeypatch,answer)
    out=await repair.repair(b,tmp_path)
    assert b==before and len(calls)==1
    assert out['nodes'][-1]['data']['shots'][0]['dialogue'][0]['line']=='Hello there'
    assert out['sourceVerification']['source_revisions']
    assert out['reskinSourceReview']['previous_report']['findings']==b['reskinSourceReview']['findings']
    again=await repair.repair(b,tmp_path)
    assert len(calls)==1 and again['sourceVerification']['digest']==out['sourceVerification']['digest']


@pytest.mark.asyncio
async def test_failed_http_is_retained_and_not_resent(monkeypatch,tmp_path):
    b,a=source_board(monkeypatch,tmp_path);calls=provider(monkeypatch,a,status=500)
    for _ in range(2):
        out=await repair.repair(b,tmp_path)
        assert out['nodes']==b['nodes'] and out['reskinSourceReview']['findings']
    assert len(calls)==1


@pytest.mark.asyncio
async def test_malformed_completed_correction_cannot_clear_findings(monkeypatch,tmp_path):
    b,a=source_board(monkeypatch,tmp_path);del a['shots'][0]['criteria']['speech_delivery']['at_s']
    calls=provider(monkeypatch,a)
    out=await repair.repair(b,tmp_path)
    assert out['nodes']==b['nodes'] and out['reskinSourceReview']['findings']
    assert len(calls)==1


@pytest.mark.asyncio
async def test_missing_original_review_is_not_a_reason_for_new_paid_correction(monkeypatch,tmp_path):
    b,a=source_board(monkeypatch,tmp_path);b['reskinSourceReview']['receipts']=[]
    calls=provider(monkeypatch,a)
    assert await repair.repair(b,tmp_path)==b
    assert not calls


@pytest.mark.asyncio
async def test_source_bytes_must_match_before_correction(monkeypatch,tmp_path):
    b,a=source_board(monkeypatch,tmp_path);b['sourceMediaSha256']='changed'
    calls=provider(monkeypatch,a)
    with pytest.raises(ValueError,match='Source bytes changed'):await repair.repair(b,tmp_path)
    assert not calls
