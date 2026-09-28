"""Orchestration: a reference video in, a source analysis and an adaptation out.

Split into two calls on purpose, because they cost very different amounts:

    analyze()  watches the video — cuts, keyframes, speech, vision, sequences,
               entities. Minutes of CPU and most of the model spend. Run once.
    adapt()    re-tells the analysis in a target world — glossary, then every
               shot. Text-only, cheap, and re-run every time a name in the
               glossary is changed or the target style is switched.

The CPU-bound halves run in child processes, side by side: measuring the
picture in machine_worker, transcribing the sound in asr_worker. Both load a
copy of FFmpeg that must not meet the other inside the server. Progress is
reported as (stage, done, total) so a caller can drive a progress bar without
knowing the stages in advance.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Optional

from flowboard.services.video_analyzer import adapt as adapt_mod
# Deliberately NOT importing cuts: it pulls in scenedetect, and scenedetect pulls
# in PyAV next to OpenCV. That half runs in machine_worker's process.
from flowboard.services.video_analyzer import asr, dialogue as dialogue_mod, frames, source_inventory, vision
from flowboard.services.video_analyzer.probe import VideoMeta, timecode
from flowboard.services.video_analyzer.validate import validate

logger = logging.getLogger(__name__)

Progress = Callable[[str, int, int], None]

_MEASURE_TIMEOUT_S = 1800

STAGES = ["probe", "cuts", "keyframes", "speech", "vision", "story", "inventory", "source_verify", "glossary", "adapt", "validate"]


def _noop(stage: str, done: int, total: int) -> None:  # pragma: no cover
    pass


class _Timer:
    def __init__(self) -> None:
        self.stages: dict[str, float] = {}
        self._t = time.monotonic()

    def lap(self, stage: str) -> None:
        now = time.monotonic()
        self.stages[stage] = round(now - self._t, 1)
        self._t = now


def _meta_from(data: dict) -> VideoMeta:
    return VideoMeta(
        duration=data["duration"], fps=data["fps"], width=data["width"],
        height=data["height"], has_audio=data["has_audio"],
    )


async def _measure(video: Path, work_dir: Path, on_progress: Progress, *, deep: bool = False) -> dict:
    """Probe, cuts and keyframes, in the machine worker process."""
    out = work_dir / "machine.json"
    if out.exists():
        return json.loads(out.read_text(encoding="utf-8"))
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "flowboard.services.video_analyzer.machine_worker",
        str(video), str(work_dir), *(["--deep"] if deep else []),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def pump() -> None:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            parts = raw.decode(errors="replace").split()
            if len(parts) == 4 and parts[0] == "PROGRESS":
                on_progress(parts[1], int(parts[2]), int(parts[3]))

    try:
        _, err = await asyncio.wait_for(asyncio.gather(pump(), proc.stderr.read()), timeout=_MEASURE_TIMEOUT_S)  # type: ignore[union-attr]
        await proc.wait()
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"cut detection timed out after {_MEASURE_TIMEOUT_S}s")
    if proc.returncode != 0 or not out.exists():
        # Skip the objc duplicate-class chatter; the traceback's last lines say what broke.
        lines = [l for l in err.decode(errors="replace").splitlines() if l.strip() and not l.startswith("objc[")]
        raise RuntimeError("cut detection failed: " + " | ".join(lines[-3:])[:400])
    return json.loads(out.read_text(encoding="utf-8"))


async def _speech(video: Path, work_dir: Path, language: Optional[str]) -> dict:
    done = work_dir / "transcript.json"
    if done.exists():
        return json.loads(done.read_text(encoding="utf-8"))
    wav = await asyncio.to_thread(asr.extract_audio, video, work_dir / "audio.wav")
    return await asr.transcribe(wav, work_dir, language=language)


async def _story(shots: list[dict], work_dir: Path):
    """Keep successful story inputs stable across source-agent resumes.

    Rebuilding sequences on every resume changed the downstream input digest,
    invalidating already paid source verification. Failed story stages remain
    retryable; a successful sibling stage does not have to be purchased again.
    """
    digest = hashlib.sha256(json.dumps({
        "version": 1, "shots": shots,
        "models": [adapt_mod.TEXT_MODEL, adapt_mod.FALLBACK_MODEL],
        "prompts": [adapt_mod._SEQUENCE_SYSTEM, adapt_mod._ENTITY_SYSTEM],
    }, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    path = work_dir / "story.v1.json"
    cache = {"digest": digest, "stages": {}}
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        if saved.get("digest") == digest and isinstance(saved.get("stages"), dict):
            cache = saved
    except (OSError, ValueError, AttributeError):
        pass
    stats = adapt_mod.TextStats()

    async def stage(name, build, expected):
        value = cache["stages"].get(name)
        if isinstance(value, expected):
            return value
        value = await build(shots, stats)
        if not isinstance(value, expected):
            raise ValueError(f"Invalid {name} result")
        cache["stages"][name] = value
        work_dir.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
        return value

    sequences, entities = await asyncio.gather(
        stage("sequences", adapt_mod.build_sequences, list),
        stage("entities", adapt_mod.extract_entities, dict),
        return_exceptions=True,
    )
    errors = []
    if isinstance(sequences, BaseException):
        errors.append(f"sequences: {sequences}")
        sequences = adapt_mod.tile_sequences([], len(shots))
    if isinstance(entities, BaseException):
        errors.append(f"entities: {entities}")
        entities = {"characters": [], "sects": [], "locations": [], "techniques": []}
    return sequences, entities, stats, errors


async def analyze(video: Path, work_dir: Path, *, language: Optional[str] = None,
                  deep: bool = False, on_progress: Progress = _noop) -> dict:
    """Watch ``video`` and return the source analysis. Never renames anything.

    ``deep`` reads every shot harder — more keyframes, fewer shots per vision
    call, a lower bar for the strong tier. Roughly double the tokens, for the
    videos where the shot detail is the point."""
    work_dir.mkdir(parents=True, exist_ok=True)
    timer = _Timer()

    # Measuring the picture and transcribing the sound share nothing, so they
    # run side by side, each in its own process. A video with no audio track
    # makes the speech side fail fast inside ffmpeg; that is caught below.
    on_progress("speech", 0, 1)
    measured, speech = await asyncio.gather(
        _measure(video, work_dir, on_progress, deep=deep),
        _speech(video, work_dir, language),
        return_exceptions=True,
    )
    if isinstance(measured, BaseException):
        raise measured
    on_progress("speech", 1, 1)
    timer.stages.update(measured["timings_s"])
    timer.lap("measure+speech")

    meta = _meta_from(measured["video"])
    transcript: dict = {"segments": []}
    speech_error: Optional[str] = None
    if isinstance(speech, BaseException):
        if meta.has_audio:  # a silent shotlist beats no shotlist
            speech_error = str(speech)
            logger.warning("video_analyzer: speech failed, continuing without dialogue: %s", speech)
    else:
        transcript = speech

    spans = [
        frames.ShotSpan(
            index=s["index"], start=s["start"], end=s["end"],
            frame_paths=[work_dir / f for f in s["frames"]],
        )
        for s in measured["spans"]
    ]

    dialogue: dict[int, str] = {}
    for span in spans:
        lines = asr.lines_for_shot(transcript, span.start, span.end)
        if lines:
            dialogue[span.index] = " ".join(l.text for l in lines).strip()

    # Vision is the expensive call. Whatever an earlier run already paid for is
    # kept in vision.json and only the shots still missing are sent again — so a
    # failure further down never costs the whole video a second time.
    vision_file = work_dir / "vision.json"
    analyses: dict[int, dict] = {}
    if vision_file.exists():
        analyses = {int(k): v for k, v in json.loads(vision_file.read_text(encoding="utf-8")).items()}
    todo = [s for s in spans if s.index not in analyses]
    vstats = vision.VisionStats()
    on_progress("vision", len(spans) - len(todo), len(spans))
    if todo:
        def checkpoint(done: int, total: int) -> None:
            # Every batch, not once at the end: a restart mid-vision keeps what it paid for.
            vision_file.write_text(json.dumps(analyses, ensure_ascii=False), encoding="utf-8")
            on_progress("vision", len(spans) - len(todo) + done, len(spans) - len(todo) + total)

        _, vstats = await vision.analyze(todo, dialogue, on_progress=checkpoint, sink=analyses, deep=deep)
        vision_file.write_text(json.dumps(analyses, ensure_ascii=False), encoding="utf-8")
    timer.lap("vision")

    shots = [
        {
            "shot": span.index,
            "start": round(span.start, 3),
            "end": round(span.end, 3),
            "frames": [str(p.relative_to(work_dir)) for p in span.frame_paths],
            "dialogue": dialogue.get(span.index, ""),
            "source": _clean_analysis(analyses.get(span.index)),
        }
        for span in spans
    ]

    # Speech was sliced per shot so the vision pass could read it. Now that the
    # burned-in subtitles are known, rebuild it as whole lines, each owned by
    # one shot — see dialogue.py for why a per-shot slice repeats itself.
    track = dialogue_mod.build_track(shots, transcript)
    dialogue_mod.attach(shots, track)

    on_progress("story", 0, 2)
    sequences, entities, tstats, story_errors = await _story(shots, work_dir)
    timer.lap("story")
    on_progress("story", 2, 2)

    # Source asset identity and state are established before any creative
    # adaptation. A separate source-frame verifier can request bounded extra
    # frames; this is not generated-video QA. Legacy vision caches are reused,
    # while this pass owns its versioned, input-fingerprinted checkpoint.
    on_progress("inventory", 0, len(shots))
    on_progress("source_verify", 0, len(shots))
    try:
        inventory, verification = await source_inventory.analyze(
            video, work_dir, shots, sequences, fps=meta.fps,
            deep=bool(measured.get("deep", deep)), on_progress=on_progress,
        )
    except Exception as exc:  # preserve the already measured/source-described film
        logger.warning("video_analyzer: source inventory failed: %s", exc)
        inventory, verification = source_inventory.unverified(shots, str(exc)[:1000])
    timer.lap("source_inventory")

    qa = validate(shots, duration=meta.duration, frame=meta.frame, sequences=sequences)
    missing = [s["shot"] for s in shots if not s["source"]]
    if missing:
        qa.error("vision_incomplete", f"{len(missing)} shots have no analysis yet — run analyse again to fill them")
    for err in story_errors:
        qa.warn("story_failed", err)
    if verification.get("status") != "verified":
        qa.warn("source_inventory_review", f"Source asset verification: {verification.get('status')}; "
                f"{len(verification.get('unresolved_shots') or [])} shots require review")
    result = {
        "video": {"path": str(video), **meta.as_dict()},
        "cuts": measured["cuts"],
        "transcript": transcript,
        "dialogue_track": [l.as_dict() for l in track],
        "speech_error": speech_error,
        "story_errors": story_errors,
        "deep": deep,
        "shots": shots,
        "sequences": sequences,
        "entities": entities,
        "scene_inventory": inventory,
        "source_verification": verification,
        "validation": qa.as_dict(),
        "usage": {"vision": vstats.as_dict(), "story": asdict(tstats)},
        "timings_s": timer.stages,
    }
    # The original extractor is a draft. Final quality repair has its own
    # evidence-bound journal and never needs to repeat ASR, cuts or vision.
    if (verification.get("reviewed_shots") and
            os.getenv("FLOWBOARD_SOURCE_REFINEMENT", "on").lower() not in {"off", "0", "false"}):
        from . import source_refinement
        result = await source_refinement.refine(video, work_dir, result, on_progress=on_progress)
        timer.lap("source_refinement")
        result["timings_s"] = timer.stages
    return result


def _clean_analysis(item: Optional[dict]) -> Optional[dict]:
    """Drop anything a model returned that belongs to the machine timeline."""
    if not item:
        return None
    return {k: v for k, v in item.items() if k not in ("start", "end", "duration", "timecode", "shot")}


def stale_source_shots(adaptation: dict | None) -> set[int]:
    """Only explicit refinement markers invalidate legacy adapted shots."""
    return {int(n) for n in (adaptation or {}).get("stale_source_shots") or []
            if not isinstance(n, bool) and str(n).isdigit() and int(n) > 0}


def changed_source_shots(before: dict, after: dict) -> set[int]:
    """Compare visual meaning, including the profiles each shot depends on.

    Additional proof frames and verifier bookkeeping do not change a source
    description. Scene unions likewise do not make an absent person relevant
    to every shot in that scene; environment profiles still provide context.
    """
    def semantic(value):
        if isinstance(value, dict):
            return {k: semantic(v) for k, v in value.items()
                    if k not in {"evidence_ids", "_model", "confidence"}}
        if isinstance(value, list):
            return [semantic(v) for v in value]
        return value

    def signatures(analysis):
        inventory = analysis.get("scene_inventory") or {}
        assets = {a["id"]: a for a in (inventory.get("assets") or []) +
                  (inventory.get("screen_graphics") or [])}
        scenes = {s["id"]: s for s in inventory.get("scenes") or []}
        observations = inventory.get("shots") or {}
        output = {}
        for shot in analysis.get("shots") or []:
            n = int(shot["shot"])
            observation = observations.get(str(n), observations.get(n, {}))
            scene = scenes.get(observation.get("scene_id")) or {}
            ids = set()
            for item in (observation.get("asset_presence") or []) + (observation.get("screen_graphics") or []):
                ids.update(key for key in [item.get("asset_id"), item.get("holder_id"),
                                          *(item.get("contains_ids") or [])] if key)
            ids.update(key for key in scene.get("present_asset_ids") or []
                       if (assets.get(key) or {}).get("kind") == "environment")
            pending = list(ids)
            while pending:
                asset = assets.get(pending.pop()) or {}
                for key in (asset.get("member_ids") or []) + (asset.get("depends_on_asset_ids") or []):
                    if key not in ids:
                        ids.add(key)
                        pending.append(key)
            scene_context = {k: v for k, v in scene.items()
                             if k not in {"shot_ids", "present_asset_ids", "screen_graphic_ids"}}
            output[n] = semantic({"source": shot.get("source") or {},
                                  "observation": observation, "scene": scene_context,
                                  "profiles": {key: assets.get(key) for key in sorted(ids)}})
        return output

    old, new = signatures(before), signatures(after)
    return {n for n, value in new.items() if old.get(n) != value}


def mark_stale_adaptation(before: dict, after: dict, adaptation: dict) -> dict:
    """Preserve existing work; record only source changes affecting its shots."""
    if not adaptation.get("shots"):
        return adaptation
    existing = {int(n) for n in adaptation["shots"]}
    stale = stale_source_shots(adaptation) | (changed_source_shots(before, after) & existing)
    return {**adaptation, "stale_source_shots": sorted(stale)} if stale else adaptation


async def adapt(analysis: dict, rules: adapt_mod.AdaptationRules, *,
                glossary: Optional[dict] = None, previous: Optional[dict] = None,
                rebuild_entities: bool = False, cast: Optional[dict] = None,
                deep: bool = False, on_progress: Progress = _noop) -> dict:
    """Re-tell ``analysis`` under ``rules``.

    Pass ``glossary`` to reuse a locked (possibly hand-edited) naming table —
    the entity and glossary passes are then skipped entirely, so the names a
    person fixed are exactly the names that come out.

    Pass ``previous`` (an earlier result of this function) to keep the shots it
    already adapted and fill only the gaps — valid only while the rules and
    glossary are unchanged, which is checked here rather than trusted.
    """
    timer = _Timer()
    stats = adapt_mod.TextStats()
    shots = analysis["shots"]
    meta = _meta_from(analysis["video"])
    ensure_dialogue(analysis)

    if rebuild_entities:
        # Reading the cast again is one cheap call, and it is the only way a
        # fixed entity rule (an appellation that was mistaken for a name)
        # reaches a video whose vision is already paid for.
        on_progress("glossary", 0, 1)
        analysis["entities"] = await adapt_mod.extract_entities(shots, stats)
    if glossary is None:
        on_progress("glossary", 0, 1)
        glossary = await adapt_mod.build_glossary(analysis.get("entities") or {}, rules, stats)
        on_progress("glossary", 1, 1)
    timer.lap("glossary")

    if cast:
        glossary = _reconcile_names(glossary, cast)

    stale = stale_source_shots(previous)
    kept: dict[int, dict] = {}
    # Compare like with like: the stored glossary may predate the bible's names
    # ("Sienna Rothwell — Queen of the Senior Class…" where the bible says
    # "Sienna Rothwell"), and reconciling only the new one made every gap-fill
    # after a cast rename look like a changed glossary — re-adapting all 432 shots.
    before = (previous or {}).get("glossary")
    if cast and before:
        before = _reconcile_names(before, cast)
    if previous and before == glossary and previous.get("rules") == rules.as_dict():
        kept = {int(k): v for k, v in (previous.get("shots") or {}).items() if int(k) not in stale}
    todo = {s["shot"] for s in shots} - set(kept)

    on_progress("adapt", len(kept), len(shots))
    adapted = dict(kept)
    regenerated = {}
    if todo:
        regenerated = await adapt_mod.adapt_shots(
            shots, analysis.get("sequences") or [], glossary, rules, stats,
            only=todo, entities=analysis.get("entities"), cast=cast, deep=deep,
            on_progress=lambda d, t: on_progress("adapt", len(kept) + d, len(shots)),
        )
        regenerated = {n: value for n, value in regenerated.items() if n in todo}
        adapted.update(regenerated)
    timer.lap("adapt")

    on_progress("validate", 0, 1)
    qa = validate(
        shots, duration=meta.duration, frame=meta.frame,
        sequences=analysis.get("sequences") or [], glossary=glossary, adapted=adapted,
    )
    on_progress("validate", 1, 1)
    out = {
        "rules": rules.as_dict(),
        "glossary": glossary,
        "entities": analysis.get("entities") or {},
        "shots": {str(k): v for k, v in sorted(adapted.items())},
        "validation": qa.as_dict(),
        "usage": asdict(stats),
        "timings_s": timer.stages,
    }
    remaining_stale = stale.intersection(s["shot"] for s in shots) - set(regenerated)
    if remaining_stale:
        # A provider may return only some shots. Retain the old editable draft,
        # but never let it count as regenerated or enter a new board/export.
        out["stale_source_shots"] = sorted(remaining_stale)
        for n in remaining_stale:
            old = (previous or {}).get("shots", {}).get(str(n))
            if old is not None:
                out["shots"][str(n)] = old
    # A gap-fill keeps the shots it kept, so it keeps what was done to them: the
    # conform's record of replaced words (the only way to undo it) and the hand
    # corrections. A fresh run replaces the shots, and these go with them.
    if kept:
        for key in ("conform", "edits"):
            if (previous or {}).get(key):
                out[key] = previous[key]
    return out


def _reconcile_names(glossary: dict, cast: dict) -> dict:
    """One name per person, and the cast bible owns it.

    Two passes name people: the glossary (built from the entity list) and the
    bible (built from the frames, and the one whose names go on the sheets).
    Left alone they disagree — on a Vietnamese drama the glossary romanised
    "Dương Viêm" to "Yang Yan" while the sheets said "Dương Viêm", and the
    shotlist then used the romanisation 230 times against the sheets' 20. A
    reader cannot tell they are the same person, and neither can a video model
    handed both.

    So every glossary entry that resolves to a character in the bible is
    rewritten to the bible's name — aliases included, since they share a target.
    """
    table = dict(glossary.get("characters") or {})
    if not table:
        return glossary
    rewrite: dict[str, str] = {}
    for character in cast.get("characters") or []:
        source, name = character.get("source_name"), character.get("name")
        if not name:
            continue
        current = table.get(source or "")
        if current and current != name:
            rewrite[current] = name          # the alias group shares this target
        if source:
            table[source] = name
    for src, target in list(table.items()):
        if target in rewrite:
            table[src] = rewrite[target]
    return {**glossary, "characters": table}


def ensure_dialogue(analysis: dict) -> None:
    """(Re)build the analysis's dialogue track, in place.

    Unconditional, not "only if missing": the track is pure and cheap, and an
    analysis adapted before a fix to the stitching would otherwise keep the old
    lines forever — the video's vision is what is expensive, not this."""
    shots = analysis.get("shots") or []
    if not shots:
        return
    track = dialogue_mod.build_track(shots, analysis.get("transcript") or {})
    dialogue_mod.attach(shots, track)
    analysis["dialogue_track"] = [l.as_dict() for l in track]


def shot_timecodes(shot: dict) -> str:
    return f"{timecode(shot['start'])}–{timecode(shot['end'])}"
