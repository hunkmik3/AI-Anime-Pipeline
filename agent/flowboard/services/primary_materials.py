"""Primary identity/location sheets: user checkpoint before derived production."""
from sqlmodel import select
from flowboard.db.models import AutomationJob
from flowboard.services import automation_jobs as jobs, production_run as production


def waiting_run(session, pid):
    return next((j for j in session.exec(select(AutomationJob).where(AutomationJob.project_id == pid,
        AutomationJob.kind == production.KIND, AutomationJob.status == 'paused')).all()
        if j.result.get('stage') == 'master_review'), None)


def items(board, pid, keys=None):
    required = None
    try:
        specs = production.material_specs(board, production.selected_packages(board, pid, keys or []))
        required = {s['node_id'] for s in specs.values() if s['kind'] in ('character', 'environment')}
    except ValueError:
        pass  # Inventory still works on boards whose shotlist isn't ready yet.
    result = []
    for n in board.get('nodes', []):
        d = n.get('data', {}); kind = d.get('kind')
        if kind not in ('character', 'environment'): continue
        definition = d[kind]; slot = 'identity' if kind == 'character' else 'plate'
        aid = definition.get('source_asset_id') or definition.get('id') or definition.get('key')
        clips = []
        for seq in board.get('nodes', []):
            sd = seq.get('data', {})
            if sd.get('kind') != 'sequence': continue
            shots = sd.get('shots', [])
            if aid in sd.get('sequence', {}).get('character_keys', []) or aid == sd.get('sequence', {}).get('environment_key') or any(aid in sh.get('character_keys', []) or aid == sh.get('environment_key') or
                any(p.get('asset_id') == aid for p in sh.get('asset_presence', [])) for sh in shots):
                clips.append(sd.get('sequence', {}).get('label') or sd.get('sequence', {}).get('key'))
        result.append({'node_id': n['id'], 'kind': kind, 'slot': slot, 'profile': definition,
            'plate': d.get(slot, {}), 'states': [{'key': st['key'], 'label': st.get('label') or st['key'],
                'ready': bool(d.get('states', {}).get(st['key'], {}).get('referenceUrl')),
                'stale': bool(d.get('states', {}).get(st['key'], {}).get('needsIdentityRefresh'))}
                for st in definition.get('states', [])] if kind == 'character' else [],
            'clips': clips, 'required': required is None or n['id'] in required})
    return result


def invalidate_identity_states(incoming, stored, all_jobs):
    """Keep old files recoverable, but never silently reuse a previous face's variants."""
    old_nodes = {n['id']: n.get('data', {}) for n in stored.get('nodes', [])}
    for n in incoming.get('nodes', []):
        d = n.get('data', {})
        if d.get('kind') != 'character': continue
        old = old_nodes.get(n['id'], {}); url = d.get('identity', {}).get('referenceUrl')
        if not url or url == old.get('identity', {}).get('referenceUrl'): continue
        for slot, plate in d.get('states', {}).items():
            if plate.get('uploaded'): continue
            related = [j for j in all_jobs if j.kind == 'plate' and j.node_id == n['id'] and j.slot == slot]
            if not plate.get('referenceUrl') and not related: continue
            completed = [j for j in all_jobs if j.kind == 'plate' and j.node_id == n['id'] and j.slot == slot
                         and j.status == 'succeeded' and str(j.id) not in plate.get('ignoredRuntimeJobIds', [])]
            latest = max(completed, key=lambda j: j.created_at) if completed else None
            if latest and url in latest.payload.get('reference_urls', []): continue
            plate['needsIdentityRefresh'] = True
            plate['ignoredRuntimeJobIds'] = sorted(set(plate.get('ignoredRuntimeJobIds', [])) |
                {str(j.id) for j in all_jobs if j.kind == 'plate' and j.node_id == n['id'] and j.slot == slot})
    return incoming


def approve(session, project, run, expected_revision):
    if project.revision != expected_revision: raise ValueError('Board đã đổi. Lưu/tải lại trước khi tiếp tục.')
    if run.status == 'running' and run.result.get('master_review_approved'):
        return jobs.public(run)  # Idempotent response after a lost HTTP reply.
    if run.status != 'paused' or run.result.get('stage') != 'master_review':
        raise ValueError('Lượt này không chờ chốt tạo hình chính.')
    all_jobs = session.exec(select(AutomationJob).where(AutomationJob.project_id == project.id)).all()
    if any(j.id != run.id and j.status in jobs.ACTIVE | {'unknown'} for j in all_jobs):
        raise ValueError('Chờ các tác vụ đang chạy hoặc cần đối soát hoàn tất trước khi chốt sheet.')
    board = jobs.project_board(session, project)
    report = production.preview(board, project.id, run.payload['config'])
    if not report['ready']: raise ValueError('Shotlist cần xử lý trước: ' + str(report['issues']))
    primary = items(board, project.id, run.payload['config'].get('sequence_keys'))
    missing = [i['profile'].get('name', i['node_id']) for i in primary if i['required'] and not i['plate'].get('referenceUrl')]
    if missing: raise ValueError('Upload hoặc gen sheet chính trước: ' + ', '.join(missing))
    run.payload = {**run.payload, 'input_version': production.input_version(board)}
    run.result = {**run.result, 'stage': 'materials', 'errors': [], 'master_review_approved': True, 'progress_plan': report['progress_plan'],
        'approved_master_refs': {i['node_id']: i['plate'].get('referenceUrl') for i in primary if i['required']}}
    run.status = 'running'; run.error = ''; run.updated_at = jobs.now(); session.add(run); session.commit(); session.refresh(run)
    return jobs.public(run)
