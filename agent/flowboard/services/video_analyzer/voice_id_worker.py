"""Voice fingerprints (speaker embeddings) for timed stretches of one audio file.

Run in a child process like the ASR workers; the server never imports onnxruntime.

    python -m flowboard.services.video_analyzer.voice_id_worker <audio.wav> <out.json> <jobs.json>

``jobs.json`` is ``[{id, spans: [[start, end], ...]}]`` in seconds — the spoken words of
one line; the pauses between them are left out. Each job becomes one 256-number
fingerprint from WeSpeaker's ResNet34 (VoxCeleb2, CC BY 4.0), L2-normalised, so the
cosine similarity of two lines is their dot product. Jobs with under ``MIN_S`` of speech
are returned as ``null``: a fingerprint of a breath says nothing about who breathed.
"""
from __future__ import annotations

import json
import sys
import time
import wave
from pathlib import Path

VERSION = 1
RATE = 16000
MODEL_REPO = 'Wespeaker/wespeaker-voxceleb-resnet34-LM'
MODEL_FILE = 'voxceleb_resnet34_LM.onnx'
MIN_S = 0.4
MAX_S = 12.0


def read_wav(path: str):
    """16 kHz mono 16-bit PCM (what asr.extract_audio writes), at int16 scale."""
    import numpy as np
    with wave.open(path, 'rb') as w:
        if w.getframerate() != RATE or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError(f'expected 16 kHz mono 16-bit audio, got {w.getframerate()} Hz '
                             f'x{w.getnchannels()} {8 * w.getsampwidth()}-bit')
        return np.frombuffer(w.readframes(w.getnframes()), dtype='<i2').astype(np.float32)


_BANKS: dict = {}


def _mel_banks(num_bins: int, fft_size: int):
    """Kaldi's triangular mel filters (low 20 Hz, high Nyquist); the Nyquist bin weighs 0."""
    import numpy as np
    key = (num_bins, fft_size)
    if key not in _BANKS:
        mel = lambda f: 1127.0 * np.log(1.0 + np.asarray(f, dtype=np.float64) / 700.0)
        lo, hi = mel(20.0), mel(RATE / 2)
        delta = (hi - lo) / (num_bins + 1)
        b = np.arange(num_bins)[:, None]
        left, center, right = lo + b * delta, lo + (b + 1) * delta, lo + (b + 2) * delta
        m = mel(np.arange(fft_size // 2) * RATE / fft_size)[None, :]
        weights = np.maximum(0.0, np.minimum((m - left) / (center - left), (right - m) / (right - center)))
        _BANKS[key] = np.pad(weights, ((0, 0), (0, 1))).astype(np.float32)
    return _BANKS[key]


def fbank(samples, *, num_bins: int = 80):
    """Kaldi log-mel filterbank as WeSpeaker computes it (torchaudio.compliance.kaldi.fbank
    with a Hamming window, no dither, 25 ms frames every 10 ms, edges snipped), then
    per-utterance mean normalisation. ``samples`` are at int16 scale."""
    import numpy as np
    length, shift, fft_size = 400, 160, 512
    x = np.asarray(samples, dtype=np.float32)
    if len(x) < length:
        return np.zeros((0, num_bins), dtype=np.float32)
    n = 1 + (len(x) - length) // shift
    frames = np.lib.stride_tricks.as_strided(x, shape=(n, length), strides=(x.strides[0] * shift, x.strides[0])).copy()
    frames -= frames.mean(axis=1, keepdims=True)
    frames[:, 1:] -= 0.97 * frames[:, :-1].copy()
    frames[:, 0] -= 0.97 * frames[:, 0]
    frames *= np.hamming(length).astype(np.float32)
    power = np.abs(np.fft.rfft(frames, n=fft_size, axis=1)) ** 2
    feats = np.log(np.maximum(power.astype(np.float32) @ _mel_banks(num_bins, fft_size).T, np.finfo(np.float32).eps))
    return feats - feats.mean(axis=0, keepdims=True)


def model_path() -> str:
    from huggingface_hub import hf_hub_download
    return hf_hub_download(MODEL_REPO, MODEL_FILE)


def run(audio: str, jobs: list[dict], model: str | None = None) -> dict:
    import numpy as np
    import onnxruntime as ort
    started = time.monotonic()
    samples = read_wav(audio)
    path = model or model_path()
    options = ort.SessionOptions()
    options.intra_op_num_threads = 4
    session = ort.InferenceSession(path, options, providers=['CPUExecutionProvider'])
    out = {}
    for job in jobs:
        pieces, total = [], 0
        for start, end in job.get('spans') or [[job['start'], job['end']]]:
            a = max(0, int(float(start) * RATE))
            b = min(len(samples), int(float(end) * RATE), a + int(MAX_S * RATE) - total)
            if b > a:
                pieces.append(samples[a:b]); total += b - a
            if total >= MAX_S * RATE:
                break
        if total / RATE < MIN_S:
            out[job['id']] = None
            continue
        feats = fbank(np.concatenate(pieces))
        emb = session.run(None, {'feats': feats[None].astype(np.float32)})[0][0]
        norm = float(np.linalg.norm(emb))
        out[job['id']] = [round(float(v), 6) for v in emb / norm] if norm else None
    return {'version': VERSION, 'model': f'{MODEL_REPO}/{MODEL_FILE}', 'embeddings': out,
            'elapsed_s': round(time.monotonic() - started, 3)}


def main(argv) -> int:
    result = run(argv[1], json.loads(Path(argv[3]).read_text()), argv[4] if len(argv) > 4 else None)
    target = Path(argv[2])
    tmp = target.with_suffix('.tmp')
    tmp.write_text(json.dumps(result))
    tmp.replace(target)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
