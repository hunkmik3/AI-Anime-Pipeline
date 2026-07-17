import uuid
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import select

from flowboard.db import get_session
from flowboard.db.models import Edge, Node, Project, Scene, Shot
from flowboard.routes.deps import get_optional_user, owner_scope
from flowboard.services import stats_service
from flowboard.short_id import generate_unique_short_id

router = APIRouter(prefix="/api/nodes", tags=["nodes"])

NodeType = Literal[
    "character",
    "image",
    "video",
    "prompt",
    "note",
    "visual_asset",
    # Phase 4: storyboard renamed to lowercase to match the other type names.
    # Pre-Phase-4 the frontend sent "Storyboard" and the Pydantic Literal
    # here didn't include it (silent Q1 bug — POST would 422); fixed by
    # adding it to the canonical set with consistent casing.
    "storyboard",
    # Phase 4 anime nodes — see docs/flowboard_modification_plan.md §4.3.
    "script",
    "bible_ref",
    "master_shot",
    "approval_gate",
    # Phase 7: audio reference for Seedance 2.0 r2v+audio.
    "audio_ref",
    # Reference video for Seedance 2.0 r2v (hoisted to a public R2 URL).
    "video_ref",
]
NodeStatus = Literal["idle", "queued", "running", "done", "error", "partial"]

_COORD_MIN = -1_000_000.0
_COORD_MAX = 1_000_000.0
_SIZE_MAX = 100_000.0


class NodeCreate(BaseModel):
    shot_id: uuid.UUID
    type: NodeType
    x: float = Field(default=0.0, ge=_COORD_MIN, le=_COORD_MAX)
    y: float = Field(default=0.0, ge=_COORD_MIN, le=_COORD_MAX)
    w: float = Field(default=240.0, gt=0, le=_SIZE_MAX)
    h: float = Field(default=160.0, gt=0, le=_SIZE_MAX)
    data: dict = {}
    status: NodeStatus = "idle"


class NodeUpdate(BaseModel):
    x: Optional[float] = Field(default=None, ge=_COORD_MIN, le=_COORD_MAX)
    y: Optional[float] = Field(default=None, ge=_COORD_MIN, le=_COORD_MAX)
    w: Optional[float] = Field(default=None, gt=0, le=_SIZE_MAX)
    h: Optional[float] = Field(default=None, gt=0, le=_SIZE_MAX)
    data: Optional[dict] = None
    status: Optional[NodeStatus] = None


@router.post("")
def create_node(body: NodeCreate):
    with get_session() as s:
        if not s.get(Shot, body.shot_id):
            raise HTTPException(404, "shot not found")
        short_id = generate_unique_short_id(s, body.shot_id)
        node = Node(
            shot_id=body.shot_id,
            short_id=short_id,
            type=body.type,
            x=body.x,
            y=body.y,
            w=body.w,
            h=body.h,
            data=body.data,
            status=body.status,
        )
        s.add(node)
        s.commit()
        s.refresh(node)
        return node


@router.patch("/{node_id}")
def update_node(node_id: int, body: NodeUpdate):
    """Partial update.

    The `data` field is **shallow-merged** into the existing JSON
    column rather than wholesale-replaced — earlier behavior dropped
    any sibling field the caller forgot to list, which silently erased
    `aspectRatio`, `aiBrief`, and other state every time the frontend
    sent a partial update. Merge is the natural REST PATCH semantic
    and prevents that whole class of regression.

    Merge depth is **one level** — patch keys at the top level of
    `data` are merged with existing keys, but if a key's value is
    itself a dict, the new dict REPLACES the old one (no recursive
    merge). All current FlowboardNodeData fields are scalars / arrays,
    so this matches the schema. If a future field needs nested-merge
    semantics, switch to a recursive walker here and update this
    docstring.

    Sentinel: a value of `null` in the data patch deletes the key. So
    callers that want to clear `aiBrief` after a regen pass
    `{aiBrief: null}` (still merge-safe — no risk of accidentally
    nuking unrelated fields). Missing keys are preserved.

    Non-`data` fields (`x`, `y`, `w`, `h`, `status`) keep the original
    setattr-replace semantic — no merge applied.
    """
    with get_session() as s:
        node = s.get(Node, node_id)
        if not node:
            raise HTTPException(404, "node not found")
        patch = body.model_dump(exclude_unset=True)
        for k, v in patch.items():
            if k == "data" and isinstance(v, dict):
                merged = dict(node.data or {})
                for dk, dv in v.items():
                    if dv is None:
                        merged.pop(dk, None)
                    else:
                        merged[dk] = dv
                node.data = merged
            else:
                setattr(node, k, v)
        s.add(node)
        s.commit()
        s.refresh(node)
        return node


@router.delete("/{node_id}")
def delete_node(node_id: int):
    with get_session() as s:
        node = s.get(Node, node_id)
        if not node:
            raise HTTPException(404, "node not found")
        edges = s.exec(
            select(Edge).where((Edge.source_id == node_id) | (Edge.target_id == node_id))
        ).all()
        for e in edges:
            s.delete(e)
        s.delete(node)
        s.commit()
        return {"ok": True, "deleted_edges": [e.id for e in edges]}


@router.get("/{node_id}/history")
def node_history(node_id: int, user=Depends(get_optional_user)):
    """Every generation ever run on this node — newest first.

    Owner-gated by walking node → shot → scene → project, because this exposes
    what each attempt cost. A caller who can't see the project gets 404 rather
    than 403, matching the rest of the structure (ids must not leak).
    """
    with get_session() as s:
        node = s.get(Node, node_id)
        if node is None:
            raise HTTPException(404, "node not found")
        scope = owner_scope(user)
        if scope is not None:
            # Resolve the owning project; a node with no shot (scene-level or
            # orphaned) has nothing to gate on, so it stays admin/no-auth only.
            shot = s.get(Shot, node.shot_id) if node.shot_id else None
            scene = s.get(Scene, shot.scene_id) if shot else None
            project = s.get(Project, scene.project_id) if scene else None
            if project is None or project.owner_user_id != scope:
                raise HTTPException(404, "node not found")
    return stats_service.node_history(node_id)
