"""Persistent board-to-film orchesator. Controller ticks never occupy provider slots.

Runs and their task references live in AutomationJob; all paid operations continue
through the existing leased worker. A stopped/unknown task is never auto-resubmitted.
"""
from copy import deepcopy
import json
import os
import uuid
from sqlmodel import select

from flowboard.db import get_session
from flowboard.db.models import AutomationJob, AutomationProject, VideoAnalysis
from flowboard.services import reference_atlas, material_dependencies, automation, automation_jobs as jobs, production_manifest as pm, shot_package, raccord, prompt_coverage, set_restyle, reskin

KIND = 'production_run'
RUNNING = {'running'}
SCHEMA = 1

def style_version(board):
    from flowboard.services import film_styles
    return film_styles.version(board['style']) if film_styles.is_preset(board.get('style')) else ''


def authored_prompt(plate):
    prompt=plate.get('prompt','')
    return plate.get('baseMaterialPrompt','') if prompt and prompt==plate.get('runtimePrompt') else prompt


def input_version(board):
    """User-authored production inputs only; generated results and canvas layout excluded."""
    from flowboard.services import film_motion
    nodes = []
    for n in board.get('nodes', []):
        d = n.get('data', {}); kind = d.get('kind')
        if kind == 'sequence':
            seq = {k:v for k,v in d.get('sequence', {}).items() if k not in ('shot_package','production_context')}
            nodes.append({'id':n['id'],'sequence':seq,'shots':d.get('shots',[]),'function':d.get('functionOf'), 'raccord':d.get('raccord')})
        elif kind in ('character','environment','asset'):
            # A set with two views has a reverse slot (older boards keep their version).
            slots={'identity':d.get('identity',{}),'plate':d.get('plate',{}),**({'reverse':d.get('reverse',{})} if shot_package.has_views(d) else {}),**d.get('states',{})}
            uploads={k:v.get('referenceUrl') for k,v in slots.items() if v.get('uploaded')}
            nodes.append({'id':n['id'],'definition':d.get(kind),'activeState':d.get('activeState'),
                          **({'uploads':uploads} if uploads else {}),
                          'prompts':{k:authored_prompt(v) for k,v in slots.items()}})
        elif kind=='video' and d.get('promptBy')=='manual':
            nodes.append({'id':n['id'],'manual_prompt':d.get('prompt',''),'end_state':d.get('endState') or '',
                          'staging_decisions':d.get('stagingDecisions') or []})
    return pm.digest({'schema':SCHEMA,'style_version':style_version(board),'video_standard_version':film_motion.version(board.get('style')),'nodes':nodes, **{k:board.get(k) for k in
        ('productionAssets','sourceVerification','preserveSourceShots','style','aspectRatio','imageModel','imageSize','kyc','unmoderated','dialogueLanguage','targetCasting','motionReference','motionProfile','reskinMaxSeconds','sourceVideoId','sourceMediaSha256','reskinVersion')}})


def selected_packages(board, pid, keys):
    packs = shot_package.build(board, str(pid))
    if keys:
        missing = set(keys) - {p['sequence_key'] for p in packs}
        if missing: raise ValueError('Unknown clips: '+', '.join(sorted(missing)))
        packs = [p for p in packs if p['sequence_key'] in keys]
    if not packs: raise ValueError('No saved shotlists. Import the analysed source board first.')
    return packs


def asset_nodes(board):
    return {shot_package._id(n['data'][n['data']['kind']]):n for n in board.get('nodes', [])
            if n.get('data',{}).get('kind') in ('character','environment','asset')}


def material_specs(board, packs):
    nodes = asset_nodes(board)
    # The verified source catalog stays immutable. Generation order is a
    # target-production decision stored on the corresponding material node.
    assets = {a['id']:deepcopy(a) for a in board.get('productionAssets',[]) if a.get('id')}
    for aid, node in nodes.items():
        data = node['data']; definition = data[data['kind']]
        if aid in assets and 'generation_depends_on_asset_ids' in definition:
            assets[aid]['generation_depends_on_asset_ids'] = list(definition['generation_depends_on_asset_ids'])
    # A prop built into a set is drawn in that set's master view; it needs no sheet of its own.
    required = {(m['fixture_of'],None) if m.get('fixture_of') else (m['asset_id'],m.get('state_key'))
                for p in packs for m in p['materials'].values()}
    available={}
    for aid,n in nodes.items():
        d=n['data'];plate=(d.get('states',{}).get(d.get('activeState'),{}) or d.get('identity',{})) if d['kind']=='character' else d.get('plate',{})
        available[aid]=bool(plate.get('referenceUrl'))
    reachable={aid for aid,_ in required};pending=list(reachable)
    while pending:
        a=assets.get(pending.pop(),{})
        for dep in a.get('generation_depends_on_asset_ids',a.get('depends_on_asset_ids',[]))+a.get('member_ids',[]):
            if dep not in reachable:reachable.add(dep);pending.append(dep)
    dependency_graph,related,notes=material_dependencies.graph({k:v for k,v in assets.items() if k in reachable},available)
    specs = {}
    def add(aid, state=None, trail=()):
        node=nodes.get(aid)
        if not node: raise ValueError('Missing material node: '+aid)
        d=node['data']; kind=d['kind']; item=d[kind]
        slot=(state or 'identity') if kind=='character' else 'reverse' if kind=='environment' and state=='reverse' else 'plate'
        key=node['id']+':'+slot
        if key in trail: raise ValueError('Cyclic material dependency: '+' → '.join((*trail,key)))
        if key in specs: return key
        plate=d.get('states',{}).get(slot,{}) if kind=='character' and slot!='identity' else d.get(slot,{})
        # A single wardrobe identity sheet already satisfies this state.
        if kind=='character' and slot!='identity' and not plate.get('referenceUrl') and len(item.get('states',[]))<=1 and (d.get('identity',{}).get('referenceUrl') or (board.get('sourceVerification') or {}).get('method')=='one_pass_production'):
            return add(aid,None,trail)
        deps=[]
        if kind=='character' and slot!='identity': deps.append(add(aid,None,(*trail,key)))
        # The reverse view is drawn from the approved master view of the same set.
        if slot=='reverse': deps.append(add(aid,None,(*trail,key)))
        definition=assets.get(aid,{})
        for dep in ([] if slot=='reverse' else sorted(dependency_graph.get(aid,set()))):
            other=nodes.get(dep,{}).get('data',{})
            deps.append(add(dep,other.get('activeState') if other.get('kind')=='character' else None,(*trail,key)))
        specs[key]={'key':key,'node_id':node['id'],'asset_id':aid,'slot':slot,'kind':kind,'item':item,'plate':plate,'deps':deps,'related_ids':related.get(aid,[]),'dependency_notes':notes,
                    # The owner can draw one material with another image model; the rest keep the board's.
                    **({'image_model':d['imageModel']} if d.get('imageModel') else {})}
        return key
    for aid,state in sorted(required,key=lambda v:(v[0],str(v[1]))): add(aid,state)
    return specs


def clip_order(specs, packs, needs):
    """Materials ordered by the first clip that needs them, every dependency before its
    dependants. Each tick rebuilds the ready set in this order, so a dependant placed
    before its dependency would wait forever."""
    position = {k: i for i, k in enumerate(specs)}
    first = {}
    for i, p in enumerate(packs):
        for k in needs.get(p['sequence_key'], ()):
            first.setdefault(k, i)
    out, seen = [], set()
    def visit(k):
        if k in seen or k not in specs:
            return
        seen.add(k)
        for d in sorted(specs[k]['deps'], key=lambda d: (first.get(d, len(packs)), position.get(d, 0))):
            visit(d)
        out.append(k)
    for k in sorted(specs, key=lambda k: (first.get(k, len(packs)), position[k])):
        visit(k)
    return {k: specs[k] for k in out}


def preview(board, pid, config):
    packs=selected_packages(board,pid,config.get('sequence_keys',[]))
    issues=[{'clip':p['sequence_key'],**i} for p in packs for i in p['issues'] if i.get('blocking') and i['code'] not in ('missing_material','missing_costume_sheet')]
    shared,held,reviewing=reskin.review_state(board,packs)
    issues += [{'code':'reskin_source','message':e,'blocking':True} for e in shared]
    if held and len(held)==len(packs):
        issues += [{'clip':k,'code':'reskin_source','message':e,'blocking':True} for k,v in held.items() for e in v]
    if reskin.enabled(board) and config.get('resolution','480p') not in ('480p','720p'):
        issues.append({'code':'reskin_resolution','message':'Provider hiện được cấu hình cho 480p hoặc 720p.','blocking':True})
    if reskin.enabled(board) and config.get('boundary_frames'):
        issues.append({'code':'reskin_frames','message':'Reskin dùng một ảnh rộng 21:9; tắt ảnh đầu/cuối từng clip.','blocking':True})
    try: specs=material_specs(board,packs)
    except ValueError as exc: specs={};issues.append({'code':'material_graph','message':str(exc),'blocking':True})
    for p in packs:
        total=sum(float(s['duration_s'] or 0) for s in p['shots'])
        if not 0 < total <= automation.VIDEO_MAX_S: issues.append({'clip':p['sequence_key'],'code':'clip_duration','blocking':True})
        if not any(n.get('id')=='vid:'+p['sequence_key'] for n in board.get('nodes',[])):
            issues.append({'clip':p['sequence_key'],'code':'missing_video_node','blocking':True})
        if prompt_coverage.is_strict(board.get('productionAssets'),board.get('sourceVerification')):
            seq=next(n['data'] for n in board['nodes'] if n.get('id')=='seq:'+p['sequence_key'])
            issues += [{'clip':p['sequence_key'],'code':'source_not_ready','message':e,'blocking':True} for e in
                       prompt_coverage.source_readiness_issues(seq['shots'],board.get('sourceVerification'))]
        count=len({m['reference_url'] or m['asset_id'] for m in p['materials'].values()})
        try:reference_atlas.plan(p['materials'],1 if reskin.enabled(board) else 2 if config.get('boundary_frames') else 0)
        except ValueError as exc:issues.append({'clip':p['sequence_key'],'code':'reference_limit','message':str(exc),'blocking':True})
    from flowboard.services.production_progress import make_plan
    return {'progress_plan': make_plan(specs, packs), 'input_version':input_version(board),'clips':[p['sequence_key'] for p in packs],
            'shots':sum(len(p['shots']) for p in packs),'materials':len(specs) if specs else len({k for p in packs for k in p['materials']}),
            'missing_materials':sum(not s['plate'].get('referenceUrl') for s in specs.values()) if specs else len({k for p in packs for k,m in p['materials'].items() if not m['reference_url']}),
            'issues':issues,'ready':not issues,'config':config,
            # Clips held by their own source findings; the others are produced meanwhile.
            **({'source_holds':held} if held and len(held)<len(packs) else {}),
            # Clips waiting for the source review that is running now; their sheets draw meanwhile.
            **({'source_pending':sorted(reviewing)} if reviewing else {}),
            'dependency_notes':next(iter(specs.values()),{}).get('dependency_notes',[]),
            'pipeline':reskin.VERSION if reskin.enabled(board) else 'cinematic',
            'atlases':[{"clip":p['sequence_key'],**reference_atlas.plan(p['materials'],1 if reskin.enabled(board) else 2 if config.get('boundary_frames') else 0)} for p in packs] if not any(i['code']=='reference_limit' for i in issues) else []}


def start(pid, revision, config, request_key):
    with get_session() as s:
        project=s.exec(select(AutomationProject).where(AutomationProject.id==pid).with_for_update()).one()
        if project.revision!=revision: raise ValueError('Board changed; save/reload before starting.')
        if request_key.startswith('source-film:'):
            source=s.get(VideoAnalysis,uuid.UUID(request_key.removeprefix('source-film:')))
            if not source or source.automation_project_id!=pid: raise ValueError('Source handoff belongs to a different project.')
            from flowboard.services.source_generation_scope import configure
            config=configure(project.board or {},source.options.get('auto_production') or {},config)
        previous=s.exec(select(AutomationJob).where(AutomationJob.project_id==pid,AutomationJob.request_key==request_key)).first()
        if previous:
            if previous.kind!=KIND or previous.payload.get('config')!=config: raise ValueError('Request key already used with different settings.')
            return jobs.public(previous)
        from flowboard.services.primary_materials import waiting_run
        pending_review = waiting_run(s, pid)
        if pending_review: raise ValueError('Chốt sheet ở tab Tạo hình chính trước khi bắt đầu lượt khác.')
        active=s.exec(select(AutomationJob).where(AutomationJob.project_id==pid,AutomationJob.kind==KIND,AutomationJob.status=='running')).first()
        if active: raise ValueError('A production run is already active for this project.')
        board=jobs.project_board(s,project);report=preview(board,pid,config)
        if not report['ready']: raise ValueError(json.dumps(report['issues'],ensure_ascii=False))
        if reskin.enabled(board):
            from flowboard.services.reskin_runtime import require_ready
            require_ready(audio=config.get('audio_cleanup',False))
            if reskin.source_fingerprint(reskin.source_path(board['sourceVideoId'])) != board.get('sourceMediaSha256'):
                raise ValueError('File nguồn đã thay đổi; đối chiếu lại nguồn trước khi chạy.')
        job=AutomationJob(project_id=pid,kind=KIND,node_id='production',request_key=request_key,status='paused' if config.get('review_masters') else 'running',
            payload={'config':config,'input_version':report['input_version']},result={'stage':'master_review' if config.get('review_masters') else 'materials','tasks':{},'created_counts':{},'progress_plan':report['progress_plan'],'preview':report})
        s.add(job);s.commit();s.refresh(job);return jobs.public(job)


class Waiting(Exception): pass
class Blocked(Exception): pass
class QualityHold(Exception):
    """A generated clip the automated review did not clear: kept for the reviewer, not
    delivered, and no reason to stop generating the rest of the run."""


def retryable_write(error) -> bool:
    """A writer failure a new run may write once more: our own format/coverage checks failed
    (the model answered), or every writer declined before the refusal chain (Grok) existed.
    Never a source issue for the owner, nor an answer that never came for another reason."""
    text = str(error or '')
    if not text.startswith('422:') or 'Source contract needs review' in text:
        return False
    if 'Provider declined' in text or 'declined this content' in text:
        return True
    return 'no answer' not in text


def _task(s, run, all_jobs, state, key, kind, node, slot, payload, metadata=None):
    """Atomic reservation + durable link under project/run row locks."""
    fingerprint=pm.digest({'kind':kind,'node':node,'slot':slot,'payload':payload})
    request='production:'+fingerprint
    # A take the owner set aside stays in the history but is never reused for a new take.
    usable=lambda j:not (j.prepared or {}).get('set_aside')
    existing=next((j for j in reversed(all_jobs) if j.kind==kind and j.node_id==node and j.slot==slot and j.status=='succeeded' and usable(j) and pm.digest(j.payload)==pm.digest(payload)),None)
    if not existing:existing=next((j for j in all_jobs if j.request_key==request),None)
    if not existing:
        existing=next((j for j in reversed(all_jobs) if j.kind==kind and j.node_id==node and j.slot==slot and usable(j) and
                       j.status in jobs.ACTIVE|{'unknown','succeeded'} and pm.digest(j.payload)==pm.digest(payload)),None)
    if not existing:
        busy=next((j for j in all_jobs if j.kind==kind and j.node_id==node and j.slot==slot and j.status in jobs.ACTIVE|{'unknown'}),None)
        if busy: raise Blocked('Target already has an active/unresolved job: '+node+' '+slot)
        config=run.payload['config'];counts=state.setdefault('created_counts',{})
        category='images' if kind=='plate' else 'videos' if kind=='clip' else 'text' if kind in ('write','raccord','clip_quality') else 'local'
        cap=config.get('max_'+category)
        if cap is not None and counts.get(category,0)>=cap: raise Blocked('Run limit reached: '+category)
        active=[j for j in all_jobs if j.status in jobs.ACTIVE and j.kind!=KIND]
        limit=config['image_parallel'] if kind=='plate' else config['video_parallel'] if kind=='clip' else config['text_parallel']
        family={'write','raccord','clip_quality'} if kind in ('write','raccord','clip_quality') else {kind}
        # Local Whisper / Demucs processes share the CPU with the clay renders.
        if kind=='clip_quality':limit=min(max(1,int(os.getenv('FLOWBOARD_QUALITY_PARALLEL','3'))),limit)
        if kind=='audio_cleanup':limit=max(1,int(os.getenv('FLOWBOARD_AUDIO_PARALLEL','2')))
        # Two local clay renders fit the CPU (4 depth threads each); more only add contention.
        if kind=='motion_reference':limit=max(1,min(4,int(os.getenv('FLOWBOARD_MOTION_PARALLEL','2'))))
        if sum(j.kind in family for j in active)>=limit: raise Waiting()
        existing=AutomationJob(project_id=run.project_id,kind=kind,node_id=node,slot=slot,payload=payload,
                    request_key=request,prepared={**(metadata or {}),'run_id':str(run.id)})
        s.add(existing);all_jobs.append(existing);counts[category]=counts.get(category,0)+1
    if (existing.status=='failed' and kind=='write' and retryable_write(existing.error)
            and existing.prepared.get('run_id')!=str(run.id) and not existing.prepared.get('write_retried')):
        # A prompt that failed only the deterministic format/coverage checks is written once
        # more by a new run: the writer resumes its saved draft under the current checks.
        existing.status='queued';existing.error='';existing.lease_token='';existing.lease_until=None
        existing.prepared={**existing.prepared,'write_retried':True,'run_id':str(run.id)};s.add(existing)
    if existing.status=='failed' and existing.prepared.get('validation_rejected') and not existing.provider_job_id:
        # No provider request occurred: a new run may dispatch these identical inputs safely.
        existing.status='queued';existing.error='';existing.lease_token='';existing.lease_until=None
        existing.prepared={**(metadata or {}),'run_id':str(run.id)};s.add(existing)
    state['tasks'][key]={'id':str(existing.id),'kind':kind,'status':existing.status,'target':node,'slot':slot}
    if existing.status in ('failed','unknown','cancelled'): raise Blocked(key+': '+existing.status+' — '+existing.error)
    if existing.status!='succeeded': raise Waiting()
    return existing


def set_fixtures(board, env_id):
    """Props built into a set (a lift door, a window): drawn in its reference views."""
    out=[]
    for n in board.get('nodes',[]):
        d=n.get('data',{})
        if d.get('kind')!='asset':continue
        item=d.get('asset') or {}
        if env_id in (item.get('fixture_of') or []):
            out.append({'name':item.get('name',''),'description':item.get('target_description') or item.get('description') or item.get('summary') or ''})
    return sorted(out,key=lambda f:f['name'])


def view_item(item, view):
    """The set's profile for one of its two reference views. The view, read from the source's
    own coverage, sets the direction; the design's camera and depth chose a view of their own
    (a lengthwise establishing shot) and would compete with it."""
    views=item.get('views') or {}
    out={k:v for k,v in item.items() if k not in ('views','set_states')}
    if isinstance(out.get('design'),dict):
        out['design']={k:v for k,v in out['design'].items() if k not in ('camera','depth')}
    out['requested_view']=f"{view.upper()} VIEW: {views.get(view,'')}"
    if item.get('set_states'):out['requested_state']=item['set_states']
    return out


def view_prompt(item, view, fixtures, anchored, master='@image1'):
    """What the plate prompt adds for a set drawn as a master view and its reverse. ``master``
    is the reverse's tag for the approved master view (after a bundled style image, if any).

    The two views follow the scene's own coverage: the master looks at the scene's focus (the
    lift a man holds open); the reverse stands at that focus and looks back, so the focus is
    behind its camera. A reverse that still shows the focus gives a video model a second place
    to put the people in."""
    views={k:str(v).strip().rstrip('.') for k,v in (item.get('views') or {}).items() if isinstance(v,str)}
    states=str(item.get('set_states') or '').strip().rstrip('.')
    lines=[f'{view.upper()} VIEW',
           'People named in a view only place its camera; draw the location empty of people.']
    if view=='reverse':
        lines.append(f"{master} is this location's approved master view: {views.get('master','')}. Draw the SAME location "
                     f"as its reverse: the camera stands at what {master} looks at, its focus, and looks back the opposite way: "
                     f"{views.get('reverse','')}. The focus of {master} is behind this camera and out of view; do not show it "
                     f"again. Keep every material, colour, moulding, fixture design, light source and the time of day of "
                     f"{master}. Never mirror {master} or repeat its view.")
        if anchored:
            lines.append('The later images are frames of the source film looking this way: keep their architecture, layout, '
                         'fixture positions and light direction; leave out their people, reflections of people and any '
                         'on-screen captions or text.')
    else:
        lines.append(f"This plate is the location's master view: {views.get('master','')}. The camera looks at the scene's "
                     'focus from where most of its coverage stands, wide enough to show that whole side of the place. '
                     'A second plate shows the reverse from the focus; keep this one to this direction.')
    if states:
        lines.append(f"LANDMARK STATES: {states}. These states win over the profile's avoid list where it "
                     'forbids what they need: an open door shows the space behind it.')
    if fixtures:
        lines.append('BUILT INTO THIS SET (draw them as part of the location, in these states): '+
                     '; '.join(f"{f['name']} — {f['description']}" if f['description'] else f['name'] for f in fixtures))
    return '\n'+'\n'.join(lines)


def material_payload(spec, deps, board):
    from flowboard.services.video_analyzer.production import build_asset_prompt
    item=spec['item'];kind=spec['kind'];style=board.get('style','realistic')
    refs=list(dict.fromkeys(d['reference_url'] for d in deps));prompt=authored_prompt(spec['plate'])
    anchored=False
    view=('reverse' if spec['slot']=='reverse' else 'master') if kind=='environment' and item.get('views') else ''
    if not prompt and kind=='environment':
        # The source film's own widest, emptiest frames of this location anchor its
        # geography; the plate restyles the room and leaves the people out. A set with two
        # views anchors each on the frames that look its way.
        from flowboard.services import set_anchor
        anchors=[u for u in set_anchor.anchor_urls(board,item,view=view) if u not in refs]
        refs+=anchors;anchored=bool(anchors)
    if not prompt:
        if kind=='character':
            state=next((a for a in item.get('states',[]) if a['key']==spec['slot']),None) or next(iter(item.get('states',[])),{'key':'identity','wardrobe':''})
            prompt=automation.build_character_prompt(item,state,has_reference=bool(refs),style=style,design=item.get('design'))
        elif kind=='environment' and view:
            shown=view_item(item,view)
            prompt=automation.build_environment_prompt(shown,aspect_ratio=board.get('aspectRatio','16:9'),style=style,design=shown.get('design'),has_reference=bool(refs))
            if anchored and view=='master':
                prompt+=('\nLOCATION ANCHOR FRAMES\nThe supplied location images are frames of the source film. Keep their '
                         'architecture, layout, fixture positions and light direction; leave out their people, reflections '
                         'of people and any on-screen captions or text. Later frames are further views of the same room.')
            from flowboard.services import film_styles
            prompt+=view_prompt(item,view,set_fixtures(board,spec['asset_id']),anchored,'@image2' if style==film_styles.KEY else '@image1')
            prompt+=set_restyle.plate_block(item)
        elif kind=='environment':
            prompt=automation.build_environment_prompt(item,aspect_ratio=board.get('aspectRatio','16:9'),style=style,design=item.get('design'),has_reference=bool(refs))
            if anchored:
                prompt+=('\nLOCATION ANCHOR FRAMES\nThe supplied location images are frames of the source film. Keep their '
                         'architecture, layout, fixture positions and light direction; leave out their people, reflections '
                         'of people and any on-screen captions or text. Later frames are further views of the same room.')
            prompt+=set_restyle.plate_block(item)
        else:
            bindings=[{'id':d['asset_id'],'name':d['name'],'ref_label':f'@image{refs.index(d["reference_url"])+1}','ref_url':d['reference_url']} for i,d in enumerate(deps)]
            prompt=build_asset_prompt({**item,'dependency_references':bindings},kind=item['kind'],style=style,
                aspect_ratio=board.get('aspectRatio','16:9'),has_reference=bool(refs))
    if board.get('styleNote'):
        prompt += '\nOWNER STYLE NOTE (every sheet of this film): '+board['styleNote']
    if board.get('styleImageUrl') and board['styleImageUrl'] not in refs:
        # A look to follow, never a source of people, objects or layout; added after the
        # prompt is built so it never counts as an identity reference.
        refs.append(board['styleImageUrl'])
        prompt += ('\nSTYLE REFERENCE: the last supplied image is a style reference only. Follow its rendering, palette, '
                   'lighting and line quality; never copy its people, objects, layout, text or composition.')
    if not style_version(board):
        prompt += '\nLOCKED TARGET DEFINITION: '+json.dumps(item,ensure_ascii=False)
    if item.get('target_description'):
        prompt += '\nOWNER TARGET DESIGN (replaces source surface appearance only): '+item['target_description']
        prompt += '\nPreserve source role, prop function, scale needed for the action and character identity across states. Do not draw source and replacement as two objects.'
    return {'prompt':prompt,'image_model':spec.get('image_model') or board.get('imageModel',automation.DEFAULT_IMAGE_MODEL),'image_size':board.get('imageSize','2K'),
            'aspect_ratio':'16:9' if style_version(board) else board.get('aspectRatio','16:9'),
            'reference_urls':list(dict.fromkeys(refs)),'variant_count':1,
            'style':style,'style_version':style_version(board), 'material_kind':item.get('kind',kind) if kind=='asset' else kind}


def writing_body(board, pid, package, continuity_refs, atlas_receipts=None, motion_receipt=None):
    from flowboard.routes.automation import VideoWriteBody
    from flowboard.services.prompt_writer import TIMING_POLICY_VERSION
    key=package['sequence_key'];seq=next(n['data'] for n in board['nodes'] if n.get('id')=='seq:'+key)
    nodes=asset_nodes(board);characters=[];environment=None;extras=[];urls=[]
    own={n for s in seq['shots'] for n in (s.get('source_shots') or [s.get('source_shot')])}
    def scoped(item):
        # An asset's observations of other shots are history: the shot package carries their
        # last-known state into this clip (with any later source correction), so a writer
        # reading both would see two accounts of the same past.
        if isinstance(item.get('observed_states'),list):
            return {**item,'observed_states':[o for o in item['observed_states'] if isinstance(o,dict) and o.get('shot') in own]}
        return item
    def binding(m):
        url=m['reference_url']
        if url not in urls: urls.append(url)
        return {'ref_label':f'@image{urls.index(url)+1}','ref_url':url,'media_id':m['media_id']}
    materials=list(reference_atlas.apply(package['materials'],atlas_receipts or []).values())
    # A set's master view precedes its reverse; a built-in prop shares the master's image.
    materials.sort(key=lambda m:({'character':0,'environment':1}.get(m['kind'],2),m['asset_id'],m.get('state_key')=='reverse'))
    for m in materials:
        node=nodes[m['asset_id']]['data'];item=node[node['kind']]
        ref={**scoped(item),'source_asset_id':m['asset_id'],'id':m['asset_id'],
             'state_key':m.get('state_key'),**binding(m)}
        if item.get('target_description'):
            ref.update(description=m['description'],target_appearance=item['target_description'])
        if m.get('view'):
            # "Hotel corridor — reverse view": the same location, named as one place in two directions.
            ref.update(view=m['view'],name=f"{item.get('name') or m['asset_id']} — {m['view'].split(':')[0]} view")
        if m.get('reference_scope'):
            ref['reference_scope']=deepcopy(m['reference_scope'])
        if m.get('atlas_instruction'):
            ref.update(atlas_cell=m['atlas_cell'],description='ORIGINAL SOURCE SHEET DESIGN (cell numbers here are INNER sheet coordinates): '+m['description']+'\n'+m['atlas_instruction'])
        if m['kind']=='character': characters.append({**ref,'wardrobe':m['wardrobe']})
        elif m['kind']=='environment' and environment is None and m.get('state_key')!='reverse': environment=ref
        else: extras.append(ref)
    for r in continuity_refs:
        extras.append({'id':r['id'],'kind':'continuity_frame','name':r['name'],
            'description':r['description'],**binding({'reference_url':r['url'],'media_id':r.get('media_id','')})})
    if len(urls)>30: raise Blocked('More than 30 references including continuity frames; prepare an atlas or disable boundary previews.')
    from flowboard.services import film_motion
    context=pm.context(pm.build(board,str(pid)),key)
    context['assets']={k:{**a,**({'design':scoped(a['design'])} if isinstance(a.get('design'),dict) else {})} for k,a in context['assets'].items()}
    sequence={**seq['sequence'],'function':seq.get('functionOf',[]),'raccord':seq.get('raccord',[]),
        'video_standard_version':film_motion.version(board.get('style')),
        'preserve_source_shots':board.get('preserveSourceShots',True),'shot_package':package,'timing_policy_version':TIMING_POLICY_VERSION,'reference_format_version':1,
        'production_context':context,'continuity_references':continuity_refs,'reference_atlases':atlas_receipts or [],
        **({'motion_reference':board['motionReference']} if board.get('motionReference') else {})}
    if motion_receipt: sequence['motion_receipt']=motion_receipt
    # Source state is already complete and deterministic; independent writers need not wait for a previous writer.
    return VideoWriteBody(sequence=sequence,shots=seq['shots'],characters=characters,environment=environment,
        reference_assets=extras,production_assets=board.get('productionAssets') if prompt_coverage.is_strict(board.get('productionAssets'),board.get('sourceVerification')) else None,
        # The record of owner source corrections keeps the values they replaced; never source facts.
        source_verification={k:v for k,v in board['sourceVerification'].items() if k!='source_corrections'} if prompt_coverage.is_strict(board.get('productionAssets'),board.get('sourceVerification')) else None,
        style=board.get('style','realistic'),aspect_ratio=board.get('aspectRatio','16:9'),language='en',
        style_note=(automation._style(board['style'])['video_style'] if style_version(board) else next((m['style_note'] for m in materials if m['style_note']),''))
                   +('\nOwner style note: '+board['styleNote'] if board.get('styleNote') else ''))


def completed_clip_contract(body):
    """Facts supporting an already rendered clip, excluding a later scene replan.

    Old direction remains frozen only for completed media. Actual shot inputs,
    material versions/scopes, incoming state, references and authored direction
    still compare exactly. This is never a relaxed submit-time validation.
    """
    contract=deepcopy(body)
    package=contract.get('sequence',{}).get('shot_package',{})
    package.pop('version',None)
    package.pop('raccord_versions',None)
    for shot in package.get('shots',[]):shot.pop('raccord',None)
    return pm.digest(contract)


# Avis Seedance 2.5 per generated second; the white-model motion reference adds about 13 %.
SEEDANCE_USD = {'480p': 0.113, '720p': 0.254, '1080p': 0.45}
MOTION_SHARE = 0.13


def seedance_usd(payload) -> float:
    return float(payload.get('duration_seconds') or 0) * SEEDANCE_USD.get(payload.get('resolution') or '480p', SEEDANCE_USD['480p']) * (1 + MOTION_SHARE)


def budget_check(board, all_jobs, node, payload):
    """Refuse a NEW render that would take the film's Seedance spend past the owner's cap
    (board budgetUsd). A render already submitted or made is never refused."""
    cap = board.get('budgetUsd')
    if not cap:
        return
    digest = pm.digest(payload)
    if any(j.kind == 'clip' and j.node_id == node and j.status in jobs.ACTIVE | {'unknown', 'succeeded'}
           and not (j.prepared or {}).get('set_aside') and pm.digest(j.payload) == digest for j in all_jobs):
        return
    spent = sum(seedance_usd(j.payload or {}) for j in all_jobs if j.kind == 'clip' and j.status in jobs.ACTIVE | {'unknown', 'succeeded'})
    more = seedance_usd(payload)
    if spent + more > float(cap) + 1e-9:
        raise Blocked(f'Vượt trần chi phí ${float(cap):.2f}: đã dùng khoảng ${spent:.2f}, clip này thêm khoảng ${more:.2f}. '
                      'Nâng trần trong Thiết lập để render tiếp.')


def reuse_completed_clip(board, package, body, config, all_jobs, state):
    key=package['sequence_key'];node='vid:'+key
    # Never conceal an unresolved generation behind an older successful one.
    if any(j.kind=='clip' and j.node_id==node and j.status in jobs.ACTIVE|{'unknown'} for j in all_jobs):
        return None
    current=completed_clip_contract(body.model_dump(mode='json'))
    draft=next((n['data'].get('prompt','') for n in board['nodes'] if n['id']==node),'')
    for job in reversed(all_jobs):
        if job.kind!='clip' or job.node_id!=node or job.status!='succeeded' or not job.result.get('url'):continue
        if (job.prepared or {}).get('set_aside'):continue  # an earlier take the owner asked to redo
        old=job.payload
        if not old.get('prompt_contract') or completed_clip_contract(old['prompt_contract'])!=current:continue
        if old.get('resolution')!=config['resolution'] or old.get('aspect_ratio')!=package['aspect_ratio']:continue
        if old.get('unmoderated')!=board.get('unmoderated',True) or bool(old.get('kyc_media_ids'))!=bool(board.get('kyc')):continue
        if draft and draft!=old.get('prompt'):continue
        state['tasks']['clip:'+key]={'id':str(job.id),'kind':'clip','status':'succeeded','target':node,'slot':'',
            'reused':True,'basis':'Unchanged clip facts, materials, incoming state and references; completed direction retained.'}
        return job
    return None


def advance(s, run, project, all_jobs):
    from flowboard.routes.automation import ClipBody
    state=deepcopy(run.result);state.setdefault('tasks',{});run.result=state
    board=jobs.overlay(project.board or {},all_jobs);config=run.payload['config']
    if input_version(board)!=run.payload['input_version']: raise Blocked('Production inputs changed. Start a new run to reuse matching tasks and rebuild affected outputs.')
    packs=selected_packages(board,project.id,config.get('sequence_keys',[]))
    shared,held,reviewing=reskin.review_state(board,packs)
    if shared: raise Blocked('; '.join(shared))
    if held and len(held)==len(packs): raise Blocked('; '.join(f'{k}: {e}' for k,v in held.items() for e in v))
    # A clip held by its own source findings waits; every other clip is produced now.
    usable=lambda ps:[p for p in ps if p['sequence_key'] not in held and p['sequence_key'] not in reviewing]
    packs=usable(packs)
    if not packs and reviewing:
        state.update(stage='source_review',source_pending=sorted(reviewing))
        return state,'running'
    specs=material_specs(board,packs)
    if held: state['source_holds']=held
    else: state.pop('source_holds',None)
    if reviewing: state['source_pending']=sorted(reviewing)
    else: state.pop('source_pending',None)
    # Re-check before ANY material call, including resumed runs. Previously the
    # source gate lived only in preview/prompt dispatch, after image spending.
    if prompt_coverage.is_strict(board.get('productionAssets'),board.get('sourceVerification')):
        for p in packs:
            seq=next(n['data'] for n in board['nodes'] if n.get('id')=='seq:'+p['sequence_key'])
            source_errors=prompt_coverage.source_readiness_issues(seq['shots'],board.get('sourceVerification'))
            if source_errors: raise Blocked('; '.join(source_errors))
    from flowboard.services.production_progress import make_plan
    state['progress_plan'] = make_plan(specs, packs)
    streaming=reskin.enabled(board)
    needs={p['sequence_key']:set(material_specs(board,[p])) for p in packs} if streaming else {}
    if streaming:
        specs=clip_order(specs,packs,needs)
    ready={};waiting=False;errors=[];failed_materials=set()
    state['stage']='materials'
    for key,spec in specs.items():
        if any(d in failed_materials for d in spec['deps']):
            failed_materials.add(key)
            errors.append('Material dependency failed: '+key)
            continue
        if any(d not in ready for d in spec['deps']): waiting=True;continue
        fixtures=set_fixtures(board,spec['asset_id']) if spec['kind']=='environment' else []
        deps=[ready[d] for d in spec['deps']];plate=spec['plate'];signature=pm.digest({'item':spec['item'],'deps':[{k:v for k,v in d.items() if k!='media_id'} for d in deps],'slot':spec['slot'],
            'prompt':authored_prompt(plate),'model':spec.get('image_model') or board.get('imageModel'),'size':board.get('imageSize'),'style':board.get('style'),'style_version':style_version(board),
            **({'fixtures':fixtures} if fixtures else {}),
            **({'style_note':board['styleNote']} if board.get('styleNote') else {}),
            **({'style_image':board['styleImageUrl']} if board.get('styleImageUrl') else {})})
        old=next((j for j in reversed(all_jobs) if j.kind in ('plate','material_binding') and j.node_id==spec['node_id'] and j.slot==spec['slot'] and j.status=='succeeded'),None)
        fresh=plate.get('referenceUrl') and not plate.get('needsIdentityRefresh') and (plate.get('uploaded') or not old or not old.prepared.get('material_signature') or old.prepared['material_signature']==signature)
        try:
            if fresh:
                image={'reference_url':plate['referenceUrl'],'media_id':plate.get('mediaId','')}
                if not old or not old.prepared.get('material_signature'):
                    binding=AutomationJob(project_id=run.project_id,kind='material_binding',node_id=spec['node_id'],slot=spec['slot'],
                        request_key='material-binding:'+pm.digest([spec['node_id'],spec['slot'],signature,image]),
                        status='succeeded',prepared={'material_signature':signature},result={'images':[image]})
                    s.add(binding);all_jobs.append(binding)
            else:
                if spec.get('related_ids') and old and old.prepared.get('material_signature'):
                    raise Blocked('A co-reference group changed; set generation_depends_on_asset_ids to define its new build order: '+spec['asset_id'])
                job=_task(s,run,all_jobs,state,'material:'+key,'plate',spec['node_id'],spec['slot'],material_payload(spec,deps,board),{'material_signature':signature,'base_material_prompt':authored_prompt(plate)})
                image=(job.result.get('images') or [{}])[0]
                if not image.get('reference_url'): raise Blocked('Material has no published reference: '+key)
            ready[key]={'reference_url':image['reference_url'],'media_id':image.get('media_id',''),'asset_id':spec['asset_id'],'name':spec['item'].get('name',spec['asset_id'])}
        except Waiting: waiting=True
        except Blocked as exc: errors.append(str(exc));failed_materials.add(key)
    state['ready_materials'] = list(ready)
    material_holds={}
    if streaming:
        # A failed sheet holds only the clips that need it; a clip whose sheets are all
        # ready goes on now, the others wait for theirs.
        material_holds={k:[e for e in errors] for k,ks in needs.items() if ks & failed_materials}
        ready_clips={k for k,ks in needs.items() if ks <= set(ready)}
        state['material_ready_clips']=sorted(ready_clips)
        if material_holds: state['material_holds']=sorted(material_holds)
        else: state.pop('material_holds',None)
        if not ready_clips:
            if waiting: return state,'running'
            state['errors']=errors;state['stage']='blocked';return state,'blocked'
        errors=[]
    else:
        if errors: state['errors']=errors;state['stage']='blocked';return state,'blocked'
        if waiting: return state,'running'
        ready_clips=None
    # Recompile only after the selected clips' material dependencies have settled.
    board=jobs.overlay(project.board or {},all_jobs);packs=usable(selected_packages(board,project.id,config.get('sequence_keys',[])))
    # A clip waiting for the running source review gets its sheets, never its prompt or video.
    todo=lambda ps:[p for p in ps if (ready_clips is None or p['sequence_key'] in ready_clips)
                    and p['sequence_key'] not in reviewing]
    if config.get('kyc_required',board.get('kyc',False)):
        missing=sorted({m['reference_url'] for p in todo(packs) for m in p['materials'].values() if not m['media_id']})
        if missing:
            try: _task(s,run,all_jobs,state,'ingest','ingest','production:ingest','',{'urls':missing})
            except Waiting: return state,'running'
            # Ingest overlays supply media IDs on the next tick.
            return state,'running'
    state['stage']='raccord';waiting=False
    # The accepted reskin uses original motion, not a second generated blocking plan.
    scenes={} if reskin.enabled(board) else raccord.scene_inputs(board,str(project.id));selected={p['sequence_key'] for p in packs}
    state['raccord_scopes'] = [scene['scope'] for scene in scenes.values() if any(sh['sequence_key'] in selected for sh in scene['shots'])]
    for scene in scenes.values():
        if not any(sh['sequence_key'] in selected for sh in scene['shots']): continue
        try:_task(s,run,all_jobs,state,'raccord:'+scene['scope'],'raccord','raccord:'+scene['version'],'',scene)
        except Waiting:waiting=True
    if waiting:return state,'running'
    board=jobs.overlay(project.board or {},all_jobs);packs=usable(selected_packages(board,project.id,config.get('sequence_keys',[])))
    blockers=[i for p in todo(packs) for i in p['issues'] if i.get('blocking')]
    if blockers: raise Blocked(json.dumps(blockers,ensure_ascii=False))
    shots={sh['id']:(p,sh) for p in packs for sh in p['shots']}
    clips={};completed=set();failed_clips=set();state['stage']='prompts' if config['mode']=='prepare' else 'videos'
    generated={};holds={}     # reviewed takes (continuity frames come from these) / takes held for review
    def checked_clip(job, package):
        from flowboard.services import production_quality as quality
        key=package['sequence_key']
        clip={'url':job.result['url'],'job_id':str(job.id),
            'duration_s':sum(float(sh['duration_s']) for sh in package['shots']), 'sequence_key':key}
        if config.get('audio_cleanup'):
            state['stage']='audio_cleanup'
            from flowboard.services import reskin_audio
            cleaned=_task(s,run,all_jobs,state,'audio:'+key,'audio_cleanup','vid:'+key,'audio',
                {'version':reskin_audio.VERSION,'source_job':str(job.id),'url':clip['url'],
                 'has_dialogue':any(d.get('line') for sh in package['shots'] for d in sh.get('dialogue',[]))})
            state.setdefault('audio_results',{})[key]=cleaned.result
            clip.update(url=cleaned.result['url'],native_url=job.result['url'])
        state.setdefault('preview_clips',{})[key]=clip
        state['stage']='quality'
        q=_task(s,run,all_jobs,state,'quality:'+key,'clip_quality','vid:'+key,'quality',
            {'version':quality.VERSION,'model':quality.MODEL,'source_job':clip['job_id'],'url':clip['url'],
             'package':package,'timing_policy':config.get('timing_policy','source_duration')})
        state.setdefault('quality_results',{})[key]=q.result
        # Reviewed, cleared or not: a continuity frame may be taken from it now.
        generated[key]={'url':clip['url'],'job_id':clip['job_id']}
        try:quality.require_pass(q.result,clip,package['version'])
        except ValueError as exc:raise QualityHold(str(exc)) from exc
        return {**clip,'quality_job':str(q.id),'package_version':package['version']}
    for p in todo(packs):
        key=p['sequence_key'];refs=[]
        try:
            motion=None
            if reskin.enabled(board):
                state['stage']='motion'
                motion=_task(s,run,all_jobs,state,'motion:'+key,'motion_reference','vid:'+key,'motion',reskin.motion_payload(board,p))
            # Boundary plans are optional paid reference stills, never falsely declared exact i2v constraints.
            if config['boundary_frames']:
                for index,which in ((0,'start'),(len(p['shots'])-1,'end')):
                    f=shot_package.keyframe(p,index,which);payload={'prompt':f['prompt'],'reference_urls':f['reference_urls'],
                        'image_model':board.get('imageModel',automation.DEFAULT_IMAGE_MODEL),'image_size':board.get('imageSize','2K'),'aspect_ratio':p['aspect_ratio']}
                    j=_task(s,run,all_jobs,state,f'frame:{key}:{which}','plate','vid:'+key,f'shotframe:{index}:{which}',payload,
                        {'sequence_key':key,'shot_frame':{k:f[k] for k in ('package_version','shot_id','shot_index','which')}})
                    im=j.result['images'][0]
                    refs.append({'id':f'frame:{key}:{which}','name':f'{which} composition guide','url':im['reference_url'],'media_id':im.get('media_id'),
                                 'description':f'Approved planned {which} composition; do not copy sheet layout or add events.'})
            if config['mode']=='render' and not reskin.enabled(board):
                for shot in p['shots']:
                    pred=shot.get('raccord',{}).get('predecessor_shot_id')
                    if not pred: continue
                    if pred not in shots: raise Blocked('Required predecessor is outside the selected clips: '+pred)
                    if shots[pred][0]['sequence_key']==key: continue
                    prev_pack,prev_shot=shots[pred];prev_key=prev_pack['sequence_key']
                    if prev_key not in generated: raise Waiting()
                    elapsed=sum(float(x['duration_s']) for x in prev_pack['shots'][:prev_shot['index']+1])
                    ending = config.get('timing_policy')=='full_take' and prev_shot['index']==len(prev_pack['shots'])-1
                    j=_task(s,run,all_jobs,state,'handoff:'+pred,'extract_frame','vid:'+prev_key,'handoff:'+pred,
                        {'url':generated[prev_key]['url'],'time_s':max(0,elapsed-1/config['fps']),'source_job':generated[prev_key]['job_id'],
                         **({'at_end':True,'fps':config['fps']} if ending else {})})
                    im=j.result['images'][0]
                    refs.append({'id':'continuity:'+pred,'name':'Previous shot closing state','url':im['reference_url'],'media_id':im.get('media_id'),
                        'description':'Closing state of the preceding generated shot; use for pose/prop continuity only. Locked source facts and identity sheets take precedence; do not copy its camera angle or propagate contradictions.'})
                # Consecutive clips of one continuous scene: the next generation sees the set as
                # the previous one filmed it, or the cut between them redesigns the room.
                index=packs.index(p);prev_pack=packs[index-1] if index else None
                if (config.get('set_continuity',True) and not refs and prev_pack and prev_pack['shots'] and p['shots']
                        and prev_pack['shots'][-1].get('continuity_scope')==p['shots'][0].get('continuity_scope')):
                    prev_key=prev_pack['sequence_key']
                    if prev_key not in generated: raise Waiting()
                    elapsed=sum(float(x['duration_s']) for x in prev_pack['shots'])
                    ending=config.get('timing_policy')=='full_take'
                    j=_task(s,run,all_jobs,state,'setframe:'+prev_key,'extract_frame','vid:'+prev_key,'setframe:'+prev_pack['shots'][-1]['id'],
                        {'url':generated[prev_key]['url'],'time_s':max(0,elapsed-1/config['fps']),'source_job':generated[prev_key]['job_id'],
                         **({'at_end':True,'fps':config['fps']} if ending else {})})
                    im=j.result['images'][0]
                    refs.append({'id':'set:'+prev_key,'name':'Set as already filmed','url':im['reference_url'],'media_id':im.get('media_id'),
                        'description':'Closing frame of the previous clip of this continuous scene. Keep this room as it was filmed: '
                                      'walls, doorways, materials, fixtures, light and where the people stand in it. The camera angle '
                                      'may differ; locked shot facts and identity sheets take precedence.'})
            atlas_receipts=[];atlas_waiting=False
            packing=reference_atlas.plan(p['materials'],1 if reskin.enabled(board) else len({r['url'] for r in refs}))
            for page in packing['pages']:
                try:
                    atlas_job=_task(s,run,all_jobs,state,'atlas:'+page['version'],'atlas','atlas:'+page['version'],'',page)
                    atlas_receipts.append({'job_id':str(atlas_job.id),'result':atlas_job.result})
                except Waiting:atlas_waiting=True
            if atlas_waiting:raise Waiting()
            if reskin.enabled(board):
                state['stage']='staging'
                materials=reference_atlas.apply(p['materials'],atlas_receipts)
                bindings=[{'name':m['name'],'kind':m['kind'],'url':m['reference_url'],
                           'asset_id':m['asset_id'],'description':m['description'],
                           'atlas_instruction':m.get('atlas_instruction','')} for m in materials.values()]
                stage=_task(s,run,all_jobs,state,'staging:'+key,'plate','vid:'+key,'staging',reskin.staging_payload(board,p,bindings))
                im=stage.result['images'][0]
                refs.append({'id':'staging:'+key,'name':'Single wide scene staging','url':im['reference_url'],'media_id':im.get('media_id'),
                             'description':'ONE continuous 21:9 wide staging scene; positions and eyelines only, never a shot/framing override.'})
                state.setdefault('reskin_references',{})[key]={'motion':motion.result,'staging':im}
            body=writing_body(board,project.id,p,refs,atlas_receipts,motion.result if motion else None)
            # Validate all deterministic inputs before queuing paid text or video.
            jobs.validate_package(board,str(project.id),body)
            if config['mode']=='render':
                old=reuse_completed_clip(board,p,body,config,all_jobs,state)
                if old:
                    clips[key]=checked_clip(old,p)
                    continue
            video=next(n['data'] for n in board['nodes'] if n.get('id')=='vid:'+key)
            payload=body.model_dump(mode='json')
            if video.get('promptBy')=='manual':
                if not video.get('prompt','').strip(): raise Blocked('Nhập prompt tự chỉnh trước khi chạy clip.')
                payload.update(provided_prompt=video['prompt'],provided_end_state=video.get('endState') or '',
                               provided_staging_decisions=video.get('stagingDecisions') or [])
            j=_task(s,run,all_jobs,state,'write:'+key,'write','vid:'+key,'prompt',payload,
                    {'base_prompt':video.get('prompt','')})
            if config['mode']=='prepare':
                completed.add(key);continue
            out=j.result;ordered,issues=prompt_coverage.reference_slots([*body.characters,*([body.environment] if body.environment else []),*body.reference_assets])
            if issues:raise Blocked('; '.join(issues))
            if board.get('kyc') and any(not r.get('media_id') for r in ordered): raise Blocked('Missing media ID for KYC reference.')
            clip=ClipBody(prompt=out['prompt'],duration_seconds=out['duration_seconds'],reference_urls=[r['ref_url'] for r in ordered],
                kyc_media_ids=[r['media_id'] for r in ordered] if board.get('kyc') else [],unmoderated=board.get('unmoderated',True),
                aspect_ratio=p['aspect_ratio'],resolution=config['resolution'],project_id=project.id,sequence_key=key,
                motion_media_id=motion.result['media_id'] if motion else '',motion_job_id=str(motion.id) if motion else '',
                prompt_contract=body,coverage=out.get('coverage'),contract_digest=out.get('contract_digest',''),coverage_token=out.get('coverage_token',''))
            budget_check(board,all_jobs,'vid:'+key,clip.model_dump(mode='json'))
            j=_task(s,run,all_jobs,state,'clip:'+key,'clip','vid:'+key,'',clip.model_dump(mode='json'))
            clips[key]=checked_clip(j,p)
        except Waiting: continue
        except QualityHold as exc:
            holds[key]=key+': '+str(exc)
        except (Blocked,ValueError) as exc:
            errors.append(key+': '+str(exc));failed_clips.add(key)
            # Prompts are independent; renders continue from the clips before them. A reskin
            # clip has no chain to the clip before it, so one clip's refusal holds only itself.
            if config['mode']!='prepare' and not streaming:break
    if streaming:
        # Settle the run only when every clip is done or held for a stated reason.
        settled=(completed if config['mode']=='prepare' else set(clips)|set(holds))|failed_clips|set(material_holds)
        errors=errors+[f'{k}: sheet failed — '+'; '.join(v)[:400] for k,v in material_holds.items()]
        if len(settled)<len(packs) or reviewing:
            if errors: state['errors']=errors
            return state,'running'
        if errors:
            state['errors']=errors;state['failed_clips']=sorted(failed_clips|set(material_holds));return state,'blocked'
        state.pop('errors',None)
    # A prepare run blocks once every other clip has its prompt; it reports all refusals together.
    if errors and (config['mode']!='prepare' or len(completed)+len(errors)==len(packs)):
        state['errors']=errors;return state,'blocked'
    if errors: state['errors']=errors
    if len(completed if config['mode']=='prepare' else set(clips)|set(holds))!=len(packs):return state,'running'
    if holds:
        # Every clip is generated; those the review did not clear wait for the reviewer.
        state['quality_holds']=list(holds.values());state['errors']=list(holds.values());state['stage']='quality'
        if reskin.enabled(board) and config.get('assemble') and not held and len(state.get('preview_clips',{}))==len(packs):
            try:
                draft=_task(s,run,all_jobs,state,'preview_assembly','assemble','production:preview','',
                    {'clips':[state['preview_clips'][p['sequence_key']] for p in packs],
                     'fps':config['fps'],'aspect_ratio':board.get('aspectRatio','1:1'),'resolution':config['resolution'],
                     'timing_policy':config.get('timing_policy','full_take'),'require_quality':False})
                state['preview_output']={**draft.result,'status':'unapproved_preview','findings_retained':True}
            except Waiting: return state,'running'
        return state,'blocked'
    if held:
        # Never assemble a film with holes: the clips produced so far are kept and a new
        # run, once the owner has resolved or accepted these findings, reuses them.
        state['stage']='source_review';state['errors']=[f'{k}: {e}' for k,v in held.items() for e in v]
        return state,'blocked'
    if config['mode']=='render' and config['assemble']:
        state['stage']='assembly'
        try:
            j=_task(s,run,all_jobs,state,'assembly','assemble','production:assembly','',{'clips':[clips[p['sequence_key']] for p in packs],
                'fps':config['fps'],'aspect_ratio':board.get('aspectRatio','16:9'),'resolution':config['resolution'],
                'timing_policy':config.get('timing_policy','source_duration'),'require_quality':True})
        except Waiting:return state,'running'
        state['output']=j.result
    if config['mode']=='render':state['quality_status']='automated_checks_passed'
    state['stage']='complete';return state,'succeeded'


def resume_settled(run_id):
    """Set a run that settled while its clips waited (e.g. for a source review) going again.
    Paid tasks stay as they are; a run waiting at the master-sheet review is left alone."""
    with get_session() as s:
        run=s.exec(select(AutomationJob).where(AutomationJob.id==uuid.UUID(str(run_id))).with_for_update()).first()
        if not run or run.kind!=KIND or run.status!='blocked' or (run.result or {}).get('stage')=='master_review':
            return False
        run.status='running';run.error='';run.result={**(run.result or {}),'errors':[]}
        run.updated_at=jobs.now();s.add(run);s.commit()
        return True


def tick(run_id):
    with get_session() as s:
        # SQLite ignores SELECT FOR UPDATE. Serialize the short scheduling
        # transaction across processes before reading, as Postgres row locks do.
        if s.bind.dialect.name == 'sqlite':
            from sqlalchemy import text
            s.execute(text('BEGIN IMMEDIATE'))
        initial=s.get(AutomationJob,run_id)
        if not initial or initial.status!='running':return
        project=s.exec(select(AutomationProject).where(AutomationProject.id==initial.project_id).with_for_update(skip_locked=True)).first()
        if not project:return
        run=s.exec(select(AutomationJob).where(AutomationJob.id==run_id).with_for_update().execution_options(populate_existing=True)).one()
        if run.status!='running':return
        all_jobs=s.exec(select(AutomationJob).where(AutomationJob.project_id==project.id).order_by(AutomationJob.created_at)).all()
        try:run.result,run.status=advance(s,run,project,all_jobs);run.error='; '.join(run.result.get('errors',[]))[:1200]
        except (Blocked,ValueError) as exc:run.status='blocked';run.error=str(exc)[:1200]
        except Exception as exc:
            import logging
            logging.getLogger(__name__).exception('Production controller stopped')
            run.status='blocked';run.error=('Controller error: '+str(exc))[:1200]
        run.result=deepcopy(run.result);run.updated_at=jobs.now();s.add(run);s.commit()


async def worker():
    import asyncio
    import logging
    log=logging.getLogger(__name__)
    while True:
        try:
            with get_session() as s: ids=s.exec(select(AutomationJob.id).where(AutomationJob.kind==KIND,AutomationJob.status=='running')).all()
            for rid in ids:
                try:await asyncio.to_thread(tick,rid)
                except Exception:log.exception('Production controller failed for %s',rid)
        except Exception:log.exception('Production queue unavailable')
        await asyncio.sleep(3)


def validate_run_input(project_id, metadata):
    if not metadata.get('run_id'): return
    with get_session() as s:
        parent=s.get(AutomationJob,uuid.UUID(metadata['run_id']))
        project=s.get(AutomationProject,project_id)
        if not parent or not project:raise ValueError('Production run/project no longer exists')
        if input_version(jobs.project_board(s,project))!=parent.payload['input_version']:
            raise ValueError('Production inputs changed before dispatch; no new generation submitted.')
