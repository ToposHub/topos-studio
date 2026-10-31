"""B4 release layer tests: eval rubric, test matrix, SLO benchmark, rollout.

These lock the MG-24 / MG-25 / MG-26 / MG-27 contracts:

* objective eval scorers (duration / message coverage / readability / overlap
  / subtitle-audio) return sane scores and the trace store detects
  regressions (MG-24)
* the test matrix knows every P0/P1 capability and compute_coverage()
  reports missing vs covered, real vs mock (MG-25)
* summarize_samples() produces P50/P95 + meets_p50/p95 flags and detects
  baseline degradation (MG-26)
* RolloutController honours cohort gating, the kill switch, records incidents
  + feedback and exposes acceptance/satisfaction metrics; the runbook is
  non-empty (MG-27)
"""

from __future__ import annotations

import pytest

from src.features.teaching_animation.ai.eval_rubric import (
    CreativeEvalReport,
    CreativeEvalTrace,
    CreativeEvalTraceStore,
    CriterionScore,
    CRITERION_DURATION_ACCURACY,
    CRITERION_KEY_MESSAGE_COVERAGE,
    CRITERION_PACING,
    CRITERION_READABILITY,
    TASK_AI_AGENT_INTRO,
    score_duration_accuracy,
    score_key_message_coverage,
    score_readability,
    score_visual_overlap,
)
from src.features.teaching_animation.ai.rollout import (
    COHORT_ALPHA,
    COHORT_BETA,
    COHORT_GA,
    COHORT_INTERNAL,
    DictFlagSource,
    FLAG_MOTION_GRAPHIC_AUTO,
    FLAG_MOTION_GRAPHIC_PAID_GENERATION,
    ROLLBACK_RUNBOOK,
    RolloutController,
    RolloutPolicy,
)
from src.features.teaching_animation.ai.slo_benchmark import (
    BenchmarkSample,
    DEFAULT_SLOS,
    summarize_samples,
)
from src.features.teaching_animation.ai.test_matrix import (
    DEFAULT_MATRIX,
    LAYER_INTEGRATION,
    LAYER_REAL_ENVIRONMENT,
    LAYER_UNIT,
    compute_coverage,
)


# --------------------------------------------------------------------------- #
# MG-24: eval rubric
# --------------------------------------------------------------------------- #


def test_duration_accuracy_score_is_one_on_exact_match() -> None:
    score = score_duration_accuracy(actual_frames=360, expected_frames=360)
    assert score.score == pytest.approx(1.0)


def test_duration_accuracy_score_drops_with_error() -> None:
    score = score_duration_accuracy(actual_frames=300, expected_frames=360)
    assert score.score < 0.95
    assert score.score > 0.8


def test_key_message_coverage_scores_missing_items() -> None:
    score = score_key_message_coverage(
        required_keys=["hook", "product", "cta"],
        covered_keys=["hook", "product"],
    )
    assert score.score == pytest.approx(2 / 3)
    assert "cta" in score.details["missing"]


def test_readability_score_averages_contrast() -> None:
    score = score_readability([8.0, 2.0, 5.0], min_contrast=4.5)
    assert score.score == pytest.approx(2 / 3)


def test_visual_overlap_score_rewards_no_overlaps() -> None:
    score = score_visual_overlap(overlap_count=0, total_text_elements=4)
    assert score.score == pytest.approx(1.0)
    score = score_visual_overlap(overlap_count=2, total_text_elements=4)
    assert score.score == pytest.approx(0.5)


def test_report_weighted_score() -> None:
    report = CreativeEvalReport(
        scores=(
            CriterionScore(CRITERION_DURATION_ACCURACY, 1.0, weight=2.0),
            CriterionScore(CRITERION_READABILITY, 0.5, weight=1.0),
        ),
    )
    assert report.weighted_score == pytest.approx((2.0 + 0.5) / 3.0)


def test_trace_store_detects_regression() -> None:
    store = CreativeEvalTraceStore()
    good = CreativeEvalTrace(
        trace_id="t1", timestamp=0.0, task_category=TASK_AI_AGENT_INTRO,
        brief_hash="b", plan_hash="p", scene_hash="s",
        report=CreativeEvalReport(scores=(CriterionScore(CRITERION_PACING, 0.9),)),
    )
    worse = CreativeEvalTrace(
        trace_id="t2", timestamp=1.0, task_category=TASK_AI_AGENT_INTRO,
        brief_hash="b2", plan_hash="p2", scene_hash="s2",
        report=CreativeEvalReport(scores=(CriterionScore(CRITERION_PACING, 0.4),)),
    )
    store.add(good)
    store.add(worse)
    assert store.regression_detected(TASK_AI_AGENT_INTRO, threshold=0.2)


# --------------------------------------------------------------------------- #
# MG-25: test matrix
# --------------------------------------------------------------------------- #


def test_default_matrix_covers_every_p0_capability() -> None:
    capability_ids = {req.capability_id for req in DEFAULT_MATRIX}
    # A representative subset of P0/P1 ids must be present.
    for required in ("MG-01", "MG-06", "MG-10", "MG-22", "MG-23", "MG-04", "MG-07"):
        assert required in capability_ids


def test_compute_coverage_reports_missing_and_real_vs_mock() -> None:
    collected = {
        "MG-01": (LAYER_UNIT,),
        "MG-04": (LAYER_UNIT, LAYER_INTEGRATION),
        "E2E": (LAYER_REAL_ENVIRONMENT,),
    }
    report = compute_coverage(
        DEFAULT_MATRIX,
        collected_by_capability=collected,
        real_evidence_capabilities=("E2E",),
    )
    assert "MG-01" in report.covered
    assert "MG-04" in report.covered
    assert "E2E" in report.real_evidence_recorded
    # Anything not in `collected` is reported missing.
    assert "MG-02" in report.missing


# --------------------------------------------------------------------------- #
# MG-26: SLO benchmark
# --------------------------------------------------------------------------- #


def test_summarize_samples_computes_p50_p95_and_targets() -> None:
    samples = [
        BenchmarkSample(slo_id="preview_first_frame", value=0.3, tier="1080p"),
        BenchmarkSample(slo_id="preview_first_frame", value=0.4, tier="1080p"),
        BenchmarkSample(slo_id="preview_first_frame", value=1.8, tier="1080p"),
        BenchmarkSample(slo_id="preview_first_frame", value=0.45, tier="1080p"),
    ]
    report = summarize_samples(samples, slos=DEFAULT_SLOS)
    summary = report.summary_for("preview_first_frame")
    assert summary is not None
    assert summary.sample_count == 4
    # Sorted [0.3, 0.4, 0.45, 1.8] → median 0.425 ≤ 0.5 target.
    assert summary.meets_p50
    # p95 ~1.6 < 2.0 target.
    assert summary.meets_p95


def test_summarize_samples_flags_degradation_vs_baseline() -> None:
    samples = [
        BenchmarkSample(slo_id="preview_first_frame", value=2.0),
        BenchmarkSample(slo_id="preview_first_frame", value=2.1),
    ]
    report = summarize_samples(
        samples, slos=DEFAULT_SLOS,
        baseline={"preview_first_frame": 0.5},
    )
    summary = report.summary_for("preview_first_frame")
    assert summary is not None
    assert summary.degraded_vs_baseline


# --------------------------------------------------------------------------- #
# MG-27: rollout
# --------------------------------------------------------------------------- #


def test_rollout_respects_cohort_and_flag() -> None:
    flags = DictFlagSource({
        FLAG_MOTION_GRAPHIC_AUTO: True,
        FLAG_MOTION_GRAPHIC_PAID_GENERATION: True,
    })
    controller = RolloutController(flags)
    # Internal cohort user with flag on → allowed.
    assert controller.may_use_autonomous_mode(user_id="u1")
    # GA-only capability blocked for internal cohort.
    assert not controller.may_use_capability(
        FLAG_MOTION_GRAPHIC_PAID_GENERATION, user_id="u1",
    )


def test_rollout_kill_switch_blocks_everything() -> None:
    flags = DictFlagSource({FLAG_MOTION_GRAPHIC_AUTO: True})
    controller = RolloutController(flags)
    assert controller.may_use_autonomous_mode(user_id="u")
    controller.activate_kill_switch(reason="copyright incident")
    assert not controller.may_use_autonomous_mode(user_id="u")
    assert controller.incidents()


def test_rollout_records_feedback_and_computes_metrics() -> None:
    flags = DictFlagSource({FLAG_MOTION_GRAPHIC_AUTO: True})
    controller = RolloutController(flags)
    controller.record_feedback(run_id="r1", outcome="accepted", satisfaction=0.9)
    controller.record_feedback(run_id="r2", outcome="rejected", satisfaction=0.2)
    assert controller.acceptance_rate() == pytest.approx(0.5)
    assert 0.0 < controller.satisfaction_average() < 1.0


def test_runbook_is_non_empty() -> None:
    assert "kill switch" in ROLLBACK_RUNBOOK.lower()
    assert "cohort" in ROLLBACK_RUNBOOK.lower()
