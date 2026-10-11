"""Reskin Studio (/api/studio): one source film's pipeline and the owner's controls.

Every write goes through the existing automation routes (board save with its revision
check, job creation, finding acceptance, provider reconciliation), so the studio keeps
the same guards as the board it drives.
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


def _save(project_id: uuid.UUID, board: dict, revision: int, user: Any) -> int:
    return auto_routes.save_project(project_id, auto_routes.ProjectSave(board=board, expected_revision=revision), user)['revision']


def _board(session, project: AutomationProject) -> dict:
    return automation_jobs.project_board(session, project)


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


class ShotEdit(BaseModel):
    expected_revision: int = Field(ge=0)
    lines: Optional[list[dict[str, Any]]] = None
    action: Optional[list[str]] = None
    staging: Optional[str] = None


@router.patch("/films/{video_id}/shots/{source_shot}")
def edit_shot(video_id: uuid.UUID, source_shot: int, body: ShotEdit, user=Depends(get_optional_user)) -> dict[str, Any]:
    patch = body.model_dump(exclude_none=True, exclude={'expected_revision'})
    if not patch:
        raise HTTPException(422, detail="Không có gì để sửa.")
    with get_session() as s:
        project = _project(s, _film(s, video_id, user), user)
        try:
            board, impact = studio.edit_shot(_board(s, project), source_shot, patch, user)
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from exc
        pid = project.id
    return {'revision': _save(pid, board, body.expected_revision, user), 'impact': impact}


class MaterialEdit(BaseModel):
    expected_revision: int = Field(ge=0)
    target_description: Optional[str] = Field(default=None, max_length=2000)
    image_model: Optional[str] = None


@router.patch("/films/{video_id}/materials/{node_id}")
def edit_material(video_id: uuid.UUID, node_id: str, body: MaterialEdit, user=Depends(get_optional_user)) -> dict[str, Any]:
    with get_session() as s:
        project = _project(s, _film(s, video_id, user), user)
        try:
            board, impact = studio.set_material(_board(s, project), node_id, target_description=body.target_description,
                                                image_model=body.image_model)
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from exc
        pid = project.id
    return {'revision': _save(pid, board, body.expected_revision, user), 'impact': impact}


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
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')
    return auto_routes.create_job(pid, auto_routes.JobCreate(kind='plate', node_id=node_id, slot=slot, payload=payload,
                                  expected_revision=body.expected_revision, request_key=f'studio-sheet:{node_id}:{slot}:{stamp}'), user)


class ImageChoice(BaseModel):
    expected_revision: int = Field(ge=0)
    url: str = Field(min_length=8, max_length=2000)
    media_id: str = Field(default='', max_length=200)


@router.post("/films/{video_id}/materials/{node_id}/{slot}/image")
def choose_material_image(video_id: uuid.UUID, node_id: str, slot: str, body: ImageChoice, user=Depends(get_optional_user)):
    with get_session() as s:
        project = _project(s, _film(s, video_id, user), user)
        jobs = s.exec(select(AutomationJob).where(AutomationJob.project_id == project.id)).all()
        try:
            board = studio.use_image(_board(s, project), node_id, slot, body.url, body.media_id, jobs)
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from exc
        pid = project.id
        clips = studio.material_clips(board, node_id)
    return {'revision': _save(pid, board, body.expected_revision, user), 'impact': {'clips': clips}}


class ClipEdit(BaseModel):
    expected_revision: int = Field(ge=0)
    staging_image_model: Optional[str] = None


@router.patch("/films/{video_id}/clips/{key}")
def edit_clip(video_id: uuid.UUID, key: str, body: ClipEdit, user=Depends(get_optional_user)) -> dict[str, Any]:
    with get_session() as s:
        project = _project(s, _film(s, video_id, user), user)
        try:
            board = studio.set_clip(_board(s, project), key, staging_image_model=body.staging_image_model)
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from exc
        pid = project.id
    return {'revision': _save(pid, board, body.expected_revision, user)}


class RenderRequest(BaseModel):
    expected_revision: int = Field(ge=0)
    keys: list[str] = Field(min_length=1, max_length=2000)
    mode: str = Field(default='render', pattern='^(prepare|render)$')
    retry_failed: bool = True


@router.post("/films/{video_id}/render", status_code=202)
def render(video_id: uuid.UUID, body: RenderRequest, user=Depends(get_optional_user)):
    with get_session() as s:
        row = _film(s, video_id, user)
        project = _project(s, row, user)
        settings = dict(row.options['auto_production'].get('settings') or {})
        pid = project.id
        superseded = studio.supersede_failed(s, pid, body.keys) if body.retry_failed else []
        s.commit()
    config = studio.run_config(settings, body.keys, body.mode)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')
    try:
        run = production_run.start(pid, body.expected_revision, config, f'studio:{body.mode}:{stamp}')
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    return {'run': run, 'superseded': superseded}


@router.get("/films/{video_id}/clips/{key}/prompt")
def clip_prompt(video_id: uuid.UUID, key: str, user=Depends(get_optional_user)):
    with get_session() as s:
        project = _project(s, _film(s, video_id, user), user)
        found = studio.prompt(s, project.id, key)
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
