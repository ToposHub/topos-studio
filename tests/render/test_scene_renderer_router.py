"""P0 tests: the renderer router wires HTML scenes into the host main chain.

These prove the W2 audit's P0 gap is closed: a scene carrying an ``html_layer``
is routed to the HTML renderer through every host entry point (preview host
adapter, export worker, public facade, editor controller), while non-HTML
scenes keep using the SVG renderer unchanged.  A fake service stands in for the
pinned Chromium pool so the tests run without a browser.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.features.teaching_animation.core.contracts import (
    AnimationFrame, RenderContext,
)
from src.features.teaching_animation.core.html_layer import build_html_layer
from src.features.teaching_animation.core.model import SceneElement, SceneTransform, TeachingSceneDocument
from src.features.teaching_animation.render import scene_renderer_router as router
from src.features.teaching_animation.render.html_renderer_spike import (
    CaptureEvidence, CaptureFailure, CaptureFailureCode, CaptureSurface,
    CapturedFrame, UrlPolicy,
)
from src.features.teaching_animation.render.scene_renderer_router import (
    HtmlSceneRendererV1Adapter, HtmlVectorSceneRendererV1Adapter,
    backend_for_scene, select_scene_renderer,
)
from src.features.teaching_animation.render.svg_renderer import SvgSceneRenderer


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _FakeService:
    """Stand-in for the pooled Chromium service; records every capture call."""

    backend_build = "fake-html/0.1"

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.fail: CaptureFailure | None = None

    def render_frame(self, html, surface, *, frame, fps_num, fps_den, csp, url_policy,
                     asset_resolver=None, timeout_ms=None, cancel_token=None,
                     transparent=False):
        self.calls.append({
            "html": html, "frame": frame, "transparent": transparent, "dpr": surface.dpr,
            "w": surface.width, "h": surface.height, "resolver": asset_resolver,
            "cancel_token": cancel_token,
        })
        if self.fail is not None:
            return self.fail
        # Deterministic, non-uniform RGBA so it is a realistic captured frame.
        base = (frame * 7 + 11) % 256
        buf = bytearray([base]) * (surface.width * surface.height * 4)
        buf[0:4] = b"\x10\x20\x30\xff"
        evidence = CaptureEvidence("fake-html", self.backend_build, "fake",
                                   (surface.width, surface.height), surface.dpr)
        return CapturedFrame(data=bytes(buf), surface=surface, evidence=evidence)

    def close(self) -> None:
        pass


def _html_scene() -> TeachingSceneDocument:
    scene = TeachingSceneDocument.create_default(name="HTML Scene")
    layer = build_html_layer(
        layer_id="lyr1",
        sanitized_html="<div style='background:#f00;width:100px;height:100px'></div>",
        authored_source="<div></div>",
        viewport_width=scene.width, viewport_height=scene.height,
        fps_num=scene.fps_num, fps_den=scene.fps_den,
        duration_frames=max(1, scene.duration_frames),
        csp="default-src 'none'; style-src 'unsafe-inline'",
    )
    return replace(scene, html_layer=layer)


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #
class TestRouting:
    def test_non_html_scene_uses_svg_renderer(self):
        scene = TeachingSceneDocument.create_default(name="svg")
        renderer = select_scene_renderer(scene)
        assert isinstance(renderer, SvgSceneRenderer)
        assert backend_for_scene(scene) == "svg"

    def test_html_scene_uses_html_adapter(self):
        service = _FakeService()
        scene = _html_scene()
        renderer = select_scene_renderer(scene, html_service=service)
        assert isinstance(renderer, HtmlSceneRendererV1Adapter)
        assert backend_for_scene(scene) == "html"

    def test_html_scene_with_native_layer_uses_composite_adapter(self):
        service = _FakeService()
        scene = replace(
            _html_scene(),
            elements=(SceneElement(
                "overlay", "shape", transform=SceneTransform(x=4.0, y=4.0, width=80.0, height=60.0),
                style={"fill": "#ff8800"}, content={"kind": "rect"},
            ),),
        )
        renderer = select_scene_renderer(scene, html_service=service)
        assert isinstance(renderer, HtmlVectorSceneRendererV1Adapter)
        renderer.prepare(scene, RenderContext(fps=scene.fps, width=scene.width, height=scene.height))
        try:
            frame = renderer.render_frame(AnimationFrame(0, scene.fps))
        finally:
            renderer.cleanup()
        assert frame.pixel_format == "rgba8"
        assert frame.metadata["native_vector_overlay"] is True
        assert "topos-native-vector-overlay" in service.calls[-1]["html"]

    def test_html_scene_without_service_raises_unsupported(self, monkeypatch):
        # Simulate the shared service being unavailable.
        monkeypatch.setattr(router, "get_shared_html_service", lambda: None)
        scene = _html_scene()
        from src.features.teaching_animation.render.html_scene_renderer_v2 import (
            HtmlRenderError,
        )
        with pytest.raises(HtmlRenderError) as exc:
            select_scene_renderer(scene)
        assert exc.value.render_error.code.value == "unsupported_backend"


# --------------------------------------------------------------------------- #
# V1 adapter end-to-end
# --------------------------------------------------------------------------- #
class TestHtmlSceneRendererV1Adapter:
    def test_renders_rgba8_frame_matching_design_dimensions(self):
        service = _FakeService()
        scene = _html_scene()
        adapter = HtmlSceneRendererV1Adapter(service)
        context = RenderContext(fps=scene.fps, width=scene.width, height=scene.height)
        adapter.prepare(scene, context)
        try:
            frame = adapter.render_frame(AnimationFrame(3, context.fps))
        finally:
            adapter.cleanup()
        assert frame.pixel_format == "rgba8"
        assert frame.width == scene.width and frame.height == scene.height
        assert len(frame.data) == scene.width * scene.height * 4
        # The fake was called at the requested frame, at DPR 1 (V1 main chain).
        assert service.calls[0]["frame"] == 3
        assert service.calls[0]["dpr"] == 1.0

    def test_threads_transparency_into_capture(self):
        service = _FakeService()
        scene = _html_scene()
        adapter = HtmlSceneRendererV1Adapter(service)
        context = RenderContext(fps=scene.fps, width=scene.width, height=scene.height,
                                transparent=True)
        adapter.prepare(scene, context)
        try:
            adapter.render_frame(AnimationFrame(0, context.fps))
        finally:
            adapter.cleanup()
        assert service.calls[0]["transparent"] is True

    def test_links_v1_cancellation_to_browser_capture_token(self):
        from src.features.teaching_animation.render.scene_renderer import CancellationToken

        service = _FakeService()
        scene = _html_scene()
        cancellation = CancellationToken()
        adapter = HtmlSceneRendererV1Adapter(service, cancellation=cancellation)
        adapter.prepare(scene, RenderContext(fps=scene.fps, width=scene.width, height=scene.height))
        try:
            adapter.render_frame(AnimationFrame(0, scene.fps))
            browser_token = service.calls[0]["cancel_token"]
            assert browser_token is not None
            assert not browser_token.is_cancelled
            cancellation.cancel()
            assert browser_token.is_cancelled
        finally:
            adapter.cleanup()

    def test_fail_closed_on_capture_failure(self):
        service = _FakeService()
        service.fail = CaptureFailure(CaptureFailureCode.UNSUPPORTED,
                                      "playwright not installed", "fake-html")
        scene = _html_scene()
        adapter = HtmlSceneRendererV1Adapter(service)
        adapter.prepare(scene, RenderContext(fps=scene.fps, width=scene.width, height=scene.height))
        from src.features.teaching_animation.render.html_scene_renderer_v2 import (
            HtmlRenderError,
        )
        with pytest.raises(HtmlRenderError):
            adapter.render_frame(AnimationFrame(0, scene.fps))


# --------------------------------------------------------------------------- #
# Host main-chain integration (preview adapter + export worker)
# --------------------------------------------------------------------------- #
class TestHostChainIntegration:
    def test_host_adapter_renders_html_scene_to_rgba(self, monkeypatch):
        service = _FakeService()
        monkeypatch.setattr(router, "get_shared_html_service", lambda: service)
        scene = _html_scene()
        from src.features.teaching_animation.host_adapters import (
            TimelineSceneFrameProvider,
        )
        from types import SimpleNamespace
        provider = TimelineSceneFrameProvider({scene.scene_id: scene})
        clip = SimpleNamespace(
            asset_id=f"teaching_animation:{scene.scene_id}",
            timeline_start=0, timeline_end=scene.duration_frames,
            source_start=0, source_duration=scene.duration_frames,
        )
        try:
            rgba = provider.render_frame(clip, 2)
        finally:
            provider.close()
        assert rgba.pixel_format == "rgba8"
        assert rgba.width == scene.width
        # Non-trivial content reached the host (not a blank SVG fallback).
        assert service.calls and service.calls[0]["frame"] == 2

    def test_export_worker_renders_html_scene_frames(self, monkeypatch):
        service = _FakeService()
        monkeypatch.setattr(router, "get_shared_html_service", lambda: service)
        scene = _html_scene()
        from src.features.teaching_animation.render.render_worker import RenderWorker
        worker = RenderWorker()
        try:
            context = RenderContext(fps=scene.fps, width=scene.width, height=scene.height)
            result = worker.render_sync(scene, range(0, min(3, scene.duration_frames)), context)
        finally:
            worker.shutdown()
        assert not result.cancelled
        assert len(result.frames) == min(3, scene.duration_frames)
        assert all(f.pixel_format == "rgba8" for f in result.frames)

    def test_facade_preview_frame_routes_html_scene(self, monkeypatch):
        service = _FakeService()
        monkeypatch.setattr(router, "get_shared_html_service", lambda: service)
        scene = _html_scene()
        from src.features.teaching_animation.facade import preview_frame
        res = preview_frame(scene.to_dict(), 1, RenderContext(
            fps=scene.fps, width=scene.width, height=scene.height))
        assert res.ok
        assert res.value.pixel_format == "rgba8"
