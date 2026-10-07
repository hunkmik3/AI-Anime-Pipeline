import asyncio
import copy

import pytest

from flowboard.services.video_analyzer import source_profile_repair as repair
from flowboard.services.video_analyzer import source_inventory as inv
from tests.test_source_entity_cleanup import _fixture


def test_canonical_presence_correction_does_not_start_global_profile_repair():
    inventory = {'assets': [{'id': 'prop', 'name': 'Broom'}, {'id': 'person', 'name': 'Figure'}]}
    findings = [{'shot': 7, 'message': 'Use the canonical prop Broom instead of Figure in shot presence.'}]
    assert repair._targets(inventory, findings) == set()
    assert repair._targets(inventory, [{'asset_id':'prop', 'message':'Canonical description is wrong.'}]) == {'prop'}


@pytest.mark.parametrize("verdict", ["approve", "reject", "foreign_evidence", "different_text"])
def test_stable_description_requires_independent_exact_evidence_check(tmp_path, monkeypatch, verdict):
    data, evidence = _fixture(tmp_path)
    data["assets"][0].update(kind="prop", description="Blue knife. Remains in the block.")
    original = copy.deepcopy(data)
    findings = [{"shot": 1, "asset_id": "casual", "message": "Canonical description conflicts with knife removal."}]
    calls = []

    async def ask(system, payload, supplied, *args, **kwargs):
        calls.append(system)
        if system == repair.EDIT:
            return {"repairs": [{"asset_id": "casual", "description": "Blue knife.",
                                 "evidence_ids": ["e1"], "reason": "Removal is a transient state."}]}
        assert system == repair.CHECK
        return {"checks": [{"asset_id": "casual", "description": "Other text" if verdict == "different_text" else "Blue knife.",
                            "approved": verdict != "reject", "reason": "The blue blade is visible.",
                            "evidence_ids": ["e2" if verdict == "foreign_evidence" else "e1"]}]}

    monkeypatch.setattr(inv, "_ask", ask)
    result, audit = asyncio.run(repair.repair_profiles(inv, data, evidence, findings, {}, tmp_path,
                                                      asyncio.Semaphore(4), lambda: None))
    assert data == original
    assert result["shots"] == data["shots"]
    assert result["assets"][0]["id"] == "casual"
    assert len(calls) == (4 if verdict == 'reject' else 2)
    assert bool(audit["changes"]) is (verdict == "approve")
    assert result["assets"][0]["description"] == ("Blue knife." if verdict == "approve" else "Blue knife. Remains in the block.")


@pytest.mark.parametrize("extra", [{"name": "New name"}, {"id": "new"}, {"kind": "character"}])
def test_profile_repairs_cannot_rename_or_reidentify(extra):
    row = {"asset_id": "knife", "description": "Steel blade", "reason": "Visible", "evidence_ids": ["e1"], **extra}
    with pytest.raises(ValueError):
        repair._valid_proposals({"repairs": [row]}, {"knife": {"asset": {}, "anchor_evidence_ids": ["e1"]}})


def test_shot_action_issue_does_not_trigger_global_profile_rewrites():
    assert repair._targets({"assets": [{"id": "person", "name": "Person"}]},
                           [{"shot": 1, "message": "Wrong visible hand"}]) == set()


def test_profile_payload_scopes_and_groups_findings_without_losing_questions():
    inventory = {'assets': [{'id': 'lead', 'name': 'Lead'}, {'id': 'car', 'name': 'Car'}]}
    findings = [
        {'code': 'registry_conflict', 'asset_id': 'lead', 'shot': n,
         'message': 'Canonical description wrongly fixes clothing.', 'irrelevant_log': 'x' * 10000}
        for n in range(1, 100)
    ] + [{'code': 'source_mismatch', 'asset_id': 'car', 'shot': 7, 'message': 'Wrong color.'},
         {'code': 'registry_conflict', 'asset_id': 'lead', 'shot': 2, 'message': 'Different profile question.'}]
    original = copy.deepcopy(findings)
    result = repair._scoped_findings(inventory, findings, ['lead'])
    assert len(result) == 2
    assert result[0]['shot_ids'] == list(range(1, 100))
    assert result[1]['shot_ids'] == [2]
    assert all('irrelevant_log' not in r and r['asset_id'] == 'lead' for r in result)
    assert findings == original
