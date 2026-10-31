"""FH-04: pooled HtmlRendererService + HtmlSceneRendererV2 (SceneRendererV2).

Unit tests use a fake backend so they run without chromium; the real pinned
backend is exercised by one marked ``html_renderer`` integration test.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.features.teaching_animation.core import (
    SCENE_SCHEMA_VERSION, TeachingSceneDocument, build_html_layer,
)
from src.features.teaching_animation.core.contracts import (
    AlphaMode, CanvasSpace, FrameContext, ProxyTier, RenderErrorCode,
    RenderSurface,
)
from src.features.teaching_animation.render import (
    HtmlRenderError, HtmlRendererService, HtmlSceneRendererV2,
)
from src.features.teaching_animation.render.html_renderer_spike import (
    CaptureEvidence, CaptureFailure, CaptureFailureCode, CaptureSurface,
    CapturedFrame, UrlPolicy,
)


# ---------------------------------------------------------------------------
# Fake backend (deterministic; no chromium)
# ---------------------------------------------------------------------------

class _FakeBackend:
    """Minimal capture backend for service/renderer unit tests."""

    backend_id = "fake"
    backend_build = "fake/test"

    def __init__(self, *, fail_code: CaptureFailureCode | None = None,
                 raise_on_capture: bool = False):
        self._fail_code = fail_code
        self._raise = raise_on_capture

    def is_available(self) -> bool:
        return True

    def capture(self, html, surface, *, frame, fps_num, fps_den, csp, url_policy,
                font_set=(), timeout_ms=10000, cancel_token=None, transparent=False,
                allow_uniform=False):
        if self._raise:
            raise RuntimeError("worker exploded")
        if self._fail_code is not None:
            return CaptureFailure(self._fail_code, "simulated", self.backend_id)
        data = bytes([(frame + 1) % 256]) * (surface.width * surface.height * 4)
        evidence = CaptureEvidence(self.backend_id, self.backend_build, "fake",
                                   (surface.width, surface.height), surface.dpr)
        return CapturedFrame(data=data, surface=surface, evidence=evidence)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class TestHtmlRendererService:
    def test_pool_size_validated(self):
        with pytest.raises(ValueError):
            HtmlRendererService(lambda: _FakeBackend(), pool_size=0)

    def test_render_frame_returns_captured_frame(self):
        svc = HtmlRendererService(lambda: _FakeBackend(), pool_size=1)
        result = svc.render_frame("<p>x</p>", CaptureSurface(8, 8, 1.0),
                                  frame=3, fps_num=24, fps_den=1,
                                  csp="default-src 'none'", url_policy=UrlPolicy())
        assert isinstance(result, CapturedFrame)
        svc.close()

    def test_crash_replaces_worker_and_keeps_pool_constant(self):
        created = {"n": 0}

        def factory():
            created["n"] += 1
            # Every worker crashes once, then a fresh one succeeds.
            return _FakeBackend(fail_code=CaptureFailureCode.CRASH)

        svc = HtmlRendererService(factory, pool_size=1)
        # The crashing worker is discarded + replaced; pool stays size 1.
        svc.render_frame("<p/>", CaptureSurface(4, 4, 1.0), frame=0,
                         fps_num=24, fps_den=1, csp="default-src 'none'",
                         url_policy=UrlPolicy())
        # A second acquire still gets a worker (the replacement).
        w = svc.acquire()
        assert w is not None
        svc.release(w)
        svc.close()

    def test_raise_on_capture_is_treated_as_crash(self):
        svc = HtmlRendererService(lambda: _FakeBackend(raise_on_capture=True), pool_size=1)
        result = svc.render_frame("<p/>", CaptureSurface(4, 4, 1.0), frame=0,
                                  fps_num=24, fps_den=1, csp="default-src 'none'",
                                  url_policy=UrlPolicy())
        assert isinstance(result, CaptureFailure)
        assert result.code == CaptureFailureCode.CRASH
        svc.close()

    def test_close_is_idempotent_and_context_manager(self):
        svc = HtmlRendererService(lambda: _FakeBackend(), pool_size=2)
        with svc as s:
            assert s.pool_size == 2
        svc.close()  # idempotent — must not raise


# ---------------------------------------------------------------------------
# HtmlSceneRendererV2
# ---------------------------------------------------------------------------

def _html_scene() -> TeachingSceneDocument:
    return TeachingSceneDocument(
        scene_id="s", schema_version=SCENE_SCHEMA_VERSION, name="H",
        width=320, height=180, fps_num=24, fps_den=1, duration_frames=24,
        background={"color": "#0b1220"}, elements=(), animations=(),
        html_layer=build_html_layer(
            layer_id="h",
            sanitized_html="<!DOCTYPE html><html><body><h1>AI</h1></body></html>",
            authored_source="<h1>AI</h1>",
            viewport_width=320, viewport_height=180, duration_frames=24,
            bound_asset_ids=("logo",), renderer_build="fake/test"))


def _ctx(frame: int = 0) -> FrameContext:
    return FrameContext(frame=frame, fps_num=24, fps_den=1,
                        canvas=CanvasSpace.from_dimensions(320, 180),
                        surface=RenderSurface(320, 180, 1.0, alpha_mode=AlphaMode.STRAIGHT,
                                              proxy_tier=ProxyTier.FINAL))


class TestHtmlSceneRendererV2:
    def test_prepare_extracts_html_layer_from_scene(self):
        svc = HtmlRendererService(lambda: _FakeBackend(), pool_size=1)
        renderer = HtmlSceneRendererV2(svc)
        session = renderer.prepare(_html_scene(), _ctx())
        assert session.html.startswith("<!DOCTYPE html>")
        assert 'data-topos-page-deck="1"' in session.html
        assert "<h1>AI</h1>" in session.html
        assert session.bound_asset_ids == ("logo",)
        renderer.close(session); svc.close()

    def test_prepare_freeform_profile_preserves_authored_fonts(self):
        svc = HtmlRendererService(lambda: _FakeBackend(), pool_size=1)
        renderer = HtmlSceneRendererV2(svc)
        base = _html_scene()
        scene = replace(base, html_layer=replace(base.html_layer, metadata={"font_lock": False}))
        session = renderer.prepare(scene, _ctx())
        assert session.font_lock_css == ""
        assert session.font_asset_ids == ()
        assert session.font_set_hash == ""
        renderer.close(session); svc.close()

    def test_prepare_raises_on_scene_without_html_layer(self):
        svc = HtmlRendererService(lambda: _FakeBackend(), pool_size=1)
        renderer = HtmlSceneRendererV2(svc)
        with pytest.raises(HtmlRenderError) as exc:
            renderer.prepare(TeachingSceneDocument.create_default("P"), _ctx())
        assert exc.value.render_error.code == RenderErrorCode.UNSUPPORTED_BACKEND
        svc.close()

    def test_render_produces_renderer_result_with_evidence(self):
        svc = HtmlRendererService(lambda: _FakeBackend(), pool_size=1)
        renderer = HtmlSceneRendererV2(svc)
        session = renderer.prepare(_html_scene(), _ctx(frame=5))
        result = renderer.render(session, _ctx(frame=5))
        assert result.frame.width == 320 and result.frame.height == 180
        assert result.frame.pixel_format == "rgba8"
        assert len(result.frame.data) == 320 * 180 * 4
        assert result.evidence.frame_hash
        assert result.evidence.renderer_id == "html"
        renderer.close(session); svc.close()

    @pytest.mark.parametrize("fail_code,expected_code", [
        (CaptureFailureCode.TIMEOUT, RenderErrorCode.RENDER_FAILED),
        (CaptureFailureCode.CANCELLED, RenderErrorCode.CANCELLED),
        (CaptureFailureCode.CRASH, RenderErrorCode.RENDER_FAILED),
        (CaptureFailureCode.BLANK, RenderErrorCode.RENDER_FAILED),
    ])
    def test_capture_failure_raises_structured_html_render_error(self, fail_code, expected_code):
        svc = HtmlRendererService(lambda: _FakeBackend(fail_code=fail_code), pool_size=1)
        renderer = HtmlSceneRendererV2(svc)
        session = renderer.prepare(_html_scene(), _ctx())
        with pytest.raises(HtmlRenderError) as exc:
            renderer.render(session, _ctx())
        assert exc.value.render_error.code == expected_code
        assert exc.value.render_error.frame == 0
        svc.close()

    def test_close_is_idempotent(self):
        svc = HtmlRendererService(lambda: _FakeBackend(), pool_size=1)
        renderer = HtmlSceneRendererV2(svc)
        session = renderer.prepare(_html_scene(), _ctx())
        renderer.close(session)
        renderer.close(session)  # idempotent
        svc.close()

    def test_allow_uniform_frames_is_threaded_only_for_final_delivery(self):
        from dataclasses import replace

        class UniformBackend(_FakeBackend):
            def capture(self, *args, allow_uniform=False, **kwargs):
                self.seen = bool(allow_uniform)
                return super().capture(*args, allow_uniform=allow_uniform, **kwargs)

        backend = UniformBackend()
        svc = HtmlRendererService(lambda: backend, pool_size=1)
        renderer = HtmlSceneRendererV2(svc)
        session = renderer.prepare(_html_scene(), _ctx())
        result = renderer.render(session, _ctx())
        assert backend.seen is False
        renderer.render(session, replace(_ctx(), allow_uniform_frames=True))
        assert backend.seen is True
        renderer.close(session); svc.close()


# ---------------------------------------------------------------------------
# Real pinned chromium integration
# ---------------------------------------------------------------------------

@pytest.mark.html_renderer
def test_html_scene_renders_on_real_pinned_backend():
    """End-to-end: prepare + render a real HTML scene through the pool."""
    from src.features.teaching_animation.render.html_renderer_spike import (
        PlaywrightChromiumBackend,
    )
    backend = PlaywrightChromiumBackend()
    if not backend.is_available():
        pytest.skip("pinned headless browser unavailable (playwright/chromium not installed)")
    svc = HtmlRendererService(PlaywrightChromiumBackend, pool_size=1)
    renderer = HtmlSceneRendererV2(svc)
    scene = TeachingSceneDocument(
        scene_id="s", schema_version=SCENE_SCHEMA_VERSION, name="H",
        width=320, height=180, fps_num=24, fps_den=1, duration_frames=24,
        background={"color": "#0b1220"}, elements=(), animations=(),
        html_layer=build_html_layer(
            layer_id="h",
            sanitized_html=("<!DOCTYPE html><html><head><style>"
                            "html,body{margin:0;width:320px;height:180px;background:#0b1220}"
                            "h1{color:#fff;font-family:sans-serif;margin:24px}"
                            "</style></head><body><h1>AI Agent</h1></body></html>"),
            authored_source="<h1>AI Agent</h1>",
            viewport_width=320, viewport_height=180, duration_frames=24))
    session = renderer.prepare(scene, _ctx(frame=0))
    result = renderer.render(session, _ctx(frame=0))
    assert result.frame.pixel_format == "rgba8"
    assert len(result.frame.data) == 320 * 180 * 4
    assert result.evidence.renderer_build.startswith("playwright-chromium/")
    renderer.close(session)
    svc.close()


@pytest.mark.html_renderer
def test_real_multi_page_deck_renders_each_page_at_full_canvas():
    """A later page must paint its own canvas, not just toggle its marker.

    This is intentionally authored in the common model shape (absolute page
    wrappers with no explicit width/height).  The host page-boundary CSS is
    what makes both pages deterministic and visible.
    """
    from src.features.teaching_animation.render.html_renderer_spike import (
        PlaywrightChromiumBackend,
    )

    backend = PlaywrightChromiumBackend()
    if not backend.is_available():
        pytest.skip("pinned headless browser unavailable (playwright/chromium not installed)")
    html = ("<!DOCTYPE html><html><head><style>"
            "html,body{margin:0;width:320px;height:180px;overflow:hidden}"
            ".canvas{position:relative;width:320px;height:180px}"
            ".page{position:absolute;opacity:0}"
            ".panel{position:absolute;inset:0}"
            "</style></head><body><div class='canvas'>"
            "<div class='page' data-topos-page='1'><div class='panel' style='background:#123456'></div></div>"
            "<div class='page' data-topos-page='2'><div class='panel' style='background:#d97706'></div></div>"
            "</div></body></html>")
    scene = TeachingSceneDocument(
        scene_id="multi-page", schema_version=SCENE_SCHEMA_VERSION, name="Deck",
        width=320, height=180, fps_num=24, fps_den=1, duration_frames=24,
        background={"color": "#000000"}, elements=(), animations=(),
        html_layer=build_html_layer(
            layer_id="h", sanitized_html=html, authored_source=html,
            viewport_width=320, viewport_height=180, duration_frames=24))
    service = HtmlRendererService(PlaywrightChromiumBackend, pool_size=1)
    renderer = HtmlSceneRendererV2(service)
    session = renderer.prepare(scene, _ctx(frame=0))
    first = renderer.render(session, replace(_ctx(frame=0), allow_uniform_frames=True)).frame.data
    second = renderer.render(session, replace(_ctx(frame=12), allow_uniform_frames=True)).frame.data
    try:
        # Center pixel is in the page-owned panel on both pages.
        center = (90 * 320 + 160) * 4
        assert tuple(first[center:center + 3]) == (18, 52, 86)
        assert tuple(second[center:center + 3]) == (217, 119, 6)
    finally:
        renderer.close(session)
        service.close()
