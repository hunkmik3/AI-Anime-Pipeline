"""Reskin Studio (/api/studio): one source film's pipeline and the owner's controls.

Every write goes through the existing automation routes (board save with its revision
check, job creation, finding acceptance, provider reconciliation, run control), so the
studio keeps the same guards as the board it drives. A board change can be asked as a
dry run first (what it reopens and roughly what that costs), and every saved change is
kept in the film's history so it can be undone while nothing newer touched it.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlmodel import select

from flowboard.db import get_session
from flowboard.db.models import AutomationJob, AutomationProject, VideoAnalysis
from flowboard.routes import automation as auto_routes
from flowboard.routes.deps import get_optional_user
from flowboard.services import automation_jobs, production_run, studio

router = APIRouter(prefix="/api/studio", tags=["studio"])
HISTORY_KEEP = 40


def _owner(user: Any) -> Optional[uuid.UUID]:
    raw = getattr(user, "id", None)
    return raw if isinstance(raw, uuid.UUID) else None


def _film(session, video_id: uuid.UUID, user: Any) -> VideoAnalysis:
    row = session.get(VideoAnalysis, video_id)
    owner = _owner(user)
    if row is None or (owner and row.owner_user_id and row.owner_user_id != owner) or not (row.options or {}).get('auto_production'):
        raise HTTPException(404, detail="Phim không tồn tại.")
    return row


def _project(session, row: VideoAnalysis, user: Any) -> AutomationProject:
    if not row.automation_project_id:
        raise HTTPException(409, detail="Phim chưa có board.")
    return auto_routes._load(session, row.automation_project_id, user)


def _board(session, project: AutomationProject) -> dict:
    return automation_jobs.project_board(session, project)


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')


def _impact(board: dict, settings: dict, clips: list[str], review: bool = True) -> dict:
    seconds = {studio._key(n): studio.clip_seconds(n) for n in studio.sequences(board)}
    known = [k for k in dict.fromkeys(clips) if k in seconds]
    return {'clips': known, 'review': review, 'estimate_usd': round(sum(studio.estimate(settings, seconds[k]) for k in known), 2)}


def _commit(video_id: uuid.UUID, project_id: uuid.UUID, before: dict, after: dict, revision: int, user: Any, *,
            kind: str, summary: str, clips: list[str], settings: dict, dry_run: bool, review: bool = True, extra: dict | None = None):
    """Dry run: what the change reopens. Otherwise save it and keep it in the history."""
    impact = {**_impact(after, settings, clips, review), **(extra or {})}
    if dry_run:
        return {'dry_run': True, 'impact': impact}
    entry = studio.change_entry(before, after, kind, summary, impact['clips'], user)
    saved = auto_routes.save_project(project_id, auto_routes.ProjectSave(board=after, expected_revision=revision), user)['revision']
    if entry:
        from flowboard.services import source_film
        with get_session() as s:
            past = list(((s.get(VideoAnalysis, video_id).options or {}).get('auto_production') or {}).get('studio_history') or [])
        source_film.state(video_id, studio_history=[entry, *past][:HISTORY_KEEP])
    return {'revision': saved, 'impact': impact}


def _change(video_id, user, fn, *, kind: str, summary: str, revision: int, dry_run: bool):
    """Load the board, apply fn(board) -> (board, impact) and commit or dry-run it."""
    with get_session() as s:
        row = _film(s, video_id, user)
        project = _project(s, row, user)
        before = _board(s, project)
        settings = dict(row.options['auto_production'].get('settings') or {})
        pid = project.id
    try:
        after, impact = fn(before)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    clips = impact.get('clips') or []
    extra = {k: v for k, v in impact.items() if k not in ('clips', 'review')}
    return _commit(video_id, pid, before, after, revision, user, kind=kind, summary=summary, clips=clips, settings=settings,
                   dry_run=dry_run, review=impact.get('review', True), extra=extra)


# ─────────────────────────────── films ───────────────────────────────

@router.get("/films")
def list_films(user=Depends(get_optional_user)) -> list[dict[str, Any]]:
    owner = _owner(user)
    with get_session() as s:
        rows = s.exec(select(VideoAnalysis).order_by(VideoAnalysis.updated_at.desc())).all()
        return [studio.summary(s, r) for r in rows
                if (r.options or {}).get('auto_production') and (not owner or r.owner_user_id in (None, owner))]


@router.get("/films/{video_id}")
def get_film(video_id: uuid.UUID, user=Depends(get_optional_user)) -> dict[str, Any]:
    with get_session() as s:
        row = _film(s, video_id, user)
        out = studio.detail(s, row)
        if row.automation_project_id:
            from flowboard.services import primary_materials
            run = primary_materials.waiting_run(s, row.automation_project_id)
            out['waiting_materials_run'] = str(run.id) if run else None
        return out


@router.get("/films/{video_id}/clips/{key}")
def get_clip(video_id: uuid.UUID, key: str, user=Depends(get_optional_user)) -> dict[str, Any]:
    with get_session() as s:
        row = _film(s, video_id, user)
        _project(s, row, user)
        try:
            return studio.clip_detail(s, row, key)
        except ValueError as exc:
            raise HTTPException(404, detail=str(exc)) from exc


@router.post("/films/{video_id}/continue")
def continue_film(video_id: uuid.UUID, user=Depends(get_optional_user)) -> dict[str, Any]:
    """Approve the review gate (if the film waits there) and resume the pipeline."""
    from flowboard.routes import video_analysis
    from flowboard.services import source_film
    with get_session() as s:
        row = _film(s, video_id, user)
        stage = (row.options['auto_production'] or {}).get('stage')
    if stage == 'awaiting_review':
        source_film.state(video_id, board_approved=True, stage='board_approved')
    video_analysis._start(video_id, video_analysis._run_analysis(video_id, None), operation={"task": "film"})
    with get_session() as s:
        return studio.summary(s, s.get(VideoAnalysis, video_id))


class SettingsEdit(BaseModel):
    expected_revision: Optional[int] = Field(default=None, ge=0)
    resolution: Optional[str] = None
    timing_policy: Optional[str] = None
    motion_profile: Optional[str] = None
    pause_for_review: Optional[bool] = None
    prepare_before_render: Optional[bool] = None
    review_masters: Optional[bool] = None
    image_fallback_model: Optional[str] = None
    budget_usd: Optional[float] = Field(default=None, ge=0, le=100000)
    clear_budget: bool = False
    style_note: Optional[str] = Field(default=None, max_length=2000)
    style_image_url: Optional[str] = Field(default=None, max_length=2000)
    dry_run: bool = False


@router.patch("/films/{video_id}/settings")
def edit_settings(video_id: uuid.UUID, body: SettingsEdit, user=Depends(get_optional_user)) -> dict[str, Any]:
    """Production settings after the film exists (never ones that reshape its board)."""
    from flowboard.services import source_film
    patch = body.model_dump(exclude_none=True, exclude={'expected_revision', 'dry_run', 'clear_budget'})
    if body.clear_budget:
        patch['budget_usd'] = None
    with get_session() as s:
        row = _film(s, video_id, user)
        auto = dict(row.options['auto_production'] or {})
        project = s.get(AutomationProject, row.automation_project_id) if row.automation_project_id else None
        before = _board(s, project) if project and studio.sequences(project.board or {}) else (project.board if project else {}) or {}
    try:
        settings, after, notes = studio.apply_settings(dict(auto.get('settings') or {}), before, patch)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    board_keys = ('motionProfile', 'imageFallbackModel', 'budgetUsd', 'styleNote', 'styleImageUrl')
    board_changed = project is not None and any(before.get(k) != after.get(k) for k in board_keys)
    restyle = any(k in patch for k in ('style_note', 'style_image_url', 'motion_profile'))
    clips = [studio._key(n) for n in studio.sequences(after)] if restyle else []
    impact = {**_impact(after, settings, clips, False), 'notes': notes}
    if body.dry_run:
        return {'dry_run': True, 'impact': impact}
    revision = project.revision if project else None
    if board_changed:
        if body.expected_revision is None:
            raise HTTPException(428, detail='Cần revision của board để lưu thiết lập này.')
        revision = _commit(video_id, project.id, before, after, body.expected_revision, user, kind='settings',
                           summary='Thiết lập: ' + ', '.join(sorted(patch)), clips=clips, settings=settings, dry_run=False, review=False)['revision']
    changes = {'settings': settings}
    if auto.get('source_board_input_digest'):
        with get_session() as s:
            fresh = s.get(VideoAnalysis, video_id)
            fresh.options = {**fresh.options, 'auto_production': {**(fresh.options.get('auto_production') or {}), 'settings': settings}}
            changes['source_board_input_digest'] = source_film.board_source_digest(fresh)
    source_film.state(video_id, **changes)
    return {'revision': revision, 'settings': settings, 'impact': impact}


@router.delete("/films/{video_id}")
def delete_film(video_id: uuid.UUID, user=Depends(get_optional_user)) -> dict[str, Any]:
    """Delete the film: its board, jobs and source analysis (refused while anything runs)."""
    from flowboard.routes import video_analysis
    with get_session() as s:
        row = _film(s, video_id, user)
        pid = row.automation_project_id
    if pid:
        auto_routes.delete_project(pid, user)
    video_analysis.delete_video(video_id, user)
    return {'ok': True}


# ─────────────────────────────── shots ───────────────────────────────

class ShotEdit(BaseModel):
    expected_revision: int = Field(ge=0)
    lines: Optional[list[dict[str, Any]]] = None
    action: Optional[list[str]] = None
    staging: Optional[str] = None
    presence: Optional[list[dict[str, Any]]] = None
    character_states: Optional[dict[str, str]] = None
    dry_run: bool = False


@router.patch("/films/{video_id}/shots/{source_shot}")
def edit_shot(video_id: uuid.UUID, source_shot: int, body: ShotEdit, user=Depends(get_optional_user)) -> dict[str, Any]:
    patch = body.model_dump(exclude_none=True, exclude={'expected_revision', 'dry_run'})
    if not patch:
        raise HTTPException(422, detail="Không có gì để sửa.")
    return _change(video_id, user, lambda b: studio.edit_shot(b, source_shot, patch, user), kind='shot',
                   summary=f"Shot {source_shot}: " + ', '.join(sorted(patch)), revision=body.expected_revision, dry_run=body.dry_run)


class CutMove(BaseModel):
    expected_revision: int = Field(ge=0)
    at: float = Field(ge=0)
    dry_run: bool = False


@router.post("/films/{video_id}/shots/{source_shot}/cut")
def move_cut(video_id: uuid.UUID, source_shot: int, body: CutMove, user=Depends(get_optional_user)) -> dict[str, Any]:
    return _change(video_id, user, lambda b: studio.move_cut(b, source_shot, body.at), kind='cut',
                   summary=f'Điểm cắt sau shot {source_shot} → {body.at:.2f} s', revision=body.expected_revision, dry_run=body.dry_run)


# ─────────────────────────────── materials ───────────────────────────────

class MaterialEdit(BaseModel):
    expected_revision: int = Field(ge=0)
    target_description: Optional[str] = Field(default=None, max_length=2000)
    image_model: Optional[str] = None
    dry_run: bool = False


@router.patch("/films/{video_id}/materials/{node_id}")
def edit_material(video_id: uuid.UUID, node_id: str, body: MaterialEdit, user=Depends(get_optional_user)) -> dict[str, Any]:
    return _change(video_id, user, lambda b: studio.set_material(b, node_id, target_description=body.target_description,
                                                                  image_model=body.image_model),
                   kind='material', summary=f'Material {node_id}', revision=body.expected_revision, dry_run=body.dry_run)


class Revision(BaseModel):
    expected_revision: int = Field(ge=0)


@router.post("/films/{video_id}/materials/{node_id}/{slot}/regenerate", status_code=202)
def regenerate_material(video_id: uuid.UUID, node_id: str, slot: str, body: Revision, user=Depends(get_optional_user)):
    """Draw one sheet again (any slot: main design, an outfit, a set's master or reverse view)."""
    with get_session() as s:
        project = _project(s, _film(s, video_id, user), user)
        if s.exec(select(AutomationJob).where(AutomationJob.project_id == project.id, AutomationJob.kind == 'production_run',
                                              AutomationJob.status == 'running')).first():
            raise HTTPException(409, detail='Đang có lượt sản xuất; tạm dừng nó trước khi vẽ lại sheet.')
        board = _board(s, project)
        try:
            specs = production_run.material_specs(board, production_run.selected_packages(board, str(project.id), []))
        except ValueError as exc:
            raise HTTPException(409, detail=str(exc)) from exc
        spec = specs.get(node_id + ':' + slot)
        if not spec:
            raise HTTPException(404, detail='Sheet này không được clip nào dùng.')
        deps = []
        for dep in spec['deps']:
            other = specs[dep]
            if not other['plate'].get('referenceUrl'):
                raise HTTPException(409, detail='Tạo sheet phụ thuộc trước: ' + other['item'].get('name', other['asset_id']))
            deps.append({'asset_id': other['asset_id'], 'name': other['item'].get('name', other['asset_id']),
                         'reference_url': other['plate']['referenceUrl'], 'media_id': other['plate'].get('mediaId', '')})
        payload = production_run.material_payload(spec, deps, board)
        pid = project.id
    return auto_routes.create_job(pid, auto_routes.JobCreate(kind='plate', node_id=node_id, slot=slot, payload=payload,
                                  expected_revision=body.expected_revision, request_key=f'studio-sheet:{node_id}:{slot}:{_stamp()}'), user)


class ImageChoice(BaseModel):
    expected_revision: int = Field(ge=0)
    url: str = Field(min_length=8, max_length=2000)
    media_id: str = Field(default='', max_length=200)
    dry_run: bool = False


@router.post("/films/{video_id}/materials/{node_id}/{slot}/image")
def choose_material_image(video_id: uuid.UUID, node_id: str, slot: str, body: ImageChoice, user=Depends(get_optional_user)):
    """Bind an image the owner chose — an upload, an earlier drawing or another sheet."""
    with get_session() as s:
        project = _project(s, _film(s, video_id, user), user)
        jobs = s.exec(select(AutomationJob).where(AutomationJob.project_id == project.id)).all()

    def fn(board):
        out = studio.use_image(board, node_id, slot, body.url, body.media_id, jobs)
        return out, {'clips': studio.material_clips(out, node_id)}
    return _change(video_id, user, fn, kind='image', summary=f'Ảnh sheet {node_id} · {slot}', revision=body.expected_revision,
                   dry_run=body.dry_run)


# ─────────────────────────────── clips ───────────────────────────────

class ClipEdit(BaseModel):
    expected_revision: int = Field(ge=0)
    staging_image_model: Optional[str] = None
    motion_profile: Optional[str] = None
    # The owner's own prompt (checked unchanged by an independent review before use) ...
    manual_prompt: Optional[str] = Field(default=None, max_length=60000)
    end_state: Optional[str] = Field(default=None, max_length=4000)
    # ... or back to the writer's newest prompt.
    auto_prompt: bool = False
    audio_choice: Optional[str] = None
    selected_take: Optional[str] = None
    dry_run: bool = False


@router.patch("/films/{video_id}/clips/{key}")
def edit_clip(video_id: uuid.UUID, key: str, body: ClipEdit, user=Depends(get_optional_user)) -> dict[str, Any]:
    with get_session() as s:
        project = _project(s, _film(s, video_id, user), user)
        auto = (studio.latest_written(s, project.id, key, manual=False) or {}) if body.auto_prompt else None
        known = {t['job_id'] for t in studio.takes(key, s.exec(select(AutomationJob).where(AutomationJob.project_id == project.id)).all())}

    def fn(board):
        out = studio.set_clip(board, key, staging_image_model=body.staging_image_model, motion_profile=body.motion_profile,
                              manual_prompt=body.manual_prompt, end_state=body.end_state, auto_prompt=auto)
        out = studio.set_audio_and_take(out, key, audio_choice=body.audio_choice, take_id=body.selected_take, known_takes=known)
        redo = body.manual_prompt is not None or body.auto_prompt or body.motion_profile is not None or body.staging_image_model is not None
        return out, {'clips': [key] if redo else [], 'review': False}
    fields = [k for k, v in body.model_dump(exclude={'expected_revision', 'dry_run'}).items() if v not in (None, False)]
    out = _change(video_id, user, fn, kind='clip', summary=f"{key}: " + ', '.join(fields), revision=body.expected_revision, dry_run=body.dry_run)
    if body.audio_choice == 'cleaned' and not body.dry_run:
        # The owner asked for this clip without music: clean its chosen take now.
        with get_session() as s:
            project = _project(s, _film(s, video_id, user), user)
            job = studio.cleanup_job(s, _board(s, project), project.id, key,
                                     s.exec(select(AutomationJob).where(AutomationJob.project_id == project.id)).all())
            s.commit()
            out['cleanup_job'] = str(job.id) if job else None
    return out


class Split(BaseModel):
    expected_revision: int = Field(ge=0)
    at_shot: int = Field(ge=2)
    dry_run: bool = False


@router.post("/films/{video_id}/clips/{key}/split")
def split_clip(video_id: uuid.UUID, key: str, body: Split, user=Depends(get_optional_user)) -> dict[str, Any]:
    return _change(video_id, user, lambda b: studio.split_clip(b, key, body.at_shot), kind='split',
                   summary=f'Tách {key} tại shot {body.at_shot}', revision=body.expected_revision, dry_run=body.dry_run)


class Merge(BaseModel):
    expected_revision: int = Field(ge=0)
    dry_run: bool = False


@router.post("/films/{video_id}/clips/{key}/merge-next")
def merge_clip(video_id: uuid.UUID, key: str, body: Merge, user=Depends(get_optional_user)) -> dict[str, Any]:
    return _change(video_id, user, lambda b: studio.merge_clips(b, key), kind='merge',
                   summary=f'Gộp {key} với clip sau', revision=body.expected_revision, dry_run=body.dry_run)


@router.get("/films/{video_id}/clips/{key}/prompt")
def clip_prompt(video_id: uuid.UUID, key: str, user=Depends(get_optional_user)):
    with get_session() as s:
        project = _project(s, _film(s, video_id, user), user)
        found = studio.prompt(s, project.id, key, _board(s, project))
    if not found:
        raise HTTPException(404, detail='Clip này chưa có prompt.')
    return found


class Approval(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    approved: bool = True
    note: str = Field(default='', max_length=500)


@router.post("/films/{video_id}/clips/{key}/approve")
def approve_clip(video_id: uuid.UUID, key: str, body: Approval, user=Depends(get_optional_user)):
    """The owner's verdict on one render of a clip; a new render asks again."""
    from flowboard.services import source_film
    with get_session() as s:
        row = _film(s, video_id, user)
        marks = dict((row.options['auto_production'] or {}).get('clip_approvals') or {})
    if body.approved:
        marks[key] = {'url': body.url, 'by': studio._owner_label(user), 'at': datetime.now(timezone.utc).isoformat(),
                      **({'note': body.note} if body.note else {})}
    else:
        marks.pop(key, None)
    source_film.state(video_id, clip_approvals=marks)
    return {'clip': key, 'approval': marks.get(key)}


@router.post("/films/{video_id}/clips/{key}/quality", status_code=202)
def recheck_quality(video_id: uuid.UUID, key: str, user=Depends(get_optional_user)):
    """Run the automatic check again on the clip's chosen take."""
    with get_session() as s:
        row = _film(s, video_id, user)
        project = _project(s, row, user)
        board = _board(s, project)
        jobs = s.exec(select(AutomationJob).where(AutomationJob.project_id == project.id)).all()
        video = next((n.get('data') or {} for n in board.get('nodes', []) if n.get('id') == 'vid:' + key), {})
        made = studio.takes(key, jobs)
        take = next((t for t in made if t['job_id'] == video.get('selectedTake')), None) or (made[0] if made else None)
        if not take:
            raise HTTPException(409, detail='Clip này chưa có bản gen để kiểm tra.')
        if any(j.kind == 'clip_quality' and j.node_id == 'vid:' + key and j.status in automation_jobs.ACTIVE for j in jobs):
            raise HTTPException(409, detail='Đang kiểm tra clip này.')
        try:
            payload = studio.quality_payload(board, str(project.id), key, take,
                                             (row.options['auto_production'].get('settings') or {}).get('timing_policy', 'full_take'))
        except ValueError as exc:
            raise HTTPException(409, detail=str(exc)) from exc
        job = AutomationJob(project_id=project.id, kind='clip_quality', node_id='vid:' + key, slot='quality',
                            request_key=f'studio-quality:{key}:{_stamp()}', payload=payload)
        s.add(job)
        s.commit()
        s.refresh(job)
        return automation_jobs.public(job)


# ─────────────────────────────── runs ───────────────────────────────

class RenderRequest(BaseModel):
    expected_revision: int = Field(ge=0)
    keys: list[str] = Field(min_length=1, max_length=2000)
    mode: str = Field(default='render', pattern='^(prepare|render)$')
    retry_failed: bool = True


def _start_run(video_id, user, keys, mode, revision, retry_failed=True, retake=False):
    with get_session() as s:
        row = _film(s, video_id, user)
        project = _project(s, row, user)
        settings = dict(row.options['auto_production'].get('settings') or {})
        pid = project.id
        superseded = studio.supersede_failed(s, pid, keys) if retry_failed else []
        set_aside = studio.set_aside_takes(s, pid, keys) if retake else []
        s.commit()
    config = studio.run_config(settings, keys, mode)
    try:
        run = production_run.start(pid, revision, config, f'studio:{mode}:{_stamp()}')
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    return {'run': run, 'superseded': superseded, 'set_aside': set_aside}


@router.post("/films/{video_id}/render", status_code=202)
def render(video_id: uuid.UUID, body: RenderRequest, user=Depends(get_optional_user)):
    return _start_run(video_id, user, body.keys, body.mode, body.expected_revision, body.retry_failed)


class Retake(BaseModel):
    expected_revision: int = Field(ge=0)
    keys: list[str] = Field(min_length=1, max_length=500)


@router.post("/films/{video_id}/retake", status_code=202)
def retake(video_id: uuid.UUID, body: Retake, user=Depends(get_optional_user)):
    """Render these clips again as new takes; earlier takes stay to choose from."""
    with get_session() as s:
        project = _project(s, _film(s, video_id, user), user)
        board = _board(s, project)
    cleared = board
    for key in body.keys:
        cleared = studio.set_audio_and_take(cleared, key, take_id='')
    revision = body.expected_revision
    if cleared != board:
        revision = auto_routes.save_project(project.id, auto_routes.ProjectSave(board=cleared, expected_revision=revision), user)['revision']
    return _start_run(video_id, user, body.keys, 'render', revision, True, retake=True)


@router.post("/films/{video_id}/runs/{run_id}/{action}")
def control_run(video_id: uuid.UUID, run_id: uuid.UUID, action: str, user=Depends(get_optional_user)):
    """Pause or resume one production run."""
    with get_session() as s:
        pid = _project(s, _film(s, video_id, user), user).id
    return auto_routes.control_production_run(pid, run_id, action, user)


@router.post("/films/{video_id}/review", status_code=202)
def review(video_id: uuid.UUID, body: Revision, user=Depends(get_optional_user)):
    with get_session() as s:
        pid = _project(s, _film(s, video_id, user), user).id
    return auto_routes.review_reskin_source(pid, auto_routes.ReskinReviewBody(expected_revision=body.expected_revision), user)


class Accept(BaseModel):
    expected_revision: int = Field(ge=0)
    keys: list[str] = Field(min_length=1, max_length=500)
    note: str = Field(default='', max_length=500)


@router.post("/films/{video_id}/accept")
def accept(video_id: uuid.UUID, body: Accept, user=Depends(get_optional_user)):
    with get_session() as s:
        pid = _project(s, _film(s, video_id, user), user).id
    return auto_routes.accept_reskin_findings(pid, auto_routes.ReskinFindingAccept(
        expected_revision=body.expected_revision, keys=body.keys, note=body.note), user)


class Absent(BaseModel):
    note: str = Field(min_length=8, max_length=1000)


@router.post("/films/{video_id}/jobs/{job_id}/absent")
def mark_absent(video_id: uuid.UUID, job_id: uuid.UUID, body: Absent, user=Depends(get_optional_user)):
    """The provider refused the request (nothing was created): close the unknown job."""
    with get_session() as s:
        pid = _project(s, _film(s, video_id, user), user).id
    return auto_routes.resolve_absent_job(pid, job_id, auto_routes.ResolveAbsentJob(provider_checked_no_submission=True, note=body.note), user)


# ─────────────────────────────── history ───────────────────────────────

@router.post("/films/{video_id}/history/{entry_id}/undo")
def undo(video_id: uuid.UUID, entry_id: str, body: Revision, user=Depends(get_optional_user)):
    with get_session() as s:
        row = _film(s, video_id, user)
        entry = next((h for h in (row.options['auto_production'] or {}).get('studio_history') or [] if h.get('id') == entry_id), None)
    if not entry:
        raise HTTPException(404, detail='Không còn mục lịch sử này.')
    return _change(video_id, user, lambda b: (studio.undo(b, entry), {'clips': entry.get('clips') or []}), kind='undo',
                   summary='Hoàn tác: ' + entry.get('summary', ''), revision=body.expected_revision, dry_run=False)


# ─────────────────────────────── assembly and export ───────────────────────────────

@router.post("/films/{video_id}/assemble")
async def assemble(video_id: uuid.UUID, user=Depends(get_optional_user)):
    from flowboard.services import source_film
    with get_session() as s:
        row = _film(s, video_id, user)
        _project(s, row, user)
        films = list((row.options['auto_production'] or {}).get('studio_films') or [])
    try:
        record = await studio.assemble(get_session, row)
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    source_film.state(video_id, studio_films=[record, *films][:20])
    return record


@router.get("/films/{video_id}/film/{filename}")
def film_file(video_id: uuid.UUID, filename: str, user=Depends(get_optional_user)):
    from flowboard.config import STORAGE_DIR
    with get_session() as s:
        row = _film(s, video_id, user)
        known = {f.get('filename') for f in (row.options['auto_production'] or {}).get('studio_films') or []}
    if filename not in known or Path(filename).name != filename:
        raise HTTPException(404, detail='Không có bản ghép này.')
    path = Path(STORAGE_DIR) / 'production-renders' / filename
    if not path.is_file():
        raise HTTPException(404, detail='File đã bị xoá.')
    return FileResponse(path, media_type='video/mp4', filename=f'{row.name}-{filename[:8]}.mp4')


@router.post("/films/{video_id}/export")
async def export(video_id: uuid.UUID, user=Depends(get_optional_user)):
    """A zip of every made clip, an EDL and a CSV for the editor."""
    from flowboard.services import source_film
    with get_session() as s:
        row = _film(s, video_id, user)
        _project(s, row, user)
        exports = list((row.options['auto_production'] or {}).get('studio_exports') or [])
    try:
        record = await studio.export(get_session, row)
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    source_film.state(video_id, studio_exports=[record, *exports][:10])
    return record


@router.get("/films/{video_id}/export/{filename}")
def export_file(video_id: uuid.UUID, filename: str, user=Depends(get_optional_user)):
    from flowboard.config import STORAGE_DIR
    with get_session() as s:
        row = _film(s, video_id, user)
        known = {f.get('filename') for f in (row.options['auto_production'] or {}).get('studio_exports') or []}
    if filename not in known or Path(filename).name != filename:
        raise HTTPException(404, detail='Không có bản xuất này.')
    path = Path(STORAGE_DIR) / 'production-renders' / 'studio-exports' / filename
    if not path.is_file():
        raise HTTPException(404, detail='File đã bị xoá.')
    return FileResponse(path, media_type='application/zip', filename=f'{row.name}-clips.zip')
