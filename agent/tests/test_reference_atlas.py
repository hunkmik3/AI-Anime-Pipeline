"""An atlas saves image slots, never source asset coverage or reference integrity."""
import json

import pytest
from fastapi import HTTPException

from flowboard.routes import automation as routes
from flowboard.services import prompt_coverage as coverage
from flowboard.services.video_analyzer import adapt
from tests.test_prompt_coverage import _draft, _film, _review, _run


def _atlas_film():
    data = _film(True)
    _, _, cast, extra, _, _ = data
    cast[0]["media_id"] = "person-media"
    for reference in extra:
        reference.update(ref_label="@image2", ref_url="https://example.test/atlas.png", media_id="atlas-media",
                         description=f"Distinct description for {reference['id']}")
    return data


def test_atlas_shares_one_slot_and_retains_both_asset_bindings():
    _, shots, cast, extra, assets, report = _atlas_film()
    assert coverage.validate_source_contract(shots, assets, report, cast + extra) == []
    slots, issues = coverage.reference_slots(cast + extra)
    assert not issues
    assert [slot["ref_label"] for slot in slots] == ["@image1", "@image2"]
    assert [coverage.asset_id(binding) for binding in slots[1]["asset_bindings"]] == ["group", "prop"]
    assert [slot["ref_url"] for slot in slots] == [cast[0]["ref_url"], extra[0]["ref_url"]]


@pytest.mark.parametrize("change", ["url", "media", "duplicate_asset", "missing_id", "gap", "invalid_label"])
def test_atlas_rejects_ambiguous_or_accidental_duplicate_bindings(change):
    _, shots, cast, extra, assets, report = _atlas_film()
    if change == "url": extra[1]["ref_url"] = "https://example.test/different.png"
    elif change == "media": extra[1]["media_id"] = "different-media"
    elif change == "duplicate_asset": extra[1]["id"] = extra[0]["id"]
    elif change == "missing_id": extra[1].pop("id")
    elif change == "gap": extra[0]["ref_label"] = extra[1]["ref_label"] = "@image3"
    else: extra[1]["ref_label"] = "@image0"
    assert coverage.validate_source_contract(shots, assets, report, cast + extra)


def test_over_thirty_distinct_slots_are_rejected_before_paid_generation():
    references = [{"id": f"asset-{i}", "ref_label": f"@image{i}", "ref_url": f"https://example.test/{i}.png"}
                  for i in range(1, 32)]
    assert any("at most 30" in error for error in coverage.reference_slots(references)[1])


def test_writer_has_one_atlas_declaration_without_losing_requirements(monkeypatch):
    calls = []
    async def fake(system, user, stats, **kwargs):
        ask = json.loads(user)
        calls.append(ask)
        return _review(ask) if "requirements" in ask else _draft(ask)
    monkeypatch.setattr(adapt, "ask_json", fake)
    result = _run(_atlas_film())
    refs = calls[0]["references"]
    assert len(refs) == 2
    assert refs[1]["kind"] == "asset_atlas"
    assert {entry["id"] for entry in refs[1]["asset_bindings"]} == {"group", "prop"}
    assert all(entry["keep"].startswith("Distinct description") for entry in refs[1]["asset_bindings"])
    assert result.prompt.count("@image2") == 1
    requirements = result.coverage["requirements"]
    assert {entry["asset_id"] for entry in requirements if entry["kind"] == "presence"} == {"person", "group", "prop"}


def _receipt(monkeypatch):
    monkeypatch.setattr(routes.auth, "_server_secret", lambda: b"test-only-secret")
    seq, shots, cast, extra, assets, report = _atlas_film()
    contract = routes.VideoWriteBody(sequence=seq, shots=shots, characters=cast, reference_assets=extra,
                                     production_assets=assets, source_verification=report, aspect_ratio="9:16")
    prompt = "[SHOT 1 — 00:00–00:03]\nThe lead waits.\n[SHOT 2 — 00:03–00:06]\nThe lead waits."
    digest = routes._writing_digest(contract)
    reviewed = {"status": "verified", "contract_digest": digest, "prompt_digest": coverage.prompt_digest(prompt),
                "semantic_review": {"status": "verified", "findings": []},
                "requirements": [{"id": "action", "shot": 1}],
                "matches": [{"requirement_id": "action", "shot": 1, "quote": "The lead waits."}]}
    return routes.ClipBody(prompt=prompt, prompt_contract=contract, duration_seconds=6, aspect_ratio="9:16",
                           reference_urls=[cast[0]["ref_url"], extra[0]["ref_url"]],
                           kyc_media_ids=["person-media", "atlas-media"], coverage=reviewed, contract_digest=digest,
                           coverage_token=routes._coverage_token(contract, prompt, 6, reviewed))


def test_generation_transports_each_atlas_slot_once_with_signed_full_bindings(monkeypatch):
    request = _receipt(monkeypatch)
    routes._validate_generation_contract(request)
    assert len(request.prompt_contract.reference_assets) == 2
    assert len(request.reference_urls) == 2


@pytest.mark.parametrize("change", ["duplicate_transport", "reordered_transport", "wrong_media", "changed_binding"])
def test_generation_rejects_atlas_transport_or_binding_changes(monkeypatch, change):
    request = _receipt(monkeypatch)
    if change == "duplicate_transport": request.reference_urls.append(request.reference_urls[-1])
    elif change == "reordered_transport": request.reference_urls.reverse()
    elif change == "wrong_media": request.kyc_media_ids[-1] = "wrong-media"
    else: request.prompt_contract.reference_assets[0]["id"] = "invented-asset"
    with pytest.raises(HTTPException) as error:
        routes._validate_generation_contract(request)
    assert error.value.status_code == 422
