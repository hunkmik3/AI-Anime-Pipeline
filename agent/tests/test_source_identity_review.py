import asyncio
import copy
import json

from flowboard.services import avis_text
from flowboard.services.video_analyzer import source_inventory as inv
from flowboard.services.video_analyzer.source_identity_review import reconcile_late
from tests.test_source_inventory import _source, _draft


def _case(tmp_path, coexist=False, count=6):
    video, work, shots = _source(tmp_path, count=count)
    inventory = _draft(shots)
    inventory['schema_version'] = inv.SCHEMA_VERSION
    lead = inventory['assets'][0]
    lead['evidence_ids'] = ['shot-3-frame-1']
    late = copy.deepcopy(lead)
    late.update(id='late-person', evidence_ids=['shot-1-frame-1'])
    inventory['assets'].append(late)
    presence = copy.deepcopy(inventory['shots']['1']['asset_presence'][0])
    presence['asset_id'] = 'late-person'
    if not coexist: inventory['shots']['1']['asset_presence'].pop(0)
    inventory['shots']['1']['asset_presence'].append(presence)
    changes = [{'code':'late_identity', 'asset_id':'late-person', 'first_shot':1,
                'current':None, 'proposed':copy.deepcopy(late)}]
    findings = [inv._identity_finding(changes[0]),
                {'code':'source_description_mismatch','message':'Actual visible source contradiction','shot':2}]
    evidence = inv._initial_evidence(work, shots, 30, False)
    return work, shots, inventory, changes, findings, evidence


def _matcher(monkeypatch, decision='match'):
    calls = []
    async def complete(model, messages, **kwargs):
        payload = json.loads(messages[1]['content'][0]['text'])
        calls.append(payload)
        if 'expected_checks' in payload:
            return avis_text.Completion(text=json.dumps({'checks':[
                {**pair, 'status':'absent', 'evidence_ids':[f"shot-{pair['shot']}-frame-1"]}
                for pair in payload['expected_checks']]}),model=model)
        rows = []
        target = next(t for t in payload['known_identities'] if t['asset']['id'] == 'market-lead')
        for candidate in payload['candidates']:
            rows.append({'candidate_id':candidate['candidate_id'], 'decision':decision,
                         'target_id':'market-lead' if decision == 'match' else None,
                         'candidate_evidence_ids':candidate['candidate_evidence_ids'][:1],
                         'target_evidence_ids':target['anchor_evidence_ids'][:1] if decision == 'match' else [],
                         'reason':'Source visual identity comparison'})
        return avis_text.Completion(text=json.dumps({'mappings':rows}),model=model)
    monkeypatch.setattr(avis_text,'complete',complete)
    return calls


def _run(case, journal):
    work, shots, inventory, changes, findings, evidence = case
    return asyncio.run(reconcile_late(inv, inventory, changes, findings, shots, evidence,
                                      journal, work, inv._CallLimiter(4), lambda:None))


def test_late_identity_visually_reconciles_without_erasing_source_findings(tmp_path, monkeypatch):
    case = _case(tmp_path); original = copy.deepcopy(case[2:5]); calls = _matcher(monkeypatch)
    journal = {}
    inventory, changes, findings, audit = _run(case, journal)
    assert not changes
    assert audit == [{'asset_id':'late-person','status':'reconciled','canonical_id':'market-lead'}]
    assert not any(a['id'] == 'late-person' for a in inventory['assets'])
    assert inventory['shots']['1']['asset_presence'][-1]['asset_id'] == 'market-lead'
    assert any(f['code'] == 'source_description_mismatch' and f['shot'] == 2 for f in findings)
    assert tuple(case[2:5]) == original
    again = _run(case, journal)
    assert again == (inventory, changes, findings, audit)
    assert len(calls) == 1


def test_uncertain_late_identity_retains_original_gates(tmp_path, monkeypatch):
    case = _case(tmp_path); _matcher(monkeypatch, 'uncertain')
    inventory, changes, findings, audit = _run(case,{})
    assert changes[0]['code'] == 'late_identity'
    assert changes[0]['coverage_checked'] is True
    assert inventory['shots']['1']['asset_presence'][-1]['asset_id'] == 'late-person'
    assert audit[0]['status'] == 'needs_review'
    assert any(f['code']=='late_identity' for f in findings)
    assert any(f['code']=='identity_unresolved' for f in findings)


def test_distinct_late_identity_preserves_original_host_id(tmp_path, monkeypatch):
    from flowboard.services.video_analyzer import source_identity_coverage
    async def coverage(inv, candidates, inventory, shots, evidence, journal, work_dir, semaphore, save):
        assert candidates == [{'asset_id':'late-person','first_shot':1}]
        return [{'code':'identity_coverage_omitted','message':'Visible later omission','shot':4,'asset_id':'late-person'}], {'late-person'}
    monkeypatch.setattr(source_identity_coverage,'audit_coverage',coverage)
    case = _case(tmp_path); _matcher(monkeypatch,'new')
    inventory, changes, findings, audit = _run(case,{})
    assert not changes
    assert any(a['id']=='late-person' for a in inventory['assets'])
    assert audit[0]['canonical_id']=='late-person'
    assert not any(f['code']=='late_identity' for f in findings)
    assert any(f['code']=='identity_coverage_omitted' and f['shot']==4 for f in findings)


def test_cooccurring_people_cannot_be_cleared_as_the_same_identity(tmp_path, monkeypatch):
    case = _case(tmp_path,coexist=True); _matcher(monkeypatch)
    inventory, changes, findings, audit = _run(case,{})
    assert changes
    assert audit[0]['status']=='needs_review'
    ids={p['asset_id'] for p in inventory['shots']['1']['asset_presence']}
    assert {'late-person','market-lead'} <= ids


def test_new_identity_without_later_coverage_keeps_stale_context_gate(tmp_path, monkeypatch):
    from flowboard.services.video_analyzer import source_identity_coverage
    async def coverage(*args, **kwargs): return [], set()
    monkeypatch.setattr(source_identity_coverage, 'audit_coverage', coverage)
    case = _case(tmp_path); _matcher(monkeypatch, 'new')
    inventory, changes, findings, audit = _run(case,{})
    assert changes == case[3]
    assert audit[0]['status'] == 'coverage_pending'
    assert any(f['code'] == 'late_identity' for f in findings)


def test_checked_coverage_scopes_uncertain_identity_to_actual_appearances(tmp_path, monkeypatch):
    case = _case(tmp_path, count=12); _matcher(monkeypatch,'uncertain')
    inventory, changes, findings, audit = _run(case,{})
    known = inv._catalog_view(case[2])
    known['assets'] = [a for a in known['assets'] if a['id'] != 'late-person']
    snapshots = {'1-6': known, '7-12': known}
    batches = [case[1][:6], case[1][6:]]
    routed = inv._route_identity_changes(changes, inventory, batches, snapshots)
    assert any(f['code']=='late_identity' and f['shot']==1 for f in routed)
    assert not any(f['code']=='registry_context_stale' for f in routed)
    assert audit[0]['coverage_status']=='checked'


def test_coverage_recognizes_accepted_late_alias_without_overwriting_canonical_profile(tmp_path, monkeypatch):
    case = _case(tmp_path)
    source = case[2]
    canonical = next(a for a in source['assets'] if a['id'] == 'late-person')
    canonical['name'] = 'Canonical original profile'
    case[3][0]['proposed'] = copy.deepcopy(canonical)
    alias = copy.deepcopy(canonical)
    alias.update(id='late-alias', name='Alternate wording must not replace canonical',
                 evidence_ids=['shot-4-frame-1'])
    source['assets'].append(alias)
    source['shots']['4']['asset_presence'].append({
        'asset_id':'late-alias', 'visibility':'visible',
        'evidence_ids':['shot-4-frame-1'], 'contains_ids':[]})
    case[3].append({'code':'late_identity', 'asset_id':'late-alias', 'first_shot':4,
                    'current':None, 'proposed':copy.deepcopy(alias)})
    original = copy.deepcopy(case[2:5])
    coverage_calls = []

    async def complete(model, messages, **kwargs):
        payload = json.loads(messages[1]['content'][0]['text'])
        if 'expected_checks' in payload:
            coverage_calls.append(payload)
            assert payload['candidates'][0]['asset']['id'] == 'late-person'
            assert payload['candidates'][0]['asset']['name'] == 'Canonical original profile'
            assert not any(pair['shot'] == 4 for pair in payload['expected_checks'])
            return avis_text.Completion(text=json.dumps({'checks':[
                {**pair, 'status':'absent', 'evidence_ids':[f"shot-{pair['shot']}-frame-1"]}
                for pair in payload['expected_checks']]}), model=model)
        first, second = payload['candidates']
        return avis_text.Completion(text=json.dumps({'mappings':[
            {'candidate_id':first['candidate_id'], 'decision':'new', 'target_id':None,
             'candidate_evidence_ids':first['candidate_evidence_ids'][:1], 'target_evidence_ids':[]},
            {'candidate_id':second['candidate_id'], 'decision':'match', 'target_id':first['candidate_id'],
             'candidate_evidence_ids':second['candidate_evidence_ids'][:1],
             'target_evidence_ids':first['candidate_evidence_ids'][:1]}]}), model=model)

    monkeypatch.setattr(avis_text, 'complete', complete)
    inventory, changes, findings, audit = _run(case, {})
    assert coverage_calls and not changes
    assert not any(f['code'].startswith('identity_coverage_') for f in findings)
    assert any(p['asset_id'] == 'late-person' for p in inventory['shots']['4']['asset_presence'])
    assert not any(a['id'] == 'late-alias' for a in inventory['assets'])
    assert next(a for a in inventory['assets'] if a['id'] == 'late-person')['name'] == 'Canonical original profile'
    assert next(a for a in audit if a['asset_id'] == 'late-alias')['canonical_id'] == 'late-person'
    assert tuple(case[2:5]) == original
