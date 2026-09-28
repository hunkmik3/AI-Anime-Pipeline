"""Explicit target-film changes layered over immutable source observations.

An adaptation is production intent, never a source verification or acceptance.
The original appearances and their signed digests remain the source contract.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any


_TEXT_FIELDS = {"title", "framing", "framing_note", "edit_note"}
_LIST_FIELDS = {"action", "performance", "sfx", "avoid"}


def source_digest(shot: dict) -> str:
    """Anchor an overlay to all original appearances, including merged shots."""
    sources = shot.get("source_shots") or [shot.get("source_shot")]
    value = {"source_shots": [str(n) for n in sources if n is not None],
             "source_appearances": shot.get("source_appearances") or shot.get("source_asset_presence") or []}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     default=str).encode()).hexdigest()


def validate(shot: dict, report: dict | None = None) -> list[str]:
    """Reject stale anchors and any attempt to erase/invent source presence."""
    if "production_adaptation" not in shot:
        return []
    overlay = shot["production_adaptation"]
    if not isinstance(overlay, dict):
        return ["production_adaptation must be an object."]
    issues: list[str] = []
    if set(overlay) - {"schema_version", "reason", "source_digest", "shot_overrides", "asset_presence"}:
        issues.append("production_adaptation contains unsupported fields.")
    if type(overlay.get("schema_version")) is not int or overlay["schema_version"] != 1:
        issues.append("production_adaptation schema_version must be 1.")
    if not isinstance(overlay.get("reason"), str) or not overlay["reason"].strip():
        issues.append("production_adaptation requires an explicit production reason.")
    originals = shot.get("source_appearances") or shot.get("source_asset_presence") or []
    if not isinstance(originals, list) or not originals:
        issues.append("production_adaptation requires immutable source_appearances.")
    if overlay.get("source_digest") != source_digest(shot):
        issues.append("production_adaptation source_digest is stale or missing.")
    if report is not None:
        digests = report.get("shot_digests")
        sources = shot.get("source_shots") or [shot.get("source_shot")]
        if not isinstance(digests, dict) or any(not digests.get(str(n)) for n in sources):
            issues.append("production_adaptation requires verified source shot digests.")
    changes = overlay.get("shot_overrides", {})
    if not isinstance(changes, dict):
        issues.append("production_adaptation shot_overrides must be an object.")
    else:
        for key, value in changes.items():
            if key in _TEXT_FIELDS:
                valid = isinstance(value, str)
            elif key in _LIST_FIELDS:
                valid = isinstance(value, list) and all(isinstance(x, str) for x in value)
            elif key == "dialogue":
                valid = isinstance(value, list) and all(isinstance(x, dict)
                    and isinstance(x.get("who"), str) and isinstance(x.get("line"), str)
                    and not (set(x) - {"who", "line", "cont", "delivery"}) for x in value)
            elif key in {"camera", "lighting"}:
                valid = isinstance(value, str) or isinstance(value, dict)
            elif key == "vfx":
                valid = isinstance(value, str) or (isinstance(value, list) and all(isinstance(x, str) for x in value))
            else:
                valid = False
            if not valid:
                issues.append(f"production_adaptation cannot override {key!r} with this value.")
    occurrences = {(str(p.get("source_shot", "")), str(p.get("asset_id", "")))
                   for p in shot.get("asset_presence") or [] if isinstance(p, dict)}
    presence = overlay.get("asset_presence", [])
    if not isinstance(presence, list):
        issues.append("production_adaptation asset_presence must be a list.")
    else:
        seen: set[tuple[str, str]] = set()
        for p in presence:
            if not isinstance(p, dict):
                issues.append("production_adaptation appearance override must be an object.")
                continue
            occurrence = (str(p.get("source_shot", "")), str(p.get("asset_id", "")))
            if occurrence not in occurrences or occurrence in seen:
                issues.append("production_adaptation must map each unique existing source-shot/asset occurrence exactly.")
            seen.add(occurrence)
            if set(p) - {"source_shot", "asset_id", "state", "position"}:
                issues.append("production_adaptation may only change state and position; visibility, identity, relations and evidence are immutable.")
            changed = set(p) & {"state", "position"}
            if not changed or any(not isinstance(p[k], str) or not p[k].strip() for k in changed):
                issues.append("production_adaptation appearance needs a non-empty state or position.")
    return issues


def apply(shot: dict) -> dict:
    """Return a derived target shot; never mutate the source shot or its records."""
    if "production_adaptation" not in shot:
        return deepcopy(shot)
    problems = validate(shot)
    if problems:
        raise ValueError("; ".join(problems))
    result = deepcopy(shot)
    overlay = result["production_adaptation"]
    result.update(deepcopy(overlay.get("shot_overrides", {})))
    changes = {(str(p["source_shot"]), str(p["asset_id"])): p
               for p in overlay.get("asset_presence", [])}

    def target(p: dict, source: Any = None) -> dict:
        key = (str(p.get("source_shot", source)), str(p.get("asset_id", "")))
        return {**deepcopy(p), **{k: v for k, v in changes.get(key, {}).items() if k in {"state", "position"}}}

    result["asset_presence"] = [target(p) for p in shot.get("asset_presence") or []]
    result["production_appearances"] = [
        {**deepcopy(a), "asset_presence": [target(p, a.get("source_shot")) for p in a.get("asset_presence") or []]}
        for a in shot.get("source_appearances") or shot.get("source_asset_presence") or []]
    return result
