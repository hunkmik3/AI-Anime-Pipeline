"""Repair mistyped display prefixes of uniquely host-issued identity tokens.

The exact 80-bit host suffix must survive. No edit distance, names, appearance
or semantic guesses can merge identities; source QA still checks every fact.
"""
from __future__ import annotations

import copy
import re

TOKEN = re.compile(r"^wave-asset[-_].*-([0-9a-f]{20})$")
SINGLE = {"asset_id", "holder_id", "canonical_id", "candidate_id", "target_id"}
MULTI = {"asset_ids", "member_ids", "depends_on_asset_ids", "contains_ids", "present_asset_ids"}


def normalize_reference_ids(value, canonical_ids):
    known = set(canonical_ids)
    by_token = {}
    for key in known:
        match = TOKEN.fullmatch(key)
        if match:
            by_token.setdefault(match[1], []).append(key)
    actions = []

    def fix(key, path):
        if not isinstance(key, str) or key in known:
            return key
        match = TOKEN.fullmatch(key)
        candidates = by_token.get(match[1], []) if match else []
        if len(candidates) != 1:
            return key
        target = candidates[0]
        actions.append({"code": "host_identity_token_repaired", "path": path,
                        "original": key, "canonical": target,
                        "authorizes_identity_or_presence": False})
        return target

    def walk(node, path="", parent=None):
        if isinstance(node, list):
            return [walk(child, f"{path}[{i}]", parent) for i, child in enumerate(node)]
        if not isinstance(node, dict):
            return copy.deepcopy(node)
        result = {}
        for field, child in node.items():
            address = f"{path}.{field}"
            if field in SINGLE or field == "id" and parent == "assets":
                result[field] = fix(child, address)
            elif field in MULTI and isinstance(child, list):
                result[field] = [fix(key, f"{address}[{i}]") for i, key in enumerate(child)]
            else:
                result[field] = walk(child, address, field)
        return result

    return walk(value), actions


def rebind_character_anchors(inventory, evidence, verified_shots):
    """Remove stale anchor ownership only after independent per-shot identity QA.

    A corrected appearance may belong to another existing character. Leaving
    its picture in the old profile would teach subsequent design/identity calls
    the old mistake again. Unreviewed source frames are never reassigned here.
    """
    result = copy.deepcopy(inventory)
    frames = {e['id']: e for e in evidence}
    actions = []
    for asset in result.get('assets', []):
        if asset.get('kind') != 'character':
            continue
        present = {int(n) for n, row in result.get('shots', {}).items()
                   if any(p.get('asset_id') == asset['id'] and p.get('visibility') in {'visible', 'partial'}
                          for p in row.get('asset_presence', []))}
        before = asset.get('evidence_ids', [])
        keep = [ref for ref in before if frames.get(ref, {}).get('shot') not in verified_shots
                or frames[ref]['shot'] in present]
        if keep == before:
            continue
        replacements = [ref for n, row in result.get('shots', {}).items() if int(n) in verified_shots
                        for p in row.get('asset_presence', []) if p.get('asset_id') == asset['id']
                        and p.get('visibility') in {'visible', 'partial'}
                        for ref in p.get('evidence_ids', []) if ref in frames and frames[ref].get('shot') == int(n)]
        replacements = sorted(set(replacements), key=lambda ref: (frames[ref].get('timestamp_s', 0), ref))
        asset['evidence_ids'] = list(dict.fromkeys(keep + (replacements[:1] + replacements[-1:])))
        actions.append({'code': 'character_anchor_ownership_updated', 'asset_id': asset['id'],
                        'before': before, 'after': copy.deepcopy(asset['evidence_ids']),
                        'basis': 'independently_verified_shot_assignments'})
    return result, actions
