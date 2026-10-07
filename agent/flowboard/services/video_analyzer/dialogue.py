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
    source: str                    # "subtitle" | "asr"
    start: Optional[float] = None
    end: Optional[float] = None
    pieces: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "first_shot": self.first_shot,
            "last_shot": self.last_shot,
            "source": self.source,
            "start": self.start,
            "end": self.end,
        }


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
            segments.append({'start':current[0]['start'],'end':current[-1]['end'],
                             'text':''.join(w['word'] for w in current).strip()})
            current.clear()
    previous_language=None
    for seg in transcript.get('segments') or []:
        words=seg.get('words') or []
        if not words:
            flush();segments.append(seg);continue
        language=seg.get('language')
        if previous_language and language and previous_language!=language:flush()
        previous_language=language
        for word in words:
            if current and word['start']-current[-1]['end']>.8:flush()
            current.append(word)
            if _SENTENCE_END.search(word['word']) or word.get('source_sentence_end'):flush()
        # Do not join an interrupted turn to the next segment/speaker. Check
        # only the segment boundary, so hyphenated words inside a segment (even
        # when the decoder tokenizes their hyphen separately) stay intact.
        if (_INTERRUPTION_END.search(str(seg.get('text') or ''))
                or _INTERRUPTION_END.search(str(words[-1].get('word') or ''))):
            flush()
    flush()
    return from_transcript({'segments':segments},shots)


def build_track(shots: list[dict], transcript: dict, *, policy: str = 'legacy') -> list[Line]:
    """Subtitles when the video burns them in, speech recognition otherwise.

    Not a mix: the two disagree on spelling constantly ("kiếm cận tu tiên" vs
    the card's "Kiếm Trận Tru Tiên"), and interleaving them is how a line ends
    up written twice in two spellings.
    """
    if policy == 'audio_verbatim':
        # Translated burned-in captions must never replace the original speech.
        # Even an empty ASR result remains empty: expose a gap, do not invent a dub.
        return audio_sentences(transcript, shots)
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
