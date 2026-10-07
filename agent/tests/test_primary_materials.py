"""Primary checkpoint and image lineage; never invoke paid providers."""
from copy import deepcopy
import uuid
import json
import pytest
from flowboard.db import get_session
from flowboard.db.models import AutomationProject, AutomationJob
from flowboard.services import primary_materials as primary, automation_jobs as jobs, shot_package
from tests.test_production_run import fixture as base_fixture, project, start, get, tick, pending, finish, drive, all_jobs


def fixture():
    return json.loads(json.dumps(base_fixture()))

def approve(client, pid, rid, revision=0):
    return client.post(f'/api/automation/projects/{pid}/primary-materials/continue', json={
        'run_id': rid, 'expected_revision': revision})


def test_review_stops_before_generation_and_cannot_be_bypassed(client):
    pid = project(); run = start(pid, review_masters=True)
    assert run['status'] == 'paused' and run['result']['stage'] == 'master_review'
    tick(run['id']); assert not all_jobs(pid)
    inventory = client.get(f'/api/automation/projects/{pid}/primary-materials').json()
    assert {i['kind'] for i in inventory['items']} == {'character', 'environment'}
    assert len(inventory['items']) == 2 and all(i['required'] for i in inventory['items'])
    assert inventory['waiting_run']['id'] == run['id']
    assert client.post(f'/api/automation/projects/{pid}/production-runs/{run["id"]}/resume').status_code == 409
    with pytest.raises(ValueError, match='sheet'): start(pid)
    response = client.post(f'/api/automation/projects/{pid}/jobs', json={
        'kind': 'plate', 'node_id': 'character:hero', 'slot': 'day', 'payload': {},
        'request_key': 'blocked-state', 'expected_revision': 0})
    assert response.status_code == 409, response.text
    assert not all_jobs(pid)


def test_missing_master_blocks_continue_but_allows_only_master_generation(client):
    b = fixture(); b['nodes'][0]['data']['identity'] = {}
    pid = project(b); r = start(pid, review_masters=True)
    assert approve(client, pid, r['id']).status_code == 409
    url = f'/api/automation/projects/{pid}/primary-materials/generate'
    body = {'node_id': 'character:hero', 'expected_revision': 0, 'request_key': 'one-master'}
    result = client.post(url, json=body)
    assert result.status_code == 202, result.text
    assert client.post(url, json=body).json()['id'] == result.json()['id']
    assert len(all_jobs(pid)) == 1
    j = get(result.json()['id'])
    assert j.slot == 'identity' and not j.payload['reference_urls'] and j.payload['prompt']
    assert approve(client, pid, r['id']).status_code == 409
    assert client.post(url, json={**body, 'node_id': 'asset:box'}).status_code == 422
    finish(j)
    assert approve(client, pid, r['id']).status_code == 200


def test_approval_revision_ownership_idempotency_and_no_source_mutation(client):
    b = fixture(); pid = project(b); r = start(pid, review_masters=True)
    assert approve(client, pid, r['id'], 99).status_code == 409
    assert approve(client, project(), r['id']).status_code == 404
    assert approve(client, pid, r['id']).status_code == 200
    assert approve(client, pid, r['id']).status_code == 200
    assert not all_jobs(pid)
    out = drive(pid, r['id']); assert out.status == 'succeeded', out.error
    with get_session() as s:
        assert s.get(AutomationProject, pid).board['sourceVerification'] == b['sourceVerification']
    assert not any(j.kind == 'clip' for j in all_jobs(pid))


def test_replacing_master_marks_generated_states_keeps_uploads_and_old_files():
    b = fixture(); d = b['nodes'][0]['data']
    d['states']['custom'] = {'referenceUrl': 'https://test/custom', 'uploaded': True}
    edited = deepcopy(b)
    edited['nodes'][0]['data']['identity'] = {'referenceUrl': 'https://test/new-face'}
    original = deepcopy(b)
    primary.invalidate_identity_states(edited, b, [])
    states = edited['nodes'][0]['data']['states']
    assert states['day']['needsIdentityRefresh']
    assert states['day']['referenceUrl'] == 'https://test/hero'
    assert not states['custom'].get('needsIdentityRefresh')
    assert b == original
    package = shot_package.build(edited, 'film', 'c1')
    assert package['materials']['hero:day']['reference_url'] == 'https://test/new-face'


def test_master_job_invalidates_old_states_until_new_identity_used():
    b = fixture(); pid = uuid.uuid4()
    def job(slot, url, refs=None):
        return AutomationJob(id=uuid.uuid4(), project_id=pid, kind='plate', node_id='character:hero', slot=slot,
            request_key=url, status='succeeded', payload={'reference_urls': refs or []},
            result={'images': [{'reference_url': url}]})
    old = job('day', 'https://test/old-state', ['https://test/hero'])
    master = job('identity', 'https://test/new-face')
    out = jobs.overlay(b, [old, master]); state = out['nodes'][0]['data']['states']['day']
    assert state['needsIdentityRefresh'] and str(old.id) in state['ignoredRuntimeJobIds']
    assert jobs.overlay(out, [old, master])['nodes'][0]['data']['states']['day']['needsIdentityRefresh']
    fresh = job('day', 'https://test/fresh-state', ['https://test/new-face'])
    out = jobs.overlay(out, [old, master, fresh])
    assert not out['nodes'][0]['data']['states']['day']['needsIdentityRefresh']
    assert out['nodes'][0]['data']['states']['day']['referenceUrl'] == 'https://test/fresh-state'
    assert jobs.public(fresh)['reference_urls'] == ['https://test/new-face']


def test_upload_master_then_variants_use_uploaded_identity(client):
    b = fixture(); pid = project(b); r = start(pid, review_masters=True)
    edited = deepcopy(b)
    edited['nodes'][0]['data']['identity'] = {'referenceUrl': 'https://test/uploaded-master', 'uploaded': True, 'mediaId': 'new-media'}
    saved = client.patch(f'/api/automation/projects/{pid}', json={'expected_revision': 0, 'board': edited})
    assert saved.status_code == 200, saved.text
    revision = saved.json()['revision']
    assert approve(client, pid, r['id'], revision).status_code == 200
    tick(r['id'])
    states = [j for j in pending(pid) if j.kind == 'plate' and j.slot == 'day']
    assert len(states) == 1
    assert states[0].payload['reference_urls'] == ['https://test/uploaded-master']
    assert get(r['id']).result['approved_master_refs']['character:hero'] == 'https://test/uploaded-master'
