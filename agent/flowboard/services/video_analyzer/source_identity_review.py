"""Resolve identities discovered by repair against the completed source catalog.

An independent visual identity decision can reconcile a late definition without
pretending that every later shot had a visual error. Ambiguous decisions retain
all original context gates. Original source-description findings are untouched.
"""
from __future__ import annotations

import copy


async def reconcile_late(inv, inventory, changes, findings, source_shots, evidence,
                         journals, work_dir, semaphore, save):
    from . import source_identity as identity

    late = {}
    for change in sorted(changes, key=lambda c: c.get('first_shot', 0)):
        if change['code'] == 'late_identity': late.setdefault(change['asset_id'], change)
    if not late: return inventory, changes, findings, []
    assets = {a['id']: a for a in inventory['assets']}
    source_by_number = {s['shot']: s for s in source_shots}
    evidence_by_id = {e['id']: e for e in evidence}
    known = copy.deepcopy(inventory)
    known['assets'] = [a for a in known['assets'] if a['id'] not in late]
    aliases, resolved, audit, retained = {}, set(), [], []
    late_ids = list(late)
    for start in range(0, len(late_ids), 6):
        keys = late_ids[start:start + 6]
        items = []
        for key in keys:
            asset = assets.get(key) or late[key]['proposed']
            numbers = {int(n) for n, row in inventory['shots'].items()
                       if any(p.get('asset_id') == key or p.get('holder_id') == key or key in (p.get('contains_ids') or [])
                              for p in row.get('asset_presence') or [])}
            numbers.update(evidence_by_id[r]['shot'] for r in asset.get('evidence_ids') or [] if r in evidence_by_id)
            numbers.add(late[key]['first_shot'])
            batch = [source_by_number[n] for n in sorted(numbers) if n in source_by_number]
            observation = {'inventory': {'schema_version': inv.SCHEMA_VERSION, 'assets': [copy.deepcopy(asset)],
                           'scenes': [], 'shots': {str(n): copy.deepcopy(inventory['shots'][str(n)])
                                                  for n in numbers if str(n) in inventory['shots']}},
                           'issues': [], 'identity_changes': []}
            items.append({'key': 'late-' + key, 'batch': batch, 'observation': observation,
                          'known_inventory': copy.deepcopy(known)})
        digest = inv._digest({'items': items, 'known': known, 'version': 1})
        name = f'{start}-{start + len(keys)}'
        journal = journals.get(name)
        if not isinstance(journal, dict) or journal.get('digest') != digest:
            journal = {'digest': digest, 'trace': [], 'usage': {}, 'calls': {}, 'frame_calls': {}}
            journals[name] = journal
        results = await identity.reconcile_wave(inv, items, known, evidence, journal,
                                                work_dir, semaphore, save)
        raw_aliases = {key: result.get('identity_aliases', {}).get(key, key) for key, result in zip(keys, results)}
        # New host-scoped candidates retain their original repair-assigned ID in
        # the final catalog. Matching existing identities uses their canonical ID.
        original_by_scoped = {identity._namespace(item['key'], key, 'asset'): key
                              for key, item in zip(keys, items)}
        for key, result in zip(keys, results):
            problems = result.get('persistent_issues') or []
            target = raw_aliases[key]
            target = original_by_scoped.get(target, target)
            if problems or result.get('identity_retryable'):
                aliases[key] = key
                for issue in problems:
                    issue = copy.deepcopy(issue)
                    issue['asset_id'] = original_by_scoped.get(issue.get('asset_id'), issue.get('asset_id'))
                    if issue.get('shot') is None: issue['shot'] = late[key]['first_shot']
                    retained.append(issue)
                audit.append({'asset_id': key, 'status': 'needs_review', 'proposed_target': target})
                continue
            aliases[key] = target
            resolved.add(key)
            audit.append({'asset_id': key, 'status': 'reconciled', 'canonical_id': target})
        # Later groups can compare with resolved new identities. Unresolved
        # profiles never become trusted matching targets for subsequent groups.
        for key in keys:
            if key in resolved and aliases[key] == key and key in assets:
                known['assets'].append(copy.deepcopy(assets[key]))
    for key in aliases:
        target, seen = aliases[key], {key}
        while target in aliases and aliases[target] != target and target not in seen:
            seen.add(target); target = aliases[target]
        aliases[key] = target

    def reject_unresolved_targets():
        # Never let coverage treat an unapproved transitive alias as a recorded
        # appearance of another identity.
        for key in list(resolved):
            if aliases[key] in late and aliases[key] not in resolved:
                resolved.remove(key); aliases[key] = key

    def remap_canonical():
        remapped = identity._remap_inventory(inventory, aliases, {})
        canonical = {a['id']: identity._asset_refs(a, aliases) for a in inventory['assets']
                     if aliases.get(a['id'], a['id']) == a['id']}
        for asset in inventory['assets']:
            target = aliases.get(asset['id'], asset['id'])
            if target in canonical:
                canonical[target]['evidence_ids'] = list(dict.fromkeys(canonical[target].get('evidence_ids', []) + asset.get('evidence_ids', [])))
        remapped['assets'] = list(canonical.values())
        return remapped

    reject_unresolved_targets()
    # Matching a new identity is not proof that later frames did not omit it.
    # Resolve that separate coverage question before lifting stale-context gates.
    coverage_candidates = [{'asset_id': key, 'first_shot': late[key]['first_shot']}
                           for key in late if key not in resolved or aliases[key] == key]
    covered = set()
    if coverage_candidates:
        from .source_identity_coverage import audit_coverage
        coverage_findings, covered = await audit_coverage(
            inv, coverage_candidates, remap_canonical(), source_shots, evidence,
            journals.setdefault('coverage', {}), work_dir, semaphore, save)
        retained.extend(coverage_findings)
        for key in list(resolved):
            if aliases[key] == key and key not in covered:
                resolved.remove(key)
                for record in audit:
                    if record['asset_id'] == key: record['status'] = 'coverage_pending'
    # Coverage may have left a formerly resolved new identity pending.
    reject_unresolved_targets()
    remapped = remap_canonical()
    remaining_changes = [copy.deepcopy(c) for c in changes if not (c['code'] == 'late_identity' and c['asset_id'] in resolved)]
    for change in remaining_changes:
        if change['code'] == 'late_identity' and change['asset_id'] in covered:
            # Identity itself remains unresolved at its actual appearances.
            # Later coverage now has explicit per-shot verdicts/findings, so it
            # must not also receive a blanket unknown-context finding.
            change['coverage_checked'] = True
    for record in audit:
        if record['asset_id'] in covered:
            record['coverage_status'] = 'checked'
            record['coverage_findings'] = sum(f.get('asset_id') == record['asset_id'] for f in retained
                                              if f.get('code', '').startswith('identity_coverage_'))
    remaining_findings = [f for f in findings if not (f.get('code') == 'late_identity' and f.get('asset_id') in resolved)]
    remaining_changes = identity._remap_metadata(remaining_changes, aliases)
    remaining_findings = identity._remap_metadata(remaining_findings + retained, aliases)
    journals['audit'] = audit
    journals['aliases'] = aliases
    save()
    return remapped, remaining_changes, remaining_findings, audit
