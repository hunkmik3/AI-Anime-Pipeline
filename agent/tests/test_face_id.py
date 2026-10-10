"""Faces as identity evidence: a scene whose faces are not the person's earlier faces is reported."""
import numpy as np
import pytest

from flowboard.services.video_analyzer import face_id, face_id_worker


def unit(v):
    return list(v / np.linalg.norm(v))


def people(seed=3):
    rng = np.random.default_rng(seed)
    a, b = rng.normal(size=(2, 128))
    return rng, a / np.linalg.norm(a), b / np.linalg.norm(b)


def face(rng, who, noise=.25):
    return [{'box': [0, 0, 100, 100], 'score': .99, 'embedding': unit(who + rng.normal(size=128) * noise / np.sqrt(128) * 4)}]


def film(second_scene_face):
    """Lily alone in shots 1–6 (scene A), then alone in shots 7–9 (scene B)."""
    rng, lily, other = people()
    rows, faces = [], {}
    for n in range(1, 10):
        scene = 'A' if n <= 6 else 'B'
        rows.append((n, {'scene_id': scene, 'asset_presence': [{'asset_id': 'lily', 'visibility': 'visible'}],
                         'character_states': {'lily': 'x'}}))
        who = lily if scene == 'A' else (other if second_scene_face == 'other' else lily)
        faces[f'shot-{n}-frame-1'] = face(rng, who)
    return rows, faces


ASSETS = {'lily': {'id': 'lily', 'kind': 'character'}}


def test_a_scene_showing_someone_else_under_a_known_id_is_reported():
    rows, faces = film('other')
    issues = face_id.identity_issues(rows, faces, ASSETS, check_from=7)
    assert [(i['code'], i['shot']) for i in issues] == [('face_contradicts_identity', 7)]
    assert issues[0]['kind'] == 'wardrobe'                 # soft: one repair, then a note
    assert face_id.audit({'assets': list(ASSETS.values()), 'shots': dict((str(n), r) for n, r in rows)}, faces)[
        'faces_not_matching'][0]['scene'] == 'B'


def test_the_same_face_in_a_new_scene_and_a_first_appearance_are_fine():
    rows, faces = film('lily')
    assert face_id.identity_issues(rows, faces, ASSETS) == []
    rows, faces = film('other')
    assert face_id.identity_issues(rows[:6], faces, ASSETS) == []       # scene A has nothing earlier to match


def test_shots_with_two_people_or_two_faces_are_not_samples():
    rows, faces = film('other')
    for n, row in rows[6:]:
        row['asset_presence'].append({'asset_id': 'damian', 'visibility': 'visible'})
    assets = {**ASSETS, 'damian': {'id': 'damian', 'kind': 'character'}}
    assert face_id.identity_issues(rows, faces, assets) == []
    rows, faces = film('other')
    for n in range(7, 10):
        faces[f'shot-{n}-frame-1'] = faces[f'shot-{n}-frame-1'] * 2
    assert face_id.identity_issues(rows, faces, ASSETS) == []


def test_a_reference_whose_faces_disagree_is_not_used():
    rng, lily, other = people()
    rows, faces = film('other')
    for n in range(1, 7):                                   # stylised faces: no stable identity
        faces[f'shot-{n}-frame-1'] = [{'box': [0, 0, 100, 100], 'score': .9, 'embedding': unit(rng.normal(size=128))}]
    assert face_id.identity_issues(rows, faces, ASSETS) == []


def test_the_worker_finds_no_face_in_a_blank_frame(tmp_path):
    import cv2
    path = tmp_path / 'blank.jpg'
    cv2.imwrite(str(path), np.full((360, 640, 3), 128, dtype=np.uint8))
    out = face_id_worker.run([{'id': 'shot-1-frame-1', 'path': str(path)}, {'id': 'shot-1-frame-2', 'path': str(tmp_path / 'missing.jpg')}])
    assert out['faces'] == {'shot-1-frame-1': [], 'shot-1-frame-2': None}
