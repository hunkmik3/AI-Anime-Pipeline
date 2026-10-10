"""Deterministic measurements of a source shotlist — what a person checking it against the
video would count, done by code.

Nothing here edits the shotlist or calls a model. The numbers say how much of the visible
speech made it into the dialogue, whether each line sits in the shot where it is spoken,
whether speakers are bound to one identity, and how many shots a video model will struggle
with. ``report`` is stored on the analysis; the CLI in agent/scripts/shotlist_audit.py prints it.
"""
from __future__ import annotations

from difflib import SequenceMatcher

from .source_integrity import caption_pieces, comparison_tokens

VERSION = 1
WORDS_PER_SECOND = 2.5   # brisk on-screen delivery; a piece longer than this is cramped
SHORT_SHOT_S = 1.0


def _shot_lines(shot: dict) -> list[str]:
    return [str(x) for x in shot.get('dialogue_lines') or [] if str(x).strip()]


def _said_words(shots: list[dict]) -> list[dict]:
    """The final dialogue as timed words: each piece's words spread over its audio span
    (the shot's span for older shotlists without pieces)."""
    out = []
    for s in shots:
        pieces = s.get('dialogue_pieces')
        rows = ([(p.get('text') or '', p.get('start'), p.get('end')) for p in pieces] if pieces
                else [(line, None, None) for line in _shot_lines(s)])
        for text, a, b in rows:
            a = float(s['start']) if a is None else float(a)
            b = float(s['end']) if b is None else float(b)
            words = str(text).split()
            step = max(b - a, 0.05) / max(len(words), 1)
            out += [{'word': ' ' + w, 'start': a + i * step, 'end': a + (i + 1) * step} for i, w in enumerate(words)]
    return out


def caption_language_match(shots: list[dict], transcript: dict, *, deep: bool = False) -> float | None:
    """Share of caption words the speech recognition also heard, over the whole film.

    High (≥0.8) means the captions transcribe the spoken language; low means they translate it.
    """
    from . import dialogue_fusion, subtitle_cues
    return dialogue_fusion.classify(subtitle_cues.build(shots, deep=deep), transcript)['match']


def dialogue_metrics(analysis: dict) -> dict:
    from . import dialogue_fusion, subtitle_cues
    shots = analysis.get('shots') or []
    by_n = {s['shot']: s for s in shots}
    cues = subtitle_cues.build(shots, deep=bool(analysis.get('deep')))
    kind = dialogue_fusion.classify(cues, analysis.get('transcript') or {})
    language, verbatim = kind['match'], kind['mode'] == 'verbatim'
    missing_cues, coverage, silent = [], None, []
    if verbatim and cues:
        # Each caption against the words said while it was on screen (one ordered,
        # time-bound alignment: overlapping or repeated captions cannot share words).
        # Words inside a piece are spread evenly over its audio span, so allow a second
        # either side of the caption.
        share = dialogue_fusion.cue_coverage(cues, _said_words(shots), lead_s=1.0, tail_s=1.0)
        total = sum(len(c['tokens']) for c in cues)
        coverage = round(sum(min(1.0, x) * len(c['tokens']) for x, c in zip(share, cues)) / total, 4)
        # Why caption recovery left a caption out (a banner, a misread repeat, speech
        # heard otherwise, no voice), for the reviewer.
        reasons = {(u.get('text'), tuple(u.get('shots') or [])): u.get('reason')
                   for u in ((analysis.get('transcript') or {}).get('caption_recovery') or {}).get('unplaced') or []}
        for cue, x in zip(cues, share):
            if len(cue['tokens']) >= 2 and x < 0.5:
                missing_cues.append({'shots': cue['shots'], 'text': cue['text'], 'heard_share': round(x, 2),
                                     **({'reason': r} if (r := reasons.get((cue['text'], tuple(cue['shots'])))) else {})})
                # Captions showing speech that no shot they are visible in carries a line for.
                if not any(_shot_lines(by_n[n]) or by_n[n].get('dialogue_continues') for n in cue['shots'] if n in by_n):
                    silent.extend(n for n in cue['shots'] if n not in silent)
    # A shot asked to hold more speech than it lasts. Pieces split at the cuts carry
    # their real audio timing; older whole-line shotlists are estimated from word count.
    overlong = []
    for s in shots:
        dur = float(s['end']) - float(s['start'])
        pieces = [p for p in s.get('dialogue_pieces') or [] if p.get('source') != 'caption'
                  and p.get('start') is not None and p.get('end') is not None]
        if pieces:
            spans = sorted((float(p['start']), float(p['end'])) for p in pieces)
            union, cur = 0.0, None
            for a, b in spans:
                if cur and a <= cur[1]:
                    cur = (cur[0], max(cur[1], b))
                else:
                    if cur:
                        union += cur[1] - cur[0]
                    cur = (a, b)
            union += cur[1] - cur[0]
            if union > dur + 0.3:
                overlong.append({'shot': s['shot'], 'speech_s': round(union, 1), 'shot_s': round(dur, 1)})
            continue
        words = sum(len(comparison_tokens(x)) for x in _shot_lines(s))
        need = words / WORDS_PER_SECOND
        if words and need > dur * 1.5 + 0.5:
            overlong.append({'shot': s['shot'], 'speech_s': round(need, 1), 'shot_s': round(dur, 1)})
    # One recorded line that holds several caption turns shown in different shots: the
    # captions' words follow one another inside the line, each turn using its own words
    # (a repeated sentence — "Damian, what are you doing in here?" / "What are you doing
    # in here?" — is two turns only if the line says it twice).
    merged = []
    for line in analysis.get('dialogue_track') or []:
        said = comparison_tokens(line.get('text', ''))
        first, last = line.get('first_shot'), line.get('last_shot')
        if first is None or not said:
            continue
        nearby = [c for c in cues if len(c['tokens']) >= 3 and first - 1 <= c['shots'][0] <= (last or first) + 3]
        hits, at = [], 0
        for c in nearby:
            m = SequenceMatcher(None, c['tokens'], said[at:], autojunk=False)
            blocks = [b for b in m.get_matching_blocks() if b.size]
            if blocks and sum(b.size for b in blocks) >= 0.8 * len(c['tokens']):
                hits.append(c['shots'][0])
                at += blocks[-1].b + blocks[-1].size
        hits = sorted(set(hits))
        if len(hits) >= 2 and any(h != first for h in hits):
            merged.append({'first_shot': first, 'caption_shots': hits, 'text': line.get('text', '')[:120]})
    durations = [float(s['end']) - float(s['start']) for s in shots]
    return {
        'shots': len(shots),
        'shots_with_captions': sum(1 for s in shots if caption_pieces(s)),
        'shots_with_lines': sum(1 for s in shots if _shot_lines(s)),
        'caption_cues': len(cues),
        'caption_language_match': language,
        'captions_verbatim': verbatim,
        'caption_coverage': coverage,
        'missing_caption_lines': missing_cues,
        'speech_shots_without_lines': silent,
        'overlong_shots': overlong,
        'merged_turn_lines': merged,
        'short_shots': sum(1 for d in durations if d < SHORT_SHOT_S),
    }


def speaker_metrics(adaptation: dict | None, cast: dict | None) -> dict:
    """Speaker identity of every production line: bound to a character id, one label each."""
    if not adaptation:
        return {}
    items = [(int(n), d) for n, s in (adaptation.get('shots') or {}).items() for d in s.get('dialogue') or []]
    chars = {c.get('source_asset_id') or c.get('key'): c for c in (cast or {}).get('characters') or []}
    labels: dict[str, set] = {}
    unbound = 0
    for _, d in items:
        sid = d.get('speaker_id')
        if not sid:
            unbound += 1
            continue
        labels.setdefault(sid, set()).add(d.get('who'))
    return {
        'lines': len(items),
        'lines_with_speaker_id': len(items) - unbound,
        'speaker_ids': sorted(labels),
        'labels_per_speaker': {k: sorted(map(str, v)) for k, v in labels.items()},
        'distinct_labels': len({d.get('who') for _, d in items}),
        'unknown_speaker_ids': sorted(k for k in labels if chars and k not in chars),
    }


def voice_metrics(analysis: dict) -> dict:
    """How well the voices measured on the audio agree with the speakers bound to lines."""
    voices = analysis.get('voices') or {}
    if voices.get('status') != 'measured':
        return {'status': voices.get('status')} if voices else {}
    from .voice_id import disputes
    track = [l for l in analysis.get('dialogue_track') or [] if l.get('source') != 'caption']
    confidence = {c: sum(1 for l in track if (l.get('voice_confidence') or 'none') == c) for c in ('high', 'low', 'none')}
    inventory = (analysis.get('scene_inventory') or {}).get('shots') or {}
    split = disputes(inventory, analysis.get('shots') or []) if inventory else voices.get('disputes') or []
    return {'status': 'measured', 'voices': len(voices.get('groups') or {}), 'lines': len(track),
            'confidence': confidence, 'listening': (voices.get('listening') or {}).get('status'),
            'speakers_changed_by_voice': len(voices.get('reconciled') or []),
            'voices_given_to_several_people': split}


def wardrobe_metrics(analysis: dict) -> dict:
    """Wardrobe and identity consistency of the catalog (see wardrobe_checks)."""
    inventory = analysis.get('scene_inventory') or {}
    if not inventory.get('shots'):
        return {}
    from .wardrobe_checks import audit
    faces = analysis.get('identity_check') or {}
    return {**audit(inventory), 'notes_left_by_catalog': len(inventory.get('catalog_notes') or []),
            'faces_not_matching': faces.get('faces_not_matching') or [],
            'face_scenes_checked': faces.get('scenes_checked')}


def report(analysis: dict, adaptation: dict | None = None, cast: dict | None = None) -> dict:
    return {'version': VERSION, 'dialogue': dialogue_metrics(analysis),
            'speakers': speaker_metrics(adaptation, cast), 'voices': voice_metrics(analysis),
            'wardrobe': wardrobe_metrics(analysis)}
