"""Describe what is inside each shot the machine already bounded.

Two tiers, because most shots in a microdrama are talking heads that any model
reads correctly and a handful are 0.3s fight inserts that only a strong one does:

    tier 1  cheap multimodal model, every shot, batched a few at a time
    tier 2  strong model, only the shots tier 1 was unsure of or that are too
            short and too busy to trust a cheap read on

On the reference clip's frame at 60.8s (hand list: "high-angle tight MS") the
cheap tier read "medium shot, high angle" and a small GPT read "close-up, high
angle" — so the cheap tier is not automatically the weaker one, and escalation
is driven by the model's own stated confidence plus shot length, not by assuming
the big model is right.

The model is never shown a timecode it could change and never returns one. It
gets a shot number and a duration for context; start/end come from the cut list
and are re-attached after parsing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

from flowboard.services import avis_text
from flowboard.services.video_analyzer.frames import ShotSpan

logger = logging.getLogger(__name__)

TIER1_MODEL = os.getenv("FLOWBOARD_VISION_TIER1", "gemini-3-8-flash")
TIER2_MODEL = os.getenv("FLOWBOARD_VISION_TIER2", "gpt-5-4")
BATCH = 5
# "Deep" is the same pass with more attention bought: fewer shots per call, so
# each one gets more of the model's context, and a lower bar for sending a shot
# to the strong tier. Roughly doubles the token cost of a video.
DEEP_BATCH = 2
DEEP_ESCALATE_BELOW = 0.8
CONCURRENCY = int(os.getenv("FLOWBOARD_VISION_CONCURRENCY", "4"))
ESCALATE_BELOW = 0.6       # model's own confidence
ESCALATE_SHORT_S = 0.7     # a sub-0.7s shot is 2 frames — too little for a cheap read

SHOT_SIZES = ["EWS", "WS", "MWS", "MS", "MCU", "CU", "ECU", "INSERT", "POV", "OTS"]
ANGLES = ["eye-level", "low angle", "extreme low angle", "high angle", "extreme high angle",
          "bird's-eye", "ground-level", "dutch", "overhead", "profile", "rear", "OTS", "POV"]
MOVES = ["static", "pan left", "pan right", "tilt up", "tilt down", "push-in", "pull-out",
         "tracking", "follow", "crane up", "crane down", "orbit", "handheld", "whip pan",
         "snap zoom", "rack focus", "unclear"]

_SYSTEM = f"""You are a professional film editor, storyboard artist and action \
director reconstructing the editorial shots of a reference video.

The shot boundaries were measured by software and are correct. For each shot you \
are given its number, its duration, 2-5 frames sampled across it in order, any \
dialogue spoken during it, and a one-line note on the neighbouring shots.

Describe ONLY what the frames support. Do not improve the shot. Do not change the \
choreography. Do not invent actions that happen between frames you cannot see.

Anti-hallucination rules:
- If something is uncertain, write "unclear" rather than guessing.
- Do not name a weapon unless it is visible.
- Do not name a technique, sect or person unless on-screen text or dialogue gives it.
- Do not infer relationships from costume alone.
- Do not claim camera movement unless framing or parallax changes across the frames.
  An actor moving toward camera is not a push-in.
- Treat flashes, energy bursts and impact frames as VFX INSIDE the shot.
- On-screen text: report ONLY text visible in the frames of the shot you are
  describing. Frames from different shots arrive in one message; never carry a
  caption from one shot into another. A name card (a person's name or title,
  usually stylised, shown once) goes in title_card; a line of speech goes in
  subtitle. Copy text exactly as written, diacritics included — it is more
  accurate than the speech-recognition dialogue, especially for names.

Use these exact vocabularies:
  shot_size: {", ".join(SHOT_SIZES)}
  camera_angle: {", ".join(ANGLES)}
  camera_movement: {", ".join(MOVES)}

Return ONE JSON array, one object per shot, in the order given, and nothing else:
[{{
  "shot": <shot number as given>,
  "shot_size": "...",
  "camera_angle": "...",
  "camera_movement": "...",
  "setting": "the place as seen — terrain or room, time of day, weather, light — or 'unclear'",
  "subjects": ["short VISUAL description of each person in frame, e.g. 'bald man, black-red robe, facial tattoos'"],
  "blocking": "where subjects are in frame and which way they face",
  "screen_direction": "left-to-right | right-to-left | toward camera | away from camera | static | unclear",
  "start_pose": "first frame",
  "action": "what physically happens across the frames",
  "reaction": "a reaction, if the shot is one, else null",
  "end_pose": "last frame",
  "expression": "facial expression, or null if no face is readable",
  "vfx": "visible effects, or null",
  "title_card": "burned-in NAME/TITLE card text in THIS shot's frames, verbatim, or null",
  "subtitle": "burned-in DIALOGUE subtitle text in THIS shot's frames, verbatim, or null",
  "continuity_note": "how this shot connects to the previous one",
  "confidence": 0.0-1.0,
  "uncertain": ["field names you were unsure of"]
}}]"""


@dataclass
class VisionStats:
    calls: dict[str, int] = field(default_factory=dict)
    prompt_tokens: dict[str, int] = field(default_factory=dict)
    completion_tokens: dict[str, int] = field(default_factory=dict)
    escalated: int = 0
    failed_batches: int = 0

    def add(self, c: avis_text.Completion) -> None:
        self.calls[c.model] = self.calls.get(c.model, 0) + 1
        self.prompt_tokens[c.model] = self.prompt_tokens.get(c.model, 0) + c.prompt_tokens
        self.completion_tokens[c.model] = self.completion_tokens.get(c.model, 0) + c.completion_tokens

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "escalated": self.escalated,
            "failed_batches": self.failed_batches,
        }


def _neighbour_note(analyses: dict[int, dict], index: int) -> str:
    a = analyses.get(index)
    if not a:
        return "not analysed yet"
    return f"{a.get('shot_size', '?')} — {a.get('action') or a.get('blocking') or '?'}"


def _shot_block(span: ShotSpan, dialogue: str, prev: str, nxt: str) -> list[dict]:
    header = (
        f"SHOT {span.index:03d} — {span.duration:.2f}s, {len(span.frame_paths)} frames in order.\n"
        f"Previous shot: {prev}\nNext shot: {nxt}\n"
        f"Dialogue during this shot: {dialogue or '(none)'}"
    )
    return [avis_text.text_part(header)] + [avis_text.image_part(p) for p in span.frame_paths]


async def _run_batch(
    model: str,
    batch: list[ShotSpan],
    dialogue: dict[int, str],
    known: dict[int, dict],
    stats: VisionStats,
) -> dict[int, dict]:
    content: list[dict] = []
    for span in batch:
        content += _shot_block(
            span,
            dialogue.get(span.index, ""),
            _neighbour_note(known, span.index - 1),
            _neighbour_note(known, span.index + 1),
        )
    content.append(avis_text.text_part(
        f"Analyse shots {', '.join(f'{s.index:03d}' for s in batch)}. Return the JSON array."
    ))
    completion = await avis_text.complete(
        model,
        [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": content}],
        temperature=0.1,
    )
    stats.add(completion)
    parsed = avis_text.extract_json(completion.text)
    if isinstance(parsed, dict):
        parsed = [parsed]
    out: dict[int, dict] = {}
    wanted = {s.index for s in batch}
    for item in parsed if isinstance(parsed, list) else []:
        try:
            idx = int(item.get("shot"))
        except (TypeError, ValueError):
            continue
        if idx in wanted:
            item["_model"] = model
            out[idx] = item
    return out


def _needs_escalation(span: ShotSpan, analysis: Optional[dict], *, deep: bool = False) -> bool:
    if analysis is None:
        return True
    try:
        conf = float(analysis.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    if conf < (DEEP_ESCALATE_BELOW if deep else ESCALATE_BELOW):
        return True
    # A field the cheap tier said it was unsure of is exactly what the strong
    # one is for — but only when the caller asked for the deeper read.
    if deep and analysis.get("uncertain"):
        return True
    # A 2-frame shot with real action in it is exactly where a cheap read slips.
    busy = bool(analysis.get("vfx")) or bool(analysis.get("reaction")) or analysis.get("camera_movement") not in (None, "static")
    return span.duration < ESCALATE_SHORT_S and busy


async def analyze(
    spans: list[ShotSpan],
    dialogue: dict[int, str],
    *,
    on_progress=None,
    sink: Optional[dict[int, dict]] = None,
    deep: bool = False,
) -> tuple[dict[int, dict], VisionStats]:
    """Analyse ``spans``. Pass ``sink`` to have results land in a dict the caller
    already holds — shots analysed on an earlier run then serve as neighbour
    context, and the caller can checkpoint it from ``on_progress``."""
    stats = VisionStats()
    results: dict[int, dict] = sink if sink is not None else {}
    sem = asyncio.Semaphore(CONCURRENCY)
    done = 0
    total = len(spans)  # grows by the escalated shots once tier 1 has named them

    async def guarded(model: str, batch: list[ShotSpan]) -> None:
        nonlocal done
        async with sem:
            try:
                results.update(await _run_batch(model, batch, dialogue, results, stats))
            except Exception as exc:  # noqa: BLE001 — one bad batch must not sink the video
                stats.failed_batches += 1
                logger.warning("vision batch %s failed: %s", [s.index for s in batch], exc)
            done += len(batch)
            if on_progress:
                on_progress(done, total)

    # Tier 1 — every shot. Batches go out in order so later batches can read
    # the neighbour notes earlier ones already filled in.
    size = DEEP_BATCH if deep else BATCH
    batches = [spans[i : i + size] for i in range(0, len(spans), size)]
    await asyncio.gather(*(guarded(TIER1_MODEL, b) for b in batches))

    # Tier 2 — only what tier 1 was unsure of. One shot per call: these are the
    # hard ones, and a strong model should spend its attention on one at a time.
    hard = [s for s in spans if _needs_escalation(s, results.get(s.index), deep=deep)]
    stats.escalated = len(hard)
    if hard:
        total += len(hard)
        await asyncio.gather(*(guarded(TIER2_MODEL, [s]) for s in hard))

    return results, stats


def summarise(analysis: dict) -> str:
    """One line for logs and the neighbour notes."""
    return json.dumps(
        {k: analysis.get(k) for k in ("shot_size", "camera_angle", "action")}, ensure_ascii=False
    )
