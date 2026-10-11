"""Clip QA hears generated speech in the source film's language: no per-clip language probe."""
import asyncio

import pytest

from flowboard.services import production_quality as quality, production_media as media
from flowboard.services.video_analyzer import asr


@pytest.mark.parametrize('package_language, source_language, expected', [
    ('source', 'en', 'en'), ('vi', 'en', 'vi'), ('source', None, None)])
def test_source_dialogue_is_heard_in_the_source_language(tmp_path, monkeypatch, package_language, source_language, expected):
    monkeypatch.setattr(quality, 'STORAGE_DIR', str(tmp_path))
    async def fetch(url, path): path.write_bytes(b'clip')
    async def probe(path): return {'format': {'duration': '4.0'}, 'streams': [{'codec_type': 'video'}, {'codec_type': 'audio'}]}
    async def command(*args): pass
    heard = []
    async def transcribe(wav, root, language=None):
        heard.append(language)
        raise RuntimeError('stop after the language is chosen')
    monkeypatch.setattr(media, 'fetch', fetch); monkeypatch.setattr(media, 'probe', probe)
    monkeypatch.setattr(media, 'command', command); monkeypatch.setattr(asr, 'transcribe', transcribe)
    package = {'shots': [{'duration_s': 4, 'dialogue': []}], 'version': 'v', 'dialogue_language': package_language,
               **({'source_speech_language': source_language} if source_language else {})}
    with pytest.raises(RuntimeError, match='stop after'):
        asyncio.run(quality.inspect({'package': package, 'url': 'https://test/clip.mp4', 'source_job': 'j'}))
    assert heard == [expected]
