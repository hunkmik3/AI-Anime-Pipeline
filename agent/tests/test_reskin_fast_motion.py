"""The fast white-model render for long films is opt-in per board; a standard board keeps
the exact payload (and so every motion video and clip already made)."""
import asyncio
import importlib.util
from pathlib import Path

import pytest

from flowboard.services import reskin, reskin_motion
from tests.test_reskin import fixture, packages


def worker():
    path = Path(reskin_motion.__file__).parent / 'reskin_workers' / 'white_model.py'
    spec = importlib.util.spec_from_file_location('white_model_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_standard_board_keeps_its_exact_motion_payload_and_a_fast_one_says_how_to_render():
    b = fixture()
    standard = reskin.motion_payload(b, packages(b)[0])
    assert standard['fps'] == 30 and 'render' not in standard
    assert set(standard) == {'version', 'source_video_id', 'source_sha256', 'start', 'end', 'cuts', 'shot_ids',
                             'source_review_digest', 'fps'}
    b['motionProfile'] = 'fast'
    fast = reskin.motion_payload(b, packages(b)[0])
    assert fast['render'] == reskin.FAST_RENDER and fast['fps'] == 24
    assert {k: v for k, v in fast.items() if k not in ('render', 'fps')} == {k: v for k, v in standard.items() if k != 'fps'}


def test_depth_is_measured_at_each_shots_ends_and_every_stride_frames_never_across_a_cut():
    plan = worker().depth_plan([0] * 7 + [1] * 4 + [2], 2)
    #                 shot 0: 0..6          shot 1: 7..10        shot 2: 11
    assert [i for i, k in enumerate(plan) if k] == [0, 2, 4, 6, 7, 9, 10, 11]
    assert all(worker().depth_plan([0, 0, 1, 1, 1], 1))


@pytest.mark.parametrize('profile', ['standard', 'fast'])
def test_the_worker_gets_render_settings_only_for_a_fast_board(monkeypatch, tmp_path, profile):
    b = fixture()
    if profile == 'fast':
        b['motionProfile'] = 'fast'
    payload = reskin.motion_payload(b, packages(b)[0])
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'source')
    seen = []

    async def spawn(*args, **kw):
        seen.append(args)
        raise RuntimeError('stop before rendering')
    monkeypatch.setattr(reskin, 'source_path', lambda _: source)
    monkeypatch.setattr(reskin, 'source_fingerprint', lambda _: payload['source_sha256'])
    monkeypatch.setattr(reskin_motion, 'STORAGE_DIR', str(tmp_path))
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    with pytest.raises(RuntimeError):
        asyncio.run(reskin_motion.generate(payload))
    args = seen[0]
    assert len(args) == (9 if profile == 'fast' else 8)        # python -u worker src start end cuts out [render]
    if profile == 'fast':
        assert '"depth_stride": 2' in args[-1]
