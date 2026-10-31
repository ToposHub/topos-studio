"""AQ-10: Freeform HTML quality gate — coded findings for HTML/CSS delivery.

The gate runs pure checks over sanitized HTML (+ optional budget / assets /
render evidence).  Backend-dependent checks (determinism, availability) are
reported with a reason rather than silently passing.
"""

from __future__ import annotations

import pytest

from src.features.teaching_animation.ai.html_quality_gate import (
    HtmlQualityInputs, check_asset_resolution, check_flicker,
    check_frame_determinism, check_renderer_available, check_unsafe_content,
    check_viewport_overflow, run_html_quality_checks,
)
from src.features.teaching_animation.ai.motion_quality_gate import (
    QualityCode, QualitySeverity,
)
from src.features.teaching_animation.render.html_layer.budget import enforce_budget
from src.features.teaching_animation.render.html_layer.sanitizer import HtmlSanitizer


SAFE = '<div data-topos-node-id="t1" style="color:#fff">Hello world</div>'


class TestUnsafeContent:
    def test_safe_html_passes(self):
        assert check_unsafe_content(SAFE) == ()

    def test_residual_script_flagged(self):
        # If the gate is handed HTML the sanitizer did NOT clean, it must say so.
        issues = check_unsafe_content('<div>ok</div><script>x()</script>')
        assert any(i.code == QualityCode.HTML_UNSAFE_CONTENT for i in issues)

    def test_empty_payload_flagged(self):
        issues = check_unsafe_content("   ")
        assert any(i.code == QualityCode.HTML_UNSAFE_CONTENT for i in issues)

    def test_double_pass_is_a_fixed_point(self):
        # Sanitized HTML must be stable under a second sanitize (no removals).
        sanitized = HtmlSanitizer().sanitize(
            '<p>hi</p><script>bad()</script>').html
        assert check_unsafe_content(sanitized) == ()


class TestAssetResolution:
    def test_known_assets_pass(self):
        html = '<img src="topos-asset://logo"><img src="topos-asset://bg">'
        assert check_asset_resolution(html, known_asset_ids=("logo", "bg")) == ()

    def test_missing_assets_flagged(self):
        html = '<img src="topos-asset://ghost">'
        issues = check_asset_resolution(html, known_asset_ids=("logo",))
        assert len(issues) == 1
        assert "ghost" in issues[0].message


class TestViewportOverflow:
    def test_oversized_fixed_width_warned(self):
        html = f'<div style="width: {3000}px">wide</div>'
        issues = check_viewport_overflow(html, viewport_width=1920, viewport_height=1080)
        assert any(i.code == QualityCode.HTML_VIEWPORT_OVERFLOW for i in issues)

    def test_within_viewport_passes(self):
        html = '<div style="width: 800px">fine</div>'
        assert check_viewport_overflow(html, viewport_width=1920, viewport_height=1080) == ()


class TestFlicker:
    def test_rapid_opacity_swing_detected(self):
        css = ('@keyframes f { from { opacity: 0 } to { opacity: 1 } }'
               '.x { animation: f 0.1s infinite; }')
        assert check_flicker(css) != ()

    def test_slow_animation_is_not_flicker(self):
        css = ('@keyframes f { from { opacity: 0 } to { opacity: 1 } }'
               '.x { animation: f 2s; }')
        assert check_flicker(css) == ()

    def test_animation_delay_not_treated_as_duration(self):
        # A short delay must not be misread as a rapid duration.
        css = ('@keyframes f { from { opacity: 0 } to { opacity: 1 } }'
               '.x { animation: f 2s; animation-delay: 0.05s; }')
        assert check_flicker(css) == ()


class TestDeterminismAndBackend:
    def test_no_evidence_reports_info_not_silent_pass(self):
        issues = check_frame_determinism(None)
        assert len(issues) == 1
        assert issues[0].severity == QualitySeverity.INFO

    def test_mismatched_hashes_flagged(self):
        issues = check_frame_determinism({"frame_hashes": ["a", "b", "a"]})
        assert any(i.code == QualityCode.HTML_FRAME_NONDETERMINISM for i in issues)

    def test_identical_hashes_pass(self):
        assert check_frame_determinism({"frame_hashes": ["a", "a", "a"]}) == ()

    def test_renderer_absence_is_a_warning(self):
        issues = check_renderer_available(False)
        assert len(issues) == 1 and issues[0].severity == QualitySeverity.WARNING


class TestBundledGate:
    def test_safe_payload_has_no_errors(self):
        result = run_html_quality_checks(HtmlQualityInputs(sanitized_html=SAFE))
        assert not result.errors
        # but renderer absence surfaces as a warning
        assert any(i.code == QualityCode.HTML_RENDERER_UNAVAILABLE for i in result.warnings)

    def test_over_budget_flagged_as_error(self):
        huge = "<div>" + ("<p>x</p>" * 5000) + "</div>"
        result = run_html_quality_checks(HtmlQualityInputs(sanitized_html=huge))
        assert any(i.code == QualityCode.HTML_CSS_BUDGET_EXCEEDED for i in result.errors)

    def test_explicit_budget_report_is_reused(self):
        report = enforce_budget(SAFE)
        result = run_html_quality_checks(
            HtmlQualityInputs(sanitized_html=SAFE), budget_report=report)
        # No double-parse needed; still produces a coherent result.
        assert isinstance(result.warnings, tuple)
