"""Focused coverage for Vector-prototype geometry migrated into MG scenes."""

from src.features.teaching_animation.core.contracts import AnimationFrame, RenderContext
from src.features.teaching_animation.core.model import SceneElement, SceneTransform, TeachingSceneDocument
from src.features.teaching_animation.core.vector_shapes import (
    path_geometry_from_scene_points,
    path_points_in_scene,
    regular_polygon_points,
    sanitize_path_data,
    star_points,
)
from src.features.teaching_animation.render.svg_renderer import SvgSceneRenderer
from src.features.teaching_animation.render.html_native_overlay import native_overlay_markup


def _scene() -> TeachingSceneDocument:
    return TeachingSceneDocument(
        scene_id="vector-shapes", schema_version=3, name="Vector Shapes",
        width=640, height=360, fps_num=24, fps_den=1, duration_frames=24,
        background={"color": "#111827"}, animations=(),
        elements=(
            SceneElement("triangle", "shape", transform=SceneTransform(x=10, y=10, width=100, height=80),
                         content={"kind": "triangle", "fill": "#ff0000"}),
            SceneElement("polygon", "shape", transform=SceneTransform(x=140, y=10, width=100, height=80),
                         content={"kind": "polygon", "sides": 7, "fill": "#00ff00"}),
            SceneElement("star", "shape", transform=SceneTransform(x=270, y=10, width=100, height=80),
                         content={"kind": "star", "points": 5, "inner_ratio": 0.4, "fill": "#0000ff"}),
            SceneElement("path", "shape", transform=SceneTransform(x=400, y=10, width=100, height=80),
                         content={"kind": "path", "d": "M 0 0 L 100 0 L 50 80 Z", "fill": "#ffffff"}),
            SceneElement("unsafe", "shape", transform=SceneTransform(x=520, y=10, width=100, height=80),
                         content={"kind": "path", "d": 'M 0 0" onload="alert(1)', "fill": "#ffffff"}),
        ),
    )


def test_vector_geometry_is_bounded_and_json_safe() -> None:
    assert len(regular_polygon_points(7, 100.0, 80.0)) == 7
    assert len(star_points(5, 0.4, 100.0, 80.0)) == 10
    assert sanitize_path_data("M 0 0 L 10 10 Z") == "M 0 0 L 10 10 Z"
    assert sanitize_path_data('M 0 0" onload="alert(1)') == ""
    x, y, width, height, content = path_geometry_from_scene_points(
        ((30.0, 10.0), (80.0, 20.0), (50.0, 70.0)), closed=True,
    )
    assert (x, y, width, height) == (30.0, 10.0, 50.0, 60.0)
    assert content["d"].endswith("Z")
    assert path_points_in_scene(content, x=x, y=y) == ((30.0, 10.0), (80.0, 20.0), (50.0, 70.0))


def test_vector_shapes_render_identically_for_preview_and_export() -> None:
    renderer = SvgSceneRenderer()
    renderer.prepare(_scene(), RenderContext(fps=24, width=640, height=360))
    try:
        frame = renderer.render_frame(AnimationFrame(0, 24))
    finally:
        renderer.cleanup()
    data = frame.data.decode("utf-8")
    assert data.count("<polygon") == 3
    assert 'data-element-id="path"' in data
    assert 'd="M 0 0 L 100 0 L 50 80 Z"' in data
    assert "onload" not in data
    assert "alert" not in data


def test_native_html_overlay_uses_css_text_and_inline_svg_shapes() -> None:
    markup = native_overlay_markup((
        SceneElement(
            "headline", "text", transform=SceneTransform(x=10.0, y=20.0, width=300.0, height=60.0),
            style={"color": "#ffffff", "font_size": 36.0}, content="Live headline",
        ),
        SceneElement(
            "box", "shape", transform=SceneTransform(x=40.0, y=120.0, width=160.0, height=80.0),
            style={"fill": "#ff8800"}, content={"kind": "rounded_rect", "corner_radius": 12.0},
        ),
    ))
    assert 'data-topos-native-element="headline"' in markup
    assert "<div" in markup and "Live headline" in markup
    assert 'font-size:36.0000px' in markup
    assert 'data-topos-native-element="box"' in markup
    assert "<svg" in markup and "<rect" in markup


def test_frame_containers_materialize_as_flex_grid_dom() -> None:
    """WP-3: frames become CSS containers; children nest inside them."""
    frame = SceneElement(
        "card", "frame", transform=SceneTransform(x=40, y=60, width=600, height=300),
        style={"fill": "#1e293b", "corner_radius": 16.0},
        layout={"mode": "flex-row", "gap": 24.0, "padding": 32.0, "justify": "between"},
    )
    title = SceneElement("title", "text", transform=SceneTransform(width=200, height=60),
                         style={"color": "#ffffff"}, content={"text": "Card"})
    badge = SceneElement("badge", "shape", transform=SceneTransform(width=80, height=80),
                         style={"fill": "#ff8800"}, content={"kind": "ellipse"},
                         layout={"width_mode": "fill"})
    markup = native_overlay_markup([frame, title, badge], {"title": "card", "badge": "card"})
    # Frame opens before its children; children are nested inside the div.
    frame_open = markup.index('data-topos-native-element="card"')
    title_open = markup.index('data-topos-native-element="title"')
    badge_open = markup.index('data-topos-native-element="badge"')
    frame_close = markup.index("</div>", badge_open)
    assert frame_open < title_open < badge_open < frame_close
    assert "display:flex;flex-direction:row;gap:24.0000px" in markup
    assert "justify-content:space-between" in markup
    assert "background:#1e293b" in markup
    assert "border-radius:16.0000px" in markup
    # Flow children drop absolute x/y and gain flex sizing instead.
    assert 'data-topos-native-element="title"' in markup
    title_style = markup[title_open:markup.index(">", title_open)]
    assert "position:relative" in title_style and "left:" not in title_style
    assert "flex:1 1 auto" in markup  # badge width_mode=fill

    # Without a hierarchy mapping the exact legacy flat output is preserved.
    flat = native_overlay_markup([title, badge])
    assert flat.count("position:absolute") == 2


def test_absolute_and_grid_frames_materialize() -> None:
    frame = SceneElement(
        "grid1", "frame", transform=SceneTransform(width=800, height=600),
        layout={"mode": "grid", "columns": 3, "rows": 2, "gap": 12.0},
    )
    markup = native_overlay_markup([frame])
    assert "display:grid;grid-template-columns:repeat(3,1fr)" in markup
    assert "grid-template-rows:repeat(2,1fr)" in markup

    absolute = SceneElement("abs", "frame", transform=SceneTransform(width=100, height=80),
                            layout={"mode": "absolute"})
    child = SceneElement("dot", "shape", transform=SceneTransform(x=10, y=12, width=20, height=20),
                         style={"fill": "#ff8800"}, content={"kind": "circle"})
    nested = native_overlay_markup([absolute, child], {"dot": "abs"})
    dot_open = nested.index('data-topos-native-element="dot"')
    dot_style = nested[dot_open:nested.index(">", dot_open)]
    # Absolute frames keep child local coordinates (left/top inside container).
    assert "left:10.0000px" in dot_style and "top:12.0000px" in dot_style
    assert "display:flex" not in nested and "display:grid" not in nested


def test_hostile_layout_values_never_reach_the_dom() -> None:
    frame = SceneElement(
        "evil", "frame", transform=SceneTransform(width=100, height=80),
        style={"fill": "javascript:alert(1)", "corner_radius": 10.0},
        layout={"mode": "flex-row</style>", "gap": "<script>", "justify": "'; drop"},
    )
    markup = native_overlay_markup([frame])
    assert "<script>" not in markup
    assert "javascript:" not in markup
    assert "</style>" not in markup
    # Unknown enum values fall back to safe CSS rather than echoing input.
    assert "display:flex" not in markup


def test_native_animation_css_samples_tracks_and_scrubs() -> None:
    """WP-5: keyframes materialize as paused CSS driven by --topos-time."""
    from src.features.teaching_animation.core.model import SceneAnimation
    from src.features.teaching_animation.render.html_native_overlay import native_animation_css
    from src.shared.keyframe import Keyframe, KeyframeTrack

    element = SceneElement("t1", "text", transform=SceneTransform(x=100.0, y=50.0),
                           style={"color": "#ffffff"}, content={"text": "Hi"})
    move = KeyframeTrack("x", [Keyframe(0, 0.0), Keyframe(24, 400.0)],
                         min_val=-100000.0, max_val=100000.0)
    fade = KeyframeTrack("opacity", [Keyframe(0, 0.0), Keyframe(12, 1.0)])
    scene = _scene().with_element_added(element)
    scene = scene.with_animation_added(SceneAnimation("a1", "t1", "x", move))
    scene = scene.with_animation_added(SceneAnimation("a2", "t1", "opacity", fade))

    css = native_animation_css(scene)
    assert "@keyframes topos-a-t1-x{" in css
    assert "@keyframes topos-a-t1-opacity{" in css
    # The sampled ramp matches the renderer's evaluation at key frames.
    assert "left:0.0000px" in css
    assert "left:400.0000px" in css
    assert "left:200.0000px" in css  # frame 12 = midpoint of the linear ramp
    # Scrubbing is a single paused animation with a variable negative delay.
    assert "animation:" in css and "both paused" in css
    assert "animation-delay:calc(var(--topos-time,0s)*-1)" in css

    # Elements without tracks produce no animation rules.
    quiet = _scene().with_element_added(
        SceneElement("t2", "text", content={"text": "x"}))
    assert native_animation_css(quiet) == ""
