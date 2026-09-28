"""A corrected source cannot silently reuse its previous adaptation."""

import asyncio
from copy import deepcopy
import json

import pytest
from fastapi import HTTPException

from flowboard.db import get_session
from flowboard.db.models import VideoAnalysis
from flowboard.routes import video_analysis as routes
from flowboard.services.video_analyzer import adapt as adapt_mod, pipeline


def _analysis():
    return {
        "video": {"duration": 2, "fps": 24, "width": 720, "height": 1280,
                  "has_audio": True, "aspect_ratio": "9:16"},
        "shots": [{"shot": n, "start": n - 1, "end": n,
                   "source": {"action": f"Source {n}"}, "dialogue": f"Speech {n}"}
                  for n in (1, 2)],
        "scene_inventory": {
            "assets": [{"id": key, "kind": kind, "name": key, "description": key,
                        "reference_required": True, "member_ids": [],
                        "depends_on_asset_ids": ["case"] if key == "watch" else [],
                        "evidence_ids": ["f1"]}
                       for key, kind in [("person", "character"), ("watch", "prop"),
                                         ("case", "prop"), ("hall", "environment")]],
            "scenes": [{"id": "scene", "shot_ids": [1, 2],
                        "present_asset_ids": ["person", "watch", "case", "hall"]}],
            "shots": {str(n): {"scene_id": "scene", "evidence_ids": [f"f{n}"],
                               "asset_presence": [{"asset_id": key, "visibility": "visible",
                                                   "evidence_ids": [f"f{n}"], "contains_ids": []}]}
                      for n, key in [(1, "person"), (2, "watch")]},
        },
        "source_verification": {"status": "verified", "retryable": False},
    }


def _adaptation():
    return {"rules": adapt_mod.AdaptationRules().as_dict(), "glossary": {},
            "shots": {str(n): {"action": [f"Old adaptation {n}"], "title": f"Title {n}"}
                      for n in (1, 2)}, "edits": [{"note": "Keep my work"}]}


@pytest.mark.parametrize("change,expected", [
    ("description", {1}), ("presence", {2}), ("dependency", {2}),
    ("person_profile", {1}), ("environment", {1, 2}), ("evidence", set()),
])
def test_source_changes_mark_only_affected_shots(change, expected):
    before = _analysis()
    after = deepcopy(before)
    if change == "description":
        after["shots"][0]["source"]["action"] = "Uses right hand"
    elif change == "presence":
        after["scene_inventory"]["shots"]["2"]["asset_presence"][0]["holder_id"] = "person"
    elif change in {"dependency", "person_profile", "environment"}:
        key = {"dependency": "case", "person_profile": "person", "environment": "hall"}[change]
        next(a for a in after["scene_inventory"]["assets"] if a["id"] == key)["description"] = "Corrected"
    else:
        after["source_verification"]["digest"] = "new audit"
        after["shots"][0]["source"].update(_model="new", confidence=0.9)
        after["scene_inventory"]["shots"]["1"]["evidence_ids"].append("fresh")
        after["scene_inventory"]["assets"][0]["evidence_ids"].append("fresh")
    assert pipeline.changed_source_shots(before, after) == expected


def test_markers_preserve_all_adaptation_data_and_previous_unresolved_changes():
    before = _analysis()
    after = deepcopy(before)
    after["shots"][0]["source"]["action"] = "Corrected"
    previous = _adaptation()
    previous["stale_source_shots"] = [2]
    original = deepcopy(previous)
    marked = pipeline.mark_stale_adaptation(before, after, previous)
    assert marked == {**original, "stale_source_shots": [1, 2]}
    assert previous == original
    legacy = _adaptation()
    assert pipeline.mark_stale_adaptation(before, before, legacy) == legacy
    assert "stale_source_shots" not in legacy


@pytest.mark.parametrize("partial", [False, True])
def test_adapt_regenerates_marked_shots_and_retains_marker_on_partial_failure(monkeypatch, partial):
    previous = _adaptation()
    previous["stale_source_shots"] = [2]
    asked = []

    async def regenerate(*args, only=None, **kwargs):
        asked.append(only)
        return {} if partial else {2: {"action": ["Fresh adaptation"]}}

    monkeypatch.setattr(adapt_mod, "adapt_shots", regenerate)
    monkeypatch.setattr(pipeline, "ensure_dialogue", lambda _: None)
    out = asyncio.run(pipeline.adapt(_analysis(), adapt_mod.AdaptationRules(),
                                    glossary={}, previous=previous))
    assert asked == [{2}]
    assert out["shots"]["1"] == previous["shots"]["1"]
    assert out["edits"] == previous["edits"]
    if partial:
        assert out["stale_source_shots"] == [2]
        assert out["shots"]["2"] == previous["shots"]["2"]
    else:
        assert not out.get("stale_source_shots")
        assert out["shots"]["2"]["action"] == ["Fresh adaptation"]


def test_legacy_adaptation_without_marker_still_reuses_shots(monkeypatch):
    async def unexpected(*args, **kwargs):
        raise AssertionError("No changed source marker means no extra paid model call")

    monkeypatch.setattr(adapt_mod, "adapt_shots", unexpected)
    monkeypatch.setattr(pipeline, "ensure_dialogue", lambda _: None)
    previous = _adaptation()
    result = asyncio.run(pipeline.adapt(_analysis(), adapt_mod.AdaptationRules(),
                                       glossary={}, previous=previous))
    assert result["shots"] == previous["shots"]
    assert not result.get("stale_source_shots")


def _save_row(adaptation=None, cast=None):
    with get_session() as session:
        row = VideoAnalysis(name="Generic source", filename="source.mp4", status="adapted",
                            analysis=_analysis(), adaptation=adaptation or _adaptation(), cast=cast or {})
        session.add(row)
        session.commit()
        session.refresh(row)
        return row.id


def test_refinement_marks_latest_adaptation_without_losing_new_media(tmp_path, monkeypatch):
    cast = {"characters": [{"key": "person", "name": "Person", "source_asset_id": "person",
                            "plate": {"url": "existing.png"}}]}
    key = _save_row(cast=cast)
    path = tmp_path / "source.mp4"
    path.write_bytes(b"source")
    monkeypatch.setattr(routes, "_source_path", lambda _: path)
    monkeypatch.setattr(routes, "_work_dir", lambda _: tmp_path)

    async def refine(source, work, analysis, **kwargs):
        # A user can finish a media upload while source refinement is running.
        with get_session() as session:
            row = session.get(VideoAnalysis, key)
            latest_cast = deepcopy(row.cast)
            latest_cast["characters"][0]["plate"] = {"url": "new-paid-media.png"}
            row.cast = latest_cast
            row.adaptation = {**row.adaptation, "edits": [{"note": "New edit"}]}
            session.add(row)
            session.commit()
        result = deepcopy(analysis)
        result["shots"][0]["source"]["action"] = "Corrected right hand"
        return result

    monkeypatch.setattr(routes.source_refinement, "refine", refine)
    asyncio.run(routes._run_source_refinement(key))
    with get_session() as session:
        row = session.get(VideoAnalysis, key)
        assert row.status == "adapted"
        assert row.adaptation["stale_source_shots"] == [1]
        assert row.adaptation["shots"] == _adaptation()["shots"]
        assert row.adaptation["edits"] == [{"note": "New edit"}]
        person = next(c for c in row.cast["characters"] if c["source_asset_id"] == "person")
        assert person["plate"] == {"url": "new-paid-media.png"}


def test_stale_board_is_blocked_and_exports_omit_only_old_overlay(monkeypatch):
    adaptation = _adaptation()
    adaptation["stale_source_shots"] = [1]
    key = _save_row(adaptation=adaptation)

    async def unexpected(*args, **kwargs):
        raise AssertionError("Do not prepare a stale board or call a cast model")

    monkeypatch.setattr(routes.board_mod, "build_board", unexpected)
    with pytest.raises(HTTPException) as err:
        asyncio.run(routes.to_board(key, user=None))
    assert err.value.status_code == 409 and "Adapt" in err.value.detail
    doc = json.loads(routes.export_video(key, format="json", user=None).body)
    assert doc["adaptation"]["stale_source_shots"] == [1]
    assert doc["shots"][0]["adaptation"] is None
    assert doc["shots"][0]["source"]["action"] == "Source 1"
    assert doc["shots"][1]["adaptation"] == adaptation["shots"]["2"]
    markdown = routes.export_video(key, user=None).body.decode()
    assert "Old adaptation 1" not in markdown
    assert "Old adaptation 2" in markdown and "Bấm Adapt" in markdown
    with get_session() as session:
        assert session.get(VideoAnalysis, key).adaptation == adaptation
