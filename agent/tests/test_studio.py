"""Reskin Studio: the owner's edits keep the source locks consistent and say what they reopen."""
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from flowboard.db.models import AutomationJob
from flowboard.services import studio
from tests.test_reskin import fixture


def board():
    b = fixture()
    shots = b['nodes'][-1]['data']['shots']
    shots[0].update(dialogue=[{'who': 'Hero', 'speaker_id': 'hero', 'line': 'Come here', 'delivery': 'offscreen'}],
                    performance=['Clip audio 1.000–2.000s: speak this line once.'],
                    source_appearances=[{'source_shot': shots[0]['source_shot'], 'asset_presence': [{'asset_id': 'hero', 'visibility': 'visible'}],
                                         'speakers': [{'label': 'Hero', 'asset_id': 'hero', 'delivery': 'offscreen', 'evidence_ids': ['f1']}]}])
    b['productionAssets'] = [{'id': 'hero', 'kind': 'character', 'name': 'Hero', 'states': [{'key': 'coat'}]}]
    n = str(shots[0]['source_shot'])
    b['sourceVerification'].update(locked_shots={n: {k: deepcopy(shots[0].get(k)) for k in ('dialogue', 'action', 'staging')} | {'duration_s': 4}},
                                   shot_digests={n: 'old'})
    return b


def test_a_dialogue_edit_moves_the_locks_digest_and_timing_and_keeps_the_old_values():
    b = board()
    n = b['nodes'][-1]['data']['shots'][0]['source_shot']
    out, impact = studio.edit_shot(b, n, {'lines': [{'who': 'Hero', 'speaker_id': 'hero', 'line': 'Come  here, now.',
                                                     'delivery': 'on_camera', 'timing': [0.5, 2.5]}]})
    shot = out['nodes'][-1]['data']['shots'][0]
    assert shot['dialogue'] == [{'who': 'Hero', 'line': 'Come here, now.', 'delivery': 'on_camera', 'speaker_id': 'hero'}]
    assert shot['performance'] == ['Clip audio 0.500–2.500s: speak this line once.'] and shot['heard'] == 'Come here, now.'
    sv = out['sourceVerification']
    assert sv['locked_shots'][str(n)]['dialogue'] == shot['dialogue'] and sv['locked_shots'][str(n)]['duration_s'] == 4
    app = shot['source_appearances'][0]
    assert sv['shot_digests'][str(n)] == studio.digest({k: v for k, v in app.items() if k != 'source_shot'})
    # The speaker keeps its evidence; its delivery follows the edit.
    assert app['speakers'] == [{'label': 'Hero', 'asset_id': 'hero', 'delivery': 'on_camera', 'evidence_ids': ['f1']}]
    change = sv['source_corrections'][-1]['changes'][0]
    assert change['before']['dialogue'][0]['line'] == 'Come here' and change['digest_before'] == 'old'
    assert impact == {'clips': [b['nodes'][-1]['data']['sequence']['key']], 'review': True}
    assert b['nodes'][-1]['data']['shots'][0]['dialogue'][0]['line'] == 'Come here'  # the input board is untouched


@pytest.mark.parametrize('line, message', [
    ({'line': '', 'delivery': 'offscreen'}, 'trống'),
    ({'line': 'Hi', 'delivery': 'shouted'}, 'Cách nói'),
    ({'line': 'Hi', 'delivery': 'offscreen', 'speaker_id': 'nobody'}, 'danh mục'),
    ({'line': 'Hi', 'delivery': 'offscreen', 'timing': [3.5, 9]}, 'nằm trong shot'),
])
def test_an_edit_that_breaks_the_source_contract_is_refused(line, message):
    b = board()
    with pytest.raises(ValueError, match=message):
        studio.edit_shot(b, b['nodes'][-1]['data']['shots'][0]['source_shot'], {'lines': [line]})


def test_an_unnamed_voice_and_action_beats_can_be_written():
    b = board()
    n = b['nodes'][-1]['data']['shots'][0]['source_shot']
    out, _ = studio.edit_shot(b, n, {'lines': [{'line': 'Harder.', 'delivery': 'offscreen'}], 'action': ['  He turns. ', '']})
    shot = out['nodes'][-1]['data']['shots'][0]
    assert shot['dialogue'] == [{'who': 'Unidentified voice', 'line': 'Harder.', 'delivery': 'offscreen'}]
    assert shot['action'] == ['He turns.'] and out['sourceVerification']['locked_shots'][str(n)]['action'] == ['He turns.']


def _job(kind, node, status, minutes, slot='', result=None, error=None):
    return AutomationJob(id=uuid.uuid4(), project_id=uuid.uuid4(), kind=kind, node_id=node, slot=slot, request_key=str(uuid.uuid4()),
                         payload={}, status=status, result=result or {}, error=error,
                         created_at=datetime(2026, 10, 12, tzinfo=timezone.utc) + timedelta(minutes=minutes),
                         updated_at=datetime(2026, 10, 12, tzinfo=timezone.utc) + timedelta(minutes=minutes))


def test_a_clip_is_done_by_its_newest_render_and_failed_by_a_newer_failure():
    jobs = [_job('write', 'vid:c1', 'succeeded', 0, 'prompt', {'prompt': 'p'}),
            _job('clip', 'vid:c1', 'succeeded', 1, result={'url': 'https://r/1.mp4'}),
            _job('audio_cleanup', 'vid:c1', 'succeeded', 2, 'audio', {'url': 'https://r/1-clean.mp4', 'native_url': 'https://r/1.mp4'})]
    st = studio.clip_state('c1', jobs, {}, set())
    assert st['status'] == 'done' and st['url'] == 'https://r/1-clean.mp4' and st['has_prompt']
    jobs.append(_job('plate', 'vid:c1', 'unknown', 3, 'staging', error='avis: 403 your account has an overdue balance'))
    st = studio.clip_state('c1', jobs, {}, set())
    assert st['status'] == 'failed' and 'nợ quá hạn' in st['hint'] and st['unknown_jobs'][0]['kind'] == 'plate'
    assert studio.clip_state('c2', [], {'c2': ['Lời thoại khác nguồn']}, set())['status'] == 'held'
    assert studio.clip_state('c3', [], {}, {'c3'})['status'] == 'reviewing'


def test_a_chosen_image_replaces_a_sheet_and_ignores_its_earlier_drawings():
    b = fixture()
    node = next(n for n in b['nodes'] if n['data'].get('kind') == 'character')
    old = _job('plate', node['id'], 'succeeded', 0, 'identity')
    out = studio.use_image(b, node['id'], 'identity', 'https://lib/hero.png', 'm1', [old])
    slot = next(n for n in out['nodes'] if n['id'] == node['id'])['data']['identity']
    assert slot['referenceUrl'] == 'https://lib/hero.png' and slot['uploaded'] and str(old.id) in slot['ignoredRuntimeJobIds']


def test_a_material_note_and_image_model_reopen_the_clips_that_use_it():
    b = fixture()
    node = next(n for n in b['nodes'] if n['data'].get('kind') == 'character')
    out, impact = studio.set_material(b, node['id'], target_description='  Blonde,  blue eyes ', image_model='gemini-3-pro-image')
    d = next(n for n in out['nodes'] if n['id'] == node['id'])['data']
    assert d['character']['target_description'] == 'Blonde, blue eyes' and d['imageModel'] == 'gemini-3-pro-image'
    assert impact['clips'] == [b['nodes'][-1]['data']['sequence']['key']]
    with pytest.raises(ValueError, match='không hỗ trợ'):
        studio.set_material(b, node['id'], image_model='midjourney')
    out, _ = studio.set_material(out, node['id'], target_description='', image_model='')
    d = next(n for n in out['nodes'] if n['id'] == node['id'])['data']
    assert 'target_description' not in d['character'] and 'imageModel' not in d


def test_a_materials_own_image_model_draws_its_sheet_and_leaves_other_signatures_alone():
    from flowboard.services import production_run as run
    from tests.test_reskin import packages
    b = fixture()
    specs = run.material_specs(b, packages(b))
    key, spec = next((k, s) for k, s in specs.items() if s['kind'] == 'character')
    assert 'image_model' not in spec and run.material_payload(spec, [], b)['image_model'] == b.get('imageModel', run.automation.DEFAULT_IMAGE_MODEL)
    next(n for n in b['nodes'] if n['id'] == spec['node_id'])['data']['imageModel'] = 'gemini-3-pro-image'
    spec = run.material_specs(b, packages(b))[key]
    assert run.material_payload(spec, [], b)['image_model'] == 'gemini-3-pro-image'


@pytest.mark.asyncio
async def test_a_film_that_pauses_for_review_waits_before_any_production(monkeypatch):
    from types import SimpleNamespace
    from flowboard.services import source_film, production_run
    from flowboard.routes import video_analysis
    states, started = [], []
    monkeypatch.setattr(source_film, 'state', lambda vid, **kw: states.append(kw))
    monkeypatch.setattr(video_analysis, '_update', lambda *a, **kw: None)
    monkeypatch.setattr(production_run, 'start', lambda *a, **kw: started.append(a) or {'id': 'run'})
    row = SimpleNamespace(id=uuid.uuid4(), automation_project_id=uuid.uuid4(), options={'auto_production': {}})
    opt = source_film.FilmOptions(pipeline='reskin_v1', pause_for_review=True)
    await source_film.finish_board(row, opt, {})
    assert states[-1]['stage'] == 'awaiting_review' and not started


def two_clip_board():
    from tests.test_reskin_review_policy import two_clips
    return two_clips()


def test_a_clip_splits_at_a_shot_into_two_clips_in_film_order():
    b = fixture()
    key = b['nodes'][-1]['data']['sequence']['key']
    out, impact = studio.split_clip(b, key, 2)
    seqs = studio.sequences(out)
    assert [studio._key(s) for s in seqs] == [key, key + 'b'] and impact == {'clips': [key, key + 'b'], 'review': True}
    first, second = seqs
    assert len(first['data']['shots']) == 1 and len(second['data']['shots']) == 1
    assert second['data']['sequence']['duration_s'] == second['data']['shots'][0]['duration_s']
    ids = [n['id'] for n in out['nodes']]
    assert ids.index('seq:' + key) < ids.index('seq:' + key + 'b') and 'vid:' + key + 'b' in ids
    assert {'source': 'seq:' + key + 'b', 'target': 'vid:' + key + 'b'}.items() <= next(
        e for e in out['edges'] if e['target'] == 'vid:' + key + 'b' and e['source'].startswith('seq:')).items()
    with pytest.raises(ValueError, match='từ thứ 2'):
        studio.split_clip(b, key, 1)


def test_two_neighbouring_clips_merge_back_when_they_fit_one_clip():
    b = fixture()
    key = b['nodes'][-1]['data']['sequence']['key']
    split, _ = studio.split_clip(b, key, 2)
    merged, impact = studio.merge_clips(split, key)
    assert [studio._key(s) for s in studio.sequences(merged)] == [key]
    seq = next(n for n in merged['nodes'] if n['id'] == 'seq:' + key)
    assert [s['source_shot'] for s in seq['data']['shots']] == [s['source_shot'] for s in b['nodes'][-1]['data']['shots']]
    assert impact['removed'] == [key + 'b'] and not any('clip' in e['id'] and key + 'b' in e['id'] for e in merged['edges'])
    with pytest.raises(ValueError, match='clip cuối'):
        studio.merge_clips(merged, key)


def test_a_merge_that_would_pass_the_reskin_limit_or_skip_source_time_is_refused():
    b = two_clip_board()
    with pytest.raises(ValueError, match='liền nhau|giới hạn'):
        studio.merge_clips(b, 'c1')


def test_a_clip_takes_the_owners_prompt_and_can_return_to_the_writer():
    b = fixture()
    key = b['nodes'][-1]['data']['sequence']['key']
    with pytest.raises(ValueError, match='quá ngắn'):
        studio.set_clip(b, key, manual_prompt='Too short')
    text = 'Create a 8-second sequence. ' * 12
    out = studio.set_clip(b, key, manual_prompt=text, end_state='She waits.')
    video = next(n for n in out['nodes'] if n['id'] == 'vid:' + key)['data']
    assert video['promptBy'] == 'manual' and video['prompt'] == text.strip() and video['endState'] == 'She waits.'
    back = studio.set_clip(out, key, auto_prompt={'prompt': 'Written prompt', 'writer': 'gpt-6-luna', 'end_state': 'x'})
    video = next(n for n in back['nodes'] if n['id'] == 'vid:' + key)['data']
    assert video['promptBy'] == 'gpt-6-luna' and video['prompt'] == 'Written prompt'
    cleared = studio.set_clip(out, key, auto_prompt={})
    assert 'promptBy' not in next(n for n in cleared['nodes'] if n['id'] == 'vid:' + key)['data']


def test_a_clip_can_take_the_pose_motion_profile_and_only_its_motion_payload_changes():
    from flowboard.services import reskin
    from tests.test_reskin import packages
    b = fixture()
    p = packages(b)[0]
    before = reskin.motion_payload(b, p)
    assert before['fps'] == 30 and 'render' not in before
    out = studio.set_clip(b, p['sequence_key'], motion_profile='pose')
    after = reskin.motion_payload(out, packages(out)[0])
    assert after['render'] == reskin.POSE_RENDER and after['fps'] == 30
    assert reskin.motion_payload({**b, 'motionProfile': 'fast'}, p)['render'] == reskin.FAST_RENDER
    with pytest.raises(ValueError, match='không hỗ trợ'):
        studio.set_clip(b, p['sequence_key'], motion_profile='mocap')
