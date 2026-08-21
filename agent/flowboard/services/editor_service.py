"""The raw material of a series, for the person who cuts it together.

An editor takes every clip the artists generated, assembles the episode in their
own software, and brings one file back. This module answers the first half: what
is there, and under what name.

**The name is the feature.** These files leave the app and live in somebody's NLE
bin for a week — `SQ03_v2.mp4` in a folder of forty is unusable, and a bare media
id is worse. Each clip is named `<EPISODE>_<SEQUENCE>_v<take>.mp4`, which is the
same vocabulary the editor uses when they come back and say which sequence is
wrong. The name is the shared reference between the two halves of the workflow.

**The latest take only, by default.** A sequence may have five attempts; four of
them are things the artist rejected. Handing all five over makes the editor do the
artist's triage. `all_takes` exists for the case where the artist wants a second
opinion on which one to use.
"""
from __future__ import annotations

import uuid
from typing import Optional

from sqlmodel import Session, select

from flowboard.db.models import Node, Request, Scene, Series, Shot


def _clip_media(req: Request) -> list[str]:
    """Media ids a generation produced, in the order it produced them."""
    result = req.result or {}
    out = [m for m in (result.get("media_ids") or []) if isinstance(m, str)]
    if not out:
        one = result.get("media_id")
        if isinstance(one, str):
            out = [one]
    return out


def materials(
    session: Session, series_id: uuid.UUID, *, all_takes: bool = False
) -> dict:
    """Every generated clip in a series, grouped episode → sequence.

    Ordered the way the episode plays — episodes by their order, sequences by
    theirs — because an editor pulling forty clips is about to lay them on a
    timeline in exactly that order, and a list sorted by anything else makes them
    do the sorting twice.
    """
    series = session.get(Series, series_id)
    if series is None:
        return {"series_id": str(series_id), "episodes": [], "clip_count": 0}

    scenes = list(
        session.exec(
            select(Scene)
            .where(Scene.series_id == series_id)
            .order_by(Scene.order_index, Scene.created_at)
        ).all()
    )
    if not scenes:
        return {"series_id": str(series_id), "name": series.name, "episodes": [],
                "clip_count": 0}

    shots = list(
        session.exec(
            select(Shot)
            .where(Shot.scene_id.in_([sc.id for sc in scenes]))  # type: ignore[attr-defined]
            .order_by(Shot.order_index, Shot.created_at)
        ).all()
    )
    nodes = list(
        session.exec(
            select(Node).where(Node.shot_id.in_([sh.id for sh in shots]))  # type: ignore[attr-defined]
        ).all()
    ) if shots else []
    node_shot = {n.id: n.shot_id for n in nodes}
    reqs = list(
        session.exec(
            select(Request)
            .where(
                Request.node_id.in_(list(node_shot)),  # type: ignore[attr-defined]
                Request.type == "gen_video",
            )
            .order_by(Request.created_at, Request.id)
        ).all()
    ) if node_shot else []

    # take number counts per SEQUENCE, not per node: an artist who adds a second
    # node to the same sequence is still on their third attempt at that shot, and
    # numbering per node would produce two files both called v1.
    by_shot: dict[uuid.UUID, list[dict]] = {}
    for req in reqs:
        if req.status == "error":
            continue
        shot_id = node_shot.get(req.node_id)
        if shot_id is None:
            continue
        media = _clip_media(req)
        if not media:
            continue
        takes = by_shot.setdefault(shot_id, [])
        for mid in media:
            takes.append({
                "media_id": mid,
                "take": len(takes) + 1,
                "request_id": req.id,
                "created_at": req.created_at.isoformat() if req.created_at else None,
                "duration_seconds": (req.params or {}).get("duration_seconds"),
                "resolution": (req.params or {}).get("resolution"),
                # Aspect the clip was generated at (e.g. "9:16"). A hint the UI
                # uses to size the inline player before the file's own metadata
                # loads; the file's real dimensions win once they arrive.
                "aspect_ratio": (req.params or {}).get("aspect_ratio"),
            })

    shots_by_scene: dict[uuid.UUID, list[Shot]] = {}
    for sh in shots:
        shots_by_scene.setdefault(sh.scene_id, []).append(sh)

    episodes, total = [], 0
    for sc in scenes:
        seqs = []
        for sh in shots_by_scene.get(sc.id, []):
            takes = by_shot.get(sh.id, [])
            if not takes:
                continue
            picked = takes if all_takes else [takes[-1]]
            for t in picked:
                t["filename"] = clip_name(sc, sh, t["take"])
            total += len(picked)
            seqs.append({
                "shot_id": str(sh.id),
                "code": sh.code or "",
                "take_count": len(takes),
                "clips": picked,
            })
        episodes.append({
            "scene_id": str(sc.id),
            "code": sc.code or "",
            "name": sc.name,
            "sequence_count": len(seqs),
            "sequences": seqs,
        })

    return {
        "series_id": str(series_id),
        "name": series.name,
        "code": series.code or "",
        "episodes": episodes,
        "clip_count": total,
        "all_takes": all_takes,
    }


def clip_name(scene: Scene, shot: Shot, take: int) -> str:
    """`DUON_EP01_SQ03_v2.mp4` — what the file is called once it leaves the app.

    Built from the codes people already say out loud, so the name an editor sees
    in their bin is the name they use when they report which sequence is wrong.
    Falls back to the episode's name when it has no code, because a file called
    `_SQ03_v2` tells nobody anything.
    """
    ep = (scene.code or scene.name or "EP").strip().replace(" ", "-")
    sq = (shot.code or "SQ").strip().replace(" ", "-")
    # The sequence code already carries the episode stem when it was generated
    # (`DUON_EP01_SQ03`), so repeating it would give `DUON_EP01_DUON_EP01_SQ03`.
    stem = sq if sq.startswith(ep) else f"{ep}_{sq}"
    return f"{stem}_v{take}.mp4"


def flat_clips(data: dict) -> list[tuple[str, str]]:
    """`(filename, media_id)` for every clip in a `materials()` result, in play
    order — what the zip is built from."""
    out: list[tuple[str, str]] = []
    for ep in data.get("episodes", []):
        for sq in ep.get("sequences", []):
            for clip in sq.get("clips", []):
                out.append((clip["filename"], clip["media_id"]))
    return out
