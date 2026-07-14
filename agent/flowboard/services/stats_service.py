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
                "username": (u.username if u else "(đã xoá)"),
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
        a["name"] = pr.name if pr else "(không thuộc project)"

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
