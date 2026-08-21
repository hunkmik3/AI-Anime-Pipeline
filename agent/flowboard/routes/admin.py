"""Admin account management (Phase 9 multi-user). Admin-only.

You (the owner/admin) provision accounts here — there is no open signup.
"""
from __future__ import annotations

import csv
import io
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlmodel import select

from flowboard.db import get_session
from flowboard.db.models import Project, User
from flowboard.routes.deps import require_admin, require_staff
from flowboard.services import (
    audit_service,
    flow_stats,
    budget_service,
    panel_service,
    project_service as ps,
    registration_service,
    sequence_quota,
    stats_service,
    user_service,
)

logger = logging.getLogger(__name__)

# The console as a whole is open to STAFF — admin or studio manager. The two
# endpoints where they must differ say so individually: budgets and promoting
# someone to admin stay `require_admin`, because a manager who could do either
# would be an admin under another name.
router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_staff)])


def _owner_only_role(caller, role: Optional[str]) -> None:
    """Only an owner mints an owner.

    A studio manager provisions the people who do the work — that is the point
    of the role. Letting them also hand out ``admin`` would make the whole split
    decorative: anyone who can create an admin has every right an admin has, one
    step removed.
    """
    if role == "admin" and getattr(caller, "role", None) != "admin":
        raise HTTPException(403, "only an admin can grant the admin role")


def _owner_only_money(caller) -> None:
    """Budgets are the owner's. A manager runs the work; the spend is not theirs
    to raise, and this endpoint sets it alongside a dozen harmless fields."""
    if getattr(caller, "role", None) != "admin":
        raise HTTPException(403, "only an admin can change budgets")


class CreateUserBody(BaseModel):
    username: str
    password: str
    role: str = "user"  # "admin" | "manager" | "user"
    display_name: Optional[str] = None
    email: Optional[str] = None


class UpdateUserBody(BaseModel):
    status: Optional[str] = None         # "active" | "suspended"
    password: Optional[str] = None       # reset password (forces change on next login)
    display_name: Optional[str] = None
    email: Optional[str] = None
    role: Optional[str] = None           # "admin" | "manager" | "user" (last-admin guarded)
    must_change_password: Optional[bool] = None
    budget_usd: Optional[float] = None       # set absolute $ budget
    add_budget_usd: Optional[float] = None   # top-up (+/-) $ budget
    # HR / employee-directory fields (Employees tab). Informational except
    # employment_status, which also suspends/reactivates the login account.
    employee_code: Optional[str] = None
    staff_category: Optional[str] = None
    job_title: Optional[str] = None
    rank: Optional[str] = None
    employment_status: Optional[str] = None  # active | resigned | terminated


def _user_with_budget(u) -> dict:
    d = user_service.public_dict(u)
    summ = budget_service.summary(u.id)
    if summ:
        d["available_usd"] = summ["available_usd"]
        d["reserved_usd"] = summ["reserved_usd"]
    return d


@router.get("/users")
def list_users() -> list[dict]:
    with get_session() as s:
        out = []
        for u in user_service.list_users():
            d = _user_with_budget(u)
            # Compact per-side role summary for the Employees table: the distinct
            # project-roles this person holds on Giant Studio and on Giantflow.
            # Exclude the owner-implicit producer role: it comes from OWNING a
            # project, not from an assignment, and the role dropdown can't change
            # it — surfacing it there just masks the role the admin actually set.
            d["studio_roles"] = sorted(
                {role for _p, role, is_owner in ps.projects_for_user(s, u.id) if not is_owner}
            )
            # GF role is a designation on the account (works with 0 comics).
            d["flow_roles"] = [u.flow_role] if u.flow_role else []
            out.append(d)
        return out


@router.post("/users")
def create_user(body: CreateUserBody, request: Request, caller=Depends(require_staff)) -> dict:
    _owner_only_role(caller, body.role)
    try:
        u = user_service.create_user(
            body.username,
            body.password,
            role=body.role,
            display_name=body.display_name,
            email=body.email,
            # Admin-provisioned password is a temp — force a change on first login.
            must_change_password=True,
        )
    except user_service.UsernameTaken:
        raise HTTPException(status_code=409, detail="username already exists")
    except user_service.UserError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    audit_service.record(
        "user.create", actor=caller, target=u,
        ip=audit_service.client_ip(request), detail=f"role={body.role}",
    )
    return user_service.public_dict(u)


@router.get("/audit")
def audit(limit: int = 200, action: str | None = None) -> list[dict]:
    """Recent security-audit entries (logins, SSO, admin actions), newest first."""
    return audit_service.list_recent(limit=min(max(1, limit), 1000), action=action)


@router.get("/stats/overview")
def stats_overview() -> dict:
    """Totals: spent, how much produced a kept clip vs burned on re-rolls."""
    return stats_service.overview()


@router.get("/stats/users")
def stats_users() -> list[dict]:
    """Per user: granted vs spent, split into money on KEPT clips vs money
    burned on discarded takes. Cannot be gamed — it's the actual bill."""
    return stats_service.user_costs()


@router.get("/stats/users/{user_id}/clips")
def stats_user_clips(user_id: str) -> list[dict]:
    """Drill-down: every clip that user made — takes, money burned, kept cost."""
    if user_service.get_by_id(user_id) is None:
        raise HTTPException(status_code=404, detail="user not found")
    return stats_service.user_clips(user_id)


@router.get("/stats/projects")
def stats_projects() -> list[dict]:
    return stats_service.project_costs()


@router.get("/stats/cost-tree")
def stats_cost_tree() -> list[dict]:
    """Spend nested project → series → episode → sequence."""
    return stats_service.cost_tree()


@router.get("/stats/shots")
def stats_all_shots() -> list[dict]:
    """Every shot that spent money (all projects), priciest first — total-only."""
    return stats_service.all_shots()


@router.get("/stats/projects/{project_id}/shots")
def stats_project_shots(project_id: str) -> list[dict]:
    """Per-shot spend inside a project (grouped by scene) — every shot, even
    the $0 ones, so admin sees which shot cost what."""
    return stats_service.project_shots(project_id)


@router.get("/stats/shots/{shot_id}/gens")
def stats_shot_gens(shot_id: str) -> list[dict]:
    """Drill-down: every generation in a shot — model, resolution, real cost,
    kept vs re-rolled, which member ran it, and when."""
    return stats_service.shot_gens(shot_id)


@router.get("/stats/timeline")
def stats_timeline(period: str = "day", buckets: int = 30) -> dict:
    """Credits burned per day/week/month/year, with who burned them in each.

    The totals say how much and the per-user view says who; this says *when*, which
    is what distinguishes steady spend from one expensive week.
    """
    return stats_service.spend_timeline(period=period, buckets=buckets)


@router.get("/stats/models")
def stats_models() -> list[dict]:
    return stats_service.model_costs()


@router.get("/stats/ledger")
def stats_ledger(
    project_id: Optional[str] = None,
    series_id: Optional[str] = None,
    scene_id: Optional[str] = None,
    shot_id: Optional[str] = None,
    user_id: Optional[str] = None,
    model: Optional[str] = None,
    kept: Optional[bool] = None,
    limit: int = 200,
    offset: int = 0,
) -> dict:
    """The full spend ledger: one row per billed generation, with who ran it and
    where it landed (project → series → episode → sequence).

    The overview says how much was spent; this says on what. Filterable down to a
    single sequence or one person, and ``totals`` always describes the whole
    filtered set rather than the visible page.
    """
    return stats_service.spend_ledger(
        project_id=project_id,
        series_id=series_id,
        scene_id=scene_id,
        shot_id=shot_id,
        user_id=user_id,
        model=model,
        kept=kept,
        limit=max(1, min(limit, 1000)),
        offset=max(0, offset),
    )


@router.get("/stats/ledger/filters")
def stats_ledger_filters() -> dict:
    """Only the projects, people and models that actually appear in the ledger —
    offering every row that has ever existed would make the filters useless."""
    return stats_service.ledger_filter_options()


class PoolBody(BaseModel):
    pool_usd: Optional[float] = None   # set the topped-up amount (absolute)
    add_usd: Optional[float] = None    # top up (+/-) by a delta


@router.get("/pool")
def get_pool() -> dict:
    """Global Avis pool: topped-up amount, real spend, holds, and whether the
    granted user budgets exceed what the shared key actually still holds.
    (Avis has no balance API — the pool is admin-entered.)"""
    return budget_service.pool_summary()


@router.patch("/pool")
def update_pool(body: PoolBody, request: Request, caller=Depends(require_admin)) -> dict:
    ip = audit_service.client_ip(request)
    if body.pool_usd is not None:
        v = budget_service.set_pool_usd(body.pool_usd)
        audit_service.record("pool.set", actor=caller, ip=ip, detail=f"pool=${v}")
    if body.add_usd is not None:
        v = budget_service.add_pool_usd(body.add_usd)
        audit_service.record("pool.topup", actor=caller, ip=ip, detail=f"add=${body.add_usd} → ${v}")
    return budget_service.pool_summary()


@router.get("/users/{user_id}/activity")
def user_activity(user_id: str, limit: int = 100) -> dict:
    """Per-user generation history for the admin view: what they generated,
    which model, the real $ cost, status, and output media ids."""
    u = user_service.get_by_id(user_id)
    if u is None:
        raise HTTPException(status_code=404, detail="user not found")
    data = budget_service.user_activity(user_id, limit=min(max(1, limit), 500))
    if data is None:
        raise HTTPException(status_code=404, detail="user not found")
    data["user"] = _user_with_budget(u)
    return data


def _parse_bound(v: Optional[str]) -> Optional[datetime]:
    """Parse an ISO date/datetime query param into a tz-aware UTC datetime.
    Accepts ``2026-08-01`` or a full ISO string (with/without ``Z``)."""
    if not v:
        return None
    s = v.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"invalid date: {v}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@router.get("/users/{user_id}/activity/export")
def user_activity_export(
    user_id: str,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = Query(None),
    tz: int = Query(0),
) -> Response:
    """Export a user's generation history in [from, to] as a CSV spreadsheet.

    ``from``/``to`` are ISO bounds (tz-aware); ``tz`` is the minutes to add to
    UTC to render timestamps in the admin's local time (JS
    ``-getTimezoneOffset()``). Admin-only via the router dependency.
    """
    u = user_service.get_by_id(user_id)
    if u is None:
        raise HTTPException(status_code=404, detail="user not found")
    df = _parse_bound(from_)
    dt_to = _parse_bound(to)
    data = budget_service.user_activity_export(user_id, date_from=df, date_to=dt_to)
    if data is None:
        raise HTTPException(status_code=404, detail="user not found")

    def _fmt(dtv) -> str:
        if not dtv:
            return ""
        if dtv.tzinfo is None:
            dtv = dtv.replace(tzinfo=timezone.utc)
        loc = dtv.astimezone(timezone.utc) + timedelta(minutes=tz)
        # HH:MM:SS D/M/YYYY (day/month not zero-padded), e.g. 03:08:54 12/8/2026
        return f"{loc:%H:%M:%S} {loc.day}/{loc.month}/{loc.year}"

    buf = io.StringIO()
    w = csv.writer(buf)
    # Columns mirror the on-screen activity table exactly.
    w.writerow(["Time", "Type / Model", "Params", "Cost", "Status"])
    for it in data["items"]:
        type_model = it.get("request_type") or it.get("kind") or ""
        model = it.get("model")
        if model:
            type_model = f"{type_model} / {model}" if type_model else str(model)
        # Params mirror the on-screen table: "{duration or —} · {resolution}".
        dur = it.get("duration_seconds")
        res = it.get("resolution")
        params = f"{dur}s" if dur else "—"
        if res:
            params = f"{params} · {res}"
        cost = it.get("cost_usd")
        w.writerow([
            _fmt(it.get("created_at")),
            type_model,
            params,
            "" if cost is None else f"${cost:.2f}",
            it.get("request_status") or "",
        ])

    # BOM so Excel opens the UTF-8 (Vietnamese prompts) correctly.
    body = ("﻿" + buf.getvalue()).encode("utf-8")
    lo = (from_ or "all").strip()[:10] or "all"
    hi = (to or "all").strip()[:10] or "all"
    fname = f"activity-{data['username']}-{lo}_{hi}.csv"
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.delete("/users/{user_id}")
def delete_user(user_id: str, request: Request, caller=Depends(require_staff)) -> dict:
    """Delete an account. Guards: can't delete yourself or the last admin.
    Owned projects are orphaned (not destroyed)."""
    if str(caller.id) == str(user_id):
        raise HTTPException(status_code=400, detail="cannot delete your own account")
    u = user_service.get_by_id(user_id)
    if u is None:
        raise HTTPException(status_code=404, detail="user not found")
    if u.role == "admin" and user_service.count_admins() <= 1:
        raise HTTPException(status_code=400, detail="cannot delete the last admin")
    label = u.username
    try:
        user_service.delete_user(user_id)
    except user_service.UserNotFound:
        raise HTTPException(status_code=404, detail="user not found")
    audit_service.record(
        "user.delete", actor=caller, target=user_id, target_label=label,
        ip=audit_service.client_ip(request),
    )
    return {"ok": True}


@router.patch("/users/{user_id}")
def update_user(user_id: str, body: UpdateUserBody, request: Request, caller=Depends(require_staff)) -> dict:
    ip = audit_service.client_ip(request)

    def _log(action: str, detail: str | None = None):
        audit_service.record(action, actor=caller, target=u, ip=ip, detail=detail)

    try:
        u = user_service.get_by_id(user_id)
        if u is None:
            raise HTTPException(status_code=404, detail="user not found")
        if body.display_name is not None:
            user_service.set_display_name(user_id, body.display_name)
        if body.email is not None:
            user_service.set_email(user_id, body.email)
            _log("user.email")
        if body.role is not None:
            _owner_only_role(caller, body.role)
            user_service.set_role(user_id, body.role)
            _log("user.role", f"role={body.role}")
        if body.password:
            user_service.set_password(user_id, body.password)
            _log("password.reset")
        if body.status is not None:
            user_service.set_status(user_id, body.status)
            _log("user.suspend" if body.status == "suspended" else "user.activate")
        if body.must_change_password is not None:
            user_service.set_must_change_password(user_id, body.must_change_password)
        if body.budget_usd is not None or body.add_budget_usd is not None:
            _owner_only_money(caller)
        if body.budget_usd is not None:
            user_service.set_budget(user_id, body.budget_usd)
            _log("user.budget", f"set=${body.budget_usd}")
        if body.add_budget_usd is not None:
            user_service.add_budget(user_id, body.add_budget_usd)
            _log("user.budget", f"add=${body.add_budget_usd}")
        # HR / employee-directory fields. Only forward the keys the client sent
        # (None = leave untouched; "" = clear that field).
        if any(
            v is not None
            for v in (body.employee_code, body.staff_category, body.job_title, body.rank)
        ):
            user_service.set_employee_fields(
                user_id,
                employee_code=body.employee_code,
                staff_category=body.staff_category,
                job_title=body.job_title,
                rank=body.rank,
            )
            _log("user.details")
        if body.employment_status is not None:
            user_service.set_employment_status(user_id, body.employment_status)
            _log("user.employment", f"status={body.employment_status}")
        refreshed = user_service.get_by_id(user_id)
        return _user_with_budget(refreshed)
    except user_service.UserNotFound:
        raise HTTPException(status_code=404, detail="user not found")
    except user_service.UserError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ── self-service signup approval queue ───────────────────────────────────────


class ApproveBody(BaseModel):
    budget_usd: Optional[float] = None    # optional starting $ budget


class RejectBody(BaseModel):
    notify: bool = False                  # email the applicant that it was declined


# ── the comic side, which the console could not see at all ──────────────────
#
# `stats_service` reads eight tables and not one of them belongs to Giantflow,
# so the console could answer "how is MoguTV going" and had nothing to say about
# 99 panels across 7 comics.


# ── sequences that have run out of attempts ─────────────────────────────────
#
# A queue, not a notification. Nothing is sent when the fifth attempt lands: the
# sequence appears on this list and leaves it when the limit is lifted or the
# work moves on. Anything recorded would need taking off the list too, and the
# day that is forgotten a PM is looking at work that is already going again.


class UnlockBody(BaseModel):
    #: How many more attempts to grant. Counted from where the sequence
    #: actually is, so the number on the button is the number they get.
    extra: int = 5


@router.get("/sequences/blocked")
def blocked_sequences(project_id: Optional[uuid.UUID] = None) -> list[dict]:
    with get_session() as s:
        return sequence_quota.blocked(s, project_id)


@router.post("/sequences/{shot_id}/unlock")
def unlock_sequence(
    shot_id: uuid.UUID, body: UnlockBody, request: Request,
    caller=Depends(require_staff),
) -> dict:
    with get_session() as s:
        try:
            out = sequence_quota.unlock(s, shot_id, body.extra)
        except Exception:
            raise HTTPException(404, "sequence not found")
        audit_service.record(
            "sequence.unlocked", actor=caller, ip=audit_service.client_ip(request),
            detail=f"{shot_id} +{body.extra} (now {out['limit']})",
        )
        return out


@router.post("/sequences/{shot_id}/relock")
def relock_sequence(
    shot_id: uuid.UUID, request: Request, caller=Depends(require_staff),
) -> dict:
    """Back to the house default — undo for a mis-click."""
    with get_session() as s:
        try:
            out = sequence_quota.relock(s, shot_id)
        except Exception:
            raise HTTPException(404, "sequence not found")
        audit_service.record(
            "sequence.relocked", actor=caller, ip=audit_service.client_ip(request),
            detail=str(shot_id),
        )
        return out


@router.get("/stats/comics")
def comic_overview() -> dict:
    """Slate-wide: how much comic work exists and how far through it is."""
    return flow_stats.overview()


@router.get("/stats/comics/by-comic")
def comic_rows() -> list[dict]:
    return flow_stats.by_comic()


@router.get("/stats/comics/by-artist")
def comic_artists() -> list[dict]:
    return flow_stats.by_artist()


@router.get("/stats/comics/quota")
def comic_quota() -> dict:
    """Today's image cap. An Atrium image costs quota, not money, so the spend
    figure alone cannot say whether the studio is about to hit a wall."""
    return flow_stats.quota_today()


@router.get("/stats/unattributed")
def unattributed_spend() -> dict:
    """Money the console cannot place, reported rather than dropped.

    A ledger that silently omits what it cannot explain is worse than one
    showing a gap: the total still looks right, so nobody goes looking.
    """
    return flow_stats.unattributed()


# ── who holds what, across both products ────────────────────────────────────
#
# The roles the studio talks about — "PM giantflow", "artist giantstudio" — were
# always expressible: a producer row on a comic, an artist row on a project. What
# was missing was anywhere to SEE them. Membership could only be queried per
# project and per comic, so "what does this person have" meant opening every one
# and counting, and granting meant walking to each project's own page.
#
# Staff, not admin-only: provisioning the people who do the work is the studio
# manager's job. Handing out the ADMIN system role is not, and is guarded
# separately (see `_owner_only_role`).


class GrantBody(BaseModel):
    #: producer | lead | artist | viewer — the project vocabulary, same on both
    #: sides. Which product a grant belongs to is the ROUTE, not a field: the two
    #: live in different tables with different guards, and a body that could name
    #: either would be one typo away from writing to the wrong one.
    role: str


def _roles_payload(session, user) -> dict:
    studio = [
        {
            "project_id": str(p.id),
            "name": p.name,
            "role": role,
            # An owner is a producer implicitly and has no member row, so their
            # grant can't be revoked here — say so rather than offering a no-op.
            "is_owner": is_owner,
        }
        for p, role, is_owner in ps.projects_for_user(session, user.id)
    ]
    flow = [
        {"series_id": comic.id, "name": comic.name, "role": role}
        for comic, role in panel_service.series_for_user(session, user.id)
    ]
    # How many targets a "one role per side" grant would actually apply to, so
    # the UI can disable a side that has nothing to assign (e.g. no comics yet)
    # instead of letting the dropdown flash and silently revert.
    total_projects = len(session.exec(select(Project)).all())
    owned = sum(1 for g in studio if g["is_owner"])
    return {
        "user_id": str(user.id),
        "username": user.username,
        "display_name": user.display_name,
        "system_role": user.role,
        "flow_role": user.flow_role,   # the GF designation (works with 0 comics)
        "studio": studio,
        "flow": flow,
        "studio_assignable": total_projects - owned,
        "flow_comics": len(panel_service.list_series(session)),
    }


@router.get("/users/{user_id}/roles")
def get_user_roles(user_id: str) -> dict:
    u = user_service.get_by_id(user_id)
    if u is None:
        raise HTTPException(status_code=404, detail="user not found")
    with get_session() as s:
        return _roles_payload(s, u)


@router.put("/users/{user_id}/roles/studio/{project_id}")
def grant_studio_role(
    user_id: str, project_id: uuid.UUID, body: GrantBody, request: Request,
    caller=Depends(require_staff),
) -> dict:
    u = user_service.get_by_id(user_id)
    if u is None:
        raise HTTPException(status_code=404, detail="user not found")
    with get_session() as s:
        if s.get(Project, project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        ps.set_project_member(s, project_id, u.id, body.role)
        audit_service.record(
            "project.role_granted", actor=caller, target=u,
            ip=audit_service.client_ip(request),
            detail=f"{project_id} = {body.role}",
        )
        return _roles_payload(s, u)


@router.delete("/users/{user_id}/roles/studio/{project_id}")
def revoke_studio_role(
    user_id: str, project_id: uuid.UUID, request: Request,
    caller=Depends(require_staff),
) -> dict:
    u = user_service.get_by_id(user_id)
    if u is None:
        raise HTTPException(status_code=404, detail="user not found")
    with get_session() as s:
        proj = s.get(Project, project_id)
        if proj is not None and proj.owner_user_id == u.id:
            raise HTTPException(
                status_code=409,
                detail="they own this project — hand it to someone else first",
            )
        ps.remove_project_member(s, project_id, u.id)
        audit_service.record(
            "project.role_revoked", actor=caller, target=u,
            ip=audit_service.client_ip(request), detail=str(project_id),
        )
        return _roles_payload(s, u)


@router.put("/users/{user_id}/roles/flow/{series_id}")
def grant_flow_role(
    user_id: str, series_id: int, body: GrantBody, request: Request,
    caller=Depends(require_staff),
) -> dict:
    u = user_service.get_by_id(user_id)
    if u is None:
        raise HTTPException(status_code=404, detail="user not found")
    with get_session() as s:
        try:
            panel_service.get_series(s, series_id)
        except panel_service.PanelError:
            raise HTTPException(status_code=404, detail="comic not found")
        panel_service.set_member(s, series_id, u.id, body.role)
        audit_service.record(
            "comic.role_granted", actor=caller, target=u,
            ip=audit_service.client_ip(request),
            detail=f"{series_id} = {body.role}",
        )
        return _roles_payload(s, u)


@router.delete("/users/{user_id}/roles/flow/{series_id}")
def revoke_flow_role(
    user_id: str, series_id: int, request: Request, caller=Depends(require_staff),
) -> dict:
    u = user_service.get_by_id(user_id)
    if u is None:
        raise HTTPException(status_code=404, detail="user not found")
    with get_session() as s:
        panel_service.remove_member(s, series_id, u.id)
        audit_service.record(
            "comic.role_revoked", actor=caller, target=u,
            ip=audit_service.client_ip(request), detail=str(series_id),
        )
        return _roles_payload(s, u)


# ── one role across a whole side (the simple Employees model) ───────────────


@router.put("/users/{user_id}/roles/studio-all")
def set_studio_role_all(
    user_id: str, body: GrantBody, request: Request, caller=Depends(require_staff),
) -> dict:
    """Give ONE Giant Studio role across every project — a person holds the same
    GS role everywhere. Projects they OWN keep their implicit producer and are
    left alone."""
    u = user_service.get_by_id(user_id)
    if u is None:
        raise HTTPException(status_code=404, detail="user not found")
    with get_session() as s:
        for p in s.exec(select(Project)).all():
            if p.owner_user_id == u.id:
                continue
            ps.set_project_member(s, p.id, u.id, body.role)
        audit_service.record(
            "project.role_granted_all", actor=caller, target=u,
            ip=audit_service.client_ip(request), detail=f"all = {body.role}",
        )
        return _roles_payload(s, u)


@router.delete("/users/{user_id}/roles/studio-all")
def clear_studio_role_all(
    user_id: str, request: Request, caller=Depends(require_staff),
) -> dict:
    """Remove this person from every Giant Studio project (owned ones excepted)."""
    u = user_service.get_by_id(user_id)
    if u is None:
        raise HTTPException(status_code=404, detail="user not found")
    with get_session() as s:
        for p in s.exec(select(Project)).all():
            if p.owner_user_id == u.id:
                continue
            ps.remove_project_member(s, p.id, u.id)
        audit_service.record(
            "project.role_revoked_all", actor=caller, target=u,
            ip=audit_service.client_ip(request), detail="all",
        )
        return _roles_payload(s, u)


@router.put("/users/{user_id}/roles/flow-all")
def set_flow_role_all(
    user_id: str, body: GrantBody, request: Request, caller=Depends(require_staff),
) -> dict:
    """Mark this person with ONE Giantflow role (the designation lives on the
    account, so it works even with zero comics) and apply it to any comics that
    already exist. New comics pick it up on creation."""
    u = user_service.get_by_id(user_id)
    if u is None:
        raise HTTPException(status_code=404, detail="user not found")
    with get_session() as s:
        db_u = s.get(User, u.id)
        db_u.flow_role = body.role
        s.add(db_u)
        s.commit()
        audit_service.record(
            "comic.role_designated", actor=caller, target=u,
            ip=audit_service.client_ip(request), detail=f"flow_role = {body.role}",
        )
        return _roles_payload(s, db_u)


@router.delete("/users/{user_id}/roles/flow-all")
def clear_flow_role_all(
    user_id: str, request: Request, caller=Depends(require_staff),
) -> dict:
    """Clear the Giantflow designation and remove them from every comic."""
    u = user_service.get_by_id(user_id)
    if u is None:
        raise HTTPException(status_code=404, detail="user not found")
    with get_session() as s:
        db_u = s.get(User, u.id)
        db_u.flow_role = None
        s.add(db_u)
        s.commit()
        audit_service.record(
            "comic.role_undesignated", actor=caller, target=u,
            ip=audit_service.client_ip(request), detail="cleared",
        )
        return _roles_payload(s, db_u)


@router.get("/registrations")
def list_registrations(status: Optional[str] = None) -> list[dict]:
    """Signup requests. `?status=pending` for the approval queue."""
    return registration_service.list_registrations(status)


@router.get("/registrations/pending-count")
def pending_registrations_count() -> dict:
    """Cheap poll for the sidebar badge."""
    return {"count": registration_service.pending_count()}


@router.post("/registrations/{reg_id}/approve")
def approve_registration(
    reg_id: str, body: ApproveBody, request: Request, caller=Depends(require_admin)
) -> dict:
    """Create the account, email the temp password, close the request.

    The response carries `temp_password` and `email_sent` — when the mail
    fails, the admin can still relay the credentials by hand.
    """
    try:
        res = registration_service.approve(
            reg_id, admin_label=caller.username, budget_usd=(body.budget_usd or 0.0)
        )
    except registration_service.RegistrationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except user_service.UserError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    audit_service.record(
        "signup.approved",
        actor=caller,
        target_label=res["email"],
        ip=audit_service.client_ip(request),
        detail=f"username={res['username']} email_sent={res['email_sent']}",
    )
    return res


@router.post("/registrations/{reg_id}/reject")
def reject_registration(
    reg_id: str, body: RejectBody, request: Request, caller=Depends(require_admin)
) -> dict:
    try:
        res = registration_service.reject(
            reg_id, admin_label=caller.username, notify=body.notify
        )
    except registration_service.RegistrationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    audit_service.record(
        "signup.rejected",
        actor=caller,
        target_label=res["email"],
        ip=audit_service.client_ip(request),
    )
    return res
