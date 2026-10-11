"""Bounded draft repairs and isolation of unresolved source-catalog intervals.

These operations never approve source fidelity or invent dialogue. A failed
interval is retained with its dependencies; only later independent sequences
may continue, and a partial catalog cannot enter production.
"""
from copy import deepcopy
import re


class CatalogNeedsReview(ValueError):
    def __init__(self, report):
        self.report = report
        count = len(report['pending_shots'])
        first = next((r.get('error') for r in report['pending'] if r.get('error')), '')
        super().__init__(f"Đã lập hồ sơ {report['completed']}/{report['total']} shot; "
                         f"{count} shot cần đối soát sau khi tự xử lý. {first}"[:1600])


class BatchNeedsReview(ValueError):
    def __init__(self, issues):
        self.issues = issues
        super().__init__('One-pass catalog needs attention: ' + str(issues)[:1600])


class SplitCatalogBatch(Exception):
    pass


class CatalogCheckpointUnknown(RuntimeError):
    pass


class CatalogMalformedResponse(ValueError):
    def __init__(self,raw):
        self.raw=raw
        super().__init__('Catalog returned malformed JSON; raw response retained for bounded correction.')


def speaker_links(data, batch, known, evidence):
    """Repair a contradictory *draft* ID only with local mouth + continuity evidence.

    Presence alone, a subtitle, or voice clustering cannot establish a speaker.
    Exact audio remains independently reviewable downstream. A continued line
    is changed as a unit, only if every affected piece agrees on one candidate.
    """
    out = deepcopy(data)
    if not isinstance(out, dict) or not isinstance(out.get('shots'), dict):
        return data, []
    assets = {a['id']:a for a in [*known.get('assets', []), *out.get('assets', [])]
              if isinstance(a, dict) and a.get('id')}
    groups = {}
    for source in batch:
        n = source['shot']; row = out['shots'].get(str(n)) or {}
        local = {e['id'] for e in evidence if e['shot'] == n}
        pres = [p for p in row.get('asset_presence') or [] if isinstance(p, dict)
                and assets.get(p.get('asset_id'), {}).get('kind') == 'character'
                and p.get('visibility') == 'visible']
        src = source.get('source') or {}
        action = ' '.join(str(src.get(k) or '') for k in ('action','start_pose','end_pose'))
        mouth_action = bool(re.search(r'\b(mouth|lips?)\b.{0,35}\b(mov\w*|speak\w*|open\w*)', action, re.I))
        mediated = bool(re.search(r'\b(screen|television|recording|voice.over|offscreen|silent)\b', action, re.I))
        pieces = source.get('dialogue_pieces') or []
        speakers = row.get('speakers') or []
        if len(pieces) != len(speakers):
            continue
        for i, (piece, speaker) in enumerate(zip(pieces, speakers)):
            if not isinstance(speaker, dict) or not piece.get('line_id'):
                continue
            refs = set(speaker.get('evidence_ids') or [])
            candidate = pres[0] if len(pres) == 1 else None
            supported = (candidate and speaker.get('delivery') == 'on_camera'
                         and speaker.get('mouth_visible') is True and refs and refs <= local
                         and refs <= set(candidate.get('evidence_ids') or []) and not mediated)
            groups.setdefault(piece['line_id'], []).append({
                'shot':n,'index':i,'speaker':speaker,'candidate':candidate if supported else None,
                'anchor':bool(supported and mouth_action),'refs':sorted(refs)})
    receipts = []
    for lid, pieces in groups.items():
        if not all(p['candidate'] for p in pieces) or not any(p['anchor'] for p in pieces):
            continue
        ids = {p['candidate']['asset_id'] for p in pieces}
        if len(ids) != 1:
            continue
        aid = ids.pop()
        if any(any(q.get('asset_id') == p['speaker'].get('asset_id')
                   and p['speaker'].get('asset_id') != aid
                   and q.get('visibility') in {'visible','partial','occluded'}
                   for q in out['shots'][str(p['shot'])].get('asset_presence') or []) for p in pieces):
            continue
        for p in pieces:
            speaker = p['speaker']
            # This is a reference-binding repair, not permission to choose between
            # two visible speakers or change a voice-over into on-camera speech.
            if speaker.get('asset_id') == aid:
                continue
            before = deepcopy(speaker)
            speaker.update(asset_id=aid, label=assets[aid].get('name') or aid)
            receipts.append({'shot':p['shot'],'line_id':lid,'speaker_index':p['index'],
                'before':before,'after':deepcopy(speaker),'evidence_ids':p['refs'],
                'basis':'Local speaking-mouth observation and consistent visible character across this line; not a voice-cluster vote.',
                'source_fidelity_approved':False})
    return out, receipts


def next_independent_offset(shots, sequences, failed_end):
    """Skip dependent shots to a later declared sequence boundary, never a guess."""
    starts = {s.get('first_shot') for s in sequences if isinstance(s, dict)
              and isinstance(s.get('first_shot'), int) and s['first_shot'] > failed_end}
    for index, shot in enumerate(shots):
        if shot['shot'] in starts:
            # Do not cut a continuing utterance at a story boundary.
            if not any(p.get('cont') for p in shot.get('dialogue_pieces') or []):
                return index
    return len(shots)


def retain_drafts(inventory, patch):
    """Merge a resumed prefix without deleting later, already accepted states."""
    from . import source_inventory
    patch=deepcopy(patch)
    prior={a['id']:a for a in inventory.get('assets',[])}
    for asset in patch.get('assets',[]):
        states={s['key']:s for s in prior.get(asset['id'],{}).get('states',[])}
        states.update({s['key']:s for s in asset.get('states',[])})
        if states: asset['states']=list(states.values())
    source_inventory._merge(inventory,patch)


def reusable_patch(inventory,batch):
    """Revalidate a completed draft against the current context without repurchasing it.

    Caller binds this cache to unchanged source inputs. This is never a source
    approval, and normal validation can reject an incompatible upstream edit.
    """
    ns={s['shot'] for s in batch}
    rows=inventory.get('shots',{})
    if not all(str(n) in rows for n in ns):return None
    selected={str(n):deepcopy(rows[str(n)]) for n in sorted(ns)}
    aids={p.get('asset_id') for row in selected.values() for p in [*row.get('asset_presence',[]),*row.get('speakers',[])]}
    aids.update(row.get('environment_key') for row in selected.values())
    aids.update(aid for row in selected.values() for p in row.get('asset_presence',[])
                for aid in [p.get('holder_id'),*p.get('contains_ids',[])])
    assets={a['id']:a for a in inventory.get('assets',[])}
    while True:
        more={aid for key in aids if key in assets for aid in [*assets[key].get('depends_on_asset_ids',[]),*assets[key].get('member_ids',[])]}
        if more<=aids:break
        aids.update(more)
    scenes={row.get('scene_id') for row in selected.values()}
    selected_assets=[]
    for aid,a in assets.items():
        if aid not in aids: continue
        asset=deepcopy(a)
        states={row.get('character_states',{}).get(aid) for row in selected.values()}
        if asset.get('states') and states-{None}: asset['states']=[s for s in asset['states'] if s['key'] in states]
        selected_assets.append(asset)
    return {'assets':selected_assets,
        'scenes':[{**deepcopy(s),'shot_ids':[n for n in s.get('shot_ids',[]) if n in ns],
                   'present_asset_ids':[a for a in s.get('present_asset_ids',[]) if a in aids]}
                  for s in inventory.get('scenes',[]) if s['id'] in scenes],
        'shots':selected,'findings':[]}
