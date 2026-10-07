"""Assistant contract tests: no provider calls or generation in this suite."""
import json
import uuid
from copy import deepcopy
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from sqlmodel import select
from flowboard.db import get_session
from flowboard.db.models import AutomationAssistantTurn, AutomationProject, AutomationJob, User
from flowboard.routes import automation_assistant as route
from flowboard.services import automation_assistant as agent
from tests.test_production_run import fixture, project


def turn(pid, message='Chỉ đọc board'):
    with get_session() as s:
        t = AutomationAssistantTurn(project_id=pid, message=message, request_key=str(uuid.uuid4()))
        s.add(t); s.commit(); s.refresh(t); return t.id


def get(tid):
    with get_session() as s: return s.get(AutomationAssistantTurn, tid)


def all_jobs(pid):
    with get_session() as s: return s.exec(select(AutomationJob).where(AutomationJob.project_id == pid)).all()


def execute(pid, name, args, expected=None):
    board, _, _ = agent.snapshot(pid, None)
    return agent.execute_tool(pid, None, name, args, expected or agent.signature(board), 'test-' + str(uuid.uuid4()))


def test_inventory_inspection_is_read_only():
    pid = project(); board, rev, _ = agent.snapshot(pid, None)
    out = execute(pid, 'inspect_project', {})
    assert [c['key'] for c in out['clips']] == ['c1']
    assert len(execute(pid, 'inspect_clip', {'sequence_key': 'c1'})['sequence']['shots']) == 2
    assert execute(pid, 'inspect_material', {'node_id': 'environment:hall'})['profile']['name'] == 'hall'
    assert agent.snapshot(pid, None)[1] == rev and not all_jobs(pid)


@pytest.mark.parametrize('name,args', [('shell', {'cmd': 'pwd'}), ('inspect_material', {'node_id': 'elsewhere'}), ('produce_clips', {'sequence_keys': ['missing'], 'mode': 'render'})])
def test_unknown_targets_never_queue(name, args):
    pid = project()
    with pytest.raises(ValueError): execute(pid, name, args)
    assert not all_jobs(pid)


def test_edit_preserves_material_identity_reference_and_source():
    pid = project(); before, _, _ = agent.snapshot(pid, None)
    out = execute(pid, 'edit_material_prompt', {'node_id': 'environment:hall', 'prompt': 'Approved style; no furniture.'})
    after, _, _ = agent.snapshot(pid, None)
    assert out['saved'] and out['after'] == 'Approved style; no furniture.'
    assert before['productionAssets'] == after['productionAssets']
    assert before['nodes'][-1] == after['nodes'][-1]
    assert after['nodes'][3]['data']['plate']['referenceUrl'] == before['nodes'][3]['data']['plate']['referenceUrl']
    assert not all_jobs(pid)


def test_stale_signature_prevents_overwrite():
    pid = project(); b, _, _ = agent.snapshot(pid, None); expected = agent.signature(b)
    execute(pid, 'set_settings', {'aspect_ratio': '9:16'})
    with pytest.raises(ValueError, match='đã đổi'):
        execute(pid, 'edit_material_prompt', {'node_id': 'environment:hall', 'prompt': 'Wrong stale edit'}, expected)
    assert agent.snapshot(pid, None)[0]['nodes'][3]['data']['plate']['prompt'] != 'Wrong stale edit'


def test_video_edit_requires_audio_policy_and_invalidates_coverage():
    b = fixture(); b['nodes'][-2]['data'].update(coverage={'ready': True}, coverageToken='old', contractDigest='old')
    pid = project(b)
    with pytest.raises(ValueError, match='AUDIO'):
        execute(pid, 'edit_video_prompt', {'sequence_key': 'c1', 'prompt': 'NO BACKGROUND MUSIC. NO BGM. NO SCORE. elsewhere'})
    execute(pid, 'edit_video_prompt', {'sequence_key': 'c1', 'prompt': 'Create a shot.\nAUDIO:\nNO BACKGROUND MUSIC. NO BGM. NO SCORE.\nCONTINUITY:\nSame identity.'})
    d = agent.snapshot(pid, None)[0]['nodes'][-2]['data']
    assert d['coverage'] is None and d['coverageToken'] is None and d['promptBy'] == 'manual'
    assert not all_jobs(pid)


def test_generate_material_queues_exactly_one_and_blocks_duplicate():
    pid = project()
    out = execute(pid, 'generate_material', {'node_id': 'environment:hall'})
    assert out['job_id']
    jobs = all_jobs(pid)
    assert len(jobs) == 1 and jobs[0].kind == 'plate' and jobs[0].node_id == 'environment:hall'
    with pytest.raises(ValueError, match='Đang có'):
        execute(pid, 'generate_material', {'node_id': 'environment:hall'})
    assert len(all_jobs(pid)) == 1


def test_prepare_scopes_clips_and_does_not_queue_video():
    pid = project()
    out = execute(pid, 'produce_clips', {'sequence_keys': ['c1'], 'mode': 'prepare'})
    jobs = all_jobs(pid)
    assert out['mode'] == 'prepare' and len(jobs) == 1 and jobs[0].kind == 'production_run'
    assert jobs[0].payload['config']['sequence_keys'] == ['c1']
    assert jobs[0].payload['config']['resolution'] == '480p'
    assert jobs[0].status == 'paused' and jobs[0].result['stage'] == 'master_review'
    assert execute(pid, 'edit_material_prompt', {'node_id': 'environment:hall', 'prompt': 'Changed'})['saved']


@pytest.mark.asyncio
async def test_send_idempotency_ownership_and_active_turn(monkeypatch):
    pid = project(); launches = []
    monkeypatch.setattr(agent, 'launch', lambda tid, user: launches.append(tid))
    body = route.Send(message='Read only', request_key=uuid.uuid4(), expected_revision=0)
    one = await route.send(pid, body, None)
    two = await route.send(pid, body, None)
    assert one['id'] == two['id'] and len(launches) == 1
    with pytest.raises(HTTPException) as active:
        await route.send(pid, body.model_copy(update={'request_key': uuid.uuid4()}), None)
    assert active.value.status_code == 409
    with get_session() as s:
        owner = User(username='owner', password_hash='unused')
        stranger = User(username='stranger', password_hash='unused')
        s.add(owner); s.add(stranger); s.commit(); s.refresh(owner); s.refresh(stranger)
        p = s.get(AutomationProject, pid); p.owner_user_id = owner.id; s.add(p); s.commit(); s.refresh(stranger); s.expunge(stranger)
    with pytest.raises(HTTPException) as denied: route.history(pid, None, stranger)
    assert denied.value.status_code in (403, 404)
    with pytest.raises(HTTPException): route.stop(pid, uuid.UUID(one['id']), stranger)


def mock_responses(monkeypatch, responses):
    calls = []
    async def complete(model, messages, **kwargs):
        calls.append((model, deepcopy(messages), kwargs))
        return SimpleNamespace(text=json.dumps(responses.pop(0)))
    monkeypatch.setattr(agent.avis_text, 'complete', complete)
    monkeypatch.setenv('AUTOMATION_ASSISTANT_MODEL', 'gpt-6-luna')
    return calls


@pytest.mark.asyncio
async def test_gpt_reads_then_saves_and_returns_receipts(monkeypatch):
    pid = project(); tid = turn(pid, 'Sửa prompt bối cảnh, chưa gen')
    calls = mock_responses(monkeypatch, [
        {'reply': 'Đọc', 'tool': {'name': 'inspect_material', 'arguments': {'node_id': 'environment:hall'}}},
        {'reply': 'Sửa', 'tool': {'name': 'edit_material_prompt', 'arguments': {'node_id': 'environment:hall', 'prompt': 'Preserved style. No chairs.'}}},
        {'reply': 'Đã lưu, chưa gen ảnh.', 'tool': None},
    ])
    await agent.run_turn(tid, None)
    t = get(tid)
    assert t.status == 'completed', t.error
    assert len(calls) == 3 and calls[0][0] == 'gpt-6-luna' and calls[0][2]['attempts'] == 1
    assert 'NO BACKGROUND MUSIC' in calls[0][1][0]['content']
    assert any(e.get('result', {}).get('saved') for e in t.events)
    assert not all_jobs(pid)


@pytest.mark.asyncio
async def test_edit_without_read_stops_before_write(monkeypatch):
    pid = project(); tid = turn(pid)
    mock_responses(monkeypatch, [{'reply': 'Sửa', 'tool': {'name': 'edit_material_prompt', 'arguments': {'node_id': 'environment:hall', 'prompt': 'Bad'}}}])
    await agent.run_turn(tid, None)
    assert get(tid).status == 'failed' and 'chưa đọc' in get(tid).error
    assert agent.snapshot(pid, None)[1] == 0


@pytest.mark.asyncio
async def test_stop_during_model_call_prevents_new_paid_work(monkeypatch):
    pid = project(); tid = turn(pid)
    async def complete(*args, **kwargs):
        route.stop(pid, tid, None)
        return SimpleNamespace(text=json.dumps({'reply': 'Generate', 'tool': {'name': 'produce_clips', 'arguments': {'sequence_keys': ['c1'], 'mode': 'render'}}}))
    monkeypatch.setattr(agent.avis_text, 'complete', complete)
    await agent.run_turn(tid, None)
    assert get(tid).status == 'stopped' and not all_jobs(pid)


def test_recovery_keeps_jobs_without_replay():
    pid = project(); tid = turn(pid)
    execute(pid, 'generate_material', {'node_id': 'environment:hall'})
    agent.recover()
    assert get(tid).status == 'interrupted'
    assert len(all_jobs(pid)) == 1 and all_jobs(pid)[0].status == 'queued'


def test_cross_board_job_read_and_binary_context():
    pid = project(); other = project()
    job = execute(other, 'generate_material', {'node_id': 'environment:hall'})
    with pytest.raises(HTTPException): execute(pid, 'inspect_job', {'job_id': job['job_id']})
    assert agent.clean_context({'image': 'binary', 'prompt': 'data:abc', 'profile': {'name': 'Hero'}}) == {'prompt': '[inline image omitted]', 'profile': {'name': 'Hero'}}

@pytest.mark.asyncio
async def test_external_change_between_read_and_edit_is_not_silently_overwritten(monkeypatch):
    pid = project(); tid = turn(pid)
    calls = mock_responses(monkeypatch, [
        {'reply': 'Đọc', 'tool': {'name': 'inspect_material', 'arguments': {'node_id': 'environment:hall'}}},
        {'reply': 'Sửa', 'tool': {'name': 'edit_material_prompt', 'arguments': {'node_id': 'environment:hall', 'prompt': 'Old context'}}},
    ])
    original = agent.add_event
    def race(tid, event):
        original(tid, event)
        if event['tool'] == 'inspect_material' and event['status'] == 'completed':
            execute(pid, 'set_settings', {'aspect_ratio': '9:16'})
    monkeypatch.setattr(agent, 'add_event', race)
    await agent.run_turn(tid, None)
    assert get(tid).status == 'failed' and 'đã đổi' in get(tid).error
    assert len(calls) == 1
    assert agent.snapshot(pid, None)[0]['nodes'][3]['data']['plate']['prompt'] != 'Old context'
