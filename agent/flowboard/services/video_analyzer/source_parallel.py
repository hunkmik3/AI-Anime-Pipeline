"""Parallel visual observation, ordered identity reconciliation, independent QA.

Raw extraction contexts are immutable and journalled. Local IDs from independently
observed batches acquire canonical meaning only through source_identity. Existing
sequential checkpoints remain reusable; speculative work never rewrites them.
"""
from __future__ import annotations

import asyncio
import copy
import json
import time
from pathlib import Path

WAVE_BATCHES = 6


def _register_with_provenance(inv, catalog, observation, batch_key, batch, origins):
    """Record publication in source order, independently of dispatch snapshots."""
    patch = observation['inventory']
    previous = {asset['id'] for asset in catalog['assets']}
    inv._register_observation(catalog, patch)
    published = {asset['id'] for asset in catalog['assets']} - previous
    for asset_id in sorted(published):
        occurrences = [int(number) for number, row in patch['shots'].items()
                       if any(p['asset_id'] == asset_id for p in row['asset_presence'])]
        origins.setdefault(asset_id, {'batch_key': batch_key, 'first_shot': batch[0]['shot'],
                                      'origin_shots': occurrences or [s['shot'] for s in batch]})
    return {'newly_published_asset_ids': sorted(published),
            'asset_registration_origins': {
                asset['id']: copy.deepcopy(origins[asset['id']])
                for asset in patch['assets'] if asset['id'] in origins}}


async def analyze(video: Path, work_dir: Path, shots: list[dict], sequences=None,
                  *, fps=0, deep=False, on_progress=None, preobserved=None):
    from . import source_inventory as inv, source_identity as identity

    started = time.monotonic()
    sequences = sequences or []
    work_dir.mkdir(parents=True, exist_ok=True)
    digest = await asyncio.to_thread(inv._fingerprint, video, work_dir, shots, sequences, fps, deep)
    cache_path = work_dir / 'source_inventory.v1.json'
    if preobserved is not None:
        digest = inv._digest({'source': digest, 'joint': preobserved})
        cache_path = work_dir / 'source_inventory.joint.v1.json'
    cache = {'cache_version': inv.CACHE_VERSION, 'digest': digest, 'batches': {},
             'budget': {'remaining': inv.MAX_EXTRA_FRAMES}}
    try:
        previous = json.loads(cache_path.read_text())
        if previous.get('cache_version') == inv.CACHE_VERSION and previous.get('digest') == digest:
            cache = previous
        elif previous.get('cache_version') != inv.CACHE_VERSION:
            legacy = work_dir / 'source_inventory.v1.legacy.json'
            if not legacy.exists(): inv._checkpoint(legacy, previous)
    except (OSError, ValueError, AttributeError):
        pass
    concurrency = min(64, max(1, inv.SOURCE_CONCURRENCY))
    observe_concurrency = min(concurrency, max(1, inv.SOURCE_OBSERVATION_CONCURRENCY))
    limiter = inv._CallLimiter(concurrency)
    observation_slots = asyncio.Semaphore(observe_concurrency)
    def save():
        cache['execution'] = {'max_observed_model_calls': limiter.peak,
                              'active_model_calls': limiter.active,
                              'max_concurrent_model_calls': concurrency,
                              'observation_concurrency': observe_concurrency,
                              'elapsed_s': round(time.monotonic() - started, 3)}
        inv._checkpoint(cache_path, cache)
    # A recovery run must not advertise a previous aggregate as its current result.
    cache.pop('result', None)
    budget = cache['budget']
    all_evidence = inv._initial_evidence(work_dir, shots, fps, deep)
    for item in all_evidence:
        item['sha256'] = inv._hash_file(work_dir / item['frame'])
    producer_evidence = list(all_evidence)
    catalog = inv._empty()
    registration_origins = {}
    usage_counts = {}
    batches = [shots[i:i + inv.BATCH_SIZE] for i in range(0, len(shots), inv.BATCH_SIZE)]
    keys = [f"{batch[0]['shot']}-{batch[-1]['shot']}" for batch in batches]
    workers = set()
    verification_tasks = {}
    snapshots = {}
    observed = set()
    processed = set()

    def supplied_for(index, known, evidence):
        batch = batches[index]
        wanted = {s['shot'] for s in batch}
        prior = shots[max(0, index * inv.BATCH_SIZE - 2):index * inv.BATCH_SIZE]
        anchors = set(inv._anchor_ids(known, usage_counts))
        cards = [e for e in evidence if e['shot'] in wanted or e['id'] in anchors]
        for row in prior:
            candidates = [e for e in evidence if e['shot'] == row['shot'] and e['sampling'] == 'machine_keyframe']
            cards.extend(candidates[:1] + candidates[-1:])
        return list({e['id']: e for e in cards}.values()), prior

    def payload_for(index, known, prior):
        batch = batches[index]
        return {'source_shots': inv._source_rows(batch), 'known_inventory': copy.deepcopy(known),
                'neighbor_source_shots': inv._source_rows(prior),
                'sequence_hints': [s for s in sequences if s.get('first_shot', 0) <= batch[-1]['shot']
                                   and s.get('last_shot', 0) >= batch[0]['shot']],
                'remaining_frame_budget': budget['remaining']}

    def entry_for(index, known, supplied, mode):
        key = keys[index]
        entry = cache['batches'].get(key)
        catalog_digest = inv._digest(known)
        initial_digest = inv._digest(supplied)
        if (not isinstance(entry, dict) or entry.get('catalog_digest') != catalog_digest or
                entry.get('initial_evidence_digest') not in (None, initial_digest) or
                not inv._entry_evidence_valid(entry, work_dir)):
            entry = {'catalog_digest': catalog_digest, 'trace': [], 'usage': {}, 'calls': {}, 'frame_calls': {}}
            cache['batches'][key] = entry
        entry.update(initial_evidence_digest=initial_digest, execution_mode=mode)
        return entry

    def register(index, observation, entry):
        provenance = _register_with_provenance(inv, catalog, observation, keys[index],
                                               batches[index], registration_origins)
        entry['registration_provenance'] = copy.deepcopy(provenance)
        cache['asset_registration_origins'] = copy.deepcopy(registration_origins)
        for row in observation['inventory']['shots'].values():
            for p in row['asset_presence']:
                usage_counts[p['asset_id']] = usage_counts.get(p['asset_id'], 0) + 1
        return provenance

    async def verify(index, observation, payload, frozen, entry):
        result = await inv._verify_batch(entry, observation, batches[index], frozen, payload,
                                         video, work_dir, fps, budget, limiter, save)
        # Generic inventory repair is not allowed to erase unresolved identity
        # reconciliation or grant identity approval by returning a clean verdict.
        retained = []
        definitions = {a['id']: a for a in frozen['assets']}
        for issue in observation.get('persistent_issues') or []:
            if issue.get('shot') is not None:
                retained.append(copy.deepcopy(issue))
                continue
            affected = []
            for number, row in observation['inventory']['shots'].items():
                used = {p['asset_id'] for p in row['asset_presence']}
                used.update(p.get('holder_id') for p in row['asset_presence'] if p.get('holder_id'))
                used.update(x for p in row['asset_presence'] for x in p.get('contains_ids') or [])
                while True:
                    expanded = used | {x for key in used for field in ('member_ids', 'depends_on_asset_ids')
                                       for x in definitions.get(key, {}).get(field) or []}
                    if expanded == used: break
                    used = expanded
                if issue.get('asset_id') in used: affected.append(int(number))
            # An unsupported standalone profile is still part of this batch's
            # output catalog; it must not escape review just because it is orphaned.
            if not affected: affected = [s['shot'] for s in batches[index]]
            retained.extend({**copy.deepcopy(issue), 'shot': n} for n in affected)
        if retained:
            result['findings'] = list({inv._digest(f): f for f in result['findings'] + retained}.values())
            result['status'] = 'needs_review'
        if observation.get('identity_retryable'):
            result['retryable'] = True
        entry['result'] = copy.deepcopy(result)
        processed.update(s['shot'] for s in batches[index])
        save()
        if on_progress: on_progress('source_verify', len(processed), len(shots))
        return result

    def launch_verification(index, observation, payload, frozen, entry):
        key = keys[index]
        snapshots[key] = frozen
        task = asyncio.create_task(verify(index, observation, payload, frozen, entry))
        verification_tasks[key] = task
        workers.add(task)

    try:
        parallel = cache.get('parallel')
        limit = parallel.get('start_index', 0) if isinstance(parallel, dict) else len(batches)
        prefix = 0
        # Reconstruct the exact paid serial prefix, including interrupted stages.
        # For a new film, one source batch establishes the first identity anchors.
        while prefix < limit:
            key = keys[prefix]
            if key not in cache['batches'] and prefix:
                break
            known = inv._catalog_view(catalog)
            supplied, prior = supplied_for(prefix, known, producer_evidence)
            entry = entry_for(prefix, known, supplied, 'serial-prefix')
            payload = payload_for(prefix, known, prior)
            observation = await inv._observe_batch(entry, batches[prefix], known, payload, supplied,
                         video, work_dir, fps, budget, limiter, save,
                         **({'preobserved': preobserved[key]} if preobserved is not None else {}))
            provenance = register(prefix, observation, entry)
            producer_evidence = list({e['id']: e for e in producer_evidence + entry['supplied']}.values())
            observed.update(s['shot'] for s in batches[prefix])
            launch_verification(prefix, observation, {**payload, **provenance}, inv._catalog_view(catalog), entry)
            prefix += 1
            if on_progress: on_progress('inventory', len(observed), len(shots))
        seed = inv._catalog_view(catalog)
        policy = inv._digest({'version': identity.VERSION, 'prompt': identity.SYSTEM, 'wave': WAVE_BATCHES})
        seed_digest = inv._digest(seed)
        if not isinstance(parallel, dict) or parallel.get('seed_digest') != seed_digest or parallel.get('policy') != policy:
            parallel = {'start_index': prefix, 'seed_digest': seed_digest, 'policy': policy, 'waves': {}}
            cache['parallel'] = parallel
        elif prefix != parallel['start_index']:
            raise ValueError('Parallel source checkpoint prefix does not match its identity seed')
        save()
        # Freeze the dispatch evidence and usage counts too. Subsequent identity
        # registrations cannot retroactively alter what a speculative call saw.
        dispatch = {}
        for index in range(prefix, len(batches)):
            supplied, prior = supplied_for(index, seed, producer_evidence)
            entry = entry_for(index, seed, supplied, 'parallel-observation-v1')
            payload = payload_for(index, seed, prior)
            dispatch[index] = (entry, payload, supplied)

        async def observe(index):
            entry, payload, supplied = dispatch[index]
            async with observation_slots:
                observation = await inv._observe_batch(entry, batches[index], seed, payload, supplied,
                             video, work_dir, fps, budget, limiter, save,
                             **({'preobserved': preobserved[keys[index]]} if preobserved is not None else {}))
            observed.update(s['shot'] for s in batches[index])
            if on_progress: on_progress('inventory', len(observed), len(shots))
            return observation

        observations = {}
        for index in range(prefix, len(batches)):
            task = asyncio.create_task(observe(index))
            observations[index] = task
            workers.add(task)
        for start in range(prefix, len(batches), WAVE_BATCHES):
            indices = list(range(start, min(start + WAVE_BATCHES, len(batches))))
            raw = await asyncio.gather(*(observations[i] for i in indices))
            items = [{'key': keys[i], 'batch': batches[i], 'observation': obs,
                      'known_inventory': seed} for i, obs in zip(indices, raw)]
            known = inv._catalog_view(catalog)
            wave_key = f'{keys[indices[0]]}:{keys[indices[-1]]}'
            evidence = list({e['id']: e for e in all_evidence + producer_evidence +
                            [e for i in indices for e in dispatch[i][0].get('supplied', [])]}.values())
            wave_digest = inv._digest({'known': known, 'items': items, 'policy': policy})
            wave = parallel['waves'].get(wave_key)
            if not isinstance(wave, dict) or wave.get('digest') != wave_digest:
                wave = {'digest': wave_digest, 'trace': [], 'usage': {}, 'calls': {}, 'frame_calls': {}}
                parallel['waves'][wave_key] = wave
            canonical = await identity.reconcile_wave(inv, items, known, evidence, wave,
                                                       work_dir, limiter, save)
            if len(canonical) != len(indices): raise ValueError('Identity reconciliation lost a source batch')
            provenance = {index: register(index, obs, dispatch[index][0])
                          for index, obs in zip(indices, canonical)}
            frozen = inv._catalog_view(catalog)
            # Every canonical target used by QA must have actual supplied source
            # evidence, including identities newly matched across observations.
            anchor_ids = {ref for asset in frozen['assets'] for ref in (asset.get('evidence_ids') or [])[:1]}
            anchor_cards = [e for e in evidence if e['id'] in anchor_ids]
            for index, observation in zip(indices, canonical):
                entry, payload, _ = dispatch[index]
                qa_evidence = list({e['id']: e for e in entry['supplied'] + anchor_cards}.values())
                context_digest = inv._digest({'inventory': observation, 'catalog': frozen, 'evidence': qa_evidence})
                if entry.get('verification_context_digest') not in (None, context_digest):
                    for stage in ('verify_initial', 'repair', 'verify_final'):
                        entry['calls'].pop(stage, None)
                    entry.pop('result', None)
                entry['verification_context_digest'] = context_digest
                entry['canonical_observation'] = copy.deepcopy(observation)
                entry['supplied'] = qa_evidence
                # Host publication provenance is deliberately outside the paid
                # QA context digest: upgrading this bookkeeping reuses evidence
                # and verdicts, then recomputes only the host retraction check.
                payload = {**payload, 'verification_known_inventory': frozen, **provenance[index]}
                # _verify_batch normalizes the repair against frozen. The exact
                # dispatch catalog remains provenance in payload and observation.
                launch_verification(index, observation, payload, frozen, entry)
            producer_evidence = list({e['id']: e for e in producer_evidence + evidence}.values())
            save()
        await asyncio.gather(*verification_tasks.values())
    except BaseException:
        for task in workers:
            if not task.done(): task.cancel()
        if workers: await asyncio.gather(*workers, return_exceptions=True)
        cache.pop('result', None)
        save()
        raise

    inventory = inv._empty()
    findings, trace, reviewed, changes = [], [], [], []
    evidence = list(all_evidence)
    for key, batch in zip(keys, batches):
        result = verification_tasks[key].result()
        inv._merge(inventory, {**result['inventory'], 'assets': []})
        reviewed.extend(result['reviewed_shots'])
        findings.extend(inv._route_findings(result['findings'], {s['shot'] for s in batch}, result['inventory']))
        evidence.extend(result['evidence'])
        changes.extend(result['identity_changes'])
        trace.extend(copy.deepcopy(cache['batches'][key]['trace']))
        trace.append({'stage': 'source_verify', 'shots': [s['shot'] for s in batch],
                      'status': 'cached' if result.get('cached') else result['status']})
    inventory['assets'] = copy.deepcopy(catalog['assets'])
    by_id = {a['id']: a for a in inventory['assets']}
    changes = list({inv._digest(c): c for c in changes}.values())
    for change in list(changes):
        if change['code'] == 'late_identity':
            if change['asset_id'] not in by_id:
                inventory['assets'].append(copy.deepcopy(change['proposed']))
                by_id[change['asset_id']] = change['proposed']
            elif inv._profile(by_id[change['asset_id']]) != inv._profile(change['proposed']):
                changes.append({**change, 'code': 'registry_conflict', 'current': by_id[change['asset_id']]})
    from .source_identity_review import reconcile_late
    late_journals = cache.setdefault('late_identity_review', {})
    try:
        inventory, changes, findings, late_audit = await reconcile_late(
            inv, inventory, changes, findings, shots, evidence, late_journals,
            work_dir, limiter, save)
    except BaseException:
        cache.pop('result', None)
        save()
        raise
    findings.extend(inv._route_identity_changes(changes, inventory, batches, snapshots))
    findings = list({inv._digest(f): f for f in findings}.values())
    wanted = {s['shot'] for s in shots}
    unresolved = sorted((wanted - set(reviewed)) | {f['shot'] for f in findings if f.get('shot') in wanted})
    usage = {}
    journals = (list(cache['batches'].values()) + list(cache['parallel']['waves'].values()) +
                [v for v in late_journals.values() if isinstance(v, dict) and 'usage' in v])
    for entry in journals:
        for model, counts in entry.get('usage', {}).items():
            total = usage.setdefault(model, {'calls': 0, 'prompt_tokens': 0, 'completion_tokens': 0})
            for field in total: total[field] += counts.get(field, 0)
    report = {'status': 'verified' if not unresolved else 'needs_review' if reviewed else 'unverified',
              'method': 'source_frames', **inv._scope_report(), 'reviewed_shots': sorted(set(reviewed)),
              'unresolved_shots': unresolved, 'findings': findings,
              'evidence': list({e['id']: e for e in evidence}.values()), 'trace': trace,
              'digest': digest, 'usage': usage, 'registry_conflicts': changes,
              'late_identity_review': late_audit,
              'execution': {**cache['execution'], 'mode': 'parallel-observation-v1'},
              'inventory_digest': inv.inventory_digest(inventory),
              'shot_digests': {n: inv._digest(row) for n, row in inventory['shots'].items()},
              'asset_digests': {a['id']: inv._digest(a) for a in inventory['assets']},
              'limits': {'extra_frames': inv.MAX_EXTRA_FRAMES, 'extra_frames_used': inv.MAX_EXTRA_FRAMES - budget['remaining'],
                         'max_review_requests_per_round': inv.MAX_REQUESTS_PER_BATCH,
                         'max_model_calls_per_batch': 5, 'identity_calls_per_wave': 1,
                         'max_concurrent_model_calls': concurrency, 'observation_concurrency': observe_concurrency}}
    cache['result'] = {'scene_inventory': inventory, 'source_verification': report}
    report['input_binding'] = inv.verification_binding(video, shots, report['evidence'])
    save()
    return inventory, report
