"""Faces in source keyframes: detection (YuNet, MIT) and a 128-number identity
fingerprint per face (SFace, Apache-2.0), both from the OpenCV model zoo.

Run in a child process like the other measuring workers:

    python -m flowboard.services.video_analyzer.face_id_worker <out.json> <jobs.json>

``jobs.json`` is ``[{id, path}]``. Each frame is read at most ``MAX_SIDE`` pixels on its
long side (YuNet finds faces of about 10 to 300 pixels). Faces below ``MIN_SCORE`` or
``MIN_FACE`` pixels are dropped: a blurred background head is not an identity.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

VERSION = 1
DETECTOR = ('opencv/face_detection_yunet', 'face_detection_yunet_2023mar.onnx')
RECOGNIZER = ('opencv/face_recognition_sface', 'face_recognition_sface_2021dec.onnx')
MAX_SIDE = 640
MIN_SCORE = 0.85
MIN_FACE = 48


def model_paths() -> tuple[str, str]:
    from huggingface_hub import hf_hub_download
    return hf_hub_download(*DETECTOR), hf_hub_download(*RECOGNIZER)


def run(jobs: list[dict]) -> dict:
    import cv2
    import numpy as np
    started = time.monotonic()
    detector_path, recognizer_path = model_paths()
    detector = cv2.FaceDetectorYN.create(detector_path, '', (320, 320), MIN_SCORE, 0.3, 50)
    recognizer = cv2.FaceRecognizerSF.create(recognizer_path, '')
    out = {}
    for job in jobs:
        image = cv2.imread(str(job['path']))
        if image is None:
            out[job['id']] = None
            continue
        h, w = image.shape[:2]
        scale = min(1.0, MAX_SIDE / max(h, w))
        if scale < 1.0:
            image = cv2.resize(image, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)
            h, w = image.shape[:2]
        detector.setInputSize((w, h))
        _, faces = detector.detect(image)
        found = []
        for row in faces if faces is not None else []:
            x, y, fw, fh, score = float(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[14])
            if score < MIN_SCORE or min(fw, fh) < MIN_FACE * scale:
                continue
            feature = recognizer.feature(recognizer.alignCrop(image, row))[0]
            norm = float(np.linalg.norm(feature))
            if not norm:
                continue
            found.append({'box': [round(v / scale, 1) for v in (x, y, fw, fh)], 'score': round(score, 3),
                          'embedding': [round(float(v), 5) for v in feature / norm]})
        out[job['id']] = found
    return {'version': VERSION, 'models': [f'{DETECTOR[0]}/{DETECTOR[1]}', f'{RECOGNIZER[0]}/{RECOGNIZER[1]}'],
            'faces': out, 'elapsed_s': round(time.monotonic() - started, 3)}


def main(argv) -> int:
    result = run(json.loads(Path(argv[2]).read_text()))
    target = Path(argv[1])
    tmp = target.with_suffix('.tmp')
    tmp.write_text(json.dumps(result))
    tmp.replace(target)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
