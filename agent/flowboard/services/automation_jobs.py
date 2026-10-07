"""Durable per-project jobs with leases; never retry an ambiguous paid submission."""
from __future__ import annotations
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import logging
import time
import os
import uuid
from sqlalchemy import or_, update
from sqlmodel import select
from flowboard.db import get_session
from flowboard.db.models import AutomationJob, AutomationProject, VideoAnalysis
from flowboard.services.production_manifest import digest

log=logging.getLogger(__name__)
ACTIVE={'queued','preparing','submitting','running'}
TERMINAL={'succeeded','failed','unknown','cancelled'}
LEASE_SECONDS=90

def now(): return datetime.now(timezone.utc)
def public(j):
    out = {k:str(getattr(j,k)) if k in ('id','project_id') and getattr(j,k) is not None else getattr(j,k) for k in
            ('id','project_id','kind','node_id','slot','status','provider_job_id','result','error','attempts','created_at','updated_at')}
    if j.kind == 'plate': out['reference_urls'] = j.payload.get('reference_urls', [])
    return out

def enqueue(project_id,kind,node_id,slot,payload,request_key,expected_revision=None,metadata=None):
    with get_session() as s:
        # Serializes duplicate/new-take requests for the same board across workers.
        project=s.exec(select(AutomationProject).where(AutomationProject.id==project_id).with_for_update()).one()
        if expected_revision is not None and project.revision != expected_revision:
            raise ValueError("Board changed while submitting. Save/reload before generation.")
        # Enforce the checkpoint under the same project lock as job creation.
        from flowboard.services.primary_materials import waiting_run
        if waiting_run(s, project_id):
            node = next((n.get('data', {}) for n in (project.board or {}).get('nodes', []) if n.get('id') == node_id), {})
            if not (kind == 'plate' and ((node.get('kind') == 'character' and slot == 'identity') or
                    (node.get('kind') == 'environment' and slot == 'plate'))):
                raise ValueError('Chốt sheet ở tab Tạo hình chính trước khi gen diện mạo phụ hoặc video.')
        existing=s.exec(select(AutomationJob).where(AutomationJob.project_id==project_id,AutomationJob.request_key==request_key)).first()
        if existing:
            if existing.kind!=kind or existing.node_id!=node_id or existing.slot!=slot or digest(existing.payload)!=digest(payload):
                raise ValueError('Idempotency key was already used for different inputs.')
            return public(existing)
        busy=s.exec(select(AutomationJob).where(AutomationJob.project_id==project_id,AutomationJob.node_id==node_id,
                    AutomationJob.slot==slot,AutomationJob.kind==kind,AutomationJob.status.in_(ACTIVE|{'unknown'}))).first()
        if busy:
            if digest(busy.payload)==digest(payload):return public(busy)
            raise ValueError('This target has an active or unresolved job. Reconcile it before another submission.')
        j=AutomationJob(project_id=project_id,kind=kind,node_id=node_id,slot=slot,payload=payload,request_key=request_key,prepared=metadata or {})
        s.add(j);s.commit();s.refresh(j);return public(j)

def overlay(board, jobs):
    """Server-owned results win over stale browser statuses, without replacing edits."""
    original_board = board
    board=deepcopy(board);nodes={n['id']:n.get('data',{}) for n in board.get('nodes',[])}
    latest={}
    for j in sorted(jobs,key=lambda x:x.created_at):
        # Retain an obsolete text attempt in history without replacing the
        # reviewed draft actually used by a submitted generation.
        if j.kind=='write' and j.prepared.get('superseded_by_generation'):
            continue
        latest[(j.node_id,j.kind,j.slot)]=j
    for j in latest.values():
        d=nodes.get(j.node_id)
        if d is None:continue
        if j.kind=='write':
            target_seq=nodes.get('seq:'+str(j.payload.get('sequence',{}).get('key','')), {})
            same_shots=target_seq.get('shots',[])==j.payload.get('shots',[])
            same_draft=d.get('prompt','') in (j.prepared.get('base_prompt',''),j.result.get('prompt'))
            d['writerJobId']=str(j.id);d['writerJobStatus']=j.status
            if j.status=='succeeded' and same_shots and same_draft and (d.get('promptBy')!='manual' or j.result.get('writer')=='manual'):
                out=j.result
                d.update(prompt=out.get('prompt'),durationS=out.get('duration_seconds'),endState=out.get('end_state'),
                    promptEngine=out.get('engine'),stagingDecisions=out.get('staging_decisions'),inspectedReferences=out.get('reference_images'),
                    promptBy=out.get('writer'),coverage=out.get('coverage'),contractDigest=out.get('contract_digest'),
                    coverageToken=out.get('coverage_token'),inputFingerprint=out.get('input_fingerprint'))
                d.pop('error',None)
                d.pop('writerError',None)
            elif j.status=='succeeded':d['writerError']='Completed prompt retained in job history; current draft/shotlist changed.'
            elif j.error:d['writerError']=j.error
            continue
        if j.kind=='plate' and j.slot.startswith('shotframe:'):
            target=d.setdefault('shotFrames',{}).setdefault(j.slot,{})
        elif j.kind=='plate':
            target=d.setdefault('states',{}).setdefault(j.slot,{}) if d.get('kind')=='character' and j.slot!='identity' else d.setdefault(j.slot,{})
        elif j.kind=='clip':target=d
        else:continue
        if str(j.id) in target.get('ignoredRuntimeJobIds', []):
            continue  # A user-supplied image supersedes these earlier jobs.
        target['runtimeJobId']=str(j.id);target['runtimeStatus']=j.status
        target['status']='running' if j.status in ACTIVE else 'done' if j.status=='succeeded' else 'error'
        if j.status=='succeeded':
            if j.kind=='clip':target.update(clipUrl=j.result.get('url'),persisted=j.result.get('persisted'),jobId=j.provider_job_id,warnings=j.result.get('warnings',[]))
            else:
                image=(j.result.get('images') or [{}])[0]
                if j.prepared.get('material_signature') and (not target.get('prompt') or target.get('prompt')==target.get('runtimePrompt')):
                    target.update(prompt=j.payload.get('prompt',''),runtimePrompt=j.payload.get('prompt',''),baseMaterialPrompt=j.prepared.get('base_material_prompt',''))
                target.update(image=image.get('url'),referenceUrl=image.get('reference_url'),mediaId=image.get('media_id'),uploaded=False,needsIdentityRefresh=False)
                if j.result.get('shot_frame'): target.update(j.result['shot_frame'])
            target.pop('error',None)
        elif j.error: target['error']=j.error
        else:target.pop('error',None)
    for j in jobs:
        if j.kind=='ingest' and j.status=='succeeded':
            for d in nodes.values():
                plates=[d.get('identity',{}),d.get('plate',{}),*d.get('states',{}).values()]
                for plate in plates:
                    media=j.result.get('media_ids',{}).get(plate.get('referenceUrl'))
                    if media: plate['mediaId']=media
    from flowboard.services import raccord
    from flowboard.services.primary_materials import invalidate_identity_states
    board = invalidate_identity_states(board, original_board, jobs)
    return raccord.attach(board, jobs)

def project_board(s,project):
    jobs=s.exec(select(AutomationJob).where(AutomationJob.project_id==project.id)).all()
    return overlay(project.board or {},jobs)

def checkpoint(jid,token,**values):
    with get_session() as s:
        changed=s.exec(update(AutomationJob).where(AutomationJob.id==jid,AutomationJob.lease_token==token)
                       .values(**values,updated_at=now(),lease_until=now()+timedelta(seconds=LEASE_SECONDS)))
        s.commit()
        if not changed.rowcount:raise RuntimeError('Job lease lost')

def claim():
    with get_session() as s:
        j=s.exec(select(AutomationJob).where(AutomationJob.status.in_(ACTIVE), AutomationJob.kind!='production_run',
              or_(AutomationJob.lease_until==None,AutomationJob.lease_until<now()))
              .order_by(AutomationJob.created_at).with_for_update(skip_locked=True)).first()
        if j is None:return None
        # A submit may have been accepted upstream. Never blindly submit it again.
        if j.status=='submitting' and not j.provider_job_id:
            j.status='unknown';j.error='Provider submission outcome is unknown. Reconcile the provider job before retrying.'
            j.updated_at=now();s.add(j);s.commit();return None
        token=str(uuid.uuid4())
        changed=s.exec(update(AutomationJob).where(AutomationJob.id==j.id,
            AutomationJob.lease_token==j.lease_token, AutomationJob.status==j.status,
            or_(AutomationJob.lease_until==None,AutomationJob.lease_until<now())).values(
                lease_token=token,lease_until=now()+timedelta(seconds=LEASE_SECONDS),
                status='preparing' if j.status=='queued' else j.status, attempts=j.attempts+1))
        if not changed.rowcount:s.rollback();return None
        s.commit();s.refresh(j)
        return j.model_dump()


async def heartbeat(jid,token):
    while True:
        await asyncio.sleep(20)
        await asyncio.to_thread(checkpoint,jid,token)

async def execute(data):
    from flowboard.services import automation
    from flowboard.services.video import registry
    from flowboard.routes.automation import ClipBody,PlateBody,_validate_generation_contract
    jid,token=data['id'],data['lease_token'];provider_id=data['provider_job_id'];submitted=data.get('prepared') or {}
    current_task=asyncio.current_task()
    beat=asyncio.create_task(heartbeat(jid,token))
    def heartbeat_finished(task):
        if not task.cancelled() and task.exception() is not None:
            log.error('Automation lease heartbeat lost for %s: %s', jid, task.exception())
            if current_task is not None: current_task.cancel()
    beat.add_done_callback(heartbeat_finished)
    submitting=False
    try:
        if not provider_id:
            from flowboard.services.production_run import validate_run_input
            try:
                validate_run_input(data['project_id'], submitted)
            except ValueError as exc:
                await asyncio.to_thread(checkpoint,jid,token,status='failed',error=str(exc),prepared={**submitted,'validation_rejected':True})
                return
        if data['kind']=='write':
            from flowboard.routes.automation import VideoWriteBody,write_video_prompt,VerifyPromptBody,verify_video_prompt,VideoWriteResponse
            body=VideoWriteBody.model_validate(data['payload'])
            validate_writing(data['project_id'],body)
            if 'provided_prompt' in data['payload']:
                draft=data['payload']['provided_prompt']
                from flowboard.services import prompt_coverage, prompt_writer
                provided=VerifyPromptBody(prompt_contract=body, prompt=draft,
                    end_state=data['payload'].get('provided_end_state',''),
                    staging_decisions=data['payload'].get('provided_staging_decisions',[]))
                if prompt_coverage.is_strict(body.production_assets,body.source_verification):
                    result=(await verify_video_prompt(provided)).model_dump(mode='json')
                else:
                    duration=prompt_writer.production_timeline(body.sequence,body.shots)[2]
                    result=VideoWriteResponse(prompt=draft,duration_seconds=duration,end_state=provided.end_state).model_dump(mode='json')
                if result['prompt']!=draft: raise ValueError('Verification changed the manual prompt; draft preserved.')
                result['writer']='manual'
                result['staging_decisions']=provided.staging_decisions
            else:
                result=(await write_video_prompt(body)).model_dump(mode='json')
            result['input_fingerprint']=submitted.get('input_fingerprint','')
            result['base_prompt']=submitted.get('base_prompt','')
            result['source_shots']=body.shots
        elif data['kind']=='atlas':
            from flowboard.services import reference_atlas
            result=await reference_atlas.generate(data['payload'])
        elif data['kind']=='raccord':
            from flowboard.services import raccord
            result=await raccord.plan(data['payload'])
        elif data['kind'] in ('extract_frame','assemble'):
            from flowboard.services import production_media
            result=await getattr(production_media,data['kind'])(data['payload'])
        elif data['kind']=='ingest':
            media=await automation.ingest_published(data['payload']['urls'])
            if any(not value for value in media.values()): raise ValueError('Could not ingest all KYC references')
            result={'media_ids':media}
        elif data['kind']=='source':
            result=await execute_source(data['payload'])
        elif data['kind']=='clip':
            body=ClipBody.model_validate(data['payload'])
            registry.register_defaults();provider=registry.get_video_provider(automation.VIDEO_MODEL_ID)
            if not provider_id:
                _validate_generation_contract(body)
                validate_current_clip(data['project_id'],body)
                params=await automation.prepare_clip(**body.model_dump(exclude={'prompt_contract','coverage','contract_digest','coverage_token','sequence_key'}))
                await asyncio.to_thread(checkpoint,jid,token,status='submitting');submitting=True
                submitted=await provider.submit(params)
                provider_id=submitted['external_job_id']
                await asyncio.to_thread(checkpoint,jid,token,status='running',provider_job_id=provider_id,prepared=submitted)
                submitting=False
            # Poll/download/publish can safely resume with the same upstream ID.
            failures=0; polling_started=time.monotonic()
            while True:
                if time.monotonic()-polling_started>7200:
                    raise RuntimeError('Polling deadline reached; keep provider ID for reconciliation.')
                try:polled=await provider.poll(provider_id);failures=0
                except Exception as exc:
                    failures+=1
                    if failures>=5:raise RuntimeError('Provider polling paused: '+str(exc)[:250]) from exc
                    await asyncio.sleep(5);continue
                if polled.get('status')=='failed':
                    await asyncio.to_thread(checkpoint,jid,token,status='failed',error=str(polled.get('error_message') or polled.get('error') or 'Provider failed'));return
                if polled.get('status')=='succeeded':break
                await asyncio.sleep(5)
            result=await automation.publish_clip(submitted,polled)
        else:
            body=PlateBody.model_validate(data['payload'])
            if submitted.get('shot_frame'):
                validate_shot_frame(data['project_id'], submitted)
            await asyncio.to_thread(checkpoint,jid,token,status='submitting');submitting=True
            images=await automation.generate_plate(**body.model_dump())
            result={'images':images, 'shot_frame': submitted.get('shot_frame')};submitting=False
        # Job result is authoritative even if a tab has an old board in memory.
        await asyncio.to_thread(checkpoint,jid,token,status='succeeded',result=result,error='')
    except asyncio.CancelledError:
        # Lease expiry handles recovery; a submit without its receipt stays unknown.
        raise
    except Exception as exc:
        definite_rejection = getattr(exc, 'code', '') in {'bad_input','auth','quota','content_filtered'}
        status=('running' if provider_id and data['attempts'] < 5 else 'unknown') if submitting or provider_id else 'failed'
        if definite_rejection and not provider_id: status='failed'
        await asyncio.to_thread(checkpoint,jid,token,status=status,error=str(exc)[:1200])
    finally:
        beat.cancel();await asyncio.gather(beat,return_exceptions=True)

async def worker():
    concurrency=max(1,min(64,int(os.getenv('FLOWBOARD_AUTOMATION_CONCURRENCY','8'))));tasks=set()
    try:
        while True:
            for task in list(tasks):
                if task.done():
                    tasks.remove(task)
                    if not task.cancelled() and task.exception() is not None:
                        log.error('Automation job interrupted: %s', task.exception())
            if len(tasks)<concurrency:
                try:item=await asyncio.to_thread(claim)
                except Exception:
                    log.exception('Automation queue unavailable');item=None
                if item:
                    task=asyncio.create_task(execute(item));tasks.add(task);continue
            await asyncio.sleep(1)
    finally:
        for t in tasks:t.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)


def validate_current_clip(project_id, body):
    from flowboard.services import production_manifest
    with get_session() as s:
        project=s.get(AutomationProject,project_id)
        if not project:raise ValueError('Project no longer exists')
        board=project_board(s,project)
        seq=next((n['data'] for n in board.get('nodes',[]) if n.get('id')=='seq:'+body.sequence_key),None)
        if not seq:raise ValueError('Shotlist no longer exists')
        if body.prompt_contract:
            if body.prompt_contract.shots!=seq.get('shots',[]):raise ValueError('Shotlist changed after queuing')
            supplied=body.prompt_contract.sequence.get('production_context')
            current=production_manifest.context(production_manifest.build(board,str(project_id)),body.sequence_key)
            if supplied and supplied.get('version')!=current['version']:raise ValueError('Scene state or asset version changed after queuing')
            validate_package(board, str(project_id), body.prompt_contract)


def source_running(video_id):
    with get_session() as s:
        return s.exec(select(AutomationJob.id).where(AutomationJob.node_id=='source:'+str(video_id),
               AutomationJob.kind=='source',AutomationJob.status.in_(ACTIVE))).first() is not None


def enqueue_source(video_id,operation):
    with get_session() as s:
        row=s.exec(select(VideoAnalysis).where(VideoAnalysis.id==video_id).with_for_update()).one()
        existing=s.exec(select(AutomationJob).where(AutomationJob.node_id=='source:'+str(video_id),
                    AutomationJob.kind=='source',AutomationJob.status.in_(ACTIVE))).first()
        if existing:raise ValueError('Video này đang được xử lý trên server.')
        job=AutomationJob(project_id=row.automation_project_id,kind='source',node_id='source:'+str(video_id),
            request_key=str(uuid.uuid4()),payload={'video_id':str(video_id),'operation':operation})
        row.status='queued';row.error=None
        s.add(row);s.add(job);s.commit();return public(job)


async def execute_source(payload):
    from flowboard.routes import video_analysis as routes
    video_id=uuid.UUID(payload['video_id']);op=payload['operation'];name=op['task']
    if name=='film':
        from flowboard.services.source_film import execute
        await execute(video_id)
    elif name=='analyze':await routes._run_analysis(video_id,op.get('then_adapt'))
    elif name=='adapt':await routes._run_adaptation(video_id,op.get('rules',{}),op.get('glossary'),fresh=op.get('fresh',False))
    elif name=='cast':await routes._run_cast(video_id)
    elif name=='design':await routes._run_design(video_id,op.get('keys',[]),op.get('world',''))
    elif name=='conform':await routes._run_conform(video_id,set(op.get('shots',[])) or None)
    elif name=='verify':await routes._run_source_verification(video_id)
    elif name=='refine':await routes._run_source_refinement(video_id,protocol_only=op.get('protocol_only',False),selected_shots=op.get('shots'),strategy=op.get('strategy','full'))
    else:raise ValueError('Unknown source operation')
    with get_session() as s:
        row=s.get(VideoAnalysis,video_id)
        if not row or row.status=='failed':raise RuntimeError(row.error if row else 'Source deleted')
        return {'video_id':str(video_id),'status':row.status}


def validate_writing(project_id,body):
    from flowboard.services import production_manifest
    with get_session() as s:
        project=s.get(AutomationProject,project_id)
        if not project:raise ValueError('Project no longer exists')
        board=project_board(s,project);key=body.sequence.get('key','')
        seq=next((n['data'] for n in board.get('nodes',[]) if n.get('id')=='seq:'+key),None)
        if not seq or body.shots!=seq.get('shots',[]):raise ValueError('Shotlist changed after queuing the writer')
        current=production_manifest.context(production_manifest.build(board,str(project_id)),key)
        if any(i.get('blocking') for i in current['issues']):raise ValueError('Resolve duplicate, merged or unknown shot/assets before writing')
        supplied=body.sequence.get('production_context')
        if supplied and supplied.get('version')!=current['version']:raise ValueError('Scene state or material version changed before writing')
        validate_package(board, str(project_id), body)


def validate_package(board, project_id, body):
    from flowboard.services import shot_package
    supplied = body.sequence.get('shot_package')
    if not supplied:
        return  # Old boards stay compatible until explicitly prepared.
    package = shot_package.build(board, project_id, body.sequence.get('key', ''))
    if supplied != package:
        raise ValueError('Shot preparation changed; refresh the package and rewrite the prompt.')
    references = [*body.characters, *([body.environment] if body.environment else []), *body.reference_assets]
    if body.sequence.get('reference_atlases'):
        from flowboard.services import reference_atlas
        references=reference_atlas.verify(project_id,package,body.sequence['reference_atlases'],references)
    errors = shot_package.check_bindings(package, references)
    if errors:
        raise ValueError('; '.join(errors))


def validate_shot_frame(project_id, metadata):
    from flowboard.services import shot_package
    with get_session() as s:
        project = s.get(AutomationProject, project_id)
        if project is None: raise ValueError('Project no longer exists')
        package = shot_package.build(project_board(s, project), str(project_id), metadata['sequence_key'])
        if package['version'] != metadata['shot_frame']['package_version']:
            raise ValueError('Shot or material changed after queuing the frame; prepare it again.')
