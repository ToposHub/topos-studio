"""Batch C: motion quality gate (AQ-03) + perf SLO (AQ-07) + HTML budget/evidence (FH-07/08)."""

from __future__ import annotations

import pytest

from src.features.teaching_animation.ai.motion_quality_gate import (
    MotionQualityInput, QualityCode, QualitySeverity, run_motion_quality_gate,
)
from src.features.teaching_animation.render.perf_slo import (
    BenchmarkEnvironment, BenchmarkSample, PerfBudget, PerfGate, PerfMetric, default_budgets,
)
from src.features.teaching_animation.render.html_layer import (
    HtmlRenderEvidence, enforce_budget, measure_dom,
)
from src.features.teaching_animation.render.html_renderer_spike.security import ResourceBudget


# ---------------------------------------------------------------------------
# AQ-03 motion quality gate
# ---------------------------------------------------------------------------

class TestQualityGate:
    def test_empty_required_frame_is_error(self) -> None:
        state = MotionQualityInput(visible_counts={0: 3, 12: 0, 23: 2}, required_frames=(0, 12, 23))
        result = run_motion_quality_gate(state)
        assert not result.passed
        assert any(i.code == QualityCode.EMPTY_VISIBLE_FRAME for i in result.errors)

    def test_transition_overlap_too_large(self) -> None:
        state = MotionQualityInput(transition_overlaps=(("t1", 10, 4, 6),))  # 10 > min(4,6)=4
        result = run_motion_quality_gate(state)
        assert any(i.code == QualityCode.TRANSITION_OVERLAP_INVALID for i in result.errors)

    def test_spring_unconverged_is_warning(self) -> None:
        state = MotionQualityInput(springs=(("pop1", 30, 24),))
        result = run_motion_quality_gate(state)
        assert result.passed is True  # warnings don't fail the gate
        assert any(i.code == QualityCode.SPRING_NOT_CONVERGED and i.severity == QualitySeverity.WARNING for i in result.warnings)

    def test_opacity_overshoot_flagged(self) -> None:
        state = MotionQualityInput(property_maxima=(("card", "opacity", 1.3),))
        result = run_motion_quality_gate(state)
        assert any(i.code == QualityCode.OVERSHOOT_OUT_OF_BOUNDS for i in result.warnings)

    def test_scale_overshoot_allowed(self) -> None:
        state = MotionQualityInput(property_maxima=(("card", "scale_x", 1.2),))
        result = run_motion_quality_gate(state)
        assert not any(i.code == QualityCode.OVERSHOOT_OUT_OF_BOUNDS for i in result.issues)

    def test_strobing_detected(self) -> None:
        # 6 on/off flips > threshold 4
        series = [0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0]
        state = MotionQualityInput(strobing_series=(("card", "opacity", tuple(series)),))
        result = run_motion_quality_gate(state)
        assert any(i.code == QualityCode.STROBING for i in result.errors)

    def test_font_unresolved_warning(self) -> None:
        state = MotionQualityInput(font_families=(("CJK", (0x4F60, 0x597D)),))
        result = run_motion_quality_gate(state)
        assert any(i.code == QualityCode.FONT_UNRESOLVED for i in result.warnings)

    def test_effect_blocked_is_error(self) -> None:
        state = MotionQualityInput(effect_dropped=(("glow", "blocked"),))
        result = run_motion_quality_gate(state)
        assert any(i.code == QualityCode.EFFECT_CAPABILITY_MISSING and i.severity == QualitySeverity.ERROR for i in result.errors)

    def test_cache_mismatch_is_error(self) -> None:
        state = MotionQualityInput(cache_pairs=(("aaaa", "bbbb"),))
        result = run_motion_quality_gate(state)
        assert any(i.code == QualityCode.CACHE_MISMATCH for i in result.errors)

    def test_clean_state_passes(self) -> None:
        state = MotionQualityInput(visible_counts={0: 3, 12: 3}, required_frames=(0, 12))
        result = run_motion_quality_gate(state)
        assert result.passed and result.issues == ()


# ---------------------------------------------------------------------------
# AQ-07 perf SLO
# ---------------------------------------------------------------------------

class TestPerfGate:
    def test_within_budget_passes(self) -> None:
        gate = PerfGate(default_budgets())
        samples = (BenchmarkSample(
            PerfMetric.INTERACTIVE_FRAME_MS, 20.0,
            BenchmarkEnvironment(profile="preview", fixture_id="steps")),)
        assert gate.evaluate(samples).passed is True

    def test_over_budget_violation(self) -> None:
        gate = PerfGate(default_budgets())
        samples = (BenchmarkSample(
            PerfMetric.INTERACTIVE_FRAME_MS, 120.0,  # > 66 preview budget
            BenchmarkEnvironment(profile="preview", fixture_id="steps")),)
        result = gate.evaluate(samples)
        assert result.passed is False
        assert len(result.violations) == 1
        assert result.violations[0].exceeded_by == pytest.approx(54.0)

    def test_export_fps_lower_is_worse(self) -> None:
        gate = PerfGate(default_budgets())
        samples = (BenchmarkSample(
            PerfMetric.EXPORT_FPS, 3.0,  # < 6 final budget
            BenchmarkEnvironment(profile="final", fixture_id="steps")),)
        result = gate.evaluate(samples)
        assert result.passed is False
        assert result.violations[0].exceeded_by == pytest.approx(3.0)

    def test_missing_budget_noted_not_failed(self) -> None:
        gate = PerfGate({"preview": default_budgets()["preview"]})
        samples = (BenchmarkSample(
            PerfMetric.FIRST_FRAME_MS, 10.0,
            BenchmarkEnvironment(profile="unknown_profile", fixture_id="x")),)
        result = gate.evaluate(samples)
        assert result.passed is True
        assert result.notes


# ---------------------------------------------------------------------------
# FH-07 HTML budget + FH-08 evidence
# ---------------------------------------------------------------------------

class TestHtmlBudget:
    def test_measure_dom_counts(self) -> None:
        html = "<div><span><b>x</b></span></div>"
        stats = measure_dom(html)
        assert stats.node_count == 3
        assert stats.max_depth == 3

    def test_enforce_budget_flags_oversize(self) -> None:
        big = "<style>" + ("a{color:red}" * 5000) + "</style>"
        budget = ResourceBudget(max_stylesheet_chars=100)
        report = enforce_budget(big, budget)
        assert not report.ok
        assert any(v.budget_field == "max_stylesheet_chars" for v in report.exceeded)

    def test_enforce_budget_flags_too_many_keyframes(self) -> None:
        html = "<style>" + "@keyframes x {} " * 200 + "</style>"
        report = enforce_budget(html, ResourceBudget(max_keyframes=50))
        assert any(v.budget_field == "max_keyframes" for v in report.exceeded)

    def test_within_budget_ok(self) -> None:
        report = enforce_budget("<div><span>hi</span></div>")
        assert report.ok


class TestHtmlEvidence:
    def test_cache_key_changes_with_build(self) -> None:
        a = HtmlRenderEvidence(renderer_id="r", renderer_build="v1", viewport=(1920, 1080),
                               dpr=1.0, source_hash="s", font_set_hash="f", asset_set_hash="g",
                               sanitizer_version="san1", frame_bridge_version="fb1")
        b = HtmlRenderEvidence(**{**a.to_dict(), "renderer_build": "v2"})  # type: ignore[arg-type]
        assert a.cache_key() != b.cache_key()

    def test_round_trip(self) -> None:
        ev = HtmlRenderEvidence(renderer_id="r", renderer_build="v1", viewport=(320, 180),
                                dpr=2.0, source_hash="s", frame_hash="fh")
        data = ev.to_dict()
        assert data["frame_hash"] == "fh"
        assert data["viewport"] == [320, 180]
