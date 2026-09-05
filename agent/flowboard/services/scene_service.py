"""Scene CRUD + bible + reorder + (Phase 7 stub) compose."""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, Optional

from sqlalchemy import func
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import Session, select

from flowboard.db.models import (
    AppSetting,
    Asset,
    Node,
    Project,
    Request,
    Scene,
    Series,
    Shot,
)


class SceneNotFound(Exception):
    pass


class ProjectNotFound(Exception):
    pass


class InvalidBibleAsset(Exception):
    """``master_establishing_asset_id`` doesn't belong to the scene's project."""


# ── Production-tracking metadata (Episode_Tracker spreadsheet columns) ──────
# Stored in Scene.production. `status` is the pipeline stage; the four names are
# role assignees (free text — no account required).
EPISODE_PROD_FIELDS: tuple[str, ...] = (
    "status",             # NotStarted | Script | Production | Completed | Dropped
    "scriptwriter",
    "concept_creator",
    "ai_creator",
    "editor",
    "duration_sec",
    "episode_link",
    "deadline",
    "complete_date",
)
_EPISODE_INT_FIELDS = {"duration_sec"}
_CREW_ROLES = ("scriptwriter", "concept_creator", "ai_creator", "editor")

# Persistent staff roster (seed of the Staff DB). Kept in app_setting so the
# crew pool survives even when no episodes exist yet (e.g. a fresh project).
STAFF_SETTING_KEY = "crm_staff_names"


def get_staff_names(session: Session) -> list[str]:
    row = session.get(AppSetting, STAFF_SETTING_KEY)
    if not row or not row.value:
        return []
    try:
        return [str(x).strip() for x in json.loads(row.value) if str(x).strip()]
    except (ValueError, TypeError):
        return []


def set_staff_names(session: Session, names: list[str]) -> list[str]:
    clean = sorted(
        {str(n).strip() for n in (names or []) if str(n).strip()},
        key=lambda s: s.lower(),
    )
    row = session.get(AppSetting, STAFF_SETTING_KEY)
    if row is None:
        row = AppSetting(key=STAFF_SETTING_KEY, value=json.dumps(clean, ensure_ascii=False))
    else:
        row.value = json.dumps(clean, ensure_ascii=False)
    session.add(row)
    session.commit()
    return clean


def distinct_crew_names(session: Session) -> list[str]:
    """The crew-dropdown pool: the persistent staff roster (app_setting) UNION
    every distinct crew name already used across episodes. Sorted. So the pool
    stays populated even in a fresh/empty project."""
    names: set[str] = set(get_staff_names(session))
    for sc in session.exec(select(Scene)).all():
        prod = sc.production or {}
        for role in _CREW_ROLES:
            v = prod.get(role)
            if isinstance(v, str) and v.strip():
                names.add(v.strip())
    return sorted(names, key=lambda s: s.lower())


# ── CRUD ──────────────────────────────────────────────────────────────────


def list_scenes(
    session: Session,
    project_id: uuid.UUID,
    *,
    series_id: Optional[uuid.UUID] = None,
) -> list[Scene]:
    project = session.get(Project, project_id)
    if project is None:
        raise ProjectNotFound(str(project_id))
    stmt = select(Scene).where(Scene.project_id == project_id)
    if series_id is not None:
        stmt = stmt.where(Scene.series_id == series_id)
    return list(
        session.exec(
            stmt.order_by(Scene.order_index, Scene.created_at, Scene.id)
        ).all()
    )


def _next_scene_order_index(
    session: Session, project_id: uuid.UUID, series_id: Optional[uuid.UUID] = None
) -> int:
    # Ordering is per-series once the tier exists, so Episode 1 of Season 2
    # doesn't inherit an index from Season 1.
    stmt = select(func.max(Scene.order_index)).where(Scene.project_id == project_id)
    if series_id is not None:
        stmt = stmt.where(Scene.series_id == series_id)
    last = session.exec(stmt).one()
    if isinstance(last, tuple):
        last = last[0]
    return int(last) + 1 if last is not None else 0


def _next_episode_code(session: Session, series_id: uuid.UUID) -> str:
    """``<SERIES_CODE>_EP<NN>`` for the next episode of this series.

    Numbered from the highest EP number already used rather than from the row
    count, so deleting an episode does not hand its code to the next one — the
    code is what the Episode_Tracker sheet is keyed on, and two episodes sharing
    it is two rows nobody can tell apart.
    """
    from flowboard.services import series_service

    series = session.get(Series, series_id)
    if series is None:
        return ""
    used = []
    for sc in session.exec(select(Scene).where(Scene.series_id == series_id)).all():
        m = re.search(r"EP(\d+)$", (sc.code or "").upper())
        if m:
            used.append(int(m.group(1)))
    return series_service.episode_code(series, (max(used) + 1) if used else 1)


def create_scene(
    session: Session,
    project_id: uuid.UUID,
    *,
    name: str,
    series_id: Optional[uuid.UUID] = None,
    code: str = "",
    order_index: Optional[int] = None,
) -> Scene:
    project = session.get(Project, project_id)
    if project is None:
        raise ProjectNotFound(str(project_id))
    if series_id is None:
        # Pre-Series callers (and a brand-new project) land in the project's
        # first series; create a "Default" one if there is none.
        from flowboard.services import series_service

        series_id = series_service.ensure_default_series(session, project_id).id
    if order_index is None:
        order_index = _next_scene_order_index(session, project_id, series_id)
    scene = Scene(
        project_id=project_id,
        series_id=series_id,
        name=name,
        # Built when the caller does not name one. The convention existed and
        # only the bulk scaffold used it, so an episode made the ordinary way —
        # which is how they are actually made — came out with no code at all,
        # and the Episode_Tracker column it mirrors stayed empty.
        code=code or _next_episode_code(session, series_id),
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
    series_id: Optional[uuid.UUID] = None,
    code: Optional[str] = None,
    order_index: Optional[int] = None,
    production: Optional[dict[str, Any]] = None,
) -> Scene:
    from flowboard.services import series_service as sers

    scene = get_scene(session, scene_id)
    if name is not None:
        scene.name = name
    if series_id is not None:
        scene.series_id = series_id
    if code is not None:
        scene.code = code
    if order_index is not None:
        scene.order_index = order_index
    if production is not None:
        # patch merged over the existing bag (unset keys untouched)
        scene.production = sers.merge_production(
            scene.production, production, EPISODE_PROD_FIELDS, _EPISODE_INT_FIELDS
        )
        flag_modified(scene, "production")
    session.add(scene)
    session.commit()
    session.refresh(scene)
    return scene


def set_scene_cover(
    session: Session, scene_id: uuid.UUID, media_id: Optional[str]
) -> Scene:
    """Set (or clear, when media_id is None) the scene's cover thumbnail.
    Stored in canvas_state.cover_media_id — cosmetic, not structural."""
    scene = get_scene(session, scene_id)
    state = dict(scene.canvas_state or {})
    if media_id:
        state["cover_media_id"] = str(media_id)
    else:
        state.pop("cover_media_id", None)
    scene.canvas_state = state
    flag_modified(scene, "canvas_state")
    session.add(scene)
    session.commit()
    session.refresh(scene)
    return scene


def scene_thumb_media_id(session: Session, scene: Scene) -> Optional[str]:
    """The media id to show on an episode card.

    A hand-set cover (``canvas_state.cover_media_id``) always wins — someone
    chose it deliberately. Otherwise fall back to the **first clip generated in
    the first sequence**, which is what the episode actually looks like and needs
    no upload step. The card renders it through ``/api/media/<id>/thumb``, and
    that route already extracts frame 0 with ffmpeg for video media — so nothing
    new decodes anything here; we only have to name the right file.

    "First sequence" means lowest ``Shot.order_index``, not the newest clip: the
    opening shot is the representative one, and ordering by time would make the
    cover jump every time someone re-rolled a later sequence.

    The source is ``Request``, not ``Asset``. Asset rows are a media *cache
    index* — for generated video they carry neither ``node_id`` nor
    ``project_id`` (verified: 514 video assets, 0 with either), so there is no
    path from an Asset back to the sequence that produced it. The Request row is
    what remembers the node, and it stores the result under ``media_id``
    (singular).

    Returns None when the episode has no generated video yet — the card then
    keeps its gradient placeholder.
    """
    override = (scene.canvas_state or {}).get("cover_media_id")
    if override:
        return str(override)

    # Only Request is selected — order_index is needed for ORDER BY, not in the
    # result set, and selecting it turns each row into a Row tuple to unpack.
    rows = session.exec(
        select(Request)
        .join(Node, Node.id == Request.node_id)
        .join(Shot, Shot.id == Node.shot_id)
        .where(
            Shot.scene_id == scene.id,
            Request.type == "gen_video",
            Request.status == "done",
        )
        .order_by(Shot.order_index, Request.id)
        .limit(40)
    ).all()

    first: Optional[str] = None
    for req in rows:
        mid = (req.result or {}).get("media_id")
        if not isinstance(mid, str) or not mid:
            continue
        if first is None:
            first = mid
        # Prefer one whose bytes are actually on disk: the thumb route has to
        # run ffmpeg over the file, and a media id whose cache was evicted would
        # render an empty card instead of a frame.
        from flowboard.services import media as _media

        if _media.cached_path(mid) is not None:
            return mid
    return first


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


# An episode used to carry a hard ceiling here — ceil(duration / sec_per_video)
# + 2 — enforced when a sequence was created. It is gone on purpose. A sequence
# is an empty container; deciding one beat needs two angles is the artist's job,
# and the ceiling that costs money is on TAKES, per sequence, in
# `sequence_quota`. The producer's duration and seconds-per-video are still
# recorded on the series: they say how long the episode should run, which is
# planning, not a rule anything refuses.


# ── Pipeline status ────────────────────────────────────────────────────────
# `production["status"]` is a dropdown a PM sets by hand, and it stayed on
# NotStarted for episodes people were plainly working in — the sheet said nothing
# had started while five takes had already run. Nobody updates a status field for
# work they are in the middle of doing.
#
# So: a human's answer is kept, and the absence of one is filled in from the work.
#
#   Completed / Dropped / Script / Production, once stored → returned untouched.
#     These are judgements. "Dropped" especially: an episode with work in it that
#     somebody decided to abandon must not creep back to Production because a
#     stray generation exists.
#   NotStarted (or blank) → DERIVED, because it is not a judgement. It is what a
#     row says before anyone has touched it, and once there is work in the episode
#     it is simply false.
#
# Derived, not written on the event. A status advanced by whoever handled the
# generation needs a write at every site that can start work — the canvas, the
# comic handover, whatever gets added next — and the one that gets forgotten is
# invisible, because a stale "NotStarted" looks exactly like a true one. Read from
# the work and it is right by construction, and it goes back down on its own when
# the work is deleted.

#: What the derived value can be. Anything else in the column came from a person.
DERIVED_STATUSES = ("NotStarted", "Script", "Production")


def effective_status_map(
    session: Session, scenes: "list[Scene]"
) -> dict[uuid.UUID, str]:
    """Pipeline status for several episodes at once.

    Batched deliberately: the CRM asks for every episode of a series and the
    per-status rollup asks for every episode of every series, so the per-scene
    version of this would be three queries per row on the busiest page there is.
    Four queries total, whatever the number of episodes.
    """
    out: dict[uuid.UUID, str] = {}
    need: list[uuid.UUID] = []
    for sc in scenes:
        stored = ((sc.production or {}).get("status") or "").strip()
        if stored and stored != "NotStarted":
            out[sc.id] = stored          # somebody decided; leave it alone
        else:
            need.append(sc.id)
    if not need:
        return out

    # Sequences: none at all means nothing has been broken down yet.
    shot_rows = session.exec(
        select(Shot.id, Shot.scene_id).where(Shot.scene_id.in_(need))  # type: ignore[attr-defined]
    ).all()
    scene_of_shot = {sid: scid for sid, scid in shot_rows}
    for sid in need:
        out[sid] = "NotStarted"
    for scid in scene_of_shot.values():
        out[scid] = "Script"
    if not scene_of_shot:
        return out

    # A generation that ran is the unambiguous signal that a person worked here —
    # it is an action somebody took and it cost money. Errors are excluded: a run
    # that failed upstream should not promote the episode on its own.
    node_rows = session.exec(
        select(Node.id, Node.shot_id).where(
            Node.shot_id.in_(list(scene_of_shot.keys()))  # type: ignore[attr-defined]
        )
    ).all()
    if not node_rows:
        return out
    scene_of_node = {
        nid: scene_of_shot[shid] for nid, shid in node_rows if shid in scene_of_shot
    }
    if not scene_of_node:
        return out
    ran = session.exec(
        select(Request.node_id).where(
            Request.node_id.in_(list(scene_of_node.keys())),  # type: ignore[attr-defined]
            Request.status != "error",
        )
    ).all()
    for nid in ran:
        scid = scene_of_node.get(nid if not isinstance(nid, tuple) else nid[0])
        if scid is not None:
            out[scid] = "Production"
    return out


def effective_status(session: Session, scene: Scene) -> str:
    return effective_status_map(session, [scene]).get(scene.id, "NotStarted")


def status_is_derived(scene: Scene) -> bool:
    """Whether the value shown was worked out rather than chosen — so the UI can
    say so instead of looking like somebody set it."""
    stored = ((scene.production or {}).get("status") or "").strip()
    return not stored or stored == "NotStarted"


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
#: Frame geometry, mirrored from routes/SceneCanvas.tsx. The CLIENT owns the
#: layout — it measures what actually rendered and re-flows the stack — and
#: these are not an attempt to take that over. They exist so the y's this
#: module hands out are not overlapping on their face, which the old flat
#: pitch of 500 always was: less than a single frame is tall.
#:
#: That went unnoticed for as long as the client re-flowed on every load, so
#: the bad seed was visible for one frame. It stopped being invisible when
#: scenes began gaining sequences after that re-flow had already run.
_GROUP_DEFAULT_H = 1080.0
_GROUP_COLLAPSED_H = 110.0
_GROUP_GAP = 100.0
_GROUP_STACK_DY = _GROUP_DEFAULT_H + _GROUP_GAP


def _implied_height(g: dict) -> float:
    """How tall this frame renders, by the same rules the canvas uses.

    Collapsed frames are short and resized ones are whatever the user made
    them, so a stack of those is legitimately tight — which is why "the pitch
    must be at least a default frame" is the wrong test and this is the right
    one.
    """
    if g.get("collapsed"):
        return _GROUP_COLLAPSED_H
    size = g.get("size")
    if isinstance(size, dict) and isinstance(size.get("h"), (int, float)):
        return float(size["h"])
    return _GROUP_DEFAULT_H


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
        # Reconciled on read, so a stale layout can never be served. The client
        # only asks to migrate when it sees NO groups at all, which meant an
        # episode that gained sequences after its first load kept the layout it
        # was born with — wrong order, and groups overlapping where two claimed
        # one slot. Reading is the one path every viewer takes.
        "shot_groups": _reconciled_groups(session, scene, shots),
    }


def _reconciled_groups(session: Session, scene: Scene, shots: list[Shot]) -> list[dict]:
    """One group per shot, in the shots' order, persisted only if it changed.

    Orphans — groups whose shot is gone — are dropped rather than filtered on
    each read: nothing else would ever clean them up, and a frame for a sequence
    that no longer exists is not a display problem, it is a wrong row.
    """
    wanted = _group_layout(scene, shots)
    current = (scene.canvas_state or {}).get("shot_groups") or []
    if current != wanted:
        state = dict(scene.canvas_state or {})
        state["shot_groups"] = wanted
        scene.canvas_state = state
        flag_modified(scene, "canvas_state")
        session.add(scene)
        session.commit()
    return wanted


def _group_layout(scene: Scene, shots: list[Shot]) -> list[dict]:
    """The layout the scene's shots imply, keeping whatever the user chose that
    the shots do not decide (a group's x, whether it is collapsed)."""
    existing = {
        g.get("shot_id"): g
        for g in ((scene.canvas_state or {}).get("shot_groups") or [])
        if isinstance(g, dict)
    }
    # Where the kept groups already sit, so a new one is never dropped on top of
    # one. Seeding by slot cannot work here: the kept y's follow whatever the
    # slots WERE, and inserting a sequence renumbers them.
    taken = [
        g["position"]["y"]
        for g in existing.values()
        if isinstance(g.get("position"), dict) and isinstance(g["position"].get("y"), (int, float))
    ]
    floor = max(taken) if taken else _GROUP_STACK_Y0 - _GROUP_STACK_DY

    def _next_y() -> float:
        """Below everything, so it cannot land on a group already there. Which
        is not where it belongs — the client re-flows the stack by `order` on
        load and puts it in place. A seed only has to be somewhere legible."""
        nonlocal floor
        floor += _GROUP_STACK_DY
        return floor

    out: list[dict] = []
    for slot, sh in enumerate(shots):
        sid = str(sh.id)
        g = dict(existing.get(sid) or {})
        pos = g.get("position") if isinstance(g.get("position"), dict) else {}
        out.append({
            "shot_id": sid,
            # `order` and `label` are a VIEW of the shot, refreshed every time.
            # A stale copy of a number that already exists somewhere is not
            # worth keeping — and it is what put the canvas in the wrong order.
            "order": sh.order_index,
            "label": f"Sequence {sh.order_index + 1}",
            "collapsed": bool(g.get("collapsed", False)),
            # A position the user chose is kept, always. Only a group that did
            # not exist yet gets seeded, and it is seeded at its own slot rather
            # than at the end of the array — appended-at-the-end was what put a
            # new sequence on top of one already there.
            #
            # The seed is only a first frame: the client re-flows the whole
            # stack by `order` from the real rendered heights on load, which is
            # what actually removes the overlap. That re-flow was reading a
            # stale `order` before, which is why it never did.
            "position": {
                "x": pos.get("x", _GROUP_STACK_X),
                "y": pos.get("y", _next_y()),
            },
            # Preserve fields the shot doesn't decide but the user/UI set: a
            # manual resize (`size`) and the sequence kind ("upscale"). Without
            # these the reconcile strips them — shrinking a hand-resized frame
            # and turning an Upscale sequence back into a normal one.
            **({"size": g["size"]} if isinstance(g.get("size"), dict) else {}),
            **({"kind": g["kind"]} if isinstance(g.get("kind"), str) and g.get("kind") else {}),
        })

    # A stored layout where a frame sits on the one before it is not somebody's
    # preference — it is this bug. These y's were written when a group's
    # position followed the order it was ADDED in, so a scene that gained
    # sequences later ended up with Sequence 1 below Sequence 2, and later
    # still with an even pitch of 500 between frames that are 1080 tall.
    # Nothing repaired either, because each individual y looked like a choice.
    #
    # Keep them while each frame clears the one above it — that is a real
    # arrangement, spacing and all, and the one the moved-group case relies on.
    # Re-flow the whole stack the moment one does not, using each frame's own
    # height so a stack of collapsed or hand-resized frames stays as tight as
    # its owner made it.
    # FREE POSITIONING: keep each frame exactly where the user placed it — do
    # NOT auto-reflow into a vertical stack when frames "don't clear". The old
    # reflow fought manual placement: drag a sequence aside, then any reload
    # (e.g. after uploading a ref into it) snapped it back into the column. New
    # groups are still seeded at a free y above (via _next_y); existing ones are
    # left untouched.
    return out


def auto_migrate_canvas(session: Session, scene_id: uuid.UUID) -> dict[str, Any]:
    """Reconcile canvas_state.shot_groups with the scene's shots.

    Kept as an endpoint because the client still calls it on a scene it finds
    with no groups, but it does nothing reading the canvas does not already do —
    delegating rather than repeating the layout means the two cannot disagree
    about what the layout is, which is exactly how the stale-`order` bug got in.
    """
    scene = get_scene(session, scene_id)
    groups = _reconciled_groups(session, scene, _shots_ordered(session, scene_id))
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
    kind: Optional[str] = None,
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
    if kind is not None:
        entry["kind"] = kind
    state["shot_groups"] = groups
    scene.canvas_state = state
    # See auto_migrate_canvas: force the JSONB UPDATE for nested mutations.
    flag_modified(scene, "canvas_state")
    session.add(scene)
    session.commit()
    return entry
