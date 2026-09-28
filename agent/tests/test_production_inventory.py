"""Inventory survives adaptation, short-shot merges, and unrelated films."""

from copy import deepcopy
import asyncio

import pytest
from fastapi import HTTPException

from flowboard.routes import automation as routes
from flowboard.routes import video_analysis as video_routes
from flowboard.services import auth, prompt_coverage, prompt_writer
from flowboard.services.video_analyzer import board, export, production, source_inventory


def film(prefix="harbor"):
    assets = [
        {
            "id": f"{prefix}-{key}",
            "kind": kind,
            "name": name,
            "description": name,
            "reference_required": True,
            "member_ids": [],
            "depends_on_asset_ids": [],
            "evidence_ids": ["e1"],
        }
        for key, kind, name in [
            ("lead", "character", "Captain"),
            ("a", "background_group", "Dock workers"),
            ("b", "background_group", "Passengers"),
            ("object", "prop", "Brass compass"),
            ("place", "environment", "Harbor"),
        ]
    ]

    def presence(key, n, visibility="visible", **state):
        return {
            "asset_id": f"{prefix}-{key}",
            "visibility": visibility,
            "position": "aft",
            "state": "unchanged",
            "contains_ids": [],
            "evidence_ids": [f"e{n}"],
            **state,
        }

    observations = {
        "1": {
            "scene_id": "arrival",
            "evidence_ids": ["e1"],
            "asset_presence": [
                presence("lead", 1),
                presence("a", 1),
                presence("object", 1, holder_id=f"{prefix}-lead", hand="left"),
            ],
        },
        "2": {
            "scene_id": "arrival",
            "evidence_ids": ["e2"],
            "asset_presence": [
                presence("lead", 2),
                presence("a", 2, "offscreen"),
                presence("b", 2),
                presence("object", 2, holder_id=f"{prefix}-lead", hand="right"),
            ],
        },
        "3": {"scene_id": "arrival", "evidence_ids": ["e3"], "asset_presence": [presence("b", 3)]},
    }
    return {
        "video": {"duration": 5, "fps": 30, "aspect_ratio": "9:16"},
        "shots": [
            {
                "shot": n,
                "start": start,
                "end": end,
                "source": {"action": "A beat", "shot_size": "MS"},
                "frames": [f"frames/shot{n:03d}_1.jpg"],
            }
            for n, start, end in [(1, 0, 0.4), (2, 0.4, 1.2), (3, 1.2, 5)]
        ],
        "sequences": [{"first_shot": 1, "last_shot": 3, "title": "Arrival"}],
        "scene_inventory": {
            "schema_version": 1,
            "assets": assets,
            "shots": observations,
            "scenes": [
                {
                    "id": "arrival",
                    "shot_ids": [1, 2, 3],
                    "present_asset_ids": [a["id"] for a in assets],
                }
            ],
        },
        "source_verification": {
            "status": "verified",
            "method": "source_frames",
            "digest": prefix,
            "reviewed_shots": [1, 2, 3],
            "unresolved_shots": [],
            "evidence": [
                {"id": f"e{n}", "shot": n, "frame": f"frames/shot{n:03d}_1.jpg", "timestamp_s": n}
                for n in (1, 2, 3)
            ],
        },
    }


def test_scene_union_does_not_make_late_arrivals_present_earlier():
    analysis = film()
    before = deepcopy(analysis)
    cast = production.attach_inventory(analysis, {})
    assert "harbor-b" not in cast["shots"]["1"]["scene_present_asset_ids"]
    assert "harbor-b" in cast["shots"]["1"]["scene_asset_ids"]
    assert "harbor-a" in cast["shots"]["2"]["scene_present_asset_ids"]
    assert "harbor-a" not in cast["shots"]["2"]["asset_keys"]  # offscreen, retained as context
    assert set(cast["shots"]["2"]["asset_keys"]) == {"harbor-b", "harbor-object"}
    assert analysis == before


def test_short_shot_merge_preserves_handoffs_and_evidence():
    analysis = film()
    result = asyncio.run(
        board.build_board(analysis, {}, cast={"characters": [], "environments": []})
    )
    first = result["shots"]["clip-01"][0]
    assert first["source_shots"] == [1, 2]
    appearances = [p for p in first["asset_presence"] if p["asset_id"] == "harbor-object"]
    assert [(p["source_shot"], p["hand"]) for p in appearances] == [(1, "left"), (2, "right")]
    assert first["source_evidence"] == ["e1", "e2"]
    assert len(first["source_appearances"]) == 2
    assert {a["kind"] for a in result["assets"]} == {"prop", "background_group"}
    assert result["source_verification"]["digest"] == "harbor"


def test_identity_link_preserves_target_names_and_generated_plate():
    analysis = film()
    old = {
        "characters": [
            {
                "key": "ren",
                "source_asset_id": "harbor-lead",
                "name": "Ren",
                "plate": {"reference_url": "https://example.test/ren.jpg"},
            }
        ]
    }
    cast = production.attach_inventory(analysis, old)
    assert len(cast["characters"]) == 1
    assert cast["characters"][0]["plate"] == old["characters"][0]["plate"]
    assert cast["shots"]["1"]["character_keys"] == ["ren"]
    lead = next(a for a in cast["production_assets"] if a["id"] == "harbor-lead")
    assert lead["name"] == "Captain" and lead["production_name"] == "Ren"


def _separate_graphic(analysis, asset_id="harbor-object"):
    inventory = analysis["scene_inventory"]
    graphic = next(a for a in inventory["assets"] if a["id"] == asset_id)
    inventory["assets"] = [a for a in inventory["assets"] if a["id"] != asset_id]
    inventory["screen_graphics"] = [deepcopy(graphic)]
    for row in inventory["shots"].values():
        row["screen_graphics"] = [p for p in row["asset_presence"] if p["asset_id"] == asset_id]
        row["asset_presence"] = [p for p in row["asset_presence"] if p["asset_id"] != asset_id]
    for scene in inventory["scenes"]:
        scene["present_asset_ids"] = [key for key in scene["present_asset_ids"] if key != asset_id]
        scene["screen_graphic_ids"] = [asset_id]
    return graphic


@pytest.mark.parametrize("bucket,field", [("props", "asset_keys"),
                                          ("characters", "character_keys"),
                                          ("environments", "environment_key")])
def test_separated_graphic_retires_only_linked_cast_and_preserves_paid_media(bucket, field):
    analysis = film()
    graphic = _separate_graphic(analysis)
    paid = {"key": "old-branding", "source_asset_id": graphic["id"], "name": "User target name",
            "design": {"palette": "silver"}, "plate": {"reference_url": "https://example.test/paid.png"},
            "variants": [{"url": "https://example.test/variant.png"}], "custom": {"approved": True}}
    unrelated = {"key": "personal-extra", "source_asset_id": "different-old-source",
                 "plate": {"reference_url": "https://example.test/personal.png"}}
    unlinked = {"key": "user-branding", "name": graphic["name"], "design": {"keep": True}}
    old = {bucket: [paid, unrelated, unlinked],
           "shots": {"99": {field: "old-branding" if field == "environment_key" else ["old-branding", "personal-extra"]}}}
    original = deepcopy(old)
    cast = production.attach_inventory(analysis, old)
    assert old == original
    assert not any(e.get("source_asset_id") == graphic["id"] for e in cast[bucket])
    assert unrelated in cast[bucket] and unlinked in cast[bucket]
    assert cast["retired_source_assets"] == [{"reason": "screen_graphic", "bucket": bucket,
             "source_asset_id": graphic["id"], "canonical_source_asset_id": None, "entry": paid}]
    assert cast["shots"]["99"][field] == ("" if field == "environment_key" else ["personal-extra"])
    assert graphic["id"] not in {a["id"] for a in cast["production_assets"]}
    assert not any(e.get("source_asset_id") == graphic["id"] for e in cast["assets"])
    again = production.attach_inventory(analysis, cast)
    assert again == cast


def test_separated_graphics_keep_source_metadata_through_board_shot_merge():
    analysis = film()
    graphic = deepcopy(_separate_graphic(analysis))
    cast = production.attach_inventory(analysis, {})
    assert cast["source_graphics"] == [graphic]
    source_graphic = analysis["scene_inventory"]["shots"]["1"]["screen_graphics"][0]
    assert cast["shots"]["1"]["source_graphics"] == [{**source_graphic, "source_shot": 1}]
    assert cast["shots"]["1"]["source_appearances"][0]["screen_graphics"] == [source_graphic]
    assert "source_graphics" not in cast["shots"]["1"]["source_appearances"][0]
    result = asyncio.run(board.build_board(analysis, {}, cast=cast))
    first = result["shots"]["clip-01"][0]
    assert all(p["asset_id"] != graphic["id"] for p in first["asset_presence"])
    assert all(graphic["id"] not in row["scene_present_asset_ids"] for row in result["shots"]["clip-01"])
    assert first["source_appearances"][0]["screen_graphics"] == [source_graphic]


@pytest.mark.parametrize("has_graphics", [False, True])
def test_verified_source_digests_survive_production_and_board_into_prompt_gate(has_graphics):
    analysis = film()
    if has_graphics:
        _separate_graphic(analysis)
    inventory = analysis["scene_inventory"]
    report = analysis["source_verification"]
    report.update(
        inventory_digest=source_inventory.inventory_digest(inventory),
        shot_digests={n: source_inventory._digest(row) for n, row in inventory["shots"].items()},
        asset_digests={a["id"]: source_inventory._digest(a) for a in inventory["assets"]},
    )
    original = deepcopy(analysis)
    cast = production.attach_inventory(analysis, {})
    for n, observation in inventory["shots"].items():
        assert cast["shots"][n]["source_appearances"] == [{"source_shot": int(n), **observation}]
    result = asyncio.run(board.build_board(analysis, {}, cast=cast))
    shots = result["shots"]["clip-01"]
    assert shots[0]["source_shots"] == [1, 2]  # exercise merged-shot provenance too
    references = [{"id": a["id"], "ref_label": f"@image{i}",
                   "ref_url": f"https://example.test/{a['id']}.png"}
                  for i, a in enumerate(result["production_assets"], 1)]
    assert prompt_coverage.validate_source_contract(
        shots, result["production_assets"], result["source_verification"], references) == []
    assert analysis == original

    # Exactness remains enforced: changing even preserved graphics metadata is
    # still a source edit and must not inherit the earlier visual verification.
    edited = deepcopy(shots)
    edited[0]["source_appearances"][0]["source_graphics"] = []
    issues = prompt_coverage.validate_source_contract(
        edited, result["production_assets"], result["source_verification"], references)
    assert any("changed after verification" in issue for issue in issues)


def test_alias_only_cast_rebinds_canonical_source_without_regenerating_design():
    analysis = film()
    analysis["scene_inventory"]["identity_aliases"] = {"old-captain": "harbor-lead"}
    paid = {"key": "ren", "source_asset_id": "old-captain", "name": "Ren",
            "design": {"hair": "silver"}, "plate": {"reference_url": "https://example.test/ren.png"},
            "variants": [{"url": "https://example.test/variant.png"}]}
    cast = production.attach_inventory(analysis, {"characters": [paid]})
    assert len(cast["characters"]) == 1
    rebound = cast["characters"][0]
    assert rebound["source_asset_id"] == "harbor-lead"
    assert rebound["source_asset_aliases"] == ["old-captain"]
    assert rebound["key"] == "ren" and rebound["name"] == "Ren"
    for field in ("design", "plate", "variants"):
        assert rebound[field] == paid[field]
    assert cast["shots"]["1"]["character_keys"] == ["ren"]
    assert not cast.get("retired_source_assets")
    assert production.attach_inventory(analysis, cast) == cast


def test_existing_canonical_cast_wins_and_alias_plate_is_archived_intact():
    analysis = film()
    analysis["scene_inventory"]["identity_aliases"] = {"old-captain": "harbor-lead"}
    alias = {"key": "alias-target", "source_asset_id": "old-captain", "name": "Alternate",
             "plate": {"reference_url": "https://example.test/alias-paid.png"}, "design": {"old": True}}
    canonical = {"key": "ren", "source_asset_id": "harbor-lead", "name": "Ren",
                 "plate": {"reference_url": "https://example.test/canonical-paid.png"}, "design": {"new": True}}
    cast = production.attach_inventory(analysis, {"characters": [alias, canonical],
                         "shots": {"99": {"character_keys": ["alias-target", "ren", "user-extra"]}}})
    assert len(cast["characters"]) == 1
    assert cast["characters"][0]["key"] == "ren"
    assert cast["characters"][0]["plate"] == canonical["plate"]
    assert cast["retired_source_assets"][0]["entry"] == alias
    assert cast["retired_source_assets"][0]["canonical_source_asset_id"] == "harbor-lead"
    assert cast["shots"]["99"]["character_keys"] == ["ren", "user-extra"]
    assert production.attach_inventory(analysis, cast) == cast


def test_alias_chain_rebinds_but_unknown_or_cyclic_aliases_do_not_retire_user_cast():
    analysis = film()
    analysis["scene_inventory"]["identity_aliases"] = {
        "old-captain": "intermediate", "intermediate": "harbor-lead",
        "user-a": "user-b", "user-b": "user-a", "user-c": "missing-canonical"}
    entries = [{"key": key, "source_asset_id": key, "plate": {"reference_url": key}}
               for key in ("old-captain", "user-a", "user-b", "user-c")]
    cast = production.attach_inventory(analysis, {"characters": entries})
    assert next(e for e in cast["characters"] if e["key"] == "old-captain")["source_asset_id"] == "harbor-lead"
    for entry in entries[1:]:
        assert entry in cast["characters"]
    assert not cast.get("retired_source_assets")


def test_unrelated_linked_legacy_assets_are_not_filtered_by_current_inventory_ids():
    analysis = film()
    old = {"props": [{"key": "user-item", "source_asset_id": "another-project-prop",
                       "design": {"preserve": "all"}, "plate": {"reference_url": "paid.png"}}],
           "characters": [{"key": "user-extra", "source_asset_id": "another-source-person"}]}
    cast = production.attach_inventory(analysis, old)
    assert old["props"][0] in cast["props"]
    assert old["props"][0] in cast["assets"]
    assert old["characters"][0] in cast["characters"]
    assert not cast.get("retired_source_assets")


def test_generic_legacy_asset_collection_preserves_unrelated_media_and_retires_explicit_graphics():
    analysis = film()
    _separate_graphic(analysis)
    graphic = {"key": "brand", "source_asset_id": "harbor-object",
               "plate": {"reference_url": "paid-brand.png"}}
    prop = {"key": "user-prop", "kind": "prop", "source_asset_id": "unrelated-source",
            "design": {"retain": True}, "plate": {"reference_url": "paid-prop.png"}}
    custom = {"key": "custom-media", "media": {"url": "personal.png"}}
    cast = production.attach_inventory(analysis, {"assets": [graphic, prop, custom]})
    assert prop in cast["assets"] and prop in cast["props"]
    assert custom in cast["assets"]
    assert graphic not in cast["assets"]
    assert cast["retired_source_assets"][0]["entry"] == graphic
    assert production.attach_inventory(analysis, cast) == cast


def test_independent_films_have_independent_registries_and_exports():
    first = production.attach_inventory(film(), {})
    second = film("lunar")
    second["scene_inventory"]["assets"][-2]["name"] = "Oxygen canister"
    second["scene_inventory"]["assets"][-2]["description"] = "Blue oxygen canister"
    cast = production.attach_inventory(second, {})
    assert not {a["id"] for a in first["production_assets"]} & {
        a["id"] for a in cast["production_assets"]
    }
    assert cast["props"][0]["name"] == "Oxygen canister"
    assert export.to_json(second)["scene_inventory"] == second["scene_inventory"]
    assert export.to_json(second)["source_verification"] == second["source_verification"]


def test_prop_image_prompt_keeps_depicted_identity_dependencies():
    prompt = production.build_asset_prompt(
        {
            "name": "Mission portrait",
            "description": "Two crew members",
            "dependency_references": [
                {"id": "pilot", "name": "Pilot"},
                {"id": "engineer", "name": "Engineer"},
            ],
        },
        kind="prop",
        style="cg3d",
        has_reference=True,
    )
    assert "Image 1: Pilot" in prompt and "Image 2: Engineer" in prompt
    assert "3D" in prompt and "Mission portrait" in prompt


def test_asset_plate_uses_its_actual_closeup_evidence_and_discards_stale_frame_urls():
    analysis = film()
    prop = next(a for a in analysis["scene_inventory"]["assets"] if a["kind"] == "prop")
    prop["evidence_ids"] = ["detail"]
    crop = "frames/source-review-1-abcdef012345.jpg"
    analysis["source_verification"]["evidence"].append(
        {"id": "detail", "shot": 1, "frame": crop, "timestamp_s": 0.2}
    )
    old = {
        "props": [
            {
                "key": prop["id"],
                "frames": ["frames/shot001_2.jpg"],
                "frame_urls": ["https://example.test/stale-frame.jpg"],
                "plate": {"reference_url": "https://example.test/paid-plate.jpg"},
            }
        ]
    }
    entry = production.attach_inventory(analysis, old)["props"][0]
    assert entry["frames"][0] == crop
    assert "frame_urls" not in entry
    assert entry["plate"] == old["props"][0]["plate"]


def clip_request(monkeypatch):
    monkeypatch.setattr(auth, "_server_secret", lambda: b"test-only-prompt-receipts")
    body = routes.VideoWriteBody(
        sequence={"label": "CLIP 01", "duration_s": 4},
        shots=[
            {
                "n": 1,
                "duration_s": 4,
                "source_shots": [1],
                "source_evidence": ["e1"],
                "asset_presence": [],
                "action": ["The clouds drift."],
            }
        ],
        production_assets=[],
        source_verification={
            "status": "verified",
            "method": "source_frames",
            "digest": "sky",
            "reviewed_shots": [1],
            "unresolved_shots": [],
            "evidence": [{"id": "e1", "shot": 1}],
        },
        aspect_ratio="9:16",
        style="cg3d",
    )
    prompt = "[SPECIFIC TIMELINE]\n[SHOT 1 — 0–4s]\nThe clouds drift.\n[OVERALL SUPPLEMENT]"
    digest = routes._writing_digest(body)
    coverage = {
        "status": "verified",
        "contract_digest": digest,
        "prompt_digest": prompt_coverage.prompt_digest(prompt),
        "semantic_review": {"status": "verified", "findings": []},
        "requirements": [{"id": "action", "shot": 1}],
        "matches": [{"requirement_id": "action", "shot": 1, "quote": "The clouds drift."}],
    }
    return routes.ClipBody(
        prompt=prompt,
        prompt_contract=body,
        duration_seconds=4,
        aspect_ratio="9:16",
        coverage=coverage,
        contract_digest=digest,
        coverage_token=routes._coverage_token(body, prompt, 4, coverage),
    )


def test_generation_accepts_an_unchanged_server_review_receipt(monkeypatch):
    routes._validate_generation_contract(clip_request(monkeypatch))


@pytest.mark.parametrize("change", ["prompt", "shots", "references", "duration", "receipt", "mode"])
def test_generation_rejects_stale_or_unchecked_inputs_before_spend(monkeypatch, change):
    request = clip_request(monkeypatch)
    if change == "prompt":
        request.prompt += "\nAdd a visitor."
    elif change == "shots":
        request.prompt_contract.shots[0]["action"] = ["The clouds vanish."]
    elif change == "references":
        request.reference_urls = ["https://example.test/changed.jpg"]
    elif change == "duration":
        request.duration_seconds = 8
    elif change == "receipt":
        request.coverage_token = "forged"
    else:
        request.previous_clip_url = "https://example.test/previous.mp4"
    with pytest.raises(HTTPException) as caught:
        routes._validate_generation_contract(request)
    assert caught.value.status_code == 422


def test_verified_source_never_silently_falls_back_to_template(monkeypatch):
    async def broken(*args, **kwargs):
        raise prompt_writer.WriterError("Missing background group coverage")

    monkeypatch.setattr(prompt_writer, "WRITER_ON", True)
    monkeypatch.setattr(prompt_writer, "write_clip_prompt", broken)
    # A strict board always travels with its source report.
    body = routes.VideoWriteBody(sequence={}, shots=[{"n": 1}], production_assets=[],
                                 source_verification={"status": "verified"})
    with pytest.raises(HTTPException) as caught:
        asyncio.run(routes.write_video_prompt(body))
    assert caught.value.status_code == 422 and "Missing background" in caught.value.detail


def test_reinspection_preserves_plates_finished_while_agent_was_running(monkeypatch, tmp_path):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from uuid import uuid4

    analysis = film()
    row = SimpleNamespace(
        id=uuid4(),
        analysis=analysis,
        adaptation={},
        options={},
        cast=production.attach_inventory(analysis, {}),
        progress={},
        status="analysed",
    )
    row.cast["props"][0]["plate"] = {"reference_url": "https://example.test/before.jpg"}

    @contextmanager
    def session():
        yield SimpleNamespace(get=lambda *args: row, add=lambda *args: None, commit=lambda: None)

    async def inspect(*args, **kwargs):
        # Simulate another request finishing a paid image while verification waits.
        row.cast["props"][0]["plate"] = {"reference_url": "https://example.test/latest.jpg"}
        return analysis["scene_inventory"], analysis["source_verification"]

    monkeypatch.setattr(video_routes, "get_session", session)
    monkeypatch.setattr(video_routes, "_source_path", lambda *_: tmp_path / "source.mp4")
    monkeypatch.setattr(video_routes, "_work_dir", lambda *_: tmp_path)
    monkeypatch.setattr(video_routes.source_inventory, "analyze", inspect)
    asyncio.run(video_routes._run_source_verification(row.id))
    assert row.status == "analysed"
    assert row.cast["props"][0]["plate"]["reference_url"].endswith("/latest.jpg")
