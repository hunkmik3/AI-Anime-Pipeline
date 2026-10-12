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
    # Seedance's own audio by default; the music-free version only when the owner chooses it.
    assert st['status'] == 'done' and st['url'] == 'https://r/1.mp4' and st['has_prompt'] and st['audio_choice'] == 'native'
    assert studio.clip_state('c1', jobs, {}, set(), {'audioChoice': 'cleaned'})['url'] == 'https://r/1-clean.mp4'
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


def test_presence_and_wardrobe_edits_move_the_shot_and_its_appearance_together():
    b = board()
    b['productionAssets'].append({'id': 'bag', 'kind': 'prop', 'name': 'Bag'})
    n = b['nodes'][-1]['data']['shots'][0]['source_shot']
    out, _ = studio.edit_shot(b, n, {'presence': [{'asset_id': 'hero', 'visibility': 'offscreen', 'state': 'In the car.', 'position': 'Outside'},
                                                  {'asset_id': 'bag', 'visibility': 'visible', 'state': ' On the seat ', 'holder_id': 'hero'}],
                                     'character_states': {'hero': 'coat'}})
    shot = out['nodes'][-1]['data']['shots'][0]
    assert [(p['asset_id'], p['visibility']) for p in shot['asset_presence']] == [('hero', 'offscreen'), ('bag', 'visible')]
    assert shot['asset_presence'][1]['state'] == 'On the seat' and shot['asset_presence'][1]['holder_id'] == 'hero'
    assert all(p['source_shot'] == n for p in shot['asset_presence'])
    app = shot['source_appearances'][0]
    assert [p['asset_id'] for p in app['asset_presence']] == ['hero', 'bag'] and 'source_shot' not in app['asset_presence'][0]
    assert shot['character_keys'] == [] and shot['character_states'] == {'hero': 'coat'} and app['character_states'] == {'hero': 'coat'}
    assert out['sourceVerification']['shot_digests'][str(n)] == studio.digest({k: v for k, v in app.items() if k != 'source_shot'})
    for bad, message in ((['ghost'], 'danh mục'), (['hero', 'hero'], 'hai lần')):
        with pytest.raises(ValueError, match=message):
            studio.edit_shot(b, n, {'presence': [{'asset_id': a, 'visibility': 'visible'} for a in bad]})
    with pytest.raises(ValueError, match='Trang phục'):
        studio.edit_shot(b, n, {'character_states': {'hero': 'ballgown'}})


def test_a_cut_moves_between_two_shots_and_keeps_every_line_on_its_side():
    b = board()
    shots = b['nodes'][-1]['data']['shots']
    first = shots[0]['source_shot']
    out, impact = studio.edit_shot(b, first, {'lines': [{'who': 'Hero', 'speaker_id': 'hero', 'line': 'Come here', 'delivery': 'offscreen',
                                                          'timing': [1.0, 2.0]}]})
    moved, impact = studio.move_cut(out, first, 3.0)
    a, c = moved['nodes'][-1]['data']['shots']
    assert (a['source_end'], a['duration_s'], c['source_start'], c['duration_s']) == (3.0, 3.0, 3.0, 5.0)
    assert moved['sourceVerification']['locked_shots'][str(first)]['duration_s'] == 3.0
    assert impact['clips'] == [b['nodes'][-1]['data']['sequence']['key']]
    with pytest.raises(ValueError, match='kéo dài qua điểm cắt'):
        studio.move_cut(out, first, 1.5)
    with pytest.raises(ValueError, match='ít nhất 0,3'):
        studio.move_cut(out, first, 7.9)


def test_a_cut_between_two_clips_moves_the_next_clips_own_timings_with_its_start():
    b = two_clip_board()
    last = shots_of(b, 'c1')[-1]
    nxt = shots_of(b, 'c2')[0]
    nxt.update(dialogue=[{'who': 'Hero', 'speaker_id': 'hero', 'line': 'Later', 'delivery': 'on_camera'}],
               performance=['Clip audio 1.000–2.000s: speak this line once.'])
    out, impact = studio.move_cut(b, last['source_shot'], 7.5)
    c2 = shots_of(out, 'c2')[0]
    assert c2['source_start'] == 7.5 and c2['performance'] == ['Clip audio 1.500–2.500s: speak this line once.']
    assert impact['clips'] == ['c1', 'c2']


def shots_of(b, key):
    return next(n['data']['shots'] for n in b['nodes'] if n.get('id') == 'seq:' + key)


def test_every_change_can_be_undone_while_nothing_newer_touched_it():
    b = board()
    n = b['nodes'][-1]['data']['shots'][0]['source_shot']
    edited, _ = studio.edit_shot(b, n, {'action': ['He waves.']})
    entry = studio.change_entry(b, edited, 'shot', 'Shot edit', ['c1'])
    assert entry and list(entry['before']['nodes']) == [b['nodes'][-1]['id']] and str(n) in entry['before']['locks']
    back = studio.undo(edited, entry)
    assert back['nodes'] == b['nodes'] and back['sourceVerification']['locked_shots'] == b['sourceVerification']['locked_shots']
    later, _ = studio.edit_shot(edited, n, {'action': ['He runs.']})
    with pytest.raises(ValueError, match='sửa tiếp'):
        studio.undo(later, entry)
    key = b['nodes'][-1]['data']['sequence']['key']
    split, _ = studio.split_clip(b, key, 2)
    entry = studio.change_entry(b, split, 'split', 'Split', [key])
    restored = studio.undo(split, entry)
    assert [x['id'] for x in restored['nodes']] == [x['id'] for x in b['nodes']]
    assert sorted(map(str, restored.get('edges', []))) == sorted(map(str, b.get('edges', [])))
    assert restored['nodes'] == b['nodes']
    assert studio.change_entry(b, deepcopy(b), 'noop', 'Nothing', []) is None


def test_production_settings_change_after_the_film_exists_and_reach_the_board():
    settings, out, notes = studio.apply_settings({'resolution': '480p'}, {'nodes': []}, {
        'resolution': '720p', 'budget_usd': 25, 'image_fallback_model': 'gemini-3-pro-image', 'style_note': '  warm  film grain ',
        'motion_profile': 'pose'})
    assert settings['resolution'] == '720p' and out['budgetUsd'] == 25.0 and out['imageFallbackModel'] == 'gemini-3-pro-image'
    assert out['styleNote'] == 'warm film grain' and out['motionProfile'] == 'pose' and notes
    _, cleared, _ = studio.apply_settings(settings, out, {'budget_usd': None, 'image_fallback_model': '', 'motion_profile': 'standard'})
    assert 'budgetUsd' not in cleared and 'imageFallbackModel' not in cleared and 'motionProfile' not in cleared
    with pytest.raises(ValueError):
        studio.apply_settings({}, {}, {'resolution': '4k'})


def test_a_render_past_the_owners_cap_is_refused_but_one_already_made_is_not():
    from flowboard.services import production_run as run
    payload = {'duration_seconds': 10, 'resolution': '480p', 'prompt': 'p'}
    made = _job('clip', 'vid:c1', 'succeeded', 0, result={'url': 'u'})
    made.payload = {'duration_seconds': 10, 'resolution': '480p', 'prompt': 'old'}
    with pytest.raises(run.Blocked, match='Vượt trần'):
        run.budget_check({'budgetUsd': 2}, [made], 'vid:c2', payload)
    run.budget_check({'budgetUsd': 2}, [made], 'vid:c1', made.payload)      # this exact render already exists
    run.budget_check({}, [made], 'vid:c2', payload)                         # no cap, no limit


def test_a_clip_shows_the_take_the_owner_chose_and_its_audio_choice():
    first = _job('clip', 'vid:c1', 'succeeded', 0, result={'url': 'https://r/1.mp4'})
    second = _job('clip', 'vid:c1', 'succeeded', 5, result={'url': 'https://r/2.mp4'})
    first.prepared = {'set_aside': True}
    clean = _job('audio_cleanup', 'vid:c1', 'succeeded', 6, 'audio', {'url': 'https://r/2-clean.mp4', 'native_url': 'https://r/2.mp4'})
    jobs = [first, second, clean]
    made = studio.takes('c1', jobs)
    assert [t['render_url'] for t in made] == ['https://r/2.mp4', 'https://r/1.mp4'] and made[1]['set_aside']
    st = studio.clip_state('c1', jobs, {}, set(), {'audioChoice': 'cleaned'})
    assert st['take_id'] == str(second.id) and st['url'] == 'https://r/2-clean.mp4' and st['take_count'] == 2
    st = studio.clip_state('c1', jobs, {}, set(), {'selectedTake': str(first.id), 'audioChoice': 'native'})
    assert st['url'] == 'https://r/1.mp4' and st['take_selected'] and st['audio_choice'] == 'native'
    b = fixture()
    key = b['nodes'][-1]['data']['sequence']['key']
    with pytest.raises(ValueError, match='Không có bản gen'):
        studio.set_audio_and_take(b, key, take_id='nope', known_takes={str(first.id)})
    with pytest.raises(ValueError, match='Âm thanh'):
        studio.set_audio_and_take(b, key, audio_choice='loud')


def test_the_edl_lays_the_clip_files_end_to_end():
    text = studio.edl('Film', [{'file': '001_c1.mp4', 'length_s': 2.5, 'source_start': 0, 'source_end': 2.4, 'label': 'CLIP 01'},
                               {'file': '002_c2.mp4', 'length_s': 1.0, 'source_start': 2.4, 'source_end': 3.4, 'label': 'CLIP 02'}])
    events = [l for l in text.splitlines() if l[:3].isdigit()]
    assert events[0].endswith('00:00:00:00 00:00:02:12') and events[1].endswith('00:00:02:12 00:00:03:12')
    assert '* FROM CLIP NAME: 002_c2.mp4' in text and text.startswith('TITLE: Film')


def test_a_provider_outage_draws_with_the_boards_fallback_model_but_a_refusal_does_not():
    from flowboard.services import automation_jobs
    from tests.test_production_run import project
    pid = project({**fixture(), 'imageFallbackModel': 'gemini-3-pro-image'})
    outage = RuntimeError('avis: 403 The request failed because your account has an overdue balance.')
    assert automation_jobs.image_fallback(pid, 'dola-seedream-5-0-pro', outage) == 'gemini-3-pro-image'
    assert automation_jobs.image_fallback(pid, 'gemini-3-pro-image', outage) is None
    assert automation_jobs.image_fallback(pid, 'dola-seedream-5-0-pro', RuntimeError('content policy: refused')) is None


def test_choosing_a_music_free_clip_cleans_its_chosen_take_once():
    b = fixture()
    key = b['nodes'][-1]['data']['sequence']['key']
    take = _job('clip', 'vid:' + key, 'succeeded', 0, result={'url': 'https://r/take.mp4'})

    class Session:
        def __init__(self): self.added = []
        def add(self, job): self.added.append(job)
    s = Session()
    job = studio.cleanup_job(s, b, uuid.uuid4(), key, [take])
    assert job and job.kind == 'audio_cleanup' and job.payload['url'] == 'https://r/take.mp4' and job.payload['source_job'] == str(take.id)
    running = _job('audio_cleanup', 'vid:' + key, 'running', 1, 'audio')
    running.payload = {'url': 'https://r/take.mp4'}
    assert studio.cleanup_job(s, b, uuid.uuid4(), key, [take, running]) is None          # already being cleaned
    state = studio.clip_state(key, [take, running], {}, set(), {'audioChoice': 'cleaned'})
    assert state['cleaning'] and state['url'] == 'https://r/take.mp4'
