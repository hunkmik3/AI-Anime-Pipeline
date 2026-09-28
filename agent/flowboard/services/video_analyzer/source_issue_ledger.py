"""Canonical active questions and conservative repairs of proven source links.

History supplies question identity, never evidence that a current fact is fixed.
The input analysis/inventory is not mutated and no human approval is inferred.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata

MAX_HISTORY_NODES = 100_000
MAX_HISTORY_DEPTH = 48
MAX_RESOLUTION_NODES = 4096
PROTOCOL_CODES = {
    "invalid_resolution", "resolution_incomplete", "invalid_verification",
    "verification_missing", "verification_evidence", "inventory_call_failed",
    "source_refinement_failed", "source_protocol_review_failed", "invalid_review_request",
    "invalid_issue_record", "issue_history_unresolved",
}
STRUCTURAL_CODES = {
    "unknown_scene_asset", "unknown_holder", "unknown_asset", "invalid_evidence",
    "invalid_visual_update", "missing_own_shot_evidence", "invalid_asset",
    "invalid_relationship", "invalid_presence", "invalid_visibility", "invented_shot",
    "missing_shot", "missing_scene", "missing_evidence", "duplicate_presence",
    "unknown_contents", "foreign_evidence", "invalid_graphic_evidence",
    "invalid_identity_aliases", "unknown_alias_target", "identity_alias_cycle",
    "identity_alias_kind_conflict", "identity_alias_layer_conflict", "unknown_relationship",
    "invalid_shot_id", "invalid_shot_record", "unknown_scene", "unknown_scene_member",
    "unknown_evidence_reference", "invalid_evidence_shot", "invalid_identity_anchor",
    "invalid_context_link", "context_link_unknown_asset", "context_link_presence",
    "context_link_current_evidence", "context_link_context_evidence", "context_link_reason",
}
SCOPE_CODES = {"audio_not_checked", "continuous_motion_not_checked", "scope_sampling"}
UNCERTAINTY_CODES = {"uncertain_presence", "source_detail_unresolved", "context_identity_unconfirmed"}
WRAPPERS = {"source_refinement_unresolved", "resolution_incomplete", "invalid_resolution"}
ASSET_FIELDS = ("asset_id", "candidate_id", "target_id", "canonical_id")


def _category(code, history_problem=None):
    """Reporting taxonomy only; no category changes any verification gate."""
    if not isinstance(code, str) or code in PROTOCOL_CODES | STRUCTURAL_CODES | SCOPE_CODES or history_problem:
        return "technical"
    return "uncertainty" if code in UNCERTAINTY_CODES else "visual"


def _text(value):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value)).strip() if isinstance(value, str) else ""


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _shot(value):
    return int(value) if not isinstance(value, bool) and isinstance(value, (str, int)) and str(value).isdigit() else None


def _snapshot(value, seen=None, depth=0):
    """Keep audit content serializable even when a malformed in-memory graph cycles."""
    seen = set() if seen is None else seen
    if depth > MAX_HISTORY_DEPTH:
        return {"audit_note": "history depth limit"}
    if isinstance(value, (dict, list)):
        if id(value) in seen:
            return {"audit_note": "cyclic history reference"}
        seen = seen | {id(value)}
        if isinstance(value, dict):
            return {str(k): _snapshot(v, seen, depth + 1) for k, v in value.items()}
        return [_snapshot(v, seen, depth + 1) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return {"audit_note": "unsupported history value", "type": type(value).__name__}


def _asset_ids(finding):
    ids = {finding[field] for field in ASSET_FIELDS if isinstance(finding.get(field), str) and finding[field]}
    if isinstance(finding.get("asset_ids"), list):
        ids.update(value for value in finding["asset_ids"] if isinstance(value, str) and value)
    for field in ("current", "proposed"):
        value = finding.get(field)
        if isinstance(value, dict) and isinstance(value.get("id"), str):
            ids.add(value["id"])
    return sorted(ids)


def build_issue_ledger(analysis):
    """Return current canonical questions, compact provenance, and separate audit.

    Only source_verification.findings is active. Resolved historical questions
    are indexed for unwrapping, never added to the active ledger by themselves.
    """
    report = analysis.get("source_verification") or {}
    if not isinstance(report, dict):
        report = {"findings": [{"code": "invalid_issue_record", "shot": None,
                                "message": "Current source-verification report is not an object."}]}
    index, paths, seen = {}, {}, set()
    limit_hit = False

    def scan(value, path, depth=0):
        nonlocal limit_hit
        if not isinstance(value, (dict, list)) or id(value) in seen:
            return
        if len(seen) >= MAX_HISTORY_NODES or depth > MAX_HISTORY_DEPTH:
            limit_hit = True
            return
        seen.add(id(value))
        if isinstance(value, dict):
            if isinstance(value.get("finding_id"), str) and ("message" in value or "code" in value):
                index.setdefault(value["finding_id"], []).append(value)
                paths[id(value)] = path
            for key, child in value.items():
                scan(child, f"{path}.{key}", depth + 1)
        else:
            for number, child in enumerate(value):
                scan(child, f"{path}[{number}]", depth + 1)

    # These are host report/audit containers, not arbitrary story/asset text.
    scan(report, "source_verification")
    for field in ("source_refinement_history", "source_protocol_review_history"):
        scan(analysis.get(field) or [], field)

    def root(node, active_shot, visited=None, depth=0, budget=None):
        budget = [MAX_RESOLUTION_NODES] if budget is None else budget
        budget[0] -= 1
        if budget[0] < 0:
            return node, [], "prior-question resolution budget exceeded"
        visited = set() if visited is None else visited
        if id(node) in visited or depth > MAX_HISTORY_DEPTH:
            return node, [], "cyclic or excessively deep prior-question chain"
        visited = visited | {id(node)}
        code = node.get("code")
        if not isinstance(code, str):
            return node, [], "prior question has no valid code"
        if code not in WRAPPERS:
            return node, [], None
        embedded = node.get("prior_question")
        if isinstance(embedded, dict):
            if active_shot is not None and embedded.get("shot") is not None and _shot(embedded.get("shot")) != active_shot:
                return node, [], "prior question belongs to another shot"
            resolved, chain, problem = root(embedded, active_shot, visited, depth + 1, budget)
            return resolved, ["embedded prior_question"] + chain, problem
        links = [node.get("prior_finding_id")]
        if isinstance(node.get("resolution"), dict):
            links.append(node["resolution"].get("finding_id"))
        for link in links:
            if not isinstance(link, str):
                continue
            candidates = [candidate for candidate in index.get(link, [])
                          if id(candidate) not in visited and
                          (active_shot is None or candidate.get("shot") is None or _shot(candidate.get("shot")) == active_shot)]
            leaves = []
            for candidate in candidates:
                leaf, chain, problem = root(candidate, active_shot, visited, depth + 1, budget)
                if not problem:
                    leaves.append((leaf, [paths.get(id(candidate), "historical finding")] + chain))
            signatures = {(_shot(leaf.get("shot")), _text(leaf.get("code")), _text(leaf.get("message")),
                           _text(leaf.get("field")), tuple(_asset_ids(leaf)))
                          for leaf, _ in leaves}
            if len(signatures) == 1:
                leaf, chain = leaves[0]
                return leaf, chain, None
            if len(signatures) > 1:
                return node, [], "ambiguous historical finding ID"
        # Unresolved wrappers retain the original concrete question directly.
        # This restores its code, not its truth or a prior acceptance decision.
        original_code = node.get("original_code")
        if (code == "source_refinement_unresolved" and isinstance(original_code, str)
                and original_code not in WRAPPERS and _text(node.get("message"))):
            return {**node, "code": original_code}, [], None
        return node, [], "prior question is unavailable"

    active = report.get("findings", [])
    if not isinstance(active, list):
        active = [{"code": "invalid_issue_record", "shot": None,
                   "message": "Current report findings is not a list."}]
    issues, audit, unresolved_links, provenance_excess = {}, [], 0, {}
    for number, original in enumerate(active):
        path = f"source_verification.findings[{number}]"
        node = original if isinstance(original, dict) else {
            "code": "invalid_issue_record", "shot": None, "message": "Current report contains a non-object finding."}
        leaf, chain, problem = root(node, _shot(node.get("shot")))
        if problem:
            unresolved_links += 1
        code = leaf.get("code") if isinstance(leaf.get("code"), str) else "invalid_issue_record"
        message = _text(leaf.get("message")) or "Current finding has no concrete message; source review is required."
        shot = _shot(node.get("shot") if node.get("shot") is not None else leaf.get("shot"))
        if node.get("shot") is not None and _shot(node["shot"]) is None:
            code = "invalid_issue_record"
            problem = "current finding has an invalid shot ID"
        ids = _asset_ids(leaf)
        # Incomplete old wrappers may omit an ID still explicit on the active
        # finding. Its identity distinction must not disappear during unwrap.
        ids = sorted(set(ids) | set(_asset_ids(node)))
        signature = {"shot": shot, "code": code, "asset_ids": ids, "message": message}
        if isinstance(leaf.get("field") or node.get("field"), str):
            signature["field"] = leaf.get("field") or node.get("field")
        key = "issue-" + _hash(signature)[:24]
        category = _category(code, problem)
        issue = issues.setdefault(key, {"finding_id": key, **signature, "category": category,
                                      "provenance": {"count": 0, "source_paths": []}})
        for field in ASSET_FIELDS:
            value = leaf.get(field) or node.get(field)
            if isinstance(value, str) and value:
                issue[field] = value
        # A canonical question is a compacted group of original occurrences,
        # not a new occurrence each time the ledger is rebuilt. Recover its
        # compact provenance from either the active row or an unwrapped root.
        source_paths, count = [path], 1
        for candidate in (node, leaf):
            provenance = candidate.get("provenance")
            if candidate.get("finding_id") != key or not isinstance(provenance, dict):
                continue
            prior_paths, prior_count = provenance.get("source_paths"), provenance.get("count")
            if (isinstance(prior_paths, list) and prior_paths and all(isinstance(p, str) for p in prior_paths)
                    and isinstance(prior_count, int) and not isinstance(prior_count, bool)
                    and prior_count >= len(set(prior_paths))):
                source_paths, count = list(dict.fromkeys(prior_paths)), prior_count
                break
        issue["provenance"]["source_paths"] = list(dict.fromkeys(issue["provenance"]["source_paths"] + source_paths))
        extras = provenance_excess.setdefault(key, {})
        extras[_hash({"source_paths": sorted(source_paths), "count": count})] = count - len(source_paths)
        issue["provenance"]["count"] = len(issue["provenance"]["source_paths"]) + sum(extras.values())
        if problem:
            issue["history_problem"] = problem
        audit.append({"finding_id": key, "source_path": path, "history_paths": chain,
                      "history_problem": problem, "active_finding": _snapshot(original),
                      "root_finding": _snapshot(leaf)})
    values = sorted(issues.values(), key=lambda row: (row["shot"] is None, row["shot"] or 0, row["finding_id"]))
    return {"issues": values, "stats": {"active_findings": len(active), "canonical_issues": len(values),
        "merged_duplicates": len(active) - len(values), "duplicates_collapsed": len(active) - len(values),
        "visual_claims": sum(i["category"] in {"visual", "uncertainty"} for i in values),
        "uncertainty_issues": sum(i["category"] == "uncertainty" for i in values),
        "protocol_issues": sum(i["category"] == "technical" and i["code"] not in SCOPE_CODES for i in values),
        "scope_notes": sum(i["code"] in SCOPE_CODES for i in values), "unresolved_history_links": unresolved_links,
        "history_nodes": len(seen), "history_limit_reached": limit_hit}, "audit": audit}


def refresh_report_metadata(report):
    """Refresh reporting labels/counts without rewriting facts or verdicts.

    The initial input audit is the sole duplicate-count cohort. Later protocol
    passes may revisit the same questions, so their counts are not accumulated.
    Every changed prior label/count is retained outside the active findings.
    """
    result = copy.deepcopy(report)
    changes = []

    def update(container, key, value, path):
        if key in container and container[key] == value:
            return
        changes.append({"path": path, "had_value": key in container,
                        "before": copy.deepcopy(container.get(key)), "after": copy.deepcopy(value)})
        container[key] = value

    rows = result.get("findings", [])
    rows = rows if isinstance(rows, list) else []
    for number, row in enumerate(rows):
        if isinstance(row, dict):
            update(row, "category", _category(row.get("code"), row.get("history_problem")),
                   f"findings[{number}].category")
    summary = result.get("issue_summary")
    if not isinstance(summary, dict):
        update(result, "issue_summary", {}, "issue_summary")
        summary = result["issue_summary"]
    if isinstance(summary, dict):
        counts = {category: sum(isinstance(row, dict) and row.get("category") == category for row in rows)
                  for category in ("technical", "visual", "uncertainty")}
        update(summary, "by_category", counts, "issue_summary.by_category")
        scope_count = sum(isinstance(row, dict) and isinstance(row.get("code"), str) and row["code"] in SCOPE_CODES for row in rows)
        for field, value in {
            "visual_claims": counts["visual"] + counts["uncertainty"],
            "uncertainty_issues": counts["uncertainty"],
            "protocol_issues": counts["technical"] - scope_count,
            "scope_notes": scope_count,
        }.items():
            if field in summary:
                update(summary, field, value, f"issue_summary.{field}")
        audit = result.get("issue_audit")
        initial = audit.get("input") if isinstance(audit, dict) else None
        if isinstance(initial, list):
            update(summary, "input_findings", len(initial), "issue_summary.input_findings")
            # Malformed entries have no proven shared identity; count each
            # separately instead of treating every absent ID as a duplicate.
            identities = {(0, row["finding_id"]) if isinstance(row, dict) and
                          isinstance(row.get("finding_id"), str) and row["finding_id"] else (1, number)
                          for number, row in enumerate(initial)}
            duplicates = len(initial) - len(identities)
            for field in ("duplicates_collapsed", "merged_duplicates"):
                update(summary, field, duplicates, f"issue_summary.{field}")
    if changes:
        audit = result.setdefault("metadata_audit", [])
        entry = {"kind": "issue_reporting_refresh", "version": 1, "changes": changes}
        if isinstance(audit, list):
            audit.append(entry)
        else:
            # Preserve a pre-existing non-list audit container verbatim.
            result["metadata_audit"] = [{"prior_metadata_audit": audit}, entry]
    return result


def repair_inventory_references(inventory, evidence):
    """Repair only proven joins and redundant citations; unknown facts stay open."""
    result = copy.deepcopy(inventory)
    actions, findings = [], []
    definitions = {a["id"]: a for a in result.get("assets", []) + result.get("screen_graphics", [])}
    definition_layers = {a["id"]: field for field in ("assets", "screen_graphics") for a in result.get(field, [])}
    raw_aliases = result.get("identity_aliases") or {}
    aliases = {}

    def affected(asset_id):
        related = {asset_id} if isinstance(asset_id, str) else set()
        for _ in range(len(definitions)):
            previous = set(related)
            related.update(aid for aid, asset in definitions.items() if any(
                isinstance(refs, list) and any(isinstance(ref, str) and ref in related for ref in refs)
                for refs in (asset.get("member_ids", []), asset.get("depends_on_asset_ids", []))))
            if previous == related: break
        return [int(n) for n, row in result.get("shots", {}).items()
                if str(n).isdigit() and isinstance(row, dict) and any(
                    (isinstance(p.get("asset_id"), str) and p["asset_id"] in related) or
                    (isinstance(p.get("holder_id"), str) and p["holder_id"] in related) or
                    (isinstance(p.get("contains_ids"), list) and any(isinstance(ref, str) and ref in related for ref in p["contains_ids"]))
                    for field in ("asset_presence", "screen_graphics")
                    for p in (row.get(field, []) if isinstance(row.get(field, []), list) else []) if isinstance(p, dict))] or [None]

    def issue(code, message, asset_id=None, shot=None):
        findings.extend({"code": code, "message": message, "shot": n, **({"asset_id": asset_id} if isinstance(asset_id, str) and asset_id else {})}
                        for n in ([shot] if shot is not None else affected(asset_id)))

    if not isinstance(raw_aliases, dict):
        issue("invalid_identity_aliases", "Identity aliases must be a mapping; no aliases were applied.")
        raw_aliases = {}
    for original in raw_aliases:
        current, visited = original, set()
        while isinstance(current, str) and current in raw_aliases and current not in visited:
            visited.add(current)
            target = raw_aliases[current]
            if target == current: break
            current = target
        if not isinstance(current, str) or current not in definitions:
            issue("unknown_alias_target", "A proven-identity mapping has no current target definition; references were retained.", original)
        elif current in visited and raw_aliases.get(current) != current:
            issue("identity_alias_cycle", "Identity aliases form a cycle; no identity was guessed.", original)
        elif original in definitions and definitions[original].get("kind") != definitions[current].get("kind"):
            issue("identity_alias_kind_conflict", "An alias crosses source asset kinds; references were retained.", original)
        elif original in definitions and definition_layers[original] != definition_layers[current]:
            issue("identity_alias_layer_conflict", "An alias crosses physical and graphic layers; references were retained.", original)
        elif current != original:
            aliases[original] = current

    def remap(value, where):
        target = aliases.get(value, value) if isinstance(value, str) else value
        if target != value:
            actions.append({"action": "canonical_alias", "path": where, "from": value, "to": target})
        return target

    for field in ("assets", "screen_graphics"):
        kept = []
        for asset in result.get(field, []):
            if asset["id"] in aliases:
                result.setdefault("identity_alias_originals", {}).setdefault(asset["id"], copy.deepcopy(asset))
                actions.append({"action": "retire_proven_alias_definition", "asset_id": asset["id"],
                                "canonical_id": aliases[asset["id"]], "original": copy.deepcopy(asset)})
                continue
            for relation in ("member_ids", "depends_on_asset_ids"):
                if relation not in asset:
                    continue
                refs = asset.get(relation, [])
                if not isinstance(refs, list):
                    issue("invalid_relationship", "Asset relationship must be a list; original value retained.", asset["id"])
                    continue
                if any(not isinstance(ref, str) for ref in refs):
                    issue("invalid_relationship", "Asset relationship contains a non-ID value; original value retained.", asset["id"])
                    continue
                asset[relation] = list(dict.fromkeys(remap(ref, f"{field}.{asset['id']}.{relation}") for ref in refs))
                for ref in asset[relation]:
                    if not isinstance(ref, str) or ref not in definitions or ref == asset["id"]:
                        issue("unknown_relationship", "Asset relationship has an unknown or self-referential ID.", asset["id"])
            kept.append(asset)
        if field in result: result[field] = kept
    definitions = {a["id"]: a for a in result.get("assets", []) + result.get("screen_graphics", [])}
    physical_ids = {a["id"] for a in result.get("assets", [])}
    graphic_ids = {a["id"] for a in result.get("screen_graphics", [])}
    evidence_by_id, ambiguous_evidence = {}, set()
    for frame in evidence:
        if not isinstance(frame, dict) or not isinstance(frame.get("id"), str): continue
        if frame["id"] in evidence_by_id and evidence_by_id[frame["id"]] != frame:
            ambiguous_evidence.add(frame["id"])
        evidence_by_id[frame["id"]] = frame
    scenes = {s["id"]: s for s in result.get("scenes", [])}
    for number, row in result.get("shots", {}).items():
        if not str(number).isdigit():
            issue("invalid_shot_id", "Source inventory contains a non-numeric shot ID.")
            continue
        n = int(number)
        if not isinstance(row, dict):
            issue("invalid_shot_record", "Source shot inventory is not an object; original value retained.", shot=n)
            continue
        if not isinstance(row.get("scene_id"), str) or row["scene_id"] not in scenes:
            issue("unknown_scene", "Shot references an unknown scene; no scene was invented.", shot=n)
        for field in ("asset_presence", "screen_graphics"):
            if not isinstance(row.get(field, []), list):
                issue("invalid_presence", "Shot presence must be a list; original value retained.", shot=n)
                continue
            for presence in row.get(field, []):
                if not isinstance(presence, dict):
                    issue("invalid_presence", "Presence record is not an object; original value retained.", shot=n)
                    continue
                aid = remap(presence.get("asset_id"), f"shots.{number}.{field}.asset_id")
                if "asset_id" in presence: presence["asset_id"] = aid
                if not isinstance(aid, str) or aid not in definitions:
                    issue("unknown_asset", "Presence references an unknown asset; original ID retained.", aid, n)
                if presence.get("holder_id"):
                    presence["holder_id"] = remap(presence["holder_id"], f"shots.{number}.{field}.holder_id")
                    if not isinstance(presence["holder_id"], str) or presence["holder_id"] not in definitions:
                        issue("unknown_holder", "Holder identity is unknown; no substitute was guessed.", aid, n)
                refs = presence.get("contains_ids", [])
                if isinstance(refs, list) and all(isinstance(ref, str) for ref in refs):
                    mapped = list(dict.fromkeys(remap(ref, f"shots.{number}.{field}.contains_ids") for ref in refs))
                    if "contains_ids" in presence: presence["contains_ids"] = mapped
                    if any(ref not in definitions for ref in mapped):
                        issue("unknown_contents", "Container refers to unknown contents; original IDs retained.", aid, n)
                else:
                    issue("unknown_contents", "Contents references are malformed; original value retained.", aid, n)
                refs = presence.get("evidence_ids", [])
                if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
                    issue("invalid_evidence", "Presence evidence must be a list of source-frame IDs.", aid, n)
                    continue
                known = [ref for ref in refs if ref in evidence_by_id and ref not in ambiguous_evidence]
                own = [ref for ref in known if _shot(evidence_by_id[ref].get("shot")) == n]
                foreign = [ref for ref in known if _shot(evidence_by_id[ref].get("shot")) not in (None, n)]
                if len(known) != len(refs):
                    issue("unknown_evidence_reference", "Presence cites unknown or ambiguous frame evidence; citation retained.", aid, n)
                if any(_shot(evidence_by_id[ref].get("shot")) is None for ref in known):
                    issue("invalid_evidence_shot", "Presence citation has no valid source shot; citation retained.", aid, n)
                if own and foreign:
                    anchor_owners = [presence] + ([definitions[aid]] if isinstance(aid, str) and aid in definitions else [])
                    if any(not isinstance(owner.get("identity_anchor_evidence_ids", []), list) or
                           any(not isinstance(ref, str) for ref in owner.get("identity_anchor_evidence_ids", []))
                           for owner in anchor_owners):
                        issue("invalid_identity_anchor", "Existing anchor metadata is malformed; presence citations retained.", aid, n)
                        continue
                    presence["evidence_ids"] = [ref for ref in refs if ref not in foreign]
                    for owner in anchor_owners:
                        owner["identity_anchor_evidence_ids"] = list(dict.fromkeys(owner.get("identity_anchor_evidence_ids", []) + foreign))
                    actions.append({"action": "separate_identity_anchor_citations", "shot": n, "asset_id": aid,
                        "before": refs, "presence_evidence_ids": copy.deepcopy(presence["evidence_ids"]),
                        "identity_anchor_evidence_ids": foreign})
                elif not own and (foreign or presence.get("visibility") in ("visible", "partial")):
                    issue("missing_own_shot_evidence", "No valid own-shot presence citation survives; foreign evidence was not removed or treated as presence proof.", aid, n)
    for scene_id, scene in scenes.items():
        timeline = [(int(n), row) for n, row in result.get("shots", {}).items()
                    if str(n).isdigit() and isinstance(row, dict) and row.get("scene_id") == scene_id]
        for field, known_ids in (("present_asset_ids", physical_ids), ("screen_graphic_ids", graphic_ids)):
            refs = scene.get(field, [])
            if not isinstance(refs, list) or any(not isinstance(ref, str) or (ref not in known_ids and ref not in aliases) for ref in refs):
                for n, _ in timeline or [(None, None)]:
                    issue("unknown_scene_member", "Original scene membership contains an unknown ID; timeline was rebuilt with the original list preserved in audit.", shot=n)
        before = {field: copy.deepcopy(scene.get(field)) for field in ("shot_ids", "present_asset_ids", "screen_graphic_ids")}
        scene["shot_ids"] = sorted(n for n, _ in timeline)
        scene["present_asset_ids"] = list(dict.fromkeys(p["asset_id"] for _, row in timeline
            for p in (row.get("asset_presence", []) if isinstance(row.get("asset_presence", []), list) else [])
            if isinstance(p, dict) and isinstance(p.get("asset_id"), str) and p["asset_id"] in physical_ids))
        if "screen_graphic_ids" in scene or graphic_ids:
            scene["screen_graphic_ids"] = list(dict.fromkeys(p["asset_id"] for _, row in timeline
                for p in (row.get("screen_graphics", []) if isinstance(row.get("screen_graphics", []), list) else [])
                if isinstance(p, dict) and isinstance(p.get("asset_id"), str) and p["asset_id"] in graphic_ids))
        after = {field: copy.deepcopy(scene.get(field)) for field in before}
        if before != after:
            actions.append({"action": "rebuild_scene_membership", "scene_id": scene_id, "before": before, "after": after})
    findings = list({_hash(f): f for f in findings}.values())
    return result, actions, findings
