"""The fast source path preserves journalled facts while overlapping observation."""
import asyncio
import copy
import json

from flowboard.services import avis_text
from flowboard.services.video_analyzer import source_inventory as inv
from tests.test_source_inventory import _source, _draft, _verdict


def _reply(model, data):
    return avis_text.Completion(text=json.dumps(data), model=model, prompt_tokens=12, completion_tokens=8)


def _enable(monkeypatch, limit=16):
    monkeypatch.setattr(inv, 'SOURCE_OBSERVATION_CONCURRENCY', limit)
    monkeypatch.setattr(inv, 'SOURCE_CONCURRENCY', limit)


def test_fast_observations_overlap_and_resume_without_repaying(tmp_path, monkeypatch):
    video, work, shots = _source(tmp_path, count=90)
    _enable(monkeypatch)
    active = peak = 0
    calls = []
    payloads = []

    async def complete(model, messages, **kwargs):
        nonlocal active, peak
        payload = json.loads(messages[1]['content'][0]['text'])
        batch = payload['source_shots']
        verify = messages[0]['content'] == inv._VERIFY
        calls.append((verify, batch[0]['shot']))
        if not verify: payloads.append(copy.deepcopy(payload))
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(.01)
            value = _verdict(batch) if verify else _draft(batch)
            return _reply(model, value)
        finally:
            active -= 1

    monkeypatch.setattr(avis_text, 'complete', complete)
    original, report = asyncio.run(inv.analyze(video, work, shots, fps=30))
    assert 5 <= peak <= 16
    assert report['status'] == 'verified'
    assert report['execution']['mode'] == 'parallel-observation-v1'
    assert report['reviewed_shots'] == list(range(1, 91))
    assert list(original['shots']) == [str(n) for n in range(1, 91)]
    assert all(not p['known_inventory']['shots'] for p in payloads)
    count = len(calls)
    restored, rerun = asyncio.run(inv.analyze(video, work, shots, fps=30))
    assert restored == original
    assert rerun['status'] == report['status']
    assert len(calls) == count
    assert active == 0


def test_fast_adopts_compatible_serial_cache_and_retains_source_findings(tmp_path, monkeypatch):
    video, work, shots = _source(tmp_path, count=42)
    _enable(monkeypatch, 1)
    calls = []

    async def complete(model, messages, **kwargs):
        payload = json.loads(messages[1]['content'][0]['text'])
        batch = payload['source_shots']
        verify = messages[0]['content'] == inv._VERIFY
        calls.append((verify, batch[0]['shot']))
        value = _verdict(batch) if verify else _draft(batch)
        if verify:
            value['checks'][0]['status'] = 'needs_review'
            value['checks'][0]['source_description_findings'] = ['The visible camera framing contradicts the source.']
        return _reply(model, value)

    monkeypatch.setattr(avis_text, 'complete', complete)
    original, report = asyncio.run(inv.analyze(video, work, shots, fps=30))
    # Emulate a serial run stopped after two complete batches. Same film and
    # fingerprint, with later paid work absent from the on-disk checkpoint.
    path = work / 'source_inventory.v1.json'
    cache = json.loads(path.read_text())
    cache['batches'] = {k:v for k,v in cache['batches'].items() if k in {'1-6','7-12'}}
    cache.pop('result', None)
    path.write_text(json.dumps(cache))
    calls.clear()
    _enable(monkeypatch)
    inventory, result = asyncio.run(inv.analyze(video, work, shots, fps=30))
    assert not any(n in (1,7) for _, n in calls)
    assert result['status'] == 'needs_review'
    assert {1,7} <= set(result['unresolved_shots'])
    assert inventory['shots']['1'] == original['shots']['1']
    count = len(calls)
    _, restored = asyncio.run(inv.analyze(video, work, shots, fps=30))
    assert len(calls) == count
    assert restored['findings'] == result['findings']


def test_fast_identity_findings_cannot_be_erased_by_clean_local_verifier(tmp_path, monkeypatch):
    from flowboard.services.video_analyzer import source_identity
    video, work, shots = _source(tmp_path, count=18)
    _enable(monkeypatch)

    async def complete(model, messages, **kwargs):
        batch = json.loads(messages[1]['content'][0]['text'])['source_shots']
        return _reply(model, _verdict(batch) if messages[0]['content'] == inv._VERIFY else _draft(batch))

    async def reconcile(inv, items, known, evidence, journal, work_dir, semaphore, save):
        result = [copy.deepcopy(item['observation']) for item in items]
        result[0]['persistent_issues'] = [{'code':'identity_reconciliation', 'message':'Unresolved candidate identity',
                                         'asset_id': 'market-prop'}]
        return result

    monkeypatch.setattr(avis_text, 'complete', complete)
    monkeypatch.setattr(source_identity, 'reconcile_wave', reconcile)
    _, report = asyncio.run(inv.analyze(video, work, shots, fps=30))
    assert report['status'] == 'needs_review'
    assert 7 in report['unresolved_shots']
    assert any(f['code'] == 'identity_reconciliation' for f in report['findings'])


def test_fast_cancellation_awaits_all_tasks_and_reuses_saved_observations(tmp_path, monkeypatch):
    video, work, shots = _source(tmp_path, count=54)
    _enable(monkeypatch)
    calls = []
    active = 0
    block = True
    reached = None

    async def complete(model, messages, **kwargs):
        nonlocal active
        batch = json.loads(messages[1]['content'][0]['text'])['source_shots']
        verify = messages[0]['content'] == inv._VERIFY
        calls.append((verify, batch[0]['shot']))
        active += 1
        try:
            if verify and block:
                reached.set()
                await asyncio.Event().wait()
            await asyncio.sleep(.001)
            return _reply(model, _verdict(batch) if verify else _draft(batch))
        finally: active -= 1

    monkeypatch.setattr(avis_text, 'complete', complete)
    async def interrupt():
        nonlocal reached
        reached = asyncio.Event()
        task = asyncio.create_task(inv.analyze(video, work, shots, fps=30))
        await asyncio.wait_for(reached.wait(), 2)
        await asyncio.sleep(.05)
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
        assert active == 0
    asyncio.run(interrupt())
    paid = [n for verify,n in calls if not verify]
    assert len(paid) > 1
    block = False
    _, report = asyncio.run(inv.analyze(video, work, shots, fps=30))
    assert report['status'] == 'verified'
    assert all(calls.count((False,n)) == 1 for n in paid)


def test_fast_reconciles_new_prop_across_batches_and_reuses_mapping_on_resume(tmp_path, monkeypatch):
    from flowboard.services.video_analyzer import source_identity
    video, work, shots = _source(tmp_path, count=18)
    _enable(monkeypatch)
    calls = []

    async def complete(model, messages, **kwargs):
        payload = json.loads(messages[1]['content'][0]['text'])
        system = messages[0]['content']
        calls.append(system)
        if system == source_identity.SYSTEM:
            candidates = payload['candidates']
            first = candidates[0]
            return _reply(model, {'mappings': [
                {'candidate_id': c['candidate_id'],
                 'decision': 'new' if i == 0 else 'match',
                 'target_id': None if i == 0 else first['candidate_id'],
                 'candidate_evidence_ids': c['candidate_evidence_ids'][:1],
                 'target_evidence_ids': [] if i == 0 else first['candidate_evidence_ids'][:1],
                 'reason': 'Matching source object'}
                for i,c in enumerate(candidates)]})
        batch = payload['source_shots']
        if system == inv._VERIFY: return _reply(model, _verdict(batch))
        draft = _draft(batch)
        if batch[0]['shot'] > 6:
            prop = copy.deepcopy(next(a for a in draft['assets'] if a['id'] == 'market-prop'))
            prop.update(id='new-gift', name='Gift box', description='A distinct white gift box')
            draft['assets'].append(prop)
            for row in draft['shots'].values():
                presence = copy.deepcopy(next(p for p in row['asset_presence'] if p['asset_id'] == 'market-prop'))
                presence['asset_id'] = 'new-gift'
                row['asset_presence'].append(presence)
            draft['scenes'][0]['present_asset_ids'].append('new-gift')
        return _reply(model, draft)

    monkeypatch.setattr(avis_text, 'complete', complete)
    inventory, report = asyncio.run(inv.analyze(video, work, shots, fps=30))
    assert report['status'] == 'verified'
    gifts = [a for a in inventory['assets'] if a['name'] == 'Gift box']
    assert len(gifts) == 1
    gift_id = gifts[0]['id']
    for n in range(7,19):
        assert sum(p['asset_id'] == gift_id for p in inventory['shots'][str(n)]['asset_presence']) == 1
    assert calls.count(source_identity.SYSTEM) == 1
    count = len(calls)
    restored, again = asyncio.run(inv.analyze(video, work, shots, fps=30))
    assert restored == inventory
    assert again['status'] == 'verified'
    assert len(calls) == count
