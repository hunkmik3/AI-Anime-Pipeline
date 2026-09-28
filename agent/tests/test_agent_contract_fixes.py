"""The two source agents meeting boards and casts made before them.

Each test pins one defect found in review: legacy boards put into the strict
path, a failed inventory wiping the cast, older cast entries duplicated,
re-cited evidence rejected, asset findings smeared over a batch, leads dropped
from the identity anchors, timestamps outside their shot, ffmpeg decoding from
the start, and a receipt the client could decline to send.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from flowboard.db import get_session
from flowboard.db.models import AutomationProject
from flowboard.routes import automation as routes
from flowboard.services import prompt_coverage
from flowboard.services.video_analyzer import board as board_mod
from flowboard.services.video_analyzer import production, source_inventory


# ── which boards are strict ──

def test_a_legacy_report_is_not_a_source_contract():
    legacy = {"status": "unverified", "findings": [{"code": "legacy_analysis"}]}
    assert not prompt_coverage.is_strict([], legacy)
    assert not prompt_coverage.is_strict(None, {"status": "legacy"})
    assert not prompt_coverage.is_strict(None, None)
    assert prompt_coverage.is_strict([], {"status": "unverified"})
    assert prompt_coverage.is_strict([{"id": "a"}], None)


def test_a_video_analysed_before_the_inventory_builds_a_board_without_a_contract():
    analysis = {
        "shots": [{"shot": 1, "start": 0.0, "end": 3.0, "source": {}, "frames": []},
                  {"shot": 2, "start": 3.0, "end": 6.0, "source": {}, "frames": []}],
        "video": {"duration": 6.0, "fps": 24, "width": 1080, "height": 1920, "aspect_ratio": "9:16"},
        "sequences": [],
    }
    cast = {"characters": [{"key": "theo", "name": "Theo"}], "environments": [],
            "shots": {"1": {"character_keys": ["theo"], "environment_key": ""}}}
    out = asyncio.run(board_mod.build_board(analysis, {"shots": {}}, cast=cast))
    assert "source_verification" not in out and "production_assets" not in out


def test_the_writer_route_drops_a_legacy_report_instead_of_going_strict(monkeypatch):
    seen = {}

    async def fake(*a, **kw):
        seen.update(kw)
        from flowboard.services.prompt_writer import Written
        return Written(prompt="P", duration=5)

    monkeypatch.setattr(routes.prompt_writer, "WRITER_ON", True)
    monkeypatch.setattr(routes.prompt_writer, "write_clip_prompt", fake)
    body = routes.VideoWriteBody(sequence={}, shots=[{"n": 1}], production_assets=[],
                                 source_verification={"status": "unverified",
                                                      "findings": [{"code": "legacy_analysis"}]})
    out = asyncio.run(routes.write_video_prompt(body))
    assert seen["production_assets"] is None and seen["source_verification"] is None
    assert out.coverage_token == ""


# ── carrying the inventory into an older cast ──

def _analysis(presence: dict[int, list[dict]], reviewed: list[int], status: str = "verified") -> dict:
    return {
        "shots": [{"shot": n, "start": n - 1.0, "end": float(n), "frames": []} for n in range(1, 6)],
        "scene_inventory": {
            "schema_version": 1,
            "assets": [
                {"id": "woman-black-blazer", "kind": "character", "name": "woman in a black blazer",
                 "description": "handler", "evidence_ids": []},
                {"id": "man-dark-suit", "kind": "character", "name": "man in a dark suit",
                 "description": "bodyguard", "evidence_ids": []},
            ],
            "scenes": [],
            "shots": {str(n): {"scene_id": "", "asset_presence": presence.get(n, []), "evidence_ids": []}
                      for n in range(1, 6)},
        },
        "source_verification": {"status": status, "reviewed_shots": reviewed, "evidence": []},
    }


def _older_cast() -> dict:
    return {
        "characters": [{"key": "dana-merrick", "name": "Dana Merrick", "plate": {"reference_url": "dana.png"}},
                       {"key": "grant-mullen", "name": "Grant Mullen", "plate": {"reference_url": "grant.png"}}],
        "environments": [],
        "shots": {"1": {"character_keys": ["dana-merrick"]}, "2": {"character_keys": ["dana-merrick"]},
                  "3": {"character_keys": ["dana-merrick"]}, "4": {"character_keys": ["grant-mullen"]},
                  "5": {"character_keys": ["grant-mullen"]}},
    }


def _seen(aid: str) -> dict:
    return {"asset_id": aid, "visibility": "visible", "evidence_ids": []}


def test_an_older_cast_entry_is_matched_by_the_shots_it_covers():
    presence = {1: [_seen("woman-black-blazer")], 2: [_seen("woman-black-blazer")],
                3: [_seen("woman-black-blazer")], 4: [_seen("man-dark-suit")], 5: [_seen("man-dark-suit")]}
    out = production.attach_inventory(_analysis(presence, [1, 2, 3, 4, 5]), _older_cast())
    keys = [c["key"] for c in out["characters"]]
    assert keys == ["dana-merrick", "grant-mullen"]                    # no duplicates appended
    dana = out["characters"][0]
    assert dana["source_asset_id"] == "woman-black-blazer" and dana["plate"]["reference_url"] == "dana.png"
    assert out["shots"]["2"]["character_keys"] == ["dana-merrick"]


def test_an_ambiguous_overlap_is_not_guessed():
    cast = _older_cast()
    cast["characters"].append({"key": "twin", "name": "Twin"})
    for n in ("1", "2", "3"):
        cast["shots"][n]["character_keys"].append("twin")           # two entries cover the same shots
    presence = {n: [_seen("woman-black-blazer")] for n in (1, 2, 3)}
    out = production.attach_inventory(_analysis(presence, [1, 2, 3, 4, 5]), cast)
    assert "woman-black-blazer" in [c["key"] for c in out["characters"]]


def test_a_shot_the_inventory_never_observed_keeps_its_cast():
    inventory, report = source_inventory.unverified(_analysis({}, [])["shots"], "gateway down")
    analysis = {**_analysis({}, []), "scene_inventory": inventory, "source_verification": report}
    out = production.attach_inventory(analysis, _older_cast())
    assert out["shots"]["1"]["character_keys"] == ["dana-merrick"]
    assert out["shots"]["5"]["character_keys"] == ["grant-mullen"]


def test_a_reviewed_shot_with_nobody_in_it_is_cleared():
    out = production.attach_inventory(_analysis({}, [1]), _older_cast())
    assert out["shots"]["1"]["character_keys"] == []                  # observed: truly empty
    assert out["shots"]["2"]["character_keys"] == ["dana-merrick"]     # not observed: kept


# ── Agent 1's own checks ──

def test_an_asset_may_recite_the_evidence_it_was_registered_with():
    known = {"assets": [{"id": "hero", "kind": "character", "name": "hero",
                         "evidence_ids": ["shot-1-frame-1", "shot-2-frame-3"]}], "scenes": [], "shots": {}}
    batch = [{"shot": 7, "start": 10.0, "end": 12.0}]
    evidence = [{"id": "shot-7-frame-1", "shot": 7, "frame": "f.jpg", "timestamp_s": 10.1},
                {"id": "shot-1-frame-1", "shot": 1, "frame": "a.jpg", "timestamp_s": 0.1}]
    data = {"assets": [{"id": "hero", "kind": "character", "name": "hero",
                        "evidence_ids": ["shot-1-frame-1", "shot-2-frame-3", "shot-7-frame-1"]}],
            "scenes": [{"id": "s1", "shot_ids": [7], "present_asset_ids": ["hero"]}],
            "shots": {"7": {"scene_id": "s1", "evidence_ids": ["shot-7-frame-1"],
                            "asset_presence": [{"asset_id": "hero", "visibility": "visible",
                                                "evidence_ids": ["shot-7-frame-1", "shot-2-frame-3"]}]}}}
    _, issues = source_inventory._normalise(data, batch, known, evidence)
    assert [i["code"] for i in issues] == []


def test_an_assets_problem_holds_back_only_the_shots_it_is_in():
    patch = {"shots": {"1": {"asset_presence": [{"asset_id": "extra"}]}, "2": {"asset_presence": []}}}
    issues = [{"code": "invalid_evidence", "shot": None, "asset_id": "extra"},
              {"code": "invalid_evidence", "shot": None, "asset_id": "ghost"},
              {"code": "inventory_call_failed", "shot": None}]
    routed = source_inventory._route_findings(issues, {1, 2}, patch)
    assert [(i["code"], i["shot"]) for i in routed] == [
        ("invalid_evidence", 1), ("invalid_evidence", None),
        ("inventory_call_failed", 1), ("inventory_call_failed", 2)]


def test_the_identity_anchors_keep_the_leads_of_a_crowded_film():
    assets = [{"id": f"prop-{i}", "kind": "prop", "evidence_ids": [f"p{i}"]} for i in range(20)]
    assets[:0] = [{"id": "lead", "kind": "character", "evidence_ids": ["lead-1"]},
                  {"id": "rival", "kind": "character", "evidence_ids": ["rival-1"]}]
    anchors = source_inventory._anchor_ids({"assets": assets, "shots": {}})
    assert anchors[:2] == ["lead-1", "rival-1"] and len(anchors) == source_inventory.MAX_ANCHORS


def test_a_keyframe_timestamp_never_falls_before_its_shot(tmp_path):
    (tmp_path / "frames").mkdir()
    for k in (1, 2, 3):
        (tmp_path / "frames" / f"shot002_{k}.jpg").write_bytes(b"x")
    shot = {"shot": 2, "start": 12.34, "end": 12.40,
            "frames": [f"frames/shot002_{k}.jpg" for k in (1, 2, 3)]}
    evidence = source_inventory._initial_evidence(tmp_path, [shot], 24.0, False)
    assert evidence and all(e["timestamp_s"] >= 12.34 for e in evidence)


def test_a_frame_is_seeked_before_it_is_decoded(monkeypatch, tmp_path):
    seen = {}

    def fake_run(args, **kw):
        seen["args"] = args
        (tmp_path / "out.jpg").write_bytes(b"x")

        class Done:
            returncode = 0
            stderr = b""
        return Done()

    monkeypatch.setattr(source_inventory.subprocess, "run", fake_run)
    source_inventory._extract_frame(tmp_path / "film.mp4", 2400.0, tmp_path / "out.jpg", None)
    assert seen["args"].index("-ss") < seen["args"].index("-i")


# ── the server decides whether a receipt is required ──

STRICT_BOARD = {"productionAssets": [{"id": "hero"}], "sourceVerification": {"status": "verified"},
                "nodes": [{"id": "char:hero", "data": {"kind": "character",
                                                       "identity": {"referenceUrl": "https://x/hero.png",
                                                                    "mediaId": "media-hero"}, "states": {}}}]}


def _project(board: dict) -> AutomationProject:
    with get_session() as s:
        row = AutomationProject(name="t", board=board)
        s.add(row)
        s.commit()
        s.refresh(row)
        return row


def test_a_strict_board_needs_a_receipt_even_when_the_client_sends_none():
    row = _project(STRICT_BOARD)
    body = routes.ClipBody(prompt="p", reference_urls=["https://x/other.png"], duration_seconds=5,
                           project_id=row.id)
    with pytest.raises(HTTPException) as caught:
        routes._validate_generation_contract(body)
    assert caught.value.status_code == 422


def test_a_request_naming_no_board_is_held_to_the_board_its_references_belong_to():
    _project(STRICT_BOARD)
    body = routes.ClipBody(prompt="p", reference_urls=[], kyc_media_ids=["media-hero"], duration_seconds=5)
    with pytest.raises(HTTPException):
        routes._validate_generation_contract(body)
    free = routes.ClipBody(prompt="p", reference_urls=["https://x/unrelated.png"], duration_seconds=5)
    routes._validate_generation_contract(free)                       # no strict board owns it


def test_a_freeform_board_generates_without_a_receipt():
    row = _project({"nodes": [], "sourceVerification": {"status": "unverified",
                                                       "findings": [{"code": "legacy_analysis"}]}})
    body = routes.ClipBody(prompt="p", reference_urls=["https://x/hero.png"], duration_seconds=5,
                           project_id=row.id)
    routes._validate_generation_contract(body)


def test_a_malformed_reply_is_asked_for_again_instead_of_losing_the_batch(monkeypatch, tmp_path):
    from flowboard.services import avis_text
    replies = iter(['{"assets": [ {"id": "a"} {"id": "b"} ]}', '{"assets": [], "shots": {}}'])
    asked = []

    async def fake(model, messages, **kw):
        asked.append(messages)
        return avis_text.Completion(text=next(replies), model=model)

    monkeypatch.setattr(source_inventory.avis_text, "complete", fake)
    out = asyncio.run(source_inventory._ask("sys", {"x": 1}, [], tmp_path, {}))
    assert out == {"assets": [], "shots": {}}
    assert len(asked) == 2 and "not one valid JSON object" in asked[1][-1]["content"]


def test_a_reviewer_accepts_what_the_verifier_could_not_settle():
    inventory = {"shots": {"1": {"evidence_ids": ["shot-1-frame-1"]}, "2": {"evidence_ids": ["shot-2-frame-1"]}}}
    report = {"status": "needs_review", "method": "source_frames", "digest": "d", "reviewed_shots": [1],
              "unresolved_shots": [1, 2], "findings": [{"code": "source_mismatch", "shot": 1}]}
    assert source_inventory.unobserved_shots(inventory) == []
    out = source_inventory.accept_review(report, inventory, by="me", at="now", note="checked")
    assert out["status"] == "verified" and out["unresolved_shots"] == [] and out["reviewed_shots"] == [1, 2]
    assert out["findings"][0]["accepted"] and out["review"]["machine_status"] == "needs_review"
    assert out["review"]["accepted_shots"] == [1, 2] and out["digest"] == "d"
    failed = {"shots": {**inventory["shots"], "3": {"evidence_ids": []}}}
    assert source_inventory.unobserved_shots(failed) == [3]


def test_an_offscreen_asset_needs_no_frame_to_prove_it_is_not_there():
    known = {"assets": [{"id": "hero", "kind": "character", "name": "hero", "evidence_ids": ["shot-1-frame-1"]},
                        {"id": "rival", "kind": "character", "name": "rival", "evidence_ids": ["shot-1-frame-1"]}],
             "scenes": [{"id": "s1", "shot_ids": [], "present_asset_ids": []}], "shots": {}}
    batch = [{"shot": 2, "start": 1.0, "end": 2.0}]
    evidence = [{"id": "shot-2-frame-1", "shot": 2, "frame": "f.jpg", "timestamp_s": 1.1}]
    data = {"assets": [], "scenes": [{"id": "s1", "shot_ids": [2], "present_asset_ids": ["hero"]}],
            "shots": {"2": {"scene_id": "s1", "evidence_ids": ["shot-2-frame-1"], "asset_presence": [
                {"asset_id": "hero", "visibility": "visible", "evidence_ids": ["shot-2-frame-1"]},
                {"asset_id": "rival", "visibility": "offscreen", "evidence_ids": []}]}}}
    rival = data["shots"]["2"]["asset_presence"][1]

    def codes():
        return [i["code"] for i in source_inventory._normalise(data, batch, known, evidence)[1]]

    assert codes() == []
    # Uncertain has nothing to point at either; it is flagged as uncertain, not as bad evidence.
    rival["visibility"] = "uncertain"
    assert codes() == ["uncertain_presence"]
    # A citation still has to resolve.
    rival.update(visibility="offscreen", evidence_ids=["made-up"])
    assert codes() == ["invalid_evidence"]
    # Someone in frame still needs a frame.
    rival.update(visibility="visible", evidence_ids=[])
    assert "invalid_evidence" in codes()
