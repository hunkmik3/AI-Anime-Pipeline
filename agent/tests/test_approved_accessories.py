"""The approved design governs accessories (owner rule, 2026-10-09): a ring, watch or earring
that no approved sheet of the clip carries leaves what the writer, its reviewer and the clip
review read, with a receipt. The stored source and the locked contract never change."""
import asyncio
import copy
import json

import pytest

from flowboard.services import approved_accessories as acc, cinematic_prompt as cp, prompt_writer as writer
from flowboard.services.video_analyzer import adapt
from tests.test_cinematic_prompt import good, inputs


@pytest.mark.parametrize('text, kept', [
    ("same standoff, man's other hand at his side with a ring visible", "same standoff, man's other hand at his side"),
    ('She glances at her watch, then turns away.', 'then turns away.'),
    ('she adjusts her earrings; he waits', 'he waits'),
    ('he pushes his sunglasses up', ''),
    # Not accessories: the verb, a boxing ring, drinking glasses, a watchful look.
    ('the phone rings twice', 'the phone rings twice'),
    ('they watch the door', 'they watch the door'),
    ('a boxing ring behind them', 'a boxing ring behind them'),
    ('two glasses of wine on the table', 'two glasses of wine on the table'),
])
def test_an_unapproved_accessory_leaves_the_text_and_the_rest_stays(text, kept):
    assert acc.scrub_text(text, set())[0] == kept


def test_only_what_a_design_describes_counts_never_what_it_forbids():
    assert acc.carried([{'design': {'avoid': ['added jewellery', 'rings'], 'face': 'watchful eyes'}}]) == set()
    assert acc.carried([{'states': [{'wardrobe': 'black suit, silver watch'}]}]) == {'watch'}
    assert acc.carried([{'name': 'Engagement ring', 'kind': 'prop'}]) == {'ring'}


def test_shots_are_scrubbed_with_receipts_and_dialogue_is_never_touched():
    shots = [{'action': ['He waits', 'his hand at his side with a ring visible'],
              'dialogue': [{'who': 'HE', 'line': 'Put the ring down.'}],
              'asset_presence': [{'asset_id': 'man', 'state': 'Black suit; a gold ring on his finger'}]}]
    out, receipts = acc.apply(shots, [{'key': 'man', 'design': {'costume': ['black suit']}}])
    assert out[0]['action'] == ['He waits', 'his hand at his side']
    assert out[0]['asset_presence'][0]['state'] == 'Black suit; a gold ring on his finger'.split(';')[0]
    assert out[0]['dialogue'] == shots[0]['dialogue'] and shots[0]['action'][1].endswith('ring visible')
    assert [(r['shot'], r['field'], r['accessory']) for r in receipts] == [
        (1, 'action', 'ring'), (1, 'asset_presence.state', 'ring')]
    kept, none = acc.apply(shots, [{'key': 'man', 'design': {'jewellery': 'a thin gold ring'}}])
    assert kept == shots and none == []


def run_writer(monkeypatch, chars):
    sequence, shots, _ = inputs()
    shots[0]['action'] = ['Rhea holds a closed parcel with a ring visible']
    original = copy.deepcopy(shots)
    seen = []

    async def images(refs):
        return [], []

    async def model(system, user, stats, **kw):
        seen.append(json.loads(user))
        stats.answered_by[writer.WRITER_MODEL] = 1
        return {'prompt': good(), 'end_state': 'Rhea retains the closed parcel.', 'staging_decisions': []}

    async def review(prompt, req, refs, assets, **kw):
        seen.append({'review_requirements': req})
        return {'status': 'verified', 'checked_requirement_ids': [r['id'] for r in req], 'findings': []}

    monkeypatch.setattr(cp, 'reference_images', images)
    monkeypatch.setattr(adapt, 'ask_json', model)
    monkeypatch.setattr(writer, 'review_prompt', review)
    result = asyncio.run(writer.write_clip_prompt(sequence, shots, characters=chars, environment=None,
                                                  aspect_ratio='1:1', look='cg3d', cinematic=True))
    assert shots == original
    return result, seen


def test_the_writer_and_its_reviewer_never_see_a_ring_the_approved_sheet_lacks(monkeypatch):
    _, _, chars = inputs()
    result, seen = run_writer(monkeypatch, chars)
    assert seen[0]['shots'][0]['action'] == ['Rhea holds a closed parcel']
    assert 'ring' not in json.dumps(seen[1]['review_requirements'])
    assert result.coverage['appearance_rewrites'][0]['accessory'] == 'ring'
    assert any('bỏ chi tiết ring' in w for w in result.warnings)


def test_a_ring_the_approved_sheet_carries_stays_in_the_shot(monkeypatch):
    _, _, chars = inputs()
    chars[0]['design']['jewellery'] = 'a thin gold ring on her right hand'
    result, seen = run_writer(monkeypatch, chars)
    assert seen[0]['shots'][0]['action'] == ['Rhea holds a closed parcel with a ring visible']
    assert 'appearance_rewrites' not in result.coverage and result.warnings == []


def test_a_persons_own_state_is_weighed_against_that_persons_design():
    shots = [{'action': ['She hands him the tray'],
              'asset_presence': [{'asset_id': 'maid', 'state': 'Uniform; a name tag on her chest'},
                                 {'asset_id': 'attendant', 'state': 'Tunic with name badge, and a crest pin'}]}]
    sheets = [{'key': 'maid', 'design': {'costume': ['uniform']}},
              {'key': 'attendant', 'design': {'costume': ['tunic with a violet name badge']}}]
    out, receipts = acc.apply(shots, sheets)
    assert out[0]['asset_presence'][0]['state'] == 'Uniform'                 # the maid's design has no name tag
    assert out[0]['asset_presence'][1]['state'].startswith('Tunic with name badge')   # the attendant's has one...
    assert 'crest pin' not in out[0]['asset_presence'][1]['state']           # ...but no crest pin
    assert {r['accessory'] for r in receipts} == {'name_badge', 'pin'}


def test_a_target_casting_overlay_stays_anchored_in_the_projected_copy():
    from flowboard.services import production_adaptation
    shot = {'source_shots': [7], 'action': ['She waits'],
            'source_appearances': [{'source_shot': 7, 'asset_presence': [{'asset_id': 'maid', 'state': 'Uniform; a name tag'}]}],
            'asset_presence': [{'asset_id': 'maid', 'source_shot': 7, 'state': 'Uniform; a name tag'}]}
    shot['production_adaptation'] = {'schema_version': 1, 'reason': 'casting', 'shot_overrides': {},
                                     'source_digest': production_adaptation.source_digest(shot),
                                     'asset_presence': [{'source_shot': 7, 'asset_id': 'maid', 'state': 'Blonde; a name tag'}]}
    out, receipts = acc.apply([shot], [{'key': 'maid', 'design': {'costume': ['uniform']}}])
    applied = production_adaptation.apply(out[0])                      # no stale-digest error
    assert 'name tag' not in json.dumps(applied) and receipts
    assert production_adaptation.validate(shot) == []                 # the original is untouched
