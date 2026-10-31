"""S0 full determinism acceptance for the HTML motion layer (W0-A/W2 gate).

Closes the determinism promise with real Chromium evidence at production scale:
1920x1080, out-of-order seeks, same-frame repetition, font-lock effectiveness,
and a same-build perceptual baseline.  The earlier S0 tests (160x90, in-order,
two runs) did not exercise font locking, out-of-order seeking, or full-HD scale.

Marked ``html_renderer`` (CI gate installs the pinned Chromium).  All cases
skip-with-reason when the backend is absent.

Determinism contract (determinism.py): same renderer_build + viewport + DPR +
font set + asset hash MUST produce identical RGBA.  Here we assert it directly:
(1) same-frame repeats are byte-identical, (2) shuffled seek order matches
ordered, (3) the font lock actually changes glyph pixels (so the contract's
"font set" term is load-bearing), and (4) same-build perceptual diff is zero.
"""

from __future__ import annotations

import random
from dataclasses import replace

import pytest

from src.features.teaching_animation.core.contracts import (
    AlphaMode, CanvasSpace, FrameContext, RenderContext, RenderSurface,
)
from src.features.teaching_animation.core.html_layer import build_html_layer
from src.features.teaching_animation.core.model import TeachingSceneDocument
from src.features.teaching_animation.render import scene_renderer_router as router
from src.features.teaching_animation.render.consistency import (
    ComparisonMode, FrameSnapshot, compare_frames,
)
from src.features.teaching_animation.render.html_renderer_spike import is_uniform_frame


W, H, FPS, DURATION = 1920, 1080, 24, 240          # 10s @ 24fps, full HD
KEY_FRAMES = (0, 60, 120, 180, 239)                # start / q1 / mid / q3 / end


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


def _animated_1080p_scene() -> TeachingSceneDocument:
    """An HTML scene with a CSS keyframe so frames genuinely differ over time."""
    html = (
        "<!DOCTYPE html><html><head><style>"
        "html,body{margin:0;background:#0a1128;width:1920px;height:1080px}"
        ".bar{position:absolute;top:200px;left:0;width:600px;height:120px;"
        "background:#00e676;"
        "animation:slide 10s linear infinite}"
        ".title{position:absolute;top:520px;left:160px;color:#fff;font-size:160px;"
        "font-weight:bold;letter-spacing:12px}"
        "@keyframes slide{0%{left:0}100%{left:1320px}}"
        "</style></head>"
        "<body><div class='bar'></div>"
        "<div class='title' data-topos-node-id='t'>TOPOS</div></body></html>"
    )
    scene = TeachingSceneDocument.create_default(name="S0 1080p")
    scene = replace(scene, fps_num=FPS, fps_den=1)
    layer = build_html_layer(
        layer_id="s0-1080p", sanitized_html=html, authored_source=html,
        viewport_width=W, viewport_height=H, fps_num=FPS, fps_den=1,
        duration_frames=DURATION,
        csp="default-src 'none'; style-src 'unsafe-inline'",
    )
    return replace(scene, html_layer=layer)


def _fc(frame: int) -> FrameContext:
    return FrameContext(
        frame=frame, fps_num=FPS, fps_den=1,
        canvas=CanvasSpace.from_dimensions(W, H),
        surface=RenderSurface(pixel_width=W, pixel_height=H, dpr=1.0,
                              alpha_mode=AlphaMode.STRAIGHT),
    )


@pytest.mark.html_renderer
class TestHtmlS0Determinism:
    def test_1080p_out_of_order_and_repetition_identical(self):
        backend = _backend()
        _skip_if_no_backend(backend)
        from src.features.teaching_animation.render.html_scene_renderer_v2 import (
            HtmlSceneRendererV2,
        )
        scene = _animated_1080p_scene()
        renderer = HtmlSceneRendererV2(router.get_shared_html_service())
        session = renderer.prepare(scene, _fc(0))

        def _render(frame: int) -> bytes:
            res = renderer.render(session, _fc(frame))
            # Defence in depth: a uniform frame here would silently pass
            # determinism while showing nothing.
            uniform, _ = is_uniform_frame(res.frame.data, W, H)
            assert not uniform, f"frame {frame} rendered uniform (blank)"
            return res.frame.data

        try:
            # 1) Same-frame repetition (5x) is byte-identical.
            for f in (KEY_FRAMES[0], KEY_FRAMES[-1]):
                samples = [_render(f) for _ in range(5)]
                assert all(s == samples[0] for s in samples), \
                    f"frame {f}: 5x repetition not byte-identical"

            # 2) Out-of-order: ordered captures vs seeded-shuffle, compared as
            #    content tuples keyed by frame, must match exactly.
            ordered = {f: _render(f) for f in KEY_FRAMES}
            shuffled_frames = list(KEY_FRAMES)
            random.Random(0xC0FFEE).shuffle(shuffled_frames)
            shuffled = {f: _render(f) for f in shuffled_frames}
            assert set(ordered) == set(shuffled)
            for f in KEY_FRAMES:
                assert ordered[f] == shuffled[f], \
                    f"frame {f}: differs when seeked out of order"

            # 3) Key frames are NOT all identical (the animation advances), so
            #    the determinism check above is meaningful and not trivially
            #    comparing a static page.
            distinct = {ordered[f] for f in KEY_FRAMES}
            assert len(distinct) > 1, "all key frames identical — animation did not advance"
        finally:
            renderer.close(session)

    def test_font_lock_loads_dejavu_not_system_fallback(self):
        """The font lock's @font-face actually loads the packaged DejaVu bytes.

        Compares a font-locked render against one that forces the browser's own
        sans-serif — the exact family ``__topos_sans`` would fall back to if the
        @font-face never loaded.  Distinct pixels prove ``__topos_sans`` resolved
        to DejaVu, not the system fallback (the cross-machine determinism
        guarantee).  The lock inlines fonts as base64 data: URIs (Chromium won't
        fetch @font-face from topos-asset://) and the CSP allows ``font-src data:``.
        """
        backend = _backend()
        _skip_if_no_backend(backend)
        from src.features.teaching_animation.render.html_renderer_service import (
            HtmlRendererService,
        )
        from src.features.teaching_animation.render.html_renderer_spike import (
            CaptureSurface, UrlPolicy,
        )
        from src.features.teaching_animation.render.html_renderer_spike.playwright_backend import (
            PlaywrightChromiumBackend,
        )
        from src.features.teaching_animation.core.html_fonts import wrap_with_font_lock
        body = ("<!DOCTYPE html><html><head></head><body style='margin:0;background:#fff'>"
                "<div style='font-size:180px;color:#000;line-height:1;font-weight:bold'>"
                "MMMMAGHI</div></body></html>")
        # Same text, but the div explicitly uses the browser's sans-serif — the
        # family __topos_sans would resolve to if @font-face failed to load.
        sans_body = body.replace("font-weight:bold'>",
                                 "font-weight:bold;font-family:sans-serif'>")
        surface = CaptureSurface(800, 240, 1.0)
        csp = "default-src 'none'; style-src 'unsafe-inline'; font-src data:"
        svc = HtmlRendererService(lambda: PlaywrightChromiumBackend(), pool_size=1)
        try:
            locked = svc.render_frame(
                wrap_with_font_lock(body), surface, frame=0, fps_num=FPS, fps_den=1,
                csp=csp, url_policy=UrlPolicy(frozenset()), asset_resolver=None)
            sans = svc.render_frame(
                sans_body, surface, frame=0, fps_num=FPS, fps_den=1,
                csp=csp, url_policy=UrlPolicy(frozenset()), asset_resolver=None)
        finally:
            svc.close()
        lu, _ = is_uniform_frame(locked.data, 800, 240)
        su, _ = is_uniform_frame(sans.data, 800, 240)
        assert not lu and not su, "a capture came back uniform (blank)"
        # Distinct pixels: __topos_sans resolved to DejaVu, not the sans-serif
        # fallback.  If these matched, the @font-face did not load.
        assert locked.data != sans.data, \
            "font-locked capture matches system sans-serif — DejaVu @font-face did not load"

    def test_font_lock_loads_under_layer_csp_without_font_src(self):
        """The renderer's render-time CSP merge lets the font lock load even when
        the layer's authored CSP omits ``font-src`` / ``data:``.

        A layer CSP without font-src would block the lock's data: @font-face and
        fall back to a system sans-serif.  The renderer merges ``font-src data:``
        in at render time, so a V2 render of such a layer still loads DejaVu —
        proven by differing from an explicit system-sans-serif reference.
        """
        backend = _backend()
        _skip_if_no_backend(backend)
        from src.features.teaching_animation.render.html_scene_renderer_v2 import (
            HtmlSceneRendererV2,
        )
        from src.features.teaching_animation.render.html_renderer_service import (
            HtmlRendererService,
        )
        from src.features.teaching_animation.render.html_renderer_spike import (
            CapturedFrame, CaptureSurface, UrlPolicy,
        )
        from src.features.teaching_animation.render.html_renderer_spike.playwright_backend import (
            PlaywrightChromiumBackend,
        )
        w, h = 720, 220
        body = ("<!DOCTYPE html><html><head></head><body style='margin:0;background:#fff'>"
                "<div style='font-size:150px;color:#000;line-height:1;font-weight:bold'>"
                "MMAGHI</div></body></html>")
        sans_body = body.replace("font-weight:bold'>",
                                 "font-weight:bold;font-family:sans-serif'>")
        # Layer CSP deliberately omits font-src / data:.
        scene = replace(
            TeachingSceneDocument.create_default(name="nf"),
            fps_num=FPS, fps_den=1,
            html_layer=build_html_layer(
                layer_id="nf", sanitized_html=body, authored_source=body,
                viewport_width=w, viewport_height=h, fps_num=FPS, fps_den=1,
                duration_frames=2, csp="default-src 'none'; style-src 'unsafe-inline'"))
        fc = FrameContext(
            frame=0, fps_num=FPS, fps_den=1,
            canvas=CanvasSpace.from_dimensions(w, h),
            surface=RenderSurface(pixel_width=w, pixel_height=h, dpr=1.0,
                                  alpha_mode=AlphaMode.STRAIGHT))
        renderer = HtmlSceneRendererV2(router.get_shared_html_service())
        session = renderer.prepare(scene, fc)
        try:
            v2 = renderer.render(session, fc).frame
        finally:
            renderer.close(session)
        # Reference: the same text in the browser's own sans-serif (the fallback
        # family), with no font lock.
        svc = HtmlRendererService(lambda: PlaywrightChromiumBackend(), pool_size=1)
        try:
            sans = svc.render_frame(
                sans_body, CaptureSurface(w, h, 1.0), frame=0, fps_num=FPS, fps_den=1,
                csp="default-src 'none'; style-src 'unsafe-inline'",
                url_policy=UrlPolicy(frozenset()), asset_resolver=None)
        finally:
            svc.close()
        assert isinstance(sans, CapturedFrame)
        vu, _ = is_uniform_frame(v2.data, w, h)
        assert not vu, "V2 capture came back uniform (blank)"
        # V2 loaded DejaVu (via the merged font-src data:), so it differs from
        # the system sans-serif reference.  A match would mean the @font-face was
        # blocked by the font-src-less layer CSP and fell back.
        assert v2.data != sans.data, \
            "V2 render matches system sans-serif — font lock did not load under a font-src-less layer CSP"

    def test_same_build_perceptual_diff_is_zero(self):
        """Baseline for future cross-build perceptual tolerance: two captures
        of the same frame on the same pinned build must be perceptually
        identical (mean_abs_diff == 0), establishing the noise floor."""
        backend = _backend()
        _skip_if_no_backend(backend)
        from src.features.teaching_animation.render.html_scene_renderer_v2 import (
            HtmlSceneRendererV2,
        )
        scene = _animated_1080p_scene()
        renderer = HtmlSceneRendererV2(router.get_shared_html_service())
        session = renderer.prepare(scene, _fc(120))
        try:
            a = renderer.render(session, _fc(120)).frame
            b = renderer.render(session, _fc(120)).frame
        finally:
            renderer.close(session)
        sa = FrameSnapshot(a.width, a.height, a.data, renderer_build=renderer.renderer_build)
        sb = FrameSnapshot(b.width, b.height, b.data, renderer_build=renderer.renderer_build)
        result = compare_frames(sa, sb, mode=ComparisonMode.PERCEPTUAL,
                                perceptual_tolerance=0.5, alpha_tolerance=0)
        assert result.passed, [i.code for i in result.issues]
        assert result.max_abs_diff == 0.0, \
            f"same-build perceptual noise floor is non-zero: {result.max_abs_diff}"

    def test_sustained_render_soak_survives_and_caches(self):
        """L5 soak (mandated at W2 by §7.1.1): a sustained multi-frame render
        through the full main chain (worker -> adapter -> service -> pinned
        backend) must not crash, exhaust the backend, or lose determinism, and
        the worker's frame cache must reuse frames on a second pass."""
        backend = _backend()
        _skip_if_no_backend(backend)
        from src.features.teaching_animation.render.cache import SceneRenderCache
        from src.features.teaching_animation.render.render_worker import RenderWorker
        w, h, n = 320, 180, 30
        html = ("<html><head><style>html,body{margin:0;background:#0a1128}"
                ".bar{position:absolute;top:30px;left:0;width:120px;height:50px;"
                "background:#00e676;animation:m 3s linear infinite}"
                ".t{position:absolute;top:100px;left:20px;color:#fff;font-size:44px;"
                "font-weight:bold}"
                "@keyframes m{0%{left:0}100%{left:180px}}"
                "</style></head><body><div class='bar'></div>"
                "<div class='t'>SOAK</div></body></html>")
        scene = replace(
            TeachingSceneDocument.create_default(name="soak"),
            fps_num=FPS, fps_den=1, duration_frames=n, width=w, height=h,
            html_layer=build_html_layer(
                layer_id="soak", sanitized_html=html, authored_source=html,
                viewport_width=w, viewport_height=h, fps_num=FPS, fps_den=1,
                duration_frames=n, csp="default-src 'none'; style-src 'unsafe-inline'"))
        ctx = RenderContext(fps=scene.fps, width=w, height=h)
        cache = SceneRenderCache()
        worker = RenderWorker(cache=cache)
        frames_n = tuple(range(n))
        try:
            first = worker.render_sync(scene, frames_n, ctx).frames
            # Second pass: identical scene -> every frame is a cache hit (no new
            # captures), proving the sustained load left the chain healthy.
            second = worker.render_sync(scene, frames_n, ctx).frames
        finally:
            worker.shutdown()
        assert len(first) == n and all(f.pixel_format == "rgba8" for f in first)
        # Every frame non-blank (the animation + text render throughout).
        for f in first:
            uniform, _ = is_uniform_frame(f.data, w, h)
            assert not uniform, "a soak frame came back uniform (blank)"
        # Animation advances (frames are not all identical -> soak is meaningful).
        assert len({f.data for f in first}) > 1, "all soak frames identical — animation did not advance"
        # Cache reused the sustained pass: second pass is byte-identical and the
        # cache recorded hits for every frame.
        assert [f.data for f in first] == [f.data for f in second]
        assert cache.stats()["hits"] >= n, cache.stats()
