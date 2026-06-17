"""Project CRUD + bible + cost rollup.

Service functions take a live ``Session`` (the caller owns transactions).
They return ORM rows or plain dicts; never raise HTTPExceptions — route
adapters do the HTTP-layer translation.
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy import func
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
    Request,
    Scene,
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
    # Multi-user: scope to the owner when given (None = all → single-user/admin).
    stmt = select(Project)
    if owner_user_id is not None:
        stmt = stmt.where(Project.owner_user_id == owner_user_id)
    return list(session.exec(stmt.order_by(Project.created_at, Project.id)).all())


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
    # Multi-user: a scoped caller that isn't the owner sees a 404 (don't leak
    # existence). None = unscoped (single-user/admin/internal).
    if owner_user_id is not None and project.owner_user_id != owner_user_id:
        raise ProjectNotFound(str(project_id))
    return project


def update_project(
    session: Session,
    project_id: uuid.UUID,
    *,
    name: Optional[str] = None,
    settings: Optional[dict[str, Any]] = None,
) -> Project:
    project = get_project(session, project_id)
    if name is not None:
        project.name = name
    if settings is not None:
        project.settings = dict(settings)
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
