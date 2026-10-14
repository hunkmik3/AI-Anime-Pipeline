"""Sequence-aware grouping. Source cuts and speech are constraints, not model output.

Text-only boundary interpretation reuses the existing source analysis. Its receipts
are cached separately from source verification and never approve or edit source facts.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
from pathlib import Path

from flowboard.services import avis_text

VERSION = "sequence-beats-v1"
SYSTEM = """Mark editorial boundary quality from the supplied source shotlist only.
Do not rewrite dialogue, infer new events, alter timing, or approve source fidelity.
A sequence is a connected dramatic development, not every camera/location change.
Telephone intercuts, listener reactions, inserts and flashbacks may belong together.
Prefer an endpoint after a complete exchange plus its reaction; avoid separating a
question from its answer or an action from its immediate consequence. A long sequence
may have several complete beats. Mark those internal endpoints too. No target seconds.
Return JSON {"boundaries":[{"after_shot":integer,"kind":"sequence_end|beat_end|continuing",
"reason":"brief evidence from supplied dialogue/action"}]}. Include each requested
boundary exactly once. Incomplete/unresolved exchanges are continuing, not invented
closure. The final supplied shot is not automatically a sequence end. Neighboring
shots are context only. These labels guide grouping, never authorize retiming."""


def _slim(s):
    return {"shot": s["shot"], "start": s["start"], "end": s["end"],
            "source": {k: s.get("source", {}).get(k) for k in
                       ("setting", "blocking", "action", "reaction", "end_pose", "continuity_note")},
            "dialogue": s.get("dialogue_pieces") or s.get("dialogue_lines") or s.get("dialogue")}


async def boundary_context(analysis: dict, directory: Path) -> dict:
    """Bounded cached calls; unknown/refused attempts are retained and not repeated.

If text interpretation is unavailable, source-sequence boundaries and exact speech
remain usable, with an explicit degraded-mode note. This does not bypass source QA.
"""
    shots = analysis["shots"]
    directory = Path(directory) / "sequence-boundaries"
    directory.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(3)

    async def window(lo):
        hi = min(len(shots), lo + 40)
        expected = [s["shot"] for s in shots[lo:hi]]
        context = shots[max(0, lo-3):min(len(shots), hi+3)]
        numbers = {s["shot"] for s in context}
        payload = {"version": VERSION, "model": "gpt-6-luna", "system": SYSTEM,
                   "requested_boundaries": expected, "shots": [_slim(s) for s in context],
                   "sequences": [q for q in analysis.get("sequences", [])
                                 if any(q["first_shot"] <= n <= q["last_shot"] for n in numbers)],
                   "dialogue_track": [d for d in analysis.get("dialogue_track", [])
                                      if d["end"] > context[0]["start"] and d["start"] < context[-1]["end"]]}
        key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        path = directory / (key + ".json")
        def save(d):
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2))
            tmp.replace(path)
        async with sem:
            if path.exists():
                try:
                    return json.loads(path.read_text())
                except (OSError, ValueError):
                    return {"state": "in_flight_or_unreadable", "input_hash": key}
            receipt = {"state": "submitted", "input_hash": key, "payload": payload}
            try:
                # A second worker must not bill the same boundary interpretation.
                with path.open('x') as stream:
                    json.dump(receipt, stream, ensure_ascii=False)
            except FileExistsError:
                return {"state": "in_flight", "input_hash": key}
            try:
                response = await avis_text.complete("gpt-6-luna", [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}], attempts=1)
                answer = avis_text.extract_json(response.text)
                receipt["answer"] = answer
                rows = answer["boundaries"]
                ids = [r["after_shot"] for r in rows]
                if len(ids) != len(set(ids)) or set(ids) != set(expected):
                    raise ValueError("Boundary coverage mismatch")
                if any(r["kind"] not in {"sequence_end", "beat_end", "continuing"}
                       or not isinstance(r.get("reason"), str) for r in rows):
                    raise ValueError("Invalid boundary labels")
                receipt["state"] = "completed"
            except Exception as exc:
                receipt.update(state="unavailable_or_unknown", error=type(exc).__name__)
            save(receipt)
            return receipt
    receipts = await asyncio.gather(*(window(lo) for lo in range(0, len(shots), 40)))
    return {"version": VERSION, "boundaries": [b for r in receipts if r["state"] == "completed"
                                               for b in r["answer"]["boundaries"]],
            "receipts": [{k: r.get(k) for k in ("state", "input_hash", "error")} for r in receipts],
            "degraded": any(r["state"] != "completed" for r in receipts)}


def plan(shots, sequences, dialogue_track, max_clip_s, *, semantic=None,
         min_clip_s=4., hard_limit_s=15., character_states=None,
         state_specific_references=True, max_shots=None):
    """Globally choose source-cut endpoints, favoring complete narrative units.

Every shot appears once. No speech overlap may cross a request. Entire sequences
that fit should stay intact; longer ones split at complete beats with shared context.
"""
    if not shots:
        return []
    limit = min(float(max_clip_s), float(hard_limit_s))
    if not math.isfinite(limit) or limit <= 0:
        raise ValueError("Invalid clip duration limit")
    for i, s in enumerate(shots):
        if not all(math.isfinite(float(s[k])) for k in ("start", "end")) or s["end"] <= s["start"]:
            raise ValueError("Invalid source shot timing")
        if i and (s["shot"] <= shots[i-1]["shot"] or abs(s["start"]-shots[i-1]["end"]) > .001):
            raise ValueError("Source shots must be ordered, unique and contiguous")
    for d in dialogue_track:
        if not all(math.isfinite(float(d[k])) for k in ("start", "end")) or d["end"] < d["start"]:
            raise ValueError("Invalid dialogue timing")
    speech = []
    for d in sorted(dialogue_track, key=lambda d: d['start']):
        # ASR sometimes emits two rows for one sentence at a breath/comma.
        # Protect that intervening silence too; an absence of words is not an
        # independent request boundary. Never join known different speakers.
        previous = speech[-1] if speech else None
        who = d.get('speaker_id') or d.get('speaker')
        same = previous and (not who or not previous.get('_who') or who == previous['_who'])
        if same and str(previous.get('text', '')).rstrip().endswith((',', ';', ':')):
            previous['end'] = max(previous['end'], d['end'])
            previous['text'] = str(previous.get('text', '')) + ' ' + str(d.get('text', ''))
        else:
            speech.append({**d, '_who': who})
    def safe(t):
        return not any(d["start"] < t-.001 and d["end"] > t+.001 for d in speech)
    if not safe(shots[0]["start"]) or not safe(shots[-1]["end"]):
        raise ValueError("Selected source scope cuts a spoken utterance")
    labels = {b["after_shot"]: b for b in (semantic or {}).get("boundaries", [])}
    ends = {q["last_shot"] for q in sequences if not q.get('_coverage_fallback')}
    n = len(shots)
    best, choices = [float("inf")]*(n+1), [None]*n
    best[n] = 0.
    floor = min(4., limit)
    preferred_floor = min(max(floor, float(min_clip_s)), limit)
    for i in range(n-1, -1, -1):
        states = {}
        for j in range(i, n):
            incoming = (character_states or {}).get(shots[j]["shot"], {})
            if not state_specific_references and any(k in states and states[k] != v for k,v in incoming.items()):
                break
            states.update(incoming)
            duration = shots[j]["end"]-shots[i]["start"]
            if duration > limit+.001:
                break
            if not safe(shots[j]["end"]) or not math.isfinite(best[j+1]):
                continue
            number = shots[j]["shot"]
            kind = labels.get(number, {}).get("kind", "continuing")
            endpoint = 0 if number in ends or j == n-1 or kind == "sequence_end" else 12 if kind == "beat_end" else 90
            crossed = sum(s["shot"] in ends for s in shots[i:j])
            # Duration is deliberately weak: do not sacrifice a finished exchange
            # merely to fill 12/15 seconds. A true short sequence remains intact.
            cost = 35 + endpoint + 180*crossed + 15*max(0, floor-duration)**2
            cost += .25*max(0, preferred_floor-duration)**2
            cost += 3*((limit-duration)/limit)**2 + .2*max(0, j-i+1-(max_shots or n))**2
            value = cost + best[j+1]
            if value < best[i]:
                best[i], choices[i] = value, j
    if choices[0] is None:
        raise ValueError(f"No dialogue-safe source-cut plan within {limit:g}s; do not truncate speech or shots")
    result, i = [], 0
    while i < n:
        j = choices[i]
        ids = [s["shot"] for s in shots[i:j+1]]
        qs = [(k,q) for k,q in enumerate(sequences) if q["first_shot"] <= ids[-1] and q["last_shot"] >= ids[0]]
        endpoint = labels.get(ids[-1], {})
        result.append({"sequence": qs[0][0] if qs else 0, "shots": ids,
                       "sequence_context": [{k:q.get(k) for k in ("first_shot", "last_shot", "title", "goal", "conflict", "beats")} for _,q in qs],
                       "cut_reason": "source sequence end" if ids[-1] in ends else endpoint.get("reason", "speech-safe source cut within duration limit"),
                       "boundary_kind": "sequence_end" if ids[-1] in ends else endpoint.get("kind", "duration_constrained"),
                       "planner_version": VERSION,
                       "semantic_degraded": bool((semantic or {}).get("degraded", False))})
        i = j+1
    return result
