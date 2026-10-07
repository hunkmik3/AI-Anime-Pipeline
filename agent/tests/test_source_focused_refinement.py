import asyncio
import copy
import json

from flowboard.services import avis_text
from flowboard.services.video_analyzer import source_refinement as refine
from flowboard.services.video_analyzer.vision import TIER1_MODEL
from tests.test_source_refinement import _case, _mock


def test_focused_uses_existing_observations_and_one_independent_visual_review(tmp_path, monkeypatch):
    case = _case(tmp_path)
    original = copy.deepcopy(case[2])
    calls, checks = _mock(monkeypatch)
    complete = avis_text.complete
    models = []
    async def record(model, messages, **kwargs):
        models.append(model)
        return await complete(model, messages, **kwargs)
    monkeypatch.setattr(avis_text, 'complete', record)
    result = asyncio.run(refine.refine(*case, strategy='focused'))
    assert len(calls) == 2 and len(checks) == 1
    assert models == [refine.inv.MODEL, TIER1_MODEL]
    assert result['source_verification']['status'] == 'verified'
    assert result['transcript'] == original['transcript']
    assert result['dialogue_track'] == original['dialogue_track']
    assert case[2] == original


def test_focused_stops_after_one_repair_and_retains_unresolved_defect(tmp_path, monkeypatch):
    case = _case(tmp_path)
    calls, checks = _mock(monkeypatch, always_issue=True)
    result = asyncio.run(refine.refine(*case, strategy='focused'))
    assert len(calls) == 4 and len(checks) == 2
    report = result['source_verification']
    assert report['status'] == 'needs_review' and 1 in report['unresolved_shots']
    assert report['findings']


def test_focused_never_adds_a_protocol_retry_or_accepts_missing_dispositions(tmp_path, monkeypatch):
    case = _case(tmp_path)
    calls, checks = _mock(monkeypatch, omit=True)
    result = asyncio.run(refine.refine(*case, strategy='focused'))
    assert len(calls) == 4 and len(checks) == 2
    assert result['source_verification']['status'] == 'needs_review'
    assert 1 in result['source_verification']['unresolved_shots']


def test_focused_route_persists_strategy_for_worker_recovery(client, monkeypatch):
    from tests.test_source_refinement_routes import _row
    from flowboard.routes import video_analysis as routes
    key, _ = _row()
    operations = []
    def start(video_id, coro, **kwargs):
        coro.close()
        operations.append(kwargs['operation'])
    monkeypatch.setattr(routes, '_start', start)
    response = client.post(f'/api/automation/videos/{key}/refine', json={'shots':[1], 'strategy':'focused'})
    assert response.status_code == 200
    assert operations == [{'task':'refine', 'shots':[1], 'strategy':'focused'}]


def test_focused_worker_deadline_preserves_original_source(tmp_path, monkeypatch):
    from tests.test_source_refinement_routes import _row
    from flowboard.routes import video_analysis as routes
    from flowboard.db import get_session
    from flowboard.db.models import VideoAnalysis
    key, original = _row()
    video = tmp_path/'source.mp4'
    video.write_bytes(b'source')
    monkeypatch.setattr(routes, '_source_path', lambda _: video)
    async def never(*args, **kwargs):
        raise AssertionError('Must be cancelled before committing any edit')
    async def expire(coro, timeout):
        assert timeout == 300
        coro.close()
        raise TimeoutError()
    monkeypatch.setattr(routes.source_refinement, 'refine', never)
    monkeypatch.setattr(routes.asyncio, 'wait_for', expire)
    asyncio.run(routes._run_source_refinement(key, strategy='focused'))
    with get_session() as session:
        row = session.get(VideoAnalysis, key)
        assert row.analysis == original and row.status == 'failed'
        assert '300-second budget' in row.error
