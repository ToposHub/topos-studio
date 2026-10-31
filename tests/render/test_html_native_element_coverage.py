"""R3: every SUPPORTED_ELEMENT_TYPES materializes in the HTML native overlay.

The pure-SVG SceneView already rendered image/svg/chart/formula/draw; before
this coverage the DOM overlay silently dropped them, so the browser preview
and every HTML export lost layers that the editor displayed.
"""

from pathlib import Path

from src.features.teaching_animation.core.contracts import RenderContext
from src.features.teaching_animation.core.model import (
    SceneAssetRef,
    SceneElement,
    SceneTransform,
    TeachingSceneDocument,
)
from src.features.teaching_animation.render.html_native_overlay import (
    native_animation_css,
    native_overlay_markup,
    scene_asset_uri_lookup,
)
from src.shared.keyframe import Keyframe, KeyframeTrack


def _element(element_type: str, *, content: object = None, asset_ref: str | None = None,
             style: dict | None = None) -> SceneElement:
    return SceneElement(
        f"el-{element_type}", element_type,
        transform=SceneTransform(x=10, y=20, width=200, height=100),
        content=content, asset_ref=asset_ref, style=style or {},
    )


def test_formula_materializes_as_html_text() -> None:
    markup = native_overlay_markup([
        _element("formula", content={"text": "\\frac{a}{b} \\times c"}),
    ])
    assert 'data-topos-native-element="el-formula"' in markup
    assert "(a)/b × c" in markup
    assert "serif" in markup


def test_image_with_resolvable_asset_uses_data_uri() -> None:
    uri = "data:image/png;base64,aGVsbG8="
    markup = native_overlay_markup(
        [_element("image", asset_ref="img-1")],
        asset_lookup=lambda ref: uri if ref == "img-1" else None,
    )
    assert markup.startswith("<img ")
    assert f'src="{uri}"' in markup
    assert "object-fit:contain" in markup


def test_image_with_missing_asset_shows_visible_placeholder() -> None:
    markup = native_overlay_markup(
        [_element("image", asset_ref="ghost-asset")], asset_lookup=lambda ref: None,
    )
    assert 'data-topos-native-element="el-image"' in markup
    assert "missing image asset" in markup
    assert "ghost-asset" in markup
    assert "#7f1d1d" in markup  # matches the SVG renderer's diagnostic plate


def test_svg_element_is_inlined_and_sanitized() -> None:
    hostile = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<circle cx="10" cy="10" r="8" fill="#00ff00"/>'
        '<script>alert(1)</script>'
        '<image href="http://evil.example/x.png" width="10" height="10"/>'
        "</svg>"
    )
    markup = native_overlay_markup([_element("svg", content=hostile)])
    assert 'data-topos-native-element="el-svg"' in markup
    assert "<circle" in markup
    assert "script" not in markup
    assert "href" not in markup


def test_chart_element_materializes_bars() -> None:
    content = {
        "chart_type": "bar",
        "color": "#22c55e",
        "records": [{"label": "a", "value": 3.0}, {"label": "b", "value": 7.0}],
    }
    markup = native_overlay_markup([_element("chart", content=content)])
    assert markup.count("<rect") == 2
    assert "#22c55e" in markup


def test_chart_with_unparsable_data_stays_empty_but_not_erroring() -> None:
    markup = native_overlay_markup([_element("chart", content={"data": "not-json"})])
    # from_json raises ValueError on a bare string; the overlay must not crash.
    assert "alert" not in markup


def test_draw_element_materializes_polyline() -> None:
    content = {"points": [[0.0, 0.0], [50.0, 25.0], [100.0, 10.0]], "stroke_width": 4.0}
    markup = native_overlay_markup([_element("draw", content=content)])
    assert "<polyline" in markup
    assert "0.000000,0.000000" in markup


def test_children_of_svg_parent_render_as_siblings_not_swallowed() -> None:
    parent = _element("svg", content="<svg><rect width=\"5\" height=\"5\"/></svg>")
    child = SceneElement(
        "child-text", "text", transform=SceneTransform(x=0, y=0, width=50, height=20),
        content={"text": "beside"},
    )
    markup = native_overlay_markup([parent, child], {"child-text": "el-svg"})
    assert 'data-topos-native-element="el-svg"' in markup
    assert "beside" in markup


def test_native_animation_css_covers_all_element_types() -> None:
    chart = _element("chart", content={"records": [{"label": "a", "value": 1.0}]})
    replaced = SceneElement(
        chart.element_id, chart.type,
        transform=SceneTransform(chart.transform.x, chart.transform.y,
                                 chart.transform.width, chart.transform.height),
        content=chart.content, style=dict(chart.style),
        local_animation_tracks=(
            KeyframeTrack("x", (Keyframe(0, 10.0), Keyframe(12, 120.0))),
        ),
    )
    scene = TeachingSceneDocument(
        scene_id="s", schema_version=3, name="s", width=640, height=360,
        fps_num=24, fps_den=1, duration_frames=24, background={"color": "#000000"},
        animations=(), elements=(replaced,),
    )
    css = native_animation_css(scene)
    assert "topos-a-el-chart-x" in css


def test_scene_asset_uri_lookup_resolves_within_asset_root(tmp_path: Path) -> None:
    asset_dir = tmp_path / "assets"
    asset_dir.mkdir()
    pixel = asset_dir / "dot.png"
    pixel.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    scene = TeachingSceneDocument(
        scene_id="s", schema_version=3, name="s", width=64, height=64,
        fps_num=24, fps_den=1, duration_frames=6, background={"color": "#000000"},
        animations=(), elements=(),
        asset_refs=(SceneAssetRef("dot", "image", "assets/dot.png"),),
    )
    context = RenderContext(fps=24, width=64, height=64, asset_root=str(tmp_path))
    lookup = scene_asset_uri_lookup(scene, context)
    assert lookup is not None
    assert lookup("dot").startswith("data:image/png;base64,")
    assert lookup("missing") is None

    no_root = scene_asset_uri_lookup(scene, RenderContext(fps=24, width=64, height=64))
    assert no_root is None
