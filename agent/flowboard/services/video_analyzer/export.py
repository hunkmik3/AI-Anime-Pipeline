"""Exports. JSON is the source of truth; Markdown is only a way of reading it (guide §32-33).

Source and adaptation stay in separate objects inside every shot (guide §78),
so a reader can always see what the reference video actually did next to what
the adaptation turned it into.
"""
from __future__ import annotations

from typing import Optional

from flowboard.services.video_analyzer.probe import timecode

MODES = ("short", "detailed", "generation")


def to_json(analysis: dict, adaptation: Optional[dict] = None) -> dict:
    adapted = (adaptation or {}).get("shots") or {}
    # Frame numbers, not only seconds: an editor conforming a cut works in
    # frames, and a 0.2s insert is 6 of them.
    fps = float((analysis.get("video") or {}).get("fps") or 0) or 0.0
    shots = []
    for s in analysis["shots"]:
        src = s.get("source") or {}
        shots.append(
            {
                "shot_id": s["shot"],
                "timeline": {
                    "start": s["start"],
                    "end": s["end"],
                    "duration": round(s["end"] - s["start"], 3),
                    "start_tc": timecode(s["start"]),
                    "end_tc": timecode(s["end"]),
                    "start_frame": round(s["start"] * fps) if fps else None,
                    "end_frame": (round(s["end"] * fps) - 1) if fps else None,
                    "frames": (round(s["end"] * fps) - round(s["start"] * fps)) if fps else None,
                },
                "source": {
                    "shot_size": src.get("shot_size"),
                    "angle": src.get("camera_angle"),
                    "movement": src.get("camera_movement"),
                    "subjects": src.get("subjects"),
                    "blocking": src.get("blocking"),
                    "screen_direction": src.get("screen_direction"),
                    "start_pose": src.get("start_pose"),
                    "action": src.get("action"),
                    "reaction": src.get("reaction"),
                    "end_pose": src.get("end_pose"),
                    "expression": src.get("expression"),
                    "vfx": src.get("vfx"),
                    "title_card": src.get("title_card"),
                    "subtitle": src.get("subtitle"),
                    "dialogue": s.get("dialogue") or None,
                    "continuity_note": src.get("continuity_note"),
                    "confidence": src.get("confidence"),
                    "uncertain": src.get("uncertain"),
                    "model": src.get("_model"),
                },
                "adaptation": adapted.get(str(s["shot"])),
                "frames": s.get("frames"),
            }
        )
    return {
        "video": analysis["video"],
        "sequences": analysis.get("sequences") or [],
        "entities": analysis.get("entities") or {},
        "scene_inventory": analysis.get("scene_inventory"),
        "source_verification": analysis.get("source_verification"),
        "adaptation": (
            {"rules": adaptation.get("rules"), "glossary": adaptation.get("glossary")} if adaptation else None
        ),
        "shots": shots,
        "validation": (adaptation or analysis).get("validation"),
    }


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        value = "; ".join(str(v) for v in value if v)
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def _camera(src: dict) -> str:
    bits = [src.get("shot_size"), src.get("camera_angle")]
    move = src.get("camera_movement")
    if move and move != "static":
        bits.append(move)
    return " · ".join(b for b in bits if b)


def _source_description(s: dict) -> str:
    src = s.get("source") or {}
    parts = [src.get("action") or src.get("blocking") or ""]
    if src.get("reaction"):
        parts.append(f"Reaction: {src['reaction']}")
    if src.get("vfx"):
        parts.append(f"VFX: {src['vfx']}")
    if src.get("title_card"):
        parts.append(f"Card: “{src['title_card']}”")
    return " ".join(p for p in parts if p)


def _adapted_description(item: dict, mode: str) -> str:
    if mode == "short":
        return item.get("adapted_description") or item.get("title") or ""
    parts: list[str] = []
    if item.get("title"):
        parts.append(f"**{item['title']}**")
    if item.get("camera"):
        parts.append(item["camera"] + ".")
    parts += [str(a) for a in item.get("action") or []]
    for line in item.get("dialogue") or []:
        if isinstance(line, dict) and line.get("line"):
            parts.append(f"{line.get('who') or '?'}: “{line['line']}”")
    if mode == "generation":
        if item.get("performance"):
            parts.append("Performance: " + "; ".join(item["performance"]))
        if item.get("vfx"):
            parts.append(f"VFX: {item['vfx']}")
        if item.get("sfx"):
            parts.append("Sound: " + "; ".join(item["sfx"]))
        if item.get("avoid"):
            parts.append("Do NOT: " + "; ".join(item["avoid"]))
        if item.get("edit_note"):
            parts.append(f"Edit: {item['edit_note']}")
    return " ".join(parts)


def to_markdown(analysis: dict, adaptation: Optional[dict] = None, *, mode: str = "detailed",
                title: str = "SHOTLIST") -> str:
    """One table per sequence. Sequences are navigation only — numbering never restarts."""
    mode = mode if mode in MODES else "detailed"
    adapted = (adaptation or {}).get("shots") or {}
    by_number = {s["shot"]: s for s in analysis["shots"]}
    video = analysis["video"]
    n = len(analysis["shots"])

    fps = float(video.get("fps") or 0)
    out = [
        f"# {title}",
        "",
        f"{n} shots · {timecode(video['duration'])} · {video.get('aspect_ratio')} · {fps:g} fps",
        "",
    ]

    if adaptation and adaptation.get("glossary"):
        rows = [
            (kind, src, dst)
            for kind, table in adaptation["glossary"].items()
            for src, dst in (table or {}).items()
        ]
        if rows:
            out += ["## Glossary", "", "| Type | Source | Target |", "|---|---|---|"]
            out += [f"| {_cell(k)} | {_cell(s)} | {_cell(d)} |" for k, s, d in rows]
            out.append("")

    sequences = analysis.get("sequences") or [{"first_shot": 1, "last_shot": n, "title": "Full video"}]
    for i, q in enumerate(sequences, start=1):
        a, b = q["first_shot"], q["last_shot"]
        out.append(f"## Sequence {i:02d} — {_cell(q.get('title'))} · shots {a:03d}–{b:03d}")
        if q.get("goal"):
            out += ["", f"_{_cell(q['goal'])}_"]
        out.append("")
        if adaptation:
            out += ["| # | Timecode | Camera | Source | Adaptation |", "|---:|---|---|---|---|"]
        else:
            out += ["| # | Timecode | Camera | Description | Dialogue |", "|---:|---|---|---|---|"]
        for num in range(a, b + 1):
            s = by_number.get(num)
            if not s:
                continue
            tc = f"{timecode(s['start'])}–{timecode(s['end'])}"
            if fps:
                tc += f"<br>f{round(s['start'] * fps)}–{round(s['end'] * fps) - 1}"
            cam = _camera(s.get("source") or {})
            src = _source_description(s)
            if adaptation:
                item = adapted.get(str(num)) or {}
                out.append(f"| {num:03d} | {tc} | {_cell(cam)} | {_cell(src)} | {_cell(_adapted_description(item, mode))} |")
            else:
                out.append(f"| {num:03d} | {tc} | {_cell(cam)} | {_cell(src)} | {_cell(s.get('dialogue'))} |")
        out.append("")

    qa = (adaptation or analysis).get("validation") or {}
    if qa.get("findings"):
        out += ["## Review", "", f"{qa.get('errors', 0)} errors · {qa.get('warnings', 0)} warnings", ""]
        for f in qa["findings"]:
            where = f"shot {f['shot']:03d} — " if f.get("shot") else ""
            out.append(f"- **{f['level']}** `{f['code']}` {where}{f['message']}")
        out.append("")
    return "\n".join(out)
