"""Read-only progress from persisted counters and scoped production receipts.

Percentages measure completed work units, never provider ETA or simulated time.
"""
from copy import deepcopy
from sqlmodel import select
from flowboard.db.models import AutomationJob, VideoAnalysis
from flowboard.services import automation_jobs as jobs

SOURCE_LABELS = {
    'probe': ('Đọc thông tin video', 'bước'), 'cuts': ('Dò điểm cắt', 'bước'),
    'keyframes': ('Trích keyframe', 'shot'), 'speech': ('Nghe & chép thoại', 'lượt'),
    'vision': ('Đọc hình & tách shotlist', 'shot'), 'joint_vision': ('Đọc hình & danh mục nguồn', 'mục'),
    'story': ('Chia mạch truyện', 'mục'), 'profiles': ('Lập hồ sơ từ shotlist', 'shot'),
    'cast': ('Lập hồ sơ nhân vật & bối cảnh', 'mục'), 'design': ('Viết hồ sơ tạo hình', 'hồ sơ'),
    'adapt': ('Chuẩn bị shotlist', 'shot'), 'glossary': ('Chuẩn hóa tên', 'mục'),
    'validate': ('Kiểm tra đầu vào', 'lượt'), 'inventory': ('Lập danh mục nguồn', 'shot'),
    'source_verify': ('Đối chiếu nguồn', 'shot'), 'source_refine': ('Sửa mục nguồn có lỗi', 'shot'),
    'source_identity': ('Đối chiếu danh tính', 'mục'), 'source_layers': ('Đọc lớp hình ảnh', 'mục'),
    'source_context': ('Đối chiếu ngữ cảnh', 'mục'), 'source_protocol_review': ('Kiểm tra kết luận', 'mục'),
}
PHASE_LABELS = {'analysis':'Phân tích video gốc', 'profiles':'Lập hồ sơ nhân vật & bối cảnh',
    'adaptation':'Chuẩn bị shotlist', 'casting':'Áp dụng tạo hình tùy chỉnh', 'design':'Viết hồ sơ tạo hình',
    'board':'Tạo board & liên kết material', 'source_refinement':'Xử lý dữ liệu nguồn'}


def record_source(video_id, stage, done, total):
    from flowboard.db import get_session
    stamp = jobs.now().isoformat()
    with get_session() as s:
        row = s.exec(select(VideoAnalysis).where(VideoAnalysis.id == video_id).with_for_update()).first()
        if not row: return
        progress = deepcopy(row.progress or {}); history = progress.setdefault('steps', {})
        old = history.get(stage, {})
        restarted = done == 0 and old.get('done', 0) > 0
        history[stage] = {'done': max(0, done), 'total': max(0, total),
            'started_at': stamp if restarted else old.get('started_at', stamp), 'updated_at': stamp}
        progress.update(stage=stage, done=done, total=total)
        row.progress = progress; row.updated_at = jobs.now(); s.add(row); s.commit()


def make_plan(specs, packs):
    return {'materials': [{'key': key, 'node': sp['node_id'], 'slot': sp['slot'],
        'name': sp['item'].get('name', sp['asset_id']),
        'group': 'masters' if sp['kind'] == 'environment' or sp['kind'] == 'character' and sp['slot'] == 'identity' else 'variants'}
        for key, sp in specs.items()], 'clips': [p['sequence_key'] for p in packs]}


def stage(key, label, done, total, status='waiting', unit='mục', **extra):
    total = max(0, int(total)) if total is not None else None
    done = max(0, min(int(done), total)) if total is not None else max(0, int(done))
    if status not in {'failed','blocked','unknown','paused','waiting_user','skipped'} and total is not None and total > 0 and done == total:
        status = 'succeeded'
    # A single opaque provider call has no intermediate percentage.
    pct = round(done / total * 100) if total and not (total == 1 and done == 0 and status in {'running','queued'}) else None
    return {'key': key, 'label': label, 'done': done, 'total': total, 'percent': pct,
        'status': status, 'unit': unit, **extra}


def source_stages(row, active_source=False):
    if not row: return []
    out = []; progress = row.progress or {}; cfg = (row.options or {}).get('auto_production') or {}
    history = progress.get('steps', {})
    if not history and progress.get('stage'):
        history = {progress['stage']: progress}
    for key, entry in history.items():
        done, total = entry.get('done', 0), entry.get('total')
        complete = total is not None and total > 0 and done >= total
        status = 'succeeded' if complete else 'running' if active_source else 'paused'
        if row.status == 'failed' and not complete: status = 'failed'
        if key == 'speech' and (row.analysis or {}).get('speech_error'): status = 'failed'; done = 0
        label, unit = SOURCE_LABELS.get(key, ('Phân tích nguồn: ' + key, 'mục'))
        out.append(stage('source:'+key, label, done, total, status, unit,
            started_at=entry.get('started_at'), updated_at=entry.get('updated_at'), error=((row.analysis or {}).get('speech_error') or row.error or '') if key == 'speech' and status == 'failed' else (row.error or '') if status == 'failed' else ''))
    phases = cfg.get('phase_history', {})
    for key, entry in phases.items():
        if key not in PHASE_LABELS: continue
        # Detailed counters replace the broad stage when available.
        detailed = {'analysis': ['probe','cuts','speech','keyframes','vision','joint_vision','story'],
            'adaptation':['adapt'], 'profiles':['profiles','cast'], 'design':['design']}.get(key, [])
        if any(k in history for k in detailed): continue
        status = entry.get('status', 'running')
        if status == 'running' and not active_source: status = 'paused'
        out.append(stage('source-phase:'+key, PHASE_LABELS[key], int(status == 'succeeded'), 1, status, 'bước', started_at=entry.get('started_at'), updated_at=entry.get('updated_at')))
    if not out:
        shots = len((row.analysis or {}).get('shots', []))
        done = bool(shots and row.status not in {'queued','analysing','failed'})
        out.append(stage('source:analysis', 'Phân tích video gốc', shots if done else 0, shots or None,
            'succeeded' if done else 'failed' if row.status == 'failed' else 'running' if active_source else 'waiting', 'shot', error=row.error or ''))
    if cfg.get('board_ready') and not any(r['key'] == 'source-phase:board' for r in out):
        out.append(stage('source-phase:board', 'Tạo board & liên kết material', 1, 1))
    # JSONB does not retain dict insertion order. Use persisted start times,
    # falling back to the normal pipeline order for older records.
    order = ['probe','cuts','keyframes','speech','vision','joint_vision','story','inventory','source_verify','source_refine','source_identity','source_layers','source_context','source_protocol_review','analysis','profiles','cast','glossary','adapt','validate','adaptation','casting','design','board']
    rank = {key:i for i,key in enumerate(order)}
    out.sort(key=lambda r: (r.get('started_at') or '', rank.get(r['key'].split(':',1)[-1], len(rank))))
    return out


def run_stages(run, board, all_jobs):
    from flowboard.services import production_run as production
    cfg = run.payload.get('config', {}); result = run.result or {}; plan = result.get('progress_plan')
    if not plan:
        # Compatibility for runs created before progress instrumentation. No mutation or provider calls.
        try:
            if production.input_version(board) != run.payload.get('input_version'): raise ValueError('Inputs changed')
            packs = production.selected_packages(board, run.project_id, cfg.get('sequence_keys', []))
            plan = make_plan(production.material_specs(board, packs), packs)
        except ValueError:
            plan = {'materials': [], 'clips': result.get('preview', {}).get('clips', [])}
    by_id = {str(j.id): j for j in all_jobs}
    tasks = {k: {**t, 'status': by_id[t['id']].status if t.get('id') in by_id else t.get('status'),
        'error': by_id[t['id']].error if t.get('id') in by_id else ''} for k,t in result.get('tasks', {}).items()}
    nodes = {n['id']: n.get('data', {}) for n in board.get('nodes', [])}
    phase = result.get('stage'); finished = run.status == 'succeeded'; ready = set(result.get('ready_materials', []))
    passed_materials = phase in {'raccord','prompts','videos','assembly','complete'}
    out = []
    def status_for(records, started=False):
        statuses = {r.get('status') for r in records}
        if 'unknown' in statuses: return 'unknown'
        if statuses & {'failed','cancelled'}: return 'failed'
        if statuses & jobs.ACTIVE: return 'running'  # Already dispatched work continues after pause.
        if run.status in {'paused','blocked'} and started: return run.status
        return 'running' if started and run.status == 'running' else 'waiting'
    for group, label in [('masters','Sheet nhân vật & bối cảnh chính'),('variants','Diện mạo phụ & đạo cụ')]:
        specs = [x for x in plan['materials'] if x['group'] == group]; count = 0; recs = []
        for item in specs:
            task = tasks.get('material:'+item['key'])
            if group == 'masters' and phase == 'master_review':
                # Primary sheets can be uploaded or queued separately while the controller is paused.
                data = nodes.get(item['node'], {}); plate = data.get(item['slot'], {})
                matching = [j for j in all_jobs if j.kind == 'plate' and j.node_id == item['node'] and j.slot == item['slot']
                    and str(j.id) not in plate.get('ignoredRuntimeJobIds', [])]
                task = {'status': matching[-1].status, 'error': matching[-1].error} if matching and matching[-1].status in jobs.ACTIVE | {'unknown','failed'} else None
                complete = bool(plate.get('referenceUrl')) and not task
            else:
                complete = finished or passed_materials or item['key'] in ready or bool(task and task['status'] == 'succeeded')
            count += bool(complete)
            if task: recs.append(task)
        status = status_for(recs, phase == 'materials')
        if phase == 'master_review' and group == 'masters' and not recs: status = 'waiting' if count == len(specs) else 'waiting_user'
        if group == 'variants' and phase == 'master_review': status = 'waiting'
        if not specs: status = 'skipped'
        out.append(stage(group, label, count, len(specs), status, 'sheet',
            failed=sum(r.get('status') in {'failed','unknown','cancelled'} for r in recs)))
    if cfg.get('review_masters'):
        approved = result.get('master_review_approved', False)
        out.insert(1, stage('approval', 'Chốt tạo hình chính', int(approved), 1, 'succeeded' if approved else 'waiting_user', 'bước'))
    scopes = result.get('raccord_scopes')
    raccord_tasks = [v for k,v in tasks.items() if k.startswith('raccord:')]
    raccord_done = sum(t['status'] == 'succeeded' for t in raccord_tasks)
    raccord_total = len(scopes) if scopes is not None else (len(raccord_tasks) if finished else None)
    out.append(stage('raccord', 'Liên tục cảnh / raccord', raccord_total if finished and raccord_total is not None else raccord_done,
        raccord_total, 'skipped' if raccord_total == 0 else status_for(raccord_tasks, phase == 'raccord'), 'cảnh'))
    keys = plan['clips']
    for prefix, label in [('write','Viết prompt video'),('clip','Gen video')]:
        if prefix == 'clip' and cfg.get('mode') != 'render': continue
        recs = [tasks[prefix+':'+key] for key in keys if prefix+':'+key in tasks]
        count = sum(finished or tasks.get(prefix+':'+key, {}).get('status') == 'succeeded' or
            prefix == 'write' and tasks.get('clip:'+key, {}).get('status') == 'succeeded' for key in keys)
        out.append(stage(prefix, label, count, len(keys), status_for(recs, phase in {'prompts','videos'}), 'clip',
            failed=sum(r.get('status') in {'failed','unknown','cancelled'} for r in recs)))
    if cfg.get('mode') == 'render' and cfg.get('assemble'):
        recs = [tasks['assembly']] if 'assembly' in tasks else []
        out.append(stage('assembly','Ghép phim', int(finished or any(r['status']=='succeeded' for r in recs)),1,
            status_for(recs,phase=='assembly'),'phim'))
    support = [v for v in tasks.values() if v.get('kind') in {'ingest','atlas','extract_frame'} or v.get('slot','').startswith('shotframe:')]
    if support:
        out.append(stage('support', 'Chuẩn bị reference / ảnh nối cảnh', sum(t['status']=='succeeded' for t in support), len(support),
            status_for(support), 'tác vụ đã xếp', note='Chỉ tính tác vụ phụ trợ đã được lên lịch; có thể phát sinh thêm theo từng clip.'))
    return out, tasks


def snapshot(session, project):
    all_jobs = session.exec(select(AutomationJob).where(AutomationJob.project_id == project.id).order_by(AutomationJob.created_at)).all()
    runs = [j for j in all_jobs if j.kind == 'production_run']; run = runs[-1] if runs else None
    source = session.exec(select(VideoAnalysis).where(VideoAnalysis.automation_project_id == project.id).order_by(VideoAnalysis.created_at.desc())).first()
    source_active = any(j.kind == 'source' and j.status in jobs.ACTIVE for j in all_jobs)
    if source and not source_active:
        from flowboard.routes.video_analysis import _is_running
        source_active = _is_running(source.id)
    rows = source_stages(source, source_active); tasks = {}
    if run:
        board = jobs.overlay(project.board or {}, all_jobs)
        stages, tasks = run_stages(run, board, all_jobs); rows += stages
    # Standalone material/video requests remain visible, independent of controller progress.
    latest = {(j.kind,j.node_id,j.slot): j for j in all_jobs if j.kind not in {'production_run','material_binding'}}
    nodes = {n['id']: n.get('data', {}) for n in (project.board or {}).get('nodes', [])}
    def ignored(j):
        data = nodes.get(j.node_id, {})
        target = data.get('states', {}).get(j.slot, {}) if j.kind == 'plate' and data.get('kind') == 'character' and j.slot != 'identity' else data.get(j.slot, {}) if j.kind == 'plate' else data
        return str(j.id) in target.get('ignoredRuntimeJobIds', []) or (j.kind == 'write' and j.prepared.get('superseded_by_generation'))
    attention = [j for j in latest.values() if j.status in jobs.ACTIVE | {'unknown','failed'} and not ignored(j)]
    node_names = {}
    for n in (project.board or {}).get('nodes', []):
        d=n.get('data', {}); definition=d.get(d.get('kind'), {})
        node_names[n['id']] = definition.get('name') or definition.get('label') or d.get('label') or n['id']
    active = [j for j in attention if j.status in jobs.ACTIVE]
    return {'project_id':str(project.id), 'run_id':str(run.id) if run else None,
        'status':run.status if run else 'running' if source_active or active else source.status if source else 'idle',
        'mode':run.payload.get('config',{}).get('mode') if run else None,
        'stages':rows, 'completed_stages':sum(r['status']=='succeeded' for r in rows),
        'total_stages':sum(r['status']!='skipped' for r in rows),
        'active_jobs':len(active), 'error': run.error if run and run.error else source.error if source and source.error else '',
        'jobs':[{'id':str(j.id),'kind':j.kind,'name':node_names.get(j.node_id,j.node_id),'slot':j.slot,
            'status':j.status,'error':j.error,'created_at':j.created_at.isoformat()} for j in attention[-30:]],
        'updated_at':jobs.now().isoformat()}
