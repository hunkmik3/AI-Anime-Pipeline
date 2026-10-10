"""Long films: cut detection's time limit, one-pass motion registration, and keyframe serving."""
import pytest
from fastapi import HTTPException

from flowboard.db import get_session
from flowboard.db.models import VideoAnalysis
from flowboard.routes import video_analysis as routes
from flowboard.services.video_analyzer import pipeline


def test_the_cut_detection_limit_grows_with_the_film(monkeypatch):
    monkeypatch.delenv('FLOWBOARD_MEASURE_TIMEOUT', raising=False)
    assert pipeline.measure_timeout(346) == 1800                 # a 6-minute short keeps 30 minutes
    assert pipeline.measure_timeout(88 * 60) == 1.5 * 88 * 60    # an 88-minute film gets 2 h 12 min
    assert pipeline.measure_timeout(0) == 1800
    monkeypatch.setenv('FLOWBOARD_MEASURE_TIMEOUT', '9000')
    assert pipeline.measure_timeout(60) == 9000


def test_many_candidates_are_registered_in_one_pass_like_one_at_a_time(tmp_path):
    import cv2
    import numpy as np
    from flowboard.services.video_analyzer import cuts
    rng = np.random.default_rng(42)
    source = cv2.normalize(cv2.GaussianBlur(rng.integers(20, 230, (320, 600), dtype=np.uint8), (5, 5), 0),
                           None, 0, 255, cv2.NORM_MINMAX)
    frames = [source[:, i * 4:i * 4 + 240] for i in range(40)] + [rng.integers(0, 255, (320, 240), dtype=np.uint8)] * 8
    path = tmp_path / 'pan.avi'
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*'MJPG'), 30, (240, 320))
    for frame in frames:
        writer.write(cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR))
    writer.release()
    together = cuts.registered_motions(path, [30, 10, 40, 0])
    assert set(together) == {0, 10, 30, 40}
    for frame in (10, 30, 40):
        assert together[frame] == pytest.approx(cuts.registered_motion(path, frame))
    assert together[10] > .94 and together[30] > .94 and together[40] < .94 and together[0] == 0.0


def _row(frames):
    with get_session() as s:
        row = VideoAnalysis(name='Frames', filename='source.mp4', status='analysed',
                            analysis={'shots': [{'shot': 1, 'start': 0, 'end': 1, 'frames': frames}]})
        s.add(row); s.commit(); s.refresh(row)
        return row.id


def test_keyframes_are_served_from_the_folder_the_analysis_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, '_work_dir', lambda _: tmp_path)
    (tmp_path / 'frames-v3-abc123def456').mkdir()
    (tmp_path / 'frames-v3-abc123def456' / 'shot001_1.jpg').write_bytes(b'new')
    (tmp_path / 'frames').mkdir()
    (tmp_path / 'frames' / 'shot001_1.jpg').write_bytes(b'old')
    key = _row(['frames-v3-abc123def456/shot001_1.jpg'])
    assert routes.frame(key, 'shot001_1.jpg', user=None).path == tmp_path / 'frames-v3-abc123def456' / 'shot001_1.jpg'
    legacy = _row(['frames/shot001_1.jpg'])
    assert routes.frame(legacy, 'shot001_1.jpg', user=None).path == tmp_path / 'frames' / 'shot001_1.jpg'
    with pytest.raises(HTTPException) as missing:
        routes.frame(key, 'shot002_1.jpg', user=None)
    assert missing.value.status_code == 404


def test_the_slower_fallback_catalog_model_gets_its_own_time_limit(monkeypatch):
    from flowboard.services.video_analyzer import one_pass_film as film
    for name in ('FLOWBOARD_ONE_PASS_CATALOG_MODEL', 'FLOWBOARD_ONE_PASS_CATALOG_TIMEOUT',
                 'FLOWBOARD_ONE_PASS_CATALOG_FALLBACK_TIMEOUT'):
        monkeypatch.delenv(name, raising=False)
    assert film.catalog_timeout('gpt-6-luna') == 180
    assert film.catalog_timeout('claude-opus-5-5') == 600     # ~12k tokens of JSON takes it 2.5 min
    monkeypatch.setenv('FLOWBOARD_ONE_PASS_CATALOG_FALLBACK_TIMEOUT', '5000')
    assert film.catalog_timeout('claude-opus-5-5') == 900
