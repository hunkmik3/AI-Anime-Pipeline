"""Resuming the source agent must not regenerate nondeterministic story inputs."""
import asyncio
import copy

from flowboard.services.video_analyzer import pipeline


def test_story_reuses_identical_inputs_and_invalidates_changed_source_or_model(tmp_path, monkeypatch):
    shots = [{"shot": 1, "source": {"action": "holds a box"}, "dialogue": "Hello."}]
    calls = {"sequences": 0, "entities": 0}

    async def sequences(rows, stats):
        calls["sequences"] += 1
        return [{"first_shot": 1, "last_shot": 1, "title": str(calls["sequences"])}]

    async def entities(rows, stats):
        calls["entities"] += 1
        return {"characters": [{"source_name": "Lead"}]}

    monkeypatch.setattr(pipeline.adapt_mod, "build_sequences", sequences)
    monkeypatch.setattr(pipeline.adapt_mod, "extract_entities", entities)
    original = asyncio.run(pipeline._story(shots, tmp_path))
    restored = asyncio.run(pipeline._story(shots, tmp_path))
    assert restored[:2] == original[:2]
    assert calls == {"sequences": 1, "entities": 1}
    changed = copy.deepcopy(shots)
    changed[0]["source"]["action"] = "drops the box"
    asyncio.run(pipeline._story(changed, tmp_path))
    assert calls == {"sequences": 2, "entities": 2}
    monkeypatch.setattr(pipeline.adapt_mod, "TEXT_MODEL", "changed-model")
    asyncio.run(pipeline._story(changed, tmp_path))
    assert calls == {"sequences": 3, "entities": 3}


def test_failed_story_stage_retries_without_repaying_successful_sibling(tmp_path, monkeypatch):
    shots = [{"shot": 1, "source": {}, "dialogue": "Hello."}]
    calls = {"sequences": 0, "entities": 0}

    async def sequences(rows, stats):
        calls["sequences"] += 1
        return [{"first_shot": 1, "last_shot": 1, "title": "Stable"}]

    async def entities(rows, stats):
        calls["entities"] += 1
        if calls["entities"] == 1:
            raise RuntimeError("temporary gateway outage")
        return {"characters": []}

    monkeypatch.setattr(pipeline.adapt_mod, "build_sequences", sequences)
    monkeypatch.setattr(pipeline.adapt_mod, "extract_entities", entities)
    first = asyncio.run(pipeline._story(shots, tmp_path))
    assert first[3] == ["entities: temporary gateway outage"]
    resumed = asyncio.run(pipeline._story(shots, tmp_path))
    assert resumed[3] == []
    assert calls == {"sequences": 1, "entities": 2}

