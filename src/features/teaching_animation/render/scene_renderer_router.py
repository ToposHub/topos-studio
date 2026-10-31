"""P0: the single renderer router for the host main chain.

Before this module the host's preview / edit-composite / export paths each
hard-coded ``SvgSceneRenderer``, which silently ignores ``scene.html_layer`` —
so an HTML scene rendered through the main chain lost its HTML content (the
P0 gap in the W2 audit).  This module is the single decision point:

* a scene whose ``html_layer`` is set is an **HTML scene** and is rendered by
  :class:`HtmlSceneRendererV1Adapter`, which wraps the frozen V2
  :class:`~src.features.teaching_animation.render.html_scene_renderer_v2.HtmlSceneRendererV2`
  behind the legacy V1 :class:`~src.features.teaching_animation.core.contracts.SceneRenderer`
  Protocol the host still speaks;
* every other scene is rendered by :class:`SvgSceneRenderer` exactly as before.

The V1 adapter bridges ``RenderContext`` / ``AnimationFrame`` (V1) to
``FrameContext`` (V2) so the host main chain does not have to migrate to V2 to
gain HTML rendering.  Failure is fail-closed: an HTML scene whose pinned
Chromium backend is unavailable raises a structured ``HtmlRenderError`` rather
than silently degrading to SVG (which would drop the HTML content and show a
blank-ish frame).  Non-HTML scenes are entirely unaffected.
"""

from __future__ import annotations

import re
import threading
from dataclasses import replace
from typing import Any, Mapping

from ..core.contracts import (
    AlphaMode,
    CanvasSpace,
    FrameContext,
    RenderContext,
    RenderErrorCode,
    RenderError,
    RenderSurface,
)
from .html_renderer_service import HtmlRendererService
from .html_renderer_spike import CancelToken, FrameCaptureBackend
from .html_renderer_spike.playwright_backend import PlaywrightChromiumBackend
from .html_scene_renderer_v2 import HtmlRenderError, HtmlSceneRendererV2
from .scene_renderer import CancellationToken, RenderCancelled
from .svg_renderer import SvgSceneRenderer


# --------------------------------------------------------------------------- #
# Shared lazy HTML renderer service (one pinned-browser pool per process).
# --------------------------------------------------------------------------- #
_shared_lock = threading.Lock()
_shared_service: HtmlRendererService | None = None


def make_playwright_backend_factory(
    *,
    allow_no_sandbox: bool = False,
    headless: bool = True,
    pool_asset_resolver: Mapping[str, bytes] | None = None,
) -> "Any":
    """Return a factory building pinned Chromium capture backends for the pool.

    ``allow_no_sandbox`` is off by default (untrusted model HTML) and exists
    only for CI/root containers that cannot run the setuid sandbox.
    """

    def factory() -> FrameCaptureBackend:
        return PlaywrightChromiumBackend(
            headless=headless,
            asset_resolver=pool_asset_resolver,
            allow_no_sandbox=allow_no_sandbox,
        )

    return factory


def get_shared_html_service() -> HtmlRendererService:
    """Return the process-wide pinned-browser pool, building it lazily.

    Construction never imports Playwright (that happens at capture time), so
    this always returns a service; backend unavailability surfaces as a
    structured ``UNSUPPORTED`` failure on the first capture, which the router
    maps to ``UNSUPPORTED_BACKEND``.
    """
    global _shared_service
    with _shared_lock:
        if _shared_service is None:
            _shared_service = HtmlRendererService(
                make_playwright_backend_factory(), pool_size=1)
        return _shared_service


def reset_shared_html_service() -> None:
    """Drop and close the shared service (test isolation / shutdown)."""
    global _shared_service
    with _shared_lock:
        svc = _shared_service
        _shared_service = None
    if svc is not None:
        svc.close()


def scene_has_html_layer(scene: Any) -> bool:
    """True when a scene document (or its JSON dict) carries an html_layer."""
    layer = getattr(scene, "html_layer", None)
    if layer is not None:
        return True
    if isinstance(scene, Mapping):
        return bool(scene.get("html_layer"))
    return False


# --------------------------------------------------------------------------- #
# V1 ← V2 adapter
# --------------------------------------------------------------------------- #
class HtmlSceneRendererV1Adapter:
    """V1 ``SceneRenderer`` Protocol adapter wrapping the V2 HTML renderer.

    The host main chain speaks the legacy V1 contract
    (``prepare(scene, RenderContext)`` / ``render_frame(AnimationFrame)`` /
    ``cleanup()``).  This adapter translates each call into the frozen V2
    contract that :class:`HtmlSceneRendererV2` requires, building a
    ``FrameContext`` at DPR 1 (so the V1 main chain renders HTML at design
    resolution, matching SVG) and threading the V1 transparency intent through
    to the capture's alpha mode.

    Cancellation follows V1 semantics (checked per frame, between captures);
    the export worker already polls its token between frames.  A V1
    ``CancellationToken`` that is already cancelled raises
    :class:`RenderCancelled` before the capture starts.
    """

    def __init__(
        self,
        service: HtmlRendererService,
        *,
        cancellation: CancellationToken | None = None,
    ) -> None:
        self._v2 = HtmlSceneRendererV2(service)
        self._cancellation = cancellation
        self._session: Any = None
        self._context: RenderContext | None = None
        self._fps_num = 24
        self._fps_den = 1
        # Keep the legacy V1 cancellation source linked to the V2/browser
        # token.  The editor's QThread can request interruption while a
        # Playwright capture is in flight; without this bridge the V1 adapter
        # would only notice cancellation before the next frame.
        self._html_cancel_token = _LinkedCancelToken(cancellation)

    @property
    def renderer_id(self) -> str:
        return self._v2.renderer_id

    @property
    def renderer_build(self) -> str:
        return self._v2.renderer_build

    # ------------------------------------------------------------------ #
    # V1 SceneRenderer Protocol
    # ------------------------------------------------------------------ #
    def prepare(self, scene: Any, context: RenderContext) -> None:
        self._context = context
        # Prefer the scene's rational fps (the source of truth the seek bridge
        # uses) over the float fps on the V1 context; export asserts they match.
        self._fps_num = int(getattr(scene, "fps_num", max(1, round(context.fps))))
        self._fps_den = int(getattr(scene, "fps_den", 1))
        # Resolve the layer's bound image assets to real bytes from the render
        # root, so captured frames show the project's actual artwork instead of
        # the deterministic 1×1 placeholder (W2 P2 real-assets).  Fonts are
        # injected separately by the font lock at render time.
        from .html_scene_renderer_v2 import resolve_bound_image_bytes
        asset_bytes = resolve_bound_image_bytes(scene, context)
        self._session = self._v2.prepare(
            scene, self._frame_context(0), asset_bytes=asset_bytes)

    def render_frame(self, frame: Any) -> Any:

        if self._session is None:
            raise RuntimeError("HtmlSceneRendererV1Adapter.render_frame before prepare")
        if self._cancellation is not None and self._cancellation.is_cancelled:
            raise RenderCancelled("HTML render cancelled before frame")
        result = self._v2.render(
            self._session,
            self._frame_context(int(frame.frame)),
            cancel_token=self._html_cancel_token,
        )
        # RendererResult.frame is already a RenderedFrame (pixel_format rgba8).
        return result.frame

    def cleanup(self) -> None:
        session = self._session
        self._session = None
        if session is not None:
            self._v2.close(session)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _frame_context(self, frame_number: int) -> FrameContext:
        assert self._context is not None
        ctx = self._context
        transparent = bool(getattr(ctx, "transparent", False))
        canvas = CanvasSpace.from_dimensions(ctx.width, ctx.height)
        surface = RenderSurface(
            pixel_width=int(ctx.width),
            pixel_height=int(ctx.height),
            dpr=1.0,
            alpha_mode=AlphaMode.PREMULT if transparent else AlphaMode.STRAIGHT,
        )
        return FrameContext(
            frame=max(0, int(frame_number)),
            fps_num=self._fps_num,
            fps_den=self._fps_den,
            canvas=canvas,
            surface=surface,
            html_validation_background=getattr(ctx, "html_validation_background", None),
            allow_uniform_frames=bool(getattr(ctx, "allow_uniform_frames", False)),
        )


class HtmlVectorSceneRendererV1Adapter(HtmlSceneRendererV1Adapter):
    """Render HTML backgrounds and native layers as one browser DOM document."""

    def __init__(
        self,
        service: HtmlRendererService,
        *,
        cancellation: CancellationToken | None = None,
    ) -> None:
        super().__init__(service, cancellation=cancellation)
        self._overlay_scene: Any = None
        self._overlay_hierarchy: dict[str, str] = {}
        self._overlay_tokens: Any = None
        self._overlay_asset_lookup: Any = None

    @property
    def renderer_id(self) -> str:
        return f"{super().renderer_id}+dom-overlay"

    @property
    def renderer_build(self) -> str:
        return f"{super().renderer_build}+dom-overlay-v1"

    def prepare(self, scene: Any, context: RenderContext) -> None:
        super().prepare(scene, context)
        self._overlay_scene = scene
        self._overlay_hierarchy: dict[str, str] = {}
        self._overlay_asset_lookup = None
        try:
            from .html_native_overlay import scene_asset_uri_lookup
            self._overlay_asset_lookup = scene_asset_uri_lookup(scene, context)
        except Exception:  # noqa: BLE001 — unresolved assets degrade to placeholders
            self._overlay_asset_lookup = None
        metadata = getattr(scene, "metadata", None)
        if isinstance(metadata, Mapping):
            from ..core.hierarchy import read_hierarchy
            self._overlay_hierarchy = dict(read_hierarchy(metadata).parents)
        from ..core.design_tokens import read_scene_tokens
        self._overlay_tokens = read_scene_tokens(metadata if isinstance(metadata, Mapping) else {})

    def _native_overlay_for_frame(self, frame: int) -> str:
        """Materialize the native overlay at one exact frame.

        The capture path evaluates keyframes, hierarchy world transforms and
        token resolution through the same ``evaluate`` the SVG renderer uses,
        then flattens the result into the overlay markup.  Every captured
        frame therefore shares the live preview's DOM semantics while
        reflecting the animated values of that specific frame.
        """
        from dataclasses import replace as dc_replace
        from ..render.evaluator import evaluate
        from .html_native_overlay import native_overlay_css_variables, native_overlay_markup
        scene = self._overlay_scene
        elements: list[Any]
        if isinstance(scene, Mapping) or not hasattr(scene, "animations"):
            # Plain mapping scenes (facade/test fixtures) have no evaluate()
            # contract; materialize their raw element list as before.
            elements = list(getattr(scene, "elements", ()) or [])
        else:
            evaluated = evaluate(scene, int(frame))
            elements = [
                dc_replace(
                    item.source,
                    transform=item.transform,
                    style={**dict(item.style), "opacity": float(item.opacity)},
                )
                for item in evaluated.elements
            ]
        tokens = self._overlay_tokens
        token_css = native_overlay_css_variables(tokens)
        return (
            (f"<style>{token_css}</style>" if token_css else "")
            + native_overlay_markup(elements, self._overlay_hierarchy, tokens, asset_lookup=self._overlay_asset_lookup)
        )

    def render_frame(self, frame: Any) -> Any:
        if self._session is None:
            raise RuntimeError("HtmlVectorSceneRendererV1Adapter.render_frame before prepare")
        if self._cancellation is not None and self._cancellation.is_cancelled:
            raise RenderCancelled("HTML/vector render cancelled before frame")
        # Text is emitted as HTML/CSS and geometry as inline SVG.  Browser
        # capture therefore shares the same DOM representation as the live
        # webpage editor, rather than compositing a frozen raster frame.
        session = replace(
            self._session,
            html=_html_with_native_overlay(self._session.html, self._native_overlay_for_frame(int(frame.frame))),
        )
        result = self._v2.render(
            session,
            self._frame_context(int(frame.frame)),
            cancel_token=self._html_cancel_token,
        )
        from ..core.contracts import RenderedFrame
        metadata = dict(result.frame.metadata or {})
        metadata["native_vector_overlay"] = True
        return RenderedFrame(
            frame=result.frame.frame, width=result.frame.width, height=result.frame.height,
            pixel_format=result.frame.pixel_format, data=result.frame.data,
            backend="html+dom-overlay", metadata=metadata,
        )

    def cleanup(self) -> None:
        self._overlay_scene = None
        super().cleanup()


#: AUD-30311: same class of bug as _append_validation_background — indices
#: taken from ``document.lower()`` slice the original document at the wrong
#: offset because ``str.lower()`` changes length for characters like ``'İ'``
#: (U+0130), which would splice the overlay inside a tag on this per-frame
#: main render path.  Match on the original document with re.IGNORECASE so
#: every index is a valid index into the original string.
_CLOSING_BODY_RE = re.compile(r"</body\s*>", re.IGNORECASE)


def _html_with_native_overlay(document: str, markup: str) -> str:
    """Add trusted HTML/CSS/SVG native MG layers above authored HTML."""
    overlay = (
        '<div id="topos-native-vector-overlay" '
        'style="position:fixed;inset:0;width:100%;height:100%;pointer-events:none;z-index:2147483647;">'
        + markup + "</div>"
    )
    closing = None
    for closing in _CLOSING_BODY_RE.finditer(document):
        pass  # last occurrence, matching the historical ``rfind`` semantics
    if closing is not None:
        closing_body = closing.start()
        return document[:closing_body] + overlay + document[closing_body:]
    return document + overlay


class _LinkedCancelToken(CancelToken):
    """A browser token that also observes the V1 render cancellation token."""

    def __init__(self, source: CancellationToken | None) -> None:
        super().__init__()
        self._source = source

    @property
    def is_cancelled(self) -> bool:
        return super().is_cancelled or bool(
            self._source is not None and self._source.is_cancelled)


# --------------------------------------------------------------------------- #
# The router
# --------------------------------------------------------------------------- #
def select_scene_renderer(
    scene: Any,
    *,
    cancellation: CancellationToken | None = None,
    html_service: HtmlRendererService | None = None,
) -> Any:
    """Return a V1 ``SceneRenderer`` for ``scene``: HTML adapter or SVG.

    Callers drive it identically to the old ``SvgSceneRenderer()`` literal::

        renderer = select_scene_renderer(scene, cancellation=token)
        renderer.prepare(scene, context)
        try:
            frame = renderer.render_frame(AnimationFrame(n, context.fps))
        finally:
            renderer.cleanup()

    An HTML scene whose pinned Chromium backend is unavailable raises
    :class:`HtmlRenderError` (``UNSUPPORTED_BACKEND``) rather than silently
    falling back to SVG, which would render the scene without its HTML content.
    """
    if scene_has_html_layer(scene):
        service = html_service if html_service is not None else get_shared_html_service()
        if service is None:
            raise HtmlRenderError(RenderError(
                RenderErrorCode.UNSUPPORTED_BACKEND,
                "HTML scene requires the pinned Chromium backend (Playwright), "
                "which is not available in this environment",
                fallback_available=True,
            ))
        if _has_native_vector_overlays(scene):
            return HtmlVectorSceneRendererV1Adapter(service, cancellation=cancellation)
        return HtmlSceneRendererV1Adapter(service, cancellation=cancellation)
    return SvgSceneRenderer(cancellation)


def _has_native_vector_overlays(scene: Any) -> bool:
    """Whether an HTML scene contains visible native elements to composite."""
    elements = getattr(scene, "elements", ())
    if isinstance(scene, Mapping):
        elements = scene.get("elements", ())
    if not isinstance(elements, (tuple, list)):
        return False
    for element in elements:
        if isinstance(element, Mapping):
            if bool(element.get("visible", True)):
                return True
        elif bool(getattr(element, "visible", True)):
            return True
    return False


def backend_for_scene(scene: Any) -> str:
    """The V1 ``RenderContext.backend`` value matching the chosen renderer.

    Keeps cache manifests and export metadata honest: an HTML scene's cache
    key uses ``backend='html'`` so its rgba8 frames never collide with the
    SVG scene's svg frames for the same scene id.
    """
    return "html" if scene_has_html_layer(scene) else "svg"


__all__ = [
    "HtmlSceneRendererV1Adapter",
    "HtmlVectorSceneRendererV1Adapter",
    "backend_for_scene",
    "get_shared_html_service",
    "make_playwright_backend_factory",
    "reset_shared_html_service",
    "scene_has_html_layer",
    "select_scene_renderer",
]
