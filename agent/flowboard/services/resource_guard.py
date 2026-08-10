"""Authorize a request against the object it names.

The app grew from a single-user tool, so its older routes authorize nothing: they
look a row up by id and act on it. An audit found 42 such endpoints. Because a
Node and an Edge use sequential integer primary keys, "look it up by id" means
any authenticated employee could walk 1..N and read, rewrite or delete another
team's canvas — and, through ``POST /api/requests``, spend another team's credit
budget.

Every one of those routes needs the same thing: walk the object up to the Project
that owns it, then apply the normal role + visibility rules. Writing that walk
per route is what caused the gap in the first place, so it lives here once.

    node → shot → scene → project
    edge → shot → scene → project
    request → node → …
    reference / media / plan → whatever owns it

Two rules hold throughout:

**Invisible is indistinguishable from absent.** Every failure is a 404, never a
403 — a 403 would confirm the id exists, which is exactly what an enumeration
attack wants. ``permissions.require`` already behaves this way for projects.

**Episode scope applies, not just project membership.** These go through
``permissions.require_scene``, so an artist assigned one episode cannot reach a
sibling episode's canvas. Gating on project membership alone was the second class
of finding in the audit — including in two routes written the same week.

The no-auth path (``REQUIRE_AUTH`` off — dev and most of the test suite) resolves
to admin and passes everything, so this changes nothing for single-user use.
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import HTTPException
from sqlmodel import Session

from flowboard.db.models import (
    Edge,
    Node,
    Project,
    Reference,
    Request,
    Scene,
    Series,
    Shot,
    Submission,
    User,
)
from flowboard import config
from flowboard.services import permissions

#: Raised as 404 for everything. See the module docstring.
_NOT_FOUND = "not found"


def _deny(what: str = "resource") -> HTTPException:
    return HTTPException(404, f"{what} {_NOT_FOUND}")


# ── walking an object up to its project ─────────────────────────────────────


def scene_of_shot(session: Session, shot_id: uuid.UUID) -> Optional[Scene]:
    shot = session.get(Shot, shot_id)
    if shot is None:
        return None
    return session.get(Scene, shot.scene_id)


def scene_of_node(session: Session, node_id: int) -> Optional[Scene]:
    node = session.get(Node, node_id)
    if node is None or not node.shot_id:
        return None
    return scene_of_shot(session, node.shot_id)


# ── the guards ──────────────────────────────────────────────────────────────


def authorize_project(
    session: Session,
    user: Optional[User],
    project_id: uuid.UUID,
    capability: str = "canvas.read",
) -> Project:
    """Gate a whole-project operation. Returns the project."""
    project = session.get(Project, project_id)
    if project is None:
        raise _deny("project")
    permissions.require(session, user, project_id, capability)
    return project


def authorize_scene(
    session: Session,
    user: Optional[User],
    scene_id: uuid.UUID,
    capability: str = "canvas.read",
) -> Scene:
    """Gate an episode-level operation, honouring the visibility scope."""
    scene = session.get(Scene, scene_id)
    if scene is None:
        raise _deny("episode")
    permissions.require_scene(session, user, scene.project_id, scene.id, capability)
    return scene


def authorize_series(
    session: Session,
    user: Optional[User],
    series_id: uuid.UUID,
    capability: str = "canvas.read",
) -> Series:
    series = session.get(Series, series_id)
    if series is None:
        raise _deny("series")
    permissions.require(session, user, series.project_id, capability)
    if not permissions.can_see_series(session, user, series.project_id, series_id):
        raise _deny("series")
    return series


def authorize_shot(
    session: Session,
    user: Optional[User],
    shot_id: uuid.UUID,
    capability: str = "canvas.read",
) -> Shot:
    """Gate a sequence-level operation."""
    shot = session.get(Shot, shot_id)
    if shot is None:
        raise _deny("sequence")
    scene = session.get(Scene, shot.scene_id)
    if scene is None:
        raise _deny("sequence")
    permissions.require_scene(session, user, scene.project_id, scene.id, capability)
    return shot


def authorize_node(
    session: Session,
    user: Optional[User],
    node_id: int,
    capability: str = "canvas.read",
) -> Node:
    """Gate a canvas-node operation.

    The single most important guard here: node ids are sequential integers, so
    without it a caller can enumerate every node in the company.
    """
    node = session.get(Node, node_id)
    if node is None:
        raise _deny("node")
    scene = scene_of_node(session, node_id)
    if scene is None:
        # An orphan node (no shot) belongs to no project, so no project role can
        # authorize it. Deny rather than fall through to an open path.
        raise _deny("node")
    permissions.require_scene(session, user, scene.project_id, scene.id, capability)
    return node


def authorize_edge(
    session: Session,
    user: Optional[User],
    edge_id: int,
    capability: str = "canvas.read",
) -> Edge:
    edge = session.get(Edge, edge_id)
    if edge is None:
        raise _deny("edge")
    scene = scene_of_shot(session, edge.shot_id) if edge.shot_id else None
    if scene is None:
        raise _deny("edge")
    permissions.require_scene(session, user, scene.project_id, scene.id, capability)
    return edge


def authorize_request(
    session: Session,
    user: Optional[User],
    request_id: int,
    capability: str = "canvas.read",
) -> Request:
    """Gate a generation request by the node it targets.

    ``POST /api/requests`` was the costliest gap in the audit: queuing work
    against someone else's node both overwrote their canvas and drew down their
    credit budget, which made the whole budget ceiling bypassable.
    """
    req = session.get(Request, request_id)
    if req is None:
        raise _deny("request")
    if req.node_id is None:
        # Flow Studio generations carry no node — the studio has no canvas. It is
        # a shared space by decision, so any signed-in caller may poll them. This
        # is deliberately narrowed to that one request type: every OTHER node-less
        # request still means "nothing to scope against", which stays admin-only.
        if req.type == "flow_gen_image":
            require_signed_in(session, user)
            return req
        if not is_unscoped(session, user):
            raise _deny("request")
        return req
    authorize_node(session, user, req.node_id, capability)
    return req


def authorize_submission(
    session: Session,
    user: Optional[User],
    submission_id: uuid.UUID,
    capability: str = "canvas.read",
) -> Submission:
    """Gate a delivery record by the SERIES it belongs to.

    Project membership alone is not enough: it let anyone on the project read
    another team's delivery history and stream its cut, which is the one thing this
    route exists to control — the file stays Restricted on Drive and the app is the
    only way in.

    Old rows point at an episode instead; they are gated by that episode, so
    history written under the previous rule keeps exactly the reach it had.
    """
    row = session.get(Submission, submission_id)
    if row is None:
        raise _deny("submission")
    if row.series_id is not None:
        series = session.get(Series, row.series_id)
        if series is None:
            raise _deny("submission")
        permissions.require(session, user, series.project_id, capability)
        if not permissions.can_see_series(session, user, series.project_id, series.id):
            raise _deny("submission")
    elif row.scene_id is not None:
        authorize_scene(session, user, row.scene_id, capability)
    else:
        raise _deny("submission")
    return row


def authorize_reference(
    session: Session,
    user: Optional[User],
    reference_id: int,
    capability: str = "canvas.read",
) -> Reference:
    """Gate an asset-library entry by its project.

    Three cases, narrowest first:

    - **Has a project** → authorize against it, as normal.
    - **Has a Flow Studio board** (``source_board_id``) → any signed-in caller.
      The studio is a shared space by decision, so its images belong to the team
      rather than to a project or a person.
    - **Neither** → a legacy global row; unscoped callers only, since there is
      nothing to authorize against.
    """
    ref = session.get(Reference, reference_id)
    if ref is None:
        raise _deny("reference")
    pid = getattr(ref, "project_id", None)
    if pid is None:
        if getattr(ref, "source_board_id", None) is not None:
            require_signed_in(session, user)
            return ref
        if not is_unscoped(session, user):
            raise _deny("reference")
        return ref
    authorize_project(session, user, pid, capability)
    return ref


def is_unscoped(session: Session, user: Optional[User]) -> bool:
    """True for admins and the no-auth dev path — callers with no project limits.

    Used by routes whose object has no owning project (installation-wide config,
    legacy global rows), where "which project?" has no answer and the only safe
    rule is admin-only.
    """
    return user is None or user.role == permissions.ADMIN


def require_unscoped(session: Session, user: Optional[User]) -> None:
    """Gate an installation-wide operation to admins.

    403 rather than 404 here: unlike an object id, the existence of a global
    setting is not a secret, and telling a user plainly that it is admin-only is
    more useful than pretending the endpoint isn't there.
    """
    if not is_unscoped(session, user):
        raise HTTPException(403, "admin only")


def require_signed_in(session: Session, user: Optional[User]) -> None:
    """Gate a **shared workspace** — open to everyone with an account, closed to
    the public.

    This is the weakest real gate in the codebase, and it exists for exactly one
    surface: Flow Studio at ``/giantflow``. The studio is a shared space by
    decision — one board list everybody works in, one API key everybody spends —
    so there is no owner to authorize against, and the honest check is "do you
    have an account here".

    What it therefore does NOT protect against: any signed-in user can see,
    rename and delete any studio board, and delete anyone's generated images.
    That is the accepted cost of leaving the studio unscoped; scoping it to a
    project is what replaces this call (see ``docs/INTEGRATION_PLAN.md``).

    ``user is None`` passes only on the no-auth dev path — with
    ``FLOWBOARD_REQUIRE_AUTH=1`` the dependency rejects anonymous callers before a
    handler ever runs, so this cannot become a public hole in a real deployment.
    """
    if user is None and config.REQUIRE_AUTH:
        raise HTTPException(401, "sign in first")
