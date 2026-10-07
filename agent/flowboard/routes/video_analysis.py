"""Reference-video endpoints — the "video mẫu" entry into /automation.

``POST   /api/automation/videos``                  upload a video, start analysing it
``GET    /api/automation/videos``                  list (no analysis bodies)
``GET    /api/automation/videos/{id}``             status, progress, analysis, adaptation
``POST   /api/automation/videos/{id}/analyze``     resume an interrupted or failed analysis
``POST   /api/automation/videos/{id}/adapt``       (re)adapt; pass a glossary to lock names
``PATCH  /api/automation/videos/{id}/glossary``    save hand-edited names without re-adapting
``GET    /api/automation/videos/{id}/frames/{f}``  one keyframe
``GET    /api/automation/videos/{id}/source``      the uploaded video, for the review player
``GET    /api/automation/videos/{id}/export``      Markdown or canonical JSON
``POST   /api/automation/videos/{id}/cast``        read the cast and places off the analysis
``POST   /api/automation/videos/{id}/design``      write the design brief for cast and places
``POST   /api/automation/videos/{id}/plate``       generate one character sheet or place plate
``POST   /api/automation/videos/{id}/board``       clips, cast and places for the board
``DELETE /api/automation/videos/{id}``

Work runs as an asyncio task inside the server — the guide's MVP shape, not
its production job queue. The heavy parts are child processes (see
video_analyzer.pipeline), and every stage checkpoints to disk, so a task lost
to a restart resumes from where it stopped instead of paying again.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
import uuid
from urllib.parse import quote
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator
from sqlmodel import select

from flowboard.config import STORAGE_DIR
from flowboard.db import get_session
from flowboard.db.models import VideoAnalysis, AutomationProject, AutomationJob
from flowboard.routes.deps import get_optional_user
from flowboard.services import automation, prompt_writer
from flowboard.services.video_analyzer import board as board_mod
from flowboard.services.video_analyzer import conform as conform_mod
from flowboard.services.video_analyzer import design as design_mod
from flowboard.services.video_analyzer import export, pipeline
from flowboard.services.video_analyzer import adapt as adapt_mod
from flowboard.services.video_analyzer import production, source_inventory, source_refinement, source_protocol_review
from flowboard.services.video_analyzer.adapt import AdaptationRules

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/automation/videos", tags=["automation"])

ROOT = STORAGE_DIR / "video_analysis"
MAX_UPLOAD_BYTES = 1024 * 1024 * 1024  # 1 GB — a microdrama episode is tens of MB
_VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
_FRAME_NAME = re.compile(r"^(?:shot\d{3,6}_\d+|source-review-\d+-[a-f0-9]{12})\.jpg$")
_RUNNING = {"analysing", "adapting", "casting", "designing", "conforming"}

# Strong references to running tasks. asyncio only keeps a weak one, and a
# task that is garbage-collected mid-analysis simply stops.
_TASKS: dict[uuid.UUID, asyncio.Task] = {}

# One lock per video for read-modify-write of the cast column. Plates are
# generated several at a time; without this the second writer reads the cast
# before the first has committed and overwrites its picture — a plate that was
# paid for and then vanished.
_CAST_LOCKS: dict[uuid.UUID, asyncio.Lock] = {}


def _cast_lock(video_id: uuid.UUID) -> asyncio.Lock:
    lock = _CAST_LOCKS.get(video_id)
    if lock is None:
        lock = _CAST_LOCKS[video_id] = asyncio.Lock()
    return lock


def _work_dir(video_id: uuid.UUID) -> Path:
    return ROOT / str(video_id)


def _owner_id(user: Any) -> Optional[uuid.UUID]:
    raw = getattr(user, "id", None)
    return raw if isinstance(raw, uuid.UUID) else None


def _load(session, video_id: uuid.UUID, user: Any) -> VideoAnalysis:
    row = session.get(VideoAnalysis, video_id)
    owner = _owner_id(user)
    if row is None or (owner and row.owner_user_id and row.owner_user_id != owner):
        raise HTTPException(status_code=404, detail="Video không tồn tại.")
    return row


def _is_running(video_id: uuid.UUID) -> bool:
    from flowboard.services.automation_jobs import source_running
    task = _TASKS.get(video_id)
    return bool(task and not task.done()) or source_running(video_id)


def _summary(row: VideoAnalysis) -> dict[str, Any]:
    status = row.status
    # A row still marked running with no task behind it was cut off by a
    # restart. Say so, rather than showing a progress bar that never moves.
    if status in _RUNNING and not _is_running(row.id):
        status = "interrupted"
    video = (row.analysis or {}).get("video") or {}
    auto_film = dict((row.options or {}).get('auto_production') or {})
    if auto_film.get('run_id'):
        with get_session() as s:
            run = s.get(AutomationJob, uuid.UUID(auto_film['run_id']))
            if run and run.project_id == row.automation_project_id:
                auto_film.update(run_status=run.status, run_stage=run.result.get('stage'),
                                 error=run.error or None, output=run.result.get('output'))
    return {
        "id": str(row.id),
        "name": row.name,
        "filename": row.filename,
        "automation_project_id": str(row.automation_project_id) if row.automation_project_id else None,
        "status": status,
        "progress": row.progress or {},
        "error": row.error,
        "duration": video.get("duration"),
        "aspect_ratio": video.get("aspect_ratio"),
        "shot_count": len((row.analysis or {}).get("shots") or []),
        "detail": (row.options or {}).get("detail", "standard"),
        "character_count": len((row.cast or {}).get("characters") or []),
        "environment_count": len((row.cast or {}).get("environments") or []),
        "asset_count": len(((row.analysis or {}).get("scene_inventory") or {}).get("assets") or []),
        "source_verification_status": ((row.analysis or {}).get("source_verification") or {}).get("status", "unverified"),
        "auto_production": auto_film or None,
        "adaptation_version": row.adaptation_version,
        "updated_at": row.updated_at.isoformat(),
    }


def _full(row: VideoAnalysis) -> dict[str, Any]:
    analysis = dict(row.analysis or {})
    transcript = analysis.pop("transcript", None) or {}
    # Word timings are what split lines at cuts; the page only needs the lines.
    analysis["transcript"] = [
        {"start": s["start"], "end": s["end"], "text": s["text"]} for s in transcript.get("segments") or []
    ]
    cast = dict(row.cast or {})
    if "props" in cast or "background_groups" in cast:
        cast["assets"] = list(cast.get("background_groups") or []) + list(cast.get("props") or [])
    return {**_summary(row), "analysis": analysis, "adaptation": row.adaptation or {}, "cast": cast}


def _update(video_id: uuid.UUID, **fields: Any) -> None:
    with get_session() as s:
        row = s.get(VideoAnalysis, video_id)
        if row is None:
            return
        for k, v in fields.items():
            if k == 'progress' and isinstance(v, dict) and 'steps' not in v and (row.progress or {}).get('steps'):
                v = {**v, 'steps': row.progress['steps']}
            setattr(row, k, v)
        row.updated_at = datetime.now(timezone.utc)
        s.add(row)
        s.commit()


def _progress_writer(video_id: uuid.UUID):
    """Throttled: a progress row write per stage change or per second, not per shot."""
    last = {"stage": None, "t": 0.0}

    def write(stage: str, done: int, total: int) -> None:
        now = time.monotonic()
        if stage == last["stage"] and now - last["t"] < 1.0 and done < total:
            return
        last.update(stage=stage, t=now)
        from flowboard.services.production_progress import record_source
        record_source(video_id, stage, done, total)

    return write


def _source_path(video_id: uuid.UUID) -> Optional[Path]:
    return next((p for p in _work_dir(video_id).glob("source.*")), None)


async def _run_analysis(video_id: uuid.UUID, then_adapt: Optional[dict]) -> None:
    source = _source_path(video_id)
    with get_session() as s:
        row = s.get(VideoAnalysis, video_id)
        options = dict((row.options if row else None) or {})
    try:
        if source is None:
            raise RuntimeError("uploaded video is missing from storage")
        _update(video_id, status="analysing", error=None)
        analysis = await pipeline.analyze(
            source, _work_dir(video_id),
            language=options.get("language") or None,
            deep=options.get("detail") == "deep",
            on_progress=_progress_writer(video_id),
            **({'analysis_mode': options['analysis_mode']} if options.get('analysis_mode') in {'fast','one_pass'} else {}),
        )
        analysis["video"]["path"] = source.name  # never leak an absolute server path to the page
        _update(video_id, status="analysed", analysis=analysis, progress={})
    except Exception as exc:  # noqa: BLE001 — the row is where the user sees it
        logger.exception("video_analysis %s: analysis failed", video_id)
        _update(video_id, status="failed", error=str(exc)[:1000])
        return
    if then_adapt is not None:
        await _run_adaptation(video_id, then_adapt, None)


async def _run_adaptation(
    video_id: uuid.UUID, rules_data: dict, glossary: Optional[dict], *, fresh: bool = False
) -> None:
    try:
        with get_session() as s:
            row = s.get(VideoAnalysis, video_id)
            analysis, previous, version = dict(row.analysis), dict(row.adaptation or {}), row.adaptation_version
            cast = dict(row.cast or {})
            deep = (row.options or {}).get("detail") == "deep"
        _update(video_id, status="adapting", error=None)
        rules = AdaptationRules.from_dict(rules_data or previous.get("rules"))
        result = await pipeline.adapt(
            analysis, rules,
            # Starting over means the names start over too: a glossary built
            # from a mistaken entity would otherwise outlive the re-run.
            glossary=glossary if glossary is not None else (None if fresh else previous.get("glossary")),
            previous=None if fresh else (previous or None),
            rebuild_entities=fresh and glossary is None,
            # The bible names people the reference never named; without it the
            # shots would call them "the man in dark armour" for 130 shots.
            cast=cast,
            deep=deep,
            on_progress=_progress_writer(video_id),
        )
        entities = result.pop("entities", None)
        if entities:
            analysis["entities"] = entities
        # The analysis picks up its dialogue track on the way through; save it
        # so the review panel and the board read whole lines, not slices.
        _update(video_id, analysis=analysis, status="adapted", adaptation=result,
                adaptation_version=version + 1, progress={})
    except Exception as exc:  # noqa: BLE001
        logger.exception("video_analysis %s: adaptation failed", video_id)
        _update(video_id, status="failed", error=str(exc)[:1000])


async def _run_cast(video_id: uuid.UUID) -> None:
    try:
        with get_session() as s:
            row = s.get(VideoAnalysis, video_id)
            analysis, adaptation, previous = dict(row.analysis), dict(row.adaptation or {}), dict(row.cast or {})
        _update(video_id, status="casting", error=None, progress={"stage": "cast", "done": 0, "total": 1})
        cast = await board_mod.build_cast(analysis, adaptation)
        # Sheets already generated survive a re-read of the cast: they are
        # matched back by key, because that is what a plate cost money for.
        plates = {e.get("key"): e.get("plate") for bucket in production.BUCKETS.values()
                  for e in previous.get(bucket) or [] if e.get("plate")}
        for entry in [e for bucket in production.BUCKETS.values() for e in cast.get(bucket) or []]:
            if plates.get(entry.get("key")):
                entry["plate"] = plates[entry["key"]]
        _update(video_id, status="adapted" if adaptation.get("shots") else "analysed",
                cast=cast, progress={})
    except Exception as exc:  # noqa: BLE001
        logger.exception("video_analysis %s: cast failed", video_id)
        _update(video_id, status="failed", error=str(exc)[:1000])


async def _run_design(video_id: uuid.UUID, keys: list[str], world: str = "", *, concurrency: int = 3) -> None:
    """Write a design brief for every cast entry, from its own keyframes.

    With ``world`` set the pass runs the other way: the frames are NOT sent and
    each entry is redesigned inside that art direction, keeping only its role
    and what it does. Generate the sheets with ``use_frames=false`` afterwards,
    or the film's own frames will pull the look back.
    """
    try:
        with get_session() as s:
            row = s.get(VideoAnalysis, video_id)
            cast = json.loads(json.dumps(row.cast or {}))
            adaptation = row.adaptation or {}
            rules = adaptation.get("rules") or {}
            # Read inside the session: the row is detached once this block ends,
            # and touching it afterwards raises rather than returning a value.
            was_adapted = bool(adaptation.get("shots"))
        work = _work_dir(video_id)
        entries = [("characters", c) for c in cast.get("characters") or []]
        entries += [("environments", e) for e in cast.get("environments") or []]
        entries += [(b, e) for b in ("background_groups", "props") for e in cast.get(b) or []]
        if keys:
            entries = [(b, e) for b, e in entries if e.get("key") in keys]
        total = len(entries)
        if not total:
            raise RuntimeError("no cast to design — read the cast first")

        _update(video_id, status="designing", error=None,
                progress={"stage": "design", "done": 0, "total": total})
        stats = adapt_mod.TextStats()
        sem = asyncio.Semaphore(max(1, min(16, concurrency)))
        done = 0

        async def one(bucket: str, entry: dict) -> None:
            nonlocal done
            async with sem:
                try:
                    if bucket == "characters":
                        entry["design"] = await design_mod.design_character(
                            entry, work, rules, stats, world=world)
                    elif bucket == "environments":
                        entry["design"] = await design_mod.design_environment(
                            entry, work, rules, stats, world=world)
                    else:
                        entry["design"] = await design_mod.design_asset(
                            entry, work, rules, stats, world=world)
                    entry.pop("design_error", None)
                except Exception as exc:  # noqa: BLE001 — one bad brief must not sink the sheet
                    logger.warning("design %s failed: %s", entry.get("key"), exc)
                    entry["design_error"] = str(exc)[:300]
            done += 1
            _update(video_id, progress={"stage": "design", "done": done, "total": total})

        await asyncio.gather(*(one(b, e) for b, e in entries))
        cast["design_usage"] = {"model": design_mod.DESIGN_MODEL, "calls": stats.calls,
                                "prompt_tokens": stats.prompt_tokens,
                                "completion_tokens": stats.completion_tokens}
        async with _cast_lock(video_id):
            with get_session() as s:
                row = s.get(VideoAnalysis, video_id)
                row.cast = cast
                row.updated_at = datetime.now(timezone.utc)
                s.add(row)
                s.commit()
        # A redesign leaves the adapted shots describing the reference actors'
        # costumes. Bring the shots of everyone just redesigned in line now,
        # rather than trusting someone to remember a second step.
        redesigned = {e.get("key") for b, e in entries
                      if b == "characters" and e.get("design") and not e.get("design_error")}
        stale = {int(n) for n, v in (cast.get("shots") or {}).items()
                 if set(v.get("character_keys") or []) & redesigned}
        if was_adapted and world and stale:
            await _run_conform(video_id, stale)
            return
        _update(video_id, status="adapted" if was_adapted else "analysed", progress={})
    except Exception as exc:  # noqa: BLE001
        logger.exception("video_analysis %s: design failed", video_id)
        _update(video_id, status="failed", error=str(exc)[:1000])


async def _run_conform(video_id: uuid.UUID, only: Optional[set[int]]) -> None:
    """Take the reference's costumes out of the shots, against the redesigned cast."""
    try:
        with get_session() as s:
            row = s.get(VideoAnalysis, video_id)
            adaptation, cast = json.loads(json.dumps(row.adaptation or {})), json.loads(json.dumps(row.cast or {}))
        _update(video_id, status="conforming", error=None,
                progress={"stage": "conform", "done": 0, "total": 1})
        stats = adapt_mod.TextStats()
        failed: list[int] = []
        changes = await conform_mod.conform_shots(
            adaptation, cast, stats, only=only, on_progress=_progress_writer(video_id), failed=failed)
        if failed and not changes:
            raise RuntimeError(f"conform: no reply from {adapt_mod.TEXT_MODEL} for any batch — run it again")
        note = {
            "at": datetime.now(timezone.utc).isoformat(),
            "model": adapt_mod.TEXT_MODEL,
            "answered_by": dict(stats.answered_by),
            "shots_changed": sorted(changes),
            # Run again with these to finish the pass.
            "shots_unanswered": sorted(failed),
            "usage": {"calls": stats.calls, "prompt_tokens": stats.prompt_tokens,
                      "completion_tokens": stats.completion_tokens},
        }
        # Applied to the row as it is now, not to the copy read before the pass:
        # a glossary saved while it ran must survive it.
        with get_session() as s:
            row = s.get(VideoAnalysis, video_id)
            row.adaptation = conform_mod.apply(row.adaptation or {}, changes, note=note)
            row.updated_at = datetime.now(timezone.utc)
            s.add(row)
            s.commit()
        _update(video_id, status="adapted", progress={})
    except Exception as exc:  # noqa: BLE001
        logger.exception("video_analysis %s: conform failed", video_id)
        _update(video_id, status="failed", error=str(exc)[:1000])


def _start(video_id: uuid.UUID, coro, *, operation: dict | None = None) -> None:
    if operation is not None:
        from flowboard.services.automation_jobs import enqueue_source
        coro.close()
        try: enqueue_source(video_id, operation)
        except ValueError as exc: raise HTTPException(409,detail=str(exc)) from exc
        return
    if _is_running(video_id):
        coro.close()
        raise HTTPException(status_code=409, detail="Video này đang được xử lý.")
    task = asyncio.create_task(coro)
    _TASKS[video_id] = task
    task.add_done_callback(lambda _t: _TASKS.pop(video_id, None) if _TASKS.get(video_id) is _t else None)


# ──────────────────────────────── routes ────────────────────────────────


@router.get('/capabilities')
def analysis_capabilities(user=Depends(get_optional_user)) -> dict:
    return {'analysis_modes': ['standard', 'fast', 'one_pass'], 'fast_mode_experimental': True,
            'one_pass_analysis_only': False, 'one_pass_auto_production': True}


@router.post("")
async def upload(
    file: UploadFile = File(...),
    name: str = Form(""),
    project_id: Optional[uuid.UUID] = Form(None),
    rules: str = Form(""),
    # "deep" reads every shot harder (more keyframes, smaller vision batches,
    # readier escalation) for about double the tokens.
    detail: str = Form("standard"),
    language: str = Form(""),
    auto_production: str = Form(""),
    analysis_mode: str = Form("one_pass"),
    user=Depends(get_optional_user),
) -> dict[str, Any]:
    """Store the video and start analysing it. With ``rules`` (JSON), adapt straight after."""
    ext = Path(file.filename or "").suffix.lower()
    if ext not in _VIDEO_EXT:
        raise HTTPException(status_code=415, detail=f"Định dạng {ext or '?'} không hỗ trợ — dùng mp4/mov/webm/mkv.")
    if analysis_mode not in {'standard', 'fast', 'one_pass'}:
        raise HTTPException(status_code=422, detail='analysis_mode must be standard, fast or one_pass')
    try:
        then_adapt = json.loads(rules) if rules.strip() else None
    except ValueError:
        raise HTTPException(status_code=422, detail="rules phải là JSON.")
    if analysis_mode=='one_pass':
        then_adapt=None  # Film controller owns preparation; analysis alone still stops here.

    auto_settings = None
    if auto_production.strip():
        from flowboard.services.source_film import FilmOptions
        try: auto_settings = FilmOptions.model_validate_json(auto_production).model_dump()
        except ValueError as exc: raise HTTPException(422, detail='Invalid film settings: '+str(exc)) from exc

    with get_session() as s:
        if auto_settings:
            from flowboard.services import film_styles
            project = AutomationProject(name=((name.strip() or Path(file.filename or 'video').stem)+' — '+
                (film_styles.metadata(auto_settings['style'])['label'] if film_styles.is_preset(auto_settings['style']) else auto_settings['style']))[:200], owner_user_id=_owner_id(user),
                board={'style':auto_settings['style'],'autoSourceFilm':True,'imageModel':'dola-seedream-5-0-pro',
                       'imageSize':'2K','aspectRatio':auto_settings['aspect_ratio'],
                       'kyc':auto_settings['kyc'],'unmoderated':auto_settings['unmoderated']})
            s.add(project);s.flush();project_id=project.id
        row = VideoAnalysis(
            name=(name.strip() or Path(file.filename or "video").stem)[:200],
            filename=file.filename or "",
            owner_user_id=_owner_id(user),
            automation_project_id=project_id,
            options={
                "detail": "deep" if detail == "deep" else "standard",
                "analysis_mode": analysis_mode,
                **({"auto_production":{"settings":auto_settings,"stage":"queued"}} if auto_settings else {}),
                **({"language": language.strip()} if language.strip() else {}),
            },
        )
        s.add(row)
        s.commit()
        s.refresh(row)
        video_id = row.id

    work = _work_dir(video_id)
    work.mkdir(parents=True, exist_ok=True)
    target = work / f"source{ext}"
    written = 0
    try:
        with target.open("wb") as fh:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="Video lớn hơn 1 GB.")
                fh.write(chunk)
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        with get_session() as s:
            dead = s.get(VideoAnalysis, video_id)
            if dead:
                s.delete(dead)
                if auto_settings:
                    empty_project = s.get(AutomationProject, project_id)
                    if empty_project:
                        s.delete(empty_project)
                s.commit()
        raise

    _start(video_id, _run_analysis(video_id, then_adapt), operation={"task":"film"} if auto_settings else {"task":"analyze","then_adapt":then_adapt})
    with get_session() as s:
        return _summary(s.get(VideoAnalysis, video_id))


@router.get("")
def list_videos(project_id: Optional[uuid.UUID] = None, user=Depends(get_optional_user)) -> list[dict[str, Any]]:
    owner = _owner_id(user)
    with get_session() as s:
        q = select(VideoAnalysis).order_by(VideoAnalysis.updated_at.desc())
        if project_id:
            # Videos with no board yet show on every board rather than
            # disappearing: one uploaded before a board existed is still the
            # video the person is working on.
            q = q.where(
                (VideoAnalysis.automation_project_id == project_id)
                | (VideoAnalysis.automation_project_id.is_(None))  # type: ignore[union-attr]
            )
        rows = [r for r in s.exec(q) if not owner or r.owner_user_id in (None, owner)]
        return [_summary(r) for r in rows]


@router.get("/{video_id}")
def get_video(video_id: uuid.UUID, user=Depends(get_optional_user)) -> dict[str, Any]:
    with get_session() as s:
        return _full(_load(s, video_id, user))


@router.post("/{video_id}/analyze")
async def resume_analysis(video_id: uuid.UUID, user=Depends(get_optional_user)) -> dict[str, Any]:
    with get_session() as s:
        row = _load(s, video_id, user)
    _start(video_id, _run_analysis(video_id, None), operation={"task":"film"} if (row.options or {}).get("auto_production") else {"task":"analyze"})
    with get_session() as s:
        return _summary(s.get(VideoAnalysis, row.id))


async def _run_source_refinement(video_id: uuid.UUID, *, protocol_only: bool = False,
                                 selected_shots: list[int] | None = None, strategy: str = 'full') -> None:
    """Repair a source draft and independently recheck it without discarding ASR."""
    try:
        with get_session() as s:
            row = s.get(VideoAnalysis, video_id)
            analysis = json.loads(json.dumps(row.analysis or {}))
            adapted = bool((row.adaptation or {}).get("shots"))
        source = _source_path(video_id)
        if source is None:
            raise RuntimeError("Video gốc không còn trong bộ lưu trữ.")
        _update(video_id, status="analysing", error=None,
                progress={"stage": "source_refine", "done": 0,
                          "total": len(selected_shots) if selected_shots is not None
                          else len(analysis.get("shots") or [])})
        if protocol_only:
            result = await source_protocol_review.review(source, _work_dir(video_id), analysis,
                on_progress=_progress_writer(video_id),
                **({'selected_shots':selected_shots} if selected_shots is not None else {}))
        else:
            task = source_refinement.refine(source, _work_dir(video_id), analysis,
                                                     on_progress=_progress_writer(video_id), only_unresolved=True,
                                                     selected_shots=selected_shots, strategy=strategy)
            if strategy == 'focused':
                try:
                    result = await asyncio.wait_for(task, timeout=300)
                except TimeoutError as exc:
                    raise RuntimeError('Focused source refinement exceeded its 300-second budget; draft preserved, no automatic retry.') from exc
            else:
                result = await task
        if (result.get("source_verification") or {}).get("retryable"):
            raise RuntimeError("Một số lượt gọi AI chưa hoàn tất. Kết quả từng bước đã lưu; bấm Sửa & đối chiếu để tiếp tục.")
        async with _cast_lock(video_id):
            with get_session() as s:
                row = s.get(VideoAnalysis, video_id)
                if row is None:
                    return
                latest_cast = json.loads(json.dumps(row.cast or {}))
                row.adaptation = pipeline.mark_stale_adaptation(
                    analysis, result, json.loads(json.dumps(row.adaptation or {})))
                row.analysis = result
                row.cast = production.attach_inventory(result, latest_cast) if latest_cast else latest_cast
                row.status = "adapted" if adapted else "analysed"
                row.progress = {}
                row.updated_at = datetime.now(timezone.utc)
                s.add(row)
                s.commit()
    except Exception as exc:
        logger.exception("video_analysis %s: source refinement failed", video_id)
        _update(video_id, status="failed", error=str(exc)[:1000])


class RefineBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shots: list[StrictInt] | None = None

    @field_validator("shots")
    @classmethod
    def valid_shots(cls, shots):
        if shots is not None:
            if not shots or any(number <= 0 for number in shots):
                raise ValueError("shots phải là danh sách số nguyên dương không rỗng.")
            if len(shots) != len(set(shots)):
                raise ValueError("Mỗi shot chỉ được chọn một lần.")
        return shots


class SourceRefineBody(RefineBody):
    strategy: str = Field(default='full', pattern='^(full|focused)$')


@router.post("/{video_id}/refine")
async def refine_source(video_id: uuid.UUID, body: SourceRefineBody | None = None,
                        user=Depends(get_optional_user)) -> dict[str, Any]:
    selected = sorted(body.shots) if body is not None and body.shots is not None else None
    with get_session() as s:
        row = _load(s, video_id, user)
        if not (row.analysis or {}).get("shots") or not (row.analysis or {}).get("scene_inventory"):
            raise HTTPException(status_code=409, detail="Phân tích video và lập danh mục nguồn trước khi sửa, đối chiếu.")
        if selected is not None:
            existing = {shot["shot"] for shot in row.analysis["shots"]}
            missing = set(selected) - existing
            if missing:
                raise HTTPException(status_code=422, detail="Shot không có trong video: " + ", ".join(map(str, sorted(missing))))
    strategy = body.strategy if body else 'full'
    extra = {'strategy':strategy} if strategy != 'full' else {}
    _start(video_id, _run_source_refinement(video_id, selected_shots=selected, **extra), operation={"task":"refine","shots":selected,**extra})
    with get_session() as s:
        return _summary(s.get(VideoAnalysis, video_id))


@router.post("/{video_id}/recheck")
async def recheck_source_protocol(video_id: uuid.UUID, body: RefineBody | None = None,
                                  user=Depends(get_optional_user)) -> dict[str, Any]:
    """Bounded QA-only continuation; never rewrite or accept source facts."""
    selected = sorted(body.shots) if body is not None and body.shots is not None else None
    with get_session() as s:
        row = _load(s, video_id, user)
        if not ((row.analysis or {}).get("source_verification") or {}).get("refinement"):
            raise HTTPException(status_code=409, detail="Sửa và đối chiếu nguồn trước khi kiểm lại kết luận.")
        if selected is not None:
            missing = set(selected) - {shot['shot'] for shot in row.analysis.get('shots',[])}
            if missing:
                raise HTTPException(status_code=422, detail="Shot không có trong video: " + ", ".join(map(str, sorted(missing))))
    _start(video_id, _run_source_refinement(video_id, protocol_only=True, selected_shots=selected), operation={"task":"refine","shots":selected,"protocol_only":True})
    with get_session() as s:
        return _summary(s.get(VideoAnalysis, video_id))


async def _run_source_verification(video_id: uuid.UUID) -> None:
    try:
        with get_session() as s:
            row = s.get(VideoAnalysis, video_id)
            analysis = json.loads(json.dumps(row.analysis or {}))
            adapted = bool((row.adaptation or {}).get("shots"))
            deep = (row.options or {}).get("detail") == "deep"
        source = _source_path(video_id)
        if source is None:
            raise RuntimeError("Video gốc không còn trong bộ lưu trữ.")
        _update(video_id, status="analysing", error=None,
                progress={"stage": "inventory", "done": 0, "total": len(analysis.get("shots") or [])})
        # Explicit reinspection retries unresolved model observations too.
        (_work_dir(video_id) / "source_inventory.v1.json").unlink(missing_ok=True)
        inventory, report = await source_inventory.analyze(source, _work_dir(video_id),
            analysis["shots"], analysis.get("sequences") or [],
            fps=float((analysis.get("video") or {}).get("fps") or 0), deep=deep,
            on_progress=_progress_writer(video_id))
        analysis.update(scene_inventory=inventory, source_verification=report)
        async with _cast_lock(video_id):
            with get_session() as s:
                row = s.get(VideoAnalysis, video_id)
                if row is None:
                    return
                latest_cast = json.loads(json.dumps(row.cast or {}))
                row.analysis = analysis
                row.cast = production.attach_inventory(analysis, latest_cast) if latest_cast else latest_cast
                row.status = "adapted" if adapted else "analysed"
                row.progress = {}
                row.updated_at = datetime.now(timezone.utc)
                s.add(row)
                s.commit()
    except Exception as exc:  # noqa: BLE001 — retain the existing editable analysis
        logger.exception("video_analysis %s: source verification failed", video_id)
        _update(video_id, status="failed", error=str(exc)[:1000])


@router.post("/{video_id}/verify")
async def verify_source(video_id: uuid.UUID, user=Depends(get_optional_user)) -> dict[str, Any]:
    """Reinspect source evidence for an existing analysis; never inspects output clips."""
    with get_session() as s:
        row = _load(s, video_id, user)
        if not (row.analysis or {}).get("shots"):
            raise HTTPException(status_code=409, detail="Phân tích video trước khi kiểm chứng.")
    _start(video_id, _run_source_verification(video_id), operation={"task":"verify"})
    with get_session() as s:
        return _summary(s.get(VideoAnalysis, video_id))


class AcceptBody(BaseModel):
    note: str = Field(default="", max_length=1000)


@router.post("/{video_id}/verify/accept")
async def accept_source(video_id: uuid.UUID, body: AcceptBody = AcceptBody(),
                        user=Depends(get_optional_user)) -> dict[str, Any]:
    """Accept, after reading them, the findings Agent 1 left unresolved.

    Without this a strict board could only ever reach "needs_review": the
    verifier's doubts have no other way to be settled. Refused while any shot
    has no observation at all — that is a failed batch, and needs a new run.
    """
    async with _cast_lock(video_id):
        with get_session() as s:
            row = _load(s, video_id, user)
            if _is_running(row.id):
                raise HTTPException(status_code=409, detail="Video này đang được xử lý.")
            analysis = json.loads(json.dumps(row.analysis or {}))
            inventory = analysis.get("scene_inventory") or {}
            report = analysis.get("source_verification") or {}
            if not inventory.get("shots"):
                raise HTTPException(status_code=409, detail="Chưa có danh mục nguồn — chạy Đối chiếu video gốc trước.")
            if report.get("status") != "verified":
                missing = source_inventory.unobserved_shots(inventory)
                if missing:
                    raise HTTPException(status_code=409, detail=(
                        f"Shot {', '.join(map(str, missing))} chưa được Agent 1 quan sát (lô bị lỗi) — "
                        "bấm Đối chiếu lại video gốc rồi mới duyệt."))
                who = str(getattr(user, "email", None) or getattr(user, "username", None) or "local user")
                analysis["source_verification"] = source_inventory.accept_review(
                    report, inventory, by=who, at=datetime.now(timezone.utc).isoformat(), note=body.note)
                row.analysis = analysis
                if row.cast:
                    row.cast = production.attach_inventory(analysis, json.loads(json.dumps(row.cast)))
                row.updated_at = datetime.now(timezone.utc)
                s.add(row)
                s.commit()
                s.refresh(row)
            return _summary(row)


class AdaptBody(BaseModel):
    rules: dict[str, Any] = {}
    # Omit to keep the current glossary (or build one on the first run). Send
    # one to lock hand-edited names; shots are re-adapted if it changed.
    glossary: Optional[dict[str, dict[str, str]]] = None
    # Throw away every adapted shot and write them all again.
    fresh: bool = False


@router.post("/{video_id}/adapt")
async def adapt(video_id: uuid.UUID, body: AdaptBody, user=Depends(get_optional_user)) -> dict[str, Any]:
    with get_session() as s:
        row = _load(s, video_id, user)
        if not (row.analysis or {}).get("shots"):
            raise HTTPException(status_code=409, detail="Phân tích chưa xong — chưa adapt được.")
    _start(video_id, _run_adaptation(video_id, body.rules, body.glossary, fresh=body.fresh), operation={"task":"adapt","rules":body.rules,"glossary":body.glossary,"fresh":body.fresh})
    with get_session() as s:
        return _summary(s.get(VideoAnalysis, video_id))


class GlossaryBody(BaseModel):
    glossary: dict[str, dict[str, str]]


@router.patch("/{video_id}/glossary")
def save_glossary(video_id: uuid.UUID, body: GlossaryBody, user=Depends(get_optional_user)) -> dict[str, Any]:
    with get_session() as s:
        row = _load(s, video_id, user)
        if _is_running(row.id):
            raise HTTPException(status_code=409, detail="Đang xử lý — đợi xong rồi sửa tên.")
        row.adaptation = {**(row.adaptation or {}), "glossary": body.glossary}
        row.updated_at = datetime.now(timezone.utc)
        s.add(row)
        s.commit()
        s.refresh(row)
        return _summary(row)


class DesignEditBody(BaseModel):
    kind: str = Field(default="character", pattern="^(character|environment|background_group|prop)$")
    # Only the fields being changed; everything else on the brief is left alone.
    design: dict[str, Any] = Field(default_factory=dict)


@router.patch("/{video_id}/cast/{key}")
async def edit_design(
    video_id: uuid.UUID, key: str, body: DesignEditBody, user=Depends(get_optional_user)
) -> dict[str, Any]:
    """Hand-edit one entry's design brief.

    A brief is written by a model and is usually right, but "remove the mole"
    should be a one-line change to the brief rather than a re-roll of the whole
    design — a re-roll also changes the face, the hair and the costume, which is
    not what was asked. Merging here means every sheet generated afterwards
    carries the correction, instead of it having to be repeated per image.
    """
    bucket = production.BUCKETS.get(body.kind)
    if not bucket:
        raise HTTPException(status_code=422, detail="Unknown asset kind.")
    async with _cast_lock(video_id):
        with get_session() as s:
            row = _load(s, video_id, user)
            if _is_running(row.id):
                raise HTTPException(status_code=409, detail="Đang xử lý — đợi xong rồi sửa.")
            stored = json.loads(json.dumps(row.cast or {}))
            entry = next((e for e in stored.get(bucket) or [] if e.get("key") == key), None)
            if entry is None:
                raise HTTPException(status_code=404, detail=f"Không thấy {body.kind} {key!r}.")
            entry["design"] = {**(entry.get("design") or {}), **body.design}
            entry["design_edited"] = True
            row.cast = stored
            row.updated_at = datetime.now(timezone.utc)
            s.add(row)
            s.commit()
            s.refresh(row)
            return _summary(row)


@router.get("/{video_id}/frames/{name}")
def frame(video_id: uuid.UUID, name: str, user=Depends(get_optional_user)) -> FileResponse:
    if not _FRAME_NAME.match(name):
        raise HTTPException(status_code=404)
    with get_session() as s:
        _load(s, video_id, user)
    path = _work_dir(video_id) / "frames" / name
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@router.get("/{video_id}/source")
def source(video_id: uuid.UUID, user=Depends(get_optional_user)) -> FileResponse:
    with get_session() as s:
        _load(s, video_id, user)
    path = _source_path(video_id)
    if path is None:
        raise HTTPException(status_code=404)
    return FileResponse(path)


@router.get("/{video_id}/export")
def export_video(
    video_id: uuid.UUID, format: str = "md", mode: str = "detailed", user=Depends(get_optional_user)
) -> Response:
    with get_session() as s:
        row = _load(s, video_id, user)
        analysis, adaptation, name = row.analysis or {}, row.adaptation or None, row.name
    if not analysis.get("shots"):
        raise HTTPException(status_code=409, detail="Phân tích chưa xong.")
    if adaptation and not adaptation.get("shots"):
        adaptation = None
    stale = sorted(pipeline.stale_source_shots(adaptation))
    if stale:
        adaptation = {**adaptation, "shots": {
            n: value for n, value in adaptation["shots"].items() if int(n) not in stale}}
    stem = re.sub(r"[^\w-]+", "-", name).strip("-").lower() or "shotlist"
    ascii_stem = re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-") or "shotlist"
    def download_header(extension: str) -> str:
        return (f'attachment; filename="{ascii_stem}.{extension}"; '
                f"filename*=UTF-8''{quote(stem + '.' + extension)}")
    if format == "json":
        document = export.to_json(analysis, adaptation)
        if stale:
            document["adaptation"]["stale_source_shots"] = stale
            document["adaptation"]["notice"] = "Source changed; adapt these shots again. Their outdated adaptation is omitted."
        body = json.dumps(document, ensure_ascii=False, indent=1)
        return Response(body, media_type="application/json",
                        headers={"Content-Disposition": download_header("json")})
    text = export.to_markdown(analysis, adaptation, mode=mode, title=f"SHOTLIST — {name}")
    if stale:
        text = ("> Bản chuyển thể cũ đã được ẩn ở các shot " + ", ".join(map(str, stale)) +
                ". Bấm Adapt để cập nhật theo nguồn đã sửa.\n\n" + text)
    return PlainTextResponse(text, media_type="text/markdown",
                             headers={"Content-Disposition": download_header("md")})


class CastBody(BaseModel):
    # The bible is written in the target world, so it needs the rules before
    # the first adaptation has stored any — a cast read with the wrong naming
    # rule has to be thrown away and read again.
    rules: dict[str, Any] = {}


@router.post("/{video_id}/cast")
async def read_cast(
    video_id: uuid.UUID, body: CastBody = CastBody(), user=Depends(get_optional_user)
) -> dict[str, Any]:
    """Read who is in the film and where it plays, in the adapted names."""
    with get_session() as s:
        row = _load(s, video_id, user)
        if not (row.analysis or {}).get("shots"):
            raise HTTPException(status_code=409, detail="Phân tích chưa xong.")
        if body.rules:
            adaptation = dict(row.adaptation or {})
            adaptation["rules"] = AdaptationRules.from_dict(
                {**(adaptation.get("rules") or {}), **body.rules}
            ).as_dict()
            row.adaptation = adaptation
            s.add(row)
            s.commit()
    _start(video_id, _run_cast(video_id), operation={"task":"cast"})
    with get_session() as s:
        return _summary(s.get(VideoAnalysis, video_id))


class DesignBody(BaseModel):
    # Empty = everyone and everywhere.
    keys: list[str] = []
    # Set to redesign instead of describe: a one-paragraph art direction that
    # the cast and the places are rebuilt inside, ignoring how they look in the
    # reference. The story and the shot list are untouched.
    world: str = ""


@router.post("/{video_id}/design")
async def design(video_id: uuid.UUID, body: DesignBody = DesignBody(),
                 user=Depends(get_optional_user)) -> dict[str, Any]:
    """Write a design brief for each entry — from the real keyframes, or, with
    ``world`` set, redesigned inside that art direction instead."""
    with get_session() as s:
        row = _load(s, video_id, user)
        if not (row.cast or {}).get("characters") and not (row.cast or {}).get("environments"):
            raise HTTPException(status_code=409, detail="Lập hồ sơ nhân vật & bối cảnh trước đã.")
    _start(video_id, _run_design(video_id, body.keys, body.world), operation={"task":"design","keys":body.keys,"world":body.world})
    with get_session() as s:
        return _summary(s.get(VideoAnalysis, video_id))


class ConformBody(BaseModel):
    # Source shot numbers. Empty: every shot a redesigned character is in.
    shots: list[int] = Field(default_factory=list)
    # Put back every word a conform replaced.
    undo: bool = False


@router.post("/{video_id}/conform")
async def conform(video_id: uuid.UUID, body: ConformBody = ConformBody(),
                  user=Depends(get_optional_user)) -> dict[str, Any]:
    """Bring the shots in line with the redesigned cast.

    Run after the designs are settled and before the board is built: the
    shots still describe the reference actors' costumes until then.
    """
    with get_session() as s:
        row = _load(s, video_id, user)
        if not (row.adaptation or {}).get("shots"):
            raise HTTPException(status_code=409, detail="Adapt video trước đã.")
        if body.undo:
            if _is_running(row.id):
                raise HTTPException(status_code=409, detail="Video này đang được xử lý.")
            row.adaptation = conform_mod.undo(row.adaptation or {})
            row.updated_at = datetime.now(timezone.utc)
            s.add(row)
            s.commit()
            s.refresh(row)
            return _summary(row)
    _start(video_id, _run_conform(video_id, set(body.shots) or None), operation={"task":"conform","shots":body.shots})
    with get_session() as s:
        return _summary(s.get(VideoAnalysis, video_id))


def _frame_urls(video_id: uuid.UUID, entry: dict, limit: int = 4) -> list[str]:
    """Publish a few of the entry's own keyframes and return their URLs.

    The sheet is generated FROM the film's own frames, not only from words
    about them — which is the difference between a character who resembles the
    reference and one who is it. Published once and remembered, because the
    same frames are used again every time the sheet is regenerated.
    """
    cached = [u for u in (entry.get("frame_urls") or []) if isinstance(u, str)]
    if cached:
        return cached[:limit]
    work = _work_dir(video_id)
    urls: list[str] = []
    for rel in (entry.get("frames") or [])[:limit]:
        path = work / rel
        if not path.is_file():
            continue
        url = automation._publish(path.read_bytes())
        if url:
            urls.append(url)
    return urls


class PlateBody(BaseModel):
    kind: str = Field(default="character", pattern="^(character|environment|background_group|prop)$")
    key: str
    state_key: str = ""              # which look of a character; default the first
    image_model: str = automation.DEFAULT_IMAGE_MODEL
    image_size: str = automation.DEFAULT_IMAGE_SIZE
    aspect_ratio: str = ""           # default: a sheet is 16:9, a plate follows the video
    chain_identity: bool = True      # a second look inherits the face of the first
    # Generate from the film's own frames of this subject, not only from text.
    use_frames: bool = True


@router.post("/{video_id}/plate")
async def plate(video_id: uuid.UUID, body: PlateBody, user=Depends(get_optional_user)) -> dict[str, Any]:
    """Generate one character sheet or one environment plate, and keep it."""
    with get_session() as s:
        row = _load(s, video_id, user)
        cast, rules = dict(row.cast or {}), (row.adaptation or {}).get("rules") or {}
    bucket = production.BUCKETS[body.kind]
    entry = next((e for e in cast.get(bucket) or [] if e.get("key") == body.key), None)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Không thấy {body.kind} {body.key!r} trong hồ sơ.")
    if body.image_model not in automation.IMAGE_MODELS:
        raise HTTPException(status_code=422, detail=f"Unknown image model {body.image_model!r}.")

    style = automation.style_from_rules(rules.get("visual_style"))
    design_brief = entry.get("design") or None
    reference_urls: list[str] = []
    if body.use_frames:
        reference_urls = await asyncio.to_thread(_frame_urls, video_id, entry)
    source_frame_urls = list(reference_urls)

    if body.kind == "character":
        states = entry.get("states") or [{}]
        state = next((st for st in states if st.get("key") == body.state_key), states[0])
        # A second look must inherit the first sheet's face, or it is a different
        # person in different clothes.
        anchor = (entry.get("plate") or {}).get("reference_url")
        chain = bool(body.chain_identity and anchor and state.get("key") != (entry.get("plate") or {}).get("state_key"))
        if chain and anchor not in reference_urls:
            reference_urls = [anchor] + reference_urls
        prompt = automation.build_character_prompt(
            entry, state, has_reference=bool(reference_urls), style=style, design=design_brief
        )
        # A turnaround sheet is a studio document: it wants width for four views.
        aspect = body.aspect_ratio or "16:9"
    elif body.kind == "environment":
        state = {}
        # A set drawing is read across; the film's own ratio does not apply.
        aspect = body.aspect_ratio or automation.ENVIRONMENT_ASPECT
        prompt = automation.build_environment_prompt(
            entry, aspect_ratio=aspect, style=style, design=design_brief,
            has_reference=bool(reference_urls),
        )

    else:
        state = {}
        aspect = body.aspect_ratio or "16:9"
        # Photos, group members and container contents reuse the already drawn
        # identities/props. Fail before spending if a required dependency is absent.
        all_entries = [e for b in production.BUCKETS.values() for e in cast.get(b) or []]
        dependency_refs = []
        for aid in dict.fromkeys([*(entry.get("depends_on_asset_ids") or []), *(entry.get("member_ids") or [])]):
            dep = next((e for e in all_entries if e.get("source_asset_id") == aid or e.get("key") == aid), None)
            url = ((dep or {}).get("plate") or {}).get("reference_url")
            if not url:
                raise HTTPException(status_code=409, detail=f"Gen tạo hình {aid} trước để giữ đúng người/đồ vật trong {body.key}.")
            dependency_refs.append({"id": aid, "name": dep.get("name"), "url": url})
        dependency_urls = [d["url"] for d in dependency_refs]
        reference_urls = dependency_urls + [u for u in reference_urls if u not in dependency_urls]
        prompt = production.build_asset_prompt({**entry, "dependency_references": dependency_refs},
            kind=body.kind, style=style, aspect_ratio=aspect, has_reference=bool(reference_urls))

    from flowboard.services import film_styles
    if film_styles.is_preset(style):
        aspect = "16:9"

    # The descriptive sections are the prompt writer's (GPT); the layout, pose
    # and exclusions stay the house text that made the approved sheets.
    written_by = "verified-asset-template"
    if body.kind in {"character", "environment"}:
        prompt, written_by = await prompt_writer.write_image_prompt(
            prompt, kind=body.kind, subject=str(entry.get("name") or body.key),
            design=design_brief, style=style)

    try:
        images = await automation.generate_plate(
            prompt,
            image_model=body.image_model,
            aspect_ratio=aspect,
            reference_urls=reference_urls,
            image_size=body.image_size,
            style=style, material_kind=body.kind,
        )
    except automation.AutomationError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    first = images[0]
    plate_row = {
        "prompt": prompt,
        "url": first.get("reference_url") or first.get("url"),
        "reference_url": first.get("reference_url"),
        "media_id": first.get("media_id"),
        "persisted": first.get("persisted", False),
        "model": body.image_model,
        "size": automation.capped_size(body.image_model, body.image_size),
        "aspect_ratio": aspect,
        "state_key": state.get("key", ""),
        "from_frames": len(reference_urls),
        "designed": bool(design_brief),
        "prompt_writer": written_by,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    # An unpublished plate is a multi-megabyte data URL; it must never reach a
    # JSON column. Hand it back for this tab and store only what is a link.
    async with _cast_lock(video_id):
        with get_session() as s:
            row = _load(s, video_id, user)
            # A DEEP copy, then assign: a JSON column is only written back when
            # the attribute is set to a different object. Mutating the dict in
            # place — which a shallow copy still does — leaves SQLAlchemy seeing
            # no change, and fifteen generated plates are silently not saved.
            stored = json.loads(json.dumps(row.cast or {}))
            for e in stored.get(bucket) or []:
                if e.get("key") == body.key:
                    e["plate"] = plate_row if plate_row["reference_url"] else {**plate_row, "url": None}
                    if source_frame_urls:
                        # Remember the published frames: the next regeneration
                        # reuses them instead of uploading the same JPEGs again.
                        e["frame_urls"] = source_frame_urls
            row.cast = stored
            row.updated_at = datetime.now(timezone.utc)
            s.add(row)
            s.commit()
    return {**plate_row, "url": first.get("url"), "kind": body.kind, "key": body.key}


class BoardBody(BaseModel):
    preserve_source_shots: bool = True
    # How long one clip may run. Seedance 2.5 allows 30s, and a longer clip is
    # one fewer seam for the cast and the light to drift across.
    clip_seconds: float = board_mod.MAX_CLIP_S


@router.post("/{video_id}/board")
async def to_board(
    video_id: uuid.UUID, body: BoardBody = BoardBody(), user=Depends(get_optional_user)
) -> dict[str, Any]:
    """Cast, places and Seedance-sized clips for the board. One model call (cast)."""
    with get_session() as s:
        row = _load(s, video_id, user)
        analysis, adaptation, cast = row.analysis or {}, row.adaptation or {}, row.cast or {}
    if not adaptation.get("shots"):
        raise HTTPException(status_code=409, detail="Adapt video trước rồi mới đưa lên board.")
    stale = sorted(pipeline.stale_source_shots(adaptation))
    if stale:
        raise HTTPException(status_code=409, detail=(
            f"Nguồn đã sửa ở {len(stale)} shot. Bấm Adapt để cập nhật các shot này rồi đưa lên board."))
    try:
        # Reuse the bible the reviewer has already seen (and generated sheets
        # from) rather than reading a second, subtly different one.
        return await board_mod.build_board(
            analysis, adaptation, cast=cast or None, max_clip_s=body.clip_seconds,
            preserve_source_shots=body.preserve_source_shots, source_id=str(video_id)
        )
    except Exception as exc:  # noqa: BLE001 — a model reply the board cannot use
        logger.exception("video_analysis %s: board failed", video_id)
        raise HTTPException(status_code=502, detail=f"Không dựng được board: {exc}")


@router.delete("/{video_id}")
def delete_video(video_id: uuid.UUID, user=Depends(get_optional_user)) -> dict[str, bool]:
    with get_session() as s:
        row = _load(s, video_id, user)
        if _is_running(row.id):
            raise HTTPException(409,detail="Video có job đang chạy; chờ hoàn tất trước khi xóa.")
        s.delete(row)
        s.commit()
    shutil.rmtree(_work_dir(video_id), ignore_errors=True)
    return {"ok": True}
