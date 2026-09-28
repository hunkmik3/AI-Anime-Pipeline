"""Evidence-backed input and per-shot output contracts for the prompt agent.

These checks establish data completeness, not visual truth. Source verification
comes from the source-frame agent; a separate model reviews prompt semantics.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from flowboard.services import production_adaptation


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), default=str).encode()).hexdigest()


def prompt_digest(prompt: str) -> str:
    return hashlib.sha256(prompt.strip().encode()).hexdigest()


def _source_digest(value: Any) -> str:
    # Same canonical form as video_analyzer.source_inventory, whose observations
    # remain immutable while production_key/production_name provide aliases.
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     default=str).encode()).hexdigest()


def contract_digest(sequence: dict, shots: list[dict], characters: list[dict],
                    environment: dict | None, production_assets: list[dict] | None = None,
                    reference_assets: list[dict] | None = None,
                    source_verification: dict | None = None, **options: Any) -> str:
    """Hash the actual writing inputs, including reference order and locations."""
    return _digest({"sequence": sequence, "shots": shots, "characters": characters,
                    "environment": environment, "production_assets": production_assets,
                    "reference_assets": reference_assets or [],
                    "source_verification": source_verification, "options": options})


def is_strict(production_assets: Any, source_verification: Any) -> bool:
    """Whether a board's clips must carry a verified-source coverage receipt.

    Strict once the board comes from a source inventory: it has production
    assets, or a source report. A report that only says the video predates the
    inventory ("legacy_analysis", status "legacy") is not a contract — such a
    board has nothing the strict path could check it against.
    """
    if production_assets:
        return True
    if not isinstance(source_verification, dict) or not source_verification:
        return False
    codes = {f.get("code") for f in source_verification.get("findings") or [] if isinstance(f, dict)}
    return source_verification.get("status") != "legacy" and "legacy_analysis" not in codes


def asset_id(asset: dict) -> str:
    return str(asset.get("source_asset_id") or asset.get("asset_id") or asset.get("id") or asset.get("key") or "")


def source_shot_ids(shot: dict) -> list[str]:
    raw = shot.get("source_shots") or [shot.get("source_shot")]
    return [str(x) for x in raw if x is not None and str(x)]


def source_readiness_issues(shots: list[dict], source_verification: dict | None) -> list[str]:
    """Judge this clip without promoting the whole film or accepting findings.

    A sampled-frame review may finish individual shots while another part of
    the film still needs review. Keep that report intact in the signed contract
    and require every selected source shot to have a settled machine verdict.
    The older fully-verified path remains compatible with reports predating
    the explicit scope label; partial-film readiness requires that label.
    """
    report = source_verification or {}
    if report.get("method") == "authored_script":
        from flowboard.services.authored_contract import readiness
        return readiness(shots, report)
    issues: list[str] = []
    status = report.get("status")
    if status not in {"verified", "needs_review"} or report.get("method") != "source_frames":
        issues.append("Source analysis must be verified against source_frames before writing.")
    if not report.get("digest"):
        issues.append("Source verification has no digest; verify the current source inventory first.")
    if status == "needs_review" and not str(report.get("scope") or "").strip():
        issues.append("Partially verified source analysis has no verification scope.")
    selected: set[str] = set()
    for index, shot in enumerate(shots, 1):
        sources = source_shot_ids(shot)
        if not sources:
            issues.append(f"Shot {index} has no source_shots mapping.")
        selected.update(sources)
    if not selected:
        issues.append("No source shots were selected for verification.")
    reviewed = {str(n) for n in report.get("reviewed_shots") or []}
    unresolved = {str(n) for n in report.get("unresolved_shots") or []}
    for source in sorted(selected):
        if source not in reviewed or source in unresolved:
            issues.append(f"Source shot {source} is not verified.")
    accepted = (status == "verified" and isinstance(report.get("review"), dict)
                and bool(report["review"].get("accepted_by")))
    for finding in report.get("findings") or []:
        if not isinstance(finding, dict):
            issues.append("Source verification contains an unscoped finding.")
            continue
        if finding.get("accepted") is True and accepted:
            continue  # Retain an existing, explicit human acceptance only.
        source = finding.get("shot")
        scoped = source is not None and str(source).isdigit() and int(source) > 0
        if not scoped or str(source) in selected:
            label = f"shot {source}" if scoped else "the source analysis"
            issues.append(f"Active source finding for {label}: {finding.get('code') or 'unresolved finding'}.")
    if issues and status == "needs_review":
        issues.insert(0, "Source analysis must be verified against source_frames before writing.")
    return list(dict.fromkeys(issues))


def scope_model_context(shots: list[dict], production_assets: list[dict],
                        source_verification: dict, references: list[dict]) -> tuple[list[dict], dict]:
    """Scope model tokens to this clip, after hashing/validating the full inputs."""
    assets = {asset_id(a): a for a in production_assets}
    sources = {n for shot in shots for n in source_shot_ids(shot)}
    needed = {asset_id(r) for r in references if asset_id(r) in assets}
    evidence_ids: set[str] = set()
    for shot in shots:
        for item in shot.get("source_evidence") or []:
            evidence_ids.add(str(item.get("id") if isinstance(item, dict) else item))
        for p in shot.get("asset_presence") or []:
            needed.add(str(p.get("asset_id") or ""))
            if p.get("holder_id"):
                needed.add(str(p["holder_id"]))
            needed.update(str(x) for x in p.get("contains_ids") or [])
            evidence_ids.update(str(x) for x in p.get("evidence_ids") or [])
        for key in shot.get("character_keys") or []:
            needed.update(aid for aid, asset in assets.items()
                          if key in {aid, asset.get("production_key")})
    pending = list(needed)
    while pending:
        asset = assets.get(pending.pop(), {})
        for key in list(asset.get("member_ids") or []) + list(asset.get("depends_on_asset_ids") or []):
            if key not in needed:
                needed.add(key)
                pending.append(key)
    scoped_assets = [a for a in production_assets if asset_id(a) in needed]
    report = {key: source_verification[key] for key in ("status", "method", "scope", "digest", "inventory_digest")
              if key in source_verification}
    report["reviewed_shots"] = [n for n in source_verification.get("reviewed_shots") or [] if str(n) in sources]
    report["unresolved_shots"] = [n for n in source_verification.get("unresolved_shots") or [] if str(n) in sources]
    report["evidence"] = [e for e in source_verification.get("evidence") or [] if str(e.get("id")) in evidence_ids]
    report["findings"] = [f for f in source_verification.get("findings") or [] if str(f.get("shot")) in sources]
    report["scope_notes"] = [f for f in source_verification.get("scope_notes") or []
                             if f.get("shot") is None or str(f.get("shot")) in sources]
    for field, allowed in (("shot_digests", sources), ("asset_digests", needed)):
        if isinstance(source_verification.get(field), dict):
            report[field] = {str(k): v for k, v in source_verification[field].items() if str(k) in allowed}
    return scoped_assets, report


def reference_slots(references: list[dict]) -> tuple[list[dict], list[str]]:
    """One image slot may bind several distinct assets in an explicit atlas.

    Binding metadata stays per asset in the contract. Only the image transport
    is shared, and only if every binding agrees on its URL and media identity.
    """
    slots: dict[str, dict] = {}
    bound: set[str] = set()
    issues: list[str] = []
    for reference in references:
        label = reference.get("ref_label")
        if not label:
            continue
        if not isinstance(label, str) or not re.fullmatch(r"@image[1-9]\d*", label):
            issues.append(f"Invalid reference label {label!r}; use @image1...@imageN.")
            continue
        url = reference.get("ref_url")
        if not url:
            issues.append(f"Reference {label} has no image URL.")
        aid = asset_id(reference)
        if aid and aid in bound:
            issues.append(f"Asset {aid!r} has duplicate reference bindings.")
        if aid:
            bound.add(aid)
        if label in slots:
            first = slots[label]
            if first.get("ref_url") != url or (first.get("media_id") or "") != (reference.get("media_id") or ""):
                issues.append(f"Reference {label} has conflicting image URLs or media IDs.")
            if not aid or not all(asset_id(entry) for entry in first["asset_bindings"]):
                issues.append(f"Shared reference {label} requires an explicit asset ID for every binding.")
            first["asset_bindings"].append(reference)
        else:
            slots[label] = {"ref_label": label, "ref_url": url,
                            "media_id": reference.get("media_id"), "asset_bindings": [reference]}
    ordered = sorted(slots.values(), key=lambda item: int(item["ref_label"][6:]))
    if [item["ref_label"] for item in ordered] != [f"@image{i}" for i in range(1, len(ordered) + 1)]:
        issues.append("Reference image slots must be contiguous @image1...@imageN.")
    if len(ordered) > 9:
        issues.append(f"Seedance supports at most 9 reference image slots; this clip has {len(ordered)}. Combine related assets into a labeled atlas.")
    return ordered, list(dict.fromkeys(issues))


def validate_source_contract(shots: list[dict], production_assets: list[dict],
                             source_verification: dict | None,
                             references: list[dict]) -> list[str]:
    """Fail closed on incomplete provenance or references, without inventing it."""
    if (source_verification or {}).get("method") == "authored_script":
        from flowboard.services.authored_contract import validate
        return validate(shots, production_assets, source_verification, references)
    issues = source_readiness_issues(shots, source_verification)
    report = source_verification or {}
    # A reviewer who accepted the verifier's findings accepted the presences it
    # could not settle, too. Uncertain then asserts nothing on screen: it stops
    # blocking, and build_requirements asks no coverage for it.
    accepted = (report.get("status") == "verified" and isinstance(report.get("review"), dict)
                and bool(report["review"].get("accepted_by")))
    evidence = {str(e.get("id")): e for e in report.get("evidence") or []
                if isinstance(e, dict) and e.get("id")}
    assets = {asset_id(a): a for a in production_assets if isinstance(a, dict) and asset_id(a)}
    if len(assets) != len(production_assets):
        issues.append("Production asset IDs must be non-empty and unique.")
    asset_digests = report.get("asset_digests")
    if isinstance(asset_digests, dict):
        for key, asset in assets.items():
            observed = {k: v for k, v in asset.items() if k not in {"production_key", "production_name"}}
            if asset_digests.get(key) != _source_digest(observed):
                issues.append(f"Source asset {key!r} changed after source verification; reverify it.")
    referenced = {asset_id(r) for r in references if r.get("ref_label") and r.get("ref_url")}
    _slots, reference_issues = reference_slots(references)
    issues.extend(reference_issues)
    used_visible: set[str] = set()
    for i, shot in enumerate(shots, 1):
        issues.extend(f"Shot {i}: {problem}" for problem in production_adaptation.validate(shot, report))
        sources = source_shot_ids(shot)
        if not sources:
            issues.append(f"Shot {i} has no source_shots mapping.")
        source_digests = report.get("shot_digests")
        if isinstance(source_digests, dict):
            appearances = shot.get("source_appearances") or shot.get("source_asset_presence") or []
            observed_sources: set[str] = set()
            flattened = []
            for appearance in appearances:
                if not isinstance(appearance, dict):
                    issues.append(f"Shot {i}: invalid source appearance.")
                    continue
                source = str(appearance.get("source_shot") or "")
                observation = {k: v for k, v in appearance.items() if k != "source_shot"}
                if source in observed_sources or source not in sources or source_digests.get(source) != _source_digest(observation):
                    issues.append(f"Shot {i}: source appearance {source!r} changed after verification.")
                observed_sources.add(source)
                flattened += [{**p, "source_shot": int(source) if source.isdigit() else source}
                              for p in observation.get("asset_presence") or []]
            if observed_sources != set(sources):
                issues.append(f"Shot {i}: source appearances must cover every mapped source shot.")
            if flattened != shot.get("asset_presence"):
                issues.append(f"Shot {i}: asset presence changed from its verified source appearances.")
        shot_evidence = shot.get("source_evidence") or []
        if not shot_evidence:
            issues.append(f"Shot {i} has no source_evidence.")
        cited: set[str] = set()
        for item in shot_evidence:
            eid = str(item.get("id") if isinstance(item, dict) else item)
            found = evidence.get(eid)
            if not found or str(found.get("shot")) not in sources:
                issues.append(f"Shot {i}: evidence {eid!r} does not belong to a mapped source shot.")
            else:
                cited.add(str(found.get("shot")))
        if set(sources) - cited:
            issues.append(f"Shot {i}: each merged source shot needs its own source-frame evidence.")
        presence = shot.get("asset_presence")
        if not isinstance(presence, list):
            issues.append(f"Shot {i} has no asset_presence inventory.")
            presence = []
        seen: set[str] = set()
        seen_appearances: set[tuple[str, str]] = set()
        for p in presence:
            if not isinstance(p, dict):
                issues.append(f"Shot {i}: every asset_presence item must be an object.")
                continue
            key = str(p.get("asset_id") or "")
            if key not in assets:
                issues.append(f"Shot {i}: unknown asset {key!r}.")
            source = str(p.get("source_shot") or "")
            occurrence = (key, source)
            if occurrence in seen_appearances:
                issues.append(f"Shot {i}: duplicate asset presence {key!r} in source {source!r}.")
            seen_appearances.add(occurrence)
            seen.add(key)
            visibility = p.get("visibility")
            if visibility not in {"visible", "partial", "occluded", "offscreen"} and not (
                    visibility == "uncertain" and accepted):
                issues.append(f"Shot {i}: resolve uncertain visibility for {key!r} against the source.")
            if visibility in {"visible", "partial", "occluded"}:
                used_visible.add(key)
            for related in ([p.get("holder_id")] if p.get("holder_id") else []) + list(p.get("contains_ids") or []):
                if related not in assets:
                    issues.append(f"Shot {i}: {key!r} relates to unknown asset {related!r}.")
            for eid in p.get("evidence_ids") or []:
                found = evidence.get(str(eid))
                own_shot_required = visibility in {"visible", "partial"}
                if not found or (own_shot_required and str(found.get("shot")) not in sources):
                    issues.append(f"Shot {i}: {key!r} cites unrelated evidence {eid!r}.")
        for key in shot.get("scene_present_asset_ids") or []:
            if key not in seen:
                issues.append(f"Shot {i}: scene asset {key!r} needs explicit visibility, including offscreen/occluded.")
        for character_key in shot.get("character_keys") or []:
            candidates = [key for key, asset in assets.items()
                          if character_key in {key, asset.get("production_key")}]
            if not candidates or not any(key in seen for key in candidates):
                issues.append(f"Shot {i}: character {character_key!r} has no source-backed asset presence.")
    for key in sorted(used_visible):
        if assets.get(key, {}).get("reference_required") and key not in referenced:
            issues.append(f"Required reference image is missing for asset {key!r}.")
    return issues


def appearance_facts(appearances: list[dict]) -> list[dict]:
    """Renderable asset facts in chronological order, not source UI overlays.

    The complete observations, including screen_graphics, remain digest-checked
    source provenance. Their watermarks/captions are not target-film demands.
    """
    return [{"source_shot": a.get("source_shot"), "asset_presence": a.get("asset_presence") or []}
            for a in appearances]


def build_requirements(rows: list[dict], shots: list[dict], production_assets: list[dict]) -> list[dict]:
    """Every supplied fact survives. The model may paraphrase, but cannot drop it."""
    assets = {asset_id(a): a for a in production_assets}
    result: list[dict] = []
    for row, shot in zip(rows, shots):
        shot = production_adaptation.apply(shot)
        n = row["shot"]
        for index, p in enumerate(shot.get("asset_presence") or [], 1):
            if p.get("visibility") == "uncertain":
                continue  # only an accepted review lets one through; it asserts nothing
            key = p["asset_id"]
            result.append({"id": f"shot:{n}:asset:{key}:{index}", "shot": n, "kind": "presence",
                           "name": assets.get(key, {}).get("production_name") or assets.get(key, {}).get("name") or key,
                           **p})
        for field in ("framing", "placement"):
            if row.get(field):
                result.append({"id": f"shot:{n}:{field}", "shot": n,
                               "kind": field, "text": row[field]})
        for field in ("camera", "lens_mm", "lighting", "vfx"):
            if row.get(field) not in (None, "", [], {}):
                result.append({"id": f"shot:{n}:{field}", "shot": n,
                               "kind": field, "value": row[field]})
        for field in ("action", "notes", "sfx", "avoid", "dialogue"):
            for index, value in enumerate(row.get(field) or [], 1):
                result.append({"id": f"shot:{n}:{field}:{index}", "shot": n,
                               "kind": field, "value": value})
        # A merged render shot may cover several source appearances. Preserve
        # changes inside it instead of flattening a prop transfer to one state.
        adapted = bool(shot.get("production_adaptation"))
        appearances = (shot.get("production_appearances") if adapted else
                       shot.get("source_appearances") or shot.get("source_asset_presence"))
        if appearances:
            result.append({"id": f"shot:{n}:source_appearances", "shot": n,
                           "kind": "production_appearances" if adapted else "source_appearances",
                           "value": appearance_facts(appearances)})
    return result


def shot_blocks(prompt: str) -> dict[int, str]:
    timeline = prompt.split("[SPECIFIC TIMELINE]", 1)[-1].split("[OVERALL SUPPLEMENT]", 1)[0]
    heads = list(re.finditer(r"^\[SHOT (\d+)\s*[—-][^\n]*\]\s*$", timeline, re.M))
    return {int(m.group(1)): timeline[m.end():heads[i + 1].start() if i + 1 < len(heads) else len(timeline)]
            for i, m in enumerate(heads)}


def check_coverage(prompt: str, requirements: list[dict], matches: Any) -> list[str]:
    """Coverage claims count only when backed by literal text in the right shot."""
    if not isinstance(matches, list):
        return ["Return coverage as a list of {requirement_id, shot, quote} for every requirement."]
    blocks = shot_blocks(prompt)
    required = {r["id"]: r for r in requirements}
    seen: set[str] = set()
    issues: list[str] = []
    for match in matches:
        if not isinstance(match, dict):
            issues.append("Every coverage match must be an object.")
            continue
        key = match.get("requirement_id")
        item = required.get(key)
        if not item:
            issues.append(f"Unknown coverage requirement {key!r}.")
            continue
        if key in seen:
            issues.append(f"Duplicate coverage for {key}.")
        seen.add(key)
        if match.get("shot") != item["shot"]:
            issues.append(f"{key}: coverage points to the wrong shot.")
        quote = match.get("quote")
        if not isinstance(quote, str) or not quote.strip() or quote not in blocks.get(item["shot"], ""):
            issues.append(f"{key}: quote must be an exact non-empty span inside shot {item['shot']}.")
    issues.extend(f"Missing coverage for {key}." for key in required if key not in seen)
    return issues


def full_shot_coverage(prompt: str, requirements: list[dict]) -> list[dict]:
    """Anchor each requirement to real text; semantic review decides support.

    This is deliberately whole-shot evidence, not a claim that a model selected
    a precise supporting phrase. Empty/missing blocks remain invalid evidence.
    """
    blocks = shot_blocks(prompt)
    return [{"requirement_id": item["id"], "shot": item["shot"],
             "quote": blocks.get(item["shot"], "").strip()}
            for item in requirements]


def expand_coverage(matches: Any) -> tuple[Any, list[str]]:
    """Expand repeated-quote groups before the unchanged literal/semantic checks.

    One quote can support several requirements in the same shot. Grouping only
    saves output tokens; each ID still needs its own validated coverage entry.
    """
    if not isinstance(matches, list):
        return matches, []
    expanded: list[Any] = []
    issues: list[str] = []
    for match in matches:
        if not isinstance(match, dict) or "requirement_ids" not in match:
            expanded.append(match)
            continue
        ids = match["requirement_ids"]
        if ("requirement_id" in match or not isinstance(ids, list) or not ids
                or any(not isinstance(key, str) or not key for key in ids)
                or len(ids) != len(set(ids))):
            issues.append("Grouped coverage needs unique non-empty requirement_ids and no singular requirement_id.")
            continue
        expanded.extend({"requirement_id": key, "shot": match.get("shot"), "quote": match.get("quote")}
                        for key in ids)
    return expanded, issues


def verify_prompt_contract(prompt: str, coverage: dict | None, *, expected_digest: str,
                           actual_digest: str) -> list[str]:
    """Recheck a stored draft before spending a video generation; no model call."""
    report = coverage or {}
    issues: list[str] = []
    if not expected_digest or expected_digest != actual_digest:
        issues.append("Prompt inputs changed; rewrite and verify the prompt before generation.")
    if report.get("contract_digest") != actual_digest:
        issues.append("Coverage belongs to another input contract.")
    if report.get("prompt_digest") != prompt_digest(prompt):
        issues.append("Prompt text changed after verification; verify the edited prompt again.")
    review = report.get("semantic_review") or {}
    if report.get("status") != "verified" or review.get("status") != "verified" or review.get("findings"):
        issues.append("Prompt has not passed the independent semantic review.")
    requirements = report.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        issues.append("Prompt coverage requirements are missing.")
    else:
        issues.extend(check_coverage(prompt, requirements, report.get("matches")))
        if report.get("evidence_scope") == "full_shot" and report.get("matches") != full_shot_coverage(prompt, requirements):
            issues.append("Full-shot evidence does not match the actual prompt shot blocks.")
    return issues
