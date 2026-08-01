"""Credit budgets scoped to the production hierarchy (Phase 11.1, 11.5).

The existing ``budget_service`` meters **per user** and against the shared Avis
pool. This module adds the production-side ceiling from the workflow diagrams:

    a budget is set on the Project, the Series, an Episode, or a Sequence
      → when a scope is exhausted, generation is BLOCKED (not warned)
        → the Employee, the Series Producer and the PM are alerted
          → a PM *requests* more credit (amount + required reason)
            → an ADMIN approves or rejects it; only an approved request
              raises the ceiling (see CreditGrant.status)

All four tiers can carry a ceiling and **every** one of them applies: the gate
reports the innermost tier that would be breached, so an artist is told "this
sequence is out of quota" rather than something about the project. Tiers are
independent — an Episode quota is not divided among its Sequences (see SCOPES).

Budget lives in JSONB bags, so no tier needed a column of its own:
``project.settings``, and ``production`` on series / scene / shot, all under the
key ``credit_budget_usd``. ``0`` / missing means *unlimited* — existing projects
keep working untouched until someone sets a number.

    effective = base + SUM(approved grants)
    remaining = effective − (settled spend + outstanding reservations)
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Session, select

from flowboard.db.models import (
    CreditGrant,
    Node,
    Project,
    Request,
    Scene,
    Series,
    Shot,
    UsageRecord,
    User,
)

BUDGET_KEY = "credit_budget_usd"
# All four tiers of the hierarchy can carry a ceiling. Ordered tightest-first,
# which is the order the gate checks them in: a Sequence's own limit binds before
# its Episode's, before the Series, before the Project.
#
# The tiers are independent on purpose — an Episode quota is NOT divided among
# its Sequences. How to split one is a production decision (evenly? by length?
# by the artist's judgement?), and guessing would silently impose a rule nobody
# asked for. Set only the Episode quota and its sequences stay individually
# uncapped but collectively bounded, which is the least surprising behaviour.
SCOPES = ("shot", "scene", "series", "project")


# "scene"/"shot" are the table names; the studio says Episode and Sequence.
# Error messages and alerts go to people, so they use the production words.
_NOUNS = {"project": "project", "series": "series", "scene": "episode", "shot": "sequence"}


def scope_noun(scope: str) -> str:
    return _NOUNS.get(scope, scope)


class GrantError(Exception):
    """Bad grant input (unknown scope, non-positive amount, missing reason)."""


# ── helpers ─────────────────────────────────────────────────────────────────


def _as_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def base_budget(session: Session, scope: str, scope_id: uuid.UUID) -> float:
    """The set base budget for a scope. 0 = unlimited."""
    if scope == "project":
        p = session.get(Project, scope_id)
        return _as_float((p.settings or {}).get(BUDGET_KEY)) if p else 0.0
    if scope == "series":
        s = session.get(Series, scope_id)
        return _as_float((s.production or {}).get(BUDGET_KEY)) if s else 0.0
    if scope == "scene":
        sc = session.get(Scene, scope_id)
        return _as_float((sc.production or {}).get(BUDGET_KEY)) if sc else 0.0
    if scope == "shot":
        sh = session.get(Shot, scope_id)
        return _as_float((sh.production or {}).get(BUDGET_KEY)) if sh else 0.0
    raise GrantError(f"unknown scope {scope!r}")


def _scope_label(session: Session, scope: str, scope_id: uuid.UUID) -> Optional[str]:
    """Human name for a scope, so the audit trail reads as names not UUIDs."""
    if scope == "project":
        p = session.get(Project, scope_id)
        return p.name if p else None
    if scope == "series":
        s = session.get(Series, scope_id)
        return (s.code or s.name) if s else None
    if scope == "scene":
        sc = session.get(Scene, scope_id)
        return (sc.code or sc.name) if sc else None
    if scope == "shot":
        sh = session.get(Shot, scope_id)
        return (getattr(sh, "code", None) or getattr(sh, "name", None)) if sh else None
    return None


def granted_total(session: Session, scope: str, scope_id: uuid.UUID) -> float:
    """Extra credit that actually landed — **approved grants only**. A pending
    request must not move the ceiling, otherwise asking would be the same as
    getting."""
    rows = session.exec(
        select(CreditGrant).where(
            CreditGrant.scope == scope,
            CreditGrant.scope_id == scope_id,
            CreditGrant.status == "approved",
        )
    ).all()
    return round(sum(_as_float(r.amount_usd) for r in rows), 6)


def _shot_ids_for_scope(
    session: Session, scope: str, scope_id: uuid.UUID
) -> list[uuid.UUID]:
    """Every Sequence under a scope — the unit generations hang off."""
    if scope == "shot":
        return [scope_id]
    if scope == "scene":
        return list(
            session.exec(select(Shot.id).where(Shot.scene_id == scope_id)).all()
        )
    if scope == "project":
        return list(
            session.exec(
                select(Shot.id).join(Scene).where(Scene.project_id == scope_id)
            ).all()
        )
    if scope == "series":
        return list(
            session.exec(
                select(Shot.id).join(Scene).where(Scene.series_id == scope_id)
            ).all()
        )
    raise GrantError(f"unknown scope {scope!r}")


def spend_usd(session: Session, scope: str, scope_id: uuid.UUID) -> dict:
    """What a scope has consumed: settled ``actual`` spend plus still-open
    reservations (so two concurrent gens can't both slip past the ceiling).

    Walks Scene → Shot → Node → Request → UsageRecord. Requests carry the real
    ``cost_usd`` the worker recorded; UsageRecord carries the reservation.
    """
    shot_ids = _shot_ids_for_scope(session, scope, scope_id)
    if not shot_ids:
        return {"spent_usd": 0.0, "reserved_usd": 0.0}
    node_ids = list(
        session.exec(
            select(Node.id).where(Node.shot_id.in_(shot_ids))  # type: ignore[attr-defined]
        ).all()
    )
    if not node_ids:
        return {"spent_usd": 0.0, "reserved_usd": 0.0}

    req_ids: list[int] = []
    spent = 0.0
    for req in session.exec(
        select(Request).where(Request.node_id.in_(node_ids))  # type: ignore[attr-defined]
    ).all():
        if req.id is not None:
            req_ids.append(req.id)
        cost = (req.result or {}).get("cost_usd")
        if isinstance(cost, (int, float)):
            spent += float(cost)

    reserved = 0.0
    if req_ids:
        for ur in session.exec(
            select(UsageRecord).where(
                UsageRecord.request_id.in_(req_ids),  # type: ignore[attr-defined]
                UsageRecord.status == "reserved",
            )
        ).all():
            reserved += _as_float(ur.estimated_usd)

    return {"spent_usd": round(spent, 6), "reserved_usd": round(reserved, 6)}


def summary(session: Session, scope: str, scope_id: uuid.UUID) -> dict:
    """Budget picture for one scope. ``unlimited`` when no base budget is set."""
    base = base_budget(session, scope, scope_id)
    grants = granted_total(session, scope, scope_id)
    sp = spend_usd(session, scope, scope_id)
    effective = round(base + grants, 6)
    used = round(sp["spent_usd"] + sp["reserved_usd"], 6)
    unlimited = base <= 0
    return {
        "scope": scope,
        "scope_id": str(scope_id),
        "base_usd": round(base, 6),
        "granted_usd": grants,
        "effective_usd": effective,
        "spent_usd": sp["spent_usd"],
        "reserved_usd": sp["reserved_usd"],
        "used_usd": used,
        "remaining_usd": None if unlimited else round(effective - used, 6),
        "unlimited": unlimited,
        "used_pct": None if unlimited or effective <= 0 else round(used / effective * 100, 1),
    }


# ── the gate ────────────────────────────────────────────────────────────────


def scope_for_node(session: Session, node_id: Optional[int]) -> dict:
    """Resolve a node up to its Episode/Series/Project so the gate knows which
    budgets to check. Missing links are returned as None rather than raising —
    a node with no shot (scene-level/orphan) simply has nothing to meter."""
    out: dict = {"shot_id": None, "scene_id": None, "series_id": None, "project_id": None}
    if node_id is None:
        return out
    node = session.get(Node, node_id)
    if node is None or not node.shot_id:
        return out
    out["shot_id"] = node.shot_id
    shot = session.get(Shot, node.shot_id)
    if shot is None:
        return out
    out["scene_id"] = shot.scene_id
    scene = session.get(Scene, shot.scene_id)
    if scene is None:
        return out
    out["series_id"] = scene.series_id
    out["project_id"] = scene.project_id
    return out


def check(session: Session, node_id: Optional[int], estimated_usd: float) -> Optional[dict]:
    """Can this generation proceed? Returns ``None`` when yes, or the blocking
    scope's summary (plus ``needed_usd``) when a ceiling would be breached.

    Every tier that has a ceiling applies, and the innermost one that would be
    breached is the one reported — so the artist is told "this sequence is out of
    quota", not "the project is", which is both truer and actionable.
    """
    loc = scope_for_node(session, node_id)
    tiers = (
        ("shot", loc["shot_id"]),
        ("scene", loc["scene_id"]),
        ("series", loc["series_id"]),
        ("project", loc["project_id"]),
    )
    for scope, sid in tiers:
        if not sid:
            continue
        s = summary(session, scope, sid)
        if s["unlimited"]:
            continue
        if (s["remaining_usd"] or 0.0) + 1e-9 < estimated_usd:
            s["needed_usd"] = round(estimated_usd, 6)
            return s
    return None


# ── commands ────────────────────────────────────────────────────────────────


def _set_bag_budget(obj, amount: float, attr: str) -> None:
    """Write the ceiling into an object's JSONB bag (copy-then-assign, so
    SQLAlchemy sees the change)."""
    bag = dict(getattr(obj, attr) or {})
    bag[BUDGET_KEY] = amount
    setattr(obj, attr, bag)


def set_budget(
    session: Session,
    scope: str,
    scope_id: uuid.UUID,
    amount_usd: float,
    *,
    user: Optional[User] = None,
    ip: Optional[str] = None,
) -> dict:
    """BOD sets (or clears, with 0) the base budget for a scope.

    Audited. Raising a ceiling here bypasses the request/approve flow entirely,
    so without a record this would be the one unlogged way to authorise spend —
    the grant path is meticulous about who asked and why, and the direct path
    must not be the quiet exception.
    """
    amount = max(0.0, _as_float(amount_usd))
    previous = base_budget(session, scope, scope_id)
    label = _scope_label(session, scope, scope_id)
    _MODELS = {
        "project": (Project, "settings", "project"),
        "series": (Series, "production", "series"),
        "scene": (Scene, "production", "episode"),
        "shot": (Shot, "production", "sequence"),
    }
    if scope not in _MODELS:
        raise GrantError(f"unknown scope {scope!r}")
    model, attr, noun = _MODELS[scope]
    obj = session.get(model, scope_id)
    if obj is None:
        raise GrantError(f"{noun} not found")
    _set_bag_budget(obj, amount, attr)
    session.add(obj)
    session.commit()

    from flowboard.services import audit_service

    # 0 means "no ceiling" here, so show that in words — a bare "100 → 0" in the
    # trail reads like the budget was zeroed when it was actually uncapped.
    def _money(v: float) -> str:
        return "unlimited" if not v else f"${v:g}"

    audit_service.record_change(
        "budget.set",
        object_type=scope,
        object_id=scope_id,
        object_label=label,
        changes={"budget": (_money(previous), _money(amount))},
        actor=user,
        ip=ip,
    )
    return summary(session, scope, scope_id)


def request_grant(
    session: Session,
    scope: str,
    scope_id: uuid.UUID,
    *,
    amount_usd: float,
    reason: str,
    user: Optional[User],
) -> CreditGrant:
    """A PM asks for extra credit. Lands as ``pending`` and moves nothing — an
    admin has to approve it. A reason is mandatory: overspend without a stated
    cause is exactly what the ceiling exists to prevent."""
    if scope not in SCOPES:
        raise GrantError(f"unknown scope {scope!r}")
    amount = _as_float(amount_usd)
    if amount <= 0:
        raise GrantError("grant amount must be greater than 0")
    clean = (reason or "").strip()
    if not clean:
        raise GrantError("a reason is required for a credit request")
    # Validate the scope exists before writing the request.
    base_budget(session, scope, scope_id)
    row = CreditGrant(
        scope=scope,
        scope_id=scope_id,
        amount_usd=amount,
        reason=clean,
        granted_by=user.id if user is not None else None,
        status="pending",
    )
    session.add(row)
    session.commit()
    session.refresh(row)

    from flowboard.services import audit_service

    audit_service.record_change(
        "budget.grant_requested",
        object_type=scope,
        object_id=scope_id,
        object_label=_scope_label(session, scope, scope_id),
        actor=user,
        note=f"asked for ${amount:g} — {clean}",
    )
    return row


def decide_grant(
    session: Session,
    grant_id: uuid.UUID,
    *,
    approve: bool,
    user: Optional[User],
    note: Optional[str] = None,
) -> CreditGrant:
    """Admin verdict on a pending request. Approving is what actually raises the
    ceiling; rejecting needs a note so the asker knows why."""
    row = session.get(CreditGrant, grant_id)
    if row is None:
        raise GrantError("credit request not found")
    if row.status != "pending":
        raise GrantError(f"this request is already {row.status}")
    clean = (note or "").strip() or None
    if not approve and not clean:
        raise GrantError("a note is required when rejecting a credit request")
    row.status = "approved" if approve else "rejected"
    row.decided_by = user.id if user is not None else None
    row.decided_at = datetime.now(timezone.utc)
    row.decision_note = clean
    session.add(row)
    session.commit()
    session.refresh(row)

    from flowboard.services import audit_service

    verdict = "approved" if approve else "rejected"
    audit_service.record_change(
        f"budget.grant_{verdict}",
        object_type=row.scope,
        object_id=row.scope_id,
        object_label=_scope_label(session, row.scope, row.scope_id),
        actor=user,
        note=(
            f"{verdict} ${_as_float(row.amount_usd):g}"
            + (f" — {clean}" if clean else "")
            + f" (asked: {row.reason})"
        ),
    )
    return row


def list_grants(session: Session, scope: str, scope_id: uuid.UUID) -> list[CreditGrant]:
    """Every request for a scope — pending, approved and rejected — newest first,
    so the panel shows both the history and what's still waiting."""
    return list(
        session.exec(
            select(CreditGrant)
            .where(CreditGrant.scope == scope, CreditGrant.scope_id == scope_id)
            .order_by(CreditGrant.created_at.desc())  # type: ignore[attr-defined]
        ).all()
    )


def pending_grants(session: Session) -> list[CreditGrant]:
    """The admin's inbox: every credit request awaiting a verdict."""
    return list(
        session.exec(
            select(CreditGrant)
            .where(CreditGrant.status == "pending")
            .order_by(CreditGrant.created_at.desc())  # type: ignore[attr-defined]
        ).all()
    )


# ── who to alert when a gen is blocked ──────────────────────────────────────


def alert_recipients(session: Session, node_id: Optional[int]) -> list[User]:
    """Employee (episode assignee) + Series Producer + project PM — the three
    people the diagram says must hear about an exhausted budget. Deduped."""
    loc = scope_for_node(session, node_id)
    ids: list[Optional[uuid.UUID]] = []
    if loc["scene_id"]:
        scene = session.get(Scene, loc["scene_id"])
        if scene is not None:
            ids.append(scene.assignee_user_id)
    if loc["series_id"]:
        series = session.get(Series, loc["series_id"])
        if series is not None:
            ids.append(series.producer_user_id)
    if loc["project_id"]:
        project = session.get(Project, loc["project_id"])
        if project is not None:
            ids.append(project.owner_user_id)

    seen: set[uuid.UUID] = set()
    out: list[User] = []
    for uid in ids:
        if not uid or uid in seen:
            continue
        seen.add(uid)
        u = session.get(User, uid)
        if u is not None:
            out.append(u)
    return out


def notify_blocked(
    session: Session,
    node_id: Optional[int],
    blocked: dict,
    actor: Optional[User] = None,
) -> None:
    """Tell the Employee, the Series Producer and the PM that a generation was
    blocked on an exhausted budget.

    Always writes an audit entry (so the block is on the record and visible in
    the admin Audit log even with no mail server), then emails whoever has an
    address — best-effort, never raising into the request path.
    """
    from flowboard.services import audit_service, email_service

    scope = blocked.get("scope")
    used = blocked.get("used_usd")
    eff = blocked.get("effective_usd")
    recipients = alert_recipients(session, node_id)
    names = ", ".join((u.display_name or u.username) for u in recipients) or "(nobody assigned)"

    try:
        audit_service.record(
            "budget.blocked",
            actor=actor,
            actor_label=(actor.display_name or actor.username) if actor else None,
            detail=(
                f"{scope} budget exhausted: used ${used} of ${eff}; "
                f"node={node_id}; alerted: {names}"
            ),
        )
    except Exception:  # pragma: no cover - never break a gen on logging
        pass

    if not email_service.is_configured():
        return
    subject = f"[Flowboard] {str(scope).title()} credit budget exhausted"
    body = (
        f"A video generation was blocked: the {scope} has used "
        f"${used} of its ${eff} credit budget.\n\n"
        "A PM can grant more credit (a reason is required) from the project's "
        "budget panel, or the work can be submitted with the best version "
        "already generated.\n"
    )
    for u in recipients:
        addr = getattr(u, "email", None)
        if addr:
            try:
                email_service.send(addr, subject, body)
            except Exception:  # pragma: no cover
                pass
