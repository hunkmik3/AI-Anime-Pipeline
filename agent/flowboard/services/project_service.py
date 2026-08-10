"""Project CRUD + bible + cost rollup.

Service functions take a live ``Session`` (the caller owns transactions).
They return ORM rows or plain dicts; never raise HTTPExceptions — route
adapters do the HTTP-layer translation.
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy import func, or_
from sqlmodel import Session, select

from flowboard.db.models import (
    Asset,
    ChatMessage,
    Edge,
    Node,
    PipelineRun,
    Plan,
    PlanRevision,
    Project,
    ProjectFlowMapping,
    ProjectMember,
    Request,
    Scene,
    SceneCollaborator,
    Series,
    Shot,
)


class ProjectNotFound(Exception):
    pass


class FlowProjectNotBound(Exception):
    pass


# ── CRUD ──────────────────────────────────────────────────────────────────


def list_projects(
    session: Session, owner_user_id: Optional[uuid.UUID] = None
) -> list[Project]:
    # Multi-user: scope to the caller when given (None = all → single-user/admin).
    # A scoped user sees a project they OWN, are a MEMBER of, or hold WORK in.
    #
    # The last one was missing and made assignment a dead end: an episode handed to
    # somebody showed on their "My work" page while this page said "No projects
    # assigned to you yet", and the episode 404'd when opened. The three work
    # sources are the ones `permissions.visible_scope` narrows by, so a project
    # listed here is one the caller can actually open — and it shows only the
    # episodes that are theirs once inside.
    stmt = select(Project)
    if owner_user_id is not None:
        member_pids = select(ProjectMember.project_id).where(
            ProjectMember.user_id == owner_user_id
        )
        assigned_pids = select(Scene.project_id).where(
            Scene.assignee_user_id == owner_user_id
        )
        helper_pids = (
            select(Scene.project_id)
            .join(SceneCollaborator, SceneCollaborator.scene_id == Scene.id)  # type: ignore[arg-type]
            .where(SceneCollaborator.user_id == owner_user_id)
        )
        produced_pids = select(Series.project_id).where(
            or_(
                Series.producer_user_id == owner_user_id,
                Series.assignee_user_id == owner_user_id,
            )
        )
        stmt = stmt.where(
            or_(
                Project.owner_user_id == owner_user_id,
                Project.id.in_(member_pids),  # type: ignore[attr-defined]
                Project.id.in_(assigned_pids),  # type: ignore[attr-defined]
                Project.id.in_(helper_pids),  # type: ignore[attr-defined]
                Project.id.in_(produced_pids),  # type: ignore[attr-defined]
            )
        )
    return list(session.exec(stmt.order_by(Project.created_at, Project.id)).all())


def is_project_member(
    session: Session, project_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    return (
        session.exec(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user_id,
            )
        ).first()
        is not None
    )


def user_can_access_project(
    session: Session, project: Project, user_id: uuid.UUID
) -> bool:
    """A scoped (non-admin) caller may access a project they own or are a
    member of. Admins/no-auth pass ``user_id=None`` and never reach here."""
    return project.owner_user_id == user_id or is_project_member(
        session, project.id, user_id
    )


def get_project_member_ids(
    session: Session, project_id: uuid.UUID
) -> list[uuid.UUID]:
    return [
        m.user_id
        for m in session.exec(
            select(ProjectMember)
            .where(ProjectMember.project_id == project_id)
            .order_by(ProjectMember.created_at, ProjectMember.id)
        ).all()
    ]


def get_project_members(session: Session, project_id: uuid.UUID) -> list[ProjectMember]:
    return list(
        session.exec(
            select(ProjectMember)
            .where(ProjectMember.project_id == project_id)
            .order_by(ProjectMember.created_at, ProjectMember.id)
        ).all()
    )


def set_project_member(
    session: Session,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str,
) -> ProjectMember:
    """Grant or change ONE person's role, leaving the rest of the roster alone.

    Separate from ``set_project_members``, which replaces the whole set. That is
    the right shape for the project page — you edit a roster there and see it
    entire — and the wrong shape for granting from the admin console, where you
    have one person in front of you and no idea who else is on the project. A
    caller with a partial view must not be able to write a total one.
    """
    from flowboard.services import permissions

    row = session.exec(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
        )
    ).first()
    if row is None:
        row = ProjectMember(
            project_id=project_id,
            user_id=user_id,
            role=permissions.normalize_role(role),
        )
    else:
        row.role = permissions.normalize_role(role)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def remove_project_member(
    session: Session, project_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    """Take one person off a project. Silent when they were not on it — the
    caller asked for them to be off, and they are."""
    row = session.exec(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
        )
    ).first()
    if row is not None:
        session.delete(row)
        session.commit()


def projects_for_user(session: Session, user_id: uuid.UUID) -> list[tuple[Project, str, bool]]:
    """Every project this person has a standing in, as (project, role, is_owner).

    Asked by USER, which nothing could do before: membership was only ever
    queried per project, so "what does this person have" meant opening every
    project and counting. That is the question the admin console asks.

    The owner is included even though they have no member row — they are a
    producer implicitly, and a list that omitted them would say an owner has no
    role on their own project.
    """
    out: list[tuple[Project, str, bool]] = []
    seen: set[uuid.UUID] = set()
    for p in session.exec(select(Project).where(Project.owner_user_id == user_id)).all():
        out.append((p, "producer", True))
        seen.add(p.id)
    rows = session.exec(
        select(ProjectMember).where(ProjectMember.user_id == user_id)
    ).all()
    for m in rows:
        if m.project_id in seen:
            continue
        proj = session.get(Project, m.project_id)
        if proj is not None:
            out.append((proj, m.role, False))
    return sorted(out, key=lambda x: (x[0].name or "").lower())


def set_project_members(
    session: Session,
    project_id: uuid.UUID,
    user_ids: list[uuid.UUID],
    roles: Optional[dict[uuid.UUID, str]] = None,
) -> None:
    """Replace a project's additional-member set with ``user_ids`` (the owner is
    stored separately on the project and need not appear here). Idempotent.

    ``roles`` maps user id → project role; anyone missing from it keeps the role
    they already had, or gets the default for a new row. Callers that don't care
    about roles (the legacy assign-by-id path) leave it None and never disturb
    existing roles.
    """
    from flowboard.services import permissions

    want = list(dict.fromkeys(user_ids))  # dedupe, keep order
    existing = session.exec(
        select(ProjectMember).where(ProjectMember.project_id == project_id)
    ).all()
    by_user = {m.user_id: m for m in existing}
    for m in existing:
        if m.user_id not in want:
            session.delete(m)
    for uid in want:
        wanted_role = (roles or {}).get(uid)
        row = by_user.get(uid)
        if row is None:
            session.add(
                ProjectMember(
                    project_id=project_id,
                    user_id=uid,
                    role=permissions.normalize_role(wanted_role),
                )
            )
        elif wanted_role is not None:
            row.role = permissions.normalize_role(wanted_role)
            session.add(row)
    session.commit()


def create_project(
    session: Session,
    *,
    name: str,
    project_bible: Optional[dict[str, Any]] = None,
    settings: Optional[dict[str, Any]] = None,
    owner_user_id: Optional[uuid.UUID] = None,
) -> Project:
    project = Project(
        name=name,
        project_bible=project_bible or {},
        settings=settings or {},
        owner_user_id=owner_user_id,
    )
    session.add(project)
    session.commit()
    session.refresh(project)
    return project


def get_project(
    session: Session,
    project_id: uuid.UUID,
    owner_user_id: Optional[uuid.UUID] = None,
) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise ProjectNotFound(str(project_id))
    # Multi-user: a scoped caller who is neither owner nor an assigned member
    # sees a 404 (don't leak existence). None = unscoped (single-user/admin/
    # internal). This is the single access chokepoint for the whole project
    # tree — scenes/shots/nodes/references all gate through get_project().
    if owner_user_id is not None and not user_can_access_project(
        session, project, owner_user_id
    ):
        raise ProjectNotFound(str(project_id))
    return project


_UNSET = object()


def update_project(
    session: Session,
    project_id: uuid.UUID,
    *,
    name: Optional[str] = None,
    settings: Optional[dict[str, Any]] = None,
    owner_user_id: Any = _UNSET,
) -> Project:
    project = get_project(session, project_id)
    if name is not None:
        project.name = name
    if settings is not None:
        project.settings = dict(settings)
    # Sentinel-guarded so "not provided" differs from "reassign to unowned".
    if owner_user_id is not _UNSET:
        project.owner_user_id = owner_user_id
    session.add(project)
    session.commit()
    session.refresh(project)
    return project


def delete_project(session: Session, project_id: uuid.UUID) -> None:
    """Cascade delete the entire project tree.

    Postgres FK ``ON DELETE CASCADE`` would handle most rows on its own,
    but we replicate the explicit-cleanup pattern from the legacy board
    shim so test assertions stay deterministic across transaction
    boundaries and Phase 7 doesn't accidentally rely on cascade timing.
    """
    project = get_project(session, project_id)

    # Plan / PlanRevision / PipelineRun hang off Shot.id; sweep them before
    # the cascade so the row-by-row CASCADE doesn't have to traverse four
    # hops in a single statement.
    shot_ids = [
        row.id
        for row in session.exec(
            select(Shot).join(Scene).where(Scene.project_id == project_id)
        ).all()
    ]
    if shot_ids:
        plan_ids = [
            p.id
            for p in session.exec(
                select(Plan).where(Plan.shot_id.in_(shot_ids))  # type: ignore[attr-defined]
            ).all()
        ]
        if plan_ids:
            for prv in session.exec(
                select(PlanRevision).where(
                    PlanRevision.plan_id.in_(plan_ids)  # type: ignore[attr-defined]
                )
            ).all():
                session.delete(prv)
            for run in session.exec(
                select(PipelineRun).where(
                    PipelineRun.plan_id.in_(plan_ids)  # type: ignore[attr-defined]
                )
            ).all():
                session.delete(run)
            for pl in session.exec(
                select(Plan).where(Plan.id.in_(plan_ids))  # type: ignore[attr-defined]
            ).all():
                session.delete(pl)

    mapping = session.get(ProjectFlowMapping, project_id)
    if mapping is not None:
        session.delete(mapping)

    # Assignment rows FK project.id (no ON DELETE CASCADE via create_all) — drop
    # them explicitly or the project delete would hit a FK violation.
    for m in session.exec(
        select(ProjectMember).where(ProjectMember.project_id == project_id)
    ).all():
        session.delete(m)

    session.delete(project)
    session.commit()


# ── Detail / counts ───────────────────────────────────────────────────────


def project_scene_count(session: Session, project_id: uuid.UUID) -> int:
    n = session.exec(
        select(func.count(Scene.id)).where(Scene.project_id == project_id)
    ).one()
    if isinstance(n, tuple):
        n = n[0]
    return int(n or 0)


def project_asset_count(session: Session, project_id: uuid.UUID) -> int:
    n = session.exec(
        select(func.count(Asset.id)).where(Asset.project_id == project_id)
    ).one()
    if isinstance(n, tuple):
        n = n[0]
    return int(n or 0)


# ── cover / thumbnail ───────────────────────────────────────────────────────


def project_images(
    session: Session, project_id: uuid.UUID, *, limit: int = 24
) -> list[dict[str, Any]]:
    """Recent image assets in a project (newest first) — the pool the admin
    picks a cover from. Each item: {media_id, url}."""
    rows = session.exec(
        select(Asset)
        .where(
            Asset.project_id == project_id,
            Asset.kind == "image",
            Asset.uuid_media_id.is_not(None),
        )
        .order_by(Asset.created_at.desc(), Asset.id.desc())
        .limit(max(1, min(limit, 100)))
    ).all()
    return [{"media_id": a.uuid_media_id, "url": f"/media/{a.uuid_media_id}"} for a in rows]


def set_project_cover(session: Session, project_id: uuid.UUID, media_id: Optional[str]):
    """Set (or clear, when media_id is None) a project's cover thumbnail,
    stored in settings.cover_media_id. Cosmetic — not structural."""
    project = get_project(session, project_id)
    settings = dict(project.settings or {})
    if media_id:
        settings["cover_media_id"] = str(media_id)
    else:
        settings.pop("cover_media_id", None)
    project.settings = settings
    session.add(project)
    session.commit()
    session.refresh(project)
    return project


def project_thumb_media_id(session: Session, project) -> Optional[str]:
    """The media id to show as the project's cover: an admin-set override
    (``settings.cover_media_id``) wins; otherwise the newest image asset."""
    override = (project.settings or {}).get("cover_media_id")
    if override:
        return str(override)
    latest = session.exec(
        select(Asset.uuid_media_id)
        .where(
            Asset.project_id == project.id,
            Asset.kind == "image",
            Asset.uuid_media_id.is_not(None),
        )
        .order_by(Asset.created_at.desc(), Asset.id.desc())
        .limit(1)
    ).first()
    if isinstance(latest, tuple):
        latest = latest[0]
    return str(latest) if latest else None


def project_cost_usd(session: Session, project_id: uuid.UUID) -> float:
    """Sum ``Request.result['cost_usd']`` across every node in every shot
    in every scene of this project.

    Phase 1 worker writes ``cost_usd`` into ``Request.result`` (may be
    missing on rows from before that landed). We coerce missing/None
    to 0.0 so the rollup stays well-defined.
    """
    # Pull the project tree in two cheap queries; the alternative is a
    # 4-table join that Postgres handles fine, but the two-step is easier
    # to reason about and the row counts are bounded by project size.
    # ``session.exec(select(col))`` returns scalar values directly under
    # SQLModel — no row wrapper to unpack.
    shot_ids = list(
        session.exec(
            select(Shot.id).join(Scene).where(Scene.project_id == project_id)
        ).all()
    )
    if not shot_ids:
        return 0.0
    node_ids = list(
        session.exec(
            select(Node.id).where(Node.shot_id.in_(shot_ids))  # type: ignore[attr-defined]
        ).all()
    )
    if not node_ids:
        return 0.0
    total = 0.0
    for req in session.exec(
        select(Request).where(Request.node_id.in_(node_ids))  # type: ignore[attr-defined]
    ).all():
        result = req.result or {}
        cost = result.get("cost_usd")
        if isinstance(cost, (int, float)):
            total += float(cost)
    return round(total, 6)


# ── Bible ─────────────────────────────────────────────────────────────────


def get_project_bible(session: Session, project_id: uuid.UUID) -> dict[str, Any]:
    project = get_project(session, project_id)
    return dict(project.project_bible or {})


def put_project_bible(
    session: Session, project_id: uuid.UUID, bible: dict[str, Any]
) -> dict[str, Any]:
    """Replace the bible JSONB wholesale. Caller validates shape via
    Pydantic before reaching this function — no extra coercion here.
    """
    project = get_project(session, project_id)
    project.project_bible = dict(bible)
    session.add(project)
    session.commit()
    session.refresh(project)
    return dict(project.project_bible or {})


# ── Flow project binding ──────────────────────────────────────────────────


def get_flow_project(session: Session, project_id: uuid.UUID) -> ProjectFlowMapping:
    get_project(session, project_id)  # 404 first
    row = session.get(ProjectFlowMapping, project_id)
    if row is None:
        raise FlowProjectNotBound(str(project_id))
    return row


def bind_flow_project(
    session: Session, project_id: uuid.UUID, flow_project_id: str
) -> ProjectFlowMapping:
    """Idempotent: returns existing row if already bound."""
    existing = session.get(ProjectFlowMapping, project_id)
    if existing is not None:
        return existing
    row = ProjectFlowMapping(project_id=project_id, flow_project_id=flow_project_id)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


# ── Chat ──────────────────────────────────────────────────────────────────


def list_project_chat(
    session: Session, project_id: uuid.UUID, *, limit: int = 500
) -> list[ChatMessage]:
    get_project(session, project_id)  # 404 first
    q = (
        select(ChatMessage)
        .where(ChatMessage.project_id == project_id)
        .order_by(ChatMessage.created_at, ChatMessage.id)
        .limit(limit)
    )
    return list(session.exec(q).all())


# Re-exported so route handlers don't need to know which child tables exist
# beyond Project — only ``delete_project`` and friends.
__all__ = [
    "ProjectNotFound",
    "FlowProjectNotBound",
    "list_projects",
    "create_project",
    "get_project",
    "update_project",
    "delete_project",
    "project_scene_count",
    "project_asset_count",
    "project_cost_usd",
    "get_project_bible",
    "put_project_bible",
    "get_flow_project",
    "bind_flow_project",
    "list_project_chat",
    # Re-export ORM types so route adapters don't import models directly
    # for type hints.
    "Asset",
    "Edge",
    "Node",
]
