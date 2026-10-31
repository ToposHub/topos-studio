"""Page-aware rendering contracts for full-scene preview and export."""

from __future__ import annotations

from dataclasses import replace

from src.features.teaching_animation.core.contracts import AnimationFrame, RenderContext
from src.features.teaching_animation.core.model import (
    SceneElement, ScenePage, SceneTransform, TeachingSceneDocument,
)
from src.features.teaching_animation.render.evaluator import evaluate
from src.features.teaching_animation.render.svg_renderer import SvgSceneRenderer


def _scene() -> TeachingSceneDocument:
    base = TeachingSceneDocument.create_default(
        "page render", width=320, height=200, duration_frames=48,
    )
    return replace(
        base,
        pages=(ScenePage("p1", duration_frames=24), ScenePage("p2", duration_frames=24)),
        elements=(
            SceneElement(
                "p1-element", "text", transform=SceneTransform(width=100, height=40),
                style={"color": "#fff"}, content="one", page_id="p1",
            ),
            SceneElement(
                "p2-element", "text", transform=SceneTransform(width=100, height=40),
                style={"color": "#fff"}, content="two", page_id="p2",
            ),
        ),
    )


def test_evaluate_renders_only_the_page_owning_the_frame() -> None:
    scene = _scene()
    assert [item.source.element_id for item in evaluate(scene, 0).elements] == ["p1-element"]
    assert [item.source.element_id for item in evaluate(scene, 24).elements] == ["p2-element"]


def test_svg_renderer_uses_the_same_page_filter() -> None:
    scene = _scene()
    renderer = SvgSceneRenderer()
    renderer.prepare(scene, RenderContext(fps=scene.fps, width=scene.width, height=scene.height))
    try:
        first = renderer.render_frame(AnimationFrame(0, scene.fps)).data.decode("utf-8")
        second = renderer.render_frame(AnimationFrame(24, scene.fps)).data.decode("utf-8")
    finally:
        renderer.cleanup()
    assert 'data-element-id="p1-element"' in first
    assert 'data-element-id="p2-element"' not in first
    assert 'data-element-id="p2-element"' in second
    assert 'data-element-id="p1-element"' not in second
