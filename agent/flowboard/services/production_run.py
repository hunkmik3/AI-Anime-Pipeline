"""Persistent board-to-film orchesator. Controller ticks never occupy provider slots.

Runs and their task references live in AutomationJob; all paid operations continue
through the existing leased worker. A stopped/unknown task is never auto-resubmitted.
"""
from copy import deepcopy
import json
import uuid
from sqlmodel import select

from flowboard.db import get_session
from flowboard.db.models import AutomationJob, AutomationProject
from flowboard.services import reference_atlas, material_dependencies, automation, automation_jobs as jobs, production_manifest as pm, shot_package, raccord, prompt_coverage

KIND = 'production_run'
RUNNING = {'running'}
SCHEMA = 1


def authored_prompt(plate):
    prompt=plate.get('prompt','')
    return plate.get('baseMaterialPrompt','') if prompt and prompt==plate.get('runtimePrompt') else prompt


def input_version(board):
    """User-authored production inputs only; generated results and canvas layout excluded."""
    nodes = []
    for n in board.get('nodes', []):
        d = n.get('data', {}); kind = d.get('kind')
        if kind == 'sequence':
            seq = {k:v for k,v in d.get('sequence', {}).items() if k not in ('shot_package','production_context')}
            nodes.append({'id':n['id'],'sequence':seq,'shots':d.get('shots',[]),'function':d.get('functionOf'), 'raccord':d.get('raccord')})
        elif kind in ('character','environment','asset'):
            nodes.append({'id':n['id'],'definition':d.get(kind),'activeState':d.get('activeState'),
                          'prompts':{k:authored_prompt(v) for k,v in {'identity':d.get('identity',{}),'plate':d.get('plate',{}),**d.get('states',{})}.items()}})
    return pm.digest({'schema':SCHEMA,'nodes':nodes, **{k:board.get(k) for k in
        ('productionAssets','sourceVerification','preserveSourceShots','style','aspectRatio','imageModel','imageSize','kyc','unmoderated')}})


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
    required = {(m['asset_id'],m.get('state_key')) for p in packs for m in p['materials'].values()}
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
        slot=(state or 'identity') if kind=='character' else 'plate'
        key=node['id']+':'+slot
        if key in trail: raise ValueError('Cyclic material dependency: '+' → '.join((*trail,key)))
        if key in specs: return key
        plate=d.get('states',{}).get(slot,{}) if kind=='character' and slot!='identity' else d.get(slot,{})
        # A single wardrobe identity sheet already satisfies this state.
        if kind=='character' and slot!='identity' and not plate.get('referenceUrl') and len(item.get('states',[]))<=1 and d.get('identity',{}).get('referenceUrl'):
            return add(aid,None,trail)
        deps=[]
        if kind=='character' and slot!='identity': deps.append(add(aid,None,(*trail,key)))
        definition=assets.get(aid,{})
        for dep in sorted(dependency_graph.get(aid,set())):
            other=nodes.get(dep,{}).get('data',{})
            deps.append(add(dep,other.get('activeState') if other.get('kind')=='character' else None,(*trail,key)))
        specs[key]={'key':key,'node_id':node['id'],'asset_id':aid,'slot':slot,'kind':kind,'item':item,'plate':plate,'deps':deps,'related_ids':related.get(aid,[]),'dependency_notes':notes}
        return key
    for aid,state in sorted(required,key=lambda v:(v[0],str(v[1]))): add(aid,state)
    return specs


def preview(board, pid, config):
    packs=selected_packages(board,pid,config.get('sequence_keys',[]))
    issues=[{'clip':p['sequence_key'],**i} for p in packs for i in p['issues'] if i.get('blocking') and i['code'] not in ('missing_material','missing_costume_sheet')]
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
        try:reference_atlas.plan(p['materials'],2 if config.get('boundary_frames') else 0)
        except ValueError as exc:issues.append({'clip':p['sequence_key'],'code':'reference_limit','message':str(exc),'blocking':True})
    return {'input_version':input_version(board),'clips':[p['sequence_key'] for p in packs],
            'shots':sum(len(p['shots']) for p in packs),'materials':len(specs) if specs else len({k for p in packs for k in p['materials']}),
            'missing_materials':sum(not s['plate'].get('referenceUrl') for s in specs.values()) if specs else len({k for p in packs for k,m in p['materials'].items() if not m['reference_url']}),
            'issues':issues,'ready':not issues,'config':config,
            'dependency_notes':next(iter(specs.values()),{}).get('dependency_notes',[]),
            'atlases':[{"clip":p['sequence_key'],**reference_atlas.plan(p['materials'],2 if config.get('boundary_frames') else 0)} for p in packs] if not any(i['code']=='reference_limit' for i in issues) else []}


def start(pid, revision, config, request_key):
    with get_session() as s:
        project=s.exec(select(AutomationProject).where(AutomationProject.id==pid).with_for_update()).one()
        if project.revision!=revision: raise ValueError('Board changed; save/reload before starting.')
        previous=s.exec(select(AutomationJob).where(AutomationJob.project_id==pid,AutomationJob.request_key==request_key)).first()
        if previous:
            if previous.kind!=KIND or previous.payload.get('config')!=config: raise ValueError('Request key already used with different settings.')
            return jobs.public(previous)
        active=s.exec(select(AutomationJob).where(AutomationJob.project_id==pid,AutomationJob.kind==KIND,AutomationJob.status=='running')).first()
        if active: raise ValueError('A production run is already active for this project.')
        board=jobs.project_board(s,project);report=preview(board,pid,config)
        if not report['ready']: raise ValueError(json.dumps(report['issues'],ensure_ascii=False))
        job=AutomationJob(project_id=pid,kind=KIND,node_id='production',request_key=request_key,status='running',
            payload={'config':config,'input_version':report['input_version']},result={'stage':'materials','tasks':{},'created_counts':{},'preview':report})
        s.add(job);s.commit();s.refresh(job);return jobs.public(job)


class Waiting(Exception): pass
class Blocked(Exception): pass


def _task(s, run, all_jobs, state, key, kind, node, slot, payload, metadata=None):
    """Atomic reservation + durable link under project/run row locks."""
    fingerprint=pm.digest({'kind':kind,'node':node,'slot':slot,'payload':payload})
    request='production:'+fingerprint
    existing=next((j for j in reversed(all_jobs) if j.kind==kind and j.node_id==node and j.slot==slot and j.status=='succeeded' and pm.digest(j.payload)==pm.digest(payload)),None)
    if not existing:existing=next((j for j in all_jobs if j.request_key==request),None)
    if not existing:
        existing=next((j for j in reversed(all_jobs) if j.kind==kind and j.node_id==node and j.slot==slot and
                       j.status in jobs.ACTIVE|{'unknown','succeeded'} and pm.digest(j.payload)==pm.digest(payload)),None)
    if not existing:
        busy=next((j for j in all_jobs if j.kind==kind and j.node_id==node and j.slot==slot and j.status in jobs.ACTIVE|{'unknown'}),None)
        if busy: raise Blocked('Target already has an active/unresolved job: '+node+' '+slot)
        config=run.payload['config'];counts=state.setdefault('created_counts',{})
        category='images' if kind=='plate' else 'videos' if kind=='clip' else 'text' if kind in ('write','raccord') else 'local'
        cap=config.get('max_'+category)
        if cap is not None and counts.get(category,0)>=cap: raise Blocked('Run limit reached: '+category)
        active=[j for j in all_jobs if j.status in jobs.ACTIVE and j.kind!=KIND]
        limit=config['image_parallel'] if kind=='plate' else config['video_parallel'] if kind=='clip' else config['text_parallel']
        family={'write','raccord'} if kind in ('write','raccord') else {kind}
        if sum(j.kind in family for j in active)>=limit: raise Waiting()
        existing=AutomationJob(project_id=run.project_id,kind=kind,node_id=node,slot=slot,payload=payload,
                    request_key=request,prepared={**(metadata or {}),'run_id':str(run.id)})
        s.add(existing);all_jobs.append(existing);counts[category]=counts.get(category,0)+1
    if existing.status=='failed' and existing.prepared.get('validation_rejected') and not existing.provider_job_id:
        # No provider request occurred: a new run may dispatch these identical inputs safely.
        existing.status='queued';existing.error='';existing.lease_token='';existing.lease_until=None
        existing.prepared={**(metadata or {}),'run_id':str(run.id)};s.add(existing)
    state['tasks'][key]={'id':str(existing.id),'kind':kind,'status':existing.status,'target':node,'slot':slot}
    if existing.status in ('failed','unknown','cancelled'): raise Blocked(key+': '+existing.status+' — '+existing.error)
    if existing.status!='succeeded': raise Waiting()
    return existing


def material_payload(spec, deps, board):
    from flowboard.services.video_analyzer.production import build_asset_prompt
    item=spec['item'];kind=spec['kind'];style=board.get('style','realistic')
    refs=list(dict.fromkeys(d['reference_url'] for d in deps));prompt=authored_prompt(spec['plate'])
    if not prompt:
        if kind=='character':
            state=next((a for a in item.get('states',[]) if a['key']==spec['slot']),None) or next(iter(item.get('states',[])),{'key':'identity','wardrobe':''})
            prompt=automation.build_character_prompt(item,state,has_reference=bool(refs),style=style,design=item.get('design'))
        elif kind=='environment': prompt=automation.build_environment_prompt(item,aspect_ratio=board.get('aspectRatio','16:9'),style=style,design=item.get('design'))
        else:
            bindings=[{'id':d['asset_id'],'name':d['name'],'ref_label':f'@image{refs.index(d["reference_url"])+1}','ref_url':d['reference_url']} for i,d in enumerate(deps)]
            prompt=build_asset_prompt({**item,'dependency_references':bindings},kind=item['kind'],style=style,
                aspect_ratio=board.get('aspectRatio','16:9'),has_reference=bool(refs))
    prompt += '\nLOCKED TARGET DEFINITION: '+json.dumps(item,ensure_ascii=False)
    return {'prompt':prompt,'image_model':board.get('imageModel',automation.DEFAULT_IMAGE_MODEL),'image_size':board.get('imageSize','2K'),
            'aspect_ratio':board.get('aspectRatio','16:9'),'reference_urls':list(dict.fromkeys(refs)),'variant_count':1}


def writing_body(board, pid, package, continuity_refs, atlas_receipts=None):
    from flowboard.routes.automation import VideoWriteBody
    from flowboard.services.prompt_writer import TIMING_POLICY_VERSION
    key=package['sequence_key'];seq=next(n['data'] for n in board['nodes'] if n.get('id')=='seq:'+key)
    nodes=asset_nodes(board);characters=[];environment=None;extras=[];urls=[]
    def binding(m):
        url=m['reference_url']
        if url not in urls: urls.append(url)
        return {'ref_label':f'@image{urls.index(url)+1}','ref_url':url,'media_id':m['media_id']}
    materials=list(reference_atlas.apply(package['materials'],atlas_receipts or []).values())
    materials.sort(key=lambda m:({'character':0,'environment':1}.get(m['kind'],2),m['asset_id']))
    for m in materials:
        node=nodes[m['asset_id']]['data'];item=node[node['kind']]
        ref={**item,'source_asset_id':m['asset_id'],'id':m['asset_id'],**binding(m)}
        if m.get('atlas_instruction'):
            ref.update(atlas_cell=m['atlas_cell'],description='ORIGINAL SOURCE SHEET DESIGN (cell numbers here are INNER sheet coordinates): '+m['description']+'\n'+m['atlas_instruction'])
        if m['kind']=='character': characters.append({**ref,'wardrobe':m['wardrobe']})
        elif m['kind']=='environment' and environment is None: environment=ref
        else: extras.append(ref)
    for r in continuity_refs:
        extras.append({'id':r['id'],'kind':'continuity_frame','name':r['name'],
            'description':r['description'],**binding({'reference_url':r['url'],'media_id':r.get('media_id','')})})
    if len(urls)>9: raise Blocked('More than 9 references including continuity frames; prepare an atlas or disable boundary previews.')
    sequence={**seq['sequence'],'function':seq.get('functionOf',[]),'raccord':seq.get('raccord',[]),
        'preserve_source_shots':board.get('preserveSourceShots',True),'shot_package':package,'timing_policy_version':TIMING_POLICY_VERSION,'reference_format_version':1,
        'production_context':pm.context(pm.build(board,str(pid)),key),'continuity_references':continuity_refs,'reference_atlases':atlas_receipts or []}
    # Source state is already complete and deterministic; independent writers need not wait for a previous writer.
    return VideoWriteBody(sequence=sequence,shots=seq['shots'],characters=characters,environment=environment,
        reference_assets=extras,production_assets=board.get('productionAssets') if prompt_coverage.is_strict(board.get('productionAssets'),board.get('sourceVerification')) else None,
        source_verification=board.get('sourceVerification') if prompt_coverage.is_strict(board.get('productionAssets'),board.get('sourceVerification')) else None,
        style=board.get('style','realistic'),aspect_ratio=board.get('aspectRatio','16:9'),language='en',
        style_note=next((m['style_note'] for m in materials if m['style_note']),''))


def advance(s, run, project, all_jobs):
    from flowboard.routes.automation import ClipBody
    state=deepcopy(run.result);state.setdefault('tasks',{});run.result=state
    board=jobs.overlay(project.board or {},all_jobs);config=run.payload['config']
    if input_version(board)!=run.payload['input_version']: raise Blocked('Production inputs changed. Start a new run to reuse matching tasks and rebuild affected outputs.')
    packs=selected_packages(board,project.id,config.get('sequence_keys',[]));specs=material_specs(board,packs)
    ready={};waiting=False;errors=[]
    state['stage']='materials'
    for key,spec in specs.items():
        if any(d not in ready for d in spec['deps']): waiting=True;continue
        deps=[ready[d] for d in spec['deps']];plate=spec['plate'];signature=pm.digest({'item':spec['item'],'deps':[{k:v for k,v in d.items() if k!='media_id'} for d in deps],'slot':spec['slot'],
            'prompt':authored_prompt(plate),'model':board.get('imageModel'),'size':board.get('imageSize'),'style':board.get('style')})
        old=next((j for j in reversed(all_jobs) if j.kind in ('plate','material_binding') and j.node_id==spec['node_id'] and j.slot==spec['slot'] and j.status=='succeeded'),None)
        fresh=plate.get('referenceUrl') and (not old or not old.prepared.get('material_signature') or old.prepared['material_signature']==signature)
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
        except Blocked as exc: errors.append(str(exc))
    if errors: state['errors']=errors;state['stage']='blocked';return state,'blocked'
    if waiting: return state,'running'
    # Recompile only after all material dependencies have settled.
    board=jobs.overlay(project.board or {},all_jobs);packs=selected_packages(board,project.id,config.get('sequence_keys',[]))
    if config.get('kyc_required',board.get('kyc',False)):
        missing=sorted({m['reference_url'] for p in packs for m in p['materials'].values() if not m['media_id']})
        if missing:
            try: _task(s,run,all_jobs,state,'ingest','ingest','production:ingest','',{'urls':missing})
            except Waiting: return state,'running'
            # Ingest overlays supply media IDs on the next tick.
            return state,'running'
    state['stage']='raccord';waiting=False
    scenes=raccord.scene_inputs(board,str(project.id));selected={p['sequence_key'] for p in packs}
    for scene in scenes.values():
        if not any(sh['sequence_key'] in selected for sh in scene['shots']): continue
        try:_task(s,run,all_jobs,state,'raccord:'+scene['scope'],'raccord','raccord:'+scene['version'],'',scene)
        except Waiting:waiting=True
    if waiting:return state,'running'
    board=jobs.overlay(project.board or {},all_jobs);packs=selected_packages(board,project.id,config.get('sequence_keys',[]))
    blockers=[i for p in packs for i in p['issues'] if i.get('blocking')]
    if blockers: raise Blocked(json.dumps(blockers,ensure_ascii=False))
    shots={sh['id']:(p,sh) for p in packs for sh in p['shots']}
    clips={};completed=set();state['stage']='prompts' if config['mode']=='prepare' else 'videos'
    for p in packs:
        key=p['sequence_key'];refs=[]
        try:
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
            if config['mode']=='render':
                for shot in p['shots']:
                    pred=shot.get('raccord',{}).get('predecessor_shot_id')
                    if not pred: continue
                    if pred not in shots: raise Blocked('Required predecessor is outside the selected clips: '+pred)
                    if shots[pred][0]['sequence_key']==key: continue
                    prev_pack,prev_shot=shots[pred];prev_key=prev_pack['sequence_key']
                    if prev_key not in clips: raise Waiting()
                    elapsed=sum(float(x['duration_s']) for x in prev_pack['shots'][:prev_shot['index']+1])
                    j=_task(s,run,all_jobs,state,'handoff:'+pred,'extract_frame','vid:'+prev_key,'handoff:'+pred,
                        {'url':clips[prev_key]['url'],'time_s':max(0,elapsed-1/config['fps']),'source_job':clips[prev_key]['job_id']})
                    im=j.result['images'][0]
                    refs.append({'id':'continuity:'+pred,'name':'Previous shot closing state','url':im['reference_url'],'media_id':im.get('media_id'),
                        'description':'Closing state of the preceding generated shot; use for pose/prop continuity only. Locked source facts and identity sheets take precedence; do not copy its camera angle or propagate contradictions.'})
            atlas_receipts=[];atlas_waiting=False
            packing=reference_atlas.plan(p['materials'],len({r['url'] for r in refs}))
            for page in packing['pages']:
                try:
                    atlas_job=_task(s,run,all_jobs,state,'atlas:'+page['version'],'atlas','atlas:'+page['version'],'',page)
                    atlas_receipts.append({'job_id':str(atlas_job.id),'result':atlas_job.result})
                except Waiting:atlas_waiting=True
            if atlas_waiting:raise Waiting()
            body=writing_body(board,project.id,p,refs,atlas_receipts)
            # Validate all deterministic inputs before queuing paid text or video.
            jobs.validate_package(board,str(project.id),body)
            j=_task(s,run,all_jobs,state,'write:'+key,'write','vid:'+key,'prompt',body.model_dump(mode='json'),
                    {'base_prompt':next(n['data'].get('prompt','') for n in board['nodes'] if n.get('id')=='vid:'+key)})
            if config['mode']=='prepare':
                completed.add(key);continue
            out=j.result;ordered,issues=prompt_coverage.reference_slots([*body.characters,*([body.environment] if body.environment else []),*body.reference_assets])
            if issues:raise Blocked('; '.join(issues))
            if board.get('kyc') and any(not r.get('media_id') for r in ordered): raise Blocked('Missing media ID for KYC reference.')
            clip=ClipBody(prompt=out['prompt'],duration_seconds=out['duration_seconds'],reference_urls=[r['ref_url'] for r in ordered],
                kyc_media_ids=[r['media_id'] for r in ordered] if board.get('kyc') else [],unmoderated=board.get('unmoderated',True),
                aspect_ratio=p['aspect_ratio'],resolution=config['resolution'],project_id=project.id,sequence_key=key,
                prompt_contract=body,coverage=out.get('coverage'),contract_digest=out.get('contract_digest',''),coverage_token=out.get('coverage_token',''))
            j=_task(s,run,all_jobs,state,'clip:'+key,'clip','vid:'+key,'',clip.model_dump(mode='json'))
            clips[key]={'url':j.result['url'],'job_id':str(j.id),'duration_s':sum(float(sh['duration_s']) for sh in p['shots']),'sequence_key':key}
        except Waiting: continue
        except (Blocked,ValueError) as exc: errors.append(key+': '+str(exc))
    if errors: state['errors']=errors;return state,'blocked'
    if len(completed if config['mode']=='prepare' else clips)!=len(packs):return state,'running'
    if config['mode']=='render' and config['assemble']:
        state['stage']='assembly'
        try:
            j=_task(s,run,all_jobs,state,'assembly','assemble','production:assembly','',{'clips':[clips[p['sequence_key']] for p in packs],
                'fps':config['fps'],'aspect_ratio':board.get('aspectRatio','16:9'),'resolution':config['resolution']})
        except Waiting:return state,'running'
        state['output']=j.result
    state['stage']='complete';return state,'succeeded'


def tick(run_id):
    with get_session() as s:
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
