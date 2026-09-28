"""Agent model selection and Avis routing, with no real provider requests."""

from __future__ import annotations

import asyncio
import json
import runpy

import httpx
import pytest

from flowboard.services import avis_text, prompt_writer
from flowboard.services.video_analyzer import adapt, source_inventory


_OVERRIDES = (
    "FLOWBOARD_INVENTORY_MODEL", "FLOWBOARD_SOURCE_VERIFY_MODEL",
    "FLOWBOARD_PROMPT_WRITER_MODEL", "FLOWBOARD_PROMPT_WRITER_FALLBACK",
    "FLOWBOARD_PROMPT_REVIEW_MODEL", "FLOWBOARD_PROMPT_WRITER",
)


@pytest.fixture
def default_agents(monkeypatch):
    for name in _OVERRIDES:
        monkeypatch.delenv(name, raising=False)
    # Existing shot-analysis choices must not leak into the two agent engines.
    monkeypatch.setenv("FLOWBOARD_VISION_TIER1", "other-vision-model")
    monkeypatch.setenv("FLOWBOARD_VISION_TIER2", "other-vision-reviewer")
    # Fresh namespaces test startup configuration without reloading shared
    # modules or leaking the test's settings into other service tests.
    return (
        runpy.run_path(source_inventory.__file__),
        runpy.run_path(prompt_writer.__file__),
    )


def test_agent_defaults_select_luna_independently_of_legacy_vision(default_agents):
    inventory, writer = default_agents
    assert inventory["MODEL"] == inventory["VERIFY_MODEL"] == "gpt-6-luna"
    assert writer["WRITER_MODEL"] == writer["REVIEW_MODEL"] == "gpt-6-luna"
    assert writer["WRITER_FALLBACK"] == ""


def test_dedicated_model_overrides_and_explicit_fallback_remain_supported(
    default_agents, monkeypatch,
):
    monkeypatch.setenv("FLOWBOARD_INVENTORY_MODEL", "custom-inventory")
    monkeypatch.setenv("FLOWBOARD_PROMPT_WRITER_MODEL", "custom-writer")
    monkeypatch.setenv("FLOWBOARD_PROMPT_WRITER_FALLBACK", "custom-backup")
    inventory = runpy.run_path(source_inventory.__file__)
    writer = runpy.run_path(prompt_writer.__file__)
    assert inventory["MODEL"] == inventory["VERIFY_MODEL"] == "custom-inventory"
    assert writer["WRITER_MODEL"] == writer["REVIEW_MODEL"] == "custom-writer"
    assert writer["WRITER_FALLBACK"] == "custom-backup"

    monkeypatch.setenv("FLOWBOARD_SOURCE_VERIFY_MODEL", "custom-source-reviewer")
    monkeypatch.setenv("FLOWBOARD_PROMPT_REVIEW_MODEL", "custom-prompt-reviewer")
    assert runpy.run_path(source_inventory.__file__)["VERIFY_MODEL"] == "custom-source-reviewer"
    assert runpy.run_path(prompt_writer.__file__)["REVIEW_MODEL"] == "custom-prompt-reviewer"


def test_source_extraction_and_verification_use_separate_avis_image_calls(
    default_agents, monkeypatch, tmp_path,
):
    inventory, _ = default_agents
    (tmp_path / "frame.jpg").write_bytes(b"mock source image")
    evidence = [{"id": "frame-1", "frame": "frame.jpg", "timestamp_s": 0.5}]
    calls = []

    async def complete(model, messages, **kwargs):
        calls.append((model, messages))
        return avis_text.Completion(text="{}", model=model)

    monkeypatch.setattr(avis_text, "complete", complete)

    async def run():
        usage = {}
        await inventory["_ask"](inventory["_EXTRACT"], {"task": "extract"}, evidence, tmp_path, usage)
        await inventory["_ask"](
            inventory["_VERIFY"], {"task": "verify"}, evidence, tmp_path, usage, verify=True,
        )

    asyncio.run(run())
    assert [model for model, _ in calls] == ["gpt-6-luna", "gpt-6-luna"]
    assert [messages[0]["content"] for _, messages in calls] == [
        inventory["_EXTRACT"], inventory["_VERIFY"],
    ]
    assert all(len(messages) == 2 for _, messages in calls)
    assert all(
        any(part["type"] == "imageBase64" for part in messages[1]["content"])
        for _, messages in calls
    )


def test_image_writer_and_prompt_reviewer_use_luna_via_avis_in_separate_contexts(
    default_agents, monkeypatch,
):
    _, writer = default_agents
    calls = []

    async def complete(model, messages, **kwargs):
        calls.append((model, messages))
        value = ({"sections": {"FACE": "Oval face with a narrow jaw."}} if len(calls) == 1 else
                 {"status": "verified", "findings": [], "checked_requirement_ids": ["r1"]})
        return avis_text.Completion(text=json.dumps(value), model=model)

    monkeypatch.setattr(avis_text, "complete", complete)

    async def run():
        draft, model = await writer["write_image_prompt"](
            "FACE:\nOval face.\n", kind="character", subject="A character",
            design={"face": "Oval"}, style="cg3d",
        )
        review = await writer["review_prompt"](
            "[SHOT 1] A character waits.", [{"id": "r1"}], [], [],
        )
        return draft, model, review

    draft, model, review = asyncio.run(run())
    assert "narrow jaw" in draft and model == "gpt-6-luna"
    assert review["status"] == "verified"
    assert [name for name, _ in calls] == ["gpt-6-luna", "gpt-6-luna"]
    assert all(len(messages) == 2 for _, messages in calls)
    assert calls[0][1][0]["content"] != calls[1][1][0]["content"]
    assert "sections" not in json.loads(calls[1][1][1]["content"])


def test_refused_luna_clip_writer_cannot_use_adaptations_other_model_fallback(
    default_agents, monkeypatch,
):
    _, writer = default_agents
    called = []

    async def refused(model, messages, **kwargs):
        called.append(model)
        raise avis_text.AvisModelError("Test provider refusal")

    monkeypatch.setattr(avis_text, "complete", refused)
    monkeypatch.setattr(adapt, "FALLBACK_MODEL", "other-adaptation-model")
    with pytest.raises(writer["WriterError"], match="Test provider refusal"):
        asyncio.run(writer["write_clip_prompt"](
            {"label": "CLIP 01", "duration_s": 2},
            [{"duration_s": 2, "action": ["A door remains closed."]}],
            characters=[], environment=None,
        ))
    assert called == ["gpt-6-luna"]


def test_luna_request_uses_avis_alias_and_omits_unsupported_temperature(monkeypatch):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, text='data: {"delta":"{}","finishReason":"stop"}\n\n')

    real_client = httpx.AsyncClient
    monkeypatch.setattr(avis_text.httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handle), **kwargs,
    ))
    monkeypatch.setattr(avis_text, "_BASE", "https://avis.test")
    monkeypatch.setenv("AVIS_API_KEY", "test-key-no-provider-access")
    answer = asyncio.run(avis_text.complete(
        "gpt-6-luna", [{"role": "user", "content": "Return JSON."}],
        temperature=0.4, max_tokens=123, attempts=1,
    ))
    assert answer.model == "gpt-6-luna" and len(requests) == 1
    request = requests[0]
    assert str(request.url) == "https://avis.test/api/v1/text/completions"
    assert request.headers["x-api-key"] == "test-key-no-provider-access"
    body = json.loads(request.content)
    assert body["model"] == "gpt-6-luna" and body["maxTokens"] == 123
    assert "temperature" not in body
