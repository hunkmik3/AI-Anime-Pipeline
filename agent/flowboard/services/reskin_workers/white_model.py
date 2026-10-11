"""Turn a source-film segment into a depth-shaded white-model ("gray clay") motion video.

Per-frame relative depth (Depth Anything V2 Small, ONNX, local), shaded as matte
gray clay. Detected faces are smoothed. This reduces appearance cues but is NOT
an anonymisation guarantee or a reconstructed 3D scene; small faces can be missed.

    cd agent && .venv/bin/python -u <this file> <source.mp4> <start_s> <end_s> <cut1,cut2,...> <out.mp4>

Cuts are segment-relative seconds; temporal smoothing never crosses one.
"""
import subprocess, sys, tempfile, json, os
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort
from huggingface_hub import hf_hub_download

FPS = 30
DEPTH_W, DEPTH_H = 364, 644            # Replaced from each source's aspect below.
MEAN = np.array([0.485, 0.456, 0.406], np.float32)
STD = np.array([0.229, 0.224, 0.225], np.float32)
LIGHT = np.array([-0.45, -0.55, 0.70], np.float32)
LIGHT /= np.linalg.norm(LIGHT)


def frames_of(src: Path, start: float, end: float, folder: Path) -> list[Path]:
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-ss', f'{start:.3f}', '-i', str(src), '-t', f'{end - start:.3f}',
                    '-vf', f'fps={FPS}', str(folder / '%05d.png')], check=True)
    return sorted(folder.glob('*.png'))


def depth_of(session, bgr: np.ndarray) -> np.ndarray:
    rgb = cv2.cvtColor(cv2.resize(bgr, (DEPTH_W, DEPTH_H), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB)
    x = ((rgb.astype(np.float32) / 255.0 - MEAN) / STD).transpose(2, 0, 1)[None]
    d = session.run(None, {'pixel_values': x})[0][0]
    lo, hi = np.percentile(d, 2), np.percentile(d, 98)
    return np.clip((d - lo) / max(hi - lo, 1e-6), 0, 1).astype(np.float32)   # 1 = near


def faces_of(detector, bgr: np.ndarray) -> list[tuple[float, float, float, float]]:
    h, w = bgr.shape[:2]
    detector.setInputSize((w, h))
    _, found = detector.detect(bgr)
    return [] if found is None else [tuple(map(float, f[:4])) for f in found]


def featureless(depth: np.ndarray, boxes, frame_size) -> np.ndarray:
    """Smooth each face region of the depth map into a dome: no eyes, nose or mouth survive."""
    if not boxes:
        return depth
    fh, fw = frame_size
    sx, sy = DEPTH_W / fw, DEPTH_H / fh
    mask = np.zeros(depth.shape, np.float32)
    for x, y, w, h in boxes:
        cx, cy = (x + w / 2) * sx, (y + h * 0.45) * sy
        # Half-axes: the face and forehead, not the hair or shoulders around them.
        cv2.ellipse(mask, (int(cx), int(cy)), (int(w * sx * 0.62), int(h * sy * 0.78)), 0, 0, 360, 1.0, -1)
    size = max(7, int(max(b[2] for b in boxes) * sx * 0.45) | 1)
    # Blur only the depth inside the face, so the background never bleeds in and the
    # head keeps its outline: a smooth dome where eyes, nose and mouth were.
    soft = cv2.GaussianBlur(depth * mask, (size, size), 0) / np.maximum(cv2.GaussianBlur(mask, (size, size), 0), 1e-3)
    feather = cv2.GaussianBlur(mask, (9, 9), 0)
    return depth * (1 - feather) + soft * feather


def clay(depth: np.ndarray, out_size) -> np.ndarray:
    z = cv2.GaussianBlur(depth, (0, 0), 1.6) * 60.0
    gx = cv2.Sobel(z, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(z, cv2.CV_32F, 0, 1, ksize=3)
    n = np.dstack([-gx, -gy, np.ones_like(z) * 8.0])
    n /= np.linalg.norm(n, axis=2, keepdims=True)
    shade = np.clip(n @ LIGHT, 0, 1)
    value = 0.22 + 0.62 * shade + 0.12 * depth                  # near surfaces a touch brighter
    edges = cv2.Canny((depth * 255).astype(np.uint8), 18, 45).astype(np.float32) / 255.0
    value = value * (1 - 0.35 * cv2.GaussianBlur(edges, (3, 3), 0))
    gray = (np.clip(value, 0, 1) * 255).astype(np.uint8)
    gray = cv2.resize(gray, out_size, interpolation=cv2.INTER_CUBIC)
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


def main() -> None:
    src, start, end = Path(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3])
    cuts = [float(c) for c in sys.argv[4].split(',') if c]
    out = Path(sys.argv[5])
    global DEPTH_W, DEPTH_H
    model = os.getenv('FLOWBOARD_DEPTH_MODEL') or hf_hub_download('onnx-community/depth-anything-v2-small', 'onnx/model.onnx')
    yunet = os.getenv('FLOWBOARD_YUNET_MODEL') or hf_hub_download('opencv/face_detection_yunet', 'face_detection_yunet_2023mar.onnx')
    options = ort.SessionOptions(); options.intra_op_num_threads = 4
    session = ort.InferenceSession(model, sess_options=options, providers=['CPUExecutionProvider'])
    detector = cv2.FaceDetectorYN.create(str(yunet), '', (320, 320), 0.55, 0.3, 5000)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / 'in').mkdir(); (tmp / 'out').mkdir()
        frames = frames_of(src, start, end, tmp / 'in')
        if not frames: raise ValueError('Selected source interval has no frames')
        first = cv2.imread(str(frames[0])); h, w = first.shape[:2]
        # Keep portrait, square and landscape source geometry. ONNX dimensions
        # must be multiples of 14; do not stretch every source into 9:16.
        DEPTH_W, DEPTH_H = [max(14, round(v/max(w,h)*644/14)*14) for v in (w,h)]
        boxes = [faces_of(detector, cv2.imread(str(f))) for f in frames]
        shot_of = [sum(i / FPS >= c for c in cuts) for i in range(len(frames))]
        previous = None
        for i, frame in enumerate(frames):
            im = cv2.imread(str(frame))
            # A face missed for a frame or two keeps the neighbours' boxes inside its shot.
            near = [b for j in range(max(0, i - 4), min(len(frames), i + 5)) if shot_of[j] == shot_of[i] for b in boxes[j]]
            d = featureless(depth_of(session, im), near, im.shape[:2])
            if previous is not None and shot_of[i] == shot_of[i - 1]:
                d = 0.55 * d + 0.45 * previous                   # steadier clay, never across a cut
            previous = d
            cv2.imwrite(str(tmp / 'out' / f'{i + 1:05d}.png'), clay(d, (im.shape[1], im.shape[0])))
            if i % 30 == 0:
                print(json.dumps({'done': i+1, 'total':len(frames)}), flush=True)
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-framerate', str(FPS), '-i', str(tmp / 'out' / '%05d.png'),
                        '-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2', '-an', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '18', '-r', str(FPS), str(out)], check=True)
    print(json.dumps({'done':len(frames), 'total':len(frames)}), flush=True)


if __name__ == '__main__':
    main()
