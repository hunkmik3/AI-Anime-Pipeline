"""Pull the keyframes a vision model needs to read each shot.

How many per shot follows the guide, because one frame cannot show a movement
and ten frames of a 0.3s insert are the same picture ten times:

    shot < 0.7s    2 frames at 20% / 80%
    0.7s – 3s      3 frames at 10% / 50% / 90%
    > 3s           5 frames at 10% / 30% / 50% / 70% / 90%

Frames are read in ONE sequential pass rather than by seeking. Seeking an H.264
stream by frame index lands on the nearest keyframe on many files, which on a
0.3s insert means grabbing a frame from the wrong shot entirely.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

_LONG_EDGE = 768        # the guide's 720-1024px band; a vision model sees no more
_JPEG_QUALITY = 85


@dataclass
class ShotSpan:
    index: int          # 1-based shot number
    start: float
    end: float
    frame_paths: list[Path] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.end - self.start


def sample_points(duration: float, *, deep: bool = False) -> list[float]:
    """Relative positions (0..1) to sample inside a shot of ``duration`` seconds.

    ``deep`` buys detail with tokens: a 10s shot read from 5 frames loses the
    middle of a move, and a model asked to describe what it cannot see fills
    the gap itself. Deep sampling is what "analyse this one properly" means.
    """
    if duration < 0.7:
        return [0.15, 0.5, 0.85] if deep else [0.2, 0.8]
    if duration <= 3.0:
        return [0.1, 0.3, 0.5, 0.7, 0.9] if deep else [0.1, 0.5, 0.9]
    if deep:
        return [0.05, 0.2, 0.35, 0.5, 0.65, 0.8, 0.95]
    return [0.1, 0.3, 0.5, 0.7, 0.9]


def spans_from_cuts(cuts: list[float], duration: float) -> list[ShotSpan]:
    """Turn cut times into contiguous shots covering the whole video.

    Contiguity is enforced here, not hoped for downstream: each shot ends
    exactly where the next begins, and the last ends at the video's end, so the
    guide's "no gap > 1 frame" check can never fail on a rounding error.
    """
    edges = [0.0] + [c for c in cuts if 0.0 < c < duration] + [duration]
    return [ShotSpan(index=i + 1, start=a, end=b) for i, (a, b) in enumerate(zip(edges, edges[1:]))]


def extract_keyframes(path: Path, spans: list[ShotSpan], out_dir: Path, fps: float,
                      *, deep: bool = False) -> list[ShotSpan]:
    """Write keyframes for every shot into ``out_dir`` and attach their paths."""
    import cv2

    out_dir.mkdir(parents=True, exist_ok=True)

    # frame index -> [(span, jpg path)]. Several shots never share a frame, but
    # a 2-frame span can land both samples on one frame index; keep both paths.
    wanted: dict[int, list[tuple[ShotSpan, Path]]] = {}
    for span in spans:
        for k, rel in enumerate(sample_points(span.duration, deep=deep)):
            t = span.start + rel * span.duration
            # Clamp inside the shot: rounding must never pull a sample across a cut.
            frame = int(t * fps)
            frame = max(int(span.start * fps), min(frame, int(span.end * fps) - 1))
            target = out_dir / f"shot{span.index:03d}_{k + 1}.jpg"
            wanted.setdefault(frame, []).append((span, target))

    if not wanted:
        return spans

    last = max(wanted)
    cap = cv2.VideoCapture(str(path))
    index = 0
    while index <= last:
        ok, frame = cap.read()
        if not ok:
            break
        if index in wanted:
            h, w = frame.shape[:2]
            scale = _LONG_EDGE / max(h, w)
            if scale < 1.0:
                frame = cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
            for span, target in wanted[index]:
                cv2.imwrite(str(target), frame, [cv2.IMWRITE_JPEG_QUALITY, _JPEG_QUALITY])
                span.frame_paths.append(target)
        index += 1
    cap.release()

    for span in spans:
        span.frame_paths.sort()
    return spans
