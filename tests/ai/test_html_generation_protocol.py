"""AQ-09: controlled Freeform HTML generation protocol.

Pins the generic safety invariant and the browser/render-only exception used by
Motion Graphic Clip.  The "approved = False" case must never be writable in the
generic pipeline, while render-only mode may skip design/static judgement.
"""

from __future__ import annotations

import pytest

from src.features.teaching_animation.ai.html_generation_protocol import (
    HtmlGenerationConstraints, HtmlPipelineStage,
    run_html_generation_pipeline,
)

BENIGN = '<div data-topos-node-id="t1" style="color:#fff">Hello</div>'


class TestWritabilityInvariant:
    def test_unapproved_is_never_writable_even_when_safe(self):
        out = run_html_generation_pipeline(BENIGN, approved=False)
        assert out.writable is False
        assert "not yet approved by the user" in out.blocking_reasons

    def test_approved_and_safe_is_writable_without_backend(self):
        # Preview can be blocked (no renderer) and the page is still writable;
        # export blocks downstream on the renderer, not write.
        out = run_html_generation_pipeline(BENIGN, approved=True, renderer_available=False)
        assert out.writable is True
        preview = next(s for s in out.stages if s.stage == HtmlPipelineStage.PREVIEW)
        assert preview.passed is False  # blocked, reported — but not a write blocker

    def test_sanitizer_empty_output_is_not_writable(self):
        # A payload the sanitizer reduces to nothing (only disallowed tags)
        # fails at SANITIZE.
        out = run_html_generation_pipeline("<script>x()</script>", approved=True)
        assert out.writable is False
        sanitize = next(s for s in out.stages if s.stage == HtmlPipelineStage.SANITIZE)
        assert sanitize.passed is False

    def test_unresolved_asset_blocks_writing(self):
        html = '<div><img src="topos-asset://ghost"></div>'
        out = run_html_generation_pipeline(html, approved=True, known_asset_ids=("real",))
        assert out.writable is False
        assert out.unresolved_assets == ("ghost",)
        bind = next(s for s in out.stages if s.stage == HtmlPipelineStage.BIND_ASSETS)
        assert bind.passed is False

    def test_render_only_mode_blocks_nested_page_boundaries_before_preview(self):
        html = (
            '<section data-topos-page="1"><h1>第一步</h1><p>建立任务上下文。</p>'
            '<article>输入</article><article>目标</article>'
            '<section data-topos-page="2"><h1>第二步</h1><p>规划和执行。</p>'
            '<article>计划</article><article>工具</article></section>'
        )
        out = run_html_generation_pipeline(
            html,
            constraints=HtmlGenerationConstraints(
                post_generation_quality_gate=False,
                viewport_width=1920,
                viewport_height=1080,
            ),
            approved=True,
        )

        static = next(s for s in out.stages if s.stage == HtmlPipelineStage.STATIC_ANALYSIS)
        assert static.passed is False
        assert "嵌套" in static.detail
        assert out.writable is False

    def test_resolved_asset_allows_writing(self):
        html = '<div><img src="topos-asset://logo"></div>'
        out = run_html_generation_pipeline(html, approved=True, known_asset_ids=("logo",))
        assert out.writable is True


class TestSanitizerNeutralisesModelOutput:
    def test_script_is_stripped_remaining_html_is_writable(self):
        # The sanitizer removes <script>; the surviving HTML is safe + approved.
        out = run_html_generation_pipeline(
            '<script>alert(1)</script><div>ok</div>', approved=True)
        assert "<script" not in out.sanitized_html
        assert out.writable is True
        sanitize = next(s for s in out.stages if s.stage == HtmlPipelineStage.SANITIZE)
        assert sanitize.passed is True
        assert "stripped" in sanitize.detail

    def test_blocking_reasons_are_human_readable(self):
        out = run_html_generation_pipeline(BENIGN, approved=False)
        text = "; ".join(out.blocking_reasons)
        assert "approve" in text or "approved" in text


class TestPipelineShape:
    def test_all_six_stages_present_in_order(self):
        out = run_html_generation_pipeline(BENIGN, approved=True)
        order = [s.stage for s in out.stages]
        assert order == [
            HtmlPipelineStage.CONSTRAIN, HtmlPipelineStage.SANITIZE,
            HtmlPipelineStage.STATIC_ANALYSIS, HtmlPipelineStage.BIND_ASSETS,
            HtmlPipelineStage.PREVIEW, HtmlPipelineStage.APPROVE,
        ]

    def test_outcome_round_trips_to_dict(self):
        out = run_html_generation_pipeline(BENIGN, approved=True)
        data = out.to_dict()
        assert data["writable"] is True
        assert len(data["stages"]) == 6

    def test_custom_constraints_applied(self):
        cons = HtmlGenerationConstraints(viewport_width=1080, viewport_height=1080)
        out = run_html_generation_pipeline(BENIGN, approved=True, constraints=cons)
        assert out.writable is True

    def test_render_only_mode_does_not_judge_model_html(self):
        """Motion Graphic browser mode reports rendering, not design quality."""
        html = '<div><img src="topos-asset://ghost"></div>'
        out = run_html_generation_pipeline(
            html,
            approved=True,
            constraints=HtmlGenerationConstraints(
                post_generation_quality_gate=False,
            ),
            known_asset_ids=(),
        )
        static = next(s for s in out.stages if s.stage == HtmlPipelineStage.STATIC_ANALYSIS)
        binding = next(s for s in out.stages if s.stage == HtmlPipelineStage.BIND_ASSETS)
        assert static.passed is True
        assert "render-only" in static.detail
        assert binding.passed is True
        assert out.quality.errors == ()
        assert out.unresolved_assets == ("ghost",)
        assert out.writable is True


# ---------------------------------------------------------------------------
# AQ-09 preview wiring (ADR 0002 — connects the pipeline to a capture backend)
# ---------------------------------------------------------------------------

from src.features.teaching_animation.render.html_renderer_spike import (  # noqa: E402
    CaptureEvidence, CaptureFailure, CaptureFailureCode, CaptureSurface,
    CapturedFrame, UrlPolicy,
)


class _FakeBackend:
    """A capture backend stub that drives the preview wiring without chromium.

    ``hashes`` controls the per-capture content hash sequence; returning two
    different hashes simulates non-determinism, which must surface as an
    ``HTML_FRAME_NONDETERMINISM`` error that blocks writability.
    """

    backend_id = "fake"
    backend_build = "fake/test"

    def __init__(self, hashes, *, available=True, fail=False):
        self._hashes = list(hashes)
        self._available = available
        self._fail = fail
        self.captures = 0

    def is_available(self):
        return self._available

    def capture(self, html, surface, *, frame, fps_num, fps_den, csp, url_policy,
                font_set=(), timeout_ms=10000, cancel_token=None, transparent=False):
        self.captures += 1
        if self._fail:
            return CaptureFailure(CaptureFailureCode.CRASH, "fake crash", self.backend_id)
        digest = self._hashes.pop(0) if self._hashes else "deadbeef"
        need = surface.width * surface.height * 4
        unit = bytes.fromhex(digest)
        data = (unit * (need // len(unit) + 1))[:need]
        evidence = CaptureEvidence(self.backend_id, self.backend_build, "fake",
                                   (surface.width, surface.height), surface.dpr)
        return CapturedFrame(data=data, surface=surface, evidence=evidence)


class TestPreviewWiring:
    def test_no_backend_reports_preview_blocked(self):
        out = run_html_generation_pipeline(BENIGN, approved=True)
        assert out.preview is None
        preview_stage = next(s for s in out.stages if s.stage == HtmlPipelineStage.PREVIEW)
        assert preview_stage.passed is False
        assert "blocked" in preview_stage.detail

    def test_unavailable_backend_reports_blocked(self):
        out = run_html_generation_pipeline(
            BENIGN, approved=True, preview_backend=_FakeBackend([], available=False))
        assert out.preview is not None and out.preview["backend_available"] is False
        preview_stage = next(s for s in out.stages if s.stage == HtmlPipelineStage.PREVIEW)
        assert preview_stage.passed is False

    def test_deterministic_backend_passes_preview_and_records_evidence(self):
        backend = _FakeBackend(["aa" * 16, "aa" * 16])  # same hash twice
        out = run_html_generation_pipeline(BENIGN, approved=True, preview_backend=backend)
        assert out.preview is not None
        assert out.preview["deterministic"] is True
        assert len(out.preview["frame_hashes"]) == 2
        assert out.preview["backend_build"] == "fake/test"
        preview_stage = next(s for s in out.stages if s.stage == HtmlPipelineStage.PREVIEW)
        assert preview_stage.passed is True
        assert out.writable is True
        # to_dict surfaces the preview summary (without raw bytes)
        assert run_html_generation_pipeline(BENIGN, approved=True).to_dict() is not None

    def test_page_deck_preview_checks_later_pages(self):
        class _PageBackend:
            backend_id = "fake-page"
            backend_build = "fake-page/test"

            def is_available(self):
                return True

            def capture(self, html, surface, *, frame, fps_num, fps_den, csp,
                        url_policy, font_set=(), timeout_ms=10000,
                        cancel_token=None, transparent=False):
                del html, fps_num, fps_den, csp, url_policy, font_set
                del timeout_ms, cancel_token, transparent
                need = surface.width * surface.height * 4
                if frame >= 52:
                    data = bytearray(bytes((248, 250, 252, 255)) * (need // 4))
                    data[:4] = bytes((30, 90, 180, 255))
                    data = bytes(data)
                else:
                    data = bytes((i % 251 for i in range(need)))
                evidence = CaptureEvidence(
                    self.backend_id, self.backend_build, "fake",
                    (surface.width, surface.height), surface.dpr,
                )
                return CapturedFrame(data=data, surface=surface, evidence=evidence)

        html = (
            '<html><body>'
            '<section data-topos-page="1"><h1>第一页</h1></section>'
            '<section data-topos-page="2"><h1>第二页</h1></section>'
            '</body></html>'
        )
        out = run_html_generation_pipeline(
            html,
            approved=True,
            preview_backend=_PageBackend(),
            constraints=HtmlGenerationConstraints(
                max_duration_seconds=4.0, fps=25.0,
                viewport_width=320, viewport_height=180,
            ),
        )
        assert out.preview is not None
        assert out.preview["page_count"] == 2
        assert out.preview["page_failures"]
        assert out.writable is False
        preview_stage = next(s for s in out.stages if s.stage == HtmlPipelineStage.PREVIEW)
        assert preview_stage.passed is False

    def test_non_deterministic_preview_blocks_writability(self):
        backend = _FakeBackend(["aa" * 16, "bb" * 16])  # differing hashes
        out = run_html_generation_pipeline(BENIGN, approved=True, preview_backend=backend)
        assert out.preview["deterministic"] is False
        # Non-determinism is an ERROR quality finding → blocks writability even
        # though the page is approved and otherwise safe.
        assert any("differing hashes" in i.message or "differ" in i.message
                   for i in out.quality.errors)
        assert out.writable is False

    def test_capture_failure_reports_failed_stage(self):
        backend = _FakeBackend([], fail=True)
        out = run_html_generation_pipeline(BENIGN, approved=True, preview_backend=backend)
        assert out.preview["failure"]
        preview_stage = next(s for s in out.stages if s.stage == HtmlPipelineStage.PREVIEW)
        assert preview_stage.passed is False
        assert "capture failed" in preview_stage.detail


@pytest.mark.html_renderer
def test_preview_runs_on_real_pinned_backend():
    """End-to-end: the PREVIEW stage captures on the pinned chromium backend."""
    from src.features.teaching_animation.render.html_renderer_spike import (
        PlaywrightChromiumBackend,
    )
    backend = PlaywrightChromiumBackend()
    if not backend.is_available():
        pytest.skip("pinned headless browser unavailable (playwright/chromium not installed)")
    html = (
        "<!DOCTYPE html><html><head><style>"
        "html,body{margin:0;width:320px;height:180px;background:#0b1220}"
        "h1{color:#fff;font-family:sans-serif;margin:24px}"
        "</style></head><body><h1>AI Agent</h1></body></html>"
    )
    out = run_html_generation_pipeline(
        html, approved=True, preview_backend=backend, preview_frame=0,
        constraints=HtmlGenerationConstraints(viewport_width=320, viewport_height=180),
    )
    assert out.preview is not None
    assert out.preview["deterministic"] is True
    assert out.preview["backend_build"].startswith("playwright-chromium/")
    preview_stage = next(s for s in out.stages if s.stage == HtmlPipelineStage.PREVIEW)
    assert preview_stage.passed is True
    assert out.writable is True
