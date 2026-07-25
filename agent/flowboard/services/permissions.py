"""Phase 10 — per-project roles.

Two tiers of authority:

**System role** (``app_user.role``)
    ``admin``  — full rights everywhere: creates/renames/deletes Projects,
                 provisions accounts, assigns project roles, sees budget and
                 audit across the company.
    ``user``   — no admin console; sees only the projects they're assigned to.

**Project role** (``project_member.role``, per project)
    ``producer`` — runs the project: Series/Episode/Sequence CRUD + invites
                   members and sets their roles. The project's
                   ``owner_user_id`` is a producer implicitly (no member row).
    ``lead``     — builds and edits the structure, but can't delete a Series
                   or change who's on the project.
    ``artist``   — works inside the project: adds/edits Sequences and does all
                   canvas/generation work. Can't restructure above that.
    ``viewer``   — read-only.

The split matters because it's what moved structure-building out of the admin
console: an admin creates the Project and hands it to a producer, who then
builds Series → Episodes → Sequences themselves on the project home page.

No-auth path (``REQUIRE_AUTH`` off — dev and the whole existing test suite)
resolves to ``admin``, preserving the original single-user behaviour exactly.
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import HTTPException
from sqlmodel import Session, select

from flowboard.db.models import Project, ProjectMember, User

ADMIN = "admin"
PRODUCER = "producer"
LEAD = "lead"
ARTIST = "artist"
VIEWER = "viewer"

#: Assignable project roles, most to least authority. ``admin`` is a system
#: role and is never stored on a member row.
PROJECT_ROLES: tuple[str, ...] = (PRODUCER, LEAD, ARTIST, VIEWER)

_RANK: dict[str, int] = {VIEWER: 0, ARTIST: 1, LEAD: 2, PRODUCER: 3, ADMIN: 4}

#: capability → minimum role. Anything not listed is admin-only by omission
#: (``require`` raises on an unknown capability rather than silently allowing).
CAPABILITIES: dict[str, str] = {
    # Series tier
    "series.create": LEAD,
    "series.update": LEAD,
    "series.delete": PRODUCER,
    # Episode / Chapter (scene)
    "episode.create": LEAD,
    "episode.update": LEAD,
    "episode.delete": LEAD,
    # Sequence (shot)
    "sequence.create": ARTIST,
    "sequence.update": ARTIST,
    "sequence.delete": LEAD,
    # Canvas: nodes, edges, prompts, generation
    "canvas.write": ARTIST,
    "canvas.read": VIEWER,
    # Cosmetic (covers, layout) — anyone who can work in the project
    "project.decorate": ARTIST,
    # Membership + roles
    "member.manage": PRODUCER,
    # Create / rename / delete the Project itself
    "project.manage": ADMIN,
}


class ProjectAccessDenied(Exception):
    """Caller is on the project but lacks the capability."""


def normalize_role(role: Optional[str]) -> str:
    """Coerce stored/incoming role strings to a known project role."""
    r = (role or "").strip().lower()
    return r if r in PROJECT_ROLES else ARTIST


def project_role(
    session: Session, user: Optional[User], project_id: uuid.UUID
) -> Optional[str]:
    """The caller's effective role on this project, or ``None`` if they have no
    access at all (which callers surface as 404, never 403 — a user shouldn't
    learn that a project they can't see exists)."""
    if user is None or user.role == ADMIN:
        # No-auth dev/test path and real admins are both unscoped.
        return ADMIN
    project = session.get(Project, project_id)
    if project is None:
        return None
    if project.owner_user_id == user.id:
        return PRODUCER
    member = session.exec(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user.id,
        )
    ).first()
    return normalize_role(member.role) if member else None


def role_allows(role: Optional[str], capability: str) -> bool:
    if role is None:
        return False
    try:
        needed = CAPABILITIES[capability]
    except KeyError:  # pragma: no cover - programming error
        raise ValueError(f"unknown capability {capability!r}")
    return _RANK.get(role, -1) >= _RANK[needed]


def require(
    session: Session,
    user: Optional[User],
    project_id: uuid.UUID,
    capability: str,
) -> str:
    """Gate a project-scoped write. Returns the caller's role on success.

    404 when they can't see the project at all, 403 when they can but the role
    is too low.
    """
    role = project_role(session, user, project_id)
    if role is None:
        raise HTTPException(404, "project not found")
    if not role_allows(role, capability):
        raise HTTPException(
            403,
            f"your role on this project ({role}) cannot {capability.replace('.', ' ')}",
        )
    return role


def capability_map(role: Optional[str]) -> dict[str, bool]:
    """Flat can-I map for the frontend, so the UI hides what it can't do
    instead of letting the user click into a 403."""
    return {cap: role_allows(role, cap) for cap in CAPABILITIES}
