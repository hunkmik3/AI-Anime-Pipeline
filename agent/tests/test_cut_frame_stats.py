"""The cut detector's stats pass resizes frames in a thread pool: the same arrays, in order."""
import shutil
import subprocess

import numpy as np
import pytest

from flowboard.services.video_analyzer import cuts

cv2 = pytest.importorskip("cv2")


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_parallel_stats_equal_a_sequential_pass(tmp_path, monkeypatch):
    video = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=240x426:rate=30:duration=5",
                    "-pix_fmt", "yuv420p", str(video)], check=True)
    monkeypatch.setattr(cuts, "_STATS_CHUNK", 7)          # chunks that do not divide the frame count
    thumbs, tiny, fps = cuts._frame_stats(video)
    cap = cv2.VideoCapture(str(video))
    frames = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frames.append(frame)
    want_thumbs, want_tiny = cuts._shrink(frames)
    assert fps == 30 and len(thumbs) == len(frames) == 150
    assert np.array_equal(thumbs, np.stack(want_thumbs)) and np.array_equal(tiny, np.stack(want_tiny))


def test_an_empty_video_gives_empty_stats(tmp_path):
    thumbs, tiny, _ = cuts._frame_stats(tmp_path / "missing.mp4")
    assert thumbs.shape == (0, cuts._THUMB[1], cuts._THUMB[0]) and tiny.shape == (0, 27, 48, 3)
