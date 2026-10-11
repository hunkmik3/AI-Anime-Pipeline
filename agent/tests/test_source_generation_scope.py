from copy import deepcopy
import uuid

import pytest
from sqlmodel import select

from flowboard.db import get_session
from flowboard.db.models import AutomationJob, AutomationProject, VideoAnalysis
from flowboard.services import production_run, source_generation_scope as scope


def board():
    return {'nodes': [
        {'id': 'seq:' + key, 'data': {'kind': 'sequence', 'sequence': {'key': key},
                                    'shots': [{'source_start': a, 'source_end': b}]}}
        for key, a, b in [('a', 0, 90), ('b', 90, 176), ('c', 176, 187), ('d', 187, 1859)]
    ]}


def source_row():
    with get_session() as s:
        project = AutomationProject(name='Thirty minute film', board=board())
        s.add(project)
        s.flush()
        source = VideoAnalysis(name='Full source', automation_project_id=project.id,
                               status='analysing', analysis={'video': {'duration': 1859}},
                               options={'auto_production': {'settings': {'review_masters': True}}})
        s.add(source)
        s.commit()
        return source.id, project.id


def test_prefix_stops_before_crossing_clip_and_keeps_entire_board():
    full = board()
    original = deepcopy(full)
    assert scope.prefix_clips(full, 180) == (['a', 'b'], 176)
    assert scope.prefix_clips(full, 187) == (['a', 'b', 'c'], 187)
    assert full == original


@pytest.mark.parametrize('end', [0, -1, float('nan'), float('inf'), 30])
def test_invalid_or_empty_prefix_never_falls_back_to_whole_film(end):
    with pytest.raises(ValueError):
        scope.prefix_clips(board(), end)


def test_prefix_rejects_missing_or_noncontiguous_source_timing():
    full = board()
    full['nodes'][1]['data']['shots'][0]['source_start'] = 100
    with pytest.raises(ValueError, match='gap or overlap'):
        scope.prefix_clips(full, 180)
    del full['nodes'][0]['data']['shots'][0]['source_start']
    with pytest.raises(KeyError):
        scope.prefix_clips(full, 180)


def test_owner_change_leaves_full_analysis_and_source_worker_untouched():
    vid, pid = source_row()
    with get_session() as s:
        job = AutomationJob(project_id=pid, kind='source', node_id='source:'+str(vid),
                            request_key='source-worker', status='preparing', lease_token='keep-lease')
        s.add(job)
        s.commit()
    auto = scope.request_prefix(vid, 180, review_masters=False)
    assert not auto['settings']['review_masters']
    assert auto['generation_scope']['analysis'] == 'full'
    config = scope.configure(board(), auto, {'review_masters': True, 'sequence_keys': [], 'mode': 'render'})
    assert config == {'review_masters': False, 'sequence_keys': ['a', 'b'], 'mode': 'render'}
    assert scope.apply_pending(vid)['status'] == 'waiting_for_full_source_analysis'
    with get_session() as s:
        assert s.get(VideoAnalysis, vid).analysis['video']['duration'] == 1859
        assert len(s.get(AutomationProject, pid).board['nodes']) == 4
        job = s.exec(select(AutomationJob)).one()
        assert job.status == 'preparing' and job.lease_token == 'keep-lease'


def paused_handoff(vid, pid):
    with get_session() as s:
        run = AutomationJob(project_id=pid, kind='production_run', node_id='production',
                            request_key='source-film:'+str(vid), status='paused',
                            payload={'config': {'review_masters': True, 'sequence_keys': []},
                                     'input_version': 'unchanged'},
                            result={'stage': 'master_review', 'tasks': {}, 'created_counts': {}})
        s.add(run)
        s.flush()
        row = s.get(VideoAnalysis, vid)
        row.options = {**row.options, 'auto_production': {
            **row.options['auto_production'], 'run_id': str(run.id)}}
        s.add(row)
        s.commit()
        return run.id


def test_legacy_inflight_handoff_applies_scope_before_releasing_and_is_idempotent(monkeypatch):
    vid, pid = source_row()
    scope.request_prefix(vid, 180, review_masters=False)
    rid = paused_handoff(vid, pid)
    monkeypatch.setattr(production_run, 'preview', lambda *a: {
        'ready': True, 'issues': [], 'input_version': 'unchanged', 'progress_plan': {'selected': 2}})
    result = scope.apply_pending(vid)
    assert result['status'] == 'applied' and result['actual_source_end_seconds'] == 176
    assert scope.apply_pending(vid) == result
    with get_session() as s:
        run = s.get(AutomationJob, rid)
        assert run.status == 'running' and run.result['stage'] == 'materials'
        assert run.payload['config']['sequence_keys'] == ['a', 'b']
        assert run.payload['config']['review_masters'] is False
        assert not run.result.get('master_review_approved')


def test_source_findings_do_not_get_approved_or_release_production(monkeypatch):
    vid, pid = source_row()
    scope.request_prefix(vid, 180, review_masters=False)
    rid = paused_handoff(vid, pid)
    monkeypatch.setattr(production_run, 'preview', lambda *a: {
        'ready': False, 'issues': [{'code': 'unresolved_source'}]})
    result = scope.apply_pending(vid)
    assert result['status'] == 'source_blocked'
    with get_session() as s:
        assert s.get(AutomationJob, rid).status == 'paused'


def test_started_jobs_are_not_reconfigured_or_retried():
    vid, pid = source_row()
    scope.request_prefix(vid, 180, review_masters=False)
    rid = paused_handoff(vid, pid)
    with get_session() as s:
        run = s.get(AutomationJob, rid)
        run.result = {**run.result, 'tasks': {'plate': {'id': str(uuid.uuid4())}}}
        s.add(run)
        s.commit()
    with pytest.raises(ValueError, match='untouched'):
        scope.apply_pending(vid)


def test_updated_server_handoff_also_applies_owner_scope(monkeypatch):
    vid, pid = source_row()
    scope.request_prefix(vid, 180, review_masters=False)
    captured = []
    def preview(full, project, config):
        captured.append(deepcopy(config))
        return {'ready': True, 'issues': [], 'input_version': 'unchanged', 'progress_plan': {}}
    monkeypatch.setattr(production_run, 'preview', preview)
    result = production_run.start(pid, 0, {'review_masters': True, 'sequence_keys': []}, 'source-film:'+str(vid))
    assert result['status'] == 'running'
    assert captured[0]['sequence_keys'] == ['a', 'b'] and not captured[0]['review_masters']
    with get_session() as s:
        row = s.get(VideoAnalysis, vid)
        row.options = {**row.options, 'auto_production': {**row.options['auto_production'], 'run_id': result['id']}}
        s.add(row)
        s.commit()
    assert scope.apply_pending(vid)['status'] == 'applied'
