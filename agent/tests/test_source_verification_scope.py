"""Visual source checks must not claim to verify unheard audio or unsampled motion."""

from __future__ import annotations

import asyncio
import copy
import json

import pytest

from flowboard.services import avis_text, prompt_coverage
from flowboard.services.video_analyzer import source_inventory as inv


def _scene(tmp_path, *, count=1):
    work = tmp_path / "analysis"
    frames = work / "frames"
    frames.mkdir(parents=True)
    video = work / "source.mp4"
    video.write_bytes(b"mock source video")
    shots = []
    for n in range(1, count + 1):
        frame = f"frames/shot{n:03d}_1.jpg"
        (work / frame).write_bytes(f"mock source frame {n}".encode())
        shots.append({
            "shot": n, "start": float(n - 1), "end": float(n), "frames": [frame],
            "dialogue": "Keep the box closed.",
            "source": {
                "subjects": ["lead", "students"],
                "action": "The lead holds the closed box in the left hand while students wait.",
                "camera_movement": "Slow push toward the lead.",
                "dialogue": "Keep the box closed.",
            },
        })
    return video, work, shots


def _draft(shots):
    first_frame = f"shot-{shots[0]['shot']}-frame-1"
    assets = [
        {"id": "lead", "kind": "character", "name": "Lead", "role": "main"},
        {"id": "students", "kind": "background_group", "name": "Waiting students"},
        {"id": "box", "kind": "prop", "name": "Closed box"},
    ]
    assets = [{**asset, "evidence_ids": [first_frame]} for asset in assets]
    return {
        "assets": assets,
        "scenes": [{"id": "hall", "shot_ids": [s["shot"] for s in shots],
                    "present_asset_ids": [a["id"] for a in assets]}],
        "shots": {
            str(s["shot"]): {
                "scene_id": "hall", "evidence_ids": [f"shot-{s['shot']}-frame-1"],
                "asset_presence": [
                    {"asset_id": a["id"], "visibility": "visible",
                     "state": "closed" if a["id"] == "box" else "waiting",
                     "evidence_ids": [f"shot-{s['shot']}-frame-1"],
                     **({"holder_id": "lead", "hand": "left"} if a["id"] == "box" else {})}
                    for a in assets
                ],
            } for s in shots
        },
    }


def _verdict(shots, *, findings=None, status="verified"):
    return {"checks": [
        {"shot": s["shot"], "status": status,
         "evidence_ids": [f"shot-{s['shot']}-frame-1"],
         "findings": copy.deepcopy(findings or [])}
        for s in shots
    ], "review_requests": []}


def _install(monkeypatch, shots, verdict):
    calls = []

    async def complete(model, messages, **kwargs):
        verify = messages[0]["content"] == inv._VERIFY
        payload = json.loads(messages[1]["content"][0]["text"])
        calls.append({"verify": verify, "payload": payload})
        reply = (verdict(payload, calls) if callable(verdict) else copy.deepcopy(verdict)) if verify else _draft(shots)
        return avis_text.Completion(text=json.dumps(reply), model=model)

    monkeypatch.setattr(avis_text, "complete", complete)
    return calls


def _run(video, work, shots):
    return asyncio.run(inv.analyze(video, work, shots, fps=24))


def test_visual_pass_discloses_scope_without_blocking_repair_or_mutating_source(
    tmp_path, monkeypatch,
):
    video, work, shots = _scene(tmp_path)
    original = copy.deepcopy(shots)
    calls = _install(monkeypatch, shots, _verdict(shots))

    inventory, report = _run(video, work, shots)

    assert report["status"] == "verified"
    assert report["reviewed_shots"] == [1]
    assert report["unresolved_shots"] == [] and report["findings"] == []
    assert [call["verify"] for call in calls] == [False, True]
    assert {note["code"] for note in report["scope_notes"]} == {
        "audio_not_checked", "continuous_motion_not_checked",
    }
    assert all(note["message"].strip() for note in report["scope_notes"])
    assert shots == original
    box = next(p for p in inventory["shots"]["1"]["asset_presence"] if p["asset_id"] == "box")
    assert box["hand"] == "left" and box["state"] == "closed"

    resumed_inventory, resumed_report = _run(video, work, shots)
    assert len(calls) == 2
    assert resumed_inventory == inventory
    assert resumed_report["status"] == "verified"
    assert resumed_report["scope_notes"] == report["scope_notes"]
    assert any(item.get("status") == "cached" for item in resumed_report["trace"])

    accepted = inv.accept_review(report, inventory, by="reviewer", at="2026-09-25T00:00:00Z")
    assert accepted["scope_notes"] == report["scope_notes"]
    assert shots == original


def test_verifier_receives_visual_fields_and_caption_text_without_audio_transcript(
    tmp_path, monkeypatch,
):
    video, work, shots = _scene(tmp_path)
    shots[0]["dialogue"] = "ASR_TRANSCRIPT_SENTINEL"
    shots[0]["dialogue_heard"] = "AUDIO_HEARD_SENTINEL"
    shots[0]["source"].update({
        "dialogue": "NESTED_DIALOGUE_SENTINEL",
        "title_card": "HALLWAY",
        "subtitle": "Please wait.",
    })
    original = copy.deepcopy(shots)
    calls = _install(monkeypatch, shots, _verdict(shots))

    _, report = _run(video, work, shots)

    assert report["status"] == "verified"
    verifier_payload = next(call["payload"] for call in calls if call["verify"])
    visual = verifier_payload["source_shots"][0]
    assert "dialogue" not in visual and "dialogue_heard" not in visual
    assert "dialogue" not in visual["source"]
    assert visual["source"]["title_card"] == "HALLWAY"
    assert visual["source"]["subtitle"] == "Please wait."
    assert visual["source"]["action"] == original[0]["source"]["action"]
    assert visual["source"]["camera_movement"] == original[0]["source"]["camera_movement"]
    assert "SENTINEL" not in json.dumps(verifier_payload)
    assert shots == original


@pytest.mark.parametrize("finding", [
    "The box is held in the right hand; the source description says left.",
    "A student visible behind the lead is missing from the inventory.",
    "The box lid is open in the frame but its recorded state is closed.",
    "The rear figure is partially hidden; its claimed identity is uncertain.",
    # No keyword filter may discard a model's explicit unresolved finding.
    "Audio is not checked and the visible box state contradicts the description.",
    "The dialogue cannot be independently verified from these images.",
])
def test_returned_findings_still_block_even_when_model_claims_verified(
    tmp_path, monkeypatch, finding,
):
    video, work, shots = _scene(tmp_path)
    original = copy.deepcopy(shots)
    calls = _install(monkeypatch, shots, _verdict(shots, findings=[finding]))

    _, report = _run(video, work, shots)

    assert report["status"] != "verified" and report["unresolved_shots"] == [1]
    assert any(finding in entry["message"] for entry in report["findings"])
    assert [call["verify"] for call in calls] == [False, True, False, True]
    assert {note["code"] for note in report["scope_notes"]} == {
        "audio_not_checked", "continuous_motion_not_checked",
    }
    assert shots == original


@pytest.mark.parametrize("notes", [None, "", {}, [None], [{}], [123]])
def test_malformed_findings_cannot_become_a_visual_pass(tmp_path, monkeypatch, notes):
    video, work, shots = _scene(tmp_path)
    verdict = _verdict(shots)
    verdict["checks"][0]["findings"] = notes
    _install(monkeypatch, shots, verdict)

    _, report = _run(video, work, shots)

    assert report["status"] != "verified"
    assert report["unresolved_shots"] == [1]
    assert report["findings"]


@pytest.mark.parametrize("defect", [
    "missing_check", "duplicate_check", "foreign_shot", "malformed_check",
    "foreign_evidence", "missing_findings", "invalid_status",
])
def test_incomplete_or_untrusted_checks_cannot_become_a_visual_pass(
    tmp_path, monkeypatch, defect,
):
    video, work, shots = _scene(tmp_path)
    verdict = _verdict(shots)
    row = verdict["checks"][0]
    if defect == "missing_check":
        verdict["checks"] = []
    elif defect == "duplicate_check":
        verdict["checks"].append(copy.deepcopy(row))
    elif defect == "foreign_shot":
        verdict["checks"].append({**row, "shot": 999})
    elif defect == "malformed_check":
        verdict["checks"].append("not a shot check")
    elif defect == "foreign_evidence":
        row["evidence_ids"].append("invented-frame")
    elif defect == "missing_findings":
        row.pop("findings")
    elif defect == "invalid_status":
        row["status"] = "probably fine"
    _install(monkeypatch, shots, verdict)

    _, report = _run(video, work, shots)

    assert report["status"] != "verified" and report["unresolved_shots"] == [1]
    assert report["findings"]


def test_needs_review_with_no_findings_is_not_silently_approved(tmp_path, monkeypatch):
    video, work, shots = _scene(tmp_path)
    _install(monkeypatch, shots, _verdict(shots, status="needs_review"))

    _, report = _run(video, work, shots)

    assert report["status"] != "verified" and report["unresolved_shots"] == [1]
    assert report["findings"]


@pytest.mark.parametrize("frame_request", [
    {"shot": 1, "timestamp_s": 2.0, "crop": None, "reason": "Inspect the box."},
    {"shot": 999, "timestamp_s": 0.5, "crop": None, "reason": "Inspect the box."},
    {"shot": 1, "timestamp_s": 0.5, "crop": [-0.1, 0, 1, 1], "reason": "Inspect the box."},
])
def test_invalid_final_frame_requests_never_provide_evidence_or_a_visual_pass(
    tmp_path, monkeypatch, frame_request,
):
    video, work, shots = _scene(tmp_path)
    verdict = _verdict(shots)
    verdict["review_requests"] = [frame_request]
    _install(monkeypatch, shots, verdict)
    extracted = []

    def extract(*args):
        extracted.append(args)
        raise AssertionError("An invalid frame request reached the extractor")

    monkeypatch.setattr(inv, "_extract_frame", extract)

    _, report = _run(video, work, shots)

    assert extracted == []
    assert report["status"] != "verified" and report["unresolved_shots"] == [1]
    assert any(item.get("status") == "rejected" for item in report["trace"])
    assert any(item["code"] == "review_budget" for item in report["findings"])
    assert report["limits"]["extra_frames_used"] == 0
    assert all(item["sampling"] != "requested_source_frame" for item in report["evidence"])


@pytest.mark.parametrize("status", ["verified", "needs_review"])
def test_source_description_defect_blocks_without_a_pointless_inventory_repair(
    tmp_path, monkeypatch, status,
):
    video, work, shots = _scene(tmp_path)
    original = copy.deepcopy(shots)
    defect = "The source description says the head tilts left, but supplied frames show it upright."
    verdict = _verdict(shots, status=status)
    verdict["checks"][0]["source_description_findings"] = [defect]
    calls = _install(monkeypatch, shots, verdict)

    _, report = _run(video, work, shots)

    assert [call["verify"] for call in calls] == [False, True]
    assert report["status"] != "verified" and report["unresolved_shots"] == [1]
    assert {entry["code"] for entry in report["findings"]} == {"source_description_mismatch"}
    assert any(defect in entry["message"] for entry in report["findings"])
    assert shots == original
    gate_errors = prompt_coverage.validate_source_contract(
        [{"source_shots": [1]}], [], report, [],
    )
    assert "Source analysis must be verified against source_frames before writing." in gate_errors


def test_source_and_inventory_defects_still_repair_and_preserve_both_findings(
    tmp_path, monkeypatch,
):
    video, work, shots = _scene(tmp_path)
    original = copy.deepcopy(shots)
    inventory_defect = "The inventory omits a student visible behind the lead."
    source_defect = "The original source framing says close-up but the lead's legs are visible."
    verdict = _verdict(shots, findings=[inventory_defect], status="needs_review")
    verdict["checks"][0]["source_description_findings"] = [source_defect]
    calls = _install(monkeypatch, shots, verdict)

    _, report = _run(video, work, shots)

    assert [call["verify"] for call in calls] == [False, True, False, True]
    assert report["status"] != "verified" and report["unresolved_shots"] == [1]
    assert {entry["code"] for entry in report["findings"]} == {
        "source_mismatch", "source_description_mismatch",
    }
    assert any(inventory_defect in entry["message"] for entry in report["findings"])
    assert any(source_defect in entry["message"] for entry in report["findings"])
    assert shots == original


@pytest.mark.parametrize("source_findings", [None, "", {}, [None], [{}], [123], [""]])
def test_malformed_source_description_findings_cannot_pass_a_verified_status(
    tmp_path, monkeypatch, source_findings,
):
    video, work, shots = _scene(tmp_path)
    verdict = _verdict(shots, status="verified")
    verdict["checks"][0]["source_description_findings"] = source_findings
    calls = _install(monkeypatch, shots, verdict)

    _, report = _run(video, work, shots)

    assert [call["verify"] for call in calls] == [False, True, False, True]
    assert report["status"] != "verified" and report["unresolved_shots"] == [1]
    assert any(entry["code"] == "invalid_verification" for entry in report["findings"])


def test_prompt_context_preserves_scope_disclosure_and_relevant_blocking_findings():
    global_notes = [
        {"code": "audio_not_checked", "message": "Audio was not independently checked."},
        {"code": "continuous_motion_not_checked", "message": "Only sampled motion was checked."},
    ]
    relevant_note = {"code": "sample_detail", "message": "Shot 4 sampling note.", "shot": 4}
    other_note = {"code": "sample_detail", "message": "Shot 9 sampling note.", "shot": 9}
    relevant_finding = {
        "code": "source_description_mismatch", "message": "Shot 4 source framing is wrong.", "shot": 4,
    }
    report = {
        "status": "needs_review", "method": "source_frames", "scope": "sampled_source_frames_only",
        "reviewed_shots": [4, 9], "unresolved_shots": [4, 9],
        "scope_notes": [*global_notes, relevant_note, other_note],
        "findings": [relevant_finding, {**relevant_finding, "shot": 9}],
    }
    original = copy.deepcopy(report)

    _, scoped = prompt_coverage.scope_model_context([{"source_shots": [4]}], [], report, [])

    assert scoped["scope"] == report["scope"]
    assert scoped["scope_notes"] == [*global_notes, relevant_note]
    assert scoped["findings"] == [relevant_finding]
    assert scoped["status"] == "needs_review" and scoped["unresolved_shots"] == [4]
    assert report == original


@pytest.mark.parametrize("evidence_case", [
    "no_new_frame", "other_shot_frame", "uncited_frame", "cited_new_frame", "repeated_frame",
])
def test_source_description_defect_requires_a_cited_new_frame_from_affected_shot_to_clear(
    tmp_path, monkeypatch, evidence_case,
):
    video, work, shots = _scene(tmp_path, count=2)
    original = copy.deepcopy(shots)
    source_defect = "The source describes the wrong hand holding the box in shot 1."
    calls = []
    extracted = []
    counts = {"extract": 0, "verify": 0}
    request_shot = 2 if evidence_case == "other_shot_frame" else 1
    frame_request = {
        "shot": request_shot, "timestamp_s": request_shot - 0.5,
        "crop": None, "reason": "Inspect the hand holding the box.",
    }

    def extract(video_path, at, output_path, crop):
        extracted.append((at, str(output_path)))
        output_path.write_bytes(f"mock requested source frame at {at}".encode())
        return True

    async def complete(model, messages, **kwargs):
        verify = messages[0]["content"] == inv._VERIFY
        calls.append(verify)
        key = "verify" if verify else "extract"
        counts[key] += 1
        supplied = [
            json.loads(part["text"].removeprefix("SOURCE EVIDENCE "))
            for part in messages[1]["content"]
            if part.get("type") == "text" and part.get("text", "").startswith("SOURCE EVIDENCE ")
        ]
        requested = [e for e in supplied if e.get("sampling") == "requested_source_frame"]
        if not verify:
            reply = _draft(shots)
            if evidence_case == "repeated_frame" and counts["extract"] == 1:
                reply["review_requests"] = [frame_request]
        else:
            reply = _verdict(shots)
            if counts["verify"] == 1:
                reply["checks"][0].update({
                    "status": "needs_review",
                    "findings": ["A visible student is missing from the inventory."],
                    "source_description_findings": [source_defect],
                })
                if evidence_case != "no_new_frame":
                    reply["review_requests"] = [frame_request]
                if evidence_case == "repeated_frame":
                    assert requested, "The first verifier must already have seen this requested frame"
                    reply["checks"][0]["evidence_ids"].append(requested[0]["id"])
            elif evidence_case in {"cited_new_frame", "other_shot_frame", "repeated_frame"}:
                assert requested, "A requested frame must exist before it can be cited"
                for item in requested:
                    reply["checks"][item["shot"] - 1]["evidence_ids"] = [item["id"]]
            # Final verdict omits the unchanged source-description defect in
            # every case. Only fresh same-shot evidence can justify clearing it.
        return avis_text.Completion(text=json.dumps(reply), model=model)

    monkeypatch.setattr(inv, "_extract_frame", extract)
    monkeypatch.setattr(avis_text, "complete", complete)

    _, report = _run(video, work, shots)

    assert counts["verify"] == 2
    assert calls == ([False, False, True, False, True] if evidence_case == "repeated_frame"
                     else [False, True, False, True])
    expected_extractions = 0 if evidence_case == "no_new_frame" else 2 if evidence_case == "repeated_frame" else 1
    assert len(extracted) == expected_extractions
    if evidence_case == "repeated_frame":
        assert extracted[0] == extracted[1]
    initial = next(item for item in report["trace"] if item.get("stage") == "source_verify_initial")
    assert any(f["code"] == "source_description_mismatch" for f in initial["findings"])
    if evidence_case == "cited_new_frame":
        assert report["status"] == "verified" and report["unresolved_shots"] == []
        assert report["findings"] == []
    else:
        assert report["status"] != "verified" and report["unresolved_shots"] == [1]
        assert any(f["code"] == "source_description_mismatch" and source_defect in f["message"]
                   for f in report["findings"])
    cache = json.loads((work / "source_inventory.v1.json").read_text())
    # Every completed verdict is now durable, including unresolved findings.
    # Persistence must never promote a needs-review result into a clean pass.
    saved_result = next(iter(cache["batches"].values()))["result"]
    assert (saved_result["status"] == "verified") == (evidence_case == "cited_new_frame")
    assert shots == original
