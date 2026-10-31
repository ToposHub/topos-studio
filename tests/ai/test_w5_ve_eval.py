"""W5 tests: editorial quality (VE-01/04/12) + agent eval enhancement (OS-12)."""

from __future__ import annotations

import pytest

from src.features.edit.ai_editing.editorial_quality import (
    OP_EXTRACT,
    OP_ROLL,
    OP_SLIP,
    THREE_TRACK_OPERATIONS,
    EditorialQualityReport,
    TimelineEditOperation,
    UsabilityDimension,
    UsabilityReport,
    UsableRange,
    compute_usability_score,
    evaluate_editorial_quality,
)


# --------------------------------------------------------------------------- #
# VE-01: usability score
# --------------------------------------------------------------------------- #


def test_compute_usability_score_weighted() -> None:
    dims = (
        UsabilityDimension("sharpness", 0.9, weight=2.0),
        UsabilityDimension("stability", 0.5, weight=1.0),
    )
    score = compute_usability_score(dims)
    assert score == pytest.approx((0.9 * 2 + 0.5) / 3)


def test_usability_report_is_usable() -> None:
    report = UsabilityReport(
        asset_id="a1",
        dimensions=(UsabilityDimension("sharpness", 0.8),),
        overall_score=0.8,
    )
    assert report.is_usable


def test_usability_report_blocked_by_copyright() -> None:
    report = UsabilityReport(
        asset_id="a1",
        dimensions=(UsabilityDimension("copyright", 0.0), UsabilityDimension("sharpness", 0.9)),
        overall_score=0.5,
    )
    assert not report.is_usable


def test_usable_range_validates_order() -> None:
    with pytest.raises(ValueError):
        UsableRange(start_frame=100, end_frame=50, recommendation="use")


# --------------------------------------------------------------------------- #
# VE-04: three-track operations
# --------------------------------------------------------------------------- #


def test_three_track_ops_have_six_new_kinds() -> None:
    new_kinds = {OP_EXTRACT, OP_ROLL, OP_SLIP, "ripple_trim", "slide", "lift"}
    assert new_kinds.issubset(THREE_TRACK_OPERATIONS)


def test_timeline_edit_op_flags_subsequent_shift() -> None:
    extract = TimelineEditOperation(op_id="1", kind=OP_EXTRACT, track_id="t1", clip_id="c1")
    assert extract.affects_subsequent_clips

    roll = TimelineEditOperation(op_id="2", kind=OP_ROLL, track_id="t1", clip_id="c1")
    assert not roll.affects_subsequent_clips


def test_timeline_edit_op_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError):
        TimelineEditOperation(op_id="1", kind="magic", track_id="t1")


# --------------------------------------------------------------------------- #
# VE-12: editorial quality evaluator
# --------------------------------------------------------------------------- #


def test_editorial_quality_flags_duration_error() -> None:
    report = evaluate_editorial_quality(
        clip_count=5, total_duration_frames=300, expected_duration_frames=360,
    )
    assert not report.is_passing
    duration = next(f for f in report.findings if f.check == "duration_accuracy")
    assert duration.severity == "error"


def test_editorial_quality_flags_jump_cuts() -> None:
    report = evaluate_editorial_quality(
        clip_count=10, total_duration_frames=300, jump_cut_count=8,
    )
    jump = next(f for f in report.findings if f.check == "jump_cut_density")
    assert jump.severity == "warning"


def test_editorial_quality_passes_clean_input() -> None:
    report = evaluate_editorial_quality(
        clip_count=5, total_duration_frames=360, expected_duration_frames=360,
        beats_total=4, beats_covered=4,
    )
    assert report.is_passing
    assert report.overall_score > 0.8


def test_editorial_quality_report_to_dict() -> None:
    report = evaluate_editorial_quality(clip_count=1, total_duration_frames=10)
    payload = report.to_dict()
    assert "is_passing" in payload
    assert "overall_score" in payload


# --------------------------------------------------------------------------- #
# OS-12: rubric + regression + clustering
# --------------------------------------------------------------------------- #


def _rubric(case_id: str, scores: tuple[RubricScore, ...]) -> RubricReport:
    return RubricReport(case_id=case_id, scores=scores)
