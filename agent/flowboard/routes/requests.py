from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from flowboard.db import get_session
from flowboard.services import flow_quota
from flowboard.services import sequence_quota
from flowboard.db.models import Node, Request
from flowboard.routes.deps import get_optional_user
from flowboard.services import budget_service, resource_guard, scope_budget
from flowboard.worker.processor import get_worker

router = APIRouter(prefix="/api/requests", tags=["requests"])


class RequestCreate(BaseModel):
    node_id: Optional[int] = None
    type: str = Field(min_length=1, max_length=40)
    params: dict[str, Any] = Field(default_factory=dict)


@router.post("")
def create_request(body: RequestCreate, user=Depends(get_optional_user)):
    params = dict(body.params)
    # Authorize the TARGET before anything else. Without this, queuing work
    # against another team's node both overwrote their canvas and drew the cost
    # from their credit ceiling — which made every budget below pointless, since
    # the spend landed on a project the caller was never allowed to touch.
    if body.node_id is not None:
        with get_session() as s:
            resource_guard.authorize_node(s, user, body.node_id, "canvas.write")

            # BEFORE the money checks, deliberately. A locked sequence and an
            # empty budget both refuse, but they are different problems with
            # different fixes, and "you are out of credit" sends the artist to
            # the wrong person. The lock is also the cheaper question: one
            # query, and nothing reserved.
            try:
                sequence_quota.check(s, body.node_id, body.type)
            except sequence_quota.SequenceLocked as exc:
                raise HTTPException(
                    423,
                    detail={
                        "error": str(exc),
                        "sequence": exc.code,
                        "used": exc.used,
                        "limit": exc.limit,
                        "unlock": "a PM can lift this in Admin › Sequences to review",
                    },
                )
    # Budget gate (Phase 9.2): video gen is metered per user. Estimate + check
    # available budget BEFORE creating the request (hard-cap on insufficient).
    est = 0.0
    if body.type == "gen_video" and user is not None:
        est = budget_service.estimate_video_usd(
            params.get("duration_seconds"), params.get("resolution")
        )
        if budget_service.available_usd(user.id) + 1e-9 < est:
            raise HTTPException(status_code=402, detail="insufficient_budget")
        # Global Avis pool guard: refuse up-front when the shared key can't
        # cover it, instead of letting the user wait on a gen Avis will reject.
        pool_avail = budget_service.pool_available_usd()
        if pool_avail is not None and pool_avail + 1e-9 < est:
            raise HTTPException(status_code=402, detail="avis_pool_exhausted")
    # Phase 11.1 — production ceilings the BOD set on the Series and the
    # Project. Blocking here (not at the worker) means the user is told before
    # anything is queued, and the alert names who can top the budget up.
    if body.type == "gen_video":
        with get_session() as s:
            blocked = scope_budget.check(s, body.node_id, est)
            if blocked is not None:
                scope_budget.notify_blocked(s, body.node_id, blocked, user)
                raise HTTPException(
                    status_code=402,
                    detail={
                        "code": "scope_budget_exhausted",
                        "message": (
                            f"the {scope_budget.scope_noun(blocked['scope'])} "
                            "credit budget is used up "
                            f"(${blocked['used_usd']:.2f} of ${blocked['effective_usd']:.2f}) — "
                            "ask your PM to grant more credit"
                        ),
                        "budget": blocked,
                    },
                )
    with get_session() as s:
        if body.type == "flow_gen_image":
            # The old studio's own generate path. Same cap, same place it is
            # defined — two entry points that disagreed about the ceiling would
            # be worse than no ceiling.
            try:
                flow_quota.check(s, params)
            except flow_quota.QuotaExceeded as exc:
                raise HTTPException(
                    429,
                    detail={
                        "error": str(exc),
                        "used": exc.used,
                        "quota": exc.quota,
                        "seconds_until_reset": exc.seconds_until_reset,
                    },
                )
        # Existence was re-checked here before; authorize_node above already
        # 404s on a missing node, so reaching this point means it exists.
        req = Request(
            node_id=body.node_id,
            # Promoted out of `params`, where the panel id has always been
            # written but could never be joined on — so every panel generation
            # landed in the ledger as spend belonging to nobody. Still written
            # to params as well: the worker and the studio UI both read it from
            # there, and moving those is a separate change.
            flow_panel_id=params.get("__panel_id"),
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
            # Distinguish "your budget ran out" from "the shared Avis key ran out"
            # so the UI can tell the user who to talk to.
            pool_avail = budget_service.pool_available_usd()
            detail = (
                "avis_pool_exhausted"
                if pool_avail is not None and pool_avail + 1e-9 < est
                else "insufficient_budget"
            )
            with get_session() as s:
                r = s.get(Request, rid)
                if r is not None:
                    r.status = "failed"
                    r.error = detail
                    s.add(r)
                    s.commit()
            raise HTTPException(status_code=402, detail=detail)
    get_worker().enqueue(rid)
    return row


@router.get("/{request_id}")
def get_request(request_id: int, user=Depends(get_optional_user)):
    with get_session() as s:
        # Request ids are sequential integers; polling one belonged to whoever
        # owns the node it targets, not to whoever guessed the number.
        return resource_guard.authorize_request(s, user, request_id)


@router.post("/{request_id}/cancel")
def cancel_request(request_id: int, user=Depends(get_optional_user)):
    """Cancel a queued request before the worker picks it up.

    Only ``queued`` rows are cancelable. The worker pulls rids off an
    in-memory ``asyncio.Queue`` and we can't yank a value back out, so
    we mark the row as ``failed`` with ``error='canceled'`` and let
    ``_process_one`` skip rows whose DB status drifted away from
    ``queued``. Returns 409 for any other state — running jobs need
    different surgery (in-flight HTTP calls to Flow).
    """
    with get_session() as s:
        req = resource_guard.authorize_request(s, user, request_id, "canvas.write")
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
