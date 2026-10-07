"""Bounded visual continuity context; neighboring images never prove presence."""
from __future__ import annotations

import copy
import math

from .source_refinement_support import build_visual_context

NEIGHBOR_RADIUS = 2
MAX_GAP_SECONDS = 8.0
MAX_FRAMES_PER_NEIGHBOR = 2
VISIBLE = {"visible", "partial"}


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _bounds(shot):
    start, end = shot.get("start"), shot.get("end")
    return _finite(start) and _finite(end) and start < end


def _near_time(timestamp, shot):
    return _finite(timestamp) and max(shot["start"] - timestamp, timestamp - shot["end"], 0) <= MAX_GAP_SECONDS


def _scene_relation(current, neighbor, inventory):
    rows = inventory.get("shots", {})
    left = rows.get(str(current), {}).get("scene_id")
    right = rows.get(str(neighbor), {}).get("scene_id")
    if not left or not right:
        return "unknown_scene"
    return "same_scene" if left == right else "scene_boundary"


def _alias_graph(inventory, known):
    aliases = inventory.get("identity_aliases") or {}
    canonical, paths, unresolved = {}, {}, []
    for key in sorted(aliases):
        if not isinstance(key, str):
            continue
        path, seen, cursor = [key], {key}, key
        while cursor in aliases:
            target = aliases[cursor]
            if not isinstance(target, str) or target in seen:
                break
            path.append(target); seen.add(target); cursor = target
        else:
            if cursor in known:
                canonical[key] = cursor
                paths[key] = path
                continue
        unresolved.append(key)
    return canonical, paths, unresolved


def build_temporal_context(inv, batch, all_shots, inventory, evidence, *, extra_asset_ids=()):
    """Wrap current-shot context with at most two neighboring shots per side.

    The caller validates image readability/hashes before this function. Neighbor
    selection uses the complete source timeline, not the subset needing review.
    Presence comes only from individual shot rows, never a scene's member union.
    """
    current = {row["shot"]: row for row in batch}
    timeline = sorted((row for row in all_shots if type(row.get("shot")) is int and _bounds(row)),
                      key=lambda row: (row["start"], row["end"], row["shot"]))
    positions = {row["shot"]: index for index, row in enumerate(timeline)}
    available = {frame["id"]: frame for frame in evidence}
    relations = []
    for number, shot in sorted(current.items()):
        if number not in positions or not _bounds(shot):
            continue
        index = positions[number]
        for offset in range(-NEIGHBOR_RADIUS, NEIGHBOR_RADIUS + 1):
            other_index = index + offset
            if not offset or not 0 <= other_index < len(timeline):
                continue
            neighbor = timeline[other_index]
            gap = max(shot["start"] - neighbor["end"], neighbor["start"] - shot["end"], 0)
            if gap <= MAX_GAP_SECONDS:
                relations.append({"for_shot": number, "shot": neighbor["shot"], "offset": offset,
                                  "gap_seconds": gap, "scene_relation": _scene_relation(number, neighbor["shot"], inventory)})
    # A cropped target may omit the person's ID precisely because the extractor
    # could not recognize it. Include identity anchors for observed neighbors,
    # not just profiles already assigned to the target. Scene unions still
    # cannot create presence; all contextual links require own-shot evidence.
    neighbor_assets = {p.get('asset_id') for relation in relations
        for p in inventory.get('shots', {}).get(str(relation['shot']), {}).get('asset_presence', [])
        if p.get('visibility') in VISIBLE and isinstance(p.get('asset_id'), str)}
    context, base = build_visual_context(inv, batch, inventory, evidence,
        extra_asset_ids=set(extra_asset_ids) | neighbor_assets)
    selected_neighbors = {}
    for number in sorted({row["shot"] for row in relations}):
        related = [row for row in relations if row["shot"] == number]
        preferred = [row for row in related if row["scene_relation"] == "same_scene"] or related
        frames = [frame for frame in evidence if frame.get("shot") == number and
                  any(_near_time(frame.get("timestamp_s"), current[row["for_shot"]]) for row in preferred)]
        full = [frame for frame in frames if not frame.get("crop")]
        frames = sorted(full or frames, key=lambda frame: (frame["timestamp_s"], frame["id"]))
        refs = list(dict.fromkeys([frames[0]["id"], frames[-1]["id"]])) if frames else []
        selected_neighbors[number] = refs[:MAX_FRAMES_PER_NEIGHBOR]
    supplied = {frame["id"]: copy.deepcopy(frame) for frame in base}
    for refs in selected_neighbors.values():
        supplied.update({ref: copy.deepcopy(available[ref]) for ref in refs})
    roles = {ref: {"shot": frame.get("shot"), "roles": [], "anchor_asset_ids": [], "neighbor_for_shots": []}
             for ref, frame in supplied.items()}
    for ref, frame in supplied.items():
        if frame.get("shot") in current:
            roles[ref]["roles"].append("current_frame")
    canonical = {asset["id"]: asset for asset in inventory.get("assets", [])}
    anchors = {}
    for asset in context["proposed_inventory"]["assets"]:
        refs = [ref for ref in asset.get("evidence_ids", []) if ref in {frame["id"] for frame in base}][:2]
        anchors[asset["id"]] = refs
        for ref in refs:
            if "identity_anchor" not in roles[ref]["roles"]:
                roles[ref]["roles"].append("identity_anchor")
            roles[ref]["anchor_asset_ids"].append(asset["id"])
    allowed = {str(number): {key: [ref for ref in refs if supplied[ref].get("shot") != number]
                            for key, refs in anchors.items()} for number in current}
    visual = {row["shot"]: row for row in inv._visual_source_rows(timeline)}
    neighbors = []
    for relation in sorted(relations, key=lambda row: (row["for_shot"], positions[row["shot"]])):
        number, target = relation["shot"], relation["for_shot"]
        refs = [ref for ref in selected_neighbors[number]
                if _near_time(supplied[ref].get("timestamp_s"), current[target])]
        row = inventory.get("shots", {}).get(str(number), {})
        neighbors.append({**relation, "boundary_label": relation["scene_relation"],
                          "source_shot": copy.deepcopy(visual[number]), "evidence_ids": refs,
                          "asset_presence": copy.deepcopy(row.get("asset_presence", []))})
        for ref in refs:
            if "temporal_neighbor" not in roles[ref]["roles"]:
                roles[ref]["roles"].append("temporal_neighbor")
            roles[ref]["neighbor_for_shots"].append(target)
        for presence in row.get("asset_presence", []):
            key = presence.get("asset_id")
            if key not in canonical or presence.get("visibility") not in VISIBLE:
                continue
            own = set(presence.get("evidence_ids") or [])
            support = [ref for ref in refs if ref in own and supplied[ref].get("shot") == number]
            allowed[str(target)].setdefault(key, []).extend(support)
    for by_asset in allowed.values():
        for key in by_asset:
            by_asset[key] = list(dict.fromkeys(by_asset[key]))
    # All frames in a review batch are already supplied, including middle
    # frames and shots outside a target's temporal radius. A cited, visible
    # appearance of the SAME catalog asset is a usable identity anchor. It is
    # not an extra temporal neighbor and never proves the target's presence.
    # Previously only two endpoint frames were allowed despite sending all
    # batch frames, causing legitimate reviewer citations to fail validation.
    for number, row in inventory.get("shots", {}).items():
        for presence in row.get("asset_presence", []):
            key = presence.get("asset_id")
            if key not in canonical or presence.get("visibility") not in VISIBLE:
                continue
            refs = [ref for ref in presence.get("evidence_ids", []) if ref in supplied
                    and str(supplied[ref].get("shot")) == str(number)]
            for target in current:
                if str(target) != str(number):
                    allowed[str(target)].setdefault(key, []).extend(refs)
            for ref in refs:
                roles[ref].setdefault("appearance_asset_ids", []).append(key)
    for by_asset in allowed.values():
        for key in by_asset:
            by_asset[key] = list(dict.fromkeys(by_asset[key]))
    aliases, paths, unresolved = _alias_graph(inventory, canonical)
    context["temporal_context"] = {
        "policy": {"neighbor_radius": NEIGHBOR_RADIUS, "max_gap_seconds": MAX_GAP_SECONDS,
                   "max_frames_per_neighbor": MAX_FRAMES_PER_NEIGHBOR,
                   "source_facts": "untrusted_visual_context_only", "presence_from_neighbor": False,
                   "identity_rule": "Own current frames prove visible presence; contextual identity requires independently checked distinctive physical continuity, not shared costume or scene membership."},
        "neighbors": neighbors, "evidence_roles": roles, "allowed_identity_context": allowed,
        "preferred_neighbor_shots": {str(number): [row["shot"] for row in sorted(
            (row for row in relations if row["for_shot"] == number),
            key=lambda row: ({"same_scene": 0, "unknown_scene": 1, "scene_boundary": 2}[row["scene_relation"]], abs(row["offset"]), row["offset"]))]
            for number in current},
        "canonical_asset_ids": sorted(canonical), "canonical_aliases": aliases,
        "alias_paths": paths, "unresolved_alias_ids": unresolved,
    }
    return context, list(supplied.values())


def retain_supplied_identity_context(original, review, supplied):
    """Changing the proposed cast must not revoke pixels already given to QA.

    A newly corrected target appearance can become the latest anchor and push
    its earlier reveal out of the anchor sampler. Keep the original host-selected
    context whitelist for pixels still supplied; independent vision must still
    confirm the identity and current-shot presence.
    """
    temporal = review['temporal_context']
    known = set(temporal.get('canonical_asset_ids', []))
    cards = {frame['id']: frame for frame in supplied}
    allowed = temporal.setdefault('allowed_identity_context', {})
    for number, by_asset in original.get('temporal_context', {}).get('allowed_identity_context', {}).items():
        if number not in allowed:
            continue
        for key, refs in by_asset.items():
            if key not in known:
                continue
            kept = [ref for ref in refs if ref in cards and str(cards[ref].get('shot')) != str(number)]
            allowed[number][key] = list(dict.fromkeys(allowed[number].get(key, []) + kept))


def validate_context_links(links, batch, inventory, supplied, context):
    """Validate citation structure only; independent vision must judge identity.

    `inventory` may be the proposed shot patch: known IDs come from the host's
    original canonical catalog. A new or offscreen ID cannot gain presence from
    neighbors. This function never merges or mutates any identity/shot data.
    """
    if links is None:
        return [], []
    if not isinstance(links, list):
        return [{"code": "invalid_context_link", "shot": None, "message": "Context links must be a list."}], []
    wanted = {row["shot"] for row in batch}
    cards = {frame["id"]: frame for frame in supplied}
    temporal = context.get("temporal_context") or {}
    known = set(temporal.get("canonical_asset_ids") or [])
    allowed = temporal.get("allowed_identity_context") or {}
    issues, audit = [], []
    for link in links:
        number = link.get("shot") if isinstance(link, dict) else None
        number = number if type(number) is int and number in wanted else None
        errors = []
        if not isinstance(link, dict) or number is None:
            errors.append(("invalid_context_link", "Context link names an invalid or foreign current shot."))
        else:
            key = link.get("asset_id")
            if not isinstance(key, str) or key not in known:
                errors.append(("context_link_unknown_asset", "Context link requires an existing canonical asset ID."))
            presences = inventory.get("shots", {}).get(str(number), {}).get("asset_presence", [])
            if not any(p.get("asset_id") == key and p.get("visibility") in VISIBLE for p in presences):
                errors.append(("context_link_presence", "Asset must already be visible or partial in the current shot; context cannot create presence."))
            refs = link.get("current_evidence_ids")
            if not isinstance(refs, list) or not refs or any(
                    not isinstance(ref, str) or ref not in cards or cards[ref].get("shot") != number for ref in refs):
                errors.append(("context_link_current_evidence", "Identity continuity requires nonempty evidence from this exact current shot."))
            refs = link.get("context_evidence_ids")
            permitted = set(allowed.get(str(number), {}).get(key, [])) if isinstance(key, str) else set()
            if not isinstance(refs, list) or not refs or any(
                    not isinstance(ref, str) or ref not in permitted or ref not in cards or cards[ref].get("shot") == number for ref in refs):
                errors.append(("context_link_context_evidence", "Context references must be host-selected other-shot anchors or neighbors for this same asset."))
            if not isinstance(link.get("reason"), str) or not link["reason"].strip():
                errors.append(("context_link_reason", "Contextual identity requires a specific visible continuity explanation."))
        identity = ({'asset_id': link['asset_id']} if isinstance(link,dict) and isinstance(link.get('asset_id'),str) else {})
        issues.extend({"code": code, "shot": number, "message": message, **identity} for code, message in errors)
        audit.append({"link": copy.deepcopy(link), "valid": not errors, "errors": [code for code, _ in errors],
                      "validation_scope": "citation_structure_only", "authorizes_presence_or_merge": False})
    return issues, audit
