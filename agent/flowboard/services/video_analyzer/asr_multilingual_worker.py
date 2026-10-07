"""Local, bounded ASR with language detection throughout the audio, not just its intro.

Run in a child process like asr_worker; do not import audio libraries in the server.
No dialogue is obtained by translating captions. Detection uncertainty is retained.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

VERSION = 1
RATE = 16000


def language_runs(probes: list[dict], duration: float) -> list[dict]:
    """Midpoints between confident probes define candidate language transitions."""
    known = [p for p in probes if p.get('probability', 0) >= .65 and p.get('language')]
    if not known:
        return [{'start': 0., 'end': duration, 'language': None}]
    runs = [{'start': 0., 'end': duration, 'language': known[0]['language']}]
    previous = known[0]
    for p in known[1:]:
        if p['language'] != previous['language']:
            boundary = (p['center'] + previous['center']) / 2
            runs[-1]['end'] = boundary
            runs.append({'start': boundary, 'end': duration, 'language': p['language']})
        previous = p
    return runs


def owned_segments(segments, start: float, end: float, offset: float, language: str | None):
    """Keep every word once across overlapped decoding windows; retain punctuation."""
    out = []
    for seg in segments:
        words = []
        for w in seg.words or []:
            a, b = offset+w.start, offset+w.end
            if start <= (a+b)/2 < end:
                words.append({'start': round(a,3), 'end': round(b,3), 'word':w.word,
                              'probability':round(w.probability,4)})
        if words:
            out.append({'start':words[0]['start'], 'end':words[-1]['end'],
                        'text':''.join(w['word'] for w in words).strip(),
                        'words':words, 'language':language,
                        'avg_logprob':seg.avg_logprob, 'no_speech_prob':seg.no_speech_prob})
    return out


def run(audio: str, model_name: str, language: str | None = None) -> dict:
    import numpy as np
    from faster_whisper import WhisperModel
    from faster_whisper.audio import decode_audio
    from faster_whisper.vad import get_speech_timestamps, VadOptions
    started=time.monotonic()
    samples=decode_audio(audio, sampling_rate=RATE)
    duration=len(samples)/RATE
    model=WhisperModel(model_name,device='cpu',compute_type='int8',cpu_threads=4)
    speech=get_speech_timestamps(samples,VadOptions(min_silence_duration_ms=300))
    def spoken(a,b):
        return sum(max(0,min(b*RATE,s['end'])-max(a*RATE,s['start']))/RATE for s in speech)
    probes=[]
    def probe(center, width):
        a,b=max(0,center-width/2),min(duration,center+width/2)
        if spoken(a,b)<.35: return
        sample=samples[int(a*RATE):int(b*RATE)]
        if len(sample)<RATE//2 or float(np.max(np.abs(sample)))<.003:return
        code,prob,_=model.detect_language(audio=sample)
        probes.append({'center':center,'start':a,'end':b,'language':code,'probability':round(prob,4)})
    if language:
        runs=[{'start':0.,'end':duration,'language':language}]
    else:
        for a in np.arange(0,duration,5.): probe(min(a+2.5,(a+duration)/2),5.)
        coarse=language_runs(probes,duration)
        # Only inspect a detected transition, once, using local 2-second windows.
        for r in coarse[1:]:
            for center in np.arange(max(1,r['start']-2),min(duration,r['start']+3),1.):probe(float(center),2.)
        probes.sort(key=lambda p:p['center'])
        runs=language_runs(probes,duration)
    detection_elapsed=time.monotonic()-started
    segments=[]; windows=[]; findings=[]
    for run in runs:
        start=run['start']
        while start < run['end']:
            end=min(start+28,run['end'])
            a,b=max(0,start-1),min(duration,end+1)
            if spoken(start,end)<.2:
                start=end; continue
            t=time.monotonic()
            decoded,info=model.transcribe(samples[int(a*RATE):int(b*RATE)],language=run['language'],
                word_timestamps=True,vad_filter=True,condition_on_previous_text=False,beam_size=5)
            rows=owned_segments(decoded,start,end,a,info.language)
            windows.append({'start':start,'end':end,'context_start':a,'context_end':b,
                            'language':info.language,'elapsed_s':round(time.monotonic()-t,3)})
            segments.extend(rows)
            if not rows:findings.append({'code':'speech_without_transcript','start':start,'end':end})
            for row in rows:
                ws=row['words']
                if (sum(w['end']-w['start']<.005 for w in ws)>len(ws)*.4 or
                        row['avg_logprob'] < -1 or row['no_speech_prob']>.6):
                    findings.append({'code':'uncertain_asr_segment','start':row['start'],'end':row['end']})
            start=end
    segments.sort(key=lambda x:x['start'])
    languages=sorted({s['language'] for s in segments})
    return {'version':VERSION,'method':'language_runs_v1','model':model_name,
            'language':languages[0] if len(languages)==1 else 'mixed','languages':languages,
            'segments':segments,'language_probes':probes,'language_runs':runs,'windows':windows,
            'findings':findings,'source_duration_s':duration,
            'timings_s':{'language_detection_and_load':round(detection_elapsed,3)},
            'elapsed_s':round(time.monotonic()-started,3)}


def main(argv):
    result=run(argv[1],argv[3] if len(argv)>3 else 'medium',argv[4] if len(argv)>4 and argv[4] else None)
    target=Path(argv[2]); tmp=target.with_suffix('.tmp')
    tmp.write_text(json.dumps(result,ensure_ascii=False,indent=2));tmp.replace(target)
    return 0

if __name__=='__main__':sys.exit(main(sys.argv))
