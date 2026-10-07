from copy import deepcopy
import json
import pytest
from flowboard.services.video_analyzer import one_pass_audio as audio
from flowboard.services.video_analyzer.one_pass_film import digest


@pytest.mark.asyncio
async def test_clean_audio_needs_no_extra_decode(tmp_path, monkeypatch):
    async def forbidden(*a, **k): raise AssertionError('Unexpected ASR call')
    monkeypatch.setattr(audio.asr, 'transcribe', forbidden)
    source = {'transcript': {'segments': [], 'findings': []}}
    result = await audio.repair(source, tmp_path)
    assert result == source and result is not source


@pytest.mark.asyncio
async def test_cached_local_decode_preserves_both_boundary_words_and_language(tmp_path, monkeypatch):
    async def forbidden(*a, **k): raise AssertionError('Paid/expensive decode repeated')
    monkeypatch.setattr(audio.asr, 'transcribe', forbidden)
    (tmp_path/'source.mp4').write_bytes(b'not-read-on-cache-hit')
    words = [{'word': w, 'start': s, 'end': e} for w,s,e in [
        ('Avant.', 0., .5), (' incorrect', 2., 2.5), (' Après.', 4., 4.5)]]
    transcript = {'cache_signature': 'source-1', 'language_runs': [{'start':0.,'end':6.,'language':'fr'}],
        'findings': [{'code':'uncertain_asr_segment','start':2.,'end':2.5}],
        'segments':[{'start':0.,'end':4.5,'text':'Avant. incorrect Après.','words':words}]}
    a,b = 1.2,3.3
    key = digest({'source':'source-1','start':a,'end':b,'language':'fr','model':audio.asr.DEFAULT_MODEL})
    cache = tmp_path/'one-pass-audio'/key/'result.json';cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({'segments':[{'start':.8,'end':1.3,'text':'Bonjour.',
        'words':[{'word':'Bonjour.','start':.8,'end':1.3}]}],'findings':[]}))
    source = {'video':{'duration':6.},'transcript':transcript,
              'shots':[{'shot':1,'start':0.,'end':6.,'dialogue_lines':[]}]}
    before=deepcopy(source)
    result=await audio.repair(source,tmp_path)
    kept=result['transcript']['segments']
    all_words=[w for s in kept for w in s.get('words',[])]
    assert [w['word'].strip() for w in all_words]==['Avant.','Bonjour.','Après.']
    assert source==before
    assert not result['transcript']['findings'] and result['transcript']['original_findings']
    assert result['transcript']['local_repairs'][0]['start']==a


@pytest.mark.asyncio
async def test_unknown_audio_findings_are_not_auto_accepted(tmp_path):
    with pytest.raises(ValueError,match='Unresolved audio'):
        await audio.repair({'transcript':{'findings':[{'code':'language_conflict'}]}},tmp_path)


def test_redecode_can_retain_sentence_ends_without_changing_words_or_times():
    original = [{'words': [{'word':'Thank', 'start':0., 'end':.2},
                          {'word':' you.', 'start':.2, 'end':.5},
                          {'word':'Actually,', 'start':.6, 'end':.8},
                          {'word':' yes.', 'start':.8, 'end':1.1}]}]
    repaired = [{'start':0., 'end':1.12, 'text':'Thank you actually yes', 'words':[
        {'word':'Thank','start':0.,'end':.2}, {'word':' you','start':.2,'end':.51},
        {'word':' actually','start':.51,'end':.81}, {'word':' yes','start':.81,'end':1.12}]}]
    untouched = deepcopy(repaired)
    segments, receipts = audio.retain_sentence_boundaries(original, repaired)
    assert repaired == untouched
    assert len(receipts) == 2
    assert [{k:v for k,v in w.items() if k!='source_sentence_end'} for w in segments[0]['words']] == repaired[0]['words']
    track = audio.dialogue.audio_sentences({'segments':segments}, [{'shot':1,'start':0.,'end':.51}, {'shot':2,'start':.51,'end':2.}])
    assert [(t.text,t.first_shot) for t in track] == [('Thank you',1),('actually yes',2)]


def test_sentence_end_is_not_transferred_to_a_distant_or_different_phrase():
    original=[{'words':[{'word':'thank','end':.3}, {'word':' you.','end':.5}]}]
    repaired=[{'words':[{'word':'see','end':.3}, {'word':' you','end':.5},
                        {'word':'thank','end':5.}, {'word':' you','end':5.3}]}]
    out, receipts=audio.retain_sentence_boundaries(original,repaired)
    assert out==repaired and receipts==[]
