"""Which reskin source-review findings stop a clip, and which only inform it.

A reskin clip takes every shot's cuts, camera, framing, blocking, screen positions and set
geometry from the white-model video rendered from the source pixels; the writer drops
those texts. What the clay cannot carry must be right before a clip is generated: the
words, who says them and how, who is on screen and what they wear. So a finding about
speech or identity stops its own clip, and every other clip goes on. A finding about
camera, positions, set wording or cut timing is shown as a warning, as are the model's
free-text notes (its real problems also arrive as criterion statuses) and differences
that are only spellings of one spoken form or an interjection.

The owner can accept a blocking finding. An acceptance is keyed to that finding in that
receipt, so it lapses as soon as the shot is edited and reviewed again.
"""
from __future__ import annotations

from datetime import datetime, timezone

from flowboard.services import production_manifest as pm
from flowboard.services.video_analyzer.source_integrity import comparison_tokens

BOARD_KEY = 'reskinFindingDispositions'
# Carried by the white-model motion video, so never a reason to stop a reskin clip.
MOTION_CARRIED = frozenset({'cuts_timing', 'framing_camera', 'action_positions', 'environment_props'})
WARNING_ORIGINS = frozenset({'model_note', 'boundary', 'speech_position', 'speech_delivery_cut', 'speech_interjection'})
BLOCKING_ORIGINS = frozenset({'speech_words', 'speech_speaker'})

# Spellings of one spoken form: the clip speaks the shotlist's words either way.
SPOKEN_FORMS = {
    'gotta': 'got to', 'gonna': 'going to', 'wanna': 'want to', 'gimme': 'give me', 'lemme': 'let me',
    'kinda': 'kind of', 'sorta': 'sort of', 'outta': 'out of', 'lotta': 'lot of', 'dunno': 'do not know',
    'cause': 'because', 'cuz': 'because', 'coz': 'because', 'ok': 'okay', 'aye': 'i',
    "we're": 'we are', "they're": 'they are', "i've": 'i have', "you've": 'you have', "we've": 'we have',
    "they've": 'they have', "won't": 'will not', "wouldn't": 'would not', "shouldn't": 'should not',
    "couldn't": 'could not', "doesn't": 'does not', "aren't": 'are not', "wasn't": 'was not',
    "weren't": 'were not', "haven't": 'have not', "hasn't": 'has not', "let's": 'let us',
    "there's": 'there is', "what's": 'what is', "who's": 'who is', "here's": 'here is', "where's": 'where is',
    "you'll": 'you will', "we'll": 'we will', "they'll": 'they will', "he'll": 'he will', "she'll": 'she will',
}
INTERJECTIONS = frozenset({
    'ah', 'ahh', 'oh', 'ohh', 'uh', 'uhh', 'um', 'umm', 'hmm', 'hm', 'mm', 'mmm', 'huh', 'ha', 'hah',
    'haha', 'eh', 'ugh', 'whoa', 'ow', 'oops', 'phew', 'psst', 'shh', 'tsk', 'argh', 'aw', 'aww',
})


def spoken(text) -> list[str]:
    """Comparison tokens with colloquial spellings folded to one form."""
    return [t for token in comparison_tokens(text) for t in SPOKEN_FORMS.get(token, token).split()]


def word_difference(expected: list[str], heard: list[str]) -> str | None:
    """None when the words agree, 'speech_interjection' when only interjections differ,
    else 'speech_words'."""
    if expected == heard:
        return None
    strip = lambda words: [w for w in words if w not in INTERJECTIONS]
    return 'speech_interjection' if strip(expected) == strip(heard) else 'speech_words'


def severity(finding: dict) -> str:
    """'blocking' or 'warning' for one finding of a reskin source review."""
    criterion, origin = finding.get('criterion'), finding.get('origin')
    if criterion == 'review_incomplete':
        return 'blocking'
    if origin in WARNING_ORIGINS:
        return 'warning'
    if origin in BLOCKING_ORIGINS:
        return 'blocking'
    # A criterion status, or a report written before findings carried an origin.
    return 'warning' if criterion in MOTION_CARRIED else 'blocking'


def finding_key(receipt_digest: str, finding: dict) -> str:
    """Stable identity of a finding inside one receipt (the receipt digest covers the shots,
    catalog, prompt and model, so any edit gives every finding a new key)."""
    body = {k: v for k, v in finding.items() if k not in {'key', 'severity', 'clip'}}
    return pm.digest({'receipt': receipt_digest, 'finding': body})


def annotate(findings: list[dict], receipt_digest: str) -> list[dict]:
    return [{**f, 'key': finding_key(receipt_digest, f), 'severity': severity(f)} for f in findings]


def accepted(board: dict) -> dict:
    value = board.get(BOARD_KEY)
    return value if isinstance(value, dict) else {}


def blocking(findings: list[dict], receipt_digest: str, board: dict) -> list[dict]:
    """The findings that stop this receipt's clip: blocking and not accepted by the owner."""
    done = accepted(board)
    return [f for f in annotate(findings, receipt_digest) if f['severity'] == 'blocking' and f['key'] not in done]


def describe(finding: dict) -> str:
    where = finding.get('shot') or finding.get('clip') or ''
    what = finding.get('message') or finding.get('evidence') or finding.get('criterion') or 'phát hiện nguồn'
    return (f'{where}: ' if where else '') + str(what)[:200]


def summarize(report_findings: list[dict]) -> dict:
    counts = {'blocking': 0, 'warning': 0}
    clips = set()
    for f in report_findings:
        level = f.get('severity') or severity(f)
        counts[level] = counts.get(level, 0) + 1
        if level == 'blocking':
            clips.add(f.get('clip'))
    return {**counts, 'blocked_clips': sorted(c for c in clips if c)}


def accept(board: dict, keys: list[str], user: str, note: str = '') -> dict:
    """Board with the owner's acceptance of these blocking findings of the current review."""
    review = board.get('reskinSourceReview') or {}
    current = {f.get('key'): f for f in review.get('findings', []) if f.get('key')}
    unknown = [k for k in keys if k not in current]
    if unknown:
        raise ValueError('Phát hiện không còn trong lần đối chiếu hiện tại; tải lại rồi chọn lại.')
    if any(current[k].get('criterion') == 'review_incomplete' for k in keys):
        raise ValueError('Lượt đối chiếu chưa có kết quả; không thể chấp nhận thay cho bằng chứng nguồn.')
    stamp = datetime.now(timezone.utc).isoformat()
    done = dict(accepted(board))
    for k in keys:
        done[k] = {'decision': 'accepted', 'by': user, 'at': stamp, 'clip': current[k].get('clip'),
                   **({'note': note[:500]} if note else {})}
    return {**board, BOARD_KEY: done}
