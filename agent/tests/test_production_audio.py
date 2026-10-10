import json
import pytest
from flowboard.services import production_audio as audio


def result(text='Good.', music='absent'):
    return {'speech':[{'speaker':'voice1','text':text}], 'background_music':music,
            'uncertain_spans':[], 'notes':'One voice.'}


def test_audio_gate_rejects_added_speech_and_music_independently():
    shots=[{'dialogue':[{'line':'Good.'}]}]
    assert audio.findings(result(),shots)==[]
    assert audio.findings(result('Good. His tone dropping.'),shots)[0]['code']=='independent_audio_text_disagreement'
    assert audio.findings(result(music='present'),shots)[0]['code']=='background_music_present'
    assert audio.findings(result(music='uncertain'),shots)[0]['code']=='background_music_uncertain'
    missing=result();missing.pop('background_music')
    with pytest.raises(ValueError):audio.findings(missing,shots)


@pytest.mark.asyncio
async def test_uncertain_paid_attempt_is_not_repeated(tmp_path,monkeypatch):
    path=tmp_path/'audio.wav';path.write_bytes(b'fake wave')
    calls=[]
    monkeypatch.setattr(audio,'_upload',lambda _: 'https://test/audio.wav')
    class Response:
        content=b'fake wave'
        status_code=503
        def raise_for_status(self): pass
    class Client:
        def __init__(self,*a,**kw):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*a):pass
        async def get(self,*a,**kw):return Response()
        async def post(self,*a,**kw):calls.append(kw['json']);return Response()
    monkeypatch.setattr(audio.httpx,'AsyncClient',Client)
    monkeypatch.setenv('ATRIUM_CLIENT_ID','fake');monkeypatch.setenv('ATRIUM_CLIENT_SECRET','fake')
    with pytest.raises(ValueError,match='503'):await audio.review(path,tmp_path)
    with pytest.raises(ValueError,match='already attempted'):await audio.review(path,tmp_path)
    assert len(calls)==1
    assert 'expected' not in calls[0]['contents'][0]['parts'][1]
    receipt=next((tmp_path/'audio-listening').glob('*/receipt.json'))
    assert json.loads(receipt.read_text())['state']=='failed_or_unknown'
