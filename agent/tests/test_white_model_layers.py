"""The clay's opt-in 4D-blocking layers: mannequin limbs and the measured camera."""
import numpy as np

from flowboard.services.reskin_workers import white_model as wm


def person(x=100, y=60, scale=1.0, score=0.9):
    """COCO-17 keypoints of a standing figure."""
    pts = {5: (x - 20, y + 40), 6: (x + 20, y + 40), 7: (x - 28, y + 80), 8: (x + 28, y + 80), 9: (x - 30, y + 115),
           10: (x + 30, y + 115), 11: (x - 14, y + 110), 12: (x + 14, y + 110), 13: (x - 16, y + 160), 14: (x + 16, y + 160),
           15: (x - 16, y + 205), 16: (x + 16, y + 205)}
    kps = np.zeros((17, 2), np.float32)
    for i, (px, py) in pts.items():
        kps[i] = (x + (px - x) * scale, y + (py - y) * scale)
    scores = np.full(17, score, np.float32)
    scores[:5] = 0.1                                            # the face points are never drawn
    return kps, scores


def test_limbs_become_gray_tubes_and_nothing_else_changes():
    image = np.full((260, 200, 3), 128, np.uint8)
    depth = np.full((64, 64), 0.5, np.float32)
    out = wm.mannequin(image, depth, [person()])
    changed = np.any(out != image, axis=2)
    assert changed[195, 85] and changed[130, 100]              # a thigh and the torso
    assert not changed[10, 10] and not changed[250, 190]       # the background is untouched
    assert np.all(out[..., 0] == out[..., 1]) and np.all(out[..., 1] == out[..., 2])  # still monochrome
    assert np.array_equal(wm.mannequin(image, depth, [person(score=0.2)]), image)    # unsure keypoints draw nothing
    assert np.array_equal(wm.mannequin(image, depth, []), image)


def test_people_are_masked_out_of_the_camera_measurement():
    mask = wm.people_mask((260, 200), [person()], [(90, 20, 20, 24)])
    assert mask[150, 100] == 0 and mask[5, 195] == 255


def test_camera_summary_names_the_move_from_the_cameras_side():
    w, h = 480, 270
    pan_right = [(-3.0, 0.0, 1.0, 0.0)] * 20                    # content moves left: the camera pans right
    assert wm.camera_summary(pan_right, w, h)['label'] == 'pan right'
    push = [(0.0, 0.0, 1.004, 0.0)] * 20
    assert wm.camera_summary(push, w, h)['label'] == 'push in'
    still = [(0.05, -0.04, 1.0, 0.01)] * 20
    out = wm.camera_summary(still, w, h)
    assert out['label'] == 'static' and out['measured'] == 20
    rng = np.random.default_rng(1)
    shaky = [(float(rng.normal(0, 4)), float(rng.normal(0, 4)), 1.0, 0.0) for _ in range(30)]
    assert 'handheld' in wm.camera_summary(shaky, w, h)['label']
    assert wm.camera_summary([None] * 10 + pan_right[:2], w, h)['label'] == 'unclear'


def test_camera_step_recovers_a_known_shift():
    rng = np.random.default_rng(0)
    base = (rng.random((300, 400)) * 255).astype(np.uint8)
    import cv2
    base = cv2.GaussianBlur(base, (5, 5), 0)
    moved = np.roll(base, 6, axis=1)                            # content 6 px to the right
    dx, dy, scale, roll = wm.camera_step(base, moved, np.full(base.shape, 255, np.uint8))
    assert abs(dx - 6) < 1 and abs(dy) < 1 and abs(scale - 1) < .01 and abs(roll) < .5
