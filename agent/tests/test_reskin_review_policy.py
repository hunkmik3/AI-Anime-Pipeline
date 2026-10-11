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
