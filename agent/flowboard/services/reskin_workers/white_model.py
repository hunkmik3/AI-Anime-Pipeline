"""Turn a source-film segment into a depth-shaded white-model ("gray clay") motion video.

Per-frame relative depth (Depth Anything V2 Small, ONNX, local), shaded as matte
gray clay. Detected faces are smoothed. This reduces appearance cues but is NOT
an anonymisation guarantee or a reconstructed 3D scene; small faces can be missed.

    cd agent && .venv/bin/python -u <this file> <source.mp4> <start_s> <end_s> <cut1,cut2,...> <out.mp4> [render JSON]

Cuts are segment-relative seconds; temporal smoothing never crosses one.

The optional render JSON trades depth detail for speed on long films:
{"depth_long_side": 448, "fps": 24, "depth_stride": 2}. Depth is computed for every
stride-th frame of a shot and for its first and last frame, and interpolated between
them inside the shot only. Without it the output is the approved standard render.

"4D blocking" layers (opt-in, same JSON): {"pose": true} draws each person's limbs and
torso as shaded gray mannequin tubes from per-frame body keypoints (YOLOX + RTMPose,
ONNX, local), so arms and legs read where depth alone is soft; faces stay domes and the
render stays monochrome. {"camera": true} measures the camera from the background (people
masked out) between consecutive frames of each shot and writes, next to the output,
<out>.layers.json with one summary per shot: pan, tilt, zoom, roll, shake and a label.
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


# COCO-17 body keypoints: arms and legs as tubes, the torso as one filled quad.
LIMBS = ((5, 7), (7, 9), (6, 8), (8, 10), (11, 13), (13, 15), (12, 14), (14, 16))
TORSO = (5, 6, 12, 11)
MIN_SCORE = 0.35


def mannequin(image: np.ndarray, depth: np.ndarray, people) -> np.ndarray:
    """Clay image with each person's visible limbs and torso drawn as shaded gray tubes.
    ``people``: [(keypoints (17, 2) in image pixels, scores (17,))]; ``depth`` 0..1, any size."""
    if not people:
        return image
    h, w = image.shape[:2]
    near = cv2.resize(depth, (w, h), interpolation=cv2.INTER_LINEAR)
    out = image.astype(np.float32)
    for kps, scores in people:
        seen = np.asarray(scores) >= MIN_SCORE
        pts = np.asarray(kps, np.float32)
        torso = [pts[i] for i in TORSO if seen[i]]
        if seen[5] and seen[6] and seen[11] and seen[12]:
            length = float(np.linalg.norm((pts[5] + pts[6]) / 2 - (pts[11] + pts[12]) / 2))
        else:
            box = pts[seen] if seen.any() else pts
            length = float(np.ptp(box[:, 1])) * 0.3 if len(box) > 1 else 0.0
        width = int(max(3, length * 0.16))
        mask = np.zeros((h, w), np.uint8)
        for a, b in LIMBS:
            if seen[a] and seen[b]:
                cv2.line(mask, tuple(int(v) for v in pts[a]), tuple(int(v) for v in pts[b]), 255, width, cv2.LINE_AA)
        if len(torso) == 4:
            cv2.fillConvexPoly(mask, np.array(torso, np.int32), 255, cv2.LINE_AA)
        if not mask.any():
            continue
        solid = (mask > 127).astype(np.uint8)
        axis = cv2.distanceTransform(solid, cv2.DIST_L2, 3)
        axis = np.sqrt(axis / max(float(axis.max()), 1.0))          # bright along each tube's axis
        level = float(near[solid > 0].mean()) if solid.any() else 0.5
        tube = np.clip(0.34 + 0.44 * axis + 0.12 * level, 0, 1) * 255.0
        alpha = cv2.GaussianBlur(mask.astype(np.float32) / 255.0, (5, 5), 0)[..., None] * 0.82
        out = out * (1 - alpha) + tube[..., None] * alpha
    return np.clip(out, 0, 255).astype(np.uint8)


def people_mask(shape, people, faces) -> np.ndarray:
    """255 where the background is: people (pose boxes, else a body below each face) are 0."""
    h, w = shape
    mask = np.full((h, w), 255, np.uint8)
    for kps, scores in people or []:
        pts = np.asarray(kps)[np.asarray(scores) >= MIN_SCORE]
        if len(pts):
            x0, y0 = pts.min(0); x1, y1 = pts.max(0)
            pad = 0.15 * max(x1 - x0, y1 - y0, 20)
            cv2.rectangle(mask, (int(x0 - pad), int(y0 - pad * 2)), (int(x1 + pad), int(y1 + pad)), 0, -1)
    for x, y, fw, fh in faces or []:
        cv2.rectangle(mask, (int(x - 1.2 * fw), int(y - 0.5 * fh)), (int(x + 2.2 * fw), int(y + 6 * fh)), 0, -1)
    return mask


def camera_step(prev_gray, gray, mask):
    """(dx, dy, scale, roll°) of the image centre between two frames, from the background."""
    pts = cv2.goodFeaturesToTrack(prev_gray, 400, 0.01, 8, mask=mask)
    if pts is None or len(pts) < 12:
        return None
    nxt, status, _ = cv2.calcOpticalFlowPyrLK(prev_gray, gray, pts, None, winSize=(21, 21), maxLevel=3)
    good = status.ravel() == 1
    if good.sum() < 10:
        return None
    m, _ = cv2.estimateAffinePartial2D(pts[good], nxt[good], method=cv2.RANSAC, ransacReprojThreshold=2.0)
    if m is None:
        return None
    h, w = gray.shape[:2]
    cx, cy = w / 2, h / 2
    moved = m @ np.array([cx, cy, 1.0])
    scale = float(np.hypot(m[0, 0], m[1, 0]))
    return float(moved[0] - cx), float(moved[1] - cy), scale, float(np.degrees(np.arctan2(m[1, 0], m[0, 0])))


def camera_summary(steps, w: int, h: int) -> dict:
    """One shot's camera from its frame steps (None where unmeasured). Signs are the
    camera's own move: pan + right, tilt + up, zoom + in, roll + clockwise."""
    valid = [s for s in steps if s]
    if len(valid) < max(3, len(steps) // 3):
        return {'label': 'unclear', 'measured': len(valid), 'frames': len(steps)}
    dx = np.array([s[0] for s in valid]) / w * 100
    dy = np.array([s[1] for s in valid]) / h * 100
    pan, tilt = -float(dx.sum()), float(dy.sum())
    zoom = (float(np.prod([s[2] for s in valid])) - 1) * 100
    roll = float(sum(s[3] for s in valid))
    shake = float(np.hypot(dx - dx.mean(), dy - dy.mean()).std()) if len(valid) > 2 else 0.0
    labels = []
    if zoom > 3: labels.append('push in')
    elif zoom < -3: labels.append('pull out')
    if pan > 4: labels.append('pan right')
    elif pan < -4: labels.append('pan left')
    if tilt > 4: labels.append('tilt up')
    elif tilt < -4: labels.append('tilt down')
    if abs(roll) > 3: labels.append('roll')
    if shake > 0.35: labels.append('handheld')
    return {'label': ' + '.join(labels) or 'static', 'pan_pct': round(pan, 2), 'tilt_pct': round(tilt, 2),
            'zoom_pct': round(zoom, 2), 'roll_deg': round(roll, 2), 'shake_pct': round(shake, 3),
            'measured': len(valid), 'frames': len(steps)}


def depth_plan(shot_of: list[int], stride: int) -> list[bool]:
    """Which frames get their own depth: every stride-th frame of each shot, and each
    shot's first and last frame, so interpolation never spans a cut."""
    n = len(shot_of)
    keep = [False] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and shot_of[j + 1] == shot_of[i]:
            j += 1
        for k in range(i, j + 1, max(1, stride)):
            keep[k] = True
        keep[j] = True
        i = j + 1
    return keep


def main() -> None:
    src, start, end = Path(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3])
    cuts = [float(c) for c in sys.argv[4].split(',') if c]
    out = Path(sys.argv[5])
    render = json.loads(sys.argv[6]) if len(sys.argv) > 6 and sys.argv[6] else {}
    global DEPTH_W, DEPTH_H, FPS
    FPS = int(render.get('fps', FPS))
    long_side = int(render.get('depth_long_side', 644))
    stride = max(1, int(render.get('depth_stride', 1)))
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
        DEPTH_W, DEPTH_H = [max(14, round(v/max(w,h)*long_side/14)*14) for v in (w,h)]
        boxes = [faces_of(detector, cv2.imread(str(f))) for f in frames]
        shot_of = [sum(i / FPS >= c for c in cuts) for i in range(len(frames))]
        keep = depth_plan(shot_of, stride)
        poses = [[] for _ in frames]
        if render.get('pose'):
            from rtmlib import Body
            body = Body(mode='balanced', backend='onnxruntime', device='cpu')
            held = []
            for i, f in enumerate(frames):
                if keep[i] or (i and shot_of[i] != shot_of[i - 1]):
                    kps, scores = body(cv2.imread(str(f)))
                    held = [(k, s) for k, s in zip(kps, scores) if (np.asarray(s) >= MIN_SCORE).sum() >= 4]
                poses[i] = held                                  # a skipped frame holds its shot's last pose
        layers = {'pose': bool(render.get('pose')), 'pose_frames': sum(1 for p in poses if p)}
        if render.get('camera'):
            steps = {}
            small = 480 / max(w, h)
            prev = None
            for i, f in enumerate(frames):
                gray = cv2.cvtColor(cv2.resize(cv2.imread(str(f)), None, fx=small, fy=small, interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
                if prev is not None and shot_of[i] == shot_of[i - 1]:
                    scaled = [([(x * small, y * small) for x, y in k], s) for k, s in poses[i - 1]]
                    faces_small = [(x * small, y * small, bw * small, bh * small) for x, y, bw, bh in boxes[i - 1]]
                    steps.setdefault(shot_of[i], []).append(camera_step(prev, gray, people_mask(prev.shape, scaled, faces_small)))
                prev = gray
            layers['camera'] = [{'shot': n, **camera_summary(steps.get(n, []), gray.shape[1], gray.shape[0])}
                                for n in range(len(cuts) + 1)]
        if layers['pose'] or 'camera' in layers:
            Path(str(out) + '.layers.json').write_text(json.dumps(layers))
        reach = 4 * stride                                      # the same face window in time
        def measured(i):
            im = cv2.imread(str(frames[i]))
            # A face missed for a frame or two keeps the neighbours' boxes inside its shot.
            near = [b for j in range(max(0, i - reach), min(len(frames), i + reach + 1)) if shot_of[j] == shot_of[i] for b in boxes[j]]
            return featureless(depth_of(session, im), near, im.shape[:2])
        previous = None
        last_i, last_d = None, None
        size = (w, h)
        for i in range(len(frames)):
            if not keep[i]:
                continue
            d_i = measured(i)
            # Frames between two measured frames of one shot take a linear blend of them.
            todo = [(k, last_d + (d_i - last_d) * ((k - last_i) / (i - last_i))) for k in range(last_i + 1, i)] if (
                last_i is not None and shot_of[last_i] == shot_of[i]) else []
            for k, d in [*todo, (i, d_i)]:
                if previous is not None and shot_of[k] == shot_of[k - 1]:
                    d = 0.55 * d + 0.45 * previous               # steadier clay, never across a cut
                previous = d
                cv2.imwrite(str(tmp / 'out' / f'{k + 1:05d}.png'), mannequin(clay(d, size), d, poses[k]), [cv2.IMWRITE_PNG_COMPRESSION, 1])
                if k % 30 == 0:
                    print(json.dumps({'done': k+1, 'total':len(frames)}), flush=True)
            last_i, last_d = i, d_i
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-framerate', str(FPS), '-i', str(tmp / 'out' / '%05d.png'),
                        '-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2', '-an', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '18', '-r', str(FPS), str(out)], check=True)
    print(json.dumps({'done':len(frames), 'total':len(frames)}), flush=True)


if __name__ == '__main__':
    main()
