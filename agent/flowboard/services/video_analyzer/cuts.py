"""Find the editorial cuts in a reference video.

Detection is two opinions and a referee:

1. ``AdaptiveDetector`` — compares each frame to a rolling average. High
   precision, but it smooths over the 0.3-1s cuts a fight edit is made of.
2. ``ContentDetector`` — compares each frame to the last, so it catches those
   fast cuts. It also "cuts" on fast camera moves and whip action.
3. The referee: take the union, then throw out candidates that are really one
   continuous shot in motion.

Measured on the 153.9s reference fight clip against a 103-cut hand list, with a
±3-frame tolerance:

    Content th=23 (the guide's microdrama preset)   F1 0.85
    Content th=27                                   F1 0.90
    Adaptive alone                                  F1 0.97
    union, no referee                               F1 0.92  (recall 0.98)

The guide recommends LOWERING the threshold for microdrama. On this footage
that is the worst option measured: a lower threshold turns more motion into cuts.

How the referee tells the two apart — found by measuring, not assumed. The
first version looked for luma flashes, and rejected nothing: brightness turned
out to be noise on both sides (true cuts lifted up to +70, false ones went
negative). What does separate them is how similarity changes with distance
across the candidate frame:

  * a HARD CUT is instantaneous. The frame just before is already shot A and
    the frame just after is already shot B, so comparing frames 1 apart or 4
    apart gives the same answer. Similarity is flat.
  * MOTION INSIDE ONE SHOT is continuous. Adjacent frames are nearly identical
    and similarity decays as the gap widens.

So the referee scores ``sim(±1) − sim(±4)``. Over the 103 true cuts that decay
has a median of 0.00 and a 90th percentile of 0.06; 14 of the 16 false cuts the
union produced score ≥ 0.14. This is the "motion continuity" signal in §7 of
the implementation guide.

── What that pipeline got wrong, and the third opinion added for it ──

The two detectors above are threshold rules on frame difference, and a
3D/AI-generated trailer breaks them in BOTH directions at once. Measured on a
233s CGI trailer (0919-1), 89 cuts shipped:

  * 21 of them were not cuts at all. The four frames either side are identical
    to the eye and correlate at 0.99+. They land on exact multiples of four
    seconds, because the film is assembled from 4-second generated segments and
    the seam changes a few pixels — enough for a threshold, invisible to a
    viewer. A shot split in half there reads as a missing beat.
  * Real cuts hidden inside whip-pans were never proposed at all: one 10.3s
    "shot" contained four separate scenes, one 4s "shot" contained four. Both
    detectors saw blur either side and stayed quiet.

TransNetV2 (a learned boundary network, `transnetv2-pytorch`, 43s on CPU for
7000 frames) finds exactly those buried boundaries — and of the 10 cuts only it
proposed, ZERO were of the "picture did not change" kind. It is not a
replacement: it missed 10 boundaries the threshold detectors caught where the
content plainly changes. So all three vote, and one hard veto settles the false
ones: if the picture is unchanged across ±8 frames (0.5s), nothing happened
there, whoever says otherwise.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import os

import numpy as np

logger = logging.getLogger(__name__)
VERSION = 3

# Frame statistics are read at thumbnail size. A cut is a change of camera, and
# that survives any amount of downscaling; the detail it throws away is exactly
# the noise that makes a flash look like a cut.
_THUMB = (64, 114)  # (w, h) — 9:16-ish; aspect distortion is irrelevant here

# Two candidates this close are the same cut seen by two detectors.
_DEDUPE_FRAMES = 3

# The veto. Content 8 frames (0.27s) either side of a real boundary does not
# come back; content across an invisible encode/segment seam is identical.
# Measured on the CGI trailer: verified false boundaries sit at 0.99+, verified
# real ones below 0.6, and 0.85 separates them with nothing in between.
_VETO_GAP = 8
_VETO_SIM = 0.85
# The same evidence read the other way. Below this the picture plainly became a
# different picture, and the motion referee — a heuristic about how similarity
# decays — does not get to overrule that. Added after the first version of this
# file dropped a verified cut at 01:58.60 whose content had changed (0.55).
_CHANGED_SIM = 0.60
# TransNetV2's own confidence for a frame being inside a transition.
_TRANSNET_THRESHOLD = 0.5


@dataclass
class Cut:
    at: float                  # seconds
    source: str                # any of adaptive / content / transnet, joined by "+"
    continuity: float = 0.0    # sim(±1) − sim(±4); high = one shot in motion
    similarity: float = 1.0    # sim(±1); near zero = two unrelated pictures
    unchanged: float = 0.0     # sim(±8); near one = the picture never changed
    kept: bool = True
    reason: str = ""
    adjacent_similarity: float = 0.0
    change_ratio: float = 0.0
    motion_match: float = 0.0


@dataclass
class CutReport:
    cuts: list[float]
    rejected: list[Cut] = field(default_factory=list)
    candidates: int = 0
    # Every cut that survived, with the measurements that let it through. The
    # times alone say nothing about which ones were close calls, and a referee
    # that cannot tell a unanimous cut from a marginal one has to re-check all
    # of them.
    kept_detail: list[Cut] = field(default_factory=list)


_STATS_WORKERS = max(1, min(8, (os.cpu_count() or 2)))
_STATS_CHUNK = 32


def _shrink(frames: list[np.ndarray]) -> tuple[list[np.ndarray], list[np.ndarray]]:
    import cv2
    thumbs, tiny = [], []
    for frame in frames:
        small = cv2.resize(frame, _THUMB, interpolation=cv2.INTER_AREA)
        thumbs.append(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY))
        # TransNetV2 is trained on 48x27 RGB and takes nothing else.
        # Resize before swapping channels: the same per-channel pixels, without
        # copying a full-resolution noncontiguous RGB array for every frame.
        tiny.append(cv2.resize(frame, (48, 27), interpolation=cv2.INTER_AREA)[:, :, ::-1])
    return thumbs, tiny


def _frame_stats(path: Path) -> tuple[np.ndarray, np.ndarray, float]:
    """One decode pass → a grey thumbnail per frame, and TransNetV2's own input.

    Everything the referee, the veto and the network need, so the video is
    decoded once rather than three times. Imported lazily: cv2 must not load in
    any process that might also load PyAV (see asr_worker).

    Decoding stays sequential; the per-frame resizes (most of this pass) run in a
    thread pool, in order and with a bounded number of frames in flight. OpenCV
    releases the GIL, and every frame gets exactly the same operations.
    """
    import cv2
    from collections import deque
    from concurrent.futures import ThreadPoolExecutor

    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    thumbs: list[np.ndarray] = []
    tiny: list[np.ndarray] = []
    pending: deque = deque()

    def collect(future) -> None:
        a, b = future.result()
        thumbs.extend(a)
        tiny.extend(b)

    with ThreadPoolExecutor(_STATS_WORKERS) as pool:
        chunk: list[np.ndarray] = []
        while True:
            ok, frame = cap.read()
            if ok:
                chunk.append(frame)
            if chunk and (not ok or len(chunk) == _STATS_CHUNK):
                pending.append(pool.submit(_shrink, chunk))
                chunk = []
                while len(pending) > 2 * _STATS_WORKERS:
                    collect(pending.popleft())
            if not ok:
                break
        while pending:
            collect(pending.popleft())
    cap.release()
    return (
        np.stack(thumbs) if thumbs else np.empty((0, _THUMB[1], _THUMB[0]), np.uint8),
        np.stack(tiny) if tiny else np.empty((0, 27, 48, 3), np.uint8),
        fps,
    )


def _predict(model, frames: np.ndarray) -> np.ndarray:
    """Per-frame boundary probabilities, without the package's progress bar in the logs.

    Measured on a 31-min film: splitting the frames across threads or processes did not
    beat one pass (the network already uses the free cores), so it stays one pass."""
    import torch
    with torch.no_grad():
        try:
            single, _all = model.predict_frames(torch.from_numpy(frames), quiet=True)
        except TypeError:                      # an older package without ``quiet``
            single, _all = model.predict_frames(torch.from_numpy(frames))
    return np.asarray(single).reshape(-1)


def _transnet_cuts(tiny: np.ndarray, fps: float) -> Optional[list[float]]:
    """Boundary times from TransNetV2, or None when it is not installed.

    Optional on purpose: the desktop build has no torch, and the threshold
    detectors alone still produce a shotlist. When it IS installed it is the
    only opinion that sees a cut buried inside a whip-pan."""
    if len(tiny) < 2:
        return None
    try:
        import torch
        from transnetv2_pytorch import TransNetV2
    except ImportError:
        logger.info("video_analyzer.cuts: TransNetV2 not installed — threshold detectors only")
        return None

    model = TransNetV2()
    model.eval()
    probs = _predict(model, tiny)

    # The network marks every frame INSIDE a transition; one boundary is the
    # peak of each run above the threshold.
    cuts: list[float] = []
    i = 0
    while i < len(probs):
        if probs[i] >= _TRANSNET_THRESHOLD:
            j = i
            while j + 1 < len(probs) and probs[j + 1] >= _TRANSNET_THRESHOLD:
                j += 1
            peak = i + int(np.argmax(probs[i : j + 1]))
            cuts.append((peak + 1) / fps)
            i = j + 1
        else:
            i += 1
    return cuts


def _unchanged(thumbs: np.ndarray, frame: int, gap: int = _VETO_GAP) -> float:
    """How alike the picture is ``gap`` frames either side of a frame."""
    if len(thumbs) == 0:
        return 0.0
    return _similarity(
        thumbs[max(0, min(len(thumbs) - 1, frame - gap))],
        thumbs[max(0, min(len(thumbs) - 1, frame + gap))],
    )


# Two variants of this veto were measured on the CGI trailer and both were
# worse than judging the candidate frame itself:
#
#   · judge the strongest change within ±0.4s: a candidate sitting near a real
#     cut inherits that cut's change and survives — 96 cuts, 14 vetoed, and
#     three of the four verified false boundaries came back;
#   · move the boundary to that strongest change: every cut shifted by up to
#     0.4s, some onto a nearby flash instead of the cut.
#
# Judging exactly where the detector fired: 83 cuts, 25 vetoed (18 of them on
# the invisible 4-second seams), the buried scenes inside the 10.3s "shot"
# recovered. That is the configuration below.


def _similarity(a: np.ndarray, b: np.ndarray) -> float:
    """1.0 = same picture. Zero-mean normalised cross-correlation, so an exposure
    shift alone does not read as a different shot."""
    af = a.astype(np.float32).ravel()
    bf = b.astype(np.float32).ravel()
    af -= af.mean()
    bf -= bf.mean()
    denom = float(np.linalg.norm(af) * np.linalg.norm(bf))
    if denom < 1e-6:
        return 1.0  # two flat frames (black, white flash) — nothing to tell apart
    return float(np.dot(af, bf) / denom)


def _continuity(thumbs: np.ndarray, frame: int, near: int = 1, far: int = 4) -> float:
    """``sim(±near) − sim(±far)`` around ``frame``. ~0 for a hard cut, positive
    for one shot in motion. See the module docstring for the measurements."""
    n = len(thumbs)
    if n == 0:
        return 0.0
    at = lambda k: thumbs[max(0, min(n - 1, k))]  # noqa: E731 — tiny clamp helper
    return _similarity(at(frame - near), at(frame + near)) - _similarity(at(frame - far), at(frame + far))


# Below this, the frames either side of the candidate share almost nothing —
# which no amount of motion inside one shot produces. Measured on the reference
# clip: it recovers a true cut in the middle of the fight (01:07.40, decay 0.22)
# and lets in no false one; every rejected non-cut sits at 0.37 or above.
_HARD_CUT_SIM = 0.25


def motion_evidence(thumbs, frame):
    """Distinguish an abrupt edit from ordinary motion on both sides of it.

    Detector agreement is correlated evidence, not permission to skip this test.
    This only proposes a motion check; an image registration must also agree.
    """
    if frame < 6 or frame + 6 >= len(thumbs):
        return 0.0, 9999.0, 0.0
    at = lambda k: thumbs[frame + k]
    adjacent = _similarity(at(-1), at(0))
    changes = [1 - _similarity(at(i-1), at(i)) for i in range(-5, 7)]
    baseline = max(.005, float(np.median(changes[:4] + changes[8:])))
    return adjacent, (1-adjacent)/baseline, _continuity(thumbs, frame)


def flash_transition(thumbs, frame):
    """A white/black transition may peak after the detector's boundary frame.

    Registering two early frames can match the OLD shot perfectly while missing
    the transition immediately after. Retain candidates near a flat flash/fade.
    """
    window=thumbs[max(0,frame-8):min(len(thumbs),frame+13)]
    return any(float(f.std())<12 and (float(f.mean())>210 or float(f.mean())<30) for f in window)


def _registered(first, second) -> float:
    """Registration score of two consecutive BGR frames (see registered_motion)."""
    import cv2
    images = []
    for image in (first, second):
        h, w = image.shape[:2]
        image = cv2.resize(image, (240, max(32, round(240 * h / w))))
        image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        images.append(image[:max(24, round(image.shape[0] * .75))])
    a, b = images
    if min(float(a.std()), float(b.std())) < 12: return 0.0
    delta, response = cv2.phaseCorrelate(a.astype(np.float32), b.astype(np.float32))
    if response < .1: return 0.0
    matrix = np.array([[1, 0, delta[0]], [0, 1, delta[1]]], dtype=np.float32)
    try:
        cc, matrix = cv2.findTransformECC(a, b, matrix, cv2.MOTION_TRANSLATION,
            (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 100, 1e-5))
    except cv2.error: return 0.0
    if cc < .94 or float(np.linalg.norm(matrix[:, 2])) < 2: return 0.0
    h, w = a.shape
    warped = cv2.warpAffine(a, matrix, (w, h))
    valid = cv2.warpAffine(np.ones_like(a), matrix, (w, h)) > 0
    if valid.mean() < .7: return 0.0
    return _similarity(warped[valid], b[valid])


_SEEK_GAP = 150   # further than this, seeking beats decoding every frame in between


def registered_motions(path, frames) -> dict:
    """``registered_motion`` for many candidate frames with ONE capture, read in order.

    Reopening and seeking the file for every candidate is what made long films time
    out; one pass decodes forward and seeks only across long gaps.
    """
    import cv2
    out = {}
    wanted = sorted({int(f) for f in frames})
    if not wanted:
        return out
    cap = cv2.VideoCapture(str(path))
    pos = 0                                   # index of the frame the next read returns
    try:
        for frame in wanted:
            start = frame - 1
            if start < 0:
                out[frame] = 0.0; continue
            if start < pos or start - pos > _SEEK_GAP:
                cap.set(cv2.CAP_PROP_POS_FRAMES, start); pos = start
            while pos < start and cap.grab():
                pos += 1
            ok_a, a = cap.read()
            ok_b, b = cap.read() if ok_a else (False, None)
            pos += int(ok_a) + int(ok_b)
            out[frame] = _registered(a, b) if ok_a and ok_b and pos - 2 == start else 0.0
    finally:
        cap.release()
    return out


def registered_motion(path, frame):
    """Strong translation registration for continuous pans, including motion blur.

    Require independent phase-correlation support, converged image registration,
    large valid overlap and whole-image agreement. A similar static frame alone
    cannot establish motion. Flat flashes/fades are checked by the caller.
    """
    return registered_motions(path, [frame]).get(int(frame), 0.0)


def detect_cuts(path: Path, *, fps_hint: float = 0.0, continuity_threshold: float = 0.12) -> CutReport:
    """Return the cut times, in seconds, for ``path``.

    Three opinions and two rules. The opinions are two threshold detectors and,
    when it is installed, TransNetV2. The rules:

      VETO     a candidate whose picture is unchanged 0.27s either side is not
               a boundary, whoever proposed it. This is what removes the
               invisible 4-second segment seams of a generated film.
      REFEREE  a candidate only ONE threshold detector proposed, and which the
               network did not confirm, still has to pass the motion-continuity
               test — that is the rule that kept the live-action drama clean.

    ``continuity_threshold`` is the referee's line: a candidate whose similarity
    decays by more than this across the frame is one shot in motion. 0.12 is
    where F1 peaked on the live-action reference clip.
    """
    from scenedetect import SceneManager, open_video
    from scenedetect.detectors import AdaptiveDetector, ContentDetector

    def run(detector) -> list[float]:
        video = open_video(str(path))
        manager = SceneManager()
        manager.auto_downscale = True
        manager.add_detector(detector)
        manager.detect_scenes(video)
        return [scene[0].seconds for scene in manager.get_scene_list()][1:]

    adaptive = run(AdaptiveDetector(min_scene_len=4))
    content = run(ContentDetector(threshold=27.0, min_scene_len=4))

    thumbs, tiny, fps = _frame_stats(path)
    fps = fps_hint or fps
    tol = _DEDUPE_FRAMES / fps
    transnet = _transnet_cuts(tiny, fps)

    # Merge the opinions, remembering who said what: agreement is evidence, and
    # the referee only gets a vote where there is none.
    candidates: list[Cut] = [Cut(at=t, source="adaptive") for t in adaptive]

    def add(times: list[float], name: str) -> None:
        for t in times:
            twin = next((c for c in candidates if abs(c.at - t) <= tol), None)
            if twin:
                if name not in twin.source:
                    twin.source = f"{twin.source}+{name}"
            else:
                candidates.append(Cut(at=t, source=name))

    add(content, "content")
    if transnet is not None:
        add(transnet, "transnet")
    candidates.sort(key=lambda c: c.at)

    return refine_candidates(path, thumbs, fps, candidates, continuity_threshold)


def refine_candidates(path, thumbs, fps, candidates, continuity_threshold=.12):
    """Rerun local evidence rules without re-running detector/model inference."""
    tol = _DEDUPE_FRAMES / fps
    kept: list[float] = []
    kept_detail: list[Cut] = []
    rejected: list[Cut] = []
    # Local evidence first, so every candidate that needs full-resolution motion
    # registration is read in one pass over the file.
    suspects = {}
    for cut in candidates:
        frame = int(round(cut.at * fps))
        cut.unchanged = _unchanged(thumbs, frame)
        cut.continuity = _continuity(thumbs, frame)
        cut.similarity = (
            _similarity(
                thumbs[max(0, min(len(thumbs) - 1, frame - 1))],
                thumbs[max(0, min(len(thumbs) - 1, frame + 1))],
            )
            if len(thumbs)
            else 1.0
        )
        cut.adjacent_similarity, cut.change_ratio, decay = motion_evidence(thumbs, frame)
        if (cut.adjacent_similarity > .35 and decay > .12 and cut.change_ratio < 2.5
                and not flash_transition(thumbs,frame)):
            suspects[id(cut)] = frame
    motions = registered_motions(path, suspects.values()) if suspects else {}
    for cut in candidates:
        if id(cut) in suspects:
            cut.motion_match = motions.get(suspects[id(cut)], 0.0)
            if cut.motion_match >= .94:
                cut.kept = False
                cut.reason = 'continuous registered camera motion; detector votes do not establish an edit'
                rejected.append(cut)
                continue
        # The veto outranks every opinion: nothing happened here.
        if cut.unchanged > _VETO_SIM:
            cut.kept = False
            cut.reason = f"picture unchanged 0.27s either side (sim {cut.unchanged:.2f})"
            rejected.append(cut)
            continue

        # Evidence that outranks the referee: two detectors agree, the network
        # says so, or the picture is simply no longer the same picture.
        confirmed = (
            "+" in cut.source
            or "transnet" in cut.source
            or cut.unchanged < _CHANGED_SIM
        )
        if (
            not confirmed
            and cut.similarity > _HARD_CUT_SIM
            and cut.continuity > continuity_threshold
        ):
            cut.kept = False
            cut.reason = (
                f"continuous motion, not a cut (decay {cut.continuity:.2f}, "
                f"neighbours still {cut.similarity:.2f} alike)"
            )
            rejected.append(cut)
            continue

        if kept and cut.at - kept[-1] <= tol:
            continue
        kept.append(cut.at)
        cut.reason = "confirmed" if confirmed else "referee let it through"
        kept_detail.append(cut)

    logger.info(
        "video_analyzer.cuts: candidates=%d kept=%d rejected=%d",
        len(candidates), len(kept), len(rejected),
    )
    return CutReport(cuts=kept, rejected=rejected, candidates=len(candidates),
                     kept_detail=kept_detail)
