"""HTML preview adapter using the exact SVG bytes used by export."""

from __future__ import annotations

from ..core.contracts import AnimationFrame, RenderContext, RenderedFrame, SceneJson
from .svg_renderer import SvgSceneRenderer


class HtmlPreviewRenderer:
    """Wrap a deterministic SVG frame in a browser-safe HTML document."""

    def __init__(self) -> None:
        self._renderer = SvgSceneRenderer()
        self._context: RenderContext | None = None

    def prepare(self, scene: SceneJson, context: RenderContext) -> None:
        self._context = context
        self._renderer.prepare(scene, context)

    def render_frame(self, frame: AnimationFrame) -> RenderedFrame:
        return self._renderer.render_frame(frame)

    def render_html(self, frame: AnimationFrame) -> str:
        rendered = self.render_frame(frame)
        return (
            "<!doctype html><html><head><meta charset=\"utf-8\"><style>"
            "html,body{margin:0;width:100%;height:100%;overflow:hidden;background:#111827}"
            "svg{display:block;width:100%;height:100%}"
            "</style></head><body>"
            + rendered.data.decode("utf-8")
            + "</body></html>"
        )

    def cleanup(self) -> None:
        self._renderer.cleanup()
        self._context = None

