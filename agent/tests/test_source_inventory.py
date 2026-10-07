"""Source inventory agent contracts, using mocked completions and frame tools."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

from flowboard.services import avis_text
from flowboard.services.video_analyzer import source_inventory as inv


def test_stage_model_override_does_not_reuse_another_models_response(tmp_path, monkeypatch):
    called = []
    async def ask(*args, **kwargs):
        called.append(kwargs.get('model_override'))
        return {'from_model': kwargs.get('model_override')}
    monkeypatch.setattr(inv, '_ask', ask)
    entry = {'usage': {}, 'trace': []}
    async def run():
        for model in ['visual-a', 'visual-a', 'visual-b']:
            result = await inv._stage_call(entry, 'observe', 'instructions', {}, [], tmp_path,
                                           asyncio.Semaphore(1), lambda: None, model_override=model)
            assert result == {'from_model': model}
    asyncio.run(run())
    assert called == ['visual-a', 'visual-b']
    assert entry['call_model_history']['observe'][0]['model'] == 'visual-a'


def _source(tmp_path: Path, *, title: str = "market", count: int = 2):
    work = tmp_path / "analysis"
    frames = work / "frames"
    frames.mkdir(parents=True, exist_ok=True)
    video = work / "source.mp4"
    video.write_bytes(title.encode())
    shots = []
    for n in range(1, count + 1):
        paths = []
        for k in range(1, 4):
            rel = f"frames/shot{n:03d}_{k}.jpg"
            (work / rel).write_bytes(f"{title}-{n}-{k}".encode())
            paths.append(rel)
        shots.append(
            {
                "shot": n,
                "start": float(n - 1),
                "end": float(n),
                "frames": paths,
                "dialogue": "",
                "source": {"setting": title, "action": "visible action", "subjects": []},
            }
        )
    return video, work, shots


def _draft(shots, *, prefix="market"):
    evidence = [f"shot-{s['shot']}-frame-1" for s in shots]
    assets = [
        {
            "id": f"{prefix}-lead",
            "kind": "character",
            "name": f"{prefix} lead",
            "role": "main",
            "description": "distinct face",
            "reference_required": True,
            "member_ids": [],
            "depends_on_asset_ids": [],
            "evidence_ids": evidence[:1],
        },
        {
            "id": f"{prefix}-group-a",
            "kind": "background_group",
            "name": f"{prefix} group A",
            "description": "three recurring people",
            "reference_required": True,
            "member_ids": [],
            "depends_on_asset_ids": [],
            "evidence_ids": evidence[:1],
        },
        {
            "id": f"{prefix}-group-b",
            "kind": "background_group",
            "name": f"{prefix} group B",
            "description": "two separate people",
            "reference_required": True,
            "member_ids": [],
            "depends_on_asset_ids": [],
            "evidence_ids": evidence[-1:],
        },
        {
            "id": f"{prefix}-prop",
            "kind": "prop",
            "name": f"{prefix} significant object",
            "description": "film-specific object",
            "reference_required": True,
            "member_ids": [],
            "depends_on_asset_ids": [],
            "evidence_ids": evidence[:1],
        },
        {
            "id": f"{prefix}-location",
            "kind": "environment",
            "name": f"{prefix} location",
            "description": prefix,
            "reference_required": True,
            "member_ids": [],
            "depends_on_asset_ids": [],
            "evidence_ids": evidence[:1],
        },
    ]
    rows = {}
    for i, s in enumerate(shots):
        ids = [a["id"] for a in assets if not a["id"].endswith("group-b") or i == len(shots) - 1]
        rows[str(s["shot"])] = {
            "scene_id": f"{prefix}-scene",
            "evidence_ids": [evidence[i]],
            "asset_presence": [
                {
                    "asset_id": key,
                    "visibility": "visible",
                    "position": "back" if "group" in key else "center",
                    "state": "unchanged",
                    "contains_ids": [],
                    "evidence_ids": [evidence[i]],
                    **(
                        {"holder_id": f"{prefix}-lead", "hand": "left"}
                        if key.endswith("prop")
                        else {}
                    ),
                }
                for key in ids
            ],
        }
    return {
        "assets": assets,
        "scenes": [
            {
                "id": f"{prefix}-scene",
                "shot_ids": [s["shot"] for s in shots],
                "present_asset_ids": [a["id"] for a in assets],
            }
        ],
        "shots": rows,
    }


def _verdict(shots):
    return {
        "checks": [
            {
                "shot": s["shot"],
                "status": "verified",
                "findings": [],
                "evidence_ids": [f"shot-{s['shot']}-frame-1"],
            }
            for s in shots
        ]
    }


def _install(monkeypatch, shots, responder=None, *, prefix="market"):
    calls = []

    async def complete(model, messages, **kwargs):
        verify = messages[0]["content"] == inv._VERIFY
        payload = json.loads(messages[1]["content"][0]["text"])
        calls.append({"verify": verify, "payload": payload, "content": messages[1]["content"]})
        value = (
            responder(verify, payload, calls)
            if responder
            else (
                _verdict(payload["source_shots"])
                if verify
                else _draft(payload["source_shots"], prefix=prefix)
            )
        )
        return avis_text.Completion(
            text=json.dumps(value), model=model, prompt_tokens=12, completion_tokens=8
        )

    monkeypatch.setattr(avis_text, "complete", complete)
    return calls


def test_groups_props_and_source_frames_survive_independent_verification(tmp_path, monkeypatch):
    video, work, shots = _source(tmp_path)
    shots[0]["adaptation"] = {"action": "THIS MUST NEVER BE SOURCE EVIDENCE"}
    calls = _install(monkeypatch, shots)
    inventory, report = asyncio.run(inv.analyze(video, work, shots, fps=30))
    assert report["status"] == "verified"
    assert report["reviewed_shots"] == [1, 2]
    assert report["unresolved_shots"] == []
    assert [c["verify"] for c in calls] == [False, True]
    assert all(any(p["type"] == "imageBase64" for p in c["content"]) for c in calls)
    assert "THIS MUST NEVER" not in json.dumps(calls)
    shot2 = inventory["shots"]["2"]["asset_presence"]
    assert {p["asset_id"] for p in shot2} >= {"market-group-a", "market-group-b", "market-prop"}
    prop = next(p for p in shot2 if p["asset_id"] == "market-prop")
    assert prop["holder_id"] == "market-lead" and prop["hand"] == "left"
    assert report["evidence"][0]["timestamp_s"] == 0.1
    assert report["evidence"][0]["frame"] == "frames/shot001_1.jpg"


def test_offscreen_does_not_erase_scene_membership(tmp_path, monkeypatch):
    video, work, shots = _source(tmp_path)

    def reply(verify, payload, calls):
        if verify:
            return _verdict(shots)
        result = _draft(shots)
        presence = next(
            p for p in result["shots"]["2"]["asset_presence"] if p["asset_id"] == "market-group-a"
        )
        presence.update(
            visibility="offscreen",
            position="behind reverse camera, same scene",
            evidence_ids=["shot-1-frame-1"],
        )
        return result

    _install(monkeypatch, shots, reply)
    inventory, report = asyncio.run(inv.analyze(video, work, shots, fps=30))
    assert report["status"] == "verified"
    assert "market-group-a" in inventory["scenes"][0]["present_asset_ids"]
    assert (
        next(
            p
            for p in inventory["shots"]["2"]["asset_presence"]
            if p["asset_id"] == "market-group-a"
        )["visibility"]
        == "offscreen"
    )


def test_unknown_evidence_and_missing_shot_cannot_be_verified(tmp_path, monkeypatch):
    video, work, shots = _source(tmp_path)

    def reply(verify, payload, calls):
        if verify:
            return _verdict(shots)
        result = _draft(shots)
        result["shots"]["1"]["asset_presence"][0]["evidence_ids"] = ["invented-proof"]
        del result["shots"]["2"]
        return result

    calls = _install(monkeypatch, shots, reply)
    inventory, report = asyncio.run(inv.analyze(video, work, shots, fps=30))
    assert report["status"] == "needs_review"
    assert report["unresolved_shots"] == [1, 2]
    assert {f["code"] for f in report["findings"]} >= {"invalid_evidence", "missing_shot"}
    assert len(calls) == 4  # a bounded repair and a fresh verification, no endless loop
    assert "invented-proof" not in json.dumps(report["evidence"])


def test_failed_api_preserves_unverified_inventory(tmp_path, monkeypatch):
    video, work, shots = _source(tmp_path)

    async def broken(*args, **kwargs):
        raise RuntimeError("mock provider unavailable")

    monkeypatch.setattr(avis_text, "complete", broken)
    inventory, report = asyncio.run(inv.analyze(video, work, shots, fps=30))
    assert set(inventory["shots"]) == {"1", "2"}
    assert report["status"] == "unverified" and report["unresolved_shots"] == [1, 2]
    assert any(f["code"] == "inventory_call_failed" for f in report["findings"])


def test_no_frames_never_calls_model_or_claims_verified(tmp_path, monkeypatch):
    video, work, shots = _source(tmp_path)
    for shot in shots:
        for rel in shot["frames"]:
            (work / rel).unlink()
    calls = _install(monkeypatch, shots)
    _, report = asyncio.run(inv.analyze(video, work, shots, fps=30))
    assert calls == [] and report["status"] == "unverified"


def test_adaptive_request_executes_safe_source_tool_then_reverifies(tmp_path, monkeypatch):
    video, work, shots = _source(tmp_path)
    grabbed = []

    def extract(video_path, at, out, crop):
        grabbed.append((video_path, at, crop))
        out.write_bytes(b"new-source-frame")
        return True

    monkeypatch.setattr(inv, "_extract_frame", extract)

    def reply(verify, payload, calls):
        if not verify:
            return _draft(shots)
        answer = _verdict(shots)
        if len([c for c in calls if c["verify"]]) == 1:
            answer["checks"][0].update(
                status="needs_review", findings=["Object detail needs a closer source view"]
            )
            answer["review_requests"] = [
                {
                    "shot": 1,
                    "timestamp_s": 0.45,
                    "crop": [0.2, 0.2, 0.4, 0.4],
                    "reason": "Read prop detail",
                }
            ]
        return answer

    calls = _install(monkeypatch, shots, reply)
    _, report = asyncio.run(inv.analyze(video, work, shots, fps=30))
    assert len(grabbed) == 1 and grabbed[0][0] == video
    assert report["status"] == "verified" and len(calls) == 4
    assert any(e["sampling"] == "requested_source_frame" for e in report["evidence"])
    assert any(t.get("tool") == "source_frame" and t["status"] == "ok" for t in report["trace"])


def test_tool_rejects_out_of_shot_and_invalid_crop_without_ffmpeg(tmp_path, monkeypatch):
    video, work, shots = _source(tmp_path)

    def forbidden(*args):
        raise AssertionError("unsafe request reached ffmpeg")

    monkeypatch.setattr(inv, "_extract_frame", forbidden)
    requests = [
        {"shot": 1, "timestamp_s": 50, "crop": None},
        {"shot": 2, "timestamp_s": 1.5, "crop": [0.5, 0.5, 0.9, 0.9]},
    ]
    trace = []
    assert (
        asyncio.run(inv._reinspect(requests, shots, video, work, 30, {"remaining": 10}, trace))
        == []
    )
    assert len(trace) == 2 and all(t["status"] == "rejected" for t in trace)


def test_verified_cache_invalidates_changed_frames_source_or_analysis(tmp_path, monkeypatch):
    video, work, shots = _source(tmp_path)
    calls = _install(monkeypatch, shots)
    _, first = asyncio.run(inv.analyze(video, work, shots, fps=30))
    _, repeat = asyncio.run(inv.analyze(video, work, shots, fps=30))
    assert len(calls) == 2 and repeat["digest"] == first["digest"]
    (work / shots[0]["frames"][0]).write_bytes(b"updated-frame")
    _, changed = asyncio.run(inv.analyze(video, work, shots, fps=30))
    assert len(calls) == 4 and changed["digest"] != first["digest"]
    shots[0]["source"]["action"] = "different source action"
    asyncio.run(inv.analyze(video, work, shots, fps=30))
    assert len(calls) == 6
    video.write_bytes(b"different-source-video")
    asyncio.run(inv.analyze(video, work, shots, fps=30))
    assert len(calls) == 8


def test_different_films_do_not_share_ids_assets_or_cache(tmp_path, monkeypatch):
    video, work, market = _source(tmp_path, title="desert-market")
    _install(monkeypatch, market, prefix="desert-market")
    first, report1 = asyncio.run(inv.analyze(video, work, market, fps=30))
    assert report1["status"] == "verified"
    # Deliberately reuse the same work directory and shot numbering: a new
    # source must not inherit this film's registry or successful checkpoint.
    video, work, spaceship = _source(tmp_path, title="spaceship-bridge")

    def reply(verify, payload, seen):
        assert payload["known_inventory"]["assets"] == []
        return _verdict(spaceship) if verify else _draft(spaceship, prefix="spaceship-bridge")

    second_calls = _install(monkeypatch, spaceship, reply)
    second, report2 = asyncio.run(inv.analyze(video, work, spaceship, fps=30))
    assert report2["status"] == "verified" and report2["digest"] != report1["digest"]
    assert len(second_calls) == 2
    assert {a["id"] for a in first["assets"]}.isdisjoint({a["id"] for a in second["assets"]})
    assert "desert-market" not in json.dumps(second)


def test_stable_inventory_and_visual_anchors_cross_batches(tmp_path, monkeypatch):
    video, work, shots = _source(tmp_path, count=3)
    monkeypatch.setattr(inv, "BATCH_SIZE", 2)

    def reply(verify, payload, calls):
        current = payload["source_shots"]
        if verify:
            return _verdict(current)
        result = _draft(current)
        if current[0]["shot"] == 3:
            assert {a["id"] for a in payload["known_inventory"]["assets"]} >= {
                "market-lead",
                "market-prop",
            }
            assert any("shot-1-frame-1" in p.get("text", "") for p in calls[-1]["content"])
        return result

    _install(monkeypatch, shots, reply)
    inventory, report = asyncio.run(inv.analyze(video, work, shots, fps=30))
    assert report["status"] == "verified" and len(inventory["assets"]) == 5
    assert inventory["scenes"][0]["shot_ids"] == [1, 2, 3]
    assert set(inventory["shots"]) == {"1", "2", "3"}


def test_empty_category_is_valid_and_foreign_verifier_evidence_is_not(tmp_path, monkeypatch):
    video, work, shots = _source(tmp_path)

    def reply(verify, payload, calls):
        if verify:
            result = _verdict(shots)
            result["checks"][1]["evidence_ids"] = ["shot-1-frame-1"]
            return result
        result = _draft(shots)
        result["assets"] = [a for a in result["assets"] if a["kind"] == "environment"]
        result["scenes"][0]["present_asset_ids"] = ["market-location"]
        for row in result["shots"].values():
            row["asset_presence"] = [
                p for p in row["asset_presence"] if p["asset_id"] == "market-location"
            ]
        return result

    _install(monkeypatch, shots, reply)
    inventory, report = asyncio.run(inv.analyze(video, work, shots, fps=30))
    assert len(inventory["assets"]) == 1
    assert report["status"] == "needs_review" and report["unresolved_shots"] == [2]
    assert report["reviewed_shots"] == [1]


def test_evidence_path_cannot_escape_work_directory(tmp_path):
    video, work, shots = _source(tmp_path)
    other = tmp_path / "outside.jpg"
    other.write_bytes(b"outside")
    assert inv._safe_path(work, "../outside.jpg") is None
    assert inv._safe_path(work, str(other)) is None


def test_pipeline_runs_source_agent_and_surfaces_unverified_without_losing_shots(
    tmp_path, monkeypatch
):
    from flowboard.services.video_analyzer import pipeline, vision

    video, work, shots = _source(tmp_path)

    async def measure(*args, **kwargs):
        return {
            "video": {"duration": 2, "fps": 30, "width": 1920, "height": 1080, "has_audio": False},
            "timings_s": {},
            "cuts": {},
            "deep": False,
            "spans": [
                {"index": s["shot"], "start": s["start"], "end": s["end"], "frames": s["frames"]}
                for s in shots
            ],
        }

    async def speech(*args, **kwargs):
        return {"segments": []}

    async def describe(spans, dialogue, **kwargs):
        sink = kwargs["sink"]
        sink.update(
            {s.index: {"confidence": 0.9, "subjects": [], "action": "source action"} for s in spans}
        )
        return sink, vision.VisionStats()

    async def sequences(*args):
        return [{"first_shot": 1, "last_shot": 2, "title": "Scene"}]

    async def entities(*args):
        return {"characters": []}

    called = []

    async def fail_inventory(*args, **kwargs):
        called.append(args)
        raise RuntimeError("inventory unavailable")

    monkeypatch.setattr(pipeline, "_measure", measure)
    monkeypatch.setattr(pipeline, "_speech", speech)
    monkeypatch.setattr(vision, "analyze", describe)
    monkeypatch.setattr(pipeline.adapt_mod, "build_sequences", sequences)
    monkeypatch.setattr(pipeline.adapt_mod, "extract_entities", entities)
    monkeypatch.setattr(inv, "analyze", fail_inventory)
    result = asyncio.run(pipeline.analyze(video, work))
    assert len(called) == 1 and len(result["shots"]) == 2
    assert result["source_verification"]["status"] == "unverified"
    assert result["scene_inventory"]["schema_version"] == 1
    assert any(f["code"] == "source_inventory_review" for f in result["validation"]["findings"])
    assert result["shots"][0]["source"]["action"] == "source action"


def test_inventory_digest_changes_with_presence_and_is_deterministic(tmp_path, monkeypatch):
    video, work, shots = _source(tmp_path)
    _install(monkeypatch, shots)
    inventory, report = asyncio.run(inv.analyze(video, work, shots, fps=30))
    assert report["inventory_digest"] == inv.inventory_digest(inventory)
    changed = copy.deepcopy(inventory)
    changed["shots"]["1"]["asset_presence"].pop()
    assert inv.inventory_digest(changed) != report["inventory_digest"]
    assert set(report["shot_digests"]) == {"1", "2"}
    assert set(report["asset_digests"]) == {a["id"] for a in inventory["assets"]}
