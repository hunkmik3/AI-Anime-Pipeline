"""Evidence-backed source revisions, kept separate from target adaptation.

No finding is cleared just because it looks harmless. Each disposition cites a
bounded original audiovisual review; unchanged evidence and original findings
remain in the revision history. Uncertain or malformed answers cannot approve
generation, and changing source bytes invalidates the entire receipt.
"""
from copy import deepcopy
import math
import re

from flowboard.services import production_manifest as pm, reskin, reskin_source_review as review
from flowboard.services.video_analyzer.one_pass_film import LOCK_FIELDS, digest as source_digest
from flowboard.services.video_analyzer.source_integrity import comparison_tokens

FIELDS = {'framing','framing_note','camera','action','staging','background','character_states'}


def shots(board):
    return [(n['data'],s,f"{n['data']['sequence']['key']}:{i+1}")
            for n in board['nodes'] if n.get('data',{}).get('kind')=='sequence'
            for i,s in enumerate(n['data']['shots'])]


def evidenced(row, start, end):
    t=row.get('at_s')
    return (type(t) in (int,float) and math.isfinite(t) and start-.15<=t<=end+.15
            and isinstance(row.get('basis'),str) and len(row['basis'].strip())>=20)


def _rebind(shot, binding, assets):
    old,new=binding['from'],binding['to'];state=binding.get('state')
    if old not in assets or new not in assets or assets[new].get('kind')!='character':
        raise ValueError('Source correction refers to an unknown character.')
    if state not in {s['key'] for s in assets[new].get('states',[])}:
        raise ValueError('Source correction refers to an unestablished costume state.')
    # The original source ID can exist in descriptive text and holder metadata,
    # but never replace arbitrary substrings in IDs or unrelated words.
    old_name=assets[old].get('name') or old;new_name=assets[new].get('name') or new
    names={old_name:new_name}
    if old_name.split(' ')[0] in {'Vivian','Claudia','Nathan'}:
        names[old_name.split(' ')[0]]=new_name.split(' ')[0]
    def walk(value):
        if isinstance(value,list):return [walk(v) for v in value]
        if isinstance(value,dict):return {(new if k==old else k):walk(v) for k,v in value.items()}
        if isinstance(value,str):
            if value==old:return new
            for a,b in sorted(names.items(),key=lambda x:-len(x[0])):
                value=re.sub(r'(?<!\w)'+re.escape(a)+r'(?!\w)',lambda _:b,value)
        return value
    revised=walk(shot)
    # Two mistaken aliases of the same person must not generate two actors.
    for key in ('character_keys','asset_keys','scene_asset_ids','scene_present_asset_ids'):
        if key in revised:revised[key]=list(dict.fromkeys(revised[key]))
    if new in revised.get('character_states',{}):
        revised['character_states'][new]=state
    for app in revised.get('source_appearances',[]):
        if new in app.get('character_states',{}):
            app['character_states'][new]=state
        unique={}
        for p in app.get('asset_presence',[]):
            prior=unique.get(p['asset_id'])
            rank={'visible':4,'partial':3,'occluded':2,'offscreen':1}
            if prior is None or rank.get(p.get('visibility'),0)>rank.get(prior.get('visibility'),0):
                unique[p['asset_id']]=p
        app['asset_presence']=list(unique.values())
    return revised


def apply_group(board, record, *, path, expected_findings):
    """Apply explicit corrections, never synthesize audiovisual observations.

    Returns a candidate board and per-clip receipts. Persistence and production
    approval are separate; callers must aggregate every selected clip first.
    """
    if record.get('state')!='completed' or record.get('source_sha256')!=board.get('sourceMediaSha256'):
        raise ValueError('Reconciliation has no completed receipt for these source bytes.')
    answer=deepcopy(record.get('answer') or {})
    if not isinstance(answer.get('limitations'),list) or not isinstance(answer.get('findings'),list):
        raise ValueError('Reconciliation omitted findings or limitations.')
    entries=answer.get('shots')
    if not isinstance(entries,list) or not entries:raise ValueError('Reconciliation omitted source observations.')
    out=deepcopy(board);mapped={sid:(seq,s) for seq,s,sid in shots(out)}
    ids=[r.get('id') for r in entries]
    if len(set(ids))!=len(ids) or any(sid not in mapped for sid in ids):
        raise ValueError('Reconciliation duplicated or invented source shots.')
    order=[sid for _,_,sid in shots(out) if sid in ids]
    if ids!=order:raise ValueError('Reconciliation changed source order.')
    selected_input=[{'id':sid,'shot':s} for _,s,sid in shots(board) if sid in ids]
    if record.get('input_shot_digest')!=pm.digest(selected_input):
        raise ValueError('Selected source shots changed after the correction was requested.')
    selected_clips={sid.split(':')[0] for sid in ids}
    if set(ids)!={sid for _,_,sid in shots(out) if sid.split(':')[0] in selected_clips}:
        raise ValueError('Reconciliation must cover every shot of an affected clip.')
    start=min(mapped[sid][1]['source_start'] for sid in ids)
    end=max(mapped[sid][1]['source_end'] for sid in ids)
    resolutions=answer.get('resolutions')
    if not isinstance(resolutions,list) or [r.get('finding_id') for r in resolutions]!=[f['finding_id'] for f in expected_findings]:
        raise ValueError('Reconciliation did not account for every original finding.')
    for f,r in zip(expected_findings,resolutions):
        # A positive summary has no physical event timestamp. Its explicit
        # false-positive disposition can cite the already timed observations of
        # the entire clip. This is a schema link, not an invented timestamp.
        scoped=[x for x in entries if x['id'].split(':')[0]==f.get('clip')]
        summary_evidence=(r.get('status')=='false_positive' and r.get('at_s') is None
            and len(r.get('basis',''))>=20 and bool(scoped)
            and all(set(x.get('criteria',{}))==set(review.CRITERIA)
                    and all(c.get('status')=='pass' for c in x['criteria'].values()) for x in scoped))
        if r.get('status') not in {'corrected','false_positive','unresolved'} or not (evidenced(r,start,end) or summary_evidence):
            raise ValueError('Finding disposition has no timed source evidence.')
    assets={a['id']:a for a in out['productionAssets']};changes=[];receipts=[]
    for observed in entries:
        seq,s=mapped[observed['id']];index=seq['shots'].index(s)
        before=deepcopy(s);a,b=s['source_start'],s['source_end']
        if observed.get('source_shot')!=s['source_shot']:
            raise ValueError('Reconciliation changed the source shot identity.')
        # Source-cut corrections require a measured neighboring boundary update,
        # not approximate times returned by an audiovisual language model.
        if any(type(observed.get(k)) not in (int,float) or abs(observed[k]-v)>.15
               for k,v in (('start',a),('end',b))):
            raise ValueError('Source cut needs a measured boundary correction.')
        fields=observed.get('fields') or {}
        if set(fields)-FIELDS:raise ValueError('Unsupported source correction field.')
        for key,value in fields.items():
            if key=='character_states':
                if not isinstance(value,dict) or any(state not in {s['key'] for s in assets.get(aid,{}).get('states',[])} for aid,state in value.items()):
                    raise ValueError('Unknown source wardrobe state.')
                continue
            if (key=='action' and (not isinstance(value,list) or not all(isinstance(v,str) for v in value))) or (key!='action' and not isinstance(value,str)):
                raise ValueError('Malformed source field correction.')
        for binding in (observed.get('identity_rebindings') or []):
            if not evidenced(binding,a,b):raise ValueError('Identity correction lacks audiovisual evidence.')
            s=_rebind(s,binding,assets)
        for removal in (observed.get('remove_presence') or []):
            if not evidenced(removal,a,b):raise ValueError('Presence correction lacks audiovisual evidence.')
            aid=removal['asset_id']
            for app in s.get('source_appearances',[]):
                app['asset_presence']=[p for p in app.get('asset_presence',[]) if p['asset_id']!=aid]
                app.get('character_states',{}).pop(aid,None)
            s.get('character_states',{}).pop(aid,None)
            for k in ('character_keys','asset_keys','scene_asset_ids','scene_present_asset_ids'):
                s[k]=[x for x in s.get(k,[]) if x!=aid]
        for addition in (observed.get('presence_additions') or []):
            p=addition.get('presence') or {}
            if not evidenced(addition,a,b) or p.get('asset_id') not in assets:
                raise ValueError('Additional presence has no known asset or source evidence.')
            if p.get('visibility') not in {'visible','partial','offscreen','occluded'}:
                raise ValueError('Additional presence has unclear visibility.')
            for app in s.get('source_appearances',[]):app['asset_presence'].append(deepcopy(p))
            s['scene_present_asset_ids']=list(dict.fromkeys([*s.get('scene_present_asset_ids',[]),p['asset_id']]))
        fields=deepcopy(fields)
        states=fields.pop('character_states',{})
        s.setdefault('character_states',{}).update(states)
        for app in s.get('source_appearances',[]):app.setdefault('character_states',{}).update(states)
        s.update(fields)
        dialogue=observed.get('dialogue')
        if not isinstance(dialogue,list):raise ValueError('Reconciliation omitted independent listening.')
        # AV models are useful listeners, not precise word aligners. When the
        # words and their shot partition are unchanged, retain the established
        # source audio alignment instead of replacing it with estimated times.
        original_lines=before.get('dialogue',[])
        timing=before.get('performance',[])
        origin=seq['shots'][0]['source_start']
        if (len(dialogue)==len(original_lines)==len(timing)
                and all(comparison_tokens(d['text'])==comparison_tokens(old.get('line',''))
                        for d,old in zip(dialogue,original_lines))):
            aligned=[]
            for text in timing:
                match=re.match(r'Clip audio (-?[\d.]+)–(-?[\d.]+)s:',text)
                if not match:break
                aligned.append(tuple(float(v)+origin for v in match.groups()))
            if len(aligned)==len(dialogue) and all(a-.15<=x<y<=b+.15 for x,y in aligned):
                dialogue=deepcopy(dialogue)
                for d,(x,y) in zip(dialogue,aligned):
                    d['timing_from']='retained_source_word_alignment'
                    d['reviewer_estimated_timing']=[d['start'],d['end']]
                    d.update(start=x,end=y)
        for d in dialogue:
            if (assets.get(d.get('speaker_id'),{}).get('kind')!='character' or not isinstance(d.get('text'),str)
                    or not d['text'].strip() or d.get('delivery') not in {'on_camera','offscreen','voice_over'}
                    or not evidenced(d,a,b) or any(type(d.get(k)) not in (int,float) for k in ('start','end'))
                    or not a-.15<=d['start']<d['end']<=b+.15):
                raise ValueError(f"{observed['id']}: unclear words, speaker, delivery or timing.")
        s['dialogue']=[{'who':assets[d['speaker_id']].get('name') or d['speaker'],
                       'speaker_id':d['speaker_id'],'line':d['text'],'delivery':d['delivery'],
                       **({'cont':True} if d.get('continues') else {})} for d in dialogue]
        s['performance']=[f"Clip audio {max(a,d['start'])-origin:.3f}–{min(b,d['end'])-origin:.3f}s: "+
                          ('continue the preceding line without repeating any word.' if d.get('continues') else 'speak this line once.')
                          for d in dialogue]
        s['source_dialogue_review']={'path':path,'source_sha256':record['source_sha256'],
                                    'observed':deepcopy(dialogue),'timing_precision':'approximate_utterance_windows'}
        appearances=s.get('source_appearances') or []
        if len(appearances)!=1:raise ValueError('Source correction requires one original cut per board shot.')
        appearances[0]['speakers']=[{'asset_id':d['speaker_id'],'label':assets[d['speaker_id']].get('name') or d['speaker'],
                                   'delivery':d['delivery'],'av_evidence':{'path':path,'at_s':d['at_s'],'basis':d['basis']}}
                                  for d in dialogue]
        s['asset_presence']=[{**p,'source_shot':s['source_shot']} for p in appearances[0]['asset_presence']]
        seq['shots'][index]=s
        # Locks change only here, as a recorded SOURCE revision with old values.
        verification=out['sourceVerification'];n=str(s['source_shot'])
        verification['locked_shots'][n]={k:deepcopy(s[k]) for k in (*LOCK_FIELDS,'duration_s')}
        verification['shot_digests'][n]=source_digest({k:v for k,v in appearances[0].items() if k!='source_shot'})
        changes.append({'source_shot':s['source_shot'],'before':before,'after':deepcopy(s),
                        'observation':deepcopy(observed),'receipt':path})
        # Effective heard observations use retained acoustic alignment as well;
        # the immutable provider record still contains its estimated timestamps.
        observed['dialogue']=dialogue
    for clip in sorted(selected_clips):
        clip_entries=[r for r in entries if r['id'].split(':')[0]==clip]
        clip_rows=[{**s,'id':sid} for seq,s,sid in shots(out) if sid.split(':')[0]==clip]
        offset=max(0,clip_rows[0]['source_start']-1)
        obs={'shots':[],'heard_dialogue':[],'findings':[],'limitations':deepcopy(answer['limitations'])}
        for entry in clip_entries:
            criteria=deepcopy(entry['criteria'])
            for c in criteria.values():c['at_s']-=offset
            obs['shots'].append({'id':entry['id'],'start':entry['start']-offset,'end':entry['end']-offset,'criteria':criteria})
            obs['heard_dialogue'] += [{**deepcopy(d),'shot_id':entry['id'],'start':d['start']-offset,'end':d['end']-offset}
                                     for d in entry['dialogue']]
        # Retain all real remaining findings, including ones without a shot ID.
        for f in answer['findings']:
            if not f.get('shot') or str(f['shot']) in {r['id'] for r in clip_entries} or f.get('source_shot') in {r['source_shot'] for r in clip_entries}:
                obs['findings'].append(deepcopy(f))
        for f,r in zip(expected_findings,resolutions):
            if r['status']=='unresolved' and f.get('clip')==clip:
                obs['findings'].append({'criterion':f.get('criterion'),'message':r['basis'],'original_finding':f})
        found=review.validate(obs,clip_rows,offset)
        receipts.append({'clip':clip,'path':path,'input_digest':pm.digest(clip_rows),'source_sha256':record['source_sha256'],
                         'observations':obs,'findings':found,'reconciliation':True})
    for n in out['nodes']:
        d=n.get('data',{})
        if d.get('kind')=='sequence' and d['sequence']['key'] in selected_clips:
            d['sequence']['character_keys']=list(dict.fromkeys(k for s in d['shots'] for k in s.get('character_keys',[])))
    revision={'parent_digest':board['sourceVerification']['digest'],'source_sha256':record['source_sha256'],
              'receipt':path,'receipt_digest':pm.digest(record),'changes_digest':pm.digest(changes),
              'resolutions':deepcopy(resolutions),'original_findings':deepcopy(expected_findings)}
    out['sourceVerification']['digest']=pm.digest(revision)
    out['sourceVerification'].setdefault('source_revisions',[]).append(revision)
    return out,receipts,changes


def aggregate(board, receipts, *, previous, history):
    """Full selection must still validate; no empty-list or sampled approval."""
    out=deepcopy(board);by_clip={r['clip']:r for r in receipts}
    findings=[];all_receipts=[]
    for n in out['nodes']:
        seq=n.get('data',{})
        if seq.get('kind')!='sequence':continue
        key=seq['sequence']['key'];r=deepcopy(by_clip.get(key))
        if not r:raise ValueError('A selected clip has no source review receipt.')
        rows=[{**s,'id':f'{key}:{i+1}'} for i,s in enumerate(seq['shots'])]
        if r.get('source_sha256')!=out.get('sourceMediaSha256'):raise ValueError('Source bytes changed after reconciliation.')
        found=review.validate(r['observations'],rows,max(0,rows[0]['source_start']-1))
        findings.extend({'clip':key,**f} for f in found)
        r['input_digest']=pm.digest(rows);all_receipts.append(r)
    out['reskinSourceReview']={'status':'needs_review' if findings else 'reviewed',
        'input_digest':pm.digest(reskin.source_contract(out)),'receipts':all_receipts,'findings':findings,'errors':[],
        'counts':{'received':len(all_receipts),'validated':len(all_receipts),'total':len(all_receipts)},
        'method':'bounded_original_av_reconciliation','previous_report':deepcopy(previous),'history':history,
        'limitations':['Source audio/visual review is evidence-based but not a 100% fidelity certification.',
                       'Dialogue times are utterance windows, not phoneme-level forced alignment.']}
    return out
