"""faster-whisper, run as its own process.

    python -m flowboard.services.video_analyzer.asr_worker <audio.wav> <out.json> [model] [language]

Never import this module into the server. faster-whisper pulls in PyAV, and
PyAV ships its own copy of FFmpeg's libavdevice — the same library OpenCV
bundles. When both load into one process on macOS the Objective-C runtime
warns that the duplicate classes "may cause spurious casting failures and
mysterious crashes", and the server process is exactly the one that loads
OpenCV. A separate process keeps the two copies apart, and keeps a CPU-bound
transcription from starving every other request the server is handling.

Output is deliberately plain JSON so the parent never has to import anything
from this side:

    {"language": "vi", "segments": [{"start": 1.2, "end": 3.4, "text": "...",
                                     "words": [{"start":..,"end":..,"word":".."}]}]}
"""
from __future__ import annotations

import json
import sys
import time


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: asr_worker <audio.wav> <out.json> [model] [language]", file=sys.stderr)
        return 2
    audio, out = argv[1], argv[2]
    model_name = argv[3] if len(argv) > 3 and argv[3] else "medium"
    language = argv[4] if len(argv) > 4 and argv[4] else None

    from faster_whisper import WhisperModel

    started = time.time()
    # No CUDA on Apple Silicon. int8 on CPU is the practical setting: close to
    # float16 accuracy, a fraction of the memory, and several times faster.
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segments, info = model.transcribe(
        audio,
        language=language,
        word_timestamps=True,
        vad_filter=True,  # skip the long silences between lines instead of hallucinating into them
        # OFF on purpose. With it on (the default) each 30s window is prompted
        # with the text of the last one — so a single hallucination becomes a
        # loop. The reference fight clip opens on 16s of battle SFX with no
        # dialogue; Whisper invented a YouTube outro ("Hãy subscribe cho kênh…")
        # for that window and then repeated it verbatim across 48s→129s,
        # replacing every real line. Action-heavy dramas almost always open
        # this way, so the loop is the common case, not the edge case.
        condition_on_previous_text=False,
    )

    payload = {"language": info.language, "language_probability": info.language_probability, "segments": []}
    for seg in segments:
        payload["segments"].append(
            {
                "start": round(seg.start, 3),
                "end": round(seg.end, 3),
                "text": seg.text.strip(),
                "words": [
                    {"start": round(w.start, 3), "end": round(w.end, 3), "word": w.word}
                    for w in (seg.words or [])
                ],
            }
        )
    payload["elapsed_s"] = round(time.time() - started, 1)
    payload["model"] = model_name

    with open(out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
