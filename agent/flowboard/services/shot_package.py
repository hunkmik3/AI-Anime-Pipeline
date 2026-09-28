"""Compile reproducible shot preparation from approved target data, without paid calls.

Source observations remain untouched. A package is production intent, never a new
verification of the source or proof that a generated frame matches its references.
"""
from copy import deepcopy
import json
import re

from flowboard.services import production_adaptation, production_manifest as pm

POLICY = (
    'Match the approved reference identities, wardrobe, prop geometry and relative scale. '
    'A camera cut is not a costume change, exit or prop transfer. Keep recurring crowds in '
    'the same continuous scene; show only the subset allowed by the framing. Offscreen and '
    'inherited assets are not automatically visible. Prior screen-left/right positions are not world coordinates; '
    'preserve set geometry across reverse angles without copying screen positions. Never invent a hand, holder, measurement or background position. Keep all dialogue in English exactly as supplied; no translated '
    'or additional speech. Flag conflicts rather than silently rewriting source facts.'
)


def _id(item):
    return item.get('source_asset_id') or item.get('id') or item.get('key')


def build(board, project_id='', sequence_key=''):
    target = deepcopy(board)
    adaptation_issues = []
    nodes = {}
    aliases = {}
    for node in target.get('nodes', []):
        d = node.get('data', {})
        if d.get('kind') == 'sequence':
            projected = []
            for shot in d.get('shots', []):
                try:
                    projected.append(production_adaptation.apply(shot))
                except ValueError as exc:
                    projected.append(shot)
                    adaptation_issues.append({'sequence_key': d['sequence']['key'], 'code': 'invalid_adaptation',
                                              'blocking': True, 'message': str(exc)})
            d['shots'] = projected
        elif d.get('kind') in ('character', 'environment', 'asset'):
            item = d[d['kind']]
            key = _id(item)
            if key:
                nodes[key] = d
                aliases[item.get('key', key)] = key
    manifest = pm.build(target, project_id)
    assets = manifest['assets']
    sequences = {n['data']['sequence']['key']: n['data'] for n in target.get('nodes', [])
                 if n.get('data', {}).get('kind') == 'sequence'}
    clips = []
    for key, seq in sequences.items():
        if sequence_key and key != sequence_key:
            continue
        rows = [r for r in manifest['shots'] if r['sequence_key'] == key]
        issues = [deepcopy(i) for i in manifest['issues'] if i['shot'] in {r['id'] for r in rows}]
        issues += [i for i in adaptation_issues if i['sequence_key'] == key]
        materials = {}
        shots = []
        variants = {}
        for index, row in enumerate(rows):
            shot = row['definition']
            # Use observed/last-known scope, not the whole film's scene asset union.
            needed = set(row['start_state']) | set(row['end_state'])
            needed.update(p['asset_id'] for p in shot.get('asset_presence', [])
                          if p.get('asset_id') and p.get('visibility') in ('visible', 'partial', 'occluded'))
            needed.update(aliases.get(k, k) for k in shot.get('character_keys', []))
            env_key = shot.get('environment_key') or seq['sequence'].get('environment_key')
            if env_key:
                needed.add(aliases.get(env_key, env_key))
            pending = list(needed)
            while pending:
                definition = assets.get(pending.pop(), {}).get('definition', {})
                for dep in definition.get('depends_on_asset_ids', []) + definition.get('member_ids', []):
                    if dep not in needed:
                        needed.add(dep)
                        pending.append(dep)
            bindings = []
            for aid in sorted(needed):
                record = assets.get(aid, {})
                d = nodes.get(aid, {})
                design = d.get(d.get('kind', ''), {})
                definition = record.get('definition', {})
                kind = design.get('kind') or definition.get('kind') or d.get('kind', 'unknown')
                state = (shot.get('character_states') or {}).get(design.get('key')) or d.get('activeState')
                state_sheet = (d.get('states') or {}).get(state, {})
                plate = (state_sheet if state_sheet.get('referenceUrl') else d.get('identity', {})) if kind == 'character' else d.get('plate', {})
                material_key = aid + (':' + str(state) if state else '')
                known_states = design.get('states') or []
                costume = next((s for s in known_states if s.get('key') == state), {})
                if kind == 'character':
                    variants.setdefault(aid, set()).add(state or 'identity')
                    if state and not state_sheet.get('referenceUrl') and len(known_states) > 1:
                        issues.append({'shot': row['id'], 'asset': aid, 'code': 'missing_costume_sheet', 'blocking': True})
                if not plate.get('referenceUrl'):
                    issues.append({'shot': row['id'], 'asset': aid, 'code': 'missing_material', 'blocking': True})
                style_match = re.search(r'(?:^|\n)STYLE[:\s]*\n?(.*?)(?=\n\s*\n|$)', plate.get('prompt', ''), re.S)
                material = {
                    'asset_id': aid, 'kind': kind, 'name': design.get('name') or definition.get('name') or aid,
                    'state_key': state, 'version': record.get('version', ''),
                    'reference_url': plate.get('referenceUrl', ''), 'media_id': plate.get('mediaId', ''),
                    'description': design.get('target_description') or design.get('description') or design.get('summary') or definition.get('description', ''),
                    'wardrobe': costume.get('wardrobe', ''),
                    'identity_anchor': design.get('identity_anchor', ''),
                    'geometry_and_scale': design.get('dimensions') or definition.get('dimensions') or
                        'Match the same reference geometry and proportions relative to hands/body; no invented numeric dimensions.',
                    'style_note': style_match.group(1).strip() if style_match else '',
                    'lighting': design.get('lighting', ''), 'layout_lock': design.get('lock', ''),
                }
                materials[material_key] = material
                bindings.append(material_key)
            presence = {p['asset_id']: p for p in shot.get('asset_presence', []) if p.get('asset_id')}
            # No invented movement at the cut: opening observations belong to this angle;
            # prior screen coordinates are kept separately as context, not copied across axes.
            visible = [aid for aid,p in presence.items() if p.get('visibility') in ('visible', 'partial', 'occluded')]
            inherited = [aid for aid in row['start_state'] if aid not in presence]
            reasons = []
            if index == 0 or (index and rows[index-1]['continuity_scope'] != row['continuity_scope']): reasons.append('scene_or_clip_opening')
            if row['transitions']: reasons.append('prop_or_costume_transition')
            if any(materials[b]['kind'] == 'background_group' for b in bindings): reasons.append('recurring_crowd')
            if any(materials[b]['kind'] == 'prop' for b in bindings): reasons.append('prop_geometry_and_hand_interaction')
            dialogue = deepcopy(shot.get('dialogue') or [])
            if any(re.search(r'[\u3400-\u9fff]', str(d.get('line', ''))) for d in dialogue):
                issues.append({'shot': row['id'], 'code': 'dialogue_not_english', 'blocking': True,
                               'message': 'Locked English dialogue contains CJK text; review translation explicitly.'})
            from flowboard.services.automation import speech_seconds
            speech = sum(speech_seconds(d['line']) for d in dialogue if d.get('line'))
            if speech > float(shot.get('duration_s') or 0):
                issues.append({'shot': row['id'], 'code': 'dialogue_timing_risk', 'blocking': False,
                               'message': 'Estimated speech exceeds source timing; do not stretch or translate automatically.'})
            shots.append({'id': row['id'], 'index': index, 'source_shots': row['source_shots'],
                          'scene_id': row['scene_id'], 'continuity_scope': row['continuity_scope'],
                          'duration_s': row['duration_s'], 'framing': shot.get('framing', ''),
                          'camera': shot.get('camera', ''), 'lens_mm': shot.get('lens_mm', ''),
                          'composition': shot.get('framing_note', ''), 'lighting': shot.get('lighting', ''),
                          'action': shot.get('action', []), 'dialogue': dialogue,
                          'material_keys': bindings, 'visible_asset_ids': visible, 'inherited_asset_ids': inherited,
                          'observed': list(presence.values()), 'previous_state': row['start_state'],
                          'end_state': row['end_state'], 'transitions': row['transitions'],
                          'keyframe_recommended': bool(reasons), 'keyframe_reasons': reasons})
        for aid, states in variants.items():
            if len(states) > 1:
                issues.append({'asset': aid, 'code': 'multiple_costumes_in_clip', 'blocking': True,
                               'message': 'Split at the wardrobe change or bind state-specific references explicitly.'})
        plans = board.get('raccordPlans', {})
        used_plans = {}
        for shot in shots:
            plan = plans.get(shot['continuity_scope'])
            if plan:
                direction = next((r for r in plan['shots'] if r['shot_id'] == shot['id']), None)
                if direction:
                    shot['raccord'] = {**direction, 'scene_rule': plan['scene_rule'], 'mode': plan['mode']}
                    used_plans[shot['continuity_scope']] = plan['version']
        anchors = {}
        for shot in shots:
            anchors.setdefault(shot['continuity_scope'], {
                'environment_materials': [k for k in shot['material_keys'] if materials[k]['kind'] == 'environment'],
                'policy': 'Use the same approved set geometry and light sources across camera angles. Do not mirror the set or reset crowd identities at a cut.',
            })
        package = {'schema_version': 1, 'project_id': project_id, 'sequence_key': key,
                   'style': board.get('style', 'realistic'), 'aspect_ratio': board.get('aspectRatio', '16:9'),
                   'dialogue_language': 'en', 'policy': POLICY, 'raccord_versions': used_plans, 'scene_anchors': anchors, 'materials': materials, 'shots': shots, 'issues': issues,
                   'ready': bool(shots) and not any(i.get('blocking') for i in issues)}
        package['version'] = pm.digest(package)
        clips.append(package)
    if sequence_key:
        if not clips: raise ValueError('Clip not found')
        return clips[0]
    order = {r['sequence_key']: i for i,r in reversed(list(enumerate(manifest['shots'])))}
    return sorted(clips, key=lambda c: order.get(c['sequence_key'], 10**9))


def check_bindings(package, references):
    """Verify actual prompt input against the material selections compiled on server."""
    errors = [i['code'] + ': ' + str(i.get('asset', i.get('shot', ''))) for i in package['issues'] if i.get('blocking')]
    bound = {}
    for ref in references:
        aid = _id(ref)
        if aid: bound[aid] = ref
    for material in package['materials'].values():
        ref = bound.get(material['asset_id'], {})
        if ref.get('ref_url') != material['reference_url'] or not ref.get('ref_url'):
            errors.append('Reference missing/changed for ' + material['asset_id'])
        if material['media_id'] and ref.get('media_id') != material['media_id']:
            errors.append('Media binding changed for ' + material['asset_id'])
    return list(dict.fromkeys(errors))


def keyframe(package, shot_index, which='start'):
    if not 0 <= shot_index < len(package['shots']): raise ValueError('Shot index out of range')
    if which not in ('start', 'end'): raise ValueError('Frame must be start or end')
    shot = package['shots'][shot_index]
    # A frame may be prepared despite unrelated clips/materials being incomplete.
    selected = [package['materials'][key] for key in shot['material_keys']]
    missing = [m['asset_id'] for m in selected if not m['reference_url']]
    if missing: raise ValueError('Missing material: ' + ', '.join(missing))
    urls = []
    declarations = []
    for m in selected:
        if m['reference_url'] not in urls: urls.append(m['reference_url'])
        declarations.append(f"@image{urls.index(m['reference_url'])+1} — {m['asset_id']} ({m['name']}): " +
                            json.dumps({k: m[k] for k in ('description','wardrobe','geometry_and_scale','lighting','layout_lock')},ensure_ascii=False))
    # Use the approved sheet's own style paragraph where available through description;
    # do not introduce an unrelated aesthetic or generic camera layout.
    from flowboard.services.automation import _style
    prompt = '\n'.join([
        'Create ONE cinematic film frame, not a character sheet, poster or collage.',
        'STYLE: ' + (next((m['style_note'] for m in selected if m.get('style_note')), '') or _style(package['style'])['video_style']),
        f"FORMAT: {package['aspect_ratio']}. FRAME: {which}. SHOT: {shot['id']}.",
        'REFERENCE BINDINGS:', *declarations,
        'RACCORD DIRECTION (production intent, not source evidence): ' + json.dumps(shot.get('raccord', {}), ensure_ascii=False),
        'SET ANCHOR: ' + json.dumps(package['scene_anchors'].get(shot['continuity_scope'], {}), ensure_ascii=False),
        'SHOT DIRECTION: ' + json.dumps({k:shot[k] for k in ('framing','camera','lens_mm','composition','lighting','action')},ensure_ascii=False),
        'Freeze the ' + ('opening setup before the listed action unfolds.' if which=='start' else 'closing state after the listed action; do not add a new beat.'),
        'OBSERVED IN THIS ANGLE: ' + json.dumps(shot['observed'],ensure_ascii=False),
        'CONTINUITY CONTEXT (not additional visible people): ' + json.dumps(shot['previous_state'],ensure_ascii=False),
        ('CLOSING STATE: ' + json.dumps(shot['end_state'],ensure_ascii=False)) if which == 'end' else 'Opening frame must not show the completed outcome of an action that has not happened yet.',
        POLICY, 'Do not draw dialogue text, captions, labels or reference tags. No extra fingers or duplicated objects.',
    ])
    return {'prompt': prompt, 'reference_urls': urls, 'aspect_ratio': package['aspect_ratio'],
            'package_version': package['version'], 'shot_id': shot['id'], 'shot_index': shot_index, 'which': which}
