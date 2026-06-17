from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from flowboard.db import get_session
from flowboard.db.models import Node, Request
from flowboard.routes.deps import get_optional_user
from flowboard.services import budget_service
from flowboard.worker.processor import get_worker

router = APIRouter(prefix="/api/requests", tags=["requests"])


class RequestCreate(BaseModel):
    node_id: Optional[int] = None
    type: str = Field(min_length=1, max_length=40)
    params: dict[str, Any] = Field(default_factory=dict)


@router.post("")
def create_request(body: RequestCreate, user=Depends(get_optional_user)):
    params = dict(body.params)
    # Budget gate (Phase 9.2): video gen is metered per user. Estimate + check
    # available budget BEFORE creating the request (hard-cap on insufficient).
    est = 0.0
    if body.type == "gen_video" and user is not None:
        est = budget_service.estimate_video_usd(
            params.get("duration_seconds"), params.get("resolution")
        )
        if budget_service.available_usd(user.id) + 1e-9 < est:
            raise HTTPException(status_code=402, detail="insufficient_budget")
    with get_session() as s:
        if body.node_id is not None and not s.get(Node, body.node_id):
            raise HTTPException(404, "node not found")
        req = Request(
            node_id=body.node_id,
            type=body.type,
            params=params,
            status="queued",
        )
        s.add(req)
        s.commit()
        s.refresh(req)
        rid = req.id
        row = req

    assert rid is not None
    # Reserve the budget hold now that the request has an id (re-checks
    # atomically — undo + reject if a concurrent gen ate the budget first).
    if body.type == "gen_video" and user is not None:
        if not budget_service.reserve(user.id, request_id=rid, estimated_usd=est, model=params.get("model_id")):
            with get_session() as s:
                r = s.get(Request, rid)
                if r is not None:
                    r.status = "failed"
                    r.error = "insufficient_budget"
                    s.add(r)
                    s.commit()
            raise HTTPException(status_code=402, detail="insufficient_budget")
    get_worker().enqueue(rid)
    return row


@router.get("/{request_id}")
def get_request(request_id: int):
    with get_session() as s:
        req = s.get(Request, request_id)
        if req is None:
            raise HTTPException(404, "request not found")
        return req


@router.post("/{request_id}/cancel")
def cancel_request(request_id: int):
    """Cancel a queued request before the worker picks it up.

    Only ``queued`` rows are cancelable. The worker pulls rids off an
    in-memory ``asyncio.Queue`` and we can't yank a value back out, so
    we mark the row as ``failed`` with ``error='canceled'`` and let
    ``_process_one`` skip rows whose DB status drifted away from
    ``queued``. Returns 409 for any other state — running jobs need
    different surgery (in-flight HTTP calls to Flow).
    """
    with get_session() as s:
        req = s.get(Request, request_id)
        if req is None:
            raise HTTPException(404, "request not found")
        if req.status != "queued":
            raise HTTPException(
                409, f"only queued requests can be canceled (status={req.status})"
            )
        req.status = "failed"
        req.error = "canceled"
        req.finished_at = datetime.now(timezone.utc)
        s.add(req)
        s.commit()
        s.refresh(req)
        # Give back any budget hold: a queued gen_video reserved its estimate
        # up-front, and the worker skips canceled rows (status != queued) so it
        # never settles/releases them. Without this the hold leaks forever.
        budget_service.release(request_id)
        return req
