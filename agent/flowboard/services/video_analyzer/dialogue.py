"""One spoken line, once — rebuilt from what is on screen, not from what a shot overlaps.

Speech does not respect cuts, and burned-in subtitles respect them even less: a
caption stays on screen across two or three shots, so shot after shot reports
the same words. Handing that straight to the adaptation is what produced this,
on the reference clip:

    shot 22  "Consider today's training complete."
    shot 23  "Consider today's training complete."
    shot 72  "May I ask for your esteemed name?"
    shot 73  "May I ask — what is your distinguished name?"

— the same sentence written twice, and sentences chopped into fragments
("We trained painstakingly—" / "—our sword qi has reached…"), because each shot
only ever saw its own slice.

So dialogue is rebuilt as a TRACK of whole lines before anything adapts it:

1. Read the caption pieces shot by shot, in order, and keep only pieces not
   already seen — that removes the overlap a persisting subtitle creates.
2. Join pieces into sentences (a sentence ends on . ? ! … or a closing quote).
3. Give each sentence the shot where it STARTS. A shot that merely continues an
   earlier sentence carries no dialogue of its own.

Where a video has no burned-in subtitles, the same track is built from the ASR
segments instead — same shape, worse spelling.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Optional

# How far back to look for a repeat. A caption persists over a few shots, never
# over a scene, and a genuine repeated line ("Mau đi đi. / Đi đi.") must survive
# — so the window is a couple of captions wide, not a scene.
_WINDOW_WORDS = 40
_SENTENCE_END = re.compile(r"[.!?…。！？]['\")»”]?\s*$")
_INTERRUPTION_END = re.compile(r"[-–—]['\")»”]?\s*$")


@dataclass
class Line:
    text: str
    first_shot: int
    last_shot: int
    source: str                    # "subtitle" | "asr" | "caption"
    start: Optional[float] = None
    end: Optional[float] = None
    pieces: list[str] = field(default_factory=list)
    # One-pass lines only: a stable id, the part spoken inside each shot (split at the
    # cuts by word time), review flags, and the timed words the line was built from.
    id: str = ""
    spans: list[dict] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    words: list[dict] = field(default_factory=list, repr=False)

    def as_dict(self) -> dict:
        out = {
            "text": self.text,
            "first_shot": self.first_shot,
            "last_shot": self.last_shot,
            "source": self.source,
            "start": self.start,
            "end": self.end,
        }
        if self.id:
            out.update(id=self.id, spans=self.spans, flags=self.flags)
        return out


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFC", text).lower()
    return re.sub(r"[^\w\s]", "", text, flags=re.UNICODE).strip()


def _sentences(piece: str) -> list[str]:
    """Split a caption piece at sentence ends, keeping the punctuation.

    A reading often arrives as one string holding a whole speech ("…any
    condition. You all go search too. If that senior agrees to join,"). Left
    whole it becomes one enormous line on one shot; split, each sentence lands
    on the shot it appeared in.
    """
    out = [p.strip() for p in re.split(r"(?<=[.!?…。！？])\s+", piece) if p.strip()]
    return out or [piece]


def _pieces(subtitle: Any) -> list[str]:
    if not isinstance(subtitle, str):
        return []
    # Some readings come back with the caption's own line breaks flattened to
    # " / " or "|" — split those back apart, or a repeat hides inside one string.
    flat = subtitle.replace("|", "\n").replace(" / ", "\n")
    out = []
    for raw in flat.split("\n"):
        piece = " ".join(raw.split())
        if piece and _norm(piece):
            out += _sentences(piece)
    return out


def _words(text: str) -> list[str]:
    return [w for w in _norm(text).split() if w]


def _trim_overlap(recent: list[str], piece: str) -> str:
    """Drop the part of ``piece`` the recent caption text already said.

    Captions do not only repeat whole — a two-line caption scrolls, so the next
    shot's reading starts with the tail of the previous one ("…Tiên Minh ta sẽ
    đáp ứng" then "Tiên Minh ta sẽ đáp ứng bất kỳ điều kiện nào."). Matching
    whole strings misses that; matching the overlap does not.
    """
    new = piece.split()
    norm = _words(piece)
    if not norm:
        return ""
    # Already said in full, somewhere in the window?
    window = " ".join(recent)
    if f" {' '.join(norm)} " in f" {window} ":
        return ""
    # Otherwise drop the longest leading run that closes the window.
    for k in range(min(len(norm), len(recent)), 0, -1):
        if recent[-k:] == norm[:k]:
            return " ".join(new[k:]).strip()
    return piece


def from_subtitles(shots: list[dict]) -> list[Line]:
    """Caption pieces → whole sentences, each attached to the shot it starts in."""
    recent: list[str] = []          # normalised words recently seen on screen
    lines: list[Line] = []
    open_line: Optional[Line] = None

    for shot in shots:
        # Only text from EARLIER shots counts as a repeat. Two lines of one
        # caption ("Mau đi đi." / "Đi đi.") are one person saying both, and
        # deduping inside a shot would silently delete the second.
        this_shot: list[str] = []
        for raw in _pieces((shot.get("source") or {}).get("subtitle")):
            piece = _trim_overlap(recent[-_WINDOW_WORDS:], raw)
            if not piece:
                continue
            this_shot += _words(piece)
            if open_line is None:
                open_line = Line(text=piece, first_shot=shot["shot"], last_shot=shot["shot"],
                                 source="subtitle", pieces=[piece])
                lines.append(open_line)
            else:
                open_line.text = f"{open_line.text} {piece}"
                open_line.pieces.append(piece)
                open_line.last_shot = shot["shot"]
            if _SENTENCE_END.search(piece):
                open_line = None
        recent += this_shot
    return lines


def from_transcript(transcript: dict, shots: list[dict]) -> list[Line]:
    """One line per ASR segment, attached to the shot holding its first word."""
    lines: list[Line] = []
    for seg in transcript.get("segments") or []:
        text = " ".join(str(seg.get("text") or "").split())
        if not text:
            continue
        start, end = float(seg["start"]), float(seg["end"])
        first = next((s["shot"] for s in shots if s["start"] <= start < s["end"]), None)
        last = next((s["shot"] for s in reversed(shots) if s["start"] < end <= s["end"]), first)
        if first is None:
            continue
        lines.append(Line(text=text, first_shot=first, last_shot=last or first,
                          source="asr", start=start, end=end))
    return lines


def audio_sentences(transcript: dict, shots: list[dict]) -> list[Line]:
    """Split timed audio at sentence ends, never at a visual cut or caption change.

    Whisper decoder windows may split a sentence. Rejoin adjacent pieces while
    retaining each word once. A terminal interruption dash at an ASR segment
    boundary closes that turn; an internal hyphen does not. Repeated spoken
    sentences are intentional data.
    """
    segments=[]
    current=[]
    def flush():
        if current:
            text=''.join(w['word'] for w in current).strip()
            # A recognised dash or music note alone ("-", "♪") is no utterance: never a line.
            if re.search(r'\w',text):
                segments.append({'start':current[0]['start'],'end':current[-1]['end'],
                                 'text':text,'words':list(current)})
            current.clear()
    previous_language=None
    for seg in transcript.get('segments') or []:
        words=seg.get('words') or []
        if not words:
            flush()
            if re.search(r'\w',str(seg.get('text') or '')):segments.append(seg)
            continue
        if seg.get('source') in ('guided_caption','guided_window'):
            # Recovered speech overlapping another speaker's running sentence is its own
            # utterance; never splice it into the sentence it overlaps.
            flush();current.extend(words);flush();continue
        language=seg.get('language')
        if previous_language and language and previous_language!=language:flush()
        previous_language=language
        for word in words:
            if current:
                gap=word['start']-current[-1]['end']; span=current[-1]['end']-current[0]['start']
                # An unpunctuated run (the recognition's habit with quick exchanges and with
                # narration) breaks at a clear pause once it is long, and at any pause past
                # ~8 s: one line should not carry several sentences or speakers.
                if (gap>.8 or word['start']<current[-1]['end']-.3
                        or (gap>=.5 and span>=3.) or (gap>=.25 and span>=8.)):flush()
            current.append(word)
            # A boundary taken from a caption (not the speech's own punctuation) never
            # leaves a one-word fragment: a rolling caption's "and..." joins what follows.
            if (_SENTENCE_END.search(word['word']) or word.get('voice_turn_end')
                    or (word.get('source_sentence_end') and len(current)>1)):flush()
        # Do not join an interrupted turn to the next segment/speaker. Check
        # only the segment boundary, so hyphenated words inside a segment (even
        # when the decoder tokenizes their hyphen separately) stay intact.
        if (_INTERRUPTION_END.search(str(seg.get('text') or ''))
                or _INTERRUPTION_END.search(str(words[-1].get('word') or ''))):
            flush()
    flush()
    lines=from_transcript({'segments':segments},shots)
    timed={(round(float(s['start']),3),round(float(s['end']),3)):s.get('words') or [] for s in segments}
    for line in lines:
        line.words=timed.get((round(float(line.start),3),round(float(line.end),3)),[])
    return lines


def _shot_at(shots: list[dict], t: float) -> Optional[int]:
    for s in shots:
        if float(s['start']) <= t < float(s['end']):
            return s['shot']
    if shots and t >= float(shots[-1]['end']):
        return shots[-1]['shot']
    return shots[0]['shot'] if shots else None


def _absorb_edge_words(groups: list[tuple], shots: list[dict], *, max_s: float = 0.35, edge_s: float = 0.25) -> list[tuple]:
    """A single word a line starts just before a cut joins the rest of the line after it.

    Editors lead the picture with the voice (a J-cut): "As | of this moment…" puts one
    word on the outgoing shot, which is not a performable beat. A voice that carries on
    over the next shot (an L-cut) stays a continuation piece of that shot."""
    ends = {s['shot']: float(s['end']) for s in shots}
    if len(groups) > 1:
        n, ws = groups[0]
        if (len(ws) == 1 and float(ws[0]['end']) - float(ws[0]['start']) <= max_s
                and ends[n] - float(ws[0]['end']) <= edge_s):
            groups = [(groups[1][0], ws + groups[1][1])] + groups[2:]
    return groups


def split_at_cuts(line: Line, shots: list[dict]) -> list[dict]:
    """The part of ``line`` spoken inside each shot, by each word's midpoint.

    A line without word timings (a caption kept for review) goes whole to the shot that
    holds most of its visible span.
    """
    if line.words:
        groups: list[tuple[int, list[dict]]] = []
        for w in line.words:
            n = _shot_at(shots, (float(w['start']) + float(w['end'])) / 2)
            if groups and groups[-1][0] == n:
                groups[-1][1].append(w)
            else:
                groups.append((n, [w]))
        groups = _absorb_edge_words(groups, shots)
        # A group of punctuation only (the dash of "nine months-" falling past the cut) is
        # no utterance of its own: it stays with the words it punctuates.
        merged: list[tuple[int, list[dict]]] = []
        for n, ws in groups:
            if merged and not re.search(r'\w', ''.join(x['word'] for x in ws)):
                merged[-1][1].extend(ws)
            elif merged and not re.search(r'\w', ''.join(x['word'] for x in merged[-1][1])):
                merged[-1] = (n, merged[-1][1] + ws)
            else:
                merged.append((n, list(ws)))
        groups = merged
        spans = [{'shot': n, 'text': ''.join(x['word'] for x in ws).strip(),
                  'start': round(float(ws[0]['start']), 3), 'end': round(float(ws[-1]['end']), 3)}
                 for n, ws in groups if ''.join(x['word'] for x in ws).strip()]
    else:
        best, overlap = line.first_shot, -1.0
        for s in shots:
            o = min(float(s['end']), float(line.end or 0)) - max(float(s['start']), float(line.start or 0))
            if o > overlap:
                best, overlap = s['shot'], o
        spans = [{'shot': best, 'text': line.text, 'start': line.start, 'end': line.end}]
    for i, span in enumerate(spans):
        span['cont'] = i > 0
    return spans


_TURN_MATCH = 0.6


def split_caption_turns(lines: list[Line], cues: list[dict], shots: list[dict]) -> list[Line]:
    """Split an audio line where it runs from one on-screen caption into the next.

    Speech recognition drops the punctuation between quick exchanges, so two speakers'
    turns can come out as one sentence. When a line holds two captions one after the
    other — each with at least 60% of its words found, in order — it is cut right after
    the last word of the first. A narrator's sentence spread over two captions becomes
    two lines by the same voice, which costs nothing. Translated captions never match.
    """
    from difflib import SequenceMatcher
    from .source_integrity import comparison_tokens
    out = []
    for line in lines:
        flat = [(i, t) for i, w in enumerate(line.words) for t in comparison_tokens(w['word'])]
        if line.source == 'caption' or len(flat) < 6:
            out.append(line)
            continue
        a, b = float(line.words[0]['start']), float(line.words[-1]['end'])
        found, cursor = [], 0
        for cue in cues:
            if len(cue['tokens']) < 3 or cue['end'] <= a - .75 or cue['start'] >= b + .75:
                continue
            m = SequenceMatcher(None, cue['tokens'], [t for _, t in flat[cursor:]], autojunk=False)
            blocks = [x for x in m.get_matching_blocks() if x.size]
            if not blocks or sum(x.size for x in blocks) < _TURN_MATCH * len(cue['tokens']):
                continue
            found.append((flat[cursor + blocks[0].b][0], flat[cursor + blocks[-1].b + blocks[-1].size - 1][0]))
            cursor += blocks[-1].b + blocks[-1].size
        cuts = sorted({prev[1] + 1 for prev, nxt in zip(found, found[1:]) if nxt[0] > prev[1]})
        if not cuts:
            out.append(line)
            continue
        bounds = [0, *cuts, len(line.words)]
        for x, y in zip(bounds, bounds[1:]):
            words = line.words[x:y]
            mid = lambda w: (float(w['start']) + float(w['end'])) / 2
            out.append(Line(text=' '.join(''.join(w['word'] for w in words).split()),
                            first_shot=_shot_at(shots, mid(words[0])), last_shot=_shot_at(shots, mid(words[-1])),
                            source=line.source, start=float(words[0]['start']), end=float(words[-1]['end']),
                            flags=[*line.flags, 'split_at_caption_turn'], words=list(words)))
    return out


def build_track(shots: list[dict], transcript: dict, *, policy: str = 'legacy', deep: bool = False,
                names: set[str] | None = None) -> list[Line]:
    """Subtitles when the video burns them in, speech recognition otherwise.

    Not a mix: the two disagree on spelling constantly ("kiếm cận tu tiên" vs
    the card's "Kiếm Trận Tru Tiên"), and interleaving them is how a line ends
    up written twice in two spellings.
    """
    if policy == 'audio_verbatim':
        # Translated burned-in captions must never replace the original speech.
        # Only VERBATIM captions (same language as the speech, measured by
        # dialogue_fusion.classify) may fix a misheard word or keep, flagged, a
        # caption line the audio pass could not confirm.
        from . import dialogue_fusion, subtitle_cues
        lines = audio_sentences(transcript, shots)
        recovery = transcript.get('caption_recovery') or {}
        cues = subtitle_cues.build(shots, deep=deep)
        # Caption turns are sentence boundaries the audio may not punctuate.
        lines = split_caption_turns(lines, cues, shots)
        if recovery.get('mode') == 'verbatim':
            heard = dialogue_fusion.vocabulary(transcript)
            for k, line in enumerate(lines):
                if not line.words:
                    continue
                # Lines said within two seconds either side are context for the alignment.
                before = lines[k - 1].words if k and lines[k - 1].words and \
                    float(line.words[0]['start']) - float(lines[k - 1].words[-1]['end']) <= 2.0 else []
                after = lines[k + 1].words if k + 1 < len(lines) and lines[k + 1].words and \
                    float(lines[k + 1].words[0]['start']) - float(line.words[-1]['end']) <= 2.0 else []
                own = (len(before), len(before) + len(line.words))
                fixed, receipts = dialogue_fusion.correct_words(before + line.words + after, cues, own=own,
                                                                heard=heard, names=names or set())
                if receipts:
                    # Word timings stay those of the audio; only the misheard spelling changes.
                    line.words = fixed[own[0]:own[1]]
                    line.text = ' '.join(''.join(w['word'] for w in line.words).split())
                    line.flags.append('caption_corrected')
            for fb in recovery.get('fallbacks') or []:
                first = _shot_at(shots, float(fb['start'])) or (fb.get('shots') or [None])[0]
                last = _shot_at(shots, max(float(fb['start']), float(fb['end']) - 1e-3)) or first
                lines.append(Line(text=fb['text'], first_shot=first, last_shot=last, source='caption',
                                  start=float(fb['start']), end=float(fb['end']), flags=['audio_unconfirmed']))
        lines.sort(key=lambda l: (float(l.start if l.start is not None else 1e12), l.first_shot))
        for i, line in enumerate(lines, start=1):
            line.id = f'L{i:04d}'
            line.spans = split_at_cuts(line, shots)
            if line.spans:
                line.first_shot = line.spans[0]['shot']
                line.last_shot = line.spans[-1]['shot']
        return lines
    if policy != 'legacy':
        raise ValueError('Unknown dialogue source policy')
    subtitles = from_subtitles(shots)
    heard = from_transcript(transcript, shots)
    if len(subtitles) >= max(3, len(heard) // 3):
        # Any stretch the subtitles skip (a shout with no caption) still needs a
        # line, so fill those gaps from speech.
        covered = {n for line in subtitles for n in range(line.first_shot, line.last_shot + 1)}
        extra = [l for l in heard if l.first_shot not in covered and l.last_shot not in covered]
        return sorted(subtitles + extra, key=lambda l: (l.first_shot, l.text))
    return heard


def attach(shots: list[dict], track: list[Line]) -> None:
    """Write each shot's own lines onto it, in place.

    ``dialogue_lines``  full sentences that START here — the only place they are
                        ever written.
    ``dialogue_continues`` the shot a sentence running through here started in.
    ``dialogue_heard``  whatever speech recognition caught inside this shot,
                        kept for timing and for a reader comparing to the video.
    """
    if any(line.spans for line in track):
        _attach_pieces(shots, track)
        return
    starts: dict[int, list[Line]] = {}
    running: dict[int, int] = {}
    for line in track:
        starts.setdefault(line.first_shot, []).append(line)
        for n in range(line.first_shot + 1, line.last_shot + 1):
            running.setdefault(n, line.first_shot)

    for shot in shots:
        n = shot["shot"]
        # Written once and kept: on a second pass `dialogue` is already a whole
        # line, and overwriting would lose what was actually heard.
        shot.setdefault("dialogue_heard", shot.get("dialogue", ""))
        shot["dialogue_lines"] = [l.text for l in starts.get(n, [])]
        shot["dialogue_continues"] = running.get(n)
        # `dialogue` stays the field everything downstream reads, and is now a
        # whole line rather than a slice of one.
        shot["dialogue"] = " ".join(shot["dialogue_lines"])


def _attach_pieces(shots: list[dict], track: list[Line]) -> None:
    """One-pass: each shot holds exactly the words spoken inside it.

    ``dialogue_lines``     the pieces of lines spoken in this shot, in time order —
                           a line crossing a cut contributes one piece per shot.
    ``dialogue_pieces``    the same pieces with their line id, timing, whether the
                           piece continues a line begun in an earlier shot, and flags.
    ``dialogue_continues`` the shot where a continued line began, else None.
    ``dialogue_heard``     the pieces confirmed by audio (caption-only lines excluded).
    """
    per: dict[int, list[dict]] = {}
    began = {line.id: line.first_shot for line in track}
    for line in track:
        for span in line.spans:
            per.setdefault(span['shot'], []).append({
                'line_id': line.id, 'text': span['text'], 'start': span['start'], 'end': span['end'],
                'cont': bool(span['cont']), 'source': line.source,
                **({'flags': list(line.flags)} if line.flags else {})})
    for shot in shots:
        pieces = sorted(per.get(shot['shot'], []),
                        key=lambda p: float(p['start']) if p['start'] is not None else float(shot['start']))
        shot['dialogue_pieces'] = pieces
        shot['dialogue_lines'] = [p['text'] for p in pieces]
        continued = next((p for p in pieces if p['cont']), None)
        shot['dialogue_continues'] = began.get(continued['line_id']) if continued else None
        shot['dialogue_heard'] = ' '.join(p['text'] for p in pieces if p['source'] != 'caption')
        shot['dialogue'] = ' '.join(shot['dialogue_lines'])
