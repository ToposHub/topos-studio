from src.features.teaching_animation.core.contracts import AnimationFrame, RenderContext
from src.features.teaching_animation.core.model import SceneCue, SceneElement, SceneTransform, TeachingSceneDocument
from src.features.teaching_animation.render.advanced import (
    ChartData,
    ModernGLEffectRegistry,
    WebGLPreviewRenderer,
    formula_to_plain_text,
    resolve_localized_text,
)
from src.features.teaching_animation.render.svg_renderer import SvgSceneRenderer


def _advanced_scene() -> TeachingSceneDocument:
    return TeachingSceneDocument(
        scene_id="advanced",
        schema_version=1,
        name="Advanced",
        width=240,
        height=160,
        fps_num=24,
        fps_den=1,
        duration_frames=24,
        background={"color": "#111827"},
        elements=(
            SceneElement("localized", "text", transform=SceneTransform(width=100, height=30), content={"locales": {"en-US": "Hello", "zh-CN": "你好"}}),
            SceneElement("formula", "formula", transform=SceneTransform(y=30, width=100, height=30), content=r"\frac{a}{b}\times c"),
            SceneElement("chart", "chart", transform=SceneTransform(x=10, y=70, width=100, height=60), content={"labels": ["A", "B"], "values": [2, 4], "chart_type": "bar"}),
            SceneElement("draw", "draw", transform=SceneTransform(x=120, y=70, width=100, height=60), content={"points": [[0, 0], [50, 40], [100, 10]]}),
        ),
        animations=(),
        cues=(SceneCue("narration-1", 4, 8, "Explain chart", "narration"),),
    )


def test_chart_csv_json_and_localized_formula_helpers_are_offline() -> None:
    chart = ChartData.from_csv("label,value\nA,2\nB,4\n")
    assert chart.values == (2.0, 4.0)
    assert ChartData.from_json('{"labels":["A"],"values":[1]}').labels == ("A",)
    assert resolve_localized_text({"locales": {"zh-CN": "你好", "en-US": "Hello"}}, "zh-CN") == "你好"
    assert formula_to_plain_text(r"\frac{a}{b}\times c") == "(a)/b× c"


def test_svg_renderer_renders_chart_draw_formula_and_locale() -> None:
    scene = _advanced_scene()
    renderer = SvgSceneRenderer()
    renderer.prepare(scene, RenderContext(fps=24, width=240, height=160, locale="zh-CN"))
    data = renderer.render_frame(AnimationFrame(0, 24)).data.decode()
    renderer.cleanup()
    assert "你好" in data
    assert "polygon" not in data  # chart/draw paths remain inert SVG primitives
    assert "polyline" in data
    assert "rect" in data


def test_cues_round_trip_and_advanced_backend_fallback_is_explicit() -> None:
    scene = _advanced_scene()
    reopened = TeachingSceneDocument.from_json(scene.to_json())
    assert reopened.cues[0].end_frame == 12
    assert reopened.active_cues(5)[0].label == "Explain chart"
    webgl = WebGLPreviewRenderer(webgl_available=False)
    assert webgl.backend_for_preview() == "svg-cpu"
    assert webgl.fallback_used
    effects = ModernGLEffectRegistry()
    effects.register("glow", parameters=("radius", "intensity"))
    assert effects.list_effects()[0]["effect_id"] == "glow"
