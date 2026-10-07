import asyncio
import uuid
from io import BytesIO
import pytest
from fastapi import HTTPException, UploadFile
from PIL import Image
from flowboard.routes import automation as routes
from flowboard.services import automation_jobs as jobs
from flowboard.db.models import AutomationJob, AutomationProject, User
from flowboard.db import get_session


def pixels():
    output = BytesIO()
    Image.new('RGB', (4, 4), '#6c91a1').save(output, format='PNG')
    return output.getvalue()


def test_upload_stores_public_reference_and_media_without_generation(monkeypatch):
    seen = []
    monkeypatch.setattr(routes.automation, '_publish', lambda raw: seen.append(raw) or 'https://test/plate.png')
    monkeypatch.setattr(routes.automation, '_ingest_plate', lambda raw: 'media-test')
    out = routes._store_uploaded_plate(pixels())
    assert out.reference_url == out.url == 'https://test/plate.png'
    assert out.media_id == 'media-test' and out.persisted
    assert Image.open(BytesIO(seen[0])).size == (4, 4)


def test_invalid_image_never_reaches_storage(monkeypatch):
    def unexpected(*args): raise AssertionError('invalid bytes were uploaded')
    monkeypatch.setattr(routes.automation, '_publish', unexpected)
    with pytest.raises(HTTPException) as error:
        routes._store_uploaded_plate(b'not an image')
    assert error.value.status_code == 415


def test_storage_failure_is_reported_without_success(monkeypatch):
    monkeypatch.setattr(routes.automation, '_publish', lambda _: None)
    with pytest.raises(HTTPException) as error:
        routes._store_uploaded_plate(pixels())
    assert error.value.status_code == 503


def test_upload_checks_project_ownership_before_storage():
    owner, outsider = uuid.uuid4(), uuid.uuid4()
    with get_session() as session:
        session.add(User(id=owner, username='upload-owner', password_hash='test-only'))
        session.commit()
        row = AutomationProject(name='Upload test', owner_user_id=owner)
        session.add(row); session.commit(); session.refresh(row); project_id = row.id
    from types import SimpleNamespace
    with pytest.raises(HTTPException) as error:
        asyncio.run(routes.upload_material_image(project_id, UploadFile(filename='test.png', file=BytesIO(pixels())), SimpleNamespace(id=outsider)))
    assert error.value.status_code == 404


def test_upload_survives_old_job_overlay_but_accepts_new_generation():
    old_id = uuid.uuid4()
    board = {'nodes': [{'id': 'env:room', 'data': {'kind': 'environment', 'plate': {
        'prompt': 'My prompt', 'referenceUrl': 'https://upload', 'ignoredRuntimeJobIds': [str(old_id)]}}}]}
    old = AutomationJob(id=old_id, project_id=uuid.uuid4(), kind='plate', node_id='env:room', slot='plate',
        request_key='old', status='succeeded', result={'images': [{'reference_url': 'https://old'}]})
    assert jobs.overlay(board, [old])['nodes'][0]['data']['plate']['referenceUrl'] == 'https://upload'
    old.id = uuid.uuid4()
    assert jobs.overlay(board, [old])['nodes'][0]['data']['plate']['referenceUrl'] == 'https://old'


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', [False, True])
async def test_queued_manual_prompt_uses_verification_never_writer(monkeypatch, changed):
    from tests.test_automation_runtime import make_project
    from flowboard.services import prompt_coverage
    pid = make_project()
    draft = 'My exact manual prompt.'
    payload = {'sequence': {'key': 'c1'}, 'shots': [], 'characters': [], 'provided_prompt': draft}
    job = jobs.enqueue(pid, 'write', 'vid:c1', 'prompt', payload, 'manual-test')
    seen = []
    async def verify(body):
        seen.append(body.prompt)
        return routes.VideoWriteResponse(prompt='unexpected rewrite' if changed else body.prompt,
                                         duration_seconds=5, coverage_token='receipt')
    async def never_write(body):
        raise AssertionError('Manual prompt reached the AI writer')
    monkeypatch.setattr(prompt_coverage, 'is_strict', lambda *args: True)
    monkeypatch.setattr(routes, 'verify_video_prompt', verify)
    monkeypatch.setattr(routes, 'write_video_prompt', never_write)
    await jobs.execute(jobs.claim())
    with get_session() as session:
        row = session.get(AutomationJob, uuid.UUID(job['id']))
        assert seen == [draft]
        assert row.status == ('failed' if changed else 'succeeded')
        if not changed:
            assert row.result['prompt'] == draft and row.result['writer'] == 'manual'


def test_batch_reuses_uploaded_image_and_queues_exact_manual_draft():
    from tests.test_production_run import fixture, project, start, tick, pending, finish, all_jobs, get
    from copy import deepcopy
    board = fixture()
    upload = board['nodes'][3]['data']['plate']
    old_id = uuid.uuid4()
    upload.update(uploaded=True, referenceUrl='https://test/upload.png', ignoredRuntimeJobIds=[str(old_id)])
    draft = 'Manual film prompt, kept exactly.'
    board['nodes'][4]['data'].update(prompt=draft, promptBy='manual')
    pid = project(board)
    with get_session() as session:
        session.add(AutomationJob(id=old_id, project_id=pid, kind='plate', node_id='environment:hall', slot='plate',
            request_key='previous-image', status='succeeded', prepared={'material_signature':'outdated'},
            result={'images':[{'reference_url':'https://test/old.png'}]}))
        session.commit()
    production = start(pid)
    for _ in range(8):
        tick(production['id'])
        queued = pending(pid)
        assert not any(j.kind == 'plate' for j in queued)
        writing = next((j for j in queued if j.kind == 'write'), None)
        if writing:
            assert writing.payload['provided_prompt'] == draft
            assert writing.payload['environment']['ref_url'] == 'https://test/upload.png'
            break
        for task in queued: finish(task)
    else:
        raise AssertionError(get(production['id']).error)
    from flowboard.services import production_run
    before = production_run.input_version(board)
    edited = deepcopy(board); edited['nodes'][4]['data']['prompt'] = 'New user draft'
    assert production_run.input_version(edited) != before
    edited = deepcopy(board); edited['nodes'][3]['data']['plate']['referenceUrl'] = 'https://test/new-upload.png'
    assert production_run.input_version(edited) != before
