"""Carry source observations into editable production assets without losing evidence.

The source inventory stays immutable. Target names/designs live on cast entries;
source_asset_id is the join, never a model's rewritten display name.
"""

from __future__ import annotations

from copy import deepcopy

BUCKETS = {
    "character": "characters",
    "environment": "environments",
    "background_group": "background_groups",
    "prop": "props",
}
VISIBLE = {"visible", "partial", "occluded"}
# Matching an older cast entry to an observed asset by the shots both cover:
# the overlap must be clear, and clearly better than the next candidate.
MATCH_MIN = 0.5
MATCH_MARGIN = 0.15


def _legacy_shots(cast: dict, entry: dict, kind: str) -> set[int]:
    """Shots an older bible put this entry in (its own shot map, else its list)."""
    rows = cast.get("shots") or {}
    key = entry.get("key")
    if kind == "character":
        found = {int(n) for n, v in rows.items() if key in ((v or {}).get("character_keys") or [])}
    else:
        found = {int(n) for n, v in rows.items() if (v or {}).get("environment_key") == key}
    return found or {int(n) for n in entry.get("shots") or [] if str(n).lstrip("-").isdigit()}


def _overlap_matches(cast: dict, assets: list[dict], source_shots: dict) -> dict[str, dict]:
    """Observed asset id → the older cast entry that covers the same shots.

    A bible written before the inventory calls people by their new names
    ("Dana Merrick") and the inventory by what it saw ("woman in a black
    blazer"), so names never meet. Where they appear is the same, though: the
    shot map put Dana in exactly the shots the inventory saw that woman in. A
    match needs an overlap of at least half (Jaccard) and a clear lead over the
    next candidate; anything closer stays unmatched rather than guessed.
    """
    scored: list[tuple[float, str, int]] = []
    pool: dict[int, dict] = {}
    for asset in assets:
        kind = asset.get("kind")
        if kind not in ("character", "environment"):
            continue
        seen = {int(n) for n, row in source_shots.items()
                if any(p.get("asset_id") == asset["id"] and p.get("visibility") in VISIBLE
                       for p in row.get("asset_presence") or [])}
        if not seen:
            continue
        candidates = []
        for entry in cast.get(BUCKETS[kind]) or []:
            if entry.get("source_asset_id"):
                continue
            legacy = _legacy_shots(cast, entry, kind)
            if legacy:
                candidates.append((len(seen & legacy) / len(seen | legacy), id(entry)))
                pool[id(entry)] = entry
        candidates.sort(reverse=True)
        if candidates and candidates[0][0] >= MATCH_MIN and (
                len(candidates) == 1 or candidates[0][0] - candidates[1][0] >= MATCH_MARGIN):
            scored.append((candidates[0][0], asset["id"], candidates[0][1]))
    out: dict[str, dict] = {}
    taken: set[int] = set()
    for _score, aid, eid in sorted(scored, reverse=True):
        if aid not in out and eid not in taken:
            out[aid] = pool[eid]
            taken.add(eid)
    return out


def _confirmed_aliases(inventory: dict) -> dict[str, str]:
    """Only explicit, acyclic joins to a current source identity can retire cast."""
    aliases = inventory.get("identity_aliases") or {}
    available = {a["id"] for a in (inventory.get("assets") or []) +
                 (inventory.get("screen_graphics") or [])}
    resolved = {}
    for original in aliases:
        current, visited = original, set()
        while isinstance(current, str) and current in aliases and current not in visited:
            visited.add(current)
            target = aliases[current]
            if target == current:
                break
            current = target
        if (isinstance(current, str) and current in available and current != original
                and (current not in visited or aliases.get(current) == current)):
            resolved[original] = current
    return resolved


def _retire_source_entries(out: dict, graphics: set[str], aliases: dict[str, str]) -> None:
    """Keep user media intact while removing only explicitly superseded joins."""
    replacements = {}
    for bucket in BUCKETS.values():
        entries = out.get(bucket) or []
        canonical = {e.get("source_asset_id"): e for e in entries
                     if e.get("source_asset_id") and e["source_asset_id"] not in aliases
                     and e["source_asset_id"] not in graphics}
        # A pre-inventory entry can already use the exact canonical key.
        for entry in entries:
            if not entry.get("source_asset_id") and entry.get("key") in set(aliases.values()) - graphics:
                canonical.setdefault(entry["key"], entry)
        active, changes = [], {}
        for entry in entries:
            source_id = entry.get("source_asset_id")
            target = aliases.get(source_id, source_id)
            reason = None
            if source_id and (source_id in graphics or target in graphics):
                reason = "screen_graphic"
                changes[entry.get("key")] = None
            elif source_id in aliases:
                if target in canonical:
                    reason = "identity_alias"
                    changes[entry.get("key")] = canonical[target].get("key")
                else:
                    # Reuse its paid design, target name and key when it is the
                    # only existing representation of the canonical identity.
                    entry["source_asset_id"] = target
                    entry["source_asset_aliases"] = list(dict.fromkeys(
                        (entry.get("source_asset_aliases") or []) + [source_id]))
                    canonical[target] = entry
            if reason:
                retired = {"reason": reason, "bucket": bucket, "source_asset_id": source_id,
                           "canonical_source_asset_id": target if reason == "identity_alias" else None,
                           "entry": deepcopy(entry)}
                archive = out.setdefault("retired_source_assets", [])
                if retired not in archive:
                    archive.append(retired)
            else:
                active.append(entry)
        if bucket in out:
            out[bucket] = active
        # A retained user entry with the same key still owns that reference.
        active_keys = {e.get("key") for e in active}
        replacements[bucket] = {key: value for key, value in changes.items()
                                if key is not None and (value is not None or key not in active_keys)}
    for row in (out.get("shots") or {}).values():
        for field, buckets in (("character_keys", ("characters",)),
                               ("asset_keys", ("props", "background_groups")),
                               ("environment_keys", ("environments",))):
            if field in row:
                mapping = {key: value for bucket in buckets for key, value in replacements[bucket].items()}
                row[field] = list(dict.fromkeys(mapping.get(key, key) for key in row[field]
                                                if mapping.get(key, key) is not None))
        if row.get("environment_key") in replacements["environments"]:
            row["environment_key"] = replacements["environments"][row["environment_key"]] or ""


def _source_observation(observation: dict, graphic_ids: set[str], aliases: dict[str, str]) -> dict:
    """Keep separated graphics out of physical memberships, with source metadata."""
    result = deepcopy(observation)
    graphics = deepcopy(result.get("screen_graphics") or [])
    presence = []
    for original in result.get("asset_presence") or []:
        item = deepcopy(original)
        item["asset_id"] = aliases.get(item["asset_id"], item["asset_id"])
        if item.get("holder_id"):
            item["holder_id"] = aliases.get(item["holder_id"], item["holder_id"])
        item["contains_ids"] = list(dict.fromkeys(aliases.get(key, key) for key in item.get("contains_ids") or []))
        if item["asset_id"] in graphic_ids:
            if item not in graphics:
                graphics.append(item)
        else:
            presence.append(item)
    result["asset_presence"] = presence
    if graphics or "screen_graphics" in result:
        result["screen_graphics"] = graphics
    return result


def attach_inventory(analysis: dict, cast: dict) -> dict:
    out = deepcopy(cast)
    inventory = analysis.get("scene_inventory")
    if not inventory:
        return out
    graphics = deepcopy(inventory.get("screen_graphics") or [])
    graphic_ids = {a["id"] for a in graphics}
    aliases = _confirmed_aliases(inventory)
    source_kinds = {a["id"]: a.get("kind") for a in (inventory.get("assets") or []) + graphics}
    legacy_assets = []
    for entry in out.get("assets") or []:
        if any(entry in (out.get(bucket) or []) for bucket in BUCKETS.values()):
            # The generic list normally mirrors these buckets, including older
            # entries without kind. Do not append that mirror as custom media.
            continue
        source_id = aliases.get(entry.get("source_asset_id"), entry.get("source_asset_id"))
        kind = entry.get("kind") or source_kinds.get(source_id)
        bucket = BUCKETS.get(kind)
        if bucket:
            entries = out.setdefault(bucket, [])
            if entry not in entries:
                entries.append(deepcopy(entry))
        else:
            # Some older/user assets exist only in the generic collection.
            # Missing from the new source catalog is never a retirement rule.
            legacy_assets.append(deepcopy(entry))
    _retire_source_entries(out, graphic_ids, aliases)
    assets = deepcopy([a for a in inventory.get("assets") or []
                       if a.get("kind") in BUCKETS and a["id"] not in graphic_ids and a["id"] not in aliases])
    source_shots = {n: _source_observation(row, graphic_ids, aliases)
                    for n, row in (inventory.get("shots") or {}).items()}
    evidence = {
        e["id"]: e for e in (analysis.get("source_verification") or {}).get("evidence") or []
    }
    by_n = {str(s["shot"]): s for s in analysis.get("shots") or []}
    scenes = {s["id"]: s for s in inventory.get("scenes") or []}
    verification = analysis.get("source_verification") or {}
    reviewed = {int(n) for n in verification.get("reviewed_shots") or [] if str(n).isdigit()}
    by_overlap = _overlap_matches(out, assets, source_shots)
    linked: dict[str, dict] = {}
    for asset in assets:
        aid, kind = asset["id"], asset["kind"]
        bucket = BUCKETS.get(kind)
        if not bucket:
            continue
        entries = out.setdefault(bucket, [])
        entry = next(
            (e for e in entries if e.get("source_asset_id") == aid or e.get("key") == aid), None
        )
        if entry is None:
            # Exact, unique source-name matches can upgrade older bibles. Do not
            # guess identities from an overlapping crowd or a similar costume.
            names = {str(asset.get(k) or "").strip().casefold() for k in ("name", "source_name")}
            names.discard("")
            matches = [
                e
                for e in entries
                if not e.get("source_asset_id")
                and any(
                    str(e.get(k) or "").strip().casefold() in names for k in ("source_name", "name")
                )
            ]
            entry = matches[0] if len(matches) == 1 else None
        if entry is None and aid in by_overlap and not by_overlap[aid].get("source_asset_id"):
            entry = by_overlap[aid]
        if entry is None:
            entry = {
                "key": aid,
                "name": asset.get("name") or aid,
                "summary": asset.get("description") or "",
                "source_name": asset.get("source_name"),
                "role": asset.get("role", kind),
            }
            if kind == "character":
                entry.update(
                    looks_like=asset.get("description", ""),
                    states=[
                        {
                            "key": "source",
                            "label": "As observed",
                            "look": asset.get("description", ""),
                            "wardrobe": "",
                            "posture": "",
                        }
                    ],
                )
            entries.append(entry)
        entry.update(
            source_asset_id=aid,
            kind=kind,
            description=asset.get("description", ""),
            reference_required=asset.get("reference_required", True),
            member_ids=asset.get("member_ids") or [],
            depends_on_asset_ids=asset.get("depends_on_asset_ids") or [],
            evidence_ids=asset.get("evidence_ids") or [],
        )
        entry["shots"] = sorted(
            int(n)
            for n, shot in source_shots.items()
            if any(
                p.get("asset_id") == aid and p.get("visibility") in VISIBLE
                for p in shot.get("asset_presence") or []
            )
        )
        evidence_ids = list(asset.get("evidence_ids") or [])
        evidence_ids += [
            eid
            for shot in source_shots.values()
            for p in shot.get("asset_presence") or []
            if p.get("asset_id") == aid and p.get("visibility") in {"visible", "partial"}
            for eid in p.get("evidence_ids") or []
        ]
        frame_names = [
            evidence[eid]["frame"]
            for eid in dict.fromkeys(evidence_ids)
            if eid in evidence and evidence[eid].get("frame")
        ]
        if not evidence:
            # Only legacy inventories lack frame evidence. A verified close-up
            # or requested crop must never be replaced by an arbitrary midpoint.
            for n in entry["shots"]:
                frames = (by_n.get(str(n)) or {}).get("frames") or []
                if frames:
                    frame_names.append(frames[len(frames) // 2])
        frame_names = list(dict.fromkeys(frame_names))[:6]
        if entry.get("frames") != frame_names:
            entry.pop("frame_urls", None)
        entry["frames"] = frame_names
        linked[aid] = entry
        # Writer resolves target naming through this alias; observed facts stay
        # in description/source_name, not replaced by a creative redesign.
        asset["production_key"] = entry["key"]
        asset["production_name"] = entry.get("name") or asset.get("name")

    mapped = out.setdefault("shots", {})
    for n, observation in source_shots.items():
        row = mapped.setdefault(str(n), {})
        presence = deepcopy(observation.get("asset_presence") or [])
        on_screen = [
            linked[p["asset_id"]]
            for p in presence
            if p.get("asset_id") in linked and p.get("visibility") in VISIBLE
        ]
        # Observations replace the older guesses for this shot only — and only
        # when the shot was actually observed. A failed batch, or the whole
        # inventory's unverified fallback, carries an empty presence list; taken
        # as "nobody is here" it wiped the cast from every shot it covered.
        if presence or int(n) in reviewed:
            row["character_keys"] = [e["key"] for e in on_screen if e["kind"] == "character"]
            envs = [e["key"] for e in on_screen if e["kind"] == "environment"]
            if envs:
                row["environment_key"] = envs[0]
            row["asset_keys"] = [
                e["key"] for e in on_screen if e["kind"] in {"prop", "background_group"}
            ]
        row["asset_presence"] = [{**p, "source_shot": int(n)} for p in presence]
        row["scene_id"] = observation.get("scene_id")
        # Scene membership is a union over time. Someone who enters later must
        # not be inserted into earlier shots just because the scene is shared.
        row["scene_asset_ids"] = list(dict.fromkeys(
            aliases.get(key, key) for key in
            (scenes.get(observation.get("scene_id")) or {}).get("present_asset_ids") or []
            if aliases.get(key, key) not in graphic_ids))
        row["scene_present_asset_ids"] = list(dict.fromkeys(p["asset_id"] for p in presence))
        row["source_evidence"] = list(observation.get("evidence_ids") or [])
        row["source_graphics"] = [{**deepcopy(p), "source_shot": int(n)}
                                  for p in observation.get("screen_graphics") or []]
        # Provenance is the exact object hashed by source verification. Display
        # aliases/defaults belong beside it; adding source_graphics here changes
        # every shot fingerprint and prevents Agent 2 from using a verified film.
        verified_observation = inventory["shots"][n]
        row["source_appearances"] = [{"source_shot": int(n), **deepcopy(verified_observation)}]
    out["production_assets"] = assets
    out["source_graphics"] = graphics
    out["source_identity_aliases"] = deepcopy(aliases)
    out["source_verification"] = deepcopy(
        analysis.get("source_verification")
        or {
            "status": "unverified",
            "findings": [
                {"code": "missing_verification", "message": "Reinspect the source video."}
            ],
        }
    )
    out["assets"] = out.get("background_groups", []) + out.get("props", []) + legacy_assets
    return out


def build_asset_prompt(
    asset: dict,
    *,
    kind: str,
    style: str = "cg3d",
    aspect_ratio: str = "16:9",
    has_reference: bool = False,
) -> str:
    """Reference plates for recurring groups and story props, including dependencies."""
    medium = {
        "cg3d": "Stylized cinematic 3D animation, consistent sculpted faces and physically based materials",
        "anime": "Cinematic 2D anime with consistent linework and cel shading",
        "realistic": "Photorealistic cinematic reference photography",
    }.get(style, style)
    description = asset.get("description") or asset.get("summary") or ""
    design = asset.get("design") or {}
    subject = asset.get("name") or asset.get("id") or asset.get("key")
    layout = (
        "One ensemble reference sheet. Show the observed recurring group together, full body, "
        "with distinct, consistent member identities, clothing, relative heights and spacing. "
        "Only include the observed members; do not clone faces or add protagonists. "
        "Do not invent an exact headcount when evidence is uncertain."
        if kind == "background_group"
        else "One prop reference sheet: hero three-quarter view and clear detail views of the SAME object. "
        "Preserve shape, scale, materials, markings and story-relevant mechanisms. "
        "Show only evidenced open/closed states. Keep a container and its contents distinguishable."
    )
    dependencies = asset.get("dependency_references") or []
    dependency_note = "\n".join(
        f"Image {i + 1}: {d.get('name') or d.get('id')}. Use this supplied identity/design "
        "for its depicted member, contents or photograph; do not invent a substitute."
        for i, d in enumerate(dependencies)
    )
    reference = (
        "Use the supplied source references for this subject's observed appearance. "
        "Other people or objects in those frames are context, not additional subjects."
        if has_reference
        else "Follow the verified description below."
    )
    import json

    return "\n\n".join(
        filter(
            None,
            [
                f"FORMAT\n{aspect_ratio} production reference sheet.",
                f"STYLE\n{medium}.",
                f"SUBJECT\n{subject}. {description}",
                f"LAYOUT\n{layout}",
                f"REFERENCES\n{reference}\n{dependency_note}",
                f"DESIGN\n{json.dumps(design, ensure_ascii=False)}" if design else "",
                "CONSTRAINTS\nNeutral readable lighting; uncluttered background. No extra people, "
                "no invented labels or watermarks. Keep recurring identities and props identical across scenes.",
            ],
        )
    )
