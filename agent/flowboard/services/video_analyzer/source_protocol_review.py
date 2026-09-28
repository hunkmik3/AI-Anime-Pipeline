"""One fresh visual QA pass for protocol-blocked shots; never rewrite source facts."""
from __future__ import annotations

import asyncio
import copy
import json
import time
from pathlib import Path

PROTOCOL_CODES = {"invalid_resolution", "resolution_incomplete", "source_protocol_review_failed"}
BATCH_SIZE = 6
INSTRUCTION = """This is fresh QA of the current source facts, not permission to accept an old report.
Return exactly one disposition for each CURRENT TOP-LEVEL finding_id in prior_findings.
Historical questions and prior resolution reasons are context, not additional tasks.
Use only allowed_decisions. Each check and disposition requires own-shot evidence.
Temporal neighbors and canonical anchors may establish identity continuity, but
never current presence, hand side, contact or unseen contents. Keep those separate
in context_links; do not put foreign evidence into an own-shot check or resolution.
Inspect all current visual
facts and retain every real uncertainty or contradiction. No source edits are possible
in this pass. If more frames are necessary, request them and leave that fact unresolved.
"""


def model_ledger(ledger):
    """Expose one addressable ID per finding while retaining its historical facts."""
    def clean(value):
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items()
                    if key not in {"finding_id", "prior_finding_id"}}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return copy.deepcopy(value)
    return [{**clean(row), "finding_id": row["finding_id"]} for row in ledger]


def _report_binding(inv, report):
    return inv._digest({key: report.get(key) for key in
                       ("status", "method", "findings", "reviewed_shots", "unresolved_shots",
                        "inventory_digest", "shot_digests", "asset_digests", "retryable", "issue_summary")})


def _protocol_findings(report):
    """Raw protocol failures may now be represented by a canonical root issue.

    Only audit rows tied to a currently active ID can schedule QA. Resolved
    history by itself never reopens a shot.
    """
    active = report.get("findings") or []
    findings = [f for f in active if f.get("code") in PROTOCOL_CODES]
    current_ids = {f["finding_id"] for f in active if isinstance(f.get("finding_id"), str)}
    audit = report.get("issue_audit") or {}
    rows = list(audit.get("final") or [])
    if audit.get("protocol"):
        rows += audit["protocol"][-1].get("final") or []
    for row in rows:
        original = row.get("active_finding") or {}
        if row.get("finding_id") in current_ids and original.get("code") in PROTOCOL_CODES:
            findings.append(original)
    return findings


async def review(video, work_dir, analysis, *, on_progress=None, selected_shots=None):
    from . import source_inventory as inv, source_refinement as refine
    from . import source_issue_ledger as issue_mod, source_temporal_context as temporal
    from . import source_refinement_support as support
    from .source_refinement_support import prepare_frame_requests

    video, work_dir = Path(video), Path(work_dir)
    started = time.monotonic()
    result = copy.deepcopy(analysis)
    shots = result.get("shots") or []
    inventory = result.get("scene_inventory") or {}
    prior = result.get("source_verification") or {}
    if selected_shots is not None and (not isinstance(selected_shots,list) or not selected_shots or
            any(type(n) is not int or n not in {s['shot'] for s in shots} for n in selected_shots) or
            len(set(selected_shots)) != len(selected_shots)):
        raise ValueError('Selected shots must be a nonempty list of current source shot numbers')
    protocol_findings = _protocol_findings(prior)
    if not protocol_findings and selected_shots is None:
        return result
    previous = prior.get("protocol_review") or {}
    refinement = prior.get("refinement") or {}
    if not shots or not inventory.get("shots"):
        raise ValueError("Current source shots and inventory are required for protocol QA")
    evidence = list({e["id"]: e for e in inv._initial_evidence(
        work_dir, shots, float(result.get("video", {}).get("fps") or 0), bool(result.get("deep")))
        + prior.get("evidence", [])}.values())
    evidence = [{**e, "sha256": inv._hash_file(path)} for e in evidence
                if (path := inv._safe_path(work_dir, e["frame"]))]
    source_binding = inv._digest({"video": inv._hash_file(video) if video.is_file() else None,
        "models": [inv.MODEL, inv.VERIFY_MODEL],
        "evidence": sorted((e["id"], e["sha256"], e.get("timestamp_s"), e.get("shot")) for e in evidence)})
    if (prior.get("method") != "source_frames" or refinement.get("source_binding") != source_binding
            or refinement.get("source_shots_digest") != inv._digest(shots)
            or prior.get("inventory_digest") != inv.inventory_digest(inventory)):
        raise ValueError("Protocol QA requires unchanged, source-bound refinement facts and evidence")
    policy = inv._digest({"module": inv._hash_file(Path(__file__)),
        "refinement": inv._hash_file(Path(refine.__file__)), "instruction": INSTRUCTION,
        "temporal_context": inv._hash_file(Path(temporal.__file__)),
        "issue_ledger": inv._hash_file(Path(issue_mod.__file__)),
        "support": inv._hash_file(Path(support.__file__)),
        "check_system": refine.CHECK_SYSTEM, "models": [inv.MODEL, inv.VERIFY_MODEL]})
    facts = inv._digest({"source_binding": source_binding, "shots": shots, "inventory": inventory})
    if (previous.get("policy") == policy and previous.get("facts_digest") == facts
            and previous.get("output_report_digest") == _report_binding(inv, prior)
            and (selected_shots is None or sorted(set(selected_shots)) == previous.get('selected_shots'))
            and not previous.get("retryable")):
        return result
    numbers = {s["shot"] for s in shots}
    affected = {f["shot"] for f in protocol_findings if f.get("shot") in numbers}
    if any(f.get("shot") is None for f in protocol_findings):
        affected = numbers
    if selected_shots is not None:
        affected = set(selected_shots)
    if not affected:
        return result
    questions = {r.get("finding", {}).get("finding_id"): r.get("finding", {})
                 for r in refinement.get("resolutions", []) + previous.get("resolutions", [])}
    findings_in = copy.deepcopy(prior.get("findings", []))
    for finding in findings_in:
        question = questions.get(finding.get("prior_finding_id"))
        if question and not finding.get("prior_question"):
            finding["prior_question"] = copy.deepcopy(question)
    input_ledger = issue_mod.build_issue_ledger({**result, "source_verification": {
        **prior, "findings": findings_in}})
    findings_in = input_ledger["issues"]
    selected = [s for s in shots if s["shot"] in affected]
    digest = inv._digest({"policy": policy, "facts": facts, "findings": findings_in,
                         "selected": sorted(affected)})
    path = work_dir / "source_protocol_review.v1.json"
    journal = {"digest": digest, "batches": {}, "prior_report": copy.deepcopy(prior),
               "input_issue_ledger": input_ledger}
    try:
        saved = json.loads(path.read_text())
        if saved.get("digest") == digest:
            journal = saved
        else:
            archived = work_dir / ("source_protocol_review." + str(saved.get("digest", "old"))[:16] + ".json")
            if not archived.exists(): inv._checkpoint(archived, saved)
    except (OSError, ValueError, AttributeError):
        pass
    concurrency = min(64, max(1, inv.SOURCE_CONCURRENCY))
    limiter = inv._CallLimiter(concurrency)
    def save():
        journal["execution"] = {"active_model_calls": limiter.active,
            "max_observed_model_calls": limiter.peak, "max_concurrent_model_calls": concurrency,
            "elapsed_s": round(time.monotonic() - started, 3)}
        inv._checkpoint(path, journal)
    if journal.get("result") and not journal["result"]["source_verification"]["protocol_review"].get("retryable"):
        return copy.deepcopy(journal["result"])
    journal.pop("result", None)
    processed = set()
    graphics = {a["id"] for a in inventory.get("screen_graphics", [])}
    if on_progress: on_progress("source_protocol_review", 0, len(selected))

    async def check_batch(batch):
        wanted = {s["shot"] for s in batch}
        ledger = refine._active_ledger(findings_in, batch, graphics)
        extras = {f["asset_id"] for f in ledger if f.get("asset_id")}
        extras.update(key for f in ledger for key in f.get("asset_ids", []) if isinstance(key, str))
        context, supplied = temporal.build_temporal_context(
            inv, batch, shots, inventory, evidence, extra_asset_ids=extras)
        patch = context["proposed_inventory"]
        _, structural = inv._normalise({**patch, "assets": []}, batch, inventory, supplied)
        required = set()
        for entry in refinement.get("context_links", []):
            if not entry.get("stage", "").startswith("checker_"):
                continue
            for item in entry.get("links", []):
                if not item.get("valid") or not isinstance(item.get("link"), dict):
                    continue
                link = item.get("link", {})
                number, asset = link.get("shot"), link.get("asset_id")
                if item.get("valid") and number in wanted and any(
                        p.get("asset_id") == asset and p.get("visibility") in {"visible", "partial"}
                        for p in inventory["shots"].get(str(number), {}).get("asset_presence", [])):
                    required.add((number, asset))
        payload = {k: v for k, v in context.items() if k != "source_shots"}
        payload.update({"proposed_source_shots": context["source_shots"],
            "identity_catalog": patch["assets"], "other_asset_index": context["other_asset_index"],
            "prior_findings": model_ledger(ledger), "structural_findings": structural,
            "screen_graphics": inventory.get("screen_graphics", []), "proposed_text_updates": {},
            "relationship_checks": refine._relationship_checks(patch),
            "required_context_reviews": [{"shot": n, "asset_id": asset} for n, asset in sorted(required)]})
        key = f"{batch[0]['shot']}-{batch[-1]['shot']}"
        entry = journal["batches"].setdefault(key, {"calls": {}, "usage": {}, "trace": []})
        if entry.get("result") and not entry["result"].get("retryable"):
            checked = copy.deepcopy(entry["result"])
        else:
            try:
                reply = await inv._stage_call(entry, "check", refine.CHECK_SYSTEM + "\n" + INSTRUCTION,
                    payload, supplied, work_dir, limiter, save, verify=True)
                reviewed, issues, audit, _, _ = refine._verify(reply, batch, supplied, structural, ledger, graphics, {})
                links = reply.get("context_links", [])
                link_issues, link_audit = temporal.validate_context_links(links, batch, inventory, supplied, context)
                issues.extend(link_issues)
                returned = {(item["link"]["shot"], item["link"]["asset_id"])
                            for item in link_audit if item["valid"]}
                issues.extend(inv._finding("context_identity_unconfirmed",
                    "Independent reviewer did not confirm the contextual identity link", number)
                    for number, asset in required if (number, asset) not in returned)
                requests, notes = prepare_frame_requests(inv, reply.get("review_requests", []), batch,
                    float(result.get("video", {}).get("fps") or 0))
                issues.extend(n for n in notes if n.get("code") == "invalid_review_request")
                issues.extend(inv._finding("source_detail_unresolved", str(r.get("reason") or
                    "Additional source evidence remains necessary after bounded QA"), r["shot"]) for r in requests)
                checked = {"reviewed_shots": reviewed, "findings": inv._route_findings(issues, wanted, patch),
                           "resolutions": audit, "context_links": link_audit, "retryable": False}
            except Exception as exc:
                checked = {"reviewed_shots": [], "retryable": True, "resolutions": [], "context_links": [],
                    "findings": [f for f in findings_in if f.get("shot") in wanted or f.get("shot") is None]
                    + [inv._finding("source_protocol_review_failed", f"{type(exc).__name__}: {str(exc)[:350]}", n)
                       for n in sorted(wanted)]}
            entry["result"] = copy.deepcopy(checked)
            save()
        processed.update(wanted)
        if on_progress: on_progress("source_protocol_review", len(processed), len(selected))
        return checked

    tasks = [asyncio.create_task(check_batch(selected[i:i+BATCH_SIZE])) for i in range(0, len(selected), BATCH_SIZE)]
    try:
        checked = await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            if not task.done(): task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        save()
        raise
    findings = [copy.deepcopy(f) for f in prior.get("findings", [])
                if f.get("shot") not in affected and (f.get("shot") is not None or affected != numbers)]
    findings += [f for batch in checked for f in batch["findings"]]
    for finding in findings:
        finding.pop("accepted", None)
    resolutions = [r for batch in checked for r in batch["resolutions"]]
    final_ledger = issue_mod.build_issue_ledger({**result, "source_verification": {
        **prior, "findings": findings, "protocol_review": {**previous, "resolutions": resolutions}}})
    findings = final_ledger["issues"]
    journal["output_issue_ledger"] = final_ledger
    reviewed = (set(prior.get("reviewed_shots", [])) - affected) | {n for batch in checked for n in batch["reviewed_shots"]}
    unresolved = (numbers - reviewed) | (set(prior.get("unresolved_shots", [])) - affected)
    unresolved.update(f["shot"] for f in findings if f.get("shot") in numbers)
    if any(f.get("shot") is None for f in findings):
        unresolved.update(numbers)
    usage = copy.deepcopy(prior.get("usage") or {})
    current_usage = {}
    for entry in journal["batches"].values():
        for model, counts in entry.get("usage", {}).items():
            for target in (usage, current_usage):
                total = target.setdefault(model, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
                for field in total: total[field] += counts.get(field, 0)
    retryable = any(batch["retryable"] for batch in checked)
    underlying_retryable = previous.get("underlying_retryable", bool(prior.get("retryable")))
    report = copy.deepcopy(prior)
    # This is a new machine verdict. The old human acceptance belongs only to
    # the preserved prior report, never to facts re-evaluated by this pass.
    report.pop("review", None)
    report.update(status="needs_review" if unresolved else "verified", findings=findings,
        reviewed_shots=sorted(reviewed), unresolved_shots=sorted(unresolved), digest=digest,
        usage=usage, retryable=retryable or underlying_retryable)
    # QA changes verdicts, never source facts. Carry actual correction history,
    # then recompute which corrected shots the machine can now confirm.
    summary = copy.deepcopy(prior.get("issue_summary") or {})
    verified = reviewed - unresolved
    processed_shots = set(summary.get("processed_shots", [])) | affected
    corrected = set(summary.get("corrected_shots", [])) & numbers
    report["issue_summary"] = {**summary,
        "input_findings": summary.get("input_findings", len(prior.get("findings", []))),
        "active_issues": len(findings),
        "duplicates_collapsed": summary.get("duplicates_collapsed", 0)
            + input_ledger["stats"]["duplicates_collapsed"] + final_ledger["stats"]["duplicates_collapsed"],
        "by_category": {category: sum(f.get("category") == category for f in findings)
                        for category in ("technical", "visual", "uncertainty")},
        "processed_shots": sorted(processed_shots), "retained_verified_shots": sorted(verified - processed_shots),
        "corrected_shots": sorted(corrected), "corrected_and_verified_shots": sorted(corrected & verified),
        "verified_shots": sorted(verified), "unresolved_shots": sorted(unresolved),
        "technical_actions": summary.get("technical_actions", 0)}
    report["protocol_review"] = {"policy": policy, "facts_digest": facts,
        "selected_shots": sorted(affected), "retryable": retryable, "underlying_retryable": underlying_retryable,
        "prior_report_digest": inv._digest(prior), "usage": current_usage,
        "resolutions": resolutions, "execution": journal["execution"],
        "context_links": [row for batch in checked for row in batch.get("context_links", [])],
        "issue_ledger": {"input": input_ledger["stats"], "output": final_ledger["stats"]}}
    issue_audit = report.setdefault("issue_audit", {})
    issue_audit["protocol"] = copy.deepcopy(issue_audit.get("protocol", [])) + [{
        "prior_report_digest": inv._digest(prior), "input": input_ledger["audit"], "final": final_ledger["audit"]}]
    report = issue_mod.refresh_report_metadata(report)
    report["protocol_review"]["output_report_digest"] = _report_binding(inv, report)
    result["source_verification"] = report
    result["source_protocol_review_history"] = copy.deepcopy(result.get("source_protocol_review_history", [])) + [
        {"prior_report": copy.deepcopy(prior), "selected_shots": sorted(affected)}]
    journal["result"] = copy.deepcopy(result)
    save()
    return result
