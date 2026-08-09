"""Phase 10 — per-project roles.

Two tiers of authority:

**System role** (``app_user.role``)
    ``admin``  — full rights everywhere: creates/renames/deletes Projects,
                 provisions accounts, assigns project roles, sees budget and
                 audit across the company.
    ``user``   — no admin console; sees only the projects they're assigned to.

**Project role** (``project_member.role``, per project)
    ``producer`` — the PM. Runs the project: Series/Episode/Sequence CRUD,
                   invites members and sets their roles. The project's
                   ``owner_user_id`` is a producer implicitly (no member row).
    ``artist``   — works inside the project: adds/edits Sequences and does all
                   canvas/generation work. Can't restructure above that.
    ``viewer``   — read-only.

There used to be a ``lead`` between producer and artist. The studio treats a
lead and a PM as the same person, so it was one job under two names, split by a
line nobody could state — a lead could rename a series but not create one, and
delete a sequence but not the episode holding it. Stored rows are still read as
producer; see ``normalize_role``.

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
#: Retired. The studio treats a lead and a PM as the same person, so the two
#: roles were one job with two names and a fiddly split of rights between them —
#: a lead could rename a series but not create one, delete a sequence but not an
#: episode. The constant stays only so stored rows can be recognised and read as
#: producer; it is not assignable and carries no rank.
LEAD = "lead"
ARTIST = "artist"
VIEWER = "viewer"

#: Assignable project roles, most to least authority. ``admin`` is a system
#: role and is never stored on a member row.
PROJECT_ROLES: tuple[str, ...] = (PRODUCER, ARTIST, VIEWER)

_RANK: dict[str, int] = {VIEWER: 0, ARTIST: 1, PRODUCER: 3, ADMIN: 4}

#: capability → minimum role. Anything not listed is admin-only by omission
#: (``require`` raises on an unknown capability rather than silently allowing).
CAPABILITIES: dict[str, str] = {
    # Structure — Series and Episode.
    #
    # All of it is the PM's, create and edit alike. The four tiers are
    # who-does-what as much as they are a shape: the admin opens a Project, the
    # PM lays out its Series and Episodes, an artist is handed an Episode and
    # generates Sequences inside it. Splitting "may rename" from "may create"
    # only made sense while there was a rank between PM and artist to give the
    # first to; with that gone, the split is a line with nobody standing on it.
    "series.create": PRODUCER,
    "series.update": PRODUCER,
    "series.delete": PRODUCER,
    "episode.create": PRODUCER,
    "episode.update": PRODUCER,
    "episode.delete": PRODUCER,
    # Sequence (shot) — the artist's own workspace. Deleting one stays with the
    # PM: an artist re-doing their work replaces a sequence's contents, and
    # removing the sequence itself changes the shape of the episode.
    "sequence.create": ARTIST,
    "sequence.update": ARTIST,
    "sequence.delete": PRODUCER,
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
    """Coerce stored/incoming role strings to a known project role.

    ``lead`` is mapped UP to producer, not left to the unknown-value fallback.
    That fallback is ``artist``, so simply deleting the role from the list would
    have quietly demoted every lead in the database — the wrong direction, and
    the kind of change nobody notices until someone cannot do their job.
    """
    r = (role or "").strip().lower()
    if r == LEAD:
        return PRODUCER
    return r if r in PROJECT_ROLES else ARTIST


def project_role(
    session: Session, user: Optional[User], project_id: uuid.UUID
) -> Optional[str]:
    """The caller's effective role on this project, or ``None`` if they have no
    access at all (which callers surface as 404, never 403 — a user shouldn't
    learn that a project they can't see exists)."""
    if user is None or user.role in ("admin", "manager"):
        # No-auth dev/test path, admins and studio managers are all unscoped
        # INSIDE a project. The manager/admin line is drawn in the console — at
        # budgets and at who may be made an admin — not here: someone who can
        # open a project but not then lay it out could not use the permission
        # they were given.
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


# ── visibility scope ────────────────────────────────────────────────────────
#
# Roles above answer *what you may do*; this answers *what you may see*.
#
# Being on a project used to mean seeing all of it, so an artist assigned one
# episode could read every colleague's work in the same project. The workflow
# diagram is narrower: you see the subtree you are assigned to, plus the
# ancestors above it (you need those to navigate), but never your siblings —
# a worker on Ep03 must not see Ep01 or Ep02.
#
# The scope is derived from the assignments a PM already makes — the episode's
# assignee and the Series Producer — rather than a second place to declare it.
# That way handing someone an episode grants exactly the access to do it, and
# taking it back removes that access, with nothing to keep in sync.
#
# Narrowing applies to ``artist`` and ``viewer`` only. Producers and leads build
# the structure (they create the series and episodes others get assigned to), so
# scoping them to their own assignments would leave them unable to do their job.

#: Roles whose view is limited to what they're assigned. Higher roles run the
#: project and see all of it.
SCOPED_ROLES: tuple[str, ...] = (ARTIST, VIEWER)


def is_scoped(role: Optional[str]) -> bool:
    return role in SCOPED_ROLES


def visible_scope(
    session: Session, user: Optional[User], project_id: uuid.UUID
) -> Optional[dict]:
    """What this caller may see inside a project.

    ``None`` means unrestricted (admins, producers, leads). Otherwise a dict of
    ``{"series_ids": set, "scene_ids": set}`` — the episodes they own or produce,
    and the series those sit in so the tree can still be navigated.
    """
    from flowboard.db.models import Scene, SceneCollaborator, Series

    role = project_role(session, user, project_id)
    if role is None or not is_scoped(role) or user is None:
        return None

    # Episodes assigned directly to them.
    scene_ids = set(
        session.exec(
            select(Scene.id).where(
                Scene.project_id == project_id,
                Scene.assignee_user_id == user.id,
            )
        ).all()
    )
    # …and episodes they were added to as a helper. An episode has one owner —
    # the person who hands the cut in — but a chapter split between three panel
    # artists arrives as one episode, and "one person animates all of it" stops
    # being realistic once it is large. A helper sees and works inside exactly
    # the episodes they were added to, and nothing else.
    scene_ids |= set(
        session.exec(
            select(SceneCollaborator.scene_id)
            .join(Scene, Scene.id == SceneCollaborator.scene_id)
            .where(
                Scene.project_id == project_id,
                SceneCollaborator.user_id == user.id,
            )
        ).all()
    )
    # Series they produce — that whole subtree is theirs (the "Manager assigned
    # at Series" case: every episode under it is visible).
    produced = set(
        session.exec(
            select(Series.id).where(
                Series.project_id == project_id,
                Series.producer_user_id == user.id,
            )
        ).all()
    )
    if produced:
        scene_ids |= set(
            session.exec(
                select(Scene.id).where(Scene.series_id.in_(produced))  # type: ignore[attr-defined]
            ).all()
        )

    # Ancestors are readable: the series holding a visible episode shows in the
    # tree, but its other episodes do not.
    series_ids = set(produced)
    if scene_ids:
        series_ids |= {
            sid
            for sid in session.exec(
                select(Scene.series_id).where(Scene.id.in_(scene_ids))  # type: ignore[attr-defined]
            ).all()
            if sid
        }
    return {"series_ids": series_ids, "scene_ids": scene_ids}


def can_see_scene(
    session: Session, user: Optional[User], project_id: uuid.UUID, scene_id: uuid.UUID
) -> bool:
    scope = visible_scope(session, user, project_id)
    return scope is None or scene_id in scope["scene_ids"]


def can_see_series(
    session: Session, user: Optional[User], project_id: uuid.UUID, series_id: uuid.UUID
) -> bool:
    scope = visible_scope(session, user, project_id)
    return scope is None or series_id in scope["series_ids"]


def require_scene(
    session: Session,
    user: Optional[User],
    project_id: uuid.UUID,
    scene_id: uuid.UUID,
    capability: str,
) -> str:
    """``require``, plus the visibility check for a specific episode.

    Out-of-scope reads 404 rather than 403 — to someone who may not see an
    episode, it must be indistinguishable from one that doesn't exist.
    """
    role = require(session, user, project_id, capability)
    if not can_see_scene(session, user, project_id, scene_id):
        raise HTTPException(404, "episode not found")
    return role
