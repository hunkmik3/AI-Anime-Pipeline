"""Generate only the opening: the upload keeps N + 30 s of the source for the analysis,
the full file stays beside it, and the production scope is the first N seconds."""
import json
import shutil
import subprocess
import uuid

import pytest
from fastapi.testclient import TestClient

from flowboard.routes import video_analysis as routes

pytestmark = pytest.mark.skipif(not shutil.which('ffmpeg'), reason='ffmpeg not installed')


def clip(path, seconds):
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', f'testsrc2=size=160x284:rate=30:duration={seconds}',
                    '-f', 'lavfi', '-i', f'sine=frequency=440:duration={seconds}', '-shortest',
                    '-pix_fmt', 'yuv420p', '-c:v', 'libx264', '-g', '30', '-c:a', 'aac', str(path)], check=True)


def test_the_opening_is_kept_and_the_original_stays(tmp_path):
    source = tmp_path / 'source.mp4'
    clip(source, 75)
    kept = routes._keep_opening(source, 40)
    assert 39.5 <= kept <= 41.5
    assert (tmp_path / 'source-original.mp4').exists() and sorted(p.name for p in tmp_path.glob('source.*')) == ['source.mp4']


def test_a_failed_cut_keeps_the_whole_upload(tmp_path):
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'not a video')
    assert routes._keep_opening(source, 40) is None
    assert source.read_bytes() == b'not a video' and not (tmp_path / 'source-original.mp4').exists()


def test_an_upload_with_a_window_sets_the_scope_and_analyses_only_the_opening(tmp_path, monkeypatch):
    from flowboard.main import app
    from flowboard.db import get_session
    from flowboard.db.models import VideoAnalysis
    started = []
    monkeypatch.setattr(routes, '_work_dir', lambda vid: tmp_path / str(vid))
    monkeypatch.setattr(routes, '_start', lambda vid, coro, operation=None: (coro.close(), started.append(vid)))
    video = tmp_path / 'upload.mp4'
    clip(video, 75)
    settings = {'pipeline': 'reskin_v1', 'style': 'anime_jp_modern', 'aspect_ratio': '1:1', 'resolution': '480p',
                'clip_seconds': 15, 'kyc': True, 'unmoderated': True, 'generate_seconds': 40}
    with TestClient(app) as client:
        r = client.post('/api/automation/videos', files={'file': ('film.mp4', video.read_bytes(), 'video/mp4')},
                        data={'name': 'film', 'auto_production': json.dumps(settings), 'analysis_mode': 'one_pass'})
    assert r.status_code == 200, r.text
    vid = uuid.UUID(r.json()['id'])
    with get_session() as s:
        auto = s.get(VideoAnalysis, vid).options['auto_production']
    assert auto['generation_scope']['source_end_seconds'] == 40 and auto['generation_scope']['analysis'] == 'full'
    assert 69.5 <= auto['analysis_window']['kept_seconds'] <= 71.5 and started == [vid]
    assert (tmp_path / str(vid) / 'source-original.mp4').exists()
