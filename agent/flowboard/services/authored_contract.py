"""Signed production intent for authored films; never source-video evidence.

The project endpoint seals saved shot/asset definitions. The prompt still needs
the normal independent semantic review and generation receipt.
"""
from __future__ import annotations

import hashlib
import hmac
import json

from flowboard.services import auth


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), default=str).encode()).hexdigest()


def _signature(report: dict) -> str:
    payload = {k: v for k, v in report.items() if k != "signature"}
    return hmac.new(auth._server_secret(), b"flowboard-authored-intent-v1\0" +
        digest(payload).encode(), hashlib.sha256).hexdigest()


def board_shots(board: dict) -> list[dict]:
    return [shot for node in board.get("nodes", [])
            if node.get("data", {}).get("kind") == "sequence"
            for shot in node["data"].get("shots", [])]


def seal(project_id: str, script: str, board: dict) -> dict:
    if board.get("adaptation", {}).get("mode") != "authored_adaptation" or not script.strip():
        raise ValueError("A saved authored screenplay is required.")
    shots = board_shots(board)
    if not shots or any(s.get("provenance") != "authored_adaptation" or not s.get("id")
            or s.get("source_shots") or s.get("source_shot") or s.get("source_appearances") for s in shots):
        raise ValueError("Authored shots need unique IDs and cannot claim source-frame provenance.")
    if len({s["id"] for s in shots}) != len(shots):
        raise ValueError("Authored shot IDs must be unique.")
    assets = board.get("productionAssets") or []
    if not assets or any(not a.get("id") for a in assets) or len({a["id"] for a in assets}) != len(assets):
        raise ValueError("Authored assets need unique IDs.")
    report = {"status": "verified", "method": "authored_script", "scope": "saved_production_intent",
              "project_id": str(project_id), "script_digest": digest(script),
              "shot_digests": {s["id"]: digest(s) for s in shots},
              "asset_digests": {a["id"]: digest(a) for a in assets},
              "scope_notes": [{"code": "authored_not_source_verified",
                               "message": "Saved authored production intent, not verification against a source video."}]}
    report["digest"] = digest(report)
    report["signature"] = _signature(report)
    return report


def readiness(shots: list[dict], report: dict) -> list[str]:
    if report.get("method") != "authored_script" or report.get("status") != "verified" or not report.get("digest"):
        return ["Invalid authored production contract."]
    if not isinstance(report.get("signature"), str) or not hmac.compare_digest(report["signature"], _signature(report)):
        return ["Authored contract needs a valid server seal from the saved project."]
    if not shots:
        return ["No authored shots selected."]
    return [f"Authored shot {s.get('id')!r} changed after its production intent was sealed."
            for s in shots if s.get("provenance") != "authored_adaptation"
            or report.get("shot_digests", {}).get(s.get("id")) != digest(s)]


def validate(shots: list[dict], assets: list[dict], report: dict, references: list[dict]) -> list[str]:
    from flowboard.services.prompt_coverage import asset_id, reference_slots
    issues = readiness(shots, report)
    by_id = {asset_id(a): a for a in assets}
    if not by_id or "" in by_id or len(by_id) != len(assets):
        issues.append("Production asset IDs must be non-empty and unique.")
    if {k: digest(a) for k, a in by_id.items()} != report.get("asset_digests"):
        issues.append("Authored asset inventory changed; seal its current production intent.")
    _, ref_issues = reference_slots(references)
    issues.extend(ref_issues)
    referenced = {asset_id(r) for r in references if r.get("ref_url") and r.get("ref_label")}
    guides = {asset_id(r) for r in references if r.get("kind") == "continuity_frame"}
    if referenced - by_id.keys() - guides:
        issues.append("References contain assets outside the authored inventory.")
    for shot in shots:
        presence = shot.get("asset_presence")
        if not isinstance(presence, list) or not presence:
            issues.append(f"{shot.get('id')}: missing authored asset presence.")
            continue
        seen = set()
        for p in presence:
            if not isinstance(p, dict):
                issues.append("Invalid authored presence entry.")
                continue
            key = p.get("asset_id")
            if key in seen or key not in by_id:
                issues.append(f"Unknown or duplicate authored asset {key!r}.")
            seen.add(key)
            visible = p.get("visibility")
            if visible not in {"visible", "partial", "occluded", "offscreen"}:
                issues.append(f"Resolve authored visibility for {key!r}.")
            if visible != "offscreen" and by_id.get(key, {}).get("reference_required") and key not in referenced:
                issues.append(f"Required reference image is missing for asset {key!r}.")
            for related in ([p["holder_id"]] if p.get("holder_id") else []) + list(p.get("contains_ids") or []):
                if related not in by_id:
                    issues.append(f"Unknown related asset {related!r}.")
        expected = set(shot.get("scene_present_asset_ids") or []) | set(shot.get("character_keys") or [])
        if expected - seen:
            issues.append(f"{shot.get('id')}: scene/cast presence is incomplete.")
    return issues


def current_project_issues(project_id: str, sequence_key: str, board: dict, script: str,
                           report: dict, shots: list[dict]) -> list[str]:
    try:
        current = seal(project_id, script, board)
    except ValueError as exc:
        return [str(exc)]
    if current != report:
        return ["Authored production intent is stale or belongs to another project."]
    sequence = next((n["data"] for n in board.get("nodes", [])
                     if n.get("data", {}).get("kind") == "sequence"
                     and n["data"].get("sequence", {}).get("key") == sequence_key), None)
    if sequence is None or sequence.get("shots") != shots:
        return ["Authored generation must use the saved sequence's complete shot list."]
    return []
