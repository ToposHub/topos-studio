"""Tests for the LLM-driven Freeform HTML generator (AQ-09 upstream half).

Two layers:

* **Plumbing** (default suite, no markers): a stub LLM client checks the prompt
  is built from the constraints, markdown fences are stripped, a successful
  model output flows through the pipeline, and an LLM error is reported as a
  fail-closed non-writable outcome.
* **Real generation** (opt-in, ``real_provider`` + ``html_renderer``): calls the
  configured model (e.g. DeepSeek-v4-flash) to design an HTML page, runs it
  through the pipeline on the pinned Chromium backend, and asserts the result
  sanitizes, passes the quality gate, previews deterministically, and renders
  real content.  Gated by ``TOPOS_RUN_REAL_LLM=1`` and an active AI profile.
"""

from __future__ import annotations

import os

import pytest

from src.features.teaching_animation.ai.html_generation_protocol import (
    HtmlGenerationConstraints, HtmlPipelineStage,
)
from src.features.teaching_animation.ai.html_llm_generator import (
    LlmGenerationRequest, build_html_system_prompt, generate_html_with_llm,
    generate_raw_html, strip_markdown_fence,
)


BENIGN = '<!DOCTYPE html><html><head><style>h1{color:#fff}</style></head><body><h1 data-topos-node-id="t1">Hi</h1></body></html>'


class _StubClient:
    """Minimal LLM client stub matching the LLMClient.complete contract."""

    def __init__(self, *, text: str = BENIGN, raise_exc: BaseException | None = None) -> None:
        self._text = text
        self._raise = raise_exc
        self.calls: list = []

    def complete(self, messages, *, json_mode=False, temperature=0.7, max_tokens=None) -> str:
        self.calls.append(messages)
        if self._raise is not None:
            raise self._raise
        return self._text


class TestPlumbing:
    def test_system_prompt_surfaces_constraints(self):
        prompt = build_html_system_prompt(HtmlGenerationConstraints(viewport_width=1080, viewport_height=1920))
        # The forbidden subset and the canvas size are in the prompt verbatim.
        assert "script" in prompt and "javascript:" in prompt
        assert "1080x1920" in prompt
        assert "@keyframes" in prompt  # tells the model how to drive motion
        assert "capture/export viewport, not a fixed-pixel layout prescription" in prompt
        assert "width:100vw; min-height:100vh" in prompt
        assert "Page richness is a delivery requirement" in prompt

    def test_system_prompt_can_pin_page_count_without_restricting_visual_design(self):
        prompt = build_html_system_prompt(HtmlGenerationConstraints(page_count=4))
        assert "exactly 4 marked page container(s)" in prompt
        assert "page timing and page-to-page transitions" in prompt

    def test_system_prompt_models_each_page_as_editor_timed_webpage(self):
        prompt = build_html_system_prompt(HtmlGenerationConstraints())
        assert "Choose any number of pages" in prompt
        assert "independent full-canvas webpage" in prompt
        assert "editor controls page order, durations, transitions" in prompt

    def test_strip_markdown_fence(self):
        assert strip_markdown_fence("```html\n<div>x</div>\n```") == "<div>x</div>"
        assert strip_markdown_fence("<div>x</div>") == "<div>x</div>"
        assert strip_markdown_fence("```HTML\n<div/>\n```") == "<div/>"
        assert strip_markdown_fence("Here is the document:\n```html\n<!doctype html><html><body>x</body></html>\n```").startswith("<!doctype html>")

    def test_generate_raw_html_calls_client_and_strips_fence(self):
        client = _StubClient(text="```html\n" + BENIGN + "\n```")
        raw = generate_raw_html(client, LlmGenerationRequest(),
                                constraints=HtmlGenerationConstraints())
        assert raw.startswith("<!DOCTYPE html>")
        assert client.calls and client.calls[0][0]["role"] == "system"

    def test_pipeline_runs_on_successful_model_output(self):
        client = _StubClient(text=BENIGN)
        outcome = generate_html_with_llm(client, approved=True,
                                         constraints=HtmlGenerationConstraints())
        assert outcome.sanitized_html  # sanitized payload survives
        stages = {s.stage: s.passed for s in outcome.stages}
        assert stages.get(HtmlPipelineStage.SANITIZE) is True
        assert stages.get(HtmlPipelineStage.STATIC_ANALYSIS) is True
        # PREVIEW is a capability stage, not a safety stage: absence of a
        # backend reports a reason but does NOT block writability.
        assert outcome.writable is True
        assert not outcome.quality.errors

    def test_llm_error_is_fail_closed_non_writable(self):
        class LlmErr(RuntimeError):
            code = "LLM_RATE_LIMITED"
        client = _StubClient(raise_exc=LlmErr("429 slow down"))
        outcome = generate_html_with_llm(client,
                                         constraints=HtmlGenerationConstraints())
        assert outcome.writable is False
        assert any("llm generation failed" in r and "LLM_RATE_LIMITED" in r
                   for r in outcome.blocking_reasons)

    def test_empty_model_output_is_fail_closed(self):
        client = _StubClient(text="   \n  ")
        outcome = generate_html_with_llm(client,
                                         constraints=HtmlGenerationConstraints())
        assert outcome.writable is False
        assert any("empty content" in r for r in outcome.blocking_reasons)


# --------------------------------------------------------------------------- #
# Real generation (opt-in): the configured model → pipeline → render
# --------------------------------------------------------------------------- #
pytestmark_real = [pytest.mark.real_provider, pytest.mark.html_renderer]


def _active_llm_client():
    """Build an LLM client from the user's active AI profile, or None."""
    try:
        from src.features.edit.ai_editing.credential_store import HybridCredentialStore
        from src.features.edit.ai_editing.llm_client import make_client
        from src.features.edit.ai_editing.provider_profiles import ProfileRegistry
    except Exception:
        return None
    reg = ProfileRegistry()
    try:
        reg.load_from_user_file()
    except Exception:
        return None
    profile = reg.active_profile
    if profile is None or not profile.model_id or not profile.endpoint:
        return None
    try:
        return make_client(profile, HybridCredentialStore())
    except Exception:
        return None


@pytest.mark.real_provider
@pytest.mark.html_renderer
@pytest.mark.skipif(os.getenv("TOPOS_RUN_REAL_LLM") != "1",
                    reason="real LLM generation requires TOPOS_RUN_REAL_LLM=1 and an active AI profile")
class TestRealLlmGeneration:
    def test_deepseek_generates_renderable_html(self):
        client = _active_llm_client()
        if client is None:
            pytest.skip("no active AI profile/credential configured in ~/.topos/ai_profiles.json")
        from src.features.teaching_animation.render import scene_renderer_router as router
        backend = router.make_playwright_backend_factory()()
        if not backend.is_available():
            pytest.skip("pinned Chromium backend unavailable (run scripts/bootstrap_html_renderer.sh)")
        from src.features.teaching_animation.render.html_renderer_spike import CaptureSurface
        request = LlmGenerationRequest(
            user_prompt=(
                "Design a 5-second dark tech title card, 480x270 CSS px: a bold "
                "title 'TOPOS', a one-line subtitle, and three small feature "
                "cards that stagger in with a CSS keyframe entrance. Use a "
                "confident dark palette and high-contrast white text. Output "
                "only the HTML document."
            ),
            temperature=0.5, max_tokens=4000,
        )
        constraints = HtmlGenerationConstraints(viewport_width=480, viewport_height=270,
                                                max_duration_seconds=5.0)
        outcome = generate_html_with_llm(
            client, request=request, constraints=constraints,
            approved=True, renderer_available=True,
            preview_backend=backend, preview_frame=12,
            preview_surface=CaptureSurface(480, 270, 1.0),
        )
        # The model's output must sanitize and pass the static quality gate.
        assert outcome.sanitized_html, "sanitized HTML is empty (model output fully stripped?)"
        stages = {s.stage: s.passed for s in outcome.stages}
        assert stages.get(HtmlPipelineStage.SANITIZE) is True, outcome.blocking_reasons
        assert stages.get(HtmlPipelineStage.STATIC_ANALYSIS) is True, outcome.blocking_reasons
        assert not outcome.quality.errors, [e.message for e in outcome.quality.errors]
        # PREVIEW captured a deterministic, non-blank frame on the pinned browser.
        assert outcome.preview is not None, "preview did not run"
        assert outcome.preview.get("deterministic") is True, outcome.preview
        assert outcome.preview.get("frame_hashes"), outcome.preview
        print("\n[real-llm] backend_build:", outcome.preview.get("backend_build"))
        print("[real-llm] frame_hashes:", outcome.preview.get("frame_hashes"))
        print("[real-llm] sanitized_html length:", len(outcome.sanitized_html))

        # End-to-end: the sanitized HTML renders real content through the V2 path.
        from dataclasses import replace
        from src.features.teaching_animation.core.contracts import (
            AlphaMode, CanvasSpace, FrameContext, RenderSurface,
        )
        from src.features.teaching_animation.core.html_layer import build_html_layer
        from src.features.teaching_animation.core.model import TeachingSceneDocument
        from src.features.teaching_animation.render.html_scene_renderer_v2 import (
            HtmlSceneRendererV2,
        )
        scene = TeachingSceneDocument.create_default(name="LLM HTML")
        layer = build_html_layer(
            layer_id="llm1", sanitized_html=outcome.sanitized_html,
            authored_source=outcome.sanitized_html,
            viewport_width=480, viewport_height=270,
            fps_num=scene.fps_num, fps_den=scene.fps_den,
            duration_frames=max(1, scene.duration_frames),
            csp="default-src 'none'; style-src 'unsafe-inline'",
        )
        scene = replace(scene, html_layer=layer)
        service = router.get_shared_html_service()
        renderer = HtmlSceneRendererV2(service)
        fc = FrameContext(frame=12, fps_num=scene.fps_num, fps_den=scene.fps_den,
                          canvas=CanvasSpace.from_dimensions(480, 270),
                          surface=RenderSurface(pixel_width=480, pixel_height=270, dpr=1.0,
                                                alpha_mode=AlphaMode.STRAIGHT))
        session = renderer.prepare(scene, fc)
        try:
            result = renderer.render(session, fc)
        finally:
            renderer.close(session)
        assert result.frame.pixel_format == "rgba8"
        assert len(result.frame.data) == 480 * 270 * 4
        # The frame is not uniform (the model's content actually rendered).
        from src.features.teaching_animation.render.html_renderer_spike import is_uniform_frame
        uniform, _ = is_uniform_frame(result.frame.data, 480, 270)
        assert not uniform, "rendered frame is uniform (blank) — content did not render"
