"""Giantflow roles — who may do what, enforced server-side.

Sealed off from ``services/permissions.py`` on purpose. That module governs the
production hierarchy (Project → Series → Episode → Sequence) and its
``project_member`` table; this one governs comics, batches and panels and reads
``flow_project_member``. The vocabulary is deliberately the same so nobody has to
learn two ladders, but a role in one grants nothing in the other.

**This is the rule. The frontend's `store/giantflowRole.ts` is a drawing hint.**
Every capability is checked here as well, because a permission the UI merely
declines to render is not a permission — a hidden button is still a reachable
endpoint.

Resolving a role, in order:

1. A system admin is ``admin`` everywhere. They provision the accounts.
2. A member row wins next: that is the explicit grant.
3. Otherwise, whoever a batch in this project is ASSIGNED to is an ``artist`` on
   it. Assigning work is already the act of saying "this is yours", and making
   the PM repeat it as a membership row would be the same fact stored twice.
4. Everyone else signed in is a ``viewer``.

Rule 4 is a decision, not an oversight: the studio is a shared space, and a
locked-down default would have made every existing comic invisible the moment
this shipped — with no member rows yet, nobody would have been able to see
anything. Reading is open; acting requires being on the project.
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import HTTPException
from sqlmodel import Session, select

from flowboard.db.models import FlowBatch, FlowProjectMember, User

ADMIN = "admin"
PRODUCER = "producer"
LEAD = "lead"
ARTIST = "artist"
VIEWER = "viewer"

#: Assignable giantflow roles, most to least authority. ``admin`` is a system
#: role and is never stored on a member row.
FLOW_ROLES: tuple[str, ...] = (PRODUCER, LEAD, ARTIST, VIEWER)

_RANK: dict[str, int] = {VIEWER: 0, ARTIST: 1, LEAD: 2, PRODUCER: 3, ADMIN: 4}

#: capability → the least role that has it. Mirrors the table in
#: `frontend/src/store/giantflowRole.ts`; the two must not drift.
CAPABILITIES: dict[str, str] = {
    "panel.read": VIEWER,
    "panel.generate": ARTIST,
    "panel.submit": ARTIST,
    # A PM approves and sends back. An artist marking their own work approved is
    # the whole reason there is a review step.
    "panel.review": PRODUCER,
    "batch.manage": PRODUCER,
    "batch.import": PRODUCER,
    "member.manage": PRODUCER,
    # Creating, renaming and deleting a comic is an admin's call.
    "project.manage": ADMIN,
}


def normalize_role(role: Optional[str]) -> str:
    """Coerce a role string into one this module ranks. Accepts ``admin``,
    because a SYSTEM role legitimately passes through here."""
    r = (role or "").strip().lower()
    return r if r in _RANK else ARTIST


def normalize_member_role(role: Optional[str]) -> str:
    """The same, for a value stored on a member row — where ``admin`` is not a
    legal answer.

    ``admin`` is a system role. Letting a ``flow_project_member`` row carry it
    would mean one row on one comic granted the right to delete everybody
    else's, which is the opposite of what a per-project role is for. The write
    path coerces and the route validates; this is the third lock, on the read
    side, so a row inserted by any other means still cannot escalate.
    """
    r = (role or "").strip().lower()
    return r if r in FLOW_ROLES else ARTIST


def role_for(session: Session, user: Optional[User], project_id: Optional[int]) -> str:
    """This user's authority on this comic. See the module docstring for order."""
    # No auth configured (dev, and the whole existing test suite) — behave as the
    # single-user app always did rather than inventing a lockout.
    if user is None:
        return ADMIN
    if getattr(user, "role", None) == ADMIN:
        return ADMIN
    if project_id is None:
        return VIEWER
    row = session.exec(
        select(FlowProjectMember).where(
            FlowProjectMember.project_id == project_id,
            FlowProjectMember.user_id == user.id,
        )
    ).first()
    if row is not None:
        return normalize_member_role(row.role)
    assigned = session.exec(
        select(FlowBatch).where(
            FlowBatch.project_id == project_id,
            FlowBatch.assignee_user_id == user.id,
        )
    ).first()
    return ARTIST if assigned is not None else VIEWER


def allows(role: Optional[str], capability: str) -> bool:
    if capability not in CAPABILITIES:
        # Unknown capability is a programming error; refusing is the safe read.
        raise KeyError(f"unknown giantflow capability: {capability!r}")
    return _RANK.get(normalize_role(role), -1) >= _RANK[CAPABILITIES[capability]]


def require(
    session: Session, user: Optional[User], project_id: Optional[int], capability: str
) -> str:
    """Raise 403 unless this user has ``capability`` on this comic."""
    role = role_for(session, user, project_id)
    if not allows(role, capability):
        raise HTTPException(
            403,
            f"your role on this project ({role}) cannot {capability.replace('.', ' ')}",
        )
    return role


def capability_map(role: Optional[str]) -> dict[str, bool]:
    """Every capability and whether this role has it — what the UI draws from."""
    return {cap: allows(role, cap) for cap in CAPABILITIES}


def best_role(session: Session, user: Optional[User]) -> str:
    """The strongest role this user holds on any comic.

    Used only by global surfaces — the nav strip deciding whether to offer the
    Review tab at all. Per-project answers still come from ``role_for``.
    """
    if user is None or getattr(user, "role", None) == ADMIN:
        return ADMIN
    rows = session.exec(
        select(FlowProjectMember).where(FlowProjectMember.user_id == user.id)
    ).all()
    best = VIEWER
    for r in rows:
        if _RANK[normalize_member_role(r.role)] > _RANK[best]:
            best = normalize_member_role(r.role)
    if _RANK[best] < _RANK[ARTIST]:
        owns = session.exec(
            select(FlowBatch).where(FlowBatch.assignee_user_id == user.id)
        ).first()
        if owns is not None:
            best = ARTIST
    return best


__all__ = [
    "ADMIN",
    "ARTIST",
    "CAPABILITIES",
    "FLOW_ROLES",
    "LEAD",
    "PRODUCER",
    "VIEWER",
    "allows",
    "best_role",
    "capability_map",
    "normalize_member_role",
    "normalize_role",
    "require",
    "role_for",
]
