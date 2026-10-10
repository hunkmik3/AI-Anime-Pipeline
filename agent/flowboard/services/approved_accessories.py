"""The approved design governs accessories (owner rule, 2026-10-09).

A source observation can name an accessory — a ring, a watch, an earring — that no approved
character design or prop of the clip carries. The approved sheet wins: the mention is left
out of what the writer, its reviewer and the clip review read, with a receipt, and the rest
of the observation stays ("his other hand at his side with a ring visible" → "his other hand
at his side"). Before this rule the writer and its reviewer refused the clip as a source
conflict. An accessory that any approved design or prop of the clip carries is kept untouched.

The stored source, the locked shot contract and its digest are never changed: only the
model-facing copies are.
"""
from __future__ import annotations

import json
import re
from copy import deepcopy

# Words before a noun that make "ring"/"watch" the object, not the verb ("the phone rings",
# "they watch").
_DET = (r"(?:a|an|the|his|her|their|its|one|two|wedding|engagement|signet|diamond|gold(?:en)?|silver|"
        r"steel|thin|thick|large|small|chunky|shiny|metal|plain|luxury)")
KINDS = {
    'ring': (rf"\b(?:{_DET}\s+){{1,3}}rings?\b|\b(?:nose|lip|eyebrow|belly)\s+rings?\b", ('ring',)),
    'watch': (rf"\bwrist\s?watch(?:es)?\b|\b(?:{_DET}\s+){{1,3}}watch(?:es)?\b", ('watch',)),
    'earring': (r"\bear(?:rings?|\s+studs?)\b|\bstud earrings?\b", ('earring', 'stud')),
    'necklace': (r"\bnecklaces?\b|\bpendants?\b", ('necklace', 'pendant')),
    'bracelet': (r"\bbracelets?\b|\bbangles?\b|\banklets?\b", ('bracelet', 'bangle', 'anklet')),
    'brooch': (r"\bbrooch(?:es)?\b|\bcufflinks?\b|\btie\s?(?:clip|pin)s?\b", ('brooch', 'cufflink', 'tie clip', 'tie pin')),
    'piercing': (r"\bpiercings?\b", ('piercing',)),
    'glasses': (r"\b(?:sun|eye|reading)\s?glasses\b|\bspectacles\b", ('glasses', 'spectacles')),
    'name_badge': (r"\bname\s?(?:tags?|badges?|plates?)\b|\bbadges?\b", ('badge', 'name tag', 'name plate')),
    'pin': (r"\b(?:crest|lapel|collar|enamel)\s+pins?\b|\bbrooch pins?\b", ('crest pin', 'lapel pin', 'collar pin', 'enamel pin')),
}
PATTERNS = {kind: re.compile(rx, re.I) for kind, (rx, _) in KINDS.items()}
# The phrase that carries the accessory: "with a ring visible", "wearing his watch".
_CARRIER = r",?\s*(?:with|wearing|showing)\s+(?:[\w'-]+\s+){{0,3}}?(?:{np})(?:\s+(?:visible|showing|glinting|gleaming|on\s+(?:his|her|their|the|one)\s+[\w-]+(?:\s+(?:finger|hand|wrist))?))?"
# Design prose that forbids rather than describes ("avoid: added jewellery").
_SKIP_KEYS = {'avoid', 'avoids', 'negative', 'negatives', 'forbidden', 'do_not', 'dont', 'frames', 'shots',
              'observed_states', 'source_appearances', 'source_asset_presence', 'evidence_ids', 'prompt',
              'plate', 'identity', 'states_seen'}
BASIS = ('Owner rule (2026-10-09): the approved design governs accessories; no approved character design '
         'or prop of this clip carries a {kind}.')


def _design_text(value) -> str:
    if isinstance(value, dict):
        return ' '.join(_design_text(v) for k, v in value.items() if str(k).lower() not in _SKIP_KEYS)
    if isinstance(value, list):
        return ' '.join(_design_text(v) for v in value)
    return value if isinstance(value, str) else ''


def carried(assets) -> set[str]:
    """Accessory kinds that an approved design or prop of the clip names."""
    text = ' '.join(_design_text(a) for a in assets or []).lower()
    return {kind for kind, (_, words) in KINDS.items()
            if any(re.search(rf'\b{re.escape(w)}(?:e?s)?\b', text) for w in words)}


def scrub_text(text: str, keep: set[str]) -> tuple[str, list[tuple[str, str]]]:
    """``text`` without accessory mentions outside ``keep``; ``[(kind, removed)]``."""
    removed = []
    for kind, pattern in PATTERNS.items():
        if kind in keep or not pattern.search(text):
            continue
        phrase = re.compile(_CARRIER.format(np=KINDS[kind][0]), re.I)
        before = text
        text = phrase.sub('', text)
        if pattern.search(text):
            # No carrying phrase to lift out: the clause that names it goes.
            parts = re.split(r'(?<=[,;.])\s+', text)
            text = ' '.join(p for p in parts if not pattern.search(p))
        if text != before:
            removed.append((kind, before))
    text = re.sub(r'\s+([,;.])', r'\1', re.sub(r'\s{2,}', ' ', text)).strip().rstrip(',;').strip()
    return text, removed


def scrub(value, keep: set[str], where: str, receipts: list, shot=None):
    """Every string inside ``value`` scrubbed; receipts name the shot and field."""
    if isinstance(value, str):
        out, removed = scrub_text(value, keep)
        for kind, before in removed:
            receipts.append({'shot': shot, 'field': where, 'accessory': kind, 'source_text': before,
                             'kept_text': out, 'basis': BASIS.format(kind=kind)})
        return out
    if isinstance(value, list):
        out = [scrub(v, keep, where, receipts, shot) for v in value]
        return [v for v in out if v != ''] if all(isinstance(v, str) for v in value) else out
    if isinstance(value, dict):
        return {k: scrub(v, keep, f'{where}.{k}' if where else str(k), receipts, shot) for k, v in value.items()}
    return value


SHOT_FIELDS = ('action', 'staging', 'background', 'framing_note', 'asset_presence',
               'source_appearances', 'production_appearances', 'source_asset_presence')


def _owner_keep(assets) -> dict:
    """Accessories each approved asset itself carries, by its asset id."""
    out = {}
    for a in assets or []:
        if isinstance(a, dict):
            aid = a.get('source_asset_id') or a.get('id') or a.get('key')
            if aid:
                out[aid] = carried([a])
    return out


def _scrub_presences(value, owners, fallback, where, receipts, shot):
    """An asset's own presence state is weighed against that asset's design, not the clip's."""
    if isinstance(value, list):
        return [_scrub_presences(v, owners, fallback, where, receipts, shot) for v in value]
    if isinstance(value, dict):
        keep = owners.get(value.get('asset_id'), fallback) if 'asset_id' in value else fallback
        return {k: (scrub(v, keep, f'{where}.{k}', receipts, shot) if k in ('state', 'position')
                    else _scrub_presences(v, owners, fallback, f'{where}.{k}', receipts, shot)) for k, v in value.items()}
    return scrub(value, fallback, where, receipts, shot)


def reanchor(shot: dict) -> dict:
    """A projected copy keeps its target-casting overlay anchored to the copy's own (projected)
    source appearances; the real overlay was validated on the original shot before projecting."""
    overlay = shot.get('production_adaptation')
    if isinstance(overlay, dict):
        from flowboard.services import production_adaptation
        shot['production_adaptation'] = {**overlay, 'source_digest': production_adaptation.source_digest(shot)}
    return shot


def apply(shots: list[dict], assets) -> tuple[list[dict], list[dict]]:
    """Model-facing shot copies with unapproved accessory mentions left out, and receipts."""
    keep = carried(assets)
    owners = _owner_keep(assets)
    receipts: list[dict] = []
    if not shots:
        return shots, []
    if not any(PATTERNS[k].search(json.dumps(s, ensure_ascii=False)) for s in shots for k in PATTERNS):
        return shots, []
    out = []
    for i, shot in enumerate(shots, 1):
        shot = deepcopy(shot)
        for field in SHOT_FIELDS:
            if field not in shot:
                continue
            if field in ('asset_presence', 'source_appearances', 'production_appearances', 'source_asset_presence'):
                shot[field] = _scrub_presences(shot[field], owners, keep, field, receipts, i)
            else:
                shot[field] = scrub(shot[field], keep, field, receipts, i)
        if isinstance(shot.get('production_adaptation'), dict):
            overlay = shot['production_adaptation']
            shot['production_adaptation'] = {**overlay, 'asset_presence': _scrub_presences(
                overlay.get('asset_presence') or [], owners, keep, 'production_adaptation', receipts, i)}
        out.append(reanchor(shot))
    return out, receipts


def scrub_copy(value, assets):
    """A deep copy of any model-facing structure with unapproved accessory mentions left out."""
    return scrub(deepcopy(value), carried(assets), '', [])


def scrub_package(package: dict | None, assets=None) -> tuple[dict | None, list[dict]]:
    """A shot package copy for a model to read, its shots scrubbed against the package's
    own approved materials (and ``assets`` when given)."""
    if not isinstance(package, dict) or not package.get('shots'):
        return package, []
    keep = carried([*(package.get('materials') or {}).values(), *(assets or [])])
    receipts: list[dict] = []
    copy = deepcopy(package)
    for i, shot in enumerate(copy['shots'], 1):
        for field in ('action', 'staging', 'background', 'composition', 'observed', 'previous_state', 'end_state'):
            if field in shot:
                shot[field] = scrub(shot[field], keep, field, receipts, i)
    return copy, receipts
