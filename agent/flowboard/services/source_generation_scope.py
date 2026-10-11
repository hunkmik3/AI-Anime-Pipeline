"""Restrict production without cropping or discarding full-film source analysis."""
from copy import deepcopy
import math
import uuid

from sqlmodel import select

from flowboard.db import get_session
from flowboard.db.models import AutomationJob, AutomationProject, VideoAnalysis


def preparation_prefix(analysis, scope):
    """A derived production view; the full source and its findings stay intact.

    Keep full audio/context evidence. Only measured complete shots and their cut
    boundaries enter production; never crop a shot or invent a new cut at the limit.
    This selects work, it does not approve any source observations.
    """
    from flowboard.services.video_analyzer.one_pass_film import source_fingerprint
    if scope.get('analysis') != 'full' or scope.get('source_start_seconds') != 0:
        raise ValueError('Only a production prefix with full source analysis is supported.')
    limit = float(scope['source_end_seconds'])
    if not math.isfinite(limit) or limit <= 0:
        raise ValueError('Invalid production prefix.')
    shots = analysis.get('shots') or []
    selected = []
    cursor = 0.
    for shot in shots:
        start, end = float(shot['start']), float(shot['end'])
        if not math.isfinite(start + end) or end <= start or abs(start-cursor) > .05:
            raise ValueError('Full source has a gap, overlap or invalid shot timing.')
        cursor = end
        if end <= limit + 1e-6:
            selected.append(shot)
    if not shots or abs(cursor-float(analysis['video']['duration'])) > .05:
        raise ValueError('Full source reading is incomplete; retain full-film analysis before production.')
    if not selected:
        raise ValueError('No complete source shot fits within the requested time limit.')
    result = deepcopy(analysis)
    result['shots'] = deepcopy(selected)
    actual_end = float(selected[-1]['end'])
    result['video']['duration'] = actual_end
    if 'kept' in result.get('cuts', {}):
        result['cuts']['kept'] = result['cuts']['kept'][:len(selected)-1]
    result['production_scope'] = {
        **deepcopy(scope), 'actual_source_end_seconds':actual_end,
        'full_source_shots':len(shots), 'full_source_video':deepcopy(analysis['video']),
        'full_source_fingerprint':source_fingerprint(analysis),
        'selected_shots':[s['shot'] for s in selected], 'source_fidelity_approved':False,
    }
    return result


def prefix_clips(board, end_seconds):
    """Whole consecutive clips ending at/before the requested source timestamp."""
    end_seconds = float(end_seconds)
    if not math.isfinite(end_seconds) or end_seconds <= 0:
        raise ValueError('Generation end must be a positive finite source timestamp.')
    clips = []
    for node in board.get('nodes', []):
        data = node.get('data', {})
        if data.get('kind') != 'sequence':
            continue
        shots = data.get('shots') or []
        if not shots:
            raise ValueError('Clip has no source shots; cannot apply a time limit.')
        ranges = [(float(s['source_start']), float(s['source_end'])) for s in shots]
        if any(not math.isfinite(a + b) or a < 0 or b <= a for a, b in ranges):
            raise ValueError('Invalid source timing; cannot apply a time limit.')
        if any(abs(a - ranges[i-1][1]) > .05 for i, (a, _) in enumerate(ranges) if i):
            raise ValueError('Source shots are not contiguous within a clip.')
        clips.append((ranges[0][0], ranges[-1][1], data['sequence']['key']))
    clips.sort()
    selected, actual_end = [], 0.0
    for start, end, key in clips:
        if end > end_seconds + 1e-6:
            break
        if abs(start - actual_end) > .05:
            raise ValueError('Generation prefix has a gap or overlap in source timing.')
        selected.append(key)
        actual_end = end
    if not selected:
        # An empty list means ALL clips to the production controller.
        raise ValueError('No complete opening clip fits within the requested time limit.')
    if len(set(selected)) != len(selected):
        raise ValueError('Duplicate clip keys in generation prefix.')
    return selected, actual_end


def configure(board, auto, config):
    """Read the latest owner scope at handoff, not the upload-time snapshot."""
    result = deepcopy(config)
    scope = auto.get('generation_scope')
    if scope:
        if scope.get('analysis') != 'full' or scope.get('source_start_seconds') != 0:
            raise ValueError('Only a production prefix with full source analysis is supported.')
        keys, _ = prefix_clips(board, scope['source_end_seconds'])
        result['sequence_keys'] = keys
    if auto.get('production_change'):
        result['review_masters'] = auto['settings']['review_masters']
    return result


def request_prefix(video_id, end_seconds, *, review_masters):
    """Persist an owner change before generation; never interrupt source analysis."""
    end_seconds = float(end_seconds)
    if not math.isfinite(end_seconds) or end_seconds <= 0:
        raise ValueError('Invalid generation end.')
    from flowboard.services.automation_jobs import now
    with get_session() as s:
        initial = s.get(VideoAnalysis, video_id)
        if not initial:
            raise ValueError('Source was deleted.')
        s.exec(select(AutomationProject).where(
            AutomationProject.id == initial.automation_project_id).with_for_update()).one()
        row = s.exec(select(VideoAnalysis).where(VideoAnalysis.id == video_id).with_for_update()
                     .execution_options(populate_existing=True)).one()
        auto = deepcopy(row.options.get('auto_production') or {})
        if not auto.get('settings') or auto.get('run_id'):
            raise ValueError('This operation is for a source film before production handoff.')
        existing = s.exec(select(AutomationJob).where(
            AutomationJob.project_id == row.automation_project_id,
            AutomationJob.kind == 'production_run')).first()
        if existing:
            raise ValueError('Production has already been created; change its selected clips explicitly.')
        auto['generation_scope'] = {'analysis': 'full', 'source_start_seconds': 0,
                                    'source_end_seconds': end_seconds,
                                    'boundary_policy': 'complete_clips_at_or_before_end'}
        auto['settings']['review_masters'] = bool(review_masters)
        auto['production_change'] = {'id': str(uuid.uuid4()), 'status': 'pending',
                                     'requested_at': now().isoformat()}
        row.options = {**row.options, 'auto_production': auto}
        s.add(row)
        s.commit()
        return deepcopy(auto)


def apply_pending(video_id):
    """Compatibility handoff for a source coroutine already running old code.

    Only an untouched master-review run may be released. This is not approval of
    source findings or generated output, and never requeues a failed paid job.
    """
    from flowboard.services import automation_jobs as jobs, production_run
    with get_session() as s:
        source = s.get(VideoAnalysis, video_id)
        if not source:
            raise ValueError('Source was deleted.')
        project = s.exec(select(AutomationProject).where(
            AutomationProject.id == source.automation_project_id).with_for_update()).one()
        row = s.exec(select(VideoAnalysis).where(VideoAnalysis.id == video_id).with_for_update()
                     .execution_options(populate_existing=True)).one()
        auto = deepcopy(row.options.get('auto_production') or {})
        change = auto.get('production_change') or {}
        if change.get('status') == 'applied':
            return change
        if row.status in ('failed', 'interrupted'):
            return {'status': 'source_blocked', 'error': row.error, 'change': change}
        if not auto.get('run_id'):
            return {'status': 'waiting_for_full_source_analysis', 'change': change}
        if not change or change.get('status') != 'pending':
            raise ValueError('No pending owner change to apply.')
        run = s.exec(select(AutomationJob).where(
            AutomationJob.id == uuid.UUID(auto['run_id'])).with_for_update()).one()
        if run.project_id != project.id or run.kind != production_run.KIND:
            raise ValueError('Source handoff does not belong to this project.')
        board = jobs.project_board(s, project)
        config = configure(board, auto, run.payload['config'])
        already_configured = run.payload['config'] == config
        if not already_configured:
            if (run.status != 'paused' or run.result.get('stage') != 'master_review'
                    or run.result.get('tasks') or any((run.result.get('created_counts') or {}).values())):
                raise ValueError('Production is no longer at the untouched master-review handoff.')
            if s.exec(select(AutomationJob).where(
                    AutomationJob.project_id == project.id, AutomationJob.kind == production_run.KIND,
                    AutomationJob.status == 'running')).first():
                raise ValueError('Another production run is active.')
            report = production_run.preview(board, project.id, config)
            if not report['ready']:
                return {'status': 'source_blocked', 'issues': report['issues'], 'change': change}
            if run.payload['input_version'] != report['input_version']:
                raise ValueError('Board changed since handoff; no automatic approval.')
            if board.get('motionReference') == 'white_model':
                from flowboard.services import reskin, reskin_runtime
                reskin_runtime.require_ready(audio=config.get('audio_cleanup', False))
                if reskin.source_fingerprint(reskin.source_path(board['sourceVideoId'])) != board.get('sourceMediaSha256'):
                    raise ValueError('Source file changed; review the new source before production.')
            run.payload = {**run.payload, 'config': config}
            run.result = {**run.result, 'stage': 'master_review' if config['review_masters'] else 'materials',
                          'preview': report, 'progress_plan': report['progress_plan']}
            run.status = 'paused' if config['review_masters'] else 'running'
            run.updated_at = jobs.now()
            s.add(run)
        keys, actual_end = prefix_clips(board, auto['generation_scope']['source_end_seconds'])
        change = {**change, 'status': 'applied', 'applied_at': jobs.now().isoformat(),
                  'run_id': str(run.id), 'sequence_keys': keys, 'actual_source_end_seconds': actual_end}
        auto['production_change'] = change
        row.options = {**row.options, 'auto_production': auto}
        s.add(row)
        s.commit()
        return change
