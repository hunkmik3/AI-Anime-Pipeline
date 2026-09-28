"""Host-owned boundaries for visual source refinement and evidence requests."""

from __future__ import annotations

import copy


VISUAL_FIELDS = {
    "vfx", "action", "setting", "blocking", "end_pose", "reaction", "subjects",
    "subtitle", "shot_size", "uncertain", "expression", "start_pose", "title_card",
    "camera_angle", "camera_movement", "continuity_note", "screen_direction",
}
TEXT_FIELDS = {"subtitle", "title_card"}
NULLABLE_FIELDS = {"vfx", "reaction", "expression", "subtitle", "title_card"}
LIST_FIELDS = {"subjects", "uncertain"}


def _issue(code, message, shot=None, **extra):
    return {"code": code, "message": message, "shot": shot, **extra}


def apply_visual_updates(shots, updates, *, confirmed_text_updates=None):
    """Apply allowlisted visual fields to a copy, retaining ASR and all metadata.

    confirmed_text_updates is a HOST-owned {shot: {subtitle/title_card: value}}
    map made after independent frame-based QA; a flag in model output cannot
    authorize changing or deleting subtitle/title text. Missing keys preserve it.
    """
    result = copy.deepcopy(shots)
    by_number = {row["shot"]: row for row in result}
    issues = []
    if not isinstance(updates, list):
        return result, [_issue("invalid_visual_update", "Visual updates must be a list of shot/source rows.")]
    grouped = {}
    for update in updates:
        number = update.get("shot") if isinstance(update, dict) else None
        if type(number) is not int or number not in by_number:
            issues.append(_issue("invalid_visual_update", "Visual update names an unknown or invalid shot."))
            continue
        grouped.setdefault(number, []).append(update)
    for number, rows in grouped.items():
        if len(rows) != 1 or not isinstance(rows[0].get("source"), dict):
            issues.append(_issue("invalid_visual_update", "Shot requires one unambiguous source update object.", number))
            continue
        original = by_number[number].get("source")
        source = copy.deepcopy(original) if isinstance(original, dict) else {}
        for field, value in rows[0]["source"].items():
            if field not in VISUAL_FIELDS:
                continue
            valid = (isinstance(value, list) and all(isinstance(v, str) for v in value)
                     if field in LIST_FIELDS else
                     isinstance(value, str) or value is None and field in NULLABLE_FIELDS)
            if not valid:
                issues.append(_issue("invalid_visual_update", "Visual field has an invalid value type.", number, field=field))
                continue
            if field in TEXT_FIELDS and value != source.get(field):
                approved = (confirmed_text_updates or {}).get(number, {})
                if field not in approved or approved[field] != value:
                    issues.append(_issue("visual_text_confirmation_required", "Visible subtitle/title changes require independent visual confirmation; original text was preserved.", number, field=field))
                    continue
            source[field] = copy.deepcopy(value)
        by_number[number]["source"] = source
    return result, issues


def prepare_frame_requests(inv, requests, batch, fps):
    """Invalid crop may fall back to the requested full frame; time never does."""
    if requests is None:
        return [], []
    if not isinstance(requests, list):
        return [], [_issue("invalid_review_request", "Frame requests must be a list.")]
    by_shot = {row["shot"]: row for row in batch}
    safe, notes, seen = [], [], set()
    for request in requests:
        if not isinstance(request, dict):
            notes.append(_issue("invalid_review_request", "Frame request is not an object."))
            continue
        # Validate a crop-free copy first. A neighboring shot, frame number,
        # relative timestamp or out-of-shot seek must never be guessed/corrected.
        full = {**copy.deepcopy(request), "crop": None}
        base = inv._request(full, by_shot, fps)
        if base is None:
            notes.append(_issue("invalid_review_request", "Frame request has an invalid shot or absolute timestamp; no frame was requested.",
                                request.get("shot") if type(request.get("shot")) is int else None))
            continue
        selected = inv._request(request, by_shot, fps)
        if selected is None:
            selected = base
            notes.append(_issue("review_crop_full_frame_fallback", "Invalid normalized crop replaced by the full source frame at the same validated time; pixel coordinates were not inferred.",
                                selected[0], timestamp_s=selected[1], original_crop=copy.deepcopy(request.get("crop"))))
        number, at, crop = selected
        key = (number, at, tuple(crop) if crop else None)
        if key not in seen:
            safe.append({"shot": number, "timestamp_s": at, "crop": copy.deepcopy(crop),
                         "reason": str(request.get("reason") or "Targeted visual clarification")})
            seen.add(key)
    return safe, notes


def build_visual_context(inv, batch, inventory, evidence, *, candidate_profiles=None, extra_asset_ids=None):
    """Compact relevant profiles, dependencies and own frames plus visual anchors.

    Candidate proposals are separate from canonical definitions; equal IDs do not
    authorize profile replacement. The caller supplies the physical catalog after
    asset-layer classification and owns image readability/hash validation.
    """
    numbers = {row["shot"] for row in batch}
    canonical = {asset["id"]: asset for asset in inventory.get("assets") or []}
    candidates = [copy.deepcopy(a) for a in candidate_profiles or [] if isinstance(a, dict) and isinstance(a.get("id"), str)]
    definitions = {**{a["id"]: a for a in candidates}, **canonical}
    rows = {str(n): copy.deepcopy(inventory.get("shots", {}).get(str(n), {})) for n in numbers}
    relevant = set(extra_asset_ids or []) | {a["id"] for a in candidates}
    for row in rows.values():
        for presence in row.get("asset_presence") or []:
            relevant.add(presence["asset_id"])
            if presence.get("holder_id"):
                relevant.add(presence["holder_id"])
            relevant.update(presence.get("contains_ids") or [])
    while True:
        expanded = relevant | {ref for key in relevant for field in ("member_ids", "depends_on_asset_ids")
                               for ref in definitions.get(key, {}).get(field) or []}
        # A conflicting proposal's dependencies must remain available to judge it.
        expanded.update(ref for asset in candidates for field in ("member_ids", "depends_on_asset_ids")
                        for ref in asset.get(field) or [])
        if expanded == relevant:
            break
        relevant = expanded
    available = {frame["id"]: frame for frame in evidence}
    selected = {frame["id"] for frame in evidence if frame.get("shot") in numbers}
    for asset in [definitions[key] for key in sorted(relevant) if key in definitions] + candidates:
        selected.update([ref for ref in asset.get("evidence_ids") or [] if ref in available][:2])
    scene_ids = {row.get("scene_id") for row in rows.values() if row.get("scene_id")}
    scenes = [{**copy.deepcopy(scene),
               "shot_ids": [n for n in scene.get("shot_ids") or [] if n in numbers],
               "present_asset_ids": [key for key in scene.get("present_asset_ids") or [] if key in relevant]}
              for scene in inventory.get("scenes") or [] if scene["id"] in scene_ids]
    context = {
        "source_shots": inv._visual_source_rows(batch),
        "proposed_inventory": {"schema_version": inventory.get("schema_version", inv.SCHEMA_VERSION),
                               "assets": [copy.deepcopy(canonical[key]) for key in sorted(relevant) if key in canonical],
                               "scenes": scenes, "shots": rows},
        "candidate_profiles": candidates,
        "other_asset_index": [{key: asset.get(key) for key in ("id", "kind", "name")}
                              for asset in inventory.get("assets") or [] if asset["id"] not in relevant],
    }
    return context, [copy.deepcopy(available[key]) for key in sorted(selected)]
