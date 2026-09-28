"""Independent visual layer checks retain uncertainty and source provenance."""
import asyncio
import copy
import json

from PIL import Image
import pytest

from flowboard.services import avis_text
from flowboard.services.video_analyzer import source_asset_layers as layers
from flowboard.services.video_analyzer import source_inventory as inv


def _case(tmp_path, ids=("overlay", "book", "person"), count=3):
    evidence = []
    for number in range(1, count + 1):
        path = tmp_path / f"frame-{number}.png"
        Image.new("RGB", (12, 12), (number * 20, 50, 70)).save(path)
        evidence.append({"id": f"e{number}", "shot": number,
                         "timestamp_s": number - 0.5, "frame": path.name})
    assets = [{"id": key, "name": key, "kind": "prop", "description": "A source visual element",
               "evidence_ids": ["e1"], "member_ids": [], "depends_on_asset_ids": []}
              for key in ids]
    inventory = {"schema_version": inv.SCHEMA_VERSION, "assets": assets,
                 "scenes": [{"id": "room", "shot_ids": list(range(1, count + 1)),
                             "present_asset_ids": list(ids)}],
                 "shots": {str(n): {"scene_id": "room", "evidence_ids": [f"e{n}"],
                           "asset_presence": [{"asset_id": key, "visibility": "visible",
                                               "evidence_ids": [f"e{n}"], "contains_ids": [],
                                               "position": "center", "state": "unchanged"}
                                              for key in ids]}
                           for n in range(1, count + 1)}}
    return inventory, evidence


def _decision(candidate, layer="physical"):
    return {"asset_id": candidate["asset_id"], "layer": layer,
            "evidence_ids": candidate["anchor_evidence_ids"], "reason": "Visible source image placement"}


def _install(monkeypatch, responder=None):
    calls = []

    async def complete(model, messages, **kwargs):
        payload = json.loads(messages[1]["content"][0]["text"])
        role = "review" if messages[0]["content"] == layers.REVIEW_SYSTEM else "classify"
        calls.append({"role": role, "payload": copy.deepcopy(payload), "model": model})
        value = responder(role, payload, len(calls)) if responder else {
            "decisions": [_decision(c, "screen_graphic" if c["asset_id"] == "overlay" else "physical")
                          for c in payload["assets"]]}
        if isinstance(value, Exception):
            raise value
        return avis_text.Completion(text=json.dumps(value), model=model,
                                    prompt_tokens=10, completion_tokens=8)

    monkeypatch.setattr(avis_text, "complete", complete)
    return calls


def _run(tmp_path, inventory, evidence, journal=None, limit=4):
    return asyncio.run(layers.classify_layers(inv, inventory, evidence,
                      journal if journal is not None else {}, tmp_path, inv._CallLimiter(limit), lambda: None))


def test_two_independent_checks_migrate_graphics_and_preserve_source_records(tmp_path, monkeypatch):
    inventory, evidence = _case(tmp_path)
    original = copy.deepcopy(inventory)
    calls = _install(monkeypatch)
    result, findings, audit = _run(tmp_path, inventory, evidence)
    assert findings == []
    assert inventory == original
    assert {a["id"] for a in result["assets"]} == {"book", "person"}
    assert result["screen_graphics"] == [original["assets"][0]]
    for key, row in result["shots"].items():
        assert row["screen_graphics"] == [original["shots"][key]["asset_presence"][0]]
        assert {p["asset_id"] for p in row["asset_presence"]} == {"book", "person"}
    assert result["scenes"][0]["present_asset_ids"] == ["book", "person"]
    assert result["scenes"][0]["screen_graphic_ids"] == ["overlay"]
    assert audit["moved_asset_ids"] == ["overlay"]
    assert len(calls) == 2
    assert {c["role"] for c in calls} == {"classify", "review"}
    assert calls[0]["payload"] == calls[1]["payload"]
    assert set(calls[0]["payload"]) == {"assets"}
    assert all(c["anchor_evidence_ids"] == ["e1", "e3"] for c in calls[0]["payload"]["assets"])


@pytest.mark.parametrize("review_layer", ["physical", "uncertain"])
def test_disagreement_or_uncertainty_retains_asset_and_blocks_its_appearances(tmp_path, monkeypatch, review_layer):
    inventory, evidence = _case(tmp_path, ids=("overlay",))
    _install(monkeypatch, lambda role, payload, _: {"decisions": [
        _decision(c, review_layer if role == "review" else "screen_graphic") for c in payload["assets"]]})
    result, findings, audit = _run(tmp_path, inventory, evidence)
    assert result["assets"] == inventory["assets"]
    assert result["screen_graphics"] == []
    assert {f["shot"] for f in findings} == {1, 2, 3}
    assert {f["code"] for f in findings} == {"asset_layer_uncertain"}
    assert audit["moved_asset_ids"] == []


@pytest.mark.parametrize("malformation", ["foreign_evidence", "first_only", "duplicate", "missing", "unknown_layer", "bad_container"])
def test_invalid_visual_decisions_never_remove_assets(tmp_path, monkeypatch, malformation):
    inventory, evidence = _case(tmp_path, ids=("overlay",))

    def reply(role, payload, _):
        row = _decision(payload["assets"][0], "screen_graphic")
        if role != "review":
            return {"decisions": [row]}
        if malformation == "foreign_evidence": row["evidence_ids"] = ["another-asset-frame"]
        if malformation == "first_only": row["evidence_ids"] = ["e1"]
        if malformation == "unknown_layer": row["layer"] = "probably_overlay"
        if malformation == "duplicate": return {"decisions": [row, row]}
        if malformation == "missing": return {"decisions": []}
        if malformation == "bad_container": return {"decisions": {"overlay": row}}
        return {"decisions": [row]}

    _install(monkeypatch, reply)
    result, findings, _ = _run(tmp_path, inventory, evidence)
    assert result["assets"] == inventory["assets"]
    assert result["screen_graphics"] == []
    assert findings


def test_names_do_not_override_visual_physical_or_effect_decisions(tmp_path, monkeypatch):
    inventory, evidence = _case(tmp_path, ids=("caption", "logo", "title", "xray-person", "skeleton-effect"))
    inventory["assets"][0]["description"] = "A photographed paper sign"
    inventory["assets"][1]["description"] = "A printed book jacket"
    inventory["assets"][2]["description"] = "A physical framed photograph"
    inventory["assets"][3]["kind"] = "character"
    _install(monkeypatch, lambda role, payload, _: {"decisions": [
        _decision(c, "uncertain" if c["asset_id"] == "skeleton-effect" else "physical")
        for c in payload["assets"]]})
    result, findings, _ = _run(tmp_path, inventory, evidence)
    assert result["assets"] == inventory["assets"]
    assert result["screen_graphics"] == []
    assert {f["asset_id"] for f in findings} == {"skeleton-effect"}
    assert "photograph" in layers.SYSTEM and "Do not invent people" in layers.SYSTEM


@pytest.mark.parametrize("broken", ["missing", "not_image", "stale_hash"])
def test_unreadable_or_changed_evidence_does_not_trigger_ungrounded_calls(tmp_path, monkeypatch, broken):
    inventory, evidence = _case(tmp_path, ids=("overlay",), count=1)
    if broken == "missing":
        (tmp_path / evidence[0]["frame"]).unlink()
    elif broken == "not_image":
        (tmp_path / evidence[0]["frame"]).write_text("not an image")
    else:
        evidence[0]["sha256"] = "outdated hash"
    calls = _install(monkeypatch)
    result, findings, _ = _run(tmp_path, inventory, evidence)
    assert result["assets"] == inventory["assets"]
    assert {f["code"] for f in findings} == {"asset_layer_evidence_missing"}
    assert calls == []


@pytest.mark.parametrize("edge", ["depends", "member", "holder", "contains", "incoming", "missing_target"])
def test_cross_layer_relationships_block_migration_without_erasing_edges(tmp_path, monkeypatch, edge):
    inventory, evidence = _case(tmp_path, ids=("overlay", "book"))
    if edge == "depends": inventory["assets"][0]["depends_on_asset_ids"] = ["book"]
    elif edge == "member": inventory["assets"][0]["member_ids"] = ["book"]
    elif edge == "incoming": inventory["assets"][1]["depends_on_asset_ids"] = ["overlay"]
    elif edge == "missing_target": inventory["assets"][0]["depends_on_asset_ids"] = ["unknown"]
    else:
        for row in inventory["shots"].values():
            row["asset_presence"][0]["holder_id" if edge == "holder" else "contains_ids"] = "book" if edge == "holder" else ["book"]
    _install(monkeypatch)
    result, findings, audit = _run(tmp_path, inventory, evidence)
    assert result["assets"] == inventory["assets"]
    assert result["screen_graphics"] == []
    assert audit["retained_graphic_ids"] == ["overlay"]
    assert {f["code"] for f in findings} == {"asset_layer_dependency_conflict"}
    assert all(result["shots"][n]["asset_presence"] == row["asset_presence"] for n, row in inventory["shots"].items())


def test_graphic_only_relationships_migrate_together_with_original_ids(tmp_path, monkeypatch):
    inventory, evidence = _case(tmp_path, ids=("overlay", "graphic-child"))
    inventory["assets"][0]["depends_on_asset_ids"] = ["graphic-child"]
    inventory["assets"][0]["member_ids"] = ["graphic-child"]
    for row in inventory["shots"].values():
        row["asset_presence"][0].update(holder_id="graphic-child", contains_ids=["graphic-child"])
    _install(monkeypatch, lambda role, payload, _: {"decisions": [_decision(c, "screen_graphic") for c in payload["assets"]]})
    result, findings, _ = _run(tmp_path, inventory, evidence)
    assert result["assets"] == []
    assert result["screen_graphics"] == inventory["assets"]
    assert findings == []
    for n, row in inventory["shots"].items():
        assert result["shots"][n]["screen_graphics"] == row["asset_presence"]
        assert result["shots"][n]["asset_presence"] == []


def test_dependency_block_propagates_across_graphic_component(tmp_path, monkeypatch):
    inventory, evidence = _case(tmp_path, ids=("overlay", "graphic-child", "person"))
    inventory["assets"][0]["depends_on_asset_ids"] = ["graphic-child"]
    inventory["assets"][1]["depends_on_asset_ids"] = ["person"]
    _install(monkeypatch, lambda role, payload, _: {"decisions": [
        _decision(c, "physical" if c["asset_id"] == "person" else "screen_graphic") for c in payload["assets"]]})
    result, findings, _ = _run(tmp_path, inventory, evidence)
    assert result["screen_graphics"] == []
    assert {f["asset_id"] for f in findings} == {"overlay", "graphic-child", "person"}


def test_completed_layers_resume_without_repaying_and_changed_images_invalidate(tmp_path, monkeypatch):
    inventory, evidence = _case(tmp_path)
    journal = {}
    calls = _install(monkeypatch)
    first = _run(tmp_path, inventory, evidence, journal)
    again = _run(tmp_path, inventory, evidence, journal)
    assert again == first
    assert len(calls) == 2
    Image.new("RGB", (12, 12), (200, 30, 20)).save(tmp_path / "frame-3.png")
    _run(tmp_path, inventory, evidence, journal)
    assert len(calls) == 4
    entry = next(iter(journal["batches"].values()))
    assert entry["context_history"][0]["calls"]


def test_failed_review_retries_only_review_and_keeps_completed_classification(tmp_path, monkeypatch):
    inventory, evidence = _case(tmp_path, ids=("overlay",))
    failed = False

    def reply(role, payload, _):
        nonlocal failed
        if role == "review" and not failed:
            failed = True
            return avis_text.AvisTextError("transient failure")
        return {"decisions": [_decision(c, "screen_graphic") for c in payload["assets"]]}

    calls = _install(monkeypatch, reply)
    journal = {}
    result, findings, _ = _run(tmp_path, inventory, evidence, journal)
    assert result["screen_graphics"] == [] and findings
    restored, findings, _ = _run(tmp_path, inventory, evidence, journal)
    assert len(restored["screen_graphics"]) == 1 and findings == []
    assert [c["role"] for c in calls].count("classify") == 1
    assert [c["role"] for c in calls].count("review") == 2


def test_partial_review_resume_only_requests_omitted_asset(tmp_path, monkeypatch):
    inventory, evidence = _case(tmp_path, ids=("overlay", "book"))
    skipped = False

    def reply(role, payload, _):
        nonlocal skipped
        candidates = payload["assets"]
        if role == "review" and not skipped:
            skipped = True
            candidates = candidates[:1]
        return {"decisions": [_decision(c, "physical") for c in candidates]}

    journal = {}
    calls = _install(monkeypatch, reply)
    _, findings, _ = _run(tmp_path, inventory, evidence, journal)
    assert {f["asset_id"] for f in findings} == {"book"}
    _, findings, _ = _run(tmp_path, inventory, evidence, journal)
    assert findings == []
    assert len(calls) == 3
    assert calls[-1]["role"] == "review"
    assert [c["asset_id"] for c in calls[-1]["payload"]["assets"]] == ["book"]


def test_all_batches_overlap_under_shared_avis_limit(tmp_path, monkeypatch):
    inventory, evidence = _case(tmp_path, ids=tuple(f"asset-{n}" for n in range(48)), count=1)
    active = peak = 0
    payload_sizes = []

    async def complete(model, messages, **kwargs):
        nonlocal active, peak
        payload = json.loads(messages[1]["content"][0]["text"])
        payload_sizes.append(len(payload["assets"]))
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.01)
            return avis_text.Completion(text=json.dumps({"decisions": [_decision(c) for c in payload["assets"]]}), model=model)
        finally:
            active -= 1

    monkeypatch.setattr(avis_text, "complete", complete)
    _, findings, _ = _run(tmp_path, inventory, evidence, limit=10)
    assert findings == []
    assert peak == 10  # No independent four-batch worker cap.
    assert active == 0
    assert payload_sizes == [8] * 12


def test_cancelled_layer_check_awaits_all_model_tasks(tmp_path, monkeypatch):
    inventory, evidence = _case(tmp_path, ids=tuple(f"asset-{n}" for n in range(24)), count=1)
    active = 0
    reached = None
    journal = {}

    async def complete(*args, **kwargs):
        nonlocal active
        active += 1
        reached.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1

    monkeypatch.setattr(avis_text, "complete", complete)

    async def interrupt():
        nonlocal reached
        reached = asyncio.Event()
        task = asyncio.create_task(layers.classify_layers(inv, inventory, evidence, journal,
                                   tmp_path, inv._CallLimiter(4), lambda: None))
        await asyncio.wait_for(reached.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert active == 0

    asyncio.run(interrupt())
    assert "result" not in journal
