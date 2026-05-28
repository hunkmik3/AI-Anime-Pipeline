"""Scene CRUD + bible + reorder + (Phase 7 stub) compose."""
from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy import func
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import Session, select

from flowboard.db.models import Asset, Project, Scene, Shot


class SceneNotFound(Exception):
    pass


class ProjectNotFound(Exception):
    pass


class InvalidBibleAsset(Exception):
    """``master_establishing_asset_id`` doesn't belong to the scene's project."""


# ── CRUD ──────────────────────────────────────────────────────────────────


def list_scenes(session: Session, project_id: uuid.UUID) -> list[Scene]:
    project = session.get(Project, project_id)
    if project is None:
        raise ProjectNotFound(str(project_id))
    return list(
        session.exec(
            select(Scene)
            .where(Scene.project_id == project_id)
            .order_by(Scene.order_index, Scene.created_at, Scene.id)
        ).all()
    )


def _next_scene_order_index(session: Session, project_id: uuid.UUID) -> int:
    last = session.exec(
        select(func.max(Scene.order_index)).where(Scene.project_id == project_id)
    ).one()
    if isinstance(last, tuple):
        last = last[0]
    return int(last) + 1 if last is not None else 0


def create_scene(
    session: Session,
    project_id: uuid.UUID,
    *,
    name: str,
    order_index: Optional[int] = None,
) -> Scene:
    project = session.get(Project, project_id)
    if project is None:
        raise ProjectNotFound(str(project_id))
    if order_index is None:
        order_index = _next_scene_order_index(session, project_id)
    scene = Scene(
        project_id=project_id,
        name=name,
        order_index=order_index,
    )
    session.add(scene)
    session.commit()
    session.refresh(scene)
    return scene


def get_scene(session: Session, scene_id: uuid.UUID) -> Scene:
    scene = session.get(Scene, scene_id)
    if scene is None:
        raise SceneNotFound(str(scene_id))
    return scene


def update_scene(
    session: Session,
    scene_id: uuid.UUID,
    *,
    name: Optional[str] = None,
    order_index: Optional[int] = None,
) -> Scene:
    scene = get_scene(session, scene_id)
    if name is not None:
        scene.name = name
    if order_index is not None:
        scene.order_index = order_index
    session.add(scene)
    session.commit()
    session.refresh(scene)
    return scene


def delete_scene(session: Session, scene_id: uuid.UUID) -> None:
    """FK CASCADE handles Shots → Nodes → Edges. Plan rows hang off
    Shot.id and CASCADE through too."""
    scene = get_scene(session, scene_id)
    session.delete(scene)
    session.commit()


def scene_shot_count(session: Session, scene_id: uuid.UUID) -> int:
    n = session.exec(
        select(func.count(Shot.id)).where(Shot.scene_id == scene_id)
    ).one()
    if isinstance(n, tuple):
        n = n[0]
    return int(n or 0)


# ── Reorder ───────────────────────────────────────────────────────────────


def reorder_shots(
    session: Session, scene_id: uuid.UUID, shot_ids: list[uuid.UUID]
) -> list[Shot]:
    """Apply array order = new order_index.

    Validates ALL of:
      - scene exists
      - every shot in ``shot_ids`` belongs to this scene
      - the payload covers every shot in the scene exactly once
        (so the caller can't half-reorder and leave duplicates)
    """
    scene = get_scene(session, scene_id)
    current = list(
        session.exec(select(Shot).where(Shot.scene_id == scene.id)).all()
    )
    current_ids = {s.id for s in current}
    payload_ids = list(shot_ids)
    if set(payload_ids) != current_ids:
        raise ValueError(
            "reorder payload must list every shot in the scene exactly once"
        )
    if len(payload_ids) != len(set(payload_ids)):
        raise ValueError("reorder payload contains duplicate shot ids")
    by_id = {s.id: s for s in current}
    for new_idx, sid in enumerate(payload_ids):
        shot = by_id[sid]
        shot.order_index = new_idx
        session.add(shot)
    session.commit()
    return list(
        session.exec(
            select(Shot)
            .where(Shot.scene_id == scene.id)
            .order_by(Shot.order_index, Shot.created_at, Shot.id)
        ).all()
    )


# ── Establishing asset (was bundled with the removed Scene Bible) ──────────


def get_scene_establishing(session: Session, scene_id: uuid.UUID) -> dict[str, Any]:
    scene = get_scene(session, scene_id)
    media_id: Optional[str] = None
    if scene.master_establishing_asset_id is not None:
        asset = session.get(Asset, scene.master_establishing_asset_id)
        if asset is not None:
            media_id = asset.uuid_media_id
    return {
        "master_establishing_asset_id": scene.master_establishing_asset_id,
        # Read-only convenience: lets the frontend MasterShotNode populate
        # ``data.mediaId`` without a second roundtrip.
        "master_establishing_media_id": media_id,
    }


def put_scene_establishing(
    session: Session,
    scene_id: uuid.UUID,
    *,
    master_establishing_asset_id: Optional[int],
) -> dict[str, Any]:
    """Set the scene's master/establishing asset. If set, validates the asset
    belongs to the scene's project."""
    scene = get_scene(session, scene_id)
    if master_establishing_asset_id is not None:
        asset = session.get(Asset, master_establishing_asset_id)
        if asset is None:
            raise InvalidBibleAsset(
                f"asset {master_establishing_asset_id} not found"
            )
        if asset.project_id is not None and asset.project_id != scene.project_id:
            raise InvalidBibleAsset(
                f"asset {master_establishing_asset_id} belongs to a different project"
            )
    scene.master_establishing_asset_id = master_establishing_asset_id
    session.add(scene)
    session.commit()
    session.refresh(scene)
    return get_scene_establishing(session, scene.id)


# ── Phase 8.3: multi-shot canvas state + group metadata + auto-migration ───

# Default vertical-stack layout for auto-migration (group origin per shot).
_GROUP_STACK_X = 120.0
_GROUP_STACK_Y0 = 100.0
_GROUP_STACK_DY = 500.0


def _shots_ordered(session: Session, scene_id: uuid.UUID) -> list[Shot]:
    return list(
        session.exec(
            select(Shot)
            .where(Shot.scene_id == scene_id)
            .order_by(Shot.order_index, Shot.created_at, Shot.id)
        ).all()
    )


def get_scene_canvas(session: Session, scene_id: uuid.UUID) -> dict[str, Any]:
    """Return the full multi-shot canvas: shots + all nodes + all edges across
    the scene's shots + the persisted shot_groups layout (canvas_state)."""
    from flowboard.db.models import Edge, Node  # local import to avoid cycle

    scene = get_scene(session, scene_id)
    shots = _shots_ordered(session, scene_id)
    shot_ids = [sh.id for sh in shots]
    shot_id_strs = {str(sid) for sid in shot_ids}

    nodes: list[Node] = []
    edges: list[Edge] = []
    if shot_ids:
        nodes = list(session.exec(select(Node).where(Node.shot_id.in_(shot_ids))).all())  # type: ignore[attr-defined]
        edges = list(session.exec(select(Edge).where(Edge.shot_id.in_(shot_ids))).all())  # type: ignore[attr-defined]

    return {
        "scene_id": str(scene.id),
        "project_id": str(scene.project_id),
        "shots": [
            {
                "id": str(sh.id),
                "order_index": sh.order_index,
                "script_text": sh.script_text,
                "status": sh.status,
            }
            for sh in shots
        ],
        "nodes": [
            {
                "id": n.id,
                "shot_id": str(n.shot_id),
                "short_id": n.short_id,
                "type": n.type,
                "x": n.x,
                "y": n.y,
                "data": n.data,
                "status": n.status,
            }
            for n in nodes
        ],
        "edges": [
            {
                "id": e.id,
                "shot_id": str(e.shot_id),
                "source_id": e.source_id,
                "target_id": e.target_id,
                "kind": e.kind,
                "source_variant_idx": e.source_variant_idx,
            }
            for e in edges
        ],
        # Defensive: drop orphan group entries whose shot no longer exists
        # (e.g. legacy data, or a shot deleted out-of-band).
        "shot_groups": [
            g
            for g in (scene.canvas_state or {}).get("shot_groups", [])
            if isinstance(g, dict) and g.get("shot_id") in shot_id_strs
        ],
    }


def auto_migrate_canvas(session: Session, scene_id: uuid.UUID) -> dict[str, Any]:
    """Idempotently build canvas_state.shot_groups for a scene: one group per
    shot, default vertical-stack origin, collapsed=false, label "Shot N".

    Existing entries are preserved; only shots missing a group get one
    appended — so a re-run never clobbers user-moved groups (Q5 idempotent).
    """
    scene = get_scene(session, scene_id)
    state = dict(scene.canvas_state or {})
    groups: list[dict] = list(state.get("shot_groups") or [])
    have = {g.get("shot_id") for g in groups if isinstance(g, dict)}

    shots = _shots_ordered(session, scene_id)
    next_slot = len(groups)
    for sh in shots:
        sid = str(sh.id)
        if sid in have:
            continue
        groups.append({
            "shot_id": sid,
            "position": {"x": _GROUP_STACK_X, "y": _GROUP_STACK_Y0 + next_slot * _GROUP_STACK_DY},
            "collapsed": False,
            "label": f"Shot {sh.order_index + 1}",
            "order": sh.order_index,
        })
        next_slot += 1

    state["shot_groups"] = groups
    scene.canvas_state = state
    # Plain JSONB (no MutableDict) doesn't track nested mutations; force the
    # UPDATE so a re-run that only touches nested dicts still persists.
    flag_modified(scene, "canvas_state")
    session.add(scene)
    session.commit()
    session.refresh(scene)
    return {"scene_id": str(scene.id), "shot_groups": groups, "migrated": True}


def update_shot_group(
    session: Session,
    scene_id: uuid.UUID,
    shot_id: uuid.UUID,
    *,
    position: Optional[dict] = None,
    collapsed: Optional[bool] = None,
    label: Optional[str] = None,
    order: Optional[int] = None,
    size: Optional[dict] = None,
) -> dict[str, Any]:
    """Patch a single shot's group metadata in scene.canvas_state. Creates the
    group entry if it doesn't exist yet (e.g. a brand-new shot)."""
    scene = get_scene(session, scene_id)
    state = dict(scene.canvas_state or {})
    groups: list[dict] = list(state.get("shot_groups") or [])
    sid = str(shot_id)
    entry = next((g for g in groups if isinstance(g, dict) and g.get("shot_id") == sid), None)
    if entry is None:
        entry = {"shot_id": sid, "position": {"x": _GROUP_STACK_X, "y": _GROUP_STACK_Y0},
                 "collapsed": False, "label": "Shot", "order": len(groups)}
        groups.append(entry)
    if position is not None:
        entry["position"] = position
    if collapsed is not None:
        entry["collapsed"] = collapsed
    if label is not None:
        entry["label"] = label
    if order is not None:
        entry["order"] = order
    if size is not None:
        entry["size"] = size
    state["shot_groups"] = groups
    scene.canvas_state = state
    # See auto_migrate_canvas: force the JSONB UPDATE for nested mutations.
    flag_modified(scene, "canvas_state")
    session.add(scene)
    session.commit()
    return entry
