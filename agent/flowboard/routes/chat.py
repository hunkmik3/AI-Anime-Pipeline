import uuid
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, StringConstraints
from sqlmodel import select
from typing_extensions import Annotated

from flowboard.db import get_session
from flowboard.db.models import ChatMessage, Plan, Scene, Shot
from flowboard.services.planner import generate_plan_reply

router = APIRouter(tags=["chat"])

# short_id alphabet is base36 4-char today; cap at 8 for a bit of headroom
# without letting callers smuggle arbitrary blobs inside a mentions array.
MentionStr = Annotated[str, StringConstraints(min_length=1, max_length=8)]


class ChatSendRequest(BaseModel):
    # `shot_id` is the legacy "board_id" — Phase 1 maps each Board to a
    # single Shot under one Project+Scene. Chat messages are stored on
    # the parent Project so they survive shot deletion (Phase 7 may
    # surface chat at the Project Bible level).
    shot_id: uuid.UUID
    message: str = Field(min_length=1, max_length=4000)
    mentions: List[MentionStr] = Field(default_factory=list, max_length=32)


def _resolve_project_id(session, shot_id: uuid.UUID) -> Optional[uuid.UUID]:
    shot = session.get(Shot, shot_id)
    if shot is None:
        return None
    scene = session.get(Scene, shot.scene_id)
    if scene is None:
        return None
    return scene.project_id


@router.post("/api/chat")
async def send_chat(body: ChatSendRequest):
    with get_session() as s:
        project_id = _resolve_project_id(s, body.shot_id)
        if project_id is None:
            raise HTTPException(404, "shot not found")

        user_msg = ChatMessage(
            project_id=project_id,
            role="user",
            content=body.message,
            mentions=list(body.mentions),
        )
        s.add(user_msg)

        # Planner can read the session (for mentions lookup). Mentions
        # resolve against Nodes scoped to the shot (not the whole project)
        # so cross-shot ref handles stay distinct.
        planner_out = await generate_plan_reply(
            s, body.shot_id, body.message, list(body.mentions)
        )

        assistant_msg = ChatMessage(
            project_id=project_id,
            role="assistant",
            content=planner_out["reply_text"],
            mentions=[],
        )
        s.add(assistant_msg)

        plan_row: Optional[Plan] = None
        if planner_out.get("plan") is not None:
            plan_row = Plan(
                shot_id=body.shot_id,
                spec=planner_out["plan"],
                status="draft",
            )
            s.add(plan_row)

        s.commit()
        s.refresh(user_msg)
        s.refresh(assistant_msg)
        if plan_row is not None:
            s.refresh(plan_row)

        resp: dict = {"user": user_msg, "assistant": assistant_msg}
        if plan_row is not None:
            resp["plan"] = plan_row
        return resp


@router.get("/api/boards/{board_id}/chat")
def list_chat(
    board_id: str,
    limit: Optional[int] = Query(default=500, ge=1, le=2000),
):
    """List chat messages for the Project that owns this Shot.

    The URL still says "board" for backwards compat with the frontend;
    `board_id` is a Shot UUID under the Phase 1 shim.
    """
    try:
        shot_uuid = uuid.UUID(board_id)
    except ValueError:
        raise HTTPException(404, "board not found")

    with get_session() as s:
        project_id = _resolve_project_id(s, shot_uuid)
        if project_id is None:
            raise HTTPException(404, "board not found")
        q = (
            select(ChatMessage)
            .where(ChatMessage.project_id == project_id)
            .order_by(ChatMessage.created_at, ChatMessage.id)
        )
        if limit:
            q = q.limit(limit)
        return list(s.exec(q).all())
