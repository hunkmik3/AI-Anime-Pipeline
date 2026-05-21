"""Shot CRUD + workflow snapshot + run/cancel/jobs.

The workflow snapshot endpoint replaces a shot's nodes+edges wholesale —
the simplest semantic for React Flow's "save canvas" gesture. Phase 7
will layer approval-gate pause/resume on top of ``run`` without changing
this surface.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import func
from sqlmodel import Session, select

from flowboard.db.models import Edge, Node, Request, Scene, Shot
from flowboard.short_id import generate_unique_short_id


class ShotNotFound(Exception):
    pass


class SceneNotFound(Exception):
    pass


# ── CRUD ──────────────────────────────────────────────────────────────────


def list_shots(session: Session, scene_id: uuid.UUID) -> list[Shot]:
    scene = session.get(Scene, scene_id)
    if scene is None:
        raise SceneNotFound(str(scene_id))
    return list(
        session.exec(
            select(Shot)
            .where(Shot.scene_id == scene_id)
            .order_by(Shot.order_index, Shot.created_at, Shot.id)
        ).all()
    )


def _next_shot_order_index(session: Session, scene_id: uuid.UUID) -> int:
    last = session.exec(
        select(func.max(Shot.order_index)).where(Shot.scene_id == scene_id)
    ).one()
    if isinstance(last, tuple):
        last = last[0]
    return int(last) + 1 if last is not None else 0


def create_shot(
    session: Session,
    scene_id: uuid.UUID,
    *,
    order_index: Optional[int] = None,
    script_text: str = "",
) -> Shot:
    scene = session.get(Scene, scene_id)
    if scene is None:
        raise SceneNotFound(str(scene_id))
    if order_index is None:
        order_index = _next_shot_order_index(session, scene_id)
    shot = Shot(scene_id=scene_id, order_index=order_index, script_text=script_text)
    session.add(shot)
    session.commit()
    session.refresh(shot)
    return shot


def get_shot(session: Session, shot_id: uuid.UUID) -> Shot:
    shot = session.get(Shot, shot_id)
    if shot is None:
        raise ShotNotFound(str(shot_id))
    return shot


def update_shot(
    session: Session,
    shot_id: uuid.UUID,
    *,
    patch: dict[str, Any],
) -> Shot:
    shot = get_shot(session, shot_id)
    for k, v in patch.items():
        setattr(shot, k, v)
    session.add(shot)
    session.commit()
    session.refresh(shot)
    return shot


def delete_shot(session: Session, shot_id: uuid.UUID) -> None:
    shot = get_shot(session, shot_id)
    session.delete(shot)
    session.commit()


# ── Workflow snapshot ─────────────────────────────────────────────────────


def get_workflow(
    session: Session, shot_id: uuid.UUID
) -> dict[str, list]:
    get_shot(session, shot_id)
    nodes = list(session.exec(select(Node).where(Node.shot_id == shot_id)).all())
    edges = list(session.exec(select(Edge).where(Edge.shot_id == shot_id)).all())
    return {"nodes": nodes, "edges": edges}


def put_workflow(
    session: Session,
    shot_id: uuid.UUID,
    *,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> dict[str, list]:
    """Snapshot-replace the shot's workflow.

    Strategy: delete every existing Node + Edge for this shot, recreate
    from the payload. Inbound payloads MAY include ``id``/``short_id`` to
    preserve identity, but the server doesn't trust client-supplied
    integer ids — we treat ``short_id`` as the stable handle so that
    references-to-node-by-short-id (in Plans, etc.) still resolve after
    the snapshot.

    Edge endpoints in the payload use ``source_id``/``target_id`` which
    may be either the *temporary* int ids from the previous snapshot
    (the client just round-tripped a GET) or, for cross-snapshot
    persistence, ``short_id`` strings. For Phase 2 we accept BOTH and
    map int IDs through a ``client_id → new_id`` table built during the
    node pass.
    """
    shot = get_shot(session, shot_id)

    # Drop the old graph. Edges first to avoid FK trouble.
    for e in session.exec(select(Edge).where(Edge.shot_id == shot.id)).all():
        session.delete(e)
    for n in session.exec(select(Node).where(Node.shot_id == shot.id)).all():
        session.delete(n)
    session.flush()

    # Build new nodes; track client-supplied id → new int id mapping so
    # edges in the same payload can wire up.
    client_to_new_id: dict[Any, int] = {}
    short_to_new_id: dict[str, int] = {}

    for raw in nodes:
        if not isinstance(raw, dict):
            continue
        node_type = raw.get("type")
        if not isinstance(node_type, str) or not node_type:
            raise ValueError("each node must include a non-empty 'type'")
        # Preserve short_id when client provides one; otherwise generate.
        short_id = raw.get("short_id")
        if not isinstance(short_id, str) or not short_id:
            short_id = generate_unique_short_id(session, shot.id)
        data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
        node = Node(
            shot_id=shot.id,
            short_id=short_id,
            type=node_type,
            x=float(raw.get("x", 0.0)),
            y=float(raw.get("y", 0.0)),
            w=float(raw.get("w", 240.0)),
            h=float(raw.get("h", 160.0)),
            data=data,
            status=raw.get("status") if isinstance(raw.get("status"), str) else "idle",
        )
        session.add(node)
        session.flush()
        assert node.id is not None
        client_id = raw.get("id")
        if client_id is not None:
            client_to_new_id[client_id] = node.id
        short_to_new_id[node.short_id] = node.id

    def _resolve_endpoint(raw: Any) -> Optional[int]:
        # int from previous snapshot
        if isinstance(raw, int) and raw in client_to_new_id:
            return client_to_new_id[raw]
        # short_id string (with or without leading #)
        if isinstance(raw, str):
            s = raw.strip().lstrip("#")
            if s in short_to_new_id:
                return short_to_new_id[s]
        return None

    for raw in edges:
        if not isinstance(raw, dict):
            continue
        src = _resolve_endpoint(raw.get("source_id"))
        dst = _resolve_endpoint(raw.get("target_id"))
        if src is None or dst is None or src == dst:
            continue
        kind = raw.get("kind") if raw.get("kind") in ("ref", "hint") else "ref"
        edge = Edge(
            shot_id=shot.id,
            source_id=src,
            target_id=dst,
            kind=kind,
            source_variant_idx=raw.get("source_variant_idx"),
        )
        session.add(edge)

    session.commit()

    fresh_nodes = list(
        session.exec(select(Node).where(Node.shot_id == shot.id)).all()
    )
    fresh_edges = list(
        session.exec(select(Edge).where(Edge.shot_id == shot.id)).all()
    )
    return {"nodes": fresh_nodes, "edges": fresh_edges}


# ── Run / cancel / jobs ───────────────────────────────────────────────────


def run_shot(session: Session, shot_id: uuid.UUID) -> Shot:
    """Phase 2: minimal contract.

    Sets the shot to ``running`` and returns it. Phase 7 will plug the
    workflow engine + approval-gate logic in here without changing the
    route signature.
    """
    shot = get_shot(session, shot_id)
    shot.status = "running"
    session.add(shot)
    session.commit()
    session.refresh(shot)
    return shot


def cancel_shot(session: Session, shot_id: uuid.UUID) -> Shot:
    """Mark the shot idle and clear any queued/running Request rows tied
    to nodes in this shot.
    """
    shot = get_shot(session, shot_id)

    node_ids = [
        n.id
        for n in session.exec(select(Node).where(Node.shot_id == shot.id)).all()
    ]
    if node_ids:
        now = datetime.now(timezone.utc)
        for req in session.exec(
            select(Request).where(
                Request.node_id.in_(node_ids),  # type: ignore[attr-defined]
                Request.status.in_(["queued", "running"]),  # type: ignore[attr-defined]
            )
        ).all():
            req.status = "failed"
            req.error = "cancelled"
            req.finished_at = now
            session.add(req)

    shot.status = "idle"
    session.add(shot)
    session.commit()
    session.refresh(shot)
    return shot


def list_shot_jobs(session: Session, shot_id: uuid.UUID) -> list[Request]:
    """List Request rows scoped to this shot via node membership."""
    get_shot(session, shot_id)
    node_ids = [
        n.id
        for n in session.exec(select(Node).where(Node.shot_id == shot_id)).all()
    ]
    if not node_ids:
        return []
    return list(
        session.exec(
            select(Request)
            .where(Request.node_id.in_(node_ids))  # type: ignore[attr-defined]
            .order_by(Request.created_at, Request.id)
        ).all()
    )
