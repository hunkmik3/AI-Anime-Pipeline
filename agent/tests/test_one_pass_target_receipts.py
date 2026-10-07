from copy import deepcopy

from flowboard.services import production_adaptation
from flowboard.services.video_analyzer import one_pass_film as film


def fixture():
    appearance = {'source_shot': 1, 'asset_presence': [{'asset_id': 'woman', 'state': 'dark hair', 'visibility': 'visible'}]}
    shot = {'source_shot': 1, 'duration_s': 2,
            'framing': 'CU', 'camera': 'static', 'framing_note': 'face',
            'action': ['her lips move'], 'character_states': {'woman': 'day'},
            'dialogue': [{'who': 'Man', 'line': 'Hello.', 'delivery': 'on_camera'}],
            'source_appearances': [appearance],
            'asset_presence': [{'source_shot': 1, **appearance['asset_presence'][0]}]}
    report = {'locked_shots': {'1': {**{k: deepcopy(shot[k]) for k in film.LOCK_FIELDS}, 'duration_s': 2}}, 'shot_digests': {'1': 'source-lock'}}
    shot['production_adaptation'] = {'schema_version': 1, 'reason': 'Owner chose blonde target hair; clarify cropped speaker mouth.',
        'source_digest': production_adaptation.source_digest(shot),
        'asset_presence': [{'source_shot': 1, 'asset_id': 'woman', 'state': 'blonde hair'}],
        'shot_overrides': {'dialogue': [{'who': 'Man', 'line': 'Hello.', 'delivery': 'offscreen'}]}}
    return shot, report


def receipt(shot, report):
    shot['target_adaptation_receipt'] = {'overlay_digest': film.digest(shot['production_adaptation']), 'basis': 'Explicit target casting choice and observed crop.'}


def test_explicit_target_edit_requires_exact_receipt_without_mutating_source():
    shot, report = fixture()
    assert film.validate_locked([shot], report)
    receipt(shot, report)
    before = deepcopy(shot)
    assert not film.validate_locked([shot], report)
    assert shot == before
    shot['production_adaptation']['asset_presence'][0]['state'] = 'red hair'
    assert film.validate_locked([shot], report)


def test_receipt_does_not_allow_source_lock_changes_or_new_dialogue():
    shot, report = fixture()
    shot['production_adaptation']['shot_overrides']['dialogue'][0]['line'] = 'Different words.'
    receipt(shot, report)
    assert any('dialogue words' in x for x in film.validate_locked([shot], report))
    shot, report = fixture()
    shot['production_adaptation']['shot_overrides']['camera'] = 'new angle'
    receipt(shot, report)
    assert any('camera' in x for x in film.validate_locked([shot], report))
    shot, report = fixture()
    receipt(shot, report)
    shot['duration_s'] = 3
    assert any('duration' in x for x in film.validate_locked([shot], report))
    shot, report = fixture()
    receipt(shot, report)
    shot['source_appearances'][0]['asset_presence'][0]['state'] = 'changed source'
    assert any('stale' in x for x in film.validate_locked([shot], report))
