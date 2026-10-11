"""Catalog independent story segments side by side, in waves, merged in film order.

The one-pass catalog is sequential because each batch extends one shared identity
catalog and reads the batches just before it. Across a story-sequence boundary that
does not cut an utterance (the same boundary a failed batch already restarts at), the
batches before it are no longer read: only the catalog is. So the first segment is
catalogued alone, establishing the main cast, and later segments run several at a time,
each from the catalog merged so far. Merging is local and free:

- a new character in a segment becomes an earlier character only on evidence: its
  faces match that character's faces, or its high-confidence voice is that character's
  voice and no face contradicts it; never when both are visible in one shot;
- a new environment, prop or group becomes an earlier one only with the same kind and
  the same name;
- every other new ID that is already taken is renamed for its segment; scenes are never
  merged across segments;
- a character's wardrobe state key that two segments define differently is renamed for
  the later segment.

Everything left uncertain (a same-named character with no face or voice evidence) stays
separate and is recorded as a note for the owner. Each segment's paid calls are journaled
by payload as before, and the merge is deterministic, so a resume repays nothing.
"""
from __future__ import annotations

import re
from collections import Counter
from copy import deepcopy

FACE_SAME = 0.62        # median similarity of a new person's faces to an earlier person's mean face
FACE_MARGIN = 0.08      # over the next best earlier person
FACE_CONTRADICTS = 0.45
MIN_FACES = 2
VOICE_LINES = 3         # high-confidence lines of a voice already given to one character
VOICE_SHARE = 0.7
# Lists of receipts a span appends to; a segment's own items are what it added past its seed.
NOTE_KEYS = ('catalog_notes', 'discarded_profile_restatements', 'discarded_wardrobe_posture_restatements',
             'discarded_wardrobe_metadata_restatements', 'reference_retrieval', 'reused_catalog_shots',
             'audiovisual_speaker_repairs', 'source_audio_observation_findings')
_FREE_TEXT = frozenset({'label', 'name', 'description', 'message', 'text', 'line', 'wardrobe', 'look',
                        'posture', 'reason', 'summary', 'action', 'state', 'evidence', 'note'})


def segments(shots: list[dict], sequences: list[dict]) -> list[tuple[int, int]]:
    """Index spans of shots split at declared sequence starts that do not cut an utterance."""
    starts = {s.get('first_shot') for s in sequences or [] if isinstance(s, dict) and isinstance(s.get('first_shot'), int)}
    cuts = [i for i, shot in enumerate(shots) if i and shot['shot'] in starts
            and not any(p.get('cont') for p in shot.get('dialogue_pieces') or [])]
    bounds = [0, *cuts, len(shots)]
    return [(a, b) for a, b in zip(bounds, bounds[1:]) if b > a]


def waves(spans: list[tuple[int, int]], parallel: int) -> list[list[tuple[int, int]]]:
    """The first segment alone (it establishes the main cast), then ``parallel`` at a time."""
    if not spans:
        return []
    rest = spans[1:]
    step = max(1, int(parallel))
    return [[spans[0]], *[rest[i:i + step] for i in range(0, len(rest), step)]]


def remap(value, aliases: dict):
    """Every ID equal to an alias key, in values and dict keys, outside free-text fields."""
    if not aliases:
        return value
    if isinstance(value, str):
        return aliases.get(value, value)
    if isinstance(value, list):
        return [remap(v, aliases) for v in value]
    if isinstance(value, dict):
        return {aliases.get(k, k): (v if k in _FREE_TEXT and isinstance(v, str) else remap(v, aliases))
                for k, v in value.items()}
    return value


def _norm(name) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', str(name or '').lower()).strip()


def _visible_together(rows: dict, a: str, b: str) -> bool:
    for row in rows.values():
        seen = {p.get('asset_id') for p in row.get('asset_presence') or [] if p.get('visibility') in ('visible', 'partial')}
        if a in seen and b in seen:
            return True
    return False


def _face_vectors(rows: dict, assets: dict, faces: dict) -> dict:
    if not faces:
        return {}
    from . import face_id
    kinds = {aid: a.get('kind') for aid, a in assets.items()}
    return {aid: [v for _, _, v in items] for aid, items in face_id.samples(list(rows.items()), faces, kinds).items()}


def _centre(vectors):
    import numpy as np
    X = np.array(vectors, dtype=np.float32)
    c = X.mean(axis=0)
    return c / (np.linalg.norm(c) or 1.0)


def _voices(line_speakers: dict, track: list[dict]) -> dict:
    """{asset_id: Counter(voice)} over high-confidence lines."""
    by_line = {l.get('id'): l for l in track or []}
    out: dict[str, Counter] = {}
    for lid, speaker in line_speakers.items():
        line = by_line.get(lid) or {}
        if line.get('voice_confidence') == 'high' and line.get('voice') and (speaker or {}).get('asset_id'):
            out.setdefault(speaker['asset_id'], Counter())[line['voice']] += 1
    return out


def match_new_assets(new_ids, seg, merged, faces, seg_speakers, merged_speakers, track):
    """({new id: earlier id}, notes) for the new assets of one segment against the catalog
    merged so far (the seed plus earlier segments of this wave)."""
    import numpy as np
    seg_assets = {a['id']: a for a in seg['assets']}
    earlier = {a['id']: a for a in merged['assets']}
    aliases, notes = {}, []
    seg_faces = _face_vectors(seg['shots'], seg_assets, faces)
    old_faces = _face_vectors(merged['shots'], earlier, faces)
    centres = {aid: _centre(v) for aid, v in old_faces.items() if len(v) >= MIN_FACES}
    seg_voice, old_voice = _voices(seg_speakers, track), _voices(merged_speakers, track)
    owner = {}
    for voice in {v for c in old_voice.values() for v in c}:
        counts = Counter({aid: c[voice] for aid, c in old_voice.items() if c[voice]})
        aid, n = counts.most_common(1)[0]
        if n >= VOICE_LINES and n / sum(counts.values()) >= VOICE_SHARE:
            owner[voice] = aid
    for nid in new_ids:
        asset = seg_assets[nid]
        kind = asset.get('kind')
        same_kind = [aid for aid, a in earlier.items() if a.get('kind') == kind and not _visible_together(seg['shots'], nid, aid)]
        if kind != 'character':
            named = [aid for aid in same_kind if _norm(earlier[aid].get('name')) == _norm(asset.get('name')) and _norm(asset.get('name'))]
            if len(named) == 1:
                aliases[nid] = named[0]
            continue
        scores = {}
        mine = seg_faces.get(nid) or []
        if len(mine) >= MIN_FACES:
            X = np.array(mine, dtype=np.float32)
            scores = {aid: float(np.median(X @ centres[aid])) for aid in same_kind if aid in centres}
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        by_face = (ranked[0][0] if ranked and ranked[0][1] >= FACE_SAME
                   and (len(ranked) < 2 or ranked[0][1] - ranked[1][1] >= FACE_MARGIN) else None)
        voice = (seg_voice.get(nid) or Counter()).most_common(1)
        by_voice = owner.get(voice[0][0]) if voice and voice[0][1] >= 2 else None
        if by_voice not in same_kind:
            by_voice = None
        if by_face and by_voice and by_face != by_voice:
            notes.append({'code': 'identity_evidence_disagrees', 'asset_id': nid, 'face': by_face, 'voice': by_voice})
            continue
        if by_voice and by_voice in scores and scores[by_voice] < FACE_CONTRADICTS:
            notes.append({'code': 'voice_without_face', 'asset_id': nid, 'voice': by_voice, 'face_similarity': round(scores[by_voice], 3)})
            continue
        target = by_face or by_voice
        if target:
            aliases[nid] = target
            notes.append({'code': 'identity_merged', 'asset_id': nid, 'into': target,
                          'by': 'face' if by_face else 'voice', 'face_similarity': round(scores.get(target, 0.0), 3)})
            continue
        twin = [aid for aid in same_kind if _norm(earlier[aid].get('name')) == _norm(asset.get('name'))]
        if twin:
            notes.append({'code': 'possible_duplicate_identity', 'asset_id': nid, 'like': twin,
                          'message': 'Same name as an earlier character, without face or voice evidence; kept separate.'})
    return aliases, notes


def absorb(merged: dict, seed: dict, seg: dict, key: str, faces: dict, merged_speakers: dict,
           seg_speakers: dict, seed_speakers: dict, track: list[dict]) -> dict:
    """Merge one finished segment (catalogued from ``seed``) into ``merged`` in place;
    returns its receipt. ``merged_speakers`` is extended with the segment's own lines."""
    from . import source_inventory as inv
    seed_ids = {a['id'] for a in seed['assets']}
    seed_scenes = {s['id'] for s in seed['scenes']}
    new_ids = [a['id'] for a in seg['assets'] if a['id'] not in seed_ids]
    own_speakers = {lid: v for lid, v in seg_speakers.items() if lid not in seed_speakers}
    aliases, notes = match_new_assets(new_ids, seg, merged, faces, own_speakers, merged_speakers, track)
    taken = {a['id'] for a in merged['assets']}
    renames = {nid: f'{nid}-{key}' for nid in new_ids if nid not in aliases and nid in taken}
    taken_scenes = {s['id'] for s in merged['scenes']}
    scene_renames = {s['id']: f"{s['id']}-{key}" for s in seg['scenes'] if s['id'] not in seed_scenes and s['id'] in taken_scenes}
    ids = {**aliases, **renames, **scene_renames}
    rows = {n: remap(row, ids) for n, row in seg['shots'].items() if n not in seed['shots']}
    assets = {a['id']: a for a in merged['assets']}
    state_renames = []
    for raw in seg['assets']:
        asset = remap(raw, ids)
        aid = asset['id']
        if aid not in assets:
            assets[aid] = asset
            continue
        if raw['id'] in seed_ids and asset == assets[aid]:
            continue
        if asset.get('kind') != 'character':
            continue
        # A known (or merged) person: add this segment's wardrobe states, renaming a key
        # another segment already gave a different outfit.
        states = {s['key']: s for s in assets[aid].get('states', [])}
        for st in asset.get('states', []):
            if st['key'] not in states:
                states[st['key']] = st
            elif states[st['key']] != st:
                new_key = f"{st['key']}-{key}"
                states[new_key] = {**st, 'key': new_key}
                state_renames.append({'asset_id': aid, 'from': st['key'], 'to': new_key})
                for row in rows.values():
                    if (row.get('character_states') or {}).get(aid) == st['key']:
                        row['character_states'][aid] = new_key
        assets[aid] = {**assets[aid], 'states': list(states.values())}
    merged['assets'] = list(assets.values())
    inv._merge(merged, {'assets': [], 'scenes': [remap(s, ids) for s in seg['scenes'] if s['id'] not in seed_scenes],
                        'shots': rows})
    for name in NOTE_KEYS:
        added = seg.get(name, [])[len(seed.get(name, [])):]
        if added:
            merged.setdefault(name, []).extend(remap(deepcopy(added), ids))
    merged_speakers.update({lid: remap(v, ids) for lid, v in own_speakers.items()})
    receipt = {'segment': key, 'shots': sorted(int(n) for n in rows), 'new_assets': len(new_ids),
               'merged': aliases, 'renamed': renames, 'scenes_renamed': scene_renames,
               'states_renamed': state_renames, 'notes': notes}
    merged.setdefault('parallel_catalog', []).append(receipt)
    return receipt


def dangling(known: dict) -> list[str]:
    """IDs a row refers to that no asset defines (must be empty after a merge)."""
    ids = {a['id'] for a in known['assets']}
    missing = set()
    for row in known['shots'].values():
        for p in row.get('asset_presence') or []:
            for ref in (p.get('asset_id'), p.get('holder_id'), *(p.get('contains_ids') or [])):
                if ref and ref not in ids:
                    missing.add(ref)
        for ref in (row.get('character_states') or {}):
            if ref not in ids:
                missing.add(ref)
        if row.get('environment_key') and row['environment_key'] not in ids:
            missing.add(row['environment_key'])
    return sorted(missing)
