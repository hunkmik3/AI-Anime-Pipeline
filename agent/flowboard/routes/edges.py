import uuid
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from flowboard.db import get_session
from flowboard.db.models import Edge, Node
from flowboard.routes.deps import get_optional_user
from flowboard.services import resource_guard

router = APIRouter(prefix="/api/edges", tags=["edges"])

EdgeKind = Literal["ref", "hint"]


class EdgeCreate(BaseModel):
    shot_id: uuid.UUID
    source_id: int
    target_id: int
    kind: EdgeKind = "ref"
    # Optional pin to a specific variant of the source's `mediaIds[]`.
    # Frontend passes when the user picks a variant before drawing the
    # edge (or when right-click → pin variant on an existing edge).
    source_variant_idx: Optional[int] = None


class EdgePatch(BaseModel):
    """Partial update — currently only the variant pin is mutable;
    swapping source/target is a delete + create."""
    source_variant_idx: Optional[int] = None


@router.post("")
def create_edge(body: EdgeCreate, user=Depends(get_optional_user)):
    with get_session() as s:
        # Authorize the sequence the body names first; the checks below then pin
        # both endpoints to that same shot, so authorizing it covers all three.
        resource_guard.authorize_shot(s, user, body.shot_id, "canvas.write")
        if body.source_id == body.target_id:
            raise HTTPException(400, "source_id and target_id must differ")
        source = s.get(Node, body.source_id)
        target = s.get(Node, body.target_id)
        if not source or not target:
            raise HTTPException(404, "source or target node not found")
        if source.shot_id != body.shot_id or target.shot_id != body.shot_id:
            raise HTTPException(400, "nodes must belong to the same shot")
        edge = Edge(
            shot_id=body.shot_id,
            source_id=body.source_id,
            target_id=body.target_id,
            kind=body.kind,
            source_variant_idx=body.source_variant_idx,
        )
        s.add(edge)
        s.commit()
        s.refresh(edge)
        return edge


@router.patch("/{edge_id}")
def patch_edge(edge_id: int, body: EdgePatch, user=Depends(get_optional_user)):
    """Update an edge's variant pin without recreating the edge.

    Used by the variant-click flow: user picks a variant on an upstream
    multi-variant node → we PATCH the existing edge to that downstream
    so the next Generate uses the chosen ref. Passing
    ``source_variant_idx: null`` clears the pin (revert to mediaId).
    """
    with get_session() as s:
        # Edge ids are sequential integers — repointing another team's ref edge
        # silently changes which image their next Generate feeds on.
        edge = resource_guard.authorize_edge(s, user, edge_id, "canvas.write")
        if "source_variant_idx" in body.model_fields_set:
            edge.source_variant_idx = body.source_variant_idx
        s.add(edge)
        s.commit()
        s.refresh(edge)
        return edge


@router.delete("/{edge_id}")
def delete_edge(edge_id: int, user=Depends(get_optional_user)):
    with get_session() as s:
        edge = resource_guard.authorize_edge(s, user, edge_id, "canvas.write")
        s.delete(edge)
        s.commit()
        return {"ok": True}
