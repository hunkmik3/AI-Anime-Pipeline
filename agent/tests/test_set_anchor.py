"""A location plate anchored on the source film's own widest, emptiest frames of that place."""
import json

import pytest
from PIL import Image

from flowboard.services import production_run, set_anchor


def board(tmp_path, shots):
    """One location, one character; ``shots`` = [(source shot, framing, [(asset, visibility)])]."""
    work = tmp_path / 'storage' / 'video_analysis' / 'vid-1'
    (work / 'frames').mkdir(parents=True)
    for n, _, _ in shots:
        for k in (1, 2, 3):
            Image.new('RGB', (160, 90), (n, k, 0)).save(work / 'frames' / f'shot{n:03d}_{k}.jpg')
    rows = [{'source_shot': n, 'framing': framing, 'environment_key': 'bath',
             'asset_presence': [{'asset_id': a, 'visibility': v} for a, v in people]} for n, framing, people in shots]
    return work, {'sourceVideoId': 'vid-1', 'style': 'live_action_feature', 'nodes': [
        {'id': 'character:lily', 'data': {'kind': 'character', 'character': {'key': 'lily', 'source_asset_id': 'lily'}}},
        {'id': 'environment:bath', 'data': {'kind': 'environment', 'environment': {
            'key': 'bath', 'source_asset_id': 'bath', 'name': 'Bathroom', 'kind': 'environment', 'summary': 'Ornate bathroom.'},
            'plate': {}}},
        {'id': 'seq:c1', 'data': {'kind': 'sequence', 'sequence': {'key': 'c1', 'environment_key': 'bath'}, 'shots': rows}}]}


@pytest.fixture
def storage(tmp_path, monkeypatch):
    import flowboard.config as config
    monkeypatch.setattr(config, 'STORAGE_DIR', tmp_path / 'storage')
    monkeypatch.setattr(set_anchor, '_captioned', lambda board: set())
    return tmp_path


def test_the_widest_emptiest_shots_anchor_and_people_heavy_ones_do_not(storage):
    work, b = board(storage, [(1, 'MS', [('lily', 'visible')]), (2, 'WIDE', []), (3, 'MWS', [('lily', 'visible')]),
                              (4, 'EWS', [('lily', 'visible'), ('lily', 'visible')]), (5, 'CU', [])])
    assert set_anchor.candidate_shots(b, 'bath') == [(50, 2), (36, 4), (28, 3)]
    frames = set_anchor.anchor_frames(b, 'bath')
    assert [f.name for f in frames] == ['shot002_2.jpg', 'shot004_2.jpg']


def test_a_big_face_in_the_frame_drops_a_candidate_when_faces_were_measured(storage):
    work, b = board(storage, [(2, 'WIDE', []), (4, 'WIDE', [])])
    (work / 'face-id').mkdir()
    big = {'box': [0, 0, 60, 60], 'score': .99, 'embedding': []}      # a quarter of a 160x90 frame
    (work / 'face-id' / 'faces-x.json').write_text(json.dumps({'faces': {'shot-2-frame-2': [big]}}))
    assert [f.name for f in set_anchor.anchor_frames(b, 'bath')] == ['shot004_2.jpg', 'shot002_2.jpg']


def test_a_frame_with_someone_bathing_or_undressed_is_never_an_anchor(storage):
    work, b = board(storage, [(2, 'WIDE', [('lily', 'visible')]), (4, 'MWS', []), (6, 'WIDE', [('lily', 'partial')])])
    b['nodes'][0]['data']['character']['states'] = [
        {'key': 'uniform', 'label': 'Uniform', 'wardrobe': 'Dark maid uniform.'},
        {'key': 'wet-bath', 'label': 'In the bath', 'wardrobe': 'Bare shoulders above the bath foam.'}]
    rows = b['nodes'][2]['data']['shots']
    rows[0]['character_states'] = {'lily': 'wet-bath'}
    rows[2]['character_states'] = {'lily': 'wet-bath'}
    assert set_anchor.candidate_shots(b, 'bath') == [(40, 4)]
    rows[0]['character_states'] = {'lily': 'uniform'}           # dressed: an ordinary wide view again
    assert set_anchor.candidate_shots(b, 'bath') == [(40, 4), (38, 2)]


def test_no_frame_good_enough_means_a_plate_from_words_as_before(storage):
    work, b = board(storage, [(1, 'MS', [('lily', 'visible')]), (2, 'CU', [])])
    assert set_anchor.anchor_frames(b, 'bath') == []


def test_a_new_environment_plate_carries_its_anchor_frames(storage, monkeypatch):
    work, b = board(storage, [(2, 'WIDE', []), (4, 'MWS', [])])
    monkeypatch.setattr(set_anchor, 'publish_stable', lambda path: 'https://r2.test/' + path.name)
    env = b['nodes'][1]['data']
    spec = {'item': env['environment'], 'kind': 'environment', 'plate': env['plate'], 'slot': ''}
    payload = production_run.material_payload(spec, [], b)
    assert payload['reference_urls'] == ['https://r2.test/shot002_2.jpg', 'https://r2.test/shot004_2.jpg']
    assert 'location anchor' in payload['prompt'] and 'LOCATION ANCHOR FRAMES' in payload['prompt']
    # An approved, hand-authored plate prompt is never re-anchored.
    spec['plate'] = {'prompt': 'Authored plate.'}
    authored = production_run.material_payload(spec, [], b)
    assert authored['reference_urls'] == [] and 'LOCATION ANCHOR FRAMES' not in authored['prompt']


def test_the_same_frame_always_publishes_under_the_same_name(tmp_path, monkeypatch):
    from flowboard.services.flowstudio import r2
    uploaded = []
    monkeypatch.setattr(r2, 'is_configured', lambda: True)
    monkeypatch.setattr(r2, 'upload_file', lambda path: uploaded.append(path.name) or 'https://r2.test/' + path.name)
    frame = tmp_path / 'shot002_2.jpg'
    Image.new('RGB', (8, 8)).save(frame)
    assert set_anchor.publish_stable(frame) == set_anchor.publish_stable(frame)
    assert uploaded[0].startswith('set-anchor-') and uploaded[0] == uploaded[1]
