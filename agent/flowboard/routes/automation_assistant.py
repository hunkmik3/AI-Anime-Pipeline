"""Authenticated chat for automation boards, separate from the old shot planner."""
import uuid
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import select
from flowboard.db import get_session
from flowboard.db.models import AutomationAssistantTurn, AutomationProject, AutomationJob
from flowboard.routes.deps import get_optional_user
from flowboard.routes.automation import _load
from flowboard.services import automation_assistant as assistant, automation_jobs

router = APIRouter(prefix='/api/automation/projects/{project_id}/assistant', tags=['automation-assistant'])

class Send(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    request_key: uuid.UUID
    expected_revision: int = Field(ge=0)
    selected_node_ids: list[str] = Field(default_factory=list, max_length=40)

@router.get('')
def history(project_id: uuid.UUID, before: uuid.UUID | None = None, user=Depends(get_optional_user)):
    with get_session() as s:
        _load(s, project_id, user)
        q = select(AutomationAssistantTurn).where(AutomationAssistantTurn.project_id == project_id)
        if before:
            anchor = s.get(AutomationAssistantTurn, before)
            if not anchor or anchor.project_id != project_id: raise HTTPException(404, 'Turn not found')
            q = q.where(AutomationAssistantTurn.created_at < anchor.created_at)
        turns = list(reversed(s.exec(q.order_by(AutomationAssistantTurn.created_at.desc()).limit(80)).all()))
        ids = {e.get('result', {}).get('job_id') for t in turns for e in t.events if e.get('result', {}).get('job_id')}
        linked = s.exec(select(AutomationJob).where(AutomationJob.project_id == project_id,
            AutomationJob.id.in_([uuid.UUID(i) for i in ids]))).all() if ids else []
        return {'turns': [assistant.public(t) for t in turns], 'jobs': [automation_jobs.public(j) for j in linked],
                'older_cursor': str(turns[0].id) if len(turns) == 80 else None}

@router.post('', status_code=202)
async def send(project_id: uuid.UUID, body: Send, user=Depends(get_optional_user)):
    message = body.message.strip()
    if not message: raise HTTPException(422, 'Nhập yêu cầu trước khi gửi.')
    with get_session() as s:
        _load(s, project_id, user)
        project = s.exec(select(AutomationProject).where(AutomationProject.id == project_id).with_for_update()).one()
        previous = s.exec(select(AutomationAssistantTurn).where(AutomationAssistantTurn.project_id == project_id,
            AutomationAssistantTurn.request_key == str(body.request_key))).first()
        if previous:
            if previous.message != message: raise HTTPException(409, 'Request key đã được dùng cho tin nhắn khác.')
            return assistant.public(previous)
        if project.revision != body.expected_revision: raise HTTPException(409, 'Board đã đổi. Tải lại trước khi giao việc.')
        if s.exec(select(AutomationAssistantTurn).where(AutomationAssistantTurn.project_id == project_id,
            AutomationAssistantTurn.status.in_(assistant.ACTIVE))).first():
            raise HTTPException(409, 'Agent đang xử lý yêu cầu trên board này. Chờ hoặc dừng lượt hiện tại.')
        valid = {n.get('id') for n in (project.board or {}).get('nodes', [])}
        if any(n not in valid for n in body.selected_node_ids): raise HTTPException(422, 'Node được chọn không thuộc board.')
        turn = AutomationAssistantTurn(project_id=project_id, message=message, request_key=str(body.request_key),
            context={'selected_node_ids': body.selected_node_ids})
        s.add(turn); s.commit(); s.refresh(turn)
        result = assistant.public(turn)
    assistant.launch(turn.id, user)
    return result

@router.post('/{turn_id}/stop')
def stop(project_id: uuid.UUID, turn_id: uuid.UUID, user=Depends(get_optional_user)):
    with get_session() as s:
        _load(s, project_id, user)
        turn = s.exec(select(AutomationAssistantTurn).where(AutomationAssistantTurn.id == turn_id).with_for_update()).first()
        if not turn or turn.project_id != project_id: raise HTTPException(404, 'Turn not found')
        if turn.status in assistant.ACTIVE:
            turn.status = 'stopped'; turn.updated_at = assistant.now()
            turn.reply = 'Đã dừng agent. Tác vụ đã gửi lên server vẫn tiếp tục; có thể yêu cầu tạm dừng lượt sản xuất.'
            s.add(turn); s.commit(); s.refresh(turn)
        return assistant.public(turn)
