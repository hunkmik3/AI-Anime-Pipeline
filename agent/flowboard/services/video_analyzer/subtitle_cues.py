"""Timed on-screen caption cues, from the one-pass vision reading.

The vision pass reports each caption with the sampled frames it is visible in
(``subtitle_events: [{text, first_frame: "S23_F2", last_frame: "S23_F5"}]``). A frame
id names its shot and sample index, and ``frames.sample_points`` gives that sample's
time, so every caption gets a visible span in seconds. A caption held across a cut is
ONE cue; a caption that carries two speakers' dash-led turns is two cues.

Cues are evidence for where speech is and how it is worded. Whether their text may
stand in for speech is decided by the caller (``dialogue_fusion``), never here.
"""
from __future__ import annotations

import re

from . import frames
from .source_integrity import JOINED_CAPTIONS, comparison_tokens

FRAME_ID = re.compile(r"^S(\d+)_F(\d+)$")
_DASH_TURN = re.compile(r"(?:^|\s)[-–—]\s+(?=\S)")


def _span(shot: dict, first: int, last: int, deep: bool) -> tuple[float, float]:
    """Visible span of samples ``first..last`` (1-based), widened halfway to the
    neighbouring samples — the caption appeared and left somewhere in those gaps."""
    start, end = float(shot['start']), float(shot['end'])
    dur = max(end - start, 1e-6)
    pts = frames.sample_points(dur, deep=deep)
    i = min(max(first, 1), len(pts)) - 1
    j = min(max(last, first, 1), len(pts)) - 1
    lo = 0.0 if i == 0 else (pts[i - 1] + pts[i]) / 2
    hi = 1.0 if j == len(pts) - 1 else (pts[j] + pts[j + 1]) / 2
    return start + lo * dur, start + hi * dur


def _turns(text: str) -> list[str]:
    """``"- Oh no. - Ha, okay!"`` is two people; a hyphenated word is not."""
    if not _DASH_TURN.match(text) and not re.search(r"\s[-–—]\s+\S", text):
        return [text]
    parts = [p.strip() for p in _DASH_TURN.split(text) if p.strip()]
    return parts or [text]


_WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)


def _cut(text: str, n: int) -> tuple[str, str]:
    """``text`` split right after its first ``n`` comparison tokens; ``(text, '')`` when no
    word boundary falls exactly there (a contraction straddling it)."""
    count = 0
    for m in _WORD.finditer(text):
        count += len(comparison_tokens(m.group(0)))
        if count == n:
            end = m.end()
            while end < len(text) and not text[end].isspace() and not _WORD.match(text, end):
                end += 1            # the word's own punctuation stays with it
            return text[:end].strip(), text[end:].strip(' \t\n,;:-–—')
        if count > n:
            break
    return text, ''


def _extend(cue: dict, shot: int, end: float) -> None:
    if shot not in cue['shots']:
        cue['shots'].append(shot)
    cue['end'] = max(cue['end'], end)


def build(shots: list[dict], *, deep: bool = False) -> list[dict]:
    """Caption cues in film order: ``{id, text, tokens, start, end, shots}``."""
    cues: list[dict] = []
    for shot in shots:
        source = shot.get('source') or {}
        events = [e for e in source.get('subtitle_events') or [] if isinstance(e, dict) and e.get('text')]
        if not events and source.get('subtitle'):
            # Older readings carry only the joined text: the caption spans the shot.
            events = [{'text': t} for t in JOINED_CAPTIONS.split(str(source['subtitle'])) if t.strip()]
        for event in events:
            ids = [FRAME_ID.match(str(event.get(k) or '')) for k in ('first_frame', 'last_frame')]
            if all(ids) and int(ids[0].group(1)) == int(ids[1].group(1)) == int(shot['shot']):
                a, b = _span(shot, int(ids[0].group(2)), int(ids[1].group(2)), deep)
            else:
                a, b = float(shot['start']), float(shot['end'])
            turns = _turns(' '.join(str(event['text']).split()))
            weights = [max(1, len(comparison_tokens(t))) for t in turns]
            cursor = a
            for text, w in zip(turns, weights):
                toks = comparison_tokens(text)
                if not toks:
                    continue
                share = (b - a) * w / sum(weights)
                piece_start, piece_end = cursor, cursor + share
                cursor = piece_end
                last = cues[-1] if cues else None
                held = last and last['shots'][-1] >= shot['shot'] - 1 and piece_start - last['end'] < 1.0
                if held and last['tokens'] == toks:
                    # The same caption still on screen after the cut.
                    _extend(last, shot['shot'], piece_end)
                    continue
                n = len(last['tokens']) if held else 0
                if held and len(toks) - n >= 2 and toks[:n] == last['tokens']:
                    # A rolling caption gained a line: the old line stays, the new one starts now.
                    head, rest = _cut(text, n)
                    if rest:
                        _extend(last, shot['shot'], piece_end)
                        cues.append({'text': rest, 'tokens': toks[n:], 'start': round(piece_start, 3),
                                     'end': round(piece_end, 3), 'shots': [shot['shot']]})
                        continue
                if held and n - len(toks) >= 2 and last['tokens'][n - len(toks):] == toks:
                    # Its first line left the screen; the line still shown has been there all along.
                    head, rest = _cut(last['text'], n - len(toks))
                    if rest:
                        tail = {'text': rest, 'tokens': toks, 'start': last['start'], 'end': round(piece_end, 3),
                                'shots': [*last['shots'], *([shot['shot']] if shot['shot'] not in last['shots'] else [])]}
                        last.update(text=head, tokens=last['tokens'][:n - len(toks)])
                        cues.append(tail)
                        continue
                if held:
                    # Its last words open the next caption ("…I can't believe I let him freak
                    # me out" / "I can't believe I let him freak me out enough to call you
                    # here."): the shared sentence was on screen from the first.
                    k = next((k for k in range(min(n, len(toks)) - 1, 2, -1)
                              if last['tokens'][n - k:] == toks[:k]), 0)
                    head, rest = _cut(last['text'], n - k) if k else ('', '')
                    if rest:
                        last.update(text=head, tokens=last['tokens'][:n - k])
                        cues.append({'text': text, 'tokens': toks, 'start': last['start'], 'end': round(piece_end, 3),
                                     'shots': [*last['shots'], *([shot['shot']] if shot['shot'] not in last['shots'] else [])]})
                        continue
                cues.append({'text': text, 'tokens': toks, 'start': round(piece_start, 3),
                             'end': round(piece_end, 3), 'shots': [shot['shot']]})
    for i, cue in enumerate(cues, start=1):
        cue['id'] = f'C{i:04d}'
        cue['end'] = round(cue['end'], 3)
    return cues
