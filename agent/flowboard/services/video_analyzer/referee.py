"""Ask a vision model about the cuts the measurements could not settle.

The three detectors and the similarity veto in ``cuts.py`` agree on most of a
film, and where they agree there is nothing to ask. What is left is a handful of
close calls: one detector, a picture that stayed largely the same, a camera that
was moving. Those are exactly the cases the machine gets wrong in both
directions — a push-in read as a cut, a cut buried in a whip-pan read as motion.

Measured 2026-09-21 against a pure-vision pass over the same 180 seconds of a
ReelShort drama: the vision model found 83 of the machine's 88 cuts, invented
NONE of its own, and correctly declined the one the machine had wrong (a push-in
at 108.0s read as a cut). It missed two real cuts, both of which the machine had
— which is why this runs as a referee over doubtful cases rather than as the
detector. Vision is precise and slow; the machine is exhaustive and cheap.

So only the doubtful ones are sent, and the model is only ever allowed to
REMOVE a cut, never to add one: adding is what it is bad at.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional

from flowboard.services import avis_text

logger = logging.getLogger(__name__)

REFEREE_MODEL = "gpt-5-4"
# How far either side of the cut to sample. Wide enough to clear motion blur on
# the frame itself, tight enough that a real cut still separates the two.
_GAP_S = 0.12
_CONC = 4

_SYSTEM = """\
You are a film editor judging one join in a finished film. The two frames you \
get are 0.24 seconds apart: BEFORE a suspected cut, and AFTER it.

A CUT is any change of CAMERA SETUP. In a quarter of a second a camera cannot \
be carried anywhere, so answer true whenever the viewpoint has jumped:
  · a reverse angle — the shot/reverse-shot of a conversation is TWO shots
  · a change of framing size: wide to close-up, close-up to wide
  · a change of angle or of which side of the subject we are on
  · a cut to a detail, an insert, a different subject, a different place
Being the same scene, the same room and the same people is NOT a reason to say \
false. Most cuts in a film join two shots of the same scene.

It is NOT a cut only when one camera kept running: a push-in, a pan, a tilt or \
handheld drift that could physically travel that far in 0.24s; a person or \
object moving through a held frame; the light changing; a dissolve mid-way.

Return ONE JSON object and nothing else:
{"cut": true|false, "why": "<one short sentence naming what decided it>"}

If the viewpoint moved further than a camera could travel in a quarter second, \
it is a cut."""


def _doubtful(detail: list[dict], *, sim_floor: float = 0.45) -> list[dict]:
    """The close calls, and only those.

    A cut two detectors found, or one the network found, or one where the
    picture is simply no longer the same picture, is not a close call — those
    are the "confirmed" branch in cuts.py and they are left alone.
    """
    out = []
    for c in detail:
        src = str(c.get("source") or "")
        confirmed = "+" in src or "transnet" in src
        if confirmed:
            continue
        # Still recognisably the same picture either side: that is the doubt.
        if float(c.get("similarity") or 0.0) >= sim_floor or float(c.get("unchanged") or 0.0) >= sim_floor:
            out.append(c)
    return out


def _grab(video: Path, at: float, out: Path) -> bool:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{max(at, 0):.3f}", "-i", str(video),
         "-frames:v", "1", "-vf", "scale=240:-1", "-q:v", "5", "-y", str(out)],
        check=False,
    )
    return out.is_file() and out.stat().st_size > 0


def _verdict(text: str) -> Optional[bool]:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        got = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return bool(got.get("cut")) if isinstance(got, dict) and "cut" in got else None


async def review(
    video: Path, kept: list[float], detail: list[dict], *, model: str = REFEREE_MODEL
) -> tuple[list[float], list[dict]]:
    """Return the cut list with the model's rejections removed, and what it said.

    Anything that cannot be judged — no frame, no reply, a malformed one — keeps
    the cut. The measurement stands unless the model actively overturns it.
    """
    doubtful = _doubtful(detail)
    if not doubtful:
        return kept, []
    tmp = Path(tempfile.mkdtemp(prefix="referee-"))
    sem = asyncio.Semaphore(_CONC)

    async def one(c: dict) -> Optional[dict]:
        at = float(c["at"])
        async with sem:
            a, b = tmp / f"{at:.3f}a.jpg", tmp / f"{at:.3f}b.jpg"
            ok = await asyncio.to_thread(_grab, video, at - _GAP_S, a)
            ok = ok and await asyncio.to_thread(_grab, video, at + _GAP_S, b)
            if not ok:
                return None
            content = [
                avis_text.text_part("BEFORE:"), avis_text.image_part(a),
                avis_text.text_part("AFTER:"), avis_text.image_part(b),
                avis_text.text_part("Is there a cut between them? JSON only."),
            ]
            try:
                out = await avis_text.complete(
                    model,
                    [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": content}],
                    max_tokens=400,
                )
            except Exception as exc:  # noqa: BLE001 — a cut we cannot ask about stays
                logger.warning("referee %.2fs failed: %s", at, exc)
                return None
            said = _verdict(out.text)
            if said is None or said:
                return None
            return {"at": at, "source": c.get("source"),
                    "similarity": c.get("similarity"), "why": out.text[:200]}

    dropped = [d for d in await asyncio.gather(*(one(c) for c in doubtful)) if d]
    drop_at = {round(d["at"], 3) for d in dropped}
    out = [t for t in kept if round(t, 3) not in drop_at]
    logger.info(
        "video_analyzer.referee: %d cut, %d doubtful, %d overturned → %d kept",
        len(kept), len(doubtful), len(dropped), len(out),
    )
    return out, dropped
