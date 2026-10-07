"""Speculative identity reconciliation: mocked model, real host validation/cache."""

import asyncio
import copy

import pytest

from flowboard.services.video_analyzer import source_identity as identity
from flowboard.services.video_analyzer import source_inventory as inv


def _asset(key, evidence, kind="character", **extra):
    return {"id": key, "name": key, "description": "visible distinguishing features", "kind": kind,
            "reference_required": True, "evidence_ids": [evidence], "member_ids": [],
            "depends_on_asset_ids": [], **extra}


def _evidence(tmp_path, *numbers):
    result = []
    for n in numbers:
        path = tmp_path / f"frame-{n}.jpg"
        path.write_bytes(f"frame {n}".encode())
        result.append({"id": f"e{n}", "shot": n, "frame": path.name, "sha256": "stale supplied hash"})
    return result


def _item(number, assets, seed=None):
    patch = {"assets": copy.deepcopy(assets),
             "scenes": [{"id": "room", "shot_ids": [number], "present_asset_ids": [a["id"] for a in assets]}],
             "shots": {str(number): {"scene_id": "room", "evidence_ids": [f"e{number}"],
                                     "asset_presence": [{"asset_id": a["id"], "visibility": "visible",
                                                         "evidence_ids": [f"e{number}"], "contains_ids": []}
                                                        for a in assets]}}}
    return {"key": f"{number}-{number}", "batch": [{"shot": number}],
            "known_inventory": copy.deepcopy(seed if seed is not None else inv._empty()),
            "observation": {"inventory": patch, "issues": [], "identity_changes": []}}


def _new(candidate):
    return {"candidate_id": candidate["candidate_id"], "decision": "new", "target_id": None,
            "candidate_evidence_ids": candidate["candidate_evidence_ids"], "target_evidence_ids": []}


def _match(candidate, target, refs):
    return {**_new(candidate), "decision": "match", "target_id": target, "target_evidence_ids": refs}


def _install(monkeypatch, response=None):
    calls = []

    async def ask(system, payload, supplied, work_dir, usage, **kwargs):
        assert system in (identity.SYSTEM, identity.COMPLETE_SYSTEM)
        calls.append(copy.deepcopy(payload))
        return response(payload, len(calls)) if response else {"mappings": [_new(c) for c in payload["candidates"]]}

    monkeypatch.setattr(inv, "_ask", ask)
    return calls


def _run(items, known, evidence, tmp_path, journal=None):
    return asyncio.run(identity.reconcile_wave(inv, items, known, evidence,
                                               journal if journal is not None else {}, tmp_path,
                                               asyncio.Semaphore(4), lambda: None))


def test_new_ids_use_each_dispatch_seed_and_scenes_are_namespaced(tmp_path, monkeypatch):
    evidence = _evidence(tmp_path, 1, 2, 99)
    calls = _install(monkeypatch)
    known = {**inv._empty(), "assets": [_asset("person", "e99")]}
    items = [_item(1, [_asset("person", "e1")]), _item(2, [_asset("person", "e2")])]
    original = copy.deepcopy(items)
    result = _run(items, known, evidence, tmp_path)
    ids = [row["identity_aliases"]["person"] for row in result]
    assert len(set(ids)) == 2 and "person" not in ids
    assert result[0]["inventory"]["scenes"][0]["id"] != result[1]["inventory"]["scenes"][0]["id"]
    assert all(not row["persistent_issues"] for row in result)
    assert len(calls) == 1
    assert items == original


def test_identity_model_is_separate_and_model_change_invalidates_saved_mapping(tmp_path, monkeypatch):
    evidence = _evidence(tmp_path, 1)
    items = [_item(1, [_asset("person", "e1")])]
    journal, used = {}, []

    async def ask(system, payload, supplied, work_dir, usage, **kwargs):
        used.append(kwargs['model_override'])
        return {"mappings": [_new(c) for c in payload['candidates']]}

    monkeypatch.setattr(inv, '_ask', ask)
    monkeypatch.setattr(inv, 'MODEL', 'extract-only')
    monkeypatch.setattr(inv, 'VERIFY_MODEL', 'review-only')
    monkeypatch.setattr(identity, 'MODEL', 'gpt-identity-a')
    _run(items, inv._empty(), evidence, tmp_path, journal)
    _run(items, inv._empty(), evidence, tmp_path, journal)
    assert used == ['gpt-identity-a']
    monkeypatch.setattr(identity, 'MODEL', 'gpt-identity-b')
    _run(items, inv._empty(), evidence, tmp_path, journal)
    assert used == ['gpt-identity-a', 'gpt-identity-b']
    assert journal['context_history']


def test_large_identity_payload_keeps_all_same_kind_targets_and_order(tmp_path, monkeypatch):
    candidates = [{'candidate_id': f'c{i}', 'asset': {'id': f'c{i}', 'kind': 'character' if i % 2 else 'prop'},
                   'candidate_evidence_ids': [f'e{i}']} for i in range(20)]
    known = [{'asset': {'id': k, 'kind': k}, 'anchor_evidence_ids': [k]}
             for k in ('character', 'prop', 'environment')]
    supplied = [{'id': f'e{i}'} for i in range(20)] + [{'id': k} for k in ('character', 'prop', 'environment')]
    calls = []

    async def stage(journal, name, system, payload, evidence, *args, **kwargs):
        calls.append(copy.deepcopy(payload))
        own = set(payload['required_candidate_ids'])
        same_kind = payload['candidates'][0]['asset']['kind']
        assert all(c['asset']['kind'] == same_kind for c in payload['candidates'])
        assert payload['known_identities'] == [k for k in known if k['asset']['kind'] == same_kind]
        assert len(own) <= 6 and kwargs['model_override'] == identity.MODEL
        assert system == identity.COMPLETE_SYSTEM
        return {'mappings': [_new(c) for c in payload['candidates'] if c['candidate_id'] in own]}

    monkeypatch.setattr(inv, '_stage_call', stage)
    result = asyncio.run(identity._partitioned_mappings(inv, candidates, {'known_identities': known}, supplied,
                        {}, tmp_path, asyncio.Semaphore(1), lambda: None))
    assert len(calls) == 4
    assert [r['candidate_id'] for r in result['mappings']] == [c['candidate_id'] for c in candidates]
    assert len(calls[1]['existing_mappings']) == 6


def test_empty_provider_response_stops_identity_wave(tmp_path, monkeypatch):
    from flowboard.services.avis_text import AvisEmptyResponse
    evidence = _evidence(tmp_path, 1)
    calls = []
    async def ask(*args, **kwargs):
        calls.append(1)
        raise AvisEmptyResponse('empty provider response')
    monkeypatch.setattr(inv, '_ask', ask)
    with pytest.raises(AvisEmptyResponse):
        _run([_item(1, [_asset('person', 'e1')])], inv._empty(), evidence, tmp_path)
    assert len(calls) == 1


def test_partition_saved_empty_failure_stays_blocking_without_paid_retry(tmp_path, monkeypatch):
    candidates = [{'candidate_id': f'c{i}', 'asset': {'id': f'c{i}', 'kind': 'character'},
                   'candidate_evidence_ids': [f'e{i}']} for i in range(8)]
    payload = {'known_identities': [], 'candidates': candidates}
    supplied = [{'id': f'e{i}'} for i in range(49)]
    journal = {'context_digest': 'scope', 'calls': {'identity_partition_character_0':
               {'model': identity.MODEL, 'error': 'AvisEmptyResponse: no text'}}}
    called = []
    async def stage(journal, name, system, part, *args, **kwargs):
        called.append(name)
        ids = set(part['required_candidate_ids'])
        return {'mappings': [_new(c) for c in part['candidates'] if c['candidate_id'] in ids]}
    monkeypatch.setattr(inv, '_stage_call', stage)
    rows, problems, retryable = asyncio.run(identity._mappings(inv, candidates, payload, supplied,
                                          journal, tmp_path, asyncio.Semaphore(1), lambda: None))
    assert called == ['identity_partition_character_6']
    assert set(rows) == {'c6', 'c7'}
    assert set(problems) == {f'c{i}' for i in range(6)}
    assert all('not retried' in problem for problem in problems.values())
    assert not retryable


def test_independent_identity_kinds_run_concurrently(tmp_path, monkeypatch):
    async def run():
        entered, released = set(), asyncio.Event()
        candidates = [{'candidate_id': k, 'asset': {'id': k, 'kind': k},
                       'candidate_evidence_ids': [k]} for k in ('character', 'prop', 'environment')]
        async def stage(journal, name, system, part, *args, **kwargs):
            entered.add(part['required_candidate_ids'][0])
            if len(entered) == 3:
                released.set()
            await released.wait()
            return {'mappings': [_new(c) for c in part['candidates']]}
        monkeypatch.setattr(inv, '_stage_call', stage)
        result = await asyncio.wait_for(identity._partitioned_mappings(inv, candidates,
            {'known_identities': []}, [{'id': k} for k in ('character', 'prop', 'environment')],
            {}, tmp_path, asyncio.Semaphore(3), lambda: None), 2)
        assert len(result['mappings']) == 3 and len(entered) == 3
    asyncio.run(run())


def test_visual_match_remaps_all_relationships_and_copies_canonical_profile(tmp_path, monkeypatch):
    evidence = _evidence(tmp_path, 1, 99)
    canonical = _asset("lead", "e99", name="Established lead")
    known = {**inv._empty(), "assets": [canonical]}
    assets = [_asset("local-person", "e1"),
              _asset("group", "e1", "background_group", member_ids=["local-person"]),
              _asset("watch", "e1", "prop", depends_on_asset_ids=["local-person"])]
    item = _item(1, assets)
    item["observation"]["inventory"]["shots"]["1"]["asset_presence"][2].update(
        holder_id="local-person", contains_ids=["local-person"])

    def response(payload, _):
        return {"mappings": [_match(c, "lead", ["e99"]) if c["asset"]["id"].startswith("wave-asset-local-person-") else _new(c)
                             for c in payload["candidates"]]}

    _install(monkeypatch, response)
    result = _run([item], known, evidence, tmp_path)[0]
    patch = result["inventory"]
    by_id = {a["id"]: a for a in patch["assets"]}
    assert result["identity_aliases"]["local-person"] == "lead"
    assert by_id["lead"]["name"] == "Established lead"
    assert set(by_id["lead"]["evidence_ids"]) == {"e1", "e99"}
    assert by_id[result["identity_aliases"]["group"]]["member_ids"] == ["lead"]
    assert by_id[result["identity_aliases"]["watch"]]["depends_on_asset_ids"] == ["lead"]
    presence = patch["shots"]["1"]["asset_presence"][2]
    assert presence["holder_id"] == "lead" and presence["contains_ids"] == ["lead"]
    assert "lead" in patch["scenes"][0]["present_asset_ids"]
    assert not result["persistent_issues"]


def test_distinct_cooccurring_people_cannot_collapse_onto_known_identity(tmp_path, monkeypatch):
    evidence = _evidence(tmp_path, 1, 99)
    known = {**inv._empty(), "assets": [_asset("lead", "e99")]}
    item = _item(1, [_asset("lead", "e99"), _asset("other", "e1")], seed=known)
    _install(monkeypatch, lambda payload, _: {"mappings": [_match(payload["candidates"][0], "lead", ["e99"])]})
    result = _run([item], known, evidence, tmp_path)[0]
    assert result["identity_aliases"]["other"] != "lead"
    assert len({p["asset_id"] for p in result["inventory"]["shots"]["1"]["asset_presence"]}) == 2
    assert any("co-occur" in f["message"] for f in result["persistent_issues"])
    assert not result["identity_retryable"]


def test_earlier_candidate_match_is_ordered_and_requires_its_own_anchor(tmp_path, monkeypatch):
    evidence = _evidence(tmp_path, 1, 2)
    items = [_item(1, [_asset("person", "e1")]), _item(2, [_asset("person", "e2")])]

    def response(payload, _):
        first, second = payload["candidates"]
        return {"mappings": [_new(first), _match(second, first["candidate_id"], ["e1"])]}

    _install(monkeypatch, response)
    result = _run(items, inv._empty(), evidence, tmp_path)
    assert result[0]["identity_aliases"]["person"] == result[1]["identity_aliases"]["person"]
    assert all(not r["persistent_issues"] for r in result)
    assert result[1]["inventory"]["assets"][0]["id"] == result[0]["inventory"]["assets"][0]["id"]


@pytest.mark.parametrize("failure", ["kind", "target_evidence", "candidate_evidence", "unknown_target", "uncertain"])
def test_unsafe_identity_decisions_stay_distinct_and_block(tmp_path, monkeypatch, failure):
    evidence = _evidence(tmp_path, 1, 99)
    known = {**inv._empty(), "assets": [_asset("lead", "e99", "prop" if failure == "kind" else "character")]}

    def response(payload, _):
        row = _match(payload["candidates"][0], "lead", ["e99"])
        if failure == "target_evidence":
            row["target_evidence_ids"] = ["e1"]
        elif failure == "candidate_evidence":
            row["candidate_evidence_ids"] = ["e99"]
        elif failure == "unknown_target":
            row["target_id"] = "invented"
        elif failure == "uncertain":
            row["decision"] = "uncertain"
        return {"mappings": [row]}

    _install(monkeypatch, response)
    result = _run([_item(1, [_asset("person", "e1")])], known, evidence, tmp_path)[0]
    assert result["identity_aliases"]["person"] != "lead"
    assert result["persistent_issues"]
    assert not result["identity_retryable"]


def test_known_profile_change_is_retained_without_an_extra_rpc(tmp_path, monkeypatch):
    evidence = _evidence(tmp_path, 1)
    known = {**inv._empty(), "assets": [_asset("lead", "e1", name="Original")]}
    item = _item(1, [_asset("lead", "e1", name="Changed")], seed=known)
    calls = _install(monkeypatch)
    result = _run([item], known, evidence, tmp_path)[0]
    assert calls == []
    assert result["inventory"]["assets"][0]["name"] == "Original"
    assert any(f["code"] == "registry_conflict" for f in result["persistent_issues"])


def test_matched_profile_cannot_silently_drop_dependencies(tmp_path, monkeypatch):
    evidence = _evidence(tmp_path, 1, 99)
    known = {**inv._empty(), "assets": [_asset("canonical-watch", "e99", "prop")]}
    item = _item(1, [_asset("watch", "e1", "prop", depends_on_asset_ids=["person"]),
                     _asset("person", "e1")])

    def response(payload, _):
        return {"mappings": [_match(c, "canonical-watch", ["e99"]) if c["asset"]["kind"] == "prop" else _new(c)
                             for c in payload["candidates"]]}

    _install(monkeypatch, response)
    result = _run([item], known, evidence, tmp_path)[0]
    assert any(f["code"] == "registry_conflict" and f["asset_id"] == "canonical-watch" for f in result["persistent_issues"])
    change = next(c for c in result["identity_changes"] if c.get("reason") == "reconciled_relationship_conflict")
    assert change["proposed"]["depends_on_asset_ids"] == [result["identity_aliases"]["person"]]
    assert change["current"]["depends_on_asset_ids"] == []


def test_schema_failure_preserves_reply_and_retries_only_reconciliation(tmp_path, monkeypatch):
    evidence = _evidence(tmp_path, 1)
    items = [_item(1, [_asset("person", "e1")])]
    calls = _install(monkeypatch, lambda payload, n: {"not_mappings": []} if n == 1 else
                     {"mappings": [_new(c) for c in payload["candidates"]]})
    journal = {}
    result = _run(items, inv._empty(), evidence, tmp_path, journal)[0]
    assert result["identity_retryable"] and result["persistent_issues"]
    assert journal["calls"]["identity_reconcile"]["history"][0]["output"] == {"not_mappings": []}
    result = _run(items, inv._empty(), evidence, tmp_path, journal)[0]
    assert not result["identity_retryable"] and not result["persistent_issues"]
    assert len(calls) == 2


def test_partial_response_completes_only_missing_rows_without_rewriting_good_rows(tmp_path, monkeypatch):
    evidence = _evidence(tmp_path, 1, 2)
    items = [_item(1, [_asset("first", "e1")]), _item(2, [_asset("second", "e2")])]

    def response(payload, n):
        first, second = payload["candidates"]
        if n == 1:
            return {"mappings": [_new(first)]}
        assert payload["required_candidate_ids"] == [second["candidate_id"]]
        assert payload["existing_mappings"] == [_new(first)]
        # The completion must not turn an accepted earlier new row uncertain.
        return {"mappings": [{**_new(first), "decision": "uncertain"},
                             _match(second, first["candidate_id"], ["e1"])]}

    calls = _install(monkeypatch, response)
    journal = {}
    result = _run(items, inv._empty(), evidence, tmp_path, journal)
    assert len(calls) == 2 and all(not r["persistent_issues"] for r in result)
    assert result[0]["identity_aliases"]["first"] == result[1]["identity_aliases"]["second"]
    assert journal["completion"]["ignored_rows"]
    _run(items, inv._empty(), evidence, tmp_path, journal)
    assert len(calls) == 2


@pytest.mark.parametrize("stored_form", ["output", "raw_response"])
def test_legacy_omission_history_is_recovered_without_repeating_original_call(tmp_path, monkeypatch, stored_form):
    evidence = _evidence(tmp_path, 1, 2)
    items = [_item(1, [_asset("first", "e1")]), _item(2, [_asset("second", "e2")])]
    first_id = identity._namespace("1-1", "first", "asset")
    second_id = identity._namespace("2-2", "second", "asset")
    first = {"candidate_id": first_id, "decision": "new", "target_id": None,
             "candidate_evidence_ids": ["e1"], "target_evidence_ids": []}
    saved = {"mappings": [first]}
    if stored_form == "output":
        previous = {"output": saved}
    else:
        import json
        previous = {"responses": [{"text": json.dumps(saved)}]}
    journal = {"calls": {"identity_reconcile": {"error": "old exact count failure", "history": [previous]}}}

    def response(payload, _):
        assert payload["required_candidate_ids"] == [second_id]
        return {"mappings": [_match(payload["candidates"][1], first_id, ["e1"])]}

    calls = _install(monkeypatch, response)
    result = _run(items, inv._empty(), evidence, tmp_path, journal)
    assert len(calls) == 1 and all(not r["persistent_issues"] for r in result)
    assert journal["recovered_saved_mapping_response"]
    assert journal["calls"]["identity_reconcile"]["history"][0] == previous


@pytest.mark.parametrize("bad_row", ["duplicate", "malformed"])
def test_bad_row_only_blocks_its_candidate_and_is_not_rewritten_by_completion(tmp_path, monkeypatch, bad_row):
    evidence = _evidence(tmp_path, 1, 2)
    items = [_item(1, [_asset("first", "e1")]), _item(2, [_asset("second", "e2")])]

    def response(payload, _):
        first, second = payload["candidates"]
        rows = [_new(first), _new(second)]
        if bad_row == "duplicate":
            rows.append(_new(second))
        else:
            rows[1]["target_evidence_ids"] = "invalid"
        return {"mappings": rows}

    calls = _install(monkeypatch, response)
    result = _run(items, inv._empty(), evidence, tmp_path)
    assert len(calls) == 1
    assert not result[0]["persistent_issues"] and result[1]["persistent_issues"]
    assert all(not r["identity_retryable"] for r in result)


def test_original_uncertainty_and_its_dependencies_remain_blocking(tmp_path, monkeypatch):
    evidence = _evidence(tmp_path, 1, 2)
    items = [_item(1, [_asset("first", "e1")]), _item(2, [_asset("second", "e2")])]

    def response(payload, n):
        first, second = payload["candidates"]
        return {"mappings": [{**_new(first), "decision": "uncertain"}]} if n == 1 else {
            "mappings": [_match(second, first["candidate_id"], ["e1"])]}

    _install(monkeypatch, response)
    result = _run(items, inv._empty(), evidence, tmp_path)
    assert all(r["persistent_issues"] for r in result)
    assert any("unresolved earlier" in p["message"] for p in result[1]["persistent_issues"])


def test_completion_failure_only_retries_missing_owner_and_preserves_saved_rows(tmp_path, monkeypatch):
    evidence = _evidence(tmp_path, 1, 2)
    items = [_item(1, [_asset("first", "e1")]), _item(2, [_asset("second", "e2")])]

    def response(payload, n):
        if n == 1:
            return {"mappings": [_new(payload["candidates"][0])]}
        if n == 2:
            raise RuntimeError("provider unavailable")
        return {"mappings": [_new(payload["candidates"][1])]}

    calls = _install(monkeypatch, response)
    journal = {}
    result = _run(items, inv._empty(), evidence, tmp_path, journal)
    assert not result[0]["identity_retryable"] and not result[0]["persistent_issues"]
    assert result[1]["identity_retryable"] and result[1]["persistent_issues"]
    result = _run(items, inv._empty(), evidence, tmp_path, journal)
    assert len(calls) == 3 and all(not r["persistent_issues"] for r in result)
    assert "required_candidate_ids" not in calls[0]
    assert all("required_candidate_ids" in p for p in calls[1:])


def test_partial_completion_preserves_its_rows_and_only_retries_still_missing(tmp_path, monkeypatch):
    evidence = _evidence(tmp_path, 1, 2, 3)
    items = [_item(n, [_asset(f"person-{n}", f"e{n}")]) for n in (1, 2, 3)]

    def response(payload, n):
        candidates = payload["candidates"]
        if n == 1:
            return {"mappings": [_new(candidates[0])]}
        if n == 2:
            assert len(payload["required_candidate_ids"]) == 2
            return {"mappings": [_new(candidates[1])]}
        assert payload["required_candidate_ids"] == [candidates[2]["candidate_id"]]
        return {"mappings": [_new(candidates[2])]}

    calls = _install(monkeypatch, response)
    journal = {}
    result = _run(items, inv._empty(), evidence, tmp_path, journal)
    assert [r["identity_retryable"] for r in result] == [False, False, True]
    result = _run(items, inv._empty(), evidence, tmp_path, journal)
    assert len(calls) == 3 and all(not r["persistent_issues"] for r in result)


def test_cached_matching_is_bound_to_actual_selected_frame_bytes(tmp_path, monkeypatch):
    evidence = _evidence(tmp_path, 1)
    items = [_item(1, [_asset("person", "e1")])]
    calls, journal = _install(monkeypatch), {}
    _run(items, inv._empty(), evidence, tmp_path, journal)
    digest = journal["context_digest"]
    _run(items, inv._empty(), evidence, tmp_path, journal)
    assert len(calls) == 1
    (tmp_path / "frame-1.jpg").write_bytes(b"replacement image")
    _run(items, inv._empty(), evidence, tmp_path, journal)
    assert len(calls) == 2 and journal["context_digest"] != digest
    assert journal["context_history"]
