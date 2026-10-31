"""S0 end-to-end tests for the HTML motion layer through the host main chain.

These close the W2 audit's S0 gap: HTML-only scenes now render real content end
to end, with the pinned Chromium backend validating the Batch-1 contract fixes
(DPR/viewport, alpha, asset resolver) against a real browser — not just fakes.

Real-browser cases are marked ``html_renderer`` and skip-with-reason when the
pinned backend is absent (the CI gate installs it so they run there).  The
clip-insert and undo cases are pure logic and run everywhere.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.features.teaching_animation.core.contracts import (
    AlphaMode, AnimationFrame, CanvasSpace, FrameContext, RenderContext,
    RenderSurface,
)
from src.features.teaching_animation.core.html_layer import build_html_layer
from src.features.teaching_animation.core.html_page_deck import PageDeckTiming
from src.features.teaching_animation.core.model import TeachingSceneDocument
from src.features.teaching_animation.render import scene_renderer_router as router
from src.features.teaching_animation.render.scene_renderer_router import (
    HtmlSceneRendererV1Adapter, select_scene_renderer,
)
from src.features.teaching_animation.render.svg_renderer import SvgSceneRenderer


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _backend():
    from src.features.teaching_animation.render.html_renderer_spike.playwright_backend import (
        PlaywrightChromiumBackend,
    )
    return PlaywrightChromiumBackend()


def _skip_if_no_backend(backend):
    if not backend.is_available():
        pytest.skip(
            "pinned headless browser unavailable (playwright/chromium not installed); "
            "run scripts/bootstrap_html_renderer.sh")


def _html_scene(*, html: str, viewport_w: int = 200, viewport_h: int = 200,
                duration: int = 6, bound_assets=()) -> TeachingSceneDocument:
    scene = TeachingSceneDocument.create_default(name="HTML S0")
    layer = build_html_layer(
        layer_id="s0",
        sanitized_html=html,
        authored_source=html,
        viewport_width=viewport_w, viewport_height=viewport_h,
        fps_num=scene.fps_num, fps_den=scene.fps_den,
        duration_frames=duration,
        bound_asset_ids=tuple(bound_assets),
        csp="default-src 'none'; style-src 'unsafe-inline'; img-src topos-asset://*",
    )
    return replace(scene, html_layer=layer)


def _pixel(rgba: bytes, w: int, x: int, y: int) -> tuple[int, int, int, int]:
    o = (y * w + x) * 4
    return (rgba[o], rgba[o + 1], rgba[o + 2], rgba[o + 3])


def _solid_png(rgba: tuple[int, int, int, int], w: int = 24, h: int = 24) -> bytes:
    import io
    from PIL import Image
    img = Image.new("RGBA", (w, h), rgba)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Real-browser end-to-end (html_renderer marker)
# --------------------------------------------------------------------------- #
@pytest.mark.html_renderer
class TestHtmlS0RealBackend:
    def test_html_only_scene_renders_content_through_host_adapter(self):
        backend = _backend()
        _skip_if_no_backend(backend)
        service = router.get_shared_html_service()
        html = ("<html><head><style>html,body{margin:0;background:#ffffff}"
                ".r{position:absolute;left:0;top:0;width:80px;height:80px;"
                "background:#ff0000}</style></head>"
                "<body><div class='r'></div></body></html>")
        scene = _html_scene(html=html, viewport_w=200, viewport_h=200)
        adapter = HtmlSceneRendererV1Adapter(service)
        ctx = RenderContext(fps=scene.fps, width=scene.width, height=scene.height)
        adapter.prepare(scene, ctx)
        try:
            frame = adapter.render_frame(AnimationFrame(0, ctx.fps))
        finally:
            adapter.cleanup()
        assert frame.pixel_format == "rgba8"
        # The red div reached the host chain (top-left is red, not a blank/white fallback).
        assert _pixel(frame.data, frame.width, 10, 10)[:3] == (255, 0, 0)

    def test_browser_page_transition_crossfades_at_page_boundary(self):
        """The deterministic capture path renders both pages during a transition."""
        backend = _backend()
        _skip_if_no_backend(backend)
        from src.features.teaching_animation.render.html_renderer_service import (
            HtmlRendererService,
        )
        from src.features.teaching_animation.render.html_scene_renderer_v2 import (
            HtmlSceneRendererV2,
        )
        from src.features.teaching_animation.render.html_renderer_spike.playwright_backend import (
            PlaywrightChromiumBackend,
        )

        width, height, fps = 160, 90, 24
        html = (
            "<html><head><style>"
            "html,body{margin:0;width:160px;height:90px;background:#000}"
            ".page-a{background:#f01e1e}.page-b{background:#1e55f0}"
            "</style></head><body>"
            "<section class='page-a' data-topos-page='1'></section>"
            "<section class='page-b' data-topos-page='2'></section>"
            "</body></html>"
        )
        scene = replace(
            TeachingSceneDocument.create_default(name="transition"),
            width=width, height=height, fps_num=fps, fps_den=1,
            duration_frames=48,
        )
        timing = PageDeckTiming(
            2, "default", 1.0, transition="crossfade", transition_seconds=0.4,
        )
        layer = build_html_layer(
            layer_id="transition", sanitized_html=html, authored_source=html,
            viewport_width=width, viewport_height=height,
            fps_num=fps, fps_den=1, duration_frames=48,
            csp="default-src 'none'; style-src 'unsafe-inline';",
            metadata={"page_timing": timing.to_dict()},
        )
        scene = replace(scene, html_layer=layer)
        service = HtmlRendererService(lambda: PlaywrightChromiumBackend(), pool_size=1)
        renderer = HtmlSceneRendererV2(service)
        session = None
        try:
            session = renderer.prepare(scene, FrameContext.for_scene(scene, 0))
            def _context(frame: int) -> FrameContext:
                return replace(
                    FrameContext.for_scene(scene, frame), allow_uniform_frames=True,
                )
            start = renderer.render(session, _context(24)).frame
            middle = renderer.render(session, _context(28)).frame
            end = renderer.render(session, _context(36)).frame
        finally:
            if session is not None:
                renderer.close(session)
            service.close()

        start_rgb = _pixel(start.data, start.width, width // 2, height // 2)[:3]
        middle_rgb = _pixel(middle.data, middle.width, width // 2, height // 2)[:3]
        end_rgb = _pixel(end.data, end.width, width // 2, height // 2)[:3]
        assert start_rgb[0] > 220 and start_rgb[2] < 80
        assert middle_rgb[0] > 40 and middle_rgb[2] > 40
        assert end_rgb[2] > 200 and end_rgb[0] < 80

    def test_2x_dpr_uses_css_viewport_not_physical(self):
        """The P1 DPR fix: CSS viewport = physical / dpr, device_scale_factor = dpr.

        A 100 CSS-px red div at DPR 2 occupies 200 physical px; at the old bug
        (physical-as-CSS-viewport, DSF 1) it would occupy only 100 physical px.
        Pixel (150,150) on a 400x400 surface is red only when DPR is honoured.
        """
        backend = _backend()
        _skip_if_no_backend(backend)
        from src.features.teaching_animation.render.html_scene_renderer_v2 import (
            HtmlSceneRendererV2,
        )
        html = ("<html><head><style>html,body{margin:0;background:#ffffff}"
                ".r{position:absolute;left:0;top:0;width:100px;height:100px;"
                "background:#ff0000}</style></head>"
                "<body><div class='r'></div></body></html>")
        scene = _html_scene(html=html, viewport_w=200, viewport_h=200)
        service = router.get_shared_html_service()
        renderer = HtmlSceneRendererV2(service)
        fc = FrameContext(
            frame=0, fps_num=24, fps_den=1,
            canvas=CanvasSpace.from_dimensions(200, 200),
            surface=RenderSurface(pixel_width=400, pixel_height=400, dpr=2.0),
        )
        session = renderer.prepare(scene, fc)
        try:
            result = renderer.render(session, fc)
        finally:
            renderer.close(session)
        assert result.frame.width == 400 and result.frame.height == 400
        # Inside the 200x200 physical red region (100 CSS * DPR 2).
        assert _pixel(result.frame.data, 400, 150, 150)[:3] == (255, 0, 0)
        # Outside it.
        assert _pixel(result.frame.data, 400, 350, 350)[:3] == (255, 255, 255)

    def test_transparent_capture_produces_alpha(self):
        backend = _backend()
        _skip_if_no_backend(backend)
        from src.features.teaching_animation.render.html_scene_renderer_v2 import (
            HtmlSceneRendererV2,
        )
        html = ("<html><head><style>html,body{margin:0;background:transparent}"
                ".r{position:absolute;left:0;top:0;width:40px;height:40px;"
                "background:#ff0000}</style></head>"
                "<body><div class='r'></div></body></html>")
        scene = _html_scene(html=html, viewport_w=100, viewport_h=100)
        service = router.get_shared_html_service()
        renderer = HtmlSceneRendererV2(service)
        fc = FrameContext(
            frame=0, fps_num=24, fps_den=1,
            canvas=CanvasSpace.from_dimensions(100, 100),
            surface=RenderSurface(pixel_width=100, pixel_height=100, dpr=1.0,
                                  alpha_mode=AlphaMode.PREMULT),
        )
        session = renderer.prepare(scene, fc)
        try:
            result = renderer.render(session, fc)
        finally:
            renderer.close(session)
        # The empty corner is transparent (alpha 0); the red div is opaque.
        assert _pixel(result.frame.data, 100, 90, 90)[3] == 0
        assert _pixel(result.frame.data, 100, 10, 10) == (255, 0, 0, 255)

    def test_bound_asset_resolves_to_real_bytes(self):
        backend = _backend()
        _skip_if_no_backend(backend)
        from src.features.teaching_animation.render.html_renderer_service import (
            HtmlRendererService,
        )
        from src.features.teaching_animation.render.html_renderer_spike import (
            CaptureSurface, UrlPolicy
        )
        from src.features.teaching_animation.render.html_renderer_spike.playwright_backend import (
            PlaywrightChromiumBackend,
        )
        red_png = _solid_png((255, 0, 0, 255), 32, 32)
        html = ("<html><head><style>html,body{margin:0;background:#fff}"
                "img{position:absolute;left:0;top:0;width:60px;height:60px}"
                "</style></head>"
                "<body><img src='topos-asset://red-tile'></body></html>")
        service = HtmlRendererService(lambda: PlaywrightChromiumBackend(), pool_size=1)
        try:
            result = service.render_frame(
                html, CaptureSurface(100, 100, 1.0),
                frame=0, fps_num=24, fps_den=1,
                csp="default-src 'none'; style-src 'unsafe-inline'; img-src topos-asset://*",
                url_policy=UrlPolicy(frozenset({"red-tile"})),
                asset_resolver={"red-tile": red_png},
            )
        finally:
            service.close()
        from src.features.teaching_animation.render.html_renderer_spike import CapturedFrame
        assert isinstance(result, CapturedFrame)
        # Real red bytes from the resolver reached the page (not the 1x1 placeholder).
        assert _pixel(result.data, 100, 30, 30)[:3] == (255, 0, 0)

    def test_asset_resolver_does_not_leak_across_requests(self):
        """The P1 isolation fix: a later capture without a resolver must NOT
        receive the previous request's bound bytes.

        The page carries a fixed non-uniform structure (a coloured bar) so
        neither capture is a uniform frame — the blank detector only flags
        truly uniform frames, and we want to inspect the <img> area in both.
        """
        backend = _backend()
        _skip_if_no_backend(backend)
        from src.features.teaching_animation.render.html_renderer_service import (
            HtmlRendererService,
        )
        from src.features.teaching_animation.render.html_renderer_spike import (
            CaptureSurface, UrlPolicy
        )
        from src.features.teaching_animation.render.html_renderer_spike.playwright_backend import (
            PlaywrightChromiumBackend,
        )
        red_png = _solid_png((255, 0, 0, 255), 32, 32)
        # Blue bar (always rendered, keeps the frame non-uniform) + an <img>
        # whose content depends on whether the resolver is bound.
        html = ("<html><head><style>html,body{margin:0;background:#ffffff}"
                ".bar{position:absolute;left:0;top:0;width:100px;height:25px;"
                "background:#0066cc}"
                "img{position:absolute;left:0;top:40px;width:40px;height:40px}"
                "</style></head>"
                "<body><div class='bar'></div>"
                "<img src='topos-asset://red-tile'></body></html>")
        service = HtmlRendererService(lambda: PlaywrightChromiumBackend(), pool_size=1)
        try:
            first = service.render_frame(
                html, CaptureSurface(100, 100, 1.0),
                frame=0, fps_num=24, fps_den=1,
                csp="default-src 'none'; style-src 'unsafe-inline'; img-src topos-asset://*",
                url_policy=UrlPolicy(frozenset({"red-tile"})),
                asset_resolver={"red-tile": red_png},
            )
            # Same bound id but NO resolver -> must not reuse the first call's
            # bytes; the img falls back to the deterministic 1x1 transparent
            # placeholder, so the img area shows the white page background.
            second = service.render_frame(
                html, CaptureSurface(100, 100, 1.0),
                frame=0, fps_num=24, fps_den=1,
                csp="default-src 'none'; style-src 'unsafe-inline'; img-src topos-asset://*",
                url_policy=UrlPolicy(frozenset({"red-tile"})),
                asset_resolver=None,
            )
        finally:
            service.close()
        from src.features.teaching_animation.render.html_renderer_spike import CapturedFrame
        assert isinstance(first, CapturedFrame), first
        assert isinstance(second, CapturedFrame), second
        # With the resolver bound the img area is red; the bar is always blue.
        assert _pixel(first.data, 100, 20, 60)[:3] == (255, 0, 0)
        assert _pixel(first.data, 100, 10, 10)[:3] == (0, 102, 204)
        # No leak: with the resolver cleared the img area is white (the
        # placeholder is transparent), NOT red. The bar is still blue, proving
        # the second capture ran on a clean worker rather than failing.
        assert _pixel(second.data, 100, 20, 60)[:3] != (255, 0, 0)
        assert _pixel(second.data, 100, 10, 10)[:3] == (0, 102, 204)

    def test_host_resolves_bound_image_from_asset_root(self, tmp_path):
        """The full host path resolves a bound image to real bytes from disk.

        ``adapter.prepare`` (the main-chain entry) reads the project's declared
        asset under ``asset_root`` and feeds it to the capture resolver, so the
        captured frame shows the real artwork — not the deterministic 1×1
        placeholder.  This is the W2 P2 real-assets gap, closed end to end.
        """
        backend = _backend()
        _skip_if_no_backend(backend)
        from src.features.teaching_animation.core.model import SceneAssetRef
        red_png = _solid_png((255, 0, 0, 255), 32, 32)
        (tmp_path / "logo.png").write_bytes(red_png)
        html = ("<html><head><style>html,body{margin:0;background:#ffffff}"
                "img{position:absolute;left:0;top:0;width:80px;height:80px}"
                "</style></head>"
                "<body><img src='topos-asset://image/logo'></body></html>")
        scene = _html_scene(html=html, viewport_w=160, viewport_h=120,
                            bound_assets=("image/logo",))
        ref = SceneAssetRef(asset_id="image/logo", kind="image", path="logo.png")
        scene = replace(scene, asset_refs=(ref,))
        service = router.get_shared_html_service()
        adapter = HtmlSceneRendererV1Adapter(service)
        ctx = RenderContext(fps=scene.fps, width=160, height=120, asset_root=str(tmp_path))
        adapter.prepare(scene, ctx)
        try:
            frame = adapter.render_frame(AnimationFrame(0, ctx.fps))
        finally:
            adapter.cleanup()
        assert frame.pixel_format == "rgba8"
        # Real red image bytes (resolved from asset_root) reached the page.
        assert _pixel(frame.data, frame.width, 30, 30)[:3] == (255, 0, 0)

    def test_export_renders_html_scene_deterministically(self):
        backend = _backend()
        _skip_if_no_backend(backend)
        html = ("<html><head><style>html,body{margin:0;background:#1034a6}"
                ".r{position:absolute;left:20px;top:20px;width:60px;height:40px;"
                "background:#ff8800}</style></head>"
                "<body><div class='r'></div></body></html>")
        scene = _html_scene(html=html, viewport_w=160, viewport_h=90, duration=4)
        from src.features.teaching_animation.render.render_worker import RenderWorker
        ctx = RenderContext(fps=scene.fps, width=160, height=90)
        # A small sample of frame indices is enough to prove byte-identity across
        # two independent runs; the scene's own duration_frames (240) would make
        # this a 480-capture test for no extra coverage.  (The full-HD multi-frame
        # determinism case lives in test_html_s0_determinism.py.)
        sample = (0, 1, 2, 3, 7)
        frames_a = RenderWorker().render_sync(scene, sample, ctx).frames
        frames_b = RenderWorker().render_sync(scene, sample, ctx).frames
        assert len(frames_a) == len(sample)
        assert all(f.pixel_format == "rgba8" for f in frames_a)
        # Frame-by-frame byte-identical across two independent runs (W0-A determinism).
        assert [f.data for f in frames_a] == [f.data for f in frames_b]


# --------------------------------------------------------------------------- #
# Logic-only (no browser): clip insert + undo restores SVG routing
# --------------------------------------------------------------------------- #
class TestHtmlS0Logic:
    def test_router_selects_svg_after_html_layer_undo(self):
        """SetHtmlLayerCommand undo must restore SVG routing through the router."""
        from src.features.teaching_animation.core.html_layer_commands import (
            SetHtmlLayerCommand,
        )
        service = _StubService()
        scene = TeachingSceneDocument.create_default(name="undo")
        # Build a minimal HTML layer to attach.
        layer = build_html_layer(
            layer_id="u", sanitized_html="<p>x</p>", authored_source="<p>x</p>",
            viewport_width=scene.width, viewport_height=scene.height,
            fps_num=scene.fps_num, fps_den=scene.fps_den,
            duration_frames=max(1, scene.duration_frames),
        )
        cmd = SetHtmlLayerCommand(layer)
        assert not router.scene_has_html_layer(scene)
        scene = cmd.apply(scene)
        assert router.scene_has_html_layer(scene)
        assert isinstance(select_scene_renderer(scene, html_service=service),
                          HtmlSceneRendererV1Adapter)
        scene = cmd.undo(scene)
        assert not router.scene_has_html_layer(scene)
        # After undo the router picks SVG again (no HTML adapter, no backend needed).
        assert isinstance(select_scene_renderer(scene), SvgSceneRenderer)

    def test_router_routes_non_html_scene_to_svg(self):
        scene = TeachingSceneDocument.create_default(name="plain")
        assert isinstance(select_scene_renderer(scene), SvgSceneRenderer)
        assert router.backend_for_scene(scene) == "svg"


class _StubService:
    """Minimal service stub so select_scene_renderer can build an adapter
    without launching a browser for the logic-only undo test."""
    backend_build = "stub-html/0.0"

    def render_frame(self, html, surface, *, frame, fps_num, fps_den, csp, url_policy,
                     asset_resolver=None, timeout_ms=None, cancel_token=None,
                     transparent=False):
        from src.features.teaching_animation.render.html_renderer_spike import (
            CaptureEvidence, CaptureSurface, CapturedFrame,
        )
        data = bytes([9]) * (surface.width * surface.height * 4)
        return CapturedFrame(
            data=data, surface=surface,
            evidence=CaptureEvidence("stub", self.backend_build, "stub",
                                     (surface.width, surface.height), surface.dpr))

    def close(self):
        pass


# --------------------------------------------------------------------------- #
# Logic-only (no browser): bound image asset resolution (W2 P2)
# --------------------------------------------------------------------------- #
class TestResolveBoundImageBytes:
    """The host resolves bound image assets to real file bytes before capture.

    No browser — just the resolution helper that feeds the capture resolver.
    Fonts are skipped (the font lock injects those); missing assets degrade to
    the backend placeholder rather than crashing the render.
    """

    def _scene_with_asset(self, tmp_path, *, asset_id="image/logo", path="logo.png",
                          kind="image", bound=("image/logo",)):
        from src.features.teaching_animation.core.model import SceneAssetRef
        png = _solid_png((255, 0, 0, 255), 32, 32)
        (tmp_path / path).write_bytes(png)
        scene = TeachingSceneDocument.create_default(name="assets")
        layer = build_html_layer(
            layer_id="a", sanitized_html="<p>x</p>", authored_source="<p>x</p>",
            viewport_width=scene.width, viewport_height=scene.height,
            fps_num=scene.fps_num, fps_den=scene.fps_den,
            duration_frames=max(1, scene.duration_frames),
            bound_asset_ids=tuple(bound),
        )
        ref = SceneAssetRef(asset_id=asset_id, kind=kind, path=path)
        return replace(scene, html_layer=layer, asset_refs=(ref,)), png

    def test_resolves_declared_image_to_real_bytes(self, tmp_path):
        from src.features.teaching_animation.render.html_scene_renderer_v2 import (
            resolve_bound_image_bytes,
        )
        scene, png = self._scene_with_asset(tmp_path)
        ctx = RenderContext(fps=scene.fps, width=scene.width, height=scene.height,
                            asset_root=str(tmp_path))
        assert resolve_bound_image_bytes(scene, ctx) == {"image/logo": png}

    def test_empty_when_no_asset_root(self, tmp_path):
        from src.features.teaching_animation.render.html_scene_renderer_v2 import (
            resolve_bound_image_bytes,
        )
        scene, _ = self._scene_with_asset(tmp_path)
        ctx = RenderContext(fps=scene.fps, width=scene.width, height=scene.height)
        assert resolve_bound_image_bytes(scene, ctx) == {}

    def test_skips_non_image_assets(self, tmp_path):
        # A font-kind asset is bound but must be skipped (font lock handles fonts).
        from src.features.teaching_animation.render.html_scene_renderer_v2 import (
            resolve_bound_image_bytes,
        )
        scene, _ = self._scene_with_asset(
            tmp_path, asset_id="font/x", kind="font", bound=("font/x",))
        ctx = RenderContext(fps=scene.fps, width=scene.width, height=scene.height,
                            asset_root=str(tmp_path))
        assert resolve_bound_image_bytes(scene, ctx) == {}

    def test_missing_file_is_best_effort_empty(self, tmp_path):
        # Non-strict: an unresolved asset simply stays absent (backend placeholder),
        # never raising — preflight strictness is the export gate's job, upstream.
        from src.features.teaching_animation.render.html_scene_renderer_v2 import (
            resolve_bound_image_bytes,
        )
        scene, _ = self._scene_with_asset(tmp_path)
        empty_root = tmp_path / "empty"
        empty_root.mkdir()
        ctx = RenderContext(fps=scene.fps, width=scene.width, height=scene.height,
                            asset_root=str(empty_root), strict_assets=False)
        assert resolve_bound_image_bytes(scene, ctx) == {}
