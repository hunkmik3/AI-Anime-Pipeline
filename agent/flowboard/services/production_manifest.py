"""Project-scoped, deterministic production state. Never invent source evidence."""
from __future__ import annotations
from copy import deepcopy
import hashlib
import json


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), default=str).encode()).hexdigest()


def build(board: dict, project_id: str = '') -> dict:
    assets = {}
    for asset in board.get('productionAssets') or []:
        key = asset.get('id') or asset.get('key')
        if key: assets[key] = {'definition': deepcopy(asset), 'references': {}}
    for node in board.get('nodes') or []:
        d = node.get('data') or {}; kind = d.get('kind')
        if kind not in ('character', 'environment', 'asset'): continue
        item = d.get(kind) or {}; key = item.get('source_asset_id') or item.get('id') or item.get('key')
        if not key: continue
        record = assets.setdefault(key, {'definition': {}, 'references': {}})
        record['design'] = deepcopy(item)
        plates = {'identity': d.get('identity'), 'plate': d.get('plate'), **(d.get('states') or {})}
        record['references'] = {k: {f:v[f] for f in ('referenceUrl', 'mediaId', 'prompt', 'referenceScope') if v.get(f)}
                                for k,v in plates.items() if isinstance(v,dict)}
    for record in assets.values(): record['version'] = digest(record)
    sequences = [n['data'] for n in board.get('nodes') or [] if n.get('data',{}).get('kind') == 'sequence']
    # Source order, never canvas coordinates. Authored order is its saved list.
    def order(d):
        nums = [s.get('source_start', s.get('source_shot', 10**9)) for s in d.get('shots') or []]
        return min(nums, default=10**9)
    sequences.sort(key=order)
    timeline=[]; scenes={}; seen=set(); issues=[]; previous_scene=None; block=0
    for seq in sequences:
        sequence = seq.get('sequence') or {}; seq_key=sequence.get('key','')
        for shot in seq.get('shots') or []:
            source = shot.get('source_shots') or ([shot['source_shot']] if shot.get('source_shot') is not None else [])
            sid = shot.get('shot_uid') or (f'{project_id}:source:{source[0]}' if len(source)==1 else f'{project_id}:{seq_key}:{shot.get("id",shot.get("n"))}')
            if sid in seen: issues.append({'shot':sid,'code':'duplicate_shot_id','blocking':True})
            seen.add(sid)
            if board.get('preserveSourceShots', True) and len(source)>1:
                issues.append({'shot':sid,'code':'merged_source_shots','blocking':True,'message':'Re-import with preserve source shots to restore the original cuts.'})
            scene = shot.get('scene_id')
            if not scene:
                # No identity-of-place heuristic: an unknown scene gets an isolated scope.
                scene = 'unassigned:'+sid
                issues.append({'shot':sid,'code':'missing_scene','blocking':False})
            # Re-entering a place after another scene does not imply continuous time.
            if scene != previous_scene or shot.get('continuity_break'): block += 1
            continuity_key=f'{scene}:block:{block}'
            previous_scene=scene
            state=scenes.setdefault(continuity_key,{})
            before=deepcopy(state); observed=set(); changes=[]
            events=shot.get('continuity_events') or []
            for event in events:
                if event.get('type')=='exit' and event.get('asset_id'):
                    state.pop(event['asset_id'],None)
            for presence in shot.get('asset_presence') or []:
                key=presence.get('asset_id'); visibility=presence.get('visibility')
                if not key: continue
                observed.add(key)
                if key not in assets: issues.append({'shot':sid,'asset':key,'code':'unknown_asset','blocking':True})
                if visibility=='uncertain':
                    issues.append({'shot':sid,'asset':key,'code':'uncertain_presence','blocking':False});continue
                # Offscreen is not exit, nor evidence that a new actor has entered.
                if visibility=='offscreen' and key not in state: continue
                old=state.get(key,{})
                # An observed object has a fresh relation snapshot. Omitted
                # holder/hand/contents are unknown now, not proof that a holder
                # from an earlier shot still carries it. Offscreen records keep
                # their last-known relations until another observation arrives.
                if visibility in ('visible','partial'):
                    old={k:v for k,v in old.items()
                         if k not in ('holder_id','hand','contains_ids') or k in presence}
                for field in ('holder_id','hand','state','wardrobe','contains_ids'):
                    if field in old and field in presence and old[field]!=presence[field]:
                        explained=any(e.get('asset_id')==key and e.get('field')==field and e.get('to')==presence[field] and e.get('reason') for e in events)
                        changes.append({'asset_id':key,'field':field,'from':old[field],'to':presence[field],'explained':explained})
                        if not explained:issues.append({'shot':sid,'asset':key,'field':field,'code':'state_transition_needs_review','blocking':False})
                state[key]={**old,**deepcopy(presence),'last_observed_shot':sid}
            after=deepcopy(state)
            for key in set(after)-observed:
                # Last-known, not a new claim of visual evidence.
                after[key]['visibility']='not_observed';after[key]['inherited']=True
            used=set(shot.get('scene_present_asset_ids') or []) | observed | set(before) | set(after)
            pending=list(used)
            while pending:
                definition=assets.get(pending.pop(),{}).get('definition',{})
                for key in (definition.get('depends_on_asset_ids') or []) + (definition.get('member_ids') or []):
                    if key not in used:used.add(key);pending.append(key)
            record={'id':sid,'sequence_key':seq_key,'scene_id':scene,'continuity_scope':continuity_key,
                    'source_shots':source,'source_start':shot.get('source_start'),'source_end':shot.get('source_end'),
                    'duration_s':shot.get('duration_s'),'shot_version':digest(shot),'definition':deepcopy(shot),
                    'assets':{k:assets[k]['version'] for k in sorted(used) if k in assets},
                    'start_state':before,'observed':deepcopy(shot.get('asset_presence') or []),
                    'end_state':after,'transitions':changes}
            timeline.append(record)
    result={'schema_version':1,'project_id':project_id,'assets':assets,'shots':timeline,'issues':issues}
    result['version']=digest(result)
    return result


def context(manifest: dict, sequence_key: str) -> dict:
    shots=[{k:v for k,v in s.items() if k!='definition'} for s in manifest['shots'] if s['sequence_key']==sequence_key]
    ids={k for s in shots for k in s['assets']}
    result={'shots':shots,'assets':{k:manifest['assets'][k] for k in sorted(ids)},
            'issues':[x for x in manifest['issues'] if x['shot'] in {s['id'] for s in shots}],
            'policy':'Preserve original cuts. Inherited state is last-known, not new source evidence. Offscreen is not exit. Do not reuse prior screen-left/right positions as world coordinates across reverse angles. Keep dialogue language from the locked script.'}
    result['version']=digest(result)
    return result
