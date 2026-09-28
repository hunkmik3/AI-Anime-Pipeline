"""The measured half of the analysis — probe, cuts, keyframes — in its own process.

    python -m flowboard.services.video_analyzer.machine_worker <video> <work_dir> [--deep]

Why not a thread in the server: PySceneDetect imports PyAV whenever it is
installed (it registers PyAV as a video backend), and faster-whisper installs
it. So importing ``scenedetect`` loads PyAV's libavdevice next to OpenCV's own
copy — the duplicate-FFmpeg clash asr_worker was written to avoid, seen in the
first full run as "Class AVFAudioReceiver is implemented in both …". A child
process holds both copies only for as long as it measures one video, and never
inside the long-lived server.

It also keeps a 60s per-frame decode loop from holding the server's GIL.

Protocol: progress on stdout as ``PROGRESS <stage> <done> <total>`` lines, the
result written to ``<work_dir>/machine.json``, and a non-zero exit with the
reason on stderr on failure.
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict
from pathlib import Path


def _progress(stage: str, done: int, total: int) -> None:
    print(f"PROGRESS {stage} {done} {total}", flush=True)


def run(video: Path, work_dir: Path, *, deep: bool = False) -> dict:
    from flowboard.services.video_analyzer import cuts, frames
    from flowboard.services.video_analyzer.probe import probe

    timings: dict[str, float] = {}
    t = time.monotonic()

    _progress("probe", 0, 1)
    meta = probe(video)
    timings["probe"] = round(time.monotonic() - t, 1)
    t = time.monotonic()

    _progress("cuts", 0, 1)
    report = cuts.detect_cuts(video, fps_hint=meta.fps)
    spans = frames.spans_from_cuts(report.cuts, meta.duration)
    timings["cuts"] = round(time.monotonic() - t, 1)
    t = time.monotonic()
    _progress("cuts", 1, 1)

    _progress("keyframes", 0, len(spans))
    frames.extract_keyframes(video, spans, work_dir / "frames", meta.fps, deep=deep)
    timings["keyframes"] = round(time.monotonic() - t, 1)
    _progress("keyframes", len(spans), len(spans))

    return {
        "video": meta.as_dict(),
        "cuts": {
            "kept": [round(c, 3) for c in report.cuts],
            "candidates": report.candidates,
            "rejected": [asdict(c) for c in report.rejected],
            "kept_detail": [asdict(c) for c in report.kept_detail],
        },
        "spans": [
            {
                "index": s.index,
                "start": s.start,
                "end": s.end,
                "frames": [str(p.relative_to(work_dir)) for p in s.frame_paths],
            }
            for s in spans
        ],
        "timings_s": timings,
        "deep": deep,
    }


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: machine_worker <video> <work_dir>", file=sys.stderr)
        return 2
    video, work_dir = Path(argv[1]), Path(argv[2])
    deep = "--deep" in argv[3:]
    work_dir.mkdir(parents=True, exist_ok=True)
    result = run(video, work_dir, deep=deep)
    (work_dir / "machine.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
