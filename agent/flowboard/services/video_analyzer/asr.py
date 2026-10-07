"""Dialogue for a reference video: extract audio, transcribe out-of-process, map to shots.

The transcription itself lives in ``asr_worker`` and is only ever reached
through a subprocess — see that module for why it cannot share the server's
process. This side does the two cheap jobs around it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from flowboard.services.frame_extract import FFMPEG_BIN, _run

logger = logging.getLogger(__name__)

# medium, chosen by measurement on the 153.9s dubbed-Vietnamese reference clip
# (CPU int8, condition_on_previous_text off):
#
#   large-v3        177s, 5.4 GB RAM — three copies of one hallucinated outro
#   large-v3-turbo   89s — still hallucinated over the SFX opening, dropped the first line
#   medium           43s — 19 real lines, no repeats, errors mostly in proper nouns
#
# Bigger Whisper models hallucinate MORE over non-speech, and an action drama is
# mostly non-speech. Proper nouns are the weak spot at every size; the burned-in
# name cards (vision title_card / subtitle) and the glossary pass cover those.
DEFAULT_MODEL = os.environ.get("FLOWBOARD_ASR_MODEL", "medium")
_ASR_TIMEOUT_S = int(os.environ.get("FLOWBOARD_ASR_TIMEOUT", "1800"))


@dataclass
class Line:
    start: float
    end: float
    text: str


def extract_audio(video: Path, out: Path) -> Path:
    """16 kHz mono WAV — the rate Whisper was trained on; anything else is resampled anyway."""
    _run(
        [FFMPEG_BIN, "-v", "error", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(out)],
        timeout=300,
    )
    return out


async def transcribe(audio: Path, work_dir: Path, *, language: Optional[str] = None, multilingual: bool = False) -> dict:
    """Run the worker and return its JSON. Raises RuntimeError with the worker's stderr."""
    out = work_dir / ("transcript.multilingual.v1.json" if multilingual else "transcript.json")
    worker = "asr_multilingual_worker" if multilingual else "asr_worker"
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", f"flowboard.services.video_analyzer.{worker}",
        str(audio), str(out), DEFAULT_MODEL, language or "",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=_ASR_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise RuntimeError(f"ASR timed out after {_ASR_TIMEOUT_S}s")
    if proc.returncode != 0 or not out.exists():
        tail = (err or b"").decode(errors="replace").strip().splitlines()[-3:]
        raise RuntimeError("ASR failed: " + " | ".join(tail)[:400])
    return json.loads(out.read_text(encoding="utf-8"))


def lines_for_shot(transcript: dict, start: float, end: float) -> list[Line]:
    """Dialogue that overlaps a shot, cut to the words actually inside it.

    Whisper segments do not respect cuts — one sentence often runs across a
    reaction shot and back. Word timestamps let the line be split at the cut,
    so the words land on the shot where they are actually spoken. A segment
    with no word timings falls back to whole-segment overlap.
    """
    out: list[Line] = []
    for seg in transcript.get("segments", []):
        if seg["end"] <= start or seg["start"] >= end:
            continue
        words = seg.get("words") or []
        if words:
            # A word belongs to the shot holding its midpoint.
            inside = [w for w in words if start <= (w["start"] + w["end"]) / 2 < end]
            if not inside:
                continue
            text = "".join(w["word"] for w in inside).strip()
            out.append(Line(start=inside[0]["start"], end=inside[-1]["end"], text=text))
        else:
            out.append(Line(start=seg["start"], end=seg["end"], text=seg["text"]))
    return out
