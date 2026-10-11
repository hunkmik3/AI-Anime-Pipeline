"""Reskin source findings hold only their own clip, and only when the clay cannot carry them.

The white-model video carries cuts, camera, positions and set geometry; the words, the
speakers and who wears what must be settled before a clip is generated. The model's
free-text notes are shown, never a hold. The owner can accept a finding for exactly
that receipt; an edited shot asks again."""
from copy import deepcopy

import pytest

from flowboard.services import reskin, reskin_review_policy as policy, reskin_source_review as review
from flowboard.services import production_manifest as pm, production_run as run
from tests.test_production_run import project, start, get, tick, pending, all_jobs
from tests.test_reskin import fixture, observations, packages, config, local_ready, finish_reskin  # noqa: F401


def shots_of(b, key):
    return next(n['data']['shots'] for n in b['nodes'] if n.get('id') == 'seq:' + key)


def receipt(b, key):
    return next(r for r in b['reskinSourceReview']['receipts'] if r['clip'] == key)


def reviewed(b):
    b['reskinSourceReview'] = {'status': 'reviewed', 'input_digest': pm.digest(reskin.source_contract(b)), 'findings': [],
        'receipts': [{'clip': k, 'input_digest': 'segment-' + k, 'source_sha256': 'test-sha',
                      'observations': observations(shots_of(b, k), k)} for k in ('c1', 'c2')]}
    return b


def two_clips():
    b = fixture()
    video = deepcopy(next(n for n in b['nodes'] if n['id'] == 'vid:c1'))
    video['id'] = 'vid:c2'
    seq = deepcopy(next(n for n in b['nodes'] if n['id'] == 'seq:c1'))
    seq['id'] = 'seq:c2'
    seq['data']['sequence']['key'] = 'c2'
    for i, s in enumerate(seq['data']['shots']):
        s.update(source_start=8. + 4 * i, source_end=12. + 4 * i, source_shot=3 + i, source_evidence=[f'f{3 + i}'])
    b['nodes'] += [video, seq]
    b['sourceVerification']['reviewed_shots'] = [1, 2, 3, 4]
    b['sourceVerification']['evidence'] += [{'id': 'f3', 'shot': 3}, {'id': 'f4', 'shot': 4}]
    return reviewed(b)


def criterion(b, key, name, status='fail'):
    receipt(b, key)['observations']['shots'][0]['criteria'][name]['status'] = status


def test_spellings_of_one_spoken_form_agree_and_interjections_only_warn():
    assert policy.word_difference(policy.spoken("I gotta go, it's late"), policy.spoken('Aye got to go it is late')) is None
    assert policy.word_difference(policy.spoken('I see.'), policy.spoken('Oh, I see.')) == 'speech_interjection'
    assert policy.word_difference(policy.spoken('I see.'), policy.spoken('You see.')) == 'speech_words'


def test_model_notes_and_motion_carried_findings_never_hold_a_clip():
    b = two_clips()
    receipt(b, 'c1')['observations']['findings'] = ['The candidate shotlist is an accurate representation of the excerpt.']
    for name in policy.MOTION_CARRIED:
        criterion(b, 'c1', name)
    assert reskin.split_readiness(b, packages(b)) == ([], {})
    report = run.preview(b, 'film', config())
    assert report['ready'] and 'source_holds' not in report


def test_a_speech_or_identity_finding_holds_only_its_own_clip():
    b = two_clips()
    criterion(b, 'c1', 'identity_wardrobe')
    shared, held = reskin.split_readiness(b, packages(b))
    assert shared == [] and list(held) == ['c1'] and 'c1:1' in held['c1'][0]
    report = run.preview(b, 'film', config())
    assert report['ready'] and list(report['source_holds']) == ['c1']
    criterion(b, 'c2', 'speech_delivery', 'unclear')
    report = run.preview(b, 'film', config())
    assert not report['ready'] and {i['clip'] for i in report['issues'] if i['code'] == 'reskin_source'} == {'c1', 'c2'}


def test_an_edit_anywhere_asks_for_a_review_of_the_board():
    b = two_clips()
    shots_of(b, 'c2')[0]['action'] = ['A different action']
    shared, _ = reskin.split_readiness(b, packages(b))
    assert shared and 'đối chiếu' in shared[0]


def test_the_owners_acceptance_releases_a_clip_until_its_receipt_changes():
    b = two_clips()
    criterion(b, 'c1', 'speech_delivery')
    rows = [{**s, 'id': f'c1:{i + 1}'} for i, s in enumerate(shots_of(b, 'c1'))]
    found = review.validate(receipt(b, 'c1')['observations'], rows, 0)
    b['reskinSourceReview']['findings'] = [{'clip': 'c1', **f} for f in policy.annotate(found, 'segment-c1')]
    keys = [f['key'] for f in b['reskinSourceReview']['findings'] if f['severity'] == 'blocking']
    assert keys and reskin.split_readiness(b, packages(b))[1].keys() == {'c1'}
    with pytest.raises(ValueError, match='không còn'):
        policy.accept(b, ['not-a-finding'], 'owner')
    b = policy.accept(b, keys, 'owner', 'ASR slip; the line is right.')
    assert reskin.split_readiness(b, packages(b)) == ([], {})
    assert b[policy.BOARD_KEY][keys[0]]['by'] == 'owner'
    receipt(b, 'c1')['input_digest'] = 'segment-c1-after-an-edit'
    assert reskin.split_readiness(b, packages(b))[1].keys() == {'c1'}


def dialogue_board():
    b = two_clips()
    first, second = shots_of(b, 'c1')
    first['dialogue'] = [{'who': 'HERO', 'speaker_id': 'hero', 'delivery': 'offscreen', 'line': 'Come here'}]
    second['dialogue'] = [{'who': 'HERO', 'speaker_id': 'hero', 'delivery': 'on_camera', 'line': 'now.'}]
    return b, [{**s, 'id': f'c1:{i + 1}'} for i, s in enumerate(shots_of(b, 'c1'))]


def test_delivery_across_a_cut_warns_but_a_wrong_speaker_holds():
    b, rows = dialogue_board()
    answer = observations(shots_of(b, 'c1'))
    answer['heard_dialogue'] = [{'speaker': 'hero', 'speaker_id': 'hero', 'delivery': 'on_camera',
                                 'text': 'Come here now.', 'start': 3.0, 'end': 4.6}]
    found = review.validate(answer, rows, 0)
    assert [(f['origin'], policy.severity(f)) for f in found] == [('speech_delivery_cut', 'warning')]
    answer['heard_dialogue'][0]['speaker_id'] = 'crowd'
    found = review.validate(answer, rows, 0)
    assert [(f['origin'], policy.severity(f)) for f in found] == [('speech_speaker', 'blocking')]
    answer['heard_dialogue'][0].update(speaker_id='hero', text='Come here now, gotta go.')
    found = review.validate(answer, rows, 0)
    assert [(f['origin'], policy.severity(f)) for f in found] == [('speech_words', 'blocking')]


def test_a_run_produces_clean_clips_and_stops_for_the_held_one(local_ready):
    b = two_clips()
    criterion(b, 'c1', 'identity_wardrobe')
    pid = project(b)
    a = start(pid, mode='render', audio_cleanup=True, set_continuity=False)
    for _ in range(60):
        tick(a['id'])
        if get(a['id']).status != 'running':
            break
        for job in pending(pid):
            finish_reskin(job)
    out = get(a['id'])
    assert out.status == 'blocked' and out.result['stage'] == 'source_review', out.error
    assert list(out.result['source_holds']) == ['c1']
    made = {j.node_id for j in all_jobs(pid) if j.kind in ('clip', 'write', 'motion_reference')}
    assert made == {'vid:c2'}
    assert not any(j.kind == 'assemble' for j in all_jobs(pid))           # never a film with a hole


def with_letter_for_c2(b):
    """A prop only clip c2 shows, whose sheet is not drawn yet."""
    box = next(n for n in b['nodes'] if n['id'] == 'asset:box')
    letter = deepcopy(box)
    letter['id'] = 'asset:letter'
    letter['data']['asset'].update(key='letter', id='letter', name='letter')
    for slot in ('identity', 'plate'):
        letter['data'][slot] = {'prompt': box['data'][slot].get('prompt', '')}
    letter['data']['states'] = {}
    b['nodes'].insert(0, letter)
    if any(a.get('id') == 'box' for a in b.get('productionAssets', [])):
        asset = deepcopy(next(a for a in b['productionAssets'] if a.get('id') == 'box'))
        asset.update(id='letter', name='letter')
        b['productionAssets'].append(asset)
    for s in shots_of(b, 'c2'):
        s['asset_presence'].append({'asset_id': 'letter', 'visibility': 'visible'})
    return reviewed(b)


def drive(pid, run_id, finish, rounds=60):
    for _ in range(rounds):
        tick(run_id)
        if get(run_id).status != 'running':
            break
        for job in pending(pid):
            finish(job)
    return get(run_id)


def test_a_clip_whose_sheets_are_ready_goes_on_while_another_waits_for_its_own(local_ready):
    b = with_letter_for_c2(two_clips())
    pid = project(b)
    a = start(pid, mode='render', audio_cleanup=True, set_continuity=False)
    held = []

    def finish(job):
        if job.kind == 'plate' and job.slot != 'staging' and 'letter' in job.node_id:
            held.append(job)                                  # the letter sheet stays in the provider
            return
        finish_reskin(job)
    out = drive(pid, a['id'], finish, rounds=25)
    assert out.status == 'running' and held, out.error
    made = {j.node_id for j in all_jobs(pid) if j.kind == 'clip'}
    assert made == {'vid:c1'} and out.result['material_ready_clips'] == ['c1']
    finish_reskin(held[0])
    out = drive(pid, a['id'], finish_reskin)
    assert out.status == 'succeeded', out.error
    assert {j.node_id for j in all_jobs(pid) if j.kind == 'clip'} == {'vid:c1', 'vid:c2'}


def test_one_clips_refusal_holds_only_that_clip(local_ready):
    from tests.test_production_run import complete
    b = two_clips()
    pid = project(b)
    a = start(pid, mode='render', audio_cleanup=True, set_continuity=False)

    def finish(job):
        if job.kind == 'write' and job.node_id == 'vid:c1':
            from flowboard.db import get_session
            from flowboard.db.models import AutomationJob
            with get_session() as s:
                row = s.get(AutomationJob, job.id); row.status = 'failed'; row.error = '422: Provider declined this content request'
                s.add(row); s.commit()
            return
        finish_reskin(job)
    out = drive(pid, a['id'], finish)
    assert out.status == 'blocked' and out.result['failed_clips'] == ['c1'], out.error
    assert {j.node_id for j in all_jobs(pid) if j.kind == 'clip'} == {'vid:c2'}
    assert not any(j.kind == 'assemble' for j in all_jobs(pid))


def test_materials_follow_the_clips_but_never_precede_what_they_depend_on():
    specs = {'env': {'deps': []}, 'env:reverse': {'deps': ['env']}, 'hero': {'deps': []},
             'hero:wet': {'deps': ['hero']}, 'letter': {'deps': []}}
    packs = [{'sequence_key': 'c1'}, {'sequence_key': 'c2'}]
    needs = {'c1': {'hero:wet', 'env:reverse'}, 'c2': {'letter', 'hero', 'env'}}
    order = list(run.clip_order(specs, packs, needs))
    assert sorted(order) == sorted(specs)
    assert all(order.index(d) < order.index(k) for k, v in specs.items() for d in v['deps'])
    assert order.index('letter') == len(order) - 1                   # only clip 2 needs it


def test_sheets_draw_while_the_source_is_reviewed_and_clips_follow_their_receipts(local_ready):
    from datetime import datetime, timezone
    from flowboard.db import get_session
    from flowboard.db.models import AutomationProject
    b = with_letter_for_c2(two_clips())
    report = b['reskinSourceReview']
    b['reskinSourceReview'] = {'status': 'in_progress', 'input_digest': report['input_digest'],
                               'started_at': datetime.now(timezone.utc).isoformat(), 'receipts': [], 'findings': []}
    pid = project(b)
    a = start(pid, mode='render', audio_cleanup=True, set_continuity=False)
    out = drive(pid, a['id'], finish_reskin, rounds=8)
    kinds = {(j.kind, j.slot) for j in all_jobs(pid)}
    assert out.status == 'running' and out.result['source_pending'] == ['c1', 'c2'], out.error
    assert ('plate', 'plate') in kinds or ('plate', 'identity') in kinds            # the letter's sheet
    assert not {k for k, _ in kinds} & {'motion_reference', 'write', 'clip'}
    with get_session() as s:
        p = s.get(AutomationProject, pid)
        p.board = {**p.board, 'reskinSourceReview': report}; p.revision += 1; s.add(p); s.commit()
    out = drive(pid, a['id'], finish_reskin)
    assert out.status == 'succeeded', out.error
    assert {j.node_id for j in all_jobs(pid) if j.kind == 'clip'} == {'vid:c1', 'vid:c2'}
