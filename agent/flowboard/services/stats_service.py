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

import json
import logging
import re
from collections import defaultdict
from typing import Optional

from sqlmodel import select

from flowboard.db import get_session
from flowboard.db.models import (
    AppSetting,
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
    """Nested spend breakdown: **project → series → episode → sequence**, each
    carrying its own rolled-up total.

    The series tier used to be missing, so a project's episodes were listed flat.
    With two series in one project that produced two rows both called "Episode 1"
    with no way to tell which show they belonged to — the numbers were right and
    unreadable.

    Only branches that actually spent money appear — this view answers "where did
    the money go". How much of the slate hasn't started is a different question, and
    the tracker already reports it as unstaffed/undelivered counts.
    """
    from flowboard.db.models import Series as _Series

    with get_session() as s:
        _u, reqs, nodes, shots, scenes, projects, paid, _d = _load(s)
        series_map = {x.id: x for x in s.exec(select(_Series)).all()}

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

    def _new_project(pr):
        return {
            "project_id": str(pr.id) if pr else "—",
            "name": pr.name if pr else "(no project)",
            "total_usd": 0.0, "gens": 0, "clips": 0, "_series": {},
        }

    def _new_series(se):
        return {
            "series_id": str(se.id) if se else "—",
            "code": (se.code or "") if se else "",
            "name": se.name if se else "(no series)",
            "_order": se.order_index if se else 9999,
            "total_usd": 0.0, "gens": 0, "clips": 0, "_ep": {},
        }

    def _new_episode(sc):
        return {
            "scene_id": str(sc.id) if sc else "—",
            "code": (sc.code or "") if sc else "",
            "name": sc.name if sc else "(no episode)",
            "_order": sc.order_index if sc else 0,
            "total_usd": 0.0, "gens": 0, "clips": 0, "sequences": [],
        }

    proj_map: dict = {}

    def _bucket(pr, se, sc):
        """Reach (or create) the project → series → episode path for a scene."""
        p = proj_map.setdefault(str(pr.id) if pr else "—", _new_project(pr))
        sid = str(se.id) if se else "—"
        srow = p["_series"].setdefault(sid, _new_series(se))
        eid = str(sc.id) if sc else "—"
        erow = srow["_ep"].setdefault(eid, _new_episode(sc))
        return p, srow, erow

    for shid, a in shot_agg.items():
        if a["usd"] <= 0:
            continue
        sh = shots.get(shid)
        sc = scenes.get(sh.scene_id) if sh else None
        se = series_map.get(sc.series_id) if (sc and sc.series_id) else None
        pr = projects.get(sc.project_id) if sc else None
        p, srow, erow = _bucket(pr, se, sc)

        clips = len(a["nodes"])
        erow["sequences"].append(
            {
                "shot_id": str(shid),
                "shot_label": _shot_label(sh, scenes) if sh else "?",
                "_order": sh.order_index if sh else 0,
                "total_usd": round(a["usd"], 4),
                "gens": a["gens"],
                "clips": clips,
            }
        )
        for level in (erow, srow, p):
            level["total_usd"] += a["usd"]
            level["gens"] += a["gens"]
            level["clips"] += clips

    def _strip(d, *keys):
        for k in keys:
            d.pop(k, None)
        return d

    out = []
    for p in proj_map.values():
        series_rows = []
        for srow in p["_series"].values():
            episodes = list(srow["_ep"].values())
            for e in episodes:
                e["sequences"].sort(key=lambda x: x["_order"])
                for seq in e["sequences"]:
                    seq.pop("_order", None)
                e["total_usd"] = round(e["total_usd"], 4)
            episodes.sort(key=lambda e: (e["_order"], e["name"]))
            for e in episodes:
                _strip(e, "_order")
            srow["episodes"] = episodes
            srow["total_usd"] = round(srow["total_usd"], 4)
            series_rows.append(srow)
        # Biggest spend first: the point of the view is where money went.
        series_rows.sort(key=lambda r: (-r["total_usd"], r["_order"], r["name"]))
        for srow in series_rows:
            _strip(srow, "_order", "_ep")
        out.append(
            {
                "project_id": p["project_id"],
                "name": p["name"],
                "total_usd": round(p["total_usd"], 4),
                "gens": p["gens"],
                "clips": p["clips"],
                "series": series_rows,
            }
        )
    out.sort(key=lambda r: r["total_usd"], reverse=True)
    return out


def _ref_num(label) -> int:
    """Sort key for @imageN reference labels (image1 before image10)."""
    m = re.search(r"\d+", label or "")
    return int(m.group()) if m else 999


def project_video_gens(project_id) -> dict:
    """Every video generation in ONE project that produced a clip, grouped
    episode (scene) → sequence (shot), newest take first. Each gen carries its
    media, prompt and settings so the project page can show a "what have we
    generated" gallery with inline playback."""
    pid = str(project_id)
    with get_session() as s:
        users, reqs, nodes, shots, scenes, projects, _paid, _d = _load(s)
        settled = {
            u.request_id: u
            for u in s.exec(select(UsageRecord).where(UsageRecord.status == "settled")).all()
        }

    ep_map: dict = {}
    total = 0
    for req in reqs.values():
        if getattr(req, "type", None) != "gen_video":
            continue
        media = [m for m in ((req.result or {}).get("media_ids") or []) if isinstance(m, str)]
        if not media:
            continue
        n = nodes.get(req.node_id) if req.node_id else None
        if n is None or n.shot_id is None:
            continue
        sh = shots.get(n.shot_id)
        sc = scenes.get(sh.scene_id) if sh else None
        if sc is None or str(sc.project_id) != pid:
            continue
        params = req.params or {}
        rec = settled.get(req.id)
        total += 1
        ref_ids = params.get("reference_images") or []
        ref_lbls = params.get("reference_labels") or []
        references = [
            {"media_id": m, "label": ref_lbls[i] if i < len(ref_lbls) and isinstance(ref_lbls[i], str) else None}
            for i, m in enumerate(ref_ids)
            if isinstance(m, str)
        ]
        references.sort(key=lambda r: _ref_num(r["label"]))
        gen = {
            "request_id": req.id,
            "node_id": n.id,
            "node_title": (n.data or {}).get("title") or f"node {n.id}",
            "created_at": req.created_at.isoformat() if req.created_at else None,
            "status": req.status,
            "media_ids": media,
            "references": references,
            "prompt": params.get("prompt") or params.get("motion_prompt"),
            "model": params.get("model_id"),
            "resolution": params.get("resolution"),
            "aspect_ratio": params.get("aspect_ratio"),
            "duration_seconds": params.get("duration_seconds"),
            "cost_usd": round(float(rec.actual_usd or 0.0), 4) if rec else None,
        }
        scid = str(sc.id)
        ep = ep_map.setdefault(
            scid,
            {"scene_id": scid, "name": sc.name or "Episode", "_order": sc.order_index or 0, "_seq": {}},
        )
        shid = str(sh.id)
        seq = ep["_seq"].setdefault(
            shid,
            {"shot_id": shid, "shot_label": _shot_label(sh, scenes), "_order": sh.order_index or 0, "gens": []},
        )
        seq["gens"].append(gen)

    episodes = []
    for ep in sorted(ep_map.values(), key=lambda e: e["_order"]):
        seqs = sorted(ep["_seq"].values(), key=lambda x: x["_order"])
        for seq in seqs:
            seq.pop("_order", None)
            seq["gens"].sort(key=lambda g: (g["created_at"] or ""), reverse=True)
        episodes.append({"scene_id": ep["scene_id"], "name": ep["name"], "sequences": seqs})
    return {"total": total, "episodes": episodes}


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
                "duration_seconds": params.get("duration_seconds") or result.get("duration"),
                "prompt": params.get("prompt") or params.get("motion_prompt"),
                "cost_usd": cost,
                "ledger_status": rec.status if rec else None,
                "user_name": (u.display_name or u.username) if u else None,
                "media_ids": media,
                # Audio (Seed Audio) history — the tile plays an <audio> element
                # and shows these settings instead of a video's resolution.
                "kind": "audio" if q.type == "gen_audio" else "video",
                "audio_format": params.get("format"),
                "sample_rate": params.get("sample_rate"),
                "speech_rate": params.get("speech_rate"),
                "loudness_rate": params.get("loudness_rate"),
                "pitch_rate": params.get("pitch_rate"),
                # Material used — so the history can spawn a node that reuses it.
                "references": [
                    r for r in (params.get("references") or []) if isinstance(r, str)
                ],
                "image_ref": params.get("image_ref"),
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


def _orphan_res_map() -> dict:
    """Resolution recovered for settled usage records whose request was deleted
    (matched to the output file by timestamp — see the one-off backfill). Lets
    the model/resolution breakdown show the real resolution for orphaned gens
    instead of a blank."""
    with get_session() as s:
        row = s.get(AppSetting, "orphan_resolution_map")
    if row and row.value:
        try:
            return json.loads(row.value)
        except (ValueError, TypeError):
            return {}
    return {}


def model_costs() -> list[dict]:
    """Which models/resolutions eat the budget (4k is ~4× 1080p)."""
    orphan_map = _orphan_res_map()
    with get_session() as s:
        _u, reqs, _n, _sh, _sc, _p, paid, _d = _load(s)
    agg: dict = defaultdict(lambda: {"usd": 0.0, "takes": 0})
    for r in paid:
        req = reqs.get(r.request_id)
        res = (req.params or {}).get("resolution") if req else orphan_map.get(str(r.id))
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


# ── the spend ledger ────────────────────────────────────────────────────────


def _episode_path(node, shots, scenes, series_map, projects):
    """Where a node sits: (project, series, episode, sequence) — as objects."""
    sh = shots.get(node.shot_id) if node is not None and node.shot_id else None
    sc = scenes.get(sh.scene_id) if sh else None
    se = series_map.get(sc.series_id) if (sc and sc.series_id) else None
    pr = projects.get(sc.project_id) if sc else None
    return pr, se, sc, sh


def spend_ledger(
    *,
    project_id=None,
    series_id=None,
    scene_id=None,
    shot_id=None,
    user_id=None,
    model=None,
    kept=None,
    limit: int = 200,
    offset: int = 0,
) -> dict:
    """Every billed generation, one row per take, with the full context of where
    it happened and who ran it.

    The overview answers "how much did we spend"; this answers "on what, exactly".
    The existing per-shot and per-user views could each show a slice, so tracing a
    charge meant opening several screens and joining them by eye — this is the one
    place that holds the whole ledger, filterable down to a single sequence.

    ``kept`` is the distinction that makes the number explainable: takes are grouped
    by node, and the newest take on a node is the one the artist settled on. The
    earlier ones are re-rolls — real money, deliberately spent, but not what shipped.
    ``downloaded`` is the only signal that a clip was actually used.

    Rows are newest-first, and ``totals`` describes the whole filtered set, not just
    the page — a total that only covered the visible rows would be worse than none.
    """
    from flowboard.db.models import Series as _Series

    with get_session() as s:
        users, reqs, nodes, shots, scenes, projects, paid, downloaded = _load(s)
        series_map = {x.id: x for x in s.exec(select(_Series)).all()}

    by_node, orphans = _group_by_node(paid, reqs)

    rows: list[dict] = []
    for nid, recs in by_node.items():
        n = nodes.get(nid)
        if n is None:
            continue
        pr, se, sc, sh = _episode_path(n, shots, scenes, series_map, projects)

        if project_id and (pr is None or str(pr.id) != str(project_id)):
            continue
        if series_id and (se is None or str(se.id) != str(series_id)):
            continue
        if scene_id and (sc is None or str(sc.id) != str(scene_id)):
            continue
        if shot_id and (sh is None or str(sh.id) != str(shot_id)):
            continue

        last = len(recs) - 1
        for i, r in enumerate(recs):
            is_kept = i == last
            if kept is not None and is_kept is not bool(kept):
                continue
            if user_id and str(r.user_id) != str(user_id):
                continue
            if model and (r.model or "") != model:
                continue
            req = reqs.get(r.request_id)
            u = users.get(r.user_id)
            rows.append(
                {
                    "usage_id": r.id,
                    "when": r.created_at.isoformat() if r.created_at else None,
                    "user_id": str(r.user_id) if r.user_id else None,
                    "user_name": (u.display_name or u.username) if u else "(deleted)",
                    "project_id": str(pr.id) if pr else None,
                    "project_name": pr.name if pr else None,
                    "series_id": str(se.id) if se else None,
                    "series_code": (se.code or se.name) if se else None,
                    "scene_id": str(sc.id) if sc else None,
                    "episode": (sc.code or sc.name) if sc else None,
                    "shot_id": str(sh.id) if sh else None,
                    "sequence": _shot_label(sh, scenes) if sh else None,
                    "node_id": nid,
                    "kind": r.kind,
                    "model": r.model,
                    "resolution": (req.params or {}).get("resolution") if req else None,
                    "duration_seconds": (
                        (req.params or {}).get("duration_seconds") if req else None
                    ),
                    "cost_usd": round(float(r.actual_usd or 0.0), 4),
                    # take 1 of N on this node — tells the artist's retake story
                    "take": i + 1,
                    "takes_on_node": len(recs),
                    "kept": is_kept,
                    "downloaded": nid in downloaded,
                }
            )

    # Charges with no node can't be attributed to a project, so they only belong in
    # an unfiltered view — hiding them there would make the total disagree with the
    # bill, which is the one thing a ledger must never do.
    if not any((project_id, series_id, scene_id, shot_id)):
        for r in orphans:
            if user_id and str(r.user_id) != str(user_id):
                continue
            if model and (r.model or "") != model:
                continue
            if kept is not None and bool(kept) is False:
                continue
            u = users.get(r.user_id)
            rows.append(
                {
                    "usage_id": r.id,
                    "when": r.created_at.isoformat() if r.created_at else None,
                    "user_id": str(r.user_id) if r.user_id else None,
                    "user_name": (u.display_name or u.username) if u else "(deleted)",
                    "project_id": None, "project_name": None,
                    "series_id": None, "series_code": None,
                    "scene_id": None, "episode": None,
                    "shot_id": None, "sequence": None,
                    "node_id": None,
                    "kind": r.kind, "model": r.model,
                    "resolution": None, "duration_seconds": None,
                    "cost_usd": round(float(r.actual_usd or 0.0), 4),
                    "take": 1, "takes_on_node": 1,
                    # Not "kept": with no node there are no sibling takes to
                    # compare against, so whether this shipped is unknowable.
                    # Marking it kept counted $830 of unclassifiable history as
                    # shipped work and reported re-rolls at 10% when the spend that
                    # can actually be classified is nearer half.
                    "kept": False, "downloaded": False,
                    "unattributed": True,
                }
            )

    rows.sort(key=lambda x: (x["when"] or ""), reverse=True)

    # Three buckets, not two. Charges with no node can't be told apart into
    # shipped-vs-re-rolled, so they are their own line rather than being folded
    # into either — and the re-roll share is measured against the spend that CAN be
    # classified, or it reads far better than reality.
    classified = [r for r in rows if not r.get("unattributed")]
    unclassified = [r for r in rows if r.get("unattributed")]

    total_usd = round(sum(r["cost_usd"] for r in rows), 4)
    kept_usd = round(sum(r["cost_usd"] for r in classified if r["kept"]), 4)
    retake_usd = round(sum(r["cost_usd"] for r in classified if not r["kept"]), 4)
    unclassified_usd = round(sum(r["cost_usd"] for r in unclassified), 4)
    classified_usd = round(kept_usd + retake_usd, 4)

    return {
        "rows": rows[offset : offset + limit],
        "total_rows": len(rows),
        "offset": offset,
        "limit": limit,
        "totals": {
            "generations": len(rows),
            "total_usd": total_usd,
            "kept_usd": kept_usd,
            "retake_usd": retake_usd,
            "unclassified_usd": unclassified_usd,
            "unclassified_count": len(unclassified),
            # Share of the CLASSIFIABLE spend that went on attempts which didn't
            # ship. Against the grand total this would be diluted by every charge
            # we can't attribute.
            "retake_pct": (
                round(retake_usd / classified_usd * 100, 1) if classified_usd else 0.0
            ),
            "people": len({r["user_id"] for r in rows if r["user_id"]}),
            "downloaded": sum(1 for r in rows if r["downloaded"]),
        },
    }


def ledger_filter_options() -> dict:
    """The values that actually appear in the ledger, so the filters offer real
    choices instead of every project and model that has ever existed."""
    from flowboard.db.models import Series as _Series

    with get_session() as s:
        users, reqs, nodes, shots, scenes, projects, paid, _d = _load(s)
        series_map = {x.id: x for x in s.exec(select(_Series)).all()}
    by_node, _ = _group_by_node(paid, reqs)

    seen_projects, seen_people, seen_models = {}, {}, set()
    for nid, recs in by_node.items():
        n = nodes.get(nid)
        if n is None:
            continue
        pr, _se, _sc, _sh = _episode_path(n, shots, scenes, series_map, projects)
        if pr is not None:
            seen_projects[str(pr.id)] = pr.name
        for r in recs:
            if r.user_id:
                u = users.get(r.user_id)
                seen_people[str(r.user_id)] = (
                    (u.display_name or u.username) if u else "(deleted)"
                )
            if r.model:
                seen_models.add(r.model)
    return {
        "projects": [{"id": k, "name": v} for k, v in sorted(seen_projects.items(), key=lambda x: x[1])],
        "people": [{"id": k, "name": v} for k, v in sorted(seen_people.items(), key=lambda x: x[1])],
        "models": sorted(seen_models),
    }


# ── over time ───────────────────────────────────────────────────────────────

_PERIODS = ("day", "week", "month", "year")


def _bucket_key(when, period: str) -> tuple[str, str]:
    """(sort key, human label) for a timestamp in the chosen period.

    ISO week for "week" rather than "7 days ago", so a bucket always means the same
    calendar span no matter when the page is opened.
    """
    if period == "year":
        return f"{when.year:04d}", str(when.year)
    if period == "month":
        return f"{when.year:04d}-{when.month:02d}", when.strftime("%b %Y")
    if period == "week":
        iso = when.isocalendar()
        return f"{iso[0]:04d}-W{iso[1]:02d}", f"W{iso[1]:02d} {iso[0]}"
    return when.strftime("%Y-%m-%d"), when.strftime("%d %b")


def spend_timeline(period: str = "day", buckets: int = 30) -> dict:
    """Credits burned per period, and who burned them.

    Answers the question the totals can't: *when*. A single grand total says the
    studio spent $1,056 but not whether that was steady or one bad week, and per-user
    totals say who spent it but not when they were working.

    Each bucket carries its own per-person breakdown, so "who did what on Tuesday"
    is one row deep rather than a separate query.

    Deliveries are counted alongside spend: credits burned with nothing handed in is
    a different situation from the same spend that shipped four episodes, and the two
    numbers only mean something next to each other.
    """
    from flowboard.db.models import Submission as _Submission

    if period not in _PERIODS:
        period = "day"
    buckets = max(1, min(buckets, 366))

    with get_session() as s:
        users, reqs, nodes, shots, scenes, projects, paid, _d = _load(s)
        subs = list(s.exec(select(_Submission)).all())

    by_node, _orphans = _group_by_node(paid, reqs)
    # node → whether each take was the kept one, so a bucket can separate the spend
    # that shipped from the spend re-rolled away.
    kept_take: dict[int, object] = {}
    for nid, recs in by_node.items():
        if recs:
            kept_take[nid] = recs[-1].id

    rows: dict[str, dict] = {}

    def _row(key: str, label: str) -> dict:
        return rows.setdefault(
            key,
            {
                "key": key,
                "label": label,
                "total_usd": 0.0,
                "kept_usd": 0.0,
                "retake_usd": 0.0,
                "generations": 0,
                "delivered": 0,
                "submitted": 0,
                "_people": {},
            },
        )

    for r in paid:
        if r.created_at is None:
            continue
        key, label = _bucket_key(r.created_at, period)
        row = _row(key, label)
        cost = float(r.actual_usd or 0.0)
        req = reqs.get(r.request_id)
        nid = req.node_id if req else None
        is_kept = nid is not None and kept_take.get(nid) == r.id

        row["total_usd"] += cost
        row["generations"] += 1
        if nid is None:
            pass  # unattributable: counted in the total, not split kept/re-roll
        elif is_kept:
            row["kept_usd"] += cost
        else:
            row["retake_usd"] += cost

        uid = str(r.user_id) if r.user_id else None
        u = users.get(r.user_id)
        person = row["_people"].setdefault(
            uid or "—",
            {
                "user_id": uid,
                "name": (u.display_name or u.username) if u else "(deleted)",
                "generations": 0,
                "total_usd": 0.0,
                "delivered": 0,
            },
        )
        person["generations"] += 1
        person["total_usd"] += cost

    # Deliveries land in the bucket of the decision, not the submission: "what did
    # this week produce" means what was accepted in it.
    for sub in subs:
        stamp = sub.reviewed_at if sub.status in ("approved", "rejected") else sub.submitted_at
        if stamp is None:
            continue
        key, label = _bucket_key(stamp, period)
        row = _row(key, label)
        if sub.status == "approved":
            row["delivered"] += 1
            uid = str(sub.submitted_by) if sub.submitted_by else None
            if uid and uid in row["_people"]:
                row["_people"][uid]["delivered"] += 1
        elif sub.status == "submitted":
            row["submitted"] += 1

    out = []
    for row in rows.values():
        people = sorted(
            row.pop("_people").values(), key=lambda p: -p["total_usd"]
        )
        for p in people:
            p["total_usd"] = round(p["total_usd"], 4)
        out.append(
            {
                **{
                    k: (round(v, 4) if isinstance(v, float) else v)
                    for k, v in row.items()
                },
                "people": people,
            }
        )
    # Newest first, then trimmed: a chart of the last 30 days is the useful window,
    # and everything older is still in the ledger.
    out.sort(key=lambda r: r["key"], reverse=True)
    trimmed = out[:buckets]
    return {
        "period": period,
        "buckets": trimmed,
        "totals": {
            "total_usd": round(sum(b["total_usd"] for b in trimmed), 4),
            "generations": sum(b["generations"] for b in trimmed),
            "delivered": sum(b["delivered"] for b in trimmed),
            "peak_usd": round(max((b["total_usd"] for b in trimmed), default=0.0), 4),
        },
    }
