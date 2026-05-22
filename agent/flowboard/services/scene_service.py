"""Scene CRUD + bible + reorder + (Phase 7 stub) compose."""
from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy import func
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
    scene_bible_text: str = "",
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
        scene_bible_text=scene_bible_text,
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
    scene_bible_text: Optional[str] = None,
) -> Scene:
    scene = get_scene(session, scene_id)
    if name is not None:
        scene.name = name
    if order_index is not None:
        scene.order_index = order_index
    if scene_bible_text is not None:
        scene.scene_bible_text = scene_bible_text
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


# ── Bible ─────────────────────────────────────────────────────────────────


def get_scene_bible(session: Session, scene_id: uuid.UUID) -> dict[str, Any]:
    scene = get_scene(session, scene_id)
    media_id: Optional[str] = None
    if scene.master_establishing_asset_id is not None:
        asset = session.get(Asset, scene.master_establishing_asset_id)
        if asset is not None:
            media_id = asset.uuid_media_id
    return {
        "scene_bible_text": scene.scene_bible_text,
        "master_establishing_asset_id": scene.master_establishing_asset_id,
        # Read-only convenience: lets the frontend MasterShotNode populate
        # ``data.mediaId`` without a second roundtrip. PUT body still
        # accepts only ``master_establishing_asset_id``.
        "master_establishing_media_id": media_id,
    }


def put_scene_bible(
    session: Session,
    scene_id: uuid.UUID,
    *,
    scene_bible_text: str,
    master_establishing_asset_id: Optional[int],
) -> dict[str, Any]:
    """Replace bible fields. If ``master_establishing_asset_id`` is set,
    validates the asset belongs to the scene's project.
    """
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
    scene.scene_bible_text = scene_bible_text
    scene.master_establishing_asset_id = master_establishing_asset_id
    session.add(scene)
    session.commit()
    session.refresh(scene)
    return get_scene_bible(session, scene.id)
