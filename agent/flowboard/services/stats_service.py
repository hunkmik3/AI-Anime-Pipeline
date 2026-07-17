"""Cost / waste analytics — where the money actually went.

The central question: **how much money produced a clip that got kept, and how
much burned on takes that were re-rolled away?**

Signals used (none of them require the user to click anything extra, so none
can be gamed — the spend is already on the bill):

- Every paid generation is a settled ``UsageRecord`` carrying the REAL Avis
  ``usdCost``. Technically-failed gens are released (refunded) → they cost $0
  and are excluded.
- Generations are grouped by the **video/image node** they ran on. On a node,
  the LAST paid take is the one the user settled on ("stopped re-rolling");
  every earlier take was thrown away → **wasted**.
- A **DownloadEvent** on the node confirms the kept take was actually taken
  away and used. It's a confirmation signal, not the basis of the maths, so a
  user can't inflate their numbers by downloading everything.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Optional

from sqlmodel import select

from flowboard.db import get_session
from flowboard.db.models import (
    DownloadEvent,
    Node,
    Project,
    Request,
    Scene,
    Shot,
    UsageRecord,
    User,
)

logger = logging.getLogger(__name__)


def _load(session):
    """One pass over the tables the stats need (small tables; keeps it simple)."""
    users = {u.id: u for u in session.exec(select(User)).all()}
    reqs = {r.id: r for r in session.exec(select(Request)).all()}
    nodes = {n.id: n for n in session.exec(select(Node)).all()}
    shots = {s.id: s for s in session.exec(select(Shot)).all()}
    scenes = {s.id: s for s in session.exec(select(Scene)).all()}
    projects = {p.id: p for p in session.exec(select(Project)).all()}
    # paid generations only (settled = Avis actually billed us)
    paid = [
        r
        for r in session.exec(select(UsageRecord).where(UsageRecord.status == "settled")).all()
    ]
    downloaded_nodes = {
        d.node_id for d in session.exec(select(DownloadEvent)).all() if d.node_id is not None
    }
    return users, reqs, nodes, shots, scenes, projects, paid, downloaded_nodes


def _group_by_node(paid, reqs):
    """(node_id → [UsageRecord…] oldest-first), plus records with no node."""
    by_node: dict[int, list] = defaultdict(list)
    orphans: list = []
    for r in paid:
        req = reqs.get(r.request_id)
        nid = req.node_id if req else None
        if nid is None:
            orphans.append(r)
        else:
            by_node[nid].append(r)
    for recs in by_node.values():
        recs.sort(key=lambda r: (r.created_at is None, r.created_at))
    return by_node, orphans


def _split(recs) -> tuple[float, float, int]:
    """(kept_usd, wasted_usd, takes) for one node — last take kept, rest wasted."""
    costs = [float(r.actual_usd or 0.0) for r in recs]
    if not costs:
        return 0.0, 0.0, 0
    return costs[-1], sum(costs[:-1]), len(costs)


def _project_of(node, shots, scenes, projects):
    sh = shots.get(node.shot_id) if node is not None else None
    sc = scenes.get(sh.scene_id) if sh else None
    return (projects.get(sc.project_id) if sc else None), sc


def _shot_label(shot, scenes) -> str:
    """Friendly shot name: the SceneCanvas group label if set, else 'Shot N'."""
    sc = scenes.get(shot.scene_id) if shot else None
    if sc:
        for g in (sc.canvas_state or {}).get("shot_groups", []):
            if str(g.get("shot_id")) == str(shot.id) and g.get("label"):
                return str(g["label"])
    return f"Sequence {(shot.order_index + 1) if shot else '?'}"


# ── per shot (admin oversight: what each shot spent + every gen) ──────────────


def project_shots(project_id) -> list[dict]:
    """Per-shot cost within a project. Every shot is listed (even $0 ones) so the
    admin sees the full picture; each shot aggregates all its nodes' takes."""
    with get_session() as s:
        _u, reqs, nodes, shots, scenes, _p, paid, dl_nodes = _load(s)
    by_node, _ = _group_by_node(paid, reqs)

    agg: dict = defaultdict(
        lambda: {"kept": 0.0, "wasted": 0.0, "takes": 0, "clips": 0, "dl": 0}
    )
    for nid, recs in by_node.items():
        n = nodes.get(nid)
        if n is None or n.shot_id is None:
            continue
        sh = shots.get(n.shot_id)
        sc = scenes.get(sh.scene_id) if sh else None
        if sc is None or str(sc.project_id) != str(project_id):
            continue
        kept, wasted, takes = _split(recs)
        a = agg[sh.id]
        a["kept"] += kept
        a["wasted"] += wasted
        a["takes"] += takes
        a["clips"] += 1
        if nid in dl_nodes:
            a["dl"] += 1

    out = []
    for sh in shots.values():
        sc = scenes.get(sh.scene_id)
        if sc is None or str(sc.project_id) != str(project_id):
            continue
        a = agg.get(sh.id, {"kept": 0.0, "wasted": 0.0, "takes": 0, "clips": 0, "dl": 0})
        total = a["kept"] + a["wasted"]
        out.append(
            {
                "shot_id": str(sh.id),
                "shot_label": _shot_label(sh, scenes),
                "order_index": sh.order_index,
                "scene_id": str(sc.id),
                "scene_name": sc.name,
                "scene_order": sc.order_index,
                "total_usd": round(total, 4),
                "kept_usd": round(a["kept"], 4),
                "wasted_usd": round(a["wasted"], 4),
                "clips": a["clips"],
                "takes": a["takes"],
                "downloaded_clips": a["dl"],
                "waste_pct": round((a["wasted"] / total * 100) if total else 0.0, 1),
            }
        )
    out.sort(key=lambda r: (r["scene_order"], r["order_index"]))
    return out


def all_shots() -> list[dict]:
    """Flat list of every shot that spent money, across all projects, sorted by
    total spend (priciest first). Total-only — no kept/wasted split. This is the
    'how much did each shot cost' oversight view."""
    with get_session() as s:
        _u, reqs, nodes, shots, scenes, projects, paid, _d = _load(s)

    agg: dict = defaultdict(lambda: {"usd": 0.0, "gens": 0, "nodes": set()})
    for r in paid:
        req = reqs.get(r.request_id)
        n = nodes.get(req.node_id) if (req and req.node_id) else None
        if n is None or n.shot_id is None:
            continue
        sh = shots.get(n.shot_id)
        if sh is None:
            continue
        a = agg[sh.id]
        a["usd"] += float(r.actual_usd or 0.0)
        a["gens"] += 1
        a["nodes"].add(n.id)

    out = []
    for shid, a in agg.items():
        sh = shots.get(shid)
        sc = scenes.get(sh.scene_id) if sh else None
        pr = projects.get(sc.project_id) if sc else None
        out.append(
            {
                "shot_id": str(shid),
                "shot_label": _shot_label(sh, scenes) if sh else "?",
                "scene_name": sc.name if sc else None,
                "project_id": str(pr.id) if pr else None,
                "project_name": pr.name if pr else "(no project)",
                "total_usd": round(a["usd"], 4),
                "gens": a["gens"],
                "clips": len(a["nodes"]),
            }
        )
    out.sort(key=lambda r: r["total_usd"], reverse=True)
    return out


def cost_tree() -> list[dict]:
    """Nested spend breakdown for the admin 'Cost' tab: project → episode
    (scene) → sequence (shot), each carrying its own rolled-up total. Only
    branches that actually spent money appear. Projects are ordered by spend
    (biggest first); episodes and sequences keep their natural story order."""
    with get_session() as s:
        _u, reqs, nodes, shots, scenes, projects, paid, _d = _load(s)

    # settled spend + gen/clip counts per shot (a clip == one node)
    shot_agg: dict = defaultdict(lambda: {"usd": 0.0, "gens": 0, "nodes": set()})
    for r in paid:
        req = reqs.get(r.request_id)
        n = nodes.get(req.node_id) if (req and req.node_id) else None
        if n is None or n.shot_id is None:
            continue
        sh = shots.get(n.shot_id)
        if sh is None:
            continue
        a = shot_agg[sh.id]
        a["usd"] += float(r.actual_usd or 0.0)
        a["gens"] += 1
        a["nodes"].add(n.id)

    # fold each spending shot up into project → episode buckets
    proj_map: dict = {}
    for shid, a in shot_agg.items():
        if a["usd"] <= 0:
            continue
        sh = shots.get(shid)
        sc = scenes.get(sh.scene_id) if sh else None
        pr = projects.get(sc.project_id) if sc else None
        pid = str(pr.id) if pr else "—"
        scid = str(sc.id) if sc else "—"
        p = proj_map.setdefault(
            pid,
            {"project_id": pid, "name": pr.name if pr else "(no project)",
             "total_usd": 0.0, "gens": 0, "clips": 0, "_ep": {}},
        )
        e = p["_ep"].setdefault(
            scid,
            {"scene_id": scid, "name": sc.name if sc else "(no episode)",
             "_order": sc.order_index if sc else 0,
             "total_usd": 0.0, "gens": 0, "clips": 0, "sequences": []},
        )
        clips = len(a["nodes"])
        e["sequences"].append(
            {
                "shot_id": str(shid),
                "shot_label": _shot_label(sh, scenes) if sh else "?",
                "_order": sh.order_index if sh else 0,
                "total_usd": round(a["usd"], 4),
                "gens": a["gens"],
                "clips": clips,
            }
        )
        for bucket in (e, p):
            bucket["total_usd"] += a["usd"]
            bucket["gens"] += a["gens"]
            bucket["clips"] += clips

    out = []
    for p in proj_map.values():
        episodes = list(p["_ep"].values())
        for e in episodes:
            e["sequences"].sort(key=lambda x: x["_order"])
            for seq in e["sequences"]:
                seq.pop("_order", None)
            e["total_usd"] = round(e["total_usd"], 4)
        episodes.sort(key=lambda e: e["_order"])
        for e in episodes:
            e.pop("_order", None)
        out.append(
            {
                "project_id": p["project_id"],
                "name": p["name"],
                "total_usd": round(p["total_usd"], 4),
                "gens": p["gens"],
                "clips": p["clips"],
                "episodes": episodes,
            }
        )
    out.sort(key=lambda r: r["total_usd"], reverse=True)
    return out


def node_history(node_id) -> list[dict]:
    """Every generation ever run on ONE node — newest first.

    The per-node "what did I already try, and what did each attempt cost?"
    view. Unlike ``shot_gens`` (admin oversight, settled money only) this shows
    the whole story: failed and in-flight attempts too, since "it errored" is
    exactly what you're looking for when you re-open the history.

    ``kept`` marks the take whose output the node is currently showing; a
    ``cost_usd`` of None means the attempt was never billed (failed → released,
    or still running).
    """
    with get_session() as s:
        node = s.get(Node, int(node_id)) if str(node_id).isdigit() else None
        if node is None:
            return []
        reqs = s.exec(
            select(Request)
            .where(Request.node_id == node.id)
            .order_by(Request.created_at.desc(), Request.id.desc())
        ).all()
        if not reqs:
            return []
        rec_by_req: dict[int, UsageRecord] = {}
        for r in s.exec(
            select(UsageRecord).where(
                UsageRecord.request_id.in_([q.id for q in reqs])  # type: ignore[attr-defined]
            )
        ).all():
            rec_by_req[r.request_id] = r
        users = {u.id: u for u in s.exec(select(User)).all()}
        node_media = (node.data or {}).get("mediaIds") or []
        current = (node.data or {}).get("mediaId")

    out = []
    for q in reqs:
        rec = rec_by_req.get(q.id)
        params = q.params or {}
        result = q.result or {}
        media = [m for m in (result.get("media_ids") or []) if isinstance(m, str)]
        u = users.get(rec.user_id) if rec else None
        # Only a settled record represents money actually taken; a reserved or
        # released one must not be shown as a charge.
        cost = (
            round(float(rec.actual_usd or 0.0), 4)
            if rec is not None and rec.status == "settled"
            else None
        )
        out.append(
            {
                "request_id": q.id,
                "status": q.status,          # queued | running | done | failed
                "error": q.error,
                "created_at": q.created_at.isoformat() if q.created_at else None,
                "finished_at": q.finished_at.isoformat() if q.finished_at else None,
                "duration_ms": (
                    int((q.finished_at - q.created_at).total_seconds() * 1000)
                    if q.finished_at and q.created_at
                    else None
                ),
                "model": (rec.model if rec else None) or params.get("video_model_id"),
                "resolution": params.get("resolution"),
                "duration_seconds": params.get("duration_seconds"),
                "prompt": params.get("prompt") or params.get("motion_prompt"),
                "cost_usd": cost,
                "ledger_status": rec.status if rec else None,
                "user_name": (u.display_name or u.username) if u else None,
                "media_ids": media,
                # The take the node is showing right now (its output survived).
                "kept": bool(media) and (
                    current in media or any(m in node_media for m in media)
                ),
            }
        )
    return out


def shot_gens(shot_id) -> list[dict]:
    """Every generation in one shot — one row per take (settled UsageRecord):
    what node, model, resolution, the real cost, whether it was kept or a
    re-rolled/wasted take, WHICH member ran it, and when."""
    with get_session() as s:
        users, reqs, nodes, shots, scenes, _p, paid, _d = _load(s)
    by_node, _ = _group_by_node(paid, reqs)

    out = []
    for nid, recs in by_node.items():
        n = nodes.get(nid)
        if n is None or str(n.shot_id) != str(shot_id):
            continue
        title = (n.data or {}).get("title") or f"node {nid}"
        last = len(recs) - 1
        for i, r in enumerate(recs):
            req = reqs.get(r.request_id)
            res = (req.params or {}).get("resolution") if req else None
            u = users.get(r.user_id)
            out.append(
                {
                    "node_id": nid,
                    "node_title": title,
                    "node_type": n.type,
                    "kind": r.kind,  # image | video
                    "model": r.model,
                    "resolution": res,
                    "cost_usd": round(float(r.actual_usd or 0.0), 4),
                    "kept": i == last,  # last take on the node = the kept one
                    "user_id": str(r.user_id) if r.user_id else None,
                    "user_name": (u.display_name or u.username) if u else "(deleted)",
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
            )
    out.sort(key=lambda x: (x["created_at"] or ""))
    return out


# ── per user ────────────────────────────────────────────────────────────────


def user_costs() -> list[dict]:
    """Per user: granted vs spent, split into money that produced a KEPT clip
    and money burned on re-rolled takes."""
    with get_session() as s:
        users, reqs, nodes, shots, scenes, projects, paid, dl_nodes = _load(s)
    by_node, orphans = _group_by_node(paid, reqs)

    agg: dict = defaultdict(
        lambda: {
            "kept_usd": 0.0,
            "wasted_usd": 0.0,
            "kept_clips": 0,
            "wasted_takes": 0,
            "takes": 0,
            "downloaded_clips": 0,
        }
    )
    for nid, recs in by_node.items():
        uid = recs[-1].user_id  # the person who settled on the final take
        kept, wasted, takes = _split(recs)
        a = agg[uid]
        a["kept_usd"] += kept
        a["wasted_usd"] += wasted
        a["kept_clips"] += 1
        a["wasted_takes"] += takes - 1
        a["takes"] += takes
        if nid in dl_nodes:
            a["downloaded_clips"] += 1
    for r in orphans:  # gens not tied to a node — count as kept (can't tell)
        a = agg[r.user_id]
        a["kept_usd"] += float(r.actual_usd or 0.0)
        a["takes"] += 1

    out = []
    for uid, a in agg.items():
        u = users.get(uid)
        total = a["kept_usd"] + a["wasted_usd"]
        out.append(
            {
                "user_id": str(uid) if uid else None,
                "username": (u.username if u else "(deleted)"),
                "display_name": (u.display_name if u else None),
                "budget_usd": round(float(u.budget_usd), 4) if u else 0.0,
                "spent_usd": round(total, 4),
                "kept_usd": round(a["kept_usd"], 4),
                "wasted_usd": round(a["wasted_usd"], 4),
                "kept_clips": a["kept_clips"],
                "wasted_takes": a["wasted_takes"],
                "takes": a["takes"],
                "downloaded_clips": a["downloaded_clips"],
                "waste_pct": round((a["wasted_usd"] / total * 100) if total else 0.0, 1),
            }
        )
    out.sort(key=lambda r: r["wasted_usd"], reverse=True)  # biggest burners first
    return out


def user_clips(user_id) -> list[dict]:
    """Drill-down: every clip that user produced — takes, money burned on the
    discarded takes, cost of the kept one, and whether they downloaded it."""
    with get_session() as s:
        _users, reqs, nodes, shots, scenes, projects, paid, dl_nodes = _load(s)
    by_node, _ = _group_by_node(paid, reqs)

    out = []
    for nid, recs in by_node.items():
        if str(recs[-1].user_id) != str(user_id):
            continue
        n = nodes.get(nid)
        if n is None:
            continue
        kept, wasted, takes = _split(recs)
        pr, sc = _project_of(n, shots, scenes, projects)
        out.append(
            {
                "node_id": nid,
                "title": (n.data or {}).get("title") or f"node {nid}",
                "node_type": n.type,
                "project": pr.name if pr else None,
                "scene": sc.name if sc else None,
                "takes": takes,
                "wasted_usd": round(wasted, 4),
                "kept_usd": round(kept, 4),
                "total_usd": round(kept + wasted, 4),
                "downloaded": nid in dl_nodes,
                "model": recs[-1].model,
                "last_at": recs[-1].created_at.isoformat() if recs[-1].created_at else None,
            }
        )
    out.sort(key=lambda r: r["total_usd"], reverse=True)  # priciest clips first
    return out


# ── per project / per model ─────────────────────────────────────────────────


def project_costs() -> list[dict]:
    with get_session() as s:
        _u, reqs, nodes, shots, scenes, projects, paid, dl_nodes = _load(s)
    by_node, _ = _group_by_node(paid, reqs)

    agg: dict = defaultdict(
        lambda: {"kept_usd": 0.0, "wasted_usd": 0.0, "clips": 0, "takes": 0, "downloaded": 0}
    )
    for nid, recs in by_node.items():
        n = nodes.get(nid)
        pr, _sc = _project_of(n, shots, scenes, projects)
        key = str(pr.id) if pr else "—"
        kept, wasted, takes = _split(recs)
        a = agg[key]
        a["kept_usd"] += kept
        a["wasted_usd"] += wasted
        a["clips"] += 1
        a["takes"] += takes
        if nid in dl_nodes:
            a["downloaded"] += 1
        a["name"] = pr.name if pr else "(no project)"

    out = []
    for key, a in agg.items():
        total = a["kept_usd"] + a["wasted_usd"]
        out.append(
            {
                "project_id": key,
                "name": a.get("name", "—"),
                "total_usd": round(total, 4),
                "kept_usd": round(a["kept_usd"], 4),
                "wasted_usd": round(a["wasted_usd"], 4),
                "clips": a["clips"],
                "takes": a["takes"],
                "downloaded_clips": a["downloaded"],
                "waste_pct": round((a["wasted_usd"] / total * 100) if total else 0.0, 1),
            }
        )
    out.sort(key=lambda r: r["total_usd"], reverse=True)
    return out


def model_costs() -> list[dict]:
    """Which models/resolutions eat the budget (4k is ~4× 1080p)."""
    with get_session() as s:
        _u, reqs, _n, _sh, _sc, _p, paid, _d = _load(s)
    agg: dict = defaultdict(lambda: {"usd": 0.0, "takes": 0})
    for r in paid:
        req = reqs.get(r.request_id)
        res = (req.params or {}).get("resolution") if req else None
        key = f"{r.model or 'unknown'}{f' · {res}' if res else ''}"
        agg[key]["usd"] += float(r.actual_usd or 0.0)
        agg[key]["takes"] += 1
    total = sum(a["usd"] for a in agg.values()) or 1.0
    out = [
        {
            "model": k,
            "usd": round(a["usd"], 4),
            "takes": a["takes"],
            "pct": round(a["usd"] / total * 100, 1),
        }
        for k, a in agg.items()
    ]
    out.sort(key=lambda r: r["usd"], reverse=True)
    return out


# ── overview ────────────────────────────────────────────────────────────────


def overview() -> dict:
    rows = user_costs()
    spent = sum(r["spent_usd"] for r in rows)
    wasted = sum(r["wasted_usd"] for r in rows)
    kept = sum(r["kept_usd"] for r in rows)
    return {
        "spent_usd": round(spent, 4),
        "kept_usd": round(kept, 4),
        "wasted_usd": round(wasted, 4),
        "waste_pct": round((wasted / spent * 100) if spent else 0.0, 1),
        "kept_clips": sum(r["kept_clips"] for r in rows),
        "takes": sum(r["takes"] for r in rows),
        "downloaded_clips": sum(r["downloaded_clips"] for r in rows),
        "cost_per_clip": round((spent / sum(r["kept_clips"] for r in rows)), 4)
        if sum(r["kept_clips"] for r in rows)
        else 0.0,
    }
