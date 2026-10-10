"""Deterministic source checks and lossless, evidence-backed audio boundaries.

No caption is converted into speech. Captions can delimit words already heard,
and contradictions become local findings rather than silently certified inputs.
"""
from copy import deepcopy
import hashlib
import json
import re
from difflib import SequenceMatcher

VERSION = 3


def tokens(text):
    return re.findall(r"[^\W_]+(?:['’][^\W_]+)*", str(text).lower().replace('’', "'"), re.UNICODE)


def comparison_tokens(text):
    """Orthographic equivalences only; never modify the stored spoken text."""
    contractions = {"i'm":"i am", "you're":"you are", "he's":"he is", "she's":"she is",
        "it's":"it is", "that's":"that is", "isn't":"is not", "don't":"do not",
        "can't":"cannot", "i'll":"i will", "didn't":"did not"}
    digits = dict(zip(map(str,range(20)), ('zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen').split()))
    tens=['','','twenty','thirty','forty','fifty','sixty','seventy','eighty','ninety']
    for n in range(20,100):digits[str(n)]=tens[n//10]+(' '+digits[str(n%10)] if n%10 else '')
    return [p for token in tokens(text) for p in contractions.get(token,digits.get(token,token)).split()]


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


# Older vision readings join a shot's captions into one string with "|" or "; ".
JOINED_CAPTIONS = re.compile(r"\s*[|;]\s*")


def caption_pieces(shot):
    source = shot.get('source') or {}
    events = source.get('subtitle_events') or []
    pieces = [e['text'] for e in events if isinstance(e, dict) and e.get('text')]
    if not pieces:
        pieces = JOINED_CAPTIONS.split(str(source.get('subtitle') or ''))
    return list(dict.fromkeys(p.strip() for p in pieces if tokens(p)))


def align_audio(transcript, shots):
    """Add boundaries only on unique timed matches; preserve audio words/language.

    ASR often omits punctuation between alternating speakers. A matching caption
    can safely split already-transcribed words without making up a new utterance.
    Genuine repetitions at different times survive. Only overlapping duplicate
    tokens from decoder windows are removed, with receipts.
    """
    result = deepcopy(transcript)
    seen = []
    removed = deepcopy((result.get('integrity_alignment') or {}).get('removed_overlap_tokens') or [])
    def zero(w):
        return float(w['end']) - float(w['start']) < .02
    for segment in result.get('segments', []):
        kept = []
        for word in segment.get('words', []):
            duplicate = next((old for old in reversed(seen[-4:])
                if tokens(old['word']) == tokens(word['word']) and tokens(word['word'])
                and min(float(old['end']), float(word['end'])) - max(float(old['start']), float(word['start'])) > .04), None)
            if duplicate is None and seen:
                # A decoder window boundary can emit a word twice: once with no duration
                # at the edge, once properly timed a moment later. One word, real timing.
                old = seen[-1]
                if (tokens(old['word']) == tokens(word['word']) and tokens(word['word'])
                        and 0 <= float(word['start']) - float(old['end']) < .25 and (zero(old) or zero(word))):
                    if zero(old) and not zero(word):
                        old.update(start=word['start'], end=word['end'])
                    duplicate = old
            if duplicate:
                removed.append({'word': word, 'retained': duplicate, 'basis': 'same token with overlapping audio time'})
            else:
                kept.append(word); seen.append(word)
        if segment.get('words'):
            segment['words'] = kept
            segment['text'] = ''.join(w['word'] for w in kept).strip()
            if kept: segment.update(start=kept[0]['start'], end=kept[-1]['end'])
    result['segments'] = [s for s in result.get('segments', []) if s.get('text', '').strip()]
    words = [w for s in result.get('segments', []) for w in s.get('words', [])]
    # A word the decoder gave no duration still took time to say: it gets up to 0.2 s of
    # the silence after it, else of the silence before it (receipts kept).
    timed = []
    for i, w in enumerate(words):
        if not zero(w):
            continue
        after = float(words[i + 1]['start']) if i + 1 < len(words) else float(w['end']) + .2
        before = float(words[i - 1]['end']) if i else max(0., float(w['start']) - .2)
        old = (w['start'], w['end'])
        if after - float(w['start']) >= .04:
            w['end'] = round(min(float(w['start']) + .2, after), 3)
        elif float(w['end']) - before >= .04:
            w['start'] = round(max(float(w['end']) - .2, before), 3)
        if (w['start'], w['end']) != old:
            timed.append({'word': w['word'], 'was': list(old), 'now': [w['start'], w['end']]})
    for segment in result.get('segments', []):
        if segment.get('words'):
            segment.update(start=segment['words'][0]['start'], end=segment['words'][-1]['end'])
    flattened = [(token, i) for i,w in enumerate(words) for token in tokens(w['word'])]
    normal = [t for t,i in flattened]
    receipts = []
    for shot in shots:
        for caption in caption_pieces(shot):
            phrase = tokens(caption)
            if len(phrase) < 2: continue
            matches = []
            for offset in range(len(normal)-len(phrase)+1):
                if normal[offset:offset+len(phrase)] != phrase: continue
                first, last = flattened[offset][1], flattened[offset+len(phrase)-1][1]
                if (float(words[last]['end']) >= float(shot['start'])-1.5
                        and float(words[first]['start']) <= float(shot['end'])+1.5):
                    matches.append((first,last))
            if len(matches) != 1: continue
            first,last = matches[0]
            if first: words[first-1]['source_sentence_end'] = True
            words[last]['source_sentence_end'] = True
            receipts.append({'shot':shot['shot'],'caption':caption,
                'start':words[first]['start'],'end':words[last]['end'],
                'basis':'unique caption phrase aligned to existing ASR words; no word replacement'})
    result['integrity_alignment'] = {'version':VERSION,'boundaries':receipts,'removed_overlap_tokens':removed,
                                     'timed_zero_length_words':timed}
    return result


def findings(analysis, inventory=None):
    issues = []
    shots = analysis.get('shots') or []
    transcript = analysis.get('transcript') or {}
    words = [w for seg in transcript.get('segments', []) for w in seg.get('words', [])]
    segments = transcript.get('segments') or []
    # A caption held over a reaction cut is still ONE caption. Compare its
    # entire contiguous display span, not just the last shot's audio window.
    caption_runs={}
    for shot in shots:
        for caption in caption_pieces(shot):
            phrase=tuple(comparison_tokens(caption));runs=caption_runs.setdefault(phrase,[])
            if runs and abs(runs[-1]['end']-shot['start'])<.05:
                runs[-1]['end']=shot['end'];runs[-1]['shots'].append(shot['shot'])
            else:runs.append({'start':shot['start'],'end':shot['end'],'shots':[shot['shot']]})
    def caption_bounds(caption,shot):
        return next(r for r in caption_runs[tuple(comparison_tokens(caption))] if shot['shot'] in r['shots'])
    def add(code, message, shot=None, **extra):
        issues.append({'code':code,'message':message,**({'shot':shot['shot'],'start':shot['start'],'end':shot['end']} if shot else {}),**extra})
    cursor = 0.
    measurement = analysis.get('cuts') or {}
    if measurement and measurement.get('measurement_version', 0) < 3:
        add('cut_measurement_outdated','Cut cache predates motion registration; remeasure locally before reusing its shot boundaries.')
    elif 'kept' in measurement:
        boundaries=[float(s['start']) for s in shots[1:]]
        measured=measurement['kept']
        if len(boundaries)!=len(measured) or any(abs(a-b)>.05 for a,b in zip(boundaries,measured)):
            add('cut_shot_disagreement','Measured boundaries changed; old shot observations/locks must be rebuilt for affected intervals, not relabelled verified.')
    for shot in shots:
        start,end = float(shot['start']),float(shot['end'])
        if end <= start or abs(start-cursor)>.05:
            add('source_timeline_gap','Source timeline overlaps, has a gap or an invalid duration.',shot)
        cursor = end
        captions = caption_pieces(shot)
        context=[caption_bounds(c,shot) for c in captions]
        near_start=min([start]+[r['start'] for r in context]);near_end=max([end]+[r['end'] for r in context])
        nearby = [seg for seg in segments if float(seg['end'])>near_start-.35 and float(seg['start'])<near_end+.35]
        if captions and not nearby:
            add('caption_without_audio','Caption/visible speech has no ASR coverage; locally check the original audio.',shot,
                captions=captions)
        # Match only sufficiently long captions in the original ASR script.
        # Translation is not inferred from a mismatch and never replaces audio.
        if captions and words:
            for caption in captions:
                span=caption_bounds(caption,shot)
                nearby_tokens = [t for w in words if float(w['end'])>span['start']-1.5 and float(w['start'])<span['end']+1.5 for t in comparison_tokens(w['word'])]
                phrase=comparison_tokens(caption)
                if len(phrase)<4 or not nearby_tokens: continue
                match=SequenceMatcher(None,phrase,nearby_tokens,autojunk=False)
                aligned=sum(b.size for b in match.get_matching_blocks())
                if 0.55 <= aligned/len(phrase) < 1:
                    add('caption_audio_disagreement','Caption mostly matches audio but words differ; do not silently accept either version.',shot,
                        caption=caption)
        source=shot.get('source') or {}
        if not source.get('action') or not source.get('shot_size'):
            add('source_visual_missing','Action or framing has not been observed.',shot)
    duration=float((analysis.get('video') or {}).get('duration') or cursor)
    if shots and abs(cursor-duration)>.05: add('source_timeline_gap','Source timeline does not cover the full video.')
    if analysis.get('speech_error'): add('source_audio_failed','Source audio failed; silence cannot be assumed.')
    for finding in transcript.get('findings') or []:
        add('source_asr_uncertain','Source ASR has an unresolved finding.',details=finding)
    for receipt in (transcript.get('integrity_alignment') or {}).get('boundaries') or []:
        span = [w for w in words if w['start']>=receipt['start'] and w['end']<=receipt['end']]
        if any(b['start']-a['end']>1.4 for a,b in zip(span,span[1:])):
            shot = next((s for s in shots if s['shot']==receipt['shot']),None)
            add('audio_timing_suspect','A caption-aligned phrase has a long internal ASR gap; check word timing before assigning its speaker.',shot,
                audio_start=receipt['start'],audio_end=receipt['end'])
    for line in analysis.get('dialogue_track') or []:
        if line.get('start') is None or line.get('end') is None: continue
        if line['end'] <= line['start']: add('audio_timing_invalid','A dialogue line has invalid timing.')
        # Several distinct caption turns contained in one line indicate a failed
        # segmentation. This is a check, not a license to guess speaker identity.
        inside=[s for s in shots if s['start']>=line['start']-.2 and s['end']<=line['end']+.2]
        turns=[]; seen_captions=set()
        for s in inside:
            own={tuple(tokens(p)) for p in caption_pieces(s) if len(tokens(p))>=3 and all(t in tokens(line['text']) for t in tokens(p))}
            if own-seen_captions: turns.append(s)
            seen_captions.update(own)
        if len(turns)>1 and len(tokens(line['text']))>15:
            add('merged_dialogue_turns','One audio line contains multiple visual caption turns; split aligned utterances before assigning a speaker.',turns[0])
    if inventory:
        for shot in shots:
            row=(inventory.get('shots') or {}).get(str(shot['shot']),{})
            speakers=row.get('speakers') or []
            if len(speakers)!=len(shot.get('dialogue_lines') or []):
                add('speaker_coverage','Each dialogue line needs an explicit speaker and delivery.',shot)
            presence={p['asset_id']:p for p in row.get('asset_presence',[]) if p.get('asset_id')}
            for speaker in speakers:
                if speaker.get('delivery')=='on_camera':
                    pres=presence.get(speaker.get('asset_id'),{})
                    if pres.get('visibility')!='visible' and not (speaker.get('mouth_visible') is True and speaker.get('evidence_ids')):
                        add('speaker_mouth_not_established','On-camera delivery needs a visible speaker; a cropped shoulder/hand does not establish lip-sync.',shot)
    unique={fingerprint(f):f for f in issues}
    return list(unique.values())


# Findings a reviewer should see but that cannot stop production: the dialogue already
# carries the decoded or verbatim-caption words, and these only describe how they were
# reached. With translated captions the caption checks stay blocking, as before.
NOTE_ALWAYS = {'audio_timing_suspect', 'source_asr_uncertain'}
NOTE_IF_VERBATIM = {'caption_audio_disagreement', 'caption_without_audio'}


def report(analysis, inventory=None):
    issues=findings(analysis,inventory)
    recovery=(analysis.get('transcript') or {}).get('caption_recovery') or {}
    verbatim=recovery.get('mode')=='verbatim'
    # Once caption recovery has decoded every unheard caption's window again, what it
    # still cannot hear is reported to the reviewer, not a reason to stop the film.
    looked=bool(recovery) and recovery.get('mode')!='none'
    blocking=[f for f in issues if f['code'] not in NOTE_ALWAYS and not (verbatim and f['code'] in NOTE_IF_VERBATIM)
              and not (looked and f['code']=='caption_without_audio')]
    notes=[f for f in issues if f not in blocking]
    return {'version':VERSION,'status':'needs_attention' if blocking else 'checked_with_notes' if notes else 'checked',
        'scope':'timeline, caption/audio conflicts, dialogue boundaries and speaker bindings; not a guarantee of perceptual accuracy',
        'findings':blocking,'notes':notes,
        'input_digest':fingerprint({k:analysis.get(k) for k in ('video','cuts','shots','dialogue_track','transcript')})}
