"""Same-language captions and speech recognition, combined without inventing speech.

Policy (AGENTS.md: keep exact dialogue and its language):

* Captions are first classified against the film's own speech recognition. Only captions
  that transcribe the spoken language ("verbatim": ≥80% of caption words were also heard)
  may shape dialogue text. Translated or mixed captions only ever delimit timing, as before.
* A verbatim caption the film-wide recognition did not hear is decoded again locally with
  no voice-activity gating — quiet and overlapped speech is what that gate drops. First
  unprompted; only if that fails, prompted with the caption under stricter acceptance
  (confident, timed words inside the caption's span). Accepted words are decoded audio.
* A verbatim caption still unheard after that stays in the film as a caption line marked
  ``audio_unconfirmed`` — the source shows the words being said; a reviewer sees the flag.
* A heard line whose words disagree with its verbatim caption by a like-for-like
  substitution ("tweet" / "sweet") takes the caption's word; nothing else is rewritten.

Every decision leaves a receipt in ``transcript['caption_recovery']``.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from copy import deepcopy
from difflib import SequenceMatcher
from pathlib import Path

from . import asr, subtitle_cues
from .source_integrity import comparison_tokens, tokens

VERSION = 1
VERBATIM_MATCH = 0.8
TRANSLATED_MATCH = 0.3
HEARD_SHARE = 0.5          # a cue with less than this share of its words heard is "missing"
PLAIN_ACCEPT = 0.6         # unprompted decode must reproduce this share of the caption
PROMPTED_ACCEPT = 0.7      # prompted decode: stricter, plus confidence and duration checks
PROMPTED_MIN_PROB = 0.5
MIN_WORD_S = 0.04
ECHO_PROB = 0.1
PAD_S = 0.6


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _words(transcript: dict) -> list[dict]:
    return [w for seg in transcript.get('segments') or [] for w in seg.get('words') or []]


def classify(cues: list[dict], transcript: dict) -> dict:
    """``verbatim`` | ``translated`` | ``mixed`` | ``none`` with the measured agreement.

    ``match`` is the share of all caption words the film's speech recognition heard, in
    order. A few misread or repeated captions, an on-screen banner or a line said a beat
    off can pull that under 0.8 for captions that are verbatim line by line, so
    ``cue_median`` — the median caption's heard share, aligned in time — also counts:
    verbatim when the typical caption is ≥0.8 heard and the whole film ≥0.6.
    """
    cue_tokens = [t for c in cues for t in c['tokens']]
    heard = [t for seg in transcript.get('segments') or [] for t in comparison_tokens(seg.get('text', ''))]
    if len(cue_tokens) < 20 or not heard:
        return {'mode': 'none', 'match': None, 'cue_tokens': len(cue_tokens)}
    m = SequenceMatcher(None, cue_tokens, heard, autojunk=False)
    match = sum(b.size for b in m.get_matching_blocks()) / len(cue_tokens)
    shares = sorted(cue_coverage(cues, _words(transcript)))
    median = shares[len(shares) // 2] if len(shares) % 2 else (shares[len(shares) // 2 - 1] + shares[len(shares) // 2]) / 2
    if match >= VERBATIM_MATCH or (median >= VERBATIM_MATCH and match >= 0.6):
        mode = 'verbatim'
    else:
        mode = 'translated' if match < TRANSLATED_MATCH else 'mixed'
    return {'mode': mode, 'match': round(match, 4), 'cue_median': round(median, 4), 'cue_tokens': len(cue_tokens)}


def _aligned(a: list[tuple[int, str]], b: list[tuple[int, str]], weight) -> list[tuple[int, int]]:
    """Order-preserving matching of ``a`` to ``b`` with the most total weight.

    ``weight(i, j)`` scores pairing a[i] with b[j] (0 = not allowed). Plain LCS dynamic
    programming, one numpy row at a time: ``row[j] = max(prev[j], prev[j-1] + w[j])``
    followed by a running maximum along the row.
    """
    import numpy as np
    n, m = len(a), len(b)
    if not n or not m:
        return []
    w = np.zeros((n, m), dtype=np.int16)
    for i in range(n):
        w[i] = weight(i)
    dp = np.zeros((n + 1, m + 1), dtype=np.int32)
    for i in range(n):
        cand = np.maximum(dp[i, 1:], dp[i, :-1] + w[i])
        dp[i + 1, 1:] = np.maximum.accumulate(cand)
    pairs, i, j = [], n, m
    while i and j:
        if w[i - 1, j - 1] and dp[i, j] == dp[i - 1, j - 1] + w[i - 1, j - 1]:
            pairs.append((i - 1, j - 1)); i -= 1; j -= 1
        elif dp[i - 1, j] >= dp[i, j - 1]:
            i -= 1
        else:
            j -= 1
    return pairs[::-1]


def cue_coverage(cues: list[dict], words: list[dict], *, budget: int = 1200,
                 lead_s: float = 0.75, tail_s: float = 0.5) -> list[float]:
    """Share of each cue's words that the speech recognition heard.

    One ordered alignment of caption words to heard words in which a heard word may only
    pair with a caption on screen when it was said — inside the cue's visible span,
    widened by ``lead_s`` before (a caption can appear a beat before the words) and
    ``tail_s`` after. Every heard word vouches for one caption only: two people saying the
    same sentence back to back are two captions, and one recognised utterance covers the
    one whose span holds it. A pairing inside the unwidened span outweighs one in the
    margin. Long films are aligned in windows of about ``budget`` caption words, cut where
    no caption's margin reaches the next.
    """
    import numpy as np
    if not cues:
        return []
    got = [0] * len(cues)
    order = sorted(range(len(cues)), key=lambda i: (cues[i]['start'], cues[i]['end']))
    mids = [(float(w['start']) + float(w['end'])) / 2 for w in words]
    by_time = sorted(range(len(words)), key=lambda j: mids[j])
    groups, cur, size, reach = [], [], 0, float('-inf')
    for i in order:
        c = cues[i]
        if cur and size >= budget and (c['start'] - lead_s > reach or size >= 2 * budget):
            groups.append(cur); cur, size = [], 0
        cur.append(i); size += len(c['tokens']); reach = max(reach, c['end'] + tail_s)
    groups.append(cur)
    edges = [float('-inf')]
    for g, h in zip(groups, groups[1:]):
        edges.append((max(cues[i]['end'] for i in g) + min(cues[i]['start'] for i in h)) / 2)
    edges.append(float('inf'))
    for k, g in enumerate(groups):
        lo, hi = edges[k], edges[k + 1]
        a = [(i, tok) for i in g for tok in cues[i]['tokens']]
        b = [(j, tok) for j in by_time if lo <= mids[j] < hi for tok in comparison_tokens(words[j]['word'])]
        if not b:
            continue
        ids = {}
        b_tok = np.array([ids.setdefault(t, len(ids)) for _, t in b])
        b_mid = np.array([mids[j] for j, _ in b])
        def weight(x, a=a, b_tok=b_tok, b_mid=b_mid):
            i, tok = a[x]
            c = cues[i]
            same = b_tok == ids.get(tok, -1)
            inside = (b_mid >= c['start']) & (b_mid <= c['end'])
            margin = (b_mid >= c['start'] - lead_s) & (b_mid <= c['end'] + tail_s)
            return np.where(same & inside, 4, np.where(same & margin, 3, 0))
        for x, _ in _aligned(a, b, weight):
            got[a[x][0]] += 1
    return [g / len(c['tokens']) for g, c in zip(got, cues)]


def _accept(cue: dict, segments: list[dict], *, prompted: bool) -> tuple[list[dict], float]:
    """The decoded words that reproduce the caption, and the share of it they reproduce.

    Only words said while the caption is on screen count: a decode window reaches into
    the neighbouring speaker's words, which belong to their own caption. ``([], 0.0)``
    when the decode does not reproduce enough of the caption.
    """
    words = [w for s in segments for w in s.get('words') or []
             if cue['start'] - 0.75 <= (float(w['start']) + float(w['end'])) / 2 <= cue['end'] + 0.5]
    if prompted:
        # A word the prompted decoder barely believes, at either edge, is its prompt echoing.
        while words and float(words[0].get('probability', 0)) < ECHO_PROB:
            words = words[1:]
        while words and float(words[-1].get('probability', 0)) < ECHO_PROB:
            words = words[:-1]
    flat = [(t, i) for i, w in enumerate(words) for t in comparison_tokens(w['word'])]
    if not flat:
        return [], 0.0
    m = SequenceMatcher(None, cue['tokens'], [t for t, _ in flat], autojunk=False)
    blocks = [b for b in m.get_matching_blocks() if b.size]
    share = sum(b.size for b in blocks) / len(cue['tokens'])
    if not blocks or share < (PROMPTED_ACCEPT if prompted else PLAIN_ACCEPT):
        return [], 0.0
    # Decoded words in the slot of caption words left unmatched at either edge are the
    # same words heard differently ("Damien!" for "Damian,"): they belong to this line.
    head = max(0, blocks[0].b - blocks[0].a)
    tail_gap = len(cue['tokens']) - (blocks[-1].a + blocks[-1].size)
    tail = min(len(flat), blocks[-1].b + blocks[-1].size + tail_gap)
    chosen = words[flat[head][1]:flat[tail - 1][1] + 1]
    if prompted:
        # A prompted decoder can echo its prompt: demand confident, really timed words.
        probs = [float(w.get('probability', 0)) for w in chosen]
        if (sum(probs) / len(probs) < PROMPTED_MIN_PROB
                or any(float(w['end']) - float(w['start']) < MIN_WORD_S for w in chosen)):
            return [], 0.0
    return chosen, round(share, 4)


def _dedupe(new: list[dict], existing: list[dict]) -> list[dict]:
    """Drop decoded words the transcript already holds (same token, overlapping time)."""
    out = []
    for w in new:
        dup = any(tokens(e['word']) == tokens(w['word']) and tokens(w['word'])
                  and min(float(e['end']), float(w['end'])) - max(float(e['start']), float(w['start'])) > MIN_WORD_S
                  for e in existing)
        if not dup:
            out.append(w)
    return out


def _heard_in_span(cue: dict, words: list[dict]) -> int:
    """Recognised words said while the caption was on screen."""
    return sum(1 for w in words if cue['start'] <= (float(w['start']) + float(w['end'])) / 2 <= cue['end'])


def _voice_in_span(cue: dict, variants: dict) -> bool:
    """Did either decode hear any confident word while the caption was on screen?"""
    for name in ('plain', 'prompted'):
        for seg in variants.get(name) or []:
            for w in seg.get('words') or []:
                mid = (float(w['start']) + float(w['end'])) / 2
                if float(w.get('probability', 0)) >= 0.3 and cue['start'] - 0.3 <= mid <= cue['end'] + 0.3:
                    return True
    return False


def caption_language(cues: list[dict], coverage: list[float], transcript: dict) -> str | None:
    """The spoken language the captions transcribe: that of the speech under well-heard captions."""
    count: dict[str, int] = {}
    for cue, share in zip(cues, coverage):
        if share < VERBATIM_MATCH:
            continue
        for seg in transcript.get('segments') or []:
            if seg.get('language') and float(seg.get('end', 0)) > cue['start'] and float(seg.get('start', 0)) < cue['end']:
                count[seg['language']] = count.get(seg['language'], 0) + 1
    return max(count, key=count.get) if count else None


def _other_language(variants: dict, language: str | None) -> bool:
    """The unprompted decode heard speech, none of it in the captions' language."""
    heard = {seg.get('language') for seg in variants.get('plain') or [] if seg.get('words') and seg.get('language')}
    return bool(language) and bool(heard) and language not in heard


async def _decode(cues: list[dict], result: dict, work: Path, *, prompted: bool) -> dict:
    """Decode the window of each cue once, with no voice-activity gate; cached by window.

    ``prompted`` also decodes with the caption as the prompt (verbatim captions only).
    Returns ``{cue id: {plain, prompted}}``.
    """
    transcript = result.get('transcript') or {}
    source = next(iter(work.glob('audio.wav')), None) or next(iter(work.glob('source.*')), None)
    if source is None:
        raise ValueError('Source audio is required to recover captioned speech.')
    duration = float((result.get('video') or {}).get('duration') or 0) or max(c['end'] for c in cues) + 1
    base = {'source': transcript.get('cache_signature') or _digest(transcript.get('segments') or []),
            'model': asr.DEFAULT_MODEL, 'version': VERSION}
    languages = {seg.get('language') for seg in transcript.get('segments') or [] if seg.get('language')}
    folder = work / 'one-pass-guided'
    folder.mkdir(parents=True, exist_ok=True)
    jobs, out, ids = [], {}, {}
    for cue in cues:
        job = {'start': round(max(0.0, cue['start'] - PAD_S), 3), 'end': round(min(duration, cue['end'] + PAD_S), 3),
               # Unprompted windows of translated captions decode in the film's own language.
               'language': None if prompted or len(languages) != 1 else next(iter(languages)),
               'prompt': cue['text'] if prompted else ''}
        job['id'] = _digest({**base, **job})[:24]
        ids[cue['id']] = job['id']
        path = folder / f"{job['id']}.json"
        if path.exists():
            out[job['id']] = json.loads(path.read_text())
        else:
            jobs.append(job)
    if jobs:
        batch = await asr.transcribe_windows(source, folder / f"batch-{_digest(jobs)[:16]}.json", jobs)
        for job_id, variants in (batch.get('results') or {}).items():
            (folder / f"{job_id}.json").write_text(json.dumps(variants, ensure_ascii=False))
            out[job_id] = variants
    return {cid: out.get(jid, {}) for cid, jid in ids.items()}


def _accept_unguided(cue: dict, segments: list[dict], language: str | None) -> list[dict]:
    """Confident speech decoded under a caption that cannot vouch for its words (a
    translation): inside the caption's span, in the film's language, not the decoder's
    guess at music or silence."""
    rows = [s for s in segments if s.get('words') and (language is None or s.get('language') == language)
            and float(s.get('no_speech_prob') or 0) <= 0.6 and float(s.get('avg_logprob') or 0) >= -1.0]
    words = [w for s in rows for w in s['words']
             if cue['start'] - 0.75 <= (float(w['start']) + float(w['end'])) / 2 <= cue['end'] + 0.5]
    if not words or sum(float(w.get('probability', 0)) for w in words) / len(words) < 0.6:
        return []
    if any(float(w['end']) - float(w['start']) < MIN_WORD_S for w in words):
        return []
    return words


async def recover(analysis: dict, work: Path) -> dict:
    """Bring speech the audio pass missed back as decoded words (or flagged/reported cues).

    Verbatim captions: every caption the recognition did not hear is decoded again, its
    text may steer a stricter second decode, and what stays unheard is either a flagged
    caption line or a reported cue (see below). Translated or mixed captions vouch for
    timing only: a caption with no recognised word under it is decoded again with no
    prompt, and only confident speech in the film's language is taken.
    """
    result = deepcopy(analysis)
    shots = result.get('shots') or []
    transcript = result.get('transcript') or {}
    cues = subtitle_cues.build(shots, deep=bool(result.get('deep')))
    cls = classify(cues, transcript)
    record = {'version': VERSION, **cls, 'cues': len(cues), 'accepted': [], 'fallbacks': []}
    words = _words(transcript)
    if cls['mode'] in ('translated', 'mixed'):
        silent = [c for c in cues if c['end'] - c['start'] >= 0.5 and not _heard_in_span(c, words)]
        record['silent_captions'] = len(silent)
        added = []
        if silent:
            decoded = await _decode(silent, result, work, prompted=False)
            languages = {seg.get('language') for seg in transcript.get('segments') or [] if seg.get('language')}
            language = next(iter(languages)) if len(languages) == 1 else None
            for cue in silent:
                chosen = _dedupe(_accept_unguided(cue, decoded[cue['id']].get('plain') or [], language),
                                 words + [w for s in added for w in s['words']])
                if chosen:
                    added.append({'start': chosen[0]['start'], 'end': chosen[-1]['end'],
                                  'text': ''.join(w['word'] for w in chosen).strip(), 'words': chosen,
                                  'language': language, 'source': 'guided_window', 'cue': cue['id']})
                    record['accepted'].append({'cue': cue['id'], 'text': cue['text'], 'shots': cue['shots'],
                                               'heard': added[-1]['text'], 'prompted': False,
                                               'start': chosen[0]['start'], 'end': chosen[-1]['end']})
                else:
                    record.setdefault('unplaced', []).append({'cue': cue['id'], 'text': cue['text'],
                                                              'shots': cue['shots'], 'reason': 'no_speech_heard'})
        transcript['segments'] = sorted((transcript.get('segments') or []) + added, key=lambda s: float(s['start']))
        transcript['caption_recovery'] = record
        result['transcript'] = transcript
        return result
    if cls['mode'] != 'verbatim':
        transcript['caption_recovery'] = record
        result['transcript'] = transcript
        return result
    coverage = cue_coverage(cues, words)
    missing = [c for c, share in zip(cues, coverage) if share < HEARD_SHARE]
    repeats: dict[tuple, int] = {}
    for c in cues:
        repeats[tuple(c['tokens'])] = repeats.get(tuple(c['tokens']), 0) + 1
    spoken = caption_language(cues, coverage, transcript)
    if not missing:
        transcript['caption_recovery'] = record
        result['transcript'] = transcript
        return result
    decoded = await _decode(missing, result, work, prompted=True)
    added = []
    for cue in missing:
        variants = decoded[cue['id']]
        plain, plain_share = _accept(cue, variants.get('plain') or [], prompted=False)
        guided, guided_share = _accept(cue, variants.get('prompted') or [], prompted=True)
        # The unprompted decode is preferred; the caption-prompted one only when it passes
        # its stricter checks and reproduces more of the caption (a name the plain pass lost).
        prompted = bool(guided) and guided_share > plain_share
        chosen, share = (guided, guided_share) if prompted else (plain, plain_share)
        if chosen:
            lo, hi = float(chosen[0]['start']) - 1.0, float(chosen[-1]['end']) + 1.0
            pool = [w for w in words + [w for s in added for w in s['words']]
                    if float(w['end']) > lo and float(w['start']) < hi]
        kept = _dedupe(chosen, pool) if chosen else []
        if chosen and len(kept) < 0.5 * len(chosen):
            # The decode only found words the transcript already holds: this speech was heard.
            record.setdefault('already_heard', []).append({'cue': cue['id'], 'text': cue['text'], 'shots': cue['shots']})
            continue
        chosen = kept
        if chosen:
            language = next((s.get('language') for s in (variants.get('prompted' if prompted else 'plain') or [])), None)
            added.append({'start': chosen[0]['start'], 'end': chosen[-1]['end'],
                          'text': ''.join(w['word'] for w in chosen).strip(), 'words': chosen,
                          'language': language, 'source': 'guided_caption', 'cue': cue['id']})
            record['accepted'].append({'cue': cue['id'], 'text': cue['text'], 'shots': cue['shots'],
                                       'heard': added[-1]['text'], 'prompted': prompted, 'match': share,
                                       'start': chosen[0]['start'], 'end': chosen[-1]['end']})
        else:
            # Kept as a flagged caption line only for a voice nothing transcribed: someone
            # audibly speaks, in the captions' language, while the film's recognition holds
            # no words there, and the caption is shown once. A banner has no voice or
            # recurs, a misread caption repeats a line, speech heard otherwise is a
            # disagreement, and a caption over another language translates it. Each of
            # those is reported, never spoken.
            reason = ('repeated_caption' if repeats[tuple(cue['tokens'])] > 1
                      else 'transcribed_differently' if _heard_in_span(cue, words) >= max(1, .3 * len(cue['tokens']))
                      else 'other_language' if _other_language(variants, spoken)
                      else 'no_speech_heard' if not _voice_in_span(cue, variants) else None)
            if reason:
                record.setdefault('unplaced', []).append({'cue': cue['id'], 'text': cue['text'],
                                                          'shots': cue['shots'], 'reason': reason})
                continue
            record['fallbacks'].append({'cue': cue['id'], 'text': cue['text'], 'shots': cue['shots'],
                                        'start': cue['start'], 'end': cue['end'], 'audio_unconfirmed': True})
    transcript['segments'] = sorted((transcript.get('segments') or []) + added, key=lambda s: float(s['start']))
    transcript['caption_recovery'] = record
    result['transcript'] = transcript
    return result


_TOKEN = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)
CORRECT_MATCH = 0.75       # the heard line must otherwise be the caption
SOUND_ALIKE = 0.5          # "tweet"/"sweet", "suit"/"suite": misheard words look alike


def vocabulary(transcript: dict) -> set[str]:
    """Words the speech recognition is sure it heard somewhere in this film."""
    return {t for seg in transcript.get('segments') or [] for w in seg.get('words') or []
            if float(w.get('probability', 1.0)) >= 0.5 for t in comparison_tokens(w['word'])}


def known_names(entities: dict | None) -> set[str]:
    """Lower-cased name words of the people the film names (``Damian``, ``Davenport``)."""
    out = set()
    for person in (entities or {}).get('characters') or []:
        for name in [person.get('source_name')] + list(person.get('aliases') or []):
            out.update(t.lower() for t in _TOKEN.findall(str(name or '')) if t[:1].isupper())
    return out


def correct_words(words: list[dict], cues: list[dict], *, own: tuple[int, int] | None = None,
                  heard: set[str] | None = None, names: set[str] | None = None
                  ) -> tuple[list[dict], list[dict]]:
    """Fix like-for-like misheard words against overlapping verbatim captions.

    A heard word takes the caption's spelling only when the rest of the words agree with
    the caption, the swap is one or two words with agreeing words (or the edge) on both
    sides, and the two words look alike. Captions have typos and slips too ("monthes",
    "the old fashion way"), so the caption's word must also be one this film's speech
    uses elsewhere (``heard``, see ``vocabulary``) or a capitalised name of one of its
    people (``names``). With neither given, any word qualifies. Contractions compare as
    their expansions, so "You're" against "You are" agrees and stays as spoken. ``own``
    limits changes to ``words[lo:hi]``; the words around it are context (a one-word line
    has none of its own). Works on the timed words, so every per-shot piece built from
    them carries the correction; each word keeps its timing, spacing and punctuation.
    """
    if not words:
        return words, []
    start, end = float(words[0]['start']), float(words[-1]['end'])
    near = [c for c in cues if c['end'] > start - 1.0 and c['start'] < end + 1.0]
    if not near:
        return words, []
    # Comparison tokens on both sides; a token keeps its word only when that word is one
    # plain token (a contraction or a hyphenated pair is never rewritten).
    a, a_word = [], []
    for i, w in enumerate(words):
        found = _TOKEN.findall(w['word'])
        single = len(found) == 1 and len(comparison_tokens(found[0])) == 1
        for t in comparison_tokens(w['word']):
            a.append(t); a_word.append(i if single else None)
    b, b_word = [], []
    for c in near:
        for found in _TOKEN.finditer(c['text']):
            expanded = comparison_tokens(found.group(0))
            for t in expanded:
                b.append(t); b_word.append(found.group(0) if len(expanded) == 1 else None)
    if not a or not b:
        return words, []
    m = SequenceMatcher(None, a, b, autojunk=False)
    if sum(x.size for x in m.get_matching_blocks()) < CORRECT_MATCH * len(a):
        return words, []
    ops = m.get_opcodes()
    out, receipts = deepcopy(words), []
    for n, (op, i1, i2, j1, j2) in enumerate(ops):
        if op != 'replace' or (i2 - i1) != (j2 - j1) or (i2 - i1) > 2:
            continue
        left = i1 == 0 or (n > 0 and ops[n - 1][0] == 'equal')
        right = i2 == len(a) or (n + 1 < len(ops) and ops[n + 1][0] == 'equal')
        if not (left and right):
            continue
        for k in range(i2 - i1):
            wi, new = a_word[i1 + k], b_word[j1 + k]
            if wi is None or new is None or (own and not own[0] <= wi < own[1]):
                continue
            w = out[wi]
            old = _TOKEN.findall(w['word'])[0]
            if (comparison_tokens(old) == comparison_tokens(new)
                    or SequenceMatcher(None, old.lower(), new.lower()).ratio() < SOUND_ALIKE):
                continue
            if (heard is not None or names is not None) and not (
                    new.lower() in (heard or set()) or (new[:1].isupper() and new.lower() in (names or set()))):
                continue
            if old[:1].isupper() and not new[:1].isupper():
                new = new[:1].upper() + new[1:]
            w['word'] = w['word'].replace(old, new, 1)
            w['caption_corrected_from'] = old
            receipts.append({'heard': old, 'caption': new, 'start': w['start']})
    return out, receipts
