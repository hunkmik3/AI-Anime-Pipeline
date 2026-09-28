"""A ready clip must not accept unresolved source shots elsewhere in the film."""
from copy import deepcopy

import pytest

from flowboard.services import prompt_coverage as coverage
from tests.test_prompt_coverage import _film


def _pending_film():
    data = _film()
    report = data[-1]
    report.update(status="needs_review", scope="sampled_source_frames", reviewed_shots=[2, 3, 99],
                  unresolved_shots=[99], findings=[{"shot": 99, "code": "source_mismatch"}],
                  scope_notes=[{"code": "audio_not_checked", "message": "Not independently checked."}])
    return data


def test_verified_clip_can_use_partial_film_without_accepting_other_shots():
    _, shots, cast, extra, assets, report = _pending_film()
    original = deepcopy(report)
    assert coverage.validate_source_contract(shots, assets, report, cast + extra) == []
    assert report == original
    assert "review" not in report
    assert coverage.source_readiness_issues([{"source_shots": [99]}], report)


@pytest.mark.parametrize("change", [
    {"unresolved_shots": [2, 99]},
    {"reviewed_shots": [3, 99]},
    {"scope": ""}, {"digest": ""}, {"method": "text_only"}, {"status": "unverified"},
    {"findings": [{"shot": 3, "code": "source_mismatch"}]},
    {"findings": [{"code": "global_failure"}]},
    {"findings": [{"shot": 0, "code": "invalid_scope"}]},
    {"findings": [{"shot": 2, "code": "mismatch", "accepted": True}]},
])
def test_partial_film_never_clears_selected_or_unscoped_problems(change):
    _, shots, cast, extra, assets, report = _pending_film()
    report.update(change)
    assert coverage.validate_source_contract(shots, assets, report, cast + extra)


def test_ready_subset_retains_reference_and_evidence_checks():
    _, shots, cast, extra, assets, report = _pending_film()
    assert any("Required reference image is missing" in error for error in
               coverage.validate_source_contract(shots, assets, report, cast))
    shots[0]["source_evidence"] = ["frame-99"]
    assert any("does not belong" in error for error in
               coverage.validate_source_contract(shots, assets, report, cast + extra))


def test_merged_clip_must_cover_all_source_shots_and_keep_mapping():
    _, _, _, _, _, report = _pending_film()
    assert coverage.source_readiness_issues([{"source_shots": [2, 99]}], report)
    assert coverage.source_readiness_issues([{}], report)
    assert coverage.source_readiness_issues([], report)


def test_existing_explicit_human_acceptance_remains_distinct_from_machine_readiness():
    _, shots, cast, extra, assets, report = _pending_film()
    report.update(status="verified", unresolved_shots=[],
                  findings=[{"shot": 2, "code": "mismatch", "accepted": True}],
                  review={"accepted_by": "actual-reviewer", "accepted_at": "2026-09-26T00:00:00Z"})
    assert coverage.validate_source_contract(shots, assets, report, cast + extra) == []
    report["findings"][0]["accepted"] = False
    assert coverage.validate_source_contract(shots, assets, report, cast + extra)
