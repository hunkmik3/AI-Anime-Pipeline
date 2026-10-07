"""One bounded local decode of flagged speech windows; never synthesize dialogue."""
from copy import deepcopy
import asyncio
import json
from pathlib import Path

from . import asr, dialogue
from flowboard.services.frame_extract import FFMPEG_BIN


def retain_sentence_boundaries(original_segments, repaired_segments):
    """Keep aligned source sentence ends when a local decode loses punctuation.

    Words/timestamps remain exactly those of the repaired decode. A boundary
    transfers only to a unique matching word near the original time, with its
    preceding word also matching; no visual cut is treated as a speaker change.
    """
    repaired = deepcopy(repaired_segments)
    before = [w for s in original_segments for w in s.get('words', [])]
    after = [w for s in repaired for w in s.get('words', [])]
    receipts = []
    for i, word in enumerate(before):
        if i == 0 or not dialogue._SENTENCE_END.search(word['word']):
            continue
        matches = [j for j, candidate in enumerate(after) if j > 0
                   and abs(float(candidate['end']) - float(word['end'])) <= .6
                   and dialogue._norm(candidate['word']) == dialogue._norm(word['word'])
                   and dialogue._norm(after[j-1]['word']) == dialogue._norm(before[i-1]['word'])]
        if len(matches) != 1:
            continue
        candidate = after[matches[0]]
        if dialogue._SENTENCE_END.search(candidate['word']):
            continue
        candidate['source_sentence_end'] = True
        receipts.append({'original_end': word['end'], 'repaired_end': candidate['end'],
                         'word': candidate['word'], 'basis': 'unique timed two-word alignment'})
    return repaired, receipts


async def repair(analysis, work):
    from .one_pass_film import atomic, digest
    transcript = analysis.get('transcript') or {}
    findings = transcript.get('findings') or []
    if not findings: return deepcopy(analysis)
    unsupported = [f for f in findings if f.get('code') not in {'uncertain_asr_segment','speech_without_transcript'}]
    if unsupported: raise ValueError('Unresolved audio findings: '+str(unsupported)[:700])
    source = next(iter(work.glob('source.*')),None)
    if source is None: raise ValueError('Source audio is required to resolve uncertain transcription.')
    duration = float(analysis['video']['duration'])
    windows = []
    for f in sorted(findings,key=lambda x:x['start']):
        a,b = max(0,float(f['start'])-.8),min(duration,float(f['end'])+.8)
        if windows and a <= windows[-1][1]: windows[-1][1] = max(b,windows[-1][1])
        else: windows.append([a,b])
    # Bound each local correction rather than repeatedly transcribing the film.
    if any(b-a > 60 for a,b in windows):
        raise ValueError('Uncertain audio spans exceed the local repair limit; source findings retained.')
    result=deepcopy(analysis);segments=deepcopy(transcript.get('segments') or []);repairs=[]
    for a,b in windows:
        language=next((r.get('language') for r in transcript.get('language_runs',[]) if r['start']<=a and r['end']>=b),None)
        key=digest({'source':transcript.get('cache_signature') or digest(transcript),'start':a,'end':b,'language':language,'model':asr.DEFAULT_MODEL})
        folder=work/'one-pass-audio'/key;folder.mkdir(parents=True,exist_ok=True)
        cache=folder/'result.json'
        if cache.exists(): out=json.loads(cache.read_text())
        else:
            wav=folder/'audio.wav'
            proc=await asyncio.create_subprocess_exec(FFMPEG_BIN,'-v','error','-y','-ss',str(a),'-i',str(source),
                '-t',str(b-a),'-vn','-ac','1','-ar','16000',str(wav),stderr=asyncio.subprocess.PIPE)
            _,err=await proc.communicate()
            if proc.returncode:raise ValueError('Audio extraction failed: '+err.decode(errors='replace')[-400:])
            out=await asr.transcribe(wav,folder,language=language,multilingual=True)
            atomic(cache,out)
        if out.get('findings') or not out.get('segments'):
            raise ValueError(f'Audio remains uncertain at {a:.2f}–{b:.2f}s after one local decode; checkpoint retained.')
        # Word midpoint ownership protects words in segments crossing either edge.
        kept=[]
        for seg in segments:
            if seg['end']<=a or seg['start']>=b:kept.append(seg);continue
            if not seg.get('words'):
                if seg['start'] >= a and seg['end'] <= b: continue
                raise ValueError('Local audio repair needs word timestamps at overlap boundaries.')
            # Keep prefix and suffix as separate spans: a single segment spanning
            # the replacement would put suffix speech before the repaired words.
            for words in ([w for w in seg['words'] if (w['start']+w['end'])/2 < a],
                          [w for w in seg['words'] if (w['start']+w['end'])/2 >= b]):
                if words: kept.append({**seg,'words':words,'start':words[0]['start'],'end':words[-1]['end'],
                                       'text':''.join(w['word'] for w in words).strip()})
        for seg in out['segments']:
            words=[{**w,'start':round(w['start']+a,3),'end':round(w['end']+a,3)} for w in seg.get('words',[])]
            if words:kept.append({**seg,'words':words,'start':words[0]['start'],'end':words[-1]['end']})
            elif seg.get('text','').strip() and 0 <= seg['start'] < seg['end'] <= b-a+.05:
                kept.append({**seg,'start':round(seg['start']+a,3),'end':round(seg['end']+a,3)})
            else: raise ValueError('Local audio repair returned unusable segment timing; source retained.')
        segments=sorted(kept,key=lambda s:s['start'])
        repairs.append({'start':a,'end':b,'cache':str(cache.relative_to(work))})
    segments, boundaries = retain_sentence_boundaries(transcript.get('segments') or [], segments)
    result['transcript']={**transcript,'segments':segments,'findings':[],
                          'original_findings':findings,'local_repairs':repairs,
                          'retained_sentence_boundaries': boundaries}
    track=dialogue.build_track(result['shots'],result['transcript'],policy='audio_verbatim')
    dialogue.attach(result['shots'],track)
    from dataclasses import asdict
    result['dialogue_track']=[asdict(d) for d in track]
    result['dialogue_policy']='audio_verbatim'
    atomic(work/'one-pass-audio-result.json',result['transcript'])
    return result
