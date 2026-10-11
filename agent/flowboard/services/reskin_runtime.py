"""Local readiness checks. Never contacts a generation provider or prints secrets."""
import importlib.util
import os
import shutil
import subprocess
import time

_cached = None


def capabilities():
    global _cached
    if _cached and time.monotonic() - _cached[0] < 30:
        return _cached[1]
    from flowboard.services import reskin_audio, automation
    from flowboard.services.flowstudio import avis_api, atrium_api
    checks = {
        'ffmpeg': bool(shutil.which('ffmpeg') and shutil.which('ffprobe')),
        'motion_runtime': all(importlib.util.find_spec(m) for m in ('cv2', 'onnxruntime', 'numpy', 'huggingface_hub')),
        'avis': avis_api.is_configured(), 'atrium': atrium_api.is_configured(),
        'reference_storage': automation.reference_chain_available(),
    }
    try:
        result = subprocess.run([reskin_audio.runtime(), '-c', 'import demucs,torch,torchaudio,soundfile'],
                                capture_output=True, timeout=25, check=False)
        checks['audio_runtime'] = result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        checks['audio_runtime'] = False
    labels = {'ffmpeg':'FFmpeg / ffprobe', 'motion_runtime':'Runtime tạo motion reference',
              'avis':'Avis', 'atrium':'Atrium đọc video/âm thanh',
              'reference_storage':'Kho reference R2', 'audio_runtime':'Runtime tách nhạc Demucs'}
    report = {'ready':all(checks.values()), 'checks':checks,
              'missing':[labels[k] for k,v in checks.items() if not v],
              'output_ratios':['1:1','9:16','16:9'], 'staging_ratio':'21:9', 'motion_ratio':'source'}
    _cached = (time.monotonic(), report)
    return report


def require_ready(audio=True):
    report = capabilities()
    checks = report['checks']
    if not all(v for k,v in checks.items() if audio or k != 'audio_runtime'):
        raise ValueError('Máy chủ chưa sẵn sàng chạy Reskin: ' + ', '.join(report['missing']) +
                         '. Xem docs/RESKIN_PIPELINE.md để cài đặt trước khi dùng lượt gen.')
