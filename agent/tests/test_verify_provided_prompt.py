"""Provided drafts earn the normal receipt only after the real review path."""
import asyncio
from copy import deepcopy
import json

import pytest
from fastapi import HTTPException

from flowboard.routes import automation as routes
from flowboard.services import automation, production_adaptation
from flowboard.services import prompt_coverage as coverage
from flowboard.services import prompt_writer as writer
from flowboard.services.video_analyzer import adapt
from tests.test_prompt_coverage import _draft, _film, _review
from tests.test_production_adaptation import _case


def _contract():
    seq, shots, cast, extra, assets, report = _film(True)
    return routes.VideoWriteBody(sequence=seq, shots=shots, characters=cast, reference_assets=extra,
                                 production_assets=assets, source_verification=report,
                                 style="cg3d", aspect_ratio="9:16", previous_state="Previous shot ends at the dock.")


def _context(contract):
    target = [production_adaptation.apply(shot) for shot in contract.shots]
    fitted = automation.fit_shots_to_lines(target)
    slots, duration = writer.whole_second_timeline(fitted, automation.clip_seconds(contract.sequence, target))
    ask = writer._payload(contract.sequence, fitted, slots, duration, characters=contract.characters,
                          environment=contract.environment, look=contract.style, aspect_ratio=contract.aspect_ratio,
                          previous_state=contract.previous_state, unsafe={}, school_age=False,
                          reference_assets=contract.reference_assets, strict=True)
    ask["production_assets"] = contract.production_assets
    ask["coverage_requirements"] = coverage.build_requirements(ask["shots"], fitted, contract.production_assets)
    return ask


def _body():
    contract = _contract()
    # Whitespace is intentional: verification must never rewrite a manual draft.
    prompt = _draft(_context(contract))["prompt"] + "\n\n"
    return routes.VerifyPromptBody(prompt_contract=contract, prompt=prompt, end_state="Lead waits with the core in the left hand.")


def _install(monkeypatch, verdict=None):
    calls = []
    async def model(system, user, stats, **kwargs):
        ask = json.loads(user)
        calls.append((ask, kwargs))
        assert "requirements" in ask and "clip" not in ask, "Only the independent review may run."
        return verdict(ask) if verdict else _review(ask)
    monkeypatch.setattr(adapt, "ask_json", model)
    monkeypatch.setattr(writer, "WRITER_ON", True)
    monkeypatch.setattr(routes.auth, "_server_secret", lambda: b"provided-prompt-test-only")
    return calls


def test_route_preserves_draft_and_returns_signed_receipt_accepted_by_generation(client, monkeypatch):
    calls = _install(monkeypatch)
    body = _body()
    original = deepcopy(body.prompt_contract.model_dump())
    response = client.post("/api/automation/video/verify-prompt", json=body.model_dump())
    assert response.status_code == 200, response.text
    out = response.json()
    assert out["prompt"] == body.prompt
    assert out["end_state"] == body.end_state
    assert out["writer"] == "provided_prompt"
    assert out["duration_seconds"] == 6
    assert len(calls) == 1
    ask, options = calls[0]
    assert options["model"] == writer.REVIEW_MODEL and options["fallback"] == "" and options["attempts"] == 1
    assert ask["prompt"] == body.prompt and ask["opening_state"] == body.prompt_contract.previous_state
    assert ask["end_state"] == body.end_state
    assert body.prompt_contract.model_dump() == original
    assert out["coverage"]["matches"] == coverage.full_shot_coverage(body.prompt, out["coverage"]["requirements"])
    assert out["coverage"]["evidence_scope"] == "full_shot"
    assert out["contract_digest"] == routes._writing_digest(body.prompt_contract)
    refs = [*body.prompt_contract.characters, *body.prompt_contract.reference_assets]
    slots, issues = coverage.reference_slots(refs)
    assert not issues
    request = routes.ClipBody(prompt=out["prompt"], prompt_contract=body.prompt_contract,
        duration_seconds=out["duration_seconds"], aspect_ratio="9:16", reference_urls=[slot["ref_url"] for slot in slots],
        coverage=out["coverage"], contract_digest=out["contract_digest"], coverage_token=out["coverage_token"])
    routes._validate_generation_contract(request)
    request.prompt += "An unchecked visitor arrives."
    with pytest.raises(HTTPException):
        routes._validate_generation_contract(request)


@pytest.mark.parametrize("defect", ["unresolved_source", "missing_ref", "wrong_timeline", "wrong_reference", "empty", "legacy", "missing_dialogue", "wrong_speaker"])
def test_invalid_input_never_calls_review_or_returns_receipt(monkeypatch, defect):
    calls = _install(monkeypatch)
    body = _body()
    if defect == "unresolved_source": body.prompt_contract.source_verification["unresolved_shots"] = [2]
    elif defect == "missing_ref": body.prompt_contract.reference_assets.pop()
    elif defect == "wrong_timeline": body.prompt = body.prompt.replace("00:00–00:03", "00:00–00:04")
    elif defect == "wrong_reference": body.prompt = body.prompt.replace("@image1", "@image8")
    elif defect == "empty": body.prompt = "   "
    elif defect == "legacy": body.prompt_contract.production_assets = None; body.prompt_contract.source_verification = None
    else:
        body.prompt_contract.shots[0]["dialogue"] = [{"who": "MIRA SOL, OFF SCREEN", "line": "Hold the core."}]
        if defect == "wrong_speaker": body.prompt = body.prompt.replace("[SHOT 1 — 00:00–00:03]", '[SHOT 1 — 00:00–00:03]\nPILOT:\n"Hold the core."')
    with pytest.raises(HTTPException) as error:
        asyncio.run(routes.verify_video_prompt(body))
    assert error.value.status_code == 422
    assert calls == []


@pytest.mark.parametrize("defect", ["missing_ids", "duplicate_ids", "needs_revision", "prompt_issue", "source_issue"])
def test_independent_review_must_check_every_id_and_report_no_findings(monkeypatch, defect):
    def verdict(ask):
        result = _review(ask)
        if defect == "missing_ids": result["checked_requirement_ids"].pop()
        elif defect == "duplicate_ids": result["checked_requirement_ids"][-1] = result["checked_requirement_ids"][0]
        elif defect == "needs_revision": result["status"] = "needs_revision"
        else: result["findings"] = [{"kind": defect, "requirement_id": ask["requirements"][0]["id"], "message": "A required visual fact is absent."}]
        return result
    calls = _install(monkeypatch, verdict)
    with pytest.raises(HTTPException) as error:
        asyncio.run(routes.verify_video_prompt(_body()))
    assert error.value.status_code == 422 and len(calls) == 1


def test_explicit_overlay_is_reviewed_but_raw_source_and_anchor_remain_intact(monkeypatch):
    shot, assets, report, cast, refs = _case()
    contract = routes.VideoWriteBody(sequence={"duration_s": 4}, shots=[shot], characters=cast,
        reference_assets=refs, production_assets=assets, source_verification=report, style="cg3d", aspect_ratio="9:16")
    before = deepcopy(contract.model_dump())
    context = _context(contract)
    duration = context["clip"]["duration_seconds"]
    declarations = "\n".join(f"{ref['tag']} — {ref['name']}. Reference." for ref in context["references"])
    prompt = (f"DURATION: {duration} seconds.\n{declarations}\n[SPECIFIC TIMELINE]\n"
              f"[SHOT 1 — {context['shots'][0]['time']}]\n"
              "Medium shot. The adult remains fully clothed in an opaque tailored suit. A cyan outline traces the closed box exterior.\n"
              "[OVERALL SUPPLEMENT]\n")
    calls = _install(monkeypatch)
    body = routes.VerifyPromptBody(prompt_contract=contract, prompt=prompt)
    result = asyncio.run(routes.verify_video_prompt(body))
    assert result.writer == "provided_prompt"
    assert "lingerie" not in json.dumps(calls[0][0]["requirements"])
    assert any(requirement["kind"] == "production_appearances" for requirement in calls[0][0]["requirements"])
    assert contract.model_dump() == before
    contract.shots[0]["production_adaptation"]["source_digest"] = "stale"
    with pytest.raises(HTTPException): asyncio.run(routes.verify_video_prompt(body))
    assert len(calls) == 1


def test_review_failure_does_not_rewrite_or_fall_back(monkeypatch):
    calls = []
    async def unavailable(*args, **kwargs):
        calls.append(kwargs)
        raise RuntimeError("review unavailable")
    monkeypatch.setattr(adapt, "ask_json", unavailable)
    with pytest.raises(HTTPException) as error:
        asyncio.run(routes.verify_video_prompt(_body()))
    assert error.value.status_code == 422
    assert "Independent prompt review failed" in error.value.detail and len(calls) == 1
