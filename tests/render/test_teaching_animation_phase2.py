from pathlib import Path

import pytest

from src.features.teaching_animation.core.contracts import AnimationFrame, RenderContext
from src.features.teaching_animation.core.model import (
    SceneAnimation,
    SceneAssetRef,
    SceneElement,
    SceneTransform,
    TeachingSceneDocument,
)
from src.features.teaching_animation.render.asset_resolver import AssetPreflightError
from src.features.teaching_animation.render.evaluator import evaluate
from src.features.teaching_animation.render.frame_adapter import FrameAdapter
from src.features.teaching_animation.render.html_preview_renderer import HtmlPreviewRenderer
from src.features.teaching_animation.render.scene_renderer import CancellationToken, RenderCancelled
from src.features.teaching_animation.render.svg_renderer import SvgSceneRenderer
from src.features.teaching_animation.core.composition_schema import Mask, MaskKind, SceneInstance
from src.features.teaching_animation.scene_store import get_shared_scene_store
from src.shared.keyframe import Keyframe, KeyframeTrack


def _track(name: str, values: list[tuple[int, float]], *, max_val: float = 1.0) -> KeyframeTrack:
    return KeyframeTrack(
        param_name=name,
        keyframes=[Keyframe(frame, value) for frame, value in values],
        min_val=0.0,
        max_val=max_val,
    )


def _scene(*, asset_refs: tuple[SceneAssetRef, ...] = ()) -> TeachingSceneDocument:
    title = SceneElement(
        element_id="title",
        type="text",
        transform=SceneTransform(x=20, y=20, width=300, height=50),
        style={"color": "#ffffff", "font_size": 24},
        content="Phase 2",
    )
    formula = SceneElement(
        element_id="formula",
        type="formula",
        transform=SceneTransform(x=20, y=70, width=300, height=50),
        content="E = mc^2",
    )
    shape = SceneElement(
        element_id="arrow",
        type="shape",
        transform=SceneTransform(x=20, y=140, width=120, height=30),
        content={"kind": "arrow", "fill": "#22c55e"},
    )
    raw_svg = SceneElement(
        element_id="raw-svg",
        type="svg",
        transform=SceneTransform(x=180, y=140, width=50, height=50),
        content='<svg><circle cx="10" cy="10" r="8" fill="#ef4444"/><script>alert(1)</script></svg>',
    )
    return TeachingSceneDocument(
        scene_id="scene",
        schema_version=1,
        name="Render test",
        width=320,
        height=200,
        fps_num=24,
        fps_den=1,
        duration_frames=48,
        background={"color": "#111827"},
        elements=(title, formula, shape, raw_svg),
        animations=(SceneAnimation("fade", "title", "opacity", _track("opacity", [(0, 0.0), (12, 1.0)])),),
        asset_refs=asset_refs,
    )


def test_evaluator_is_integer_frame_deterministic_and_interpolates() -> None:
    scene = _scene()
    first = evaluate(scene, 6)
    second = evaluate(scene, AnimationFrame(6, scene.fps))
    assert first == second
    assert first.elements[0].opacity == pytest.approx(0.5)
    with pytest.raises(ValueError, match="outside"):
        evaluate(scene, scene.duration_frames)


def test_svg_renderer_renders_structured_elements_and_filters_script() -> None:
    renderer = SvgSceneRenderer()
    context = RenderContext(fps=24, width=320, height=200)
    renderer.prepare(_scene(), context)
    frame = renderer.render_frame(AnimationFrame(6, 24))
    renderer.cleanup()
    data = frame.data.decode("utf-8")
    assert frame.pixel_format == "svg"
    assert 'data-element-id="formula"' in data
    assert "E = mc^2" in data
    assert "polygon" in data
    assert "script" not in data
    assert 'opacity="0.500000"' in data


def test_svg_renderer_preserves_rich_model_authored_style() -> None:
    """Gradients, rounded shapes and typography survive SVG rendering."""
    scene = TeachingSceneDocument.create_default(
        "rich", width=320, height=200, duration_frames=12,
    )
    rich_shape = SceneElement(
        element_id="gradient-card", type="shape", z_index=1,
        transform=SceneTransform(x=20, y=20, width=180, height=100),
        style={
            "corner_radius": 18,
            "gradient": {
                "kind": "linear", "angle": 90,
                "stops": [
                    {"position": 0.0, "color": "#ff006e"},
                    {"position": 1.0, "color": "#8338ec"},
                ],
            },
            "stroke": "#ffffff", "stroke_width": 2,
        },
        content={"kind": "rounded_rect"},
    )
    rich_text = SceneElement(
        element_id="rich-copy", type="text", z_index=2,
        transform=SceneTransform(x=40, y=50, width=200, height=50),
        style={
            "color": "#ffffff", "font_size": 22,
            "font_family": "Arial", "font_weight": "bold",
            "letter_spacing": 1.5,
        },
        content="Motion\n自由发挥",
    )
    scene = scene.with_element_added(rich_shape).with_element_added(rich_text)
    renderer = SvgSceneRenderer()
    renderer.prepare(scene, RenderContext(fps=24, width=320, height=200))
    try:
        data = renderer.render_frame(AnimationFrame(0, 24)).data.decode("utf-8")
    finally:
        renderer.cleanup()
    assert "linearGradient" in data
    assert "url(#gradient_gradient-card)" in data
    assert 'rx="18.0"' in data
    assert 'font-weight="bold"' in data
    assert "自由发挥" in data


def test_svg_renderer_applies_vector_mask_from_composition_metadata() -> None:
    from dataclasses import replace
    from src.features.teaching_animation.core.composition_schema import write_composition_into

    scene = _scene()
    masked = "formula"
    scene = replace(scene, metadata=write_composition_into(
        scene.metadata,
        masks=(Mask("m1", masked, kind=MaskKind.VECTOR, path_ref="M0 0 L50 0 L50 50 Z"),),
    ))
    renderer = SvgSceneRenderer()
    renderer.prepare(scene, RenderContext(fps=24, width=320, height=200))
    data = renderer.render_frame(AnimationFrame(0, 24)).data.decode("utf-8")
    renderer.cleanup()
    assert '<mask id="mask_m1"' in data
    assert 'mask="url(#mask_m1)"' in data


def test_svg_renderer_resolves_precomp_by_scene_reference() -> None:
    from dataclasses import replace
    from src.features.teaching_animation.core.composition_schema import write_composition_into

    store = get_shared_scene_store()
    child = TeachingSceneDocument.create_default("Child", scene_id="phase2-child", width=64, height=48, duration_frames=6)
    store.register(child)
    parent = TeachingSceneDocument.create_default("Parent", scene_id="phase2-parent", width=320, height=200, duration_frames=6)
    parent = replace(parent, metadata=write_composition_into(
        parent.metadata, instances=(SceneInstance("pre1", child.scene_id, params={"x": 10}),),
    ))
    renderer = SvgSceneRenderer()
    renderer.prepare(parent, RenderContext(fps=24, width=320, height=200))
    data = renderer.render_frame(AnimationFrame(0, 24)).data.decode("utf-8")
    renderer.cleanup()
    store.remove(child.scene_id)
    assert 'data-instance-id="pre1"' in data


def test_same_frame_is_identical_for_preview_html_and_export() -> None:
    scene = _scene()
    context = RenderContext(fps=24, width=320, height=200)
    export_renderer = SvgSceneRenderer()
    preview_renderer = HtmlPreviewRenderer()
    export_renderer.prepare(scene, context)
    preview_renderer.prepare(scene, context)
    export_frame = export_renderer.render_frame(AnimationFrame(7, 24))
    preview_frame = preview_renderer.render_frame(AnimationFrame(7, 24))
    html = preview_renderer.render_html(AnimationFrame(7, 24))
    export_renderer.cleanup()
    preview_renderer.cleanup()
    assert export_frame.data == preview_frame.data
    assert export_frame.data.decode("utf-8") in html


def test_missing_assets_block_strict_render_and_downgrade_explicitly(tmp_path: Path) -> None:
    missing = SceneAssetRef("image-1", "image", "missing.svg")
    scene = _scene(asset_refs=(missing,))
    image = SceneElement(
        element_id="image",
        type="image",
        transform=SceneTransform(width=40, height=40),
        asset_ref="image-1",
    )
    scene = scene.with_element_added(image)
    with pytest.raises(AssetPreflightError, match="missing image"):
        SvgSceneRenderer().prepare(scene, RenderContext(asset_root=str(tmp_path)))
    renderer = SvgSceneRenderer()
    renderer.prepare(scene, RenderContext(asset_root=str(tmp_path), strict_assets=False, width=320, height=200))
    assert "#7f1d1d" in renderer.render_frame(AnimationFrame(0, 24)).data.decode("utf-8")
    renderer.cleanup()


def test_rgba_adapter_produces_timeline_consumable_frame() -> None:
    renderer = SvgSceneRenderer()
    renderer.prepare(_scene(), RenderContext(fps=24, width=64, height=48))
    rendered = renderer.render_frame(AnimationFrame(0, 24))
    rgba = FrameAdapter.from_rendered_frame(rendered)
    renderer.cleanup()
    assert rgba.width == 64
    assert rgba.height == 48
    assert len(rgba.data) == 64 * 48 * 4


def test_cancellation_and_cleanup_release_renderer_state() -> None:
    token = CancellationToken()
    renderer = SvgSceneRenderer(token)
    renderer.prepare(_scene(), RenderContext(fps=24, width=320, height=200))
    token.cancel()
    with pytest.raises(RenderCancelled):
        renderer.render_frame(AnimationFrame(0, 24))
    renderer.cleanup()
    with pytest.raises(RuntimeError, match="prepared"):
        renderer.render_frame(AnimationFrame(0, 24))


# --------------------------------------------------------------------------- #
# TM-05: design-token resolution at render time
# --------------------------------------------------------------------------- #


def test_evaluate_resolves_design_token_references_in_styles() -> None:
    """evaluate() resolves ``$category.key`` token refs against the default
    design-token set so neither renderer ever emits a literal ``$...`` string.
    Template-derived scenes resolve at compile time; this is the safety net for
    token refs that slip in via hand-editing, migration or a partial edit.
    """
    heading = SceneElement(
        element_id="tok-heading", type="text",
        transform=SceneTransform(x=20, y=20, width=300, height=50),
        style={"color": "$palette.primary", "font_size": 20.0},
        content="Hi",
    )
    scene = TeachingSceneDocument.create_default("tokens", width=64, height=48, duration_frames=6)
    scene = scene.with_element_added(heading)
    ev = evaluate(scene, AnimationFrame(0, scene.fps))
    style = next(e.style for e in ev.elements if e.source.element_id == "tok-heading")
    # The token ref is resolved to the default palette's primary, not left literal.
    assert style["color"] == "#3b82f6", style["color"]
    # Non-string / literal values pass through unchanged.
    assert style["font_size"] == 20.0


def test_evaluate_keeps_locked_styles_literal() -> None:
    """A style marked ``__locked__`` keeps its literal token ref — brand updates
    and the render-time resolver never silently overwrite hand-tuned content."""
    locked = SceneElement(
        element_id="tok-locked", type="text",
        transform=SceneTransform(x=20, y=20, width=300, height=50),
        style={"color": "$palette.primary", "__locked__": ["color"]},
        content="Hi",
    )
    scene = TeachingSceneDocument.create_default("locked", width=64, height=48, duration_frames=6)
    scene = scene.with_element_added(locked)
    ev = evaluate(scene, AnimationFrame(0, scene.fps))
    style = next(e.style for e in ev.elements if e.source.element_id == "tok-locked")
    # Locked property is preserved verbatim (not resolved).
    assert style["color"] == "$palette.primary"


# --------------------------------------------------------------------------- #
# SG-01: hierarchy — world transform + visibility in the evaluator
# --------------------------------------------------------------------------- #


def _hierarchy_scene() -> tuple[TeachingSceneDocument, str, str]:
    """A parent group at (100, 50) with one child at local (10, 10)."""
    from src.features.teaching_animation.core.hierarchy import Hierarchy, write_hierarchy_into
    parent = SceneElement("grp", "shape", transform=SceneTransform(x=100, y=50, width=80, height=60),
                          content={"kind": "rect"})
    child = SceneElement("chip", "shape", transform=SceneTransform(x=10, y=10, width=20, height=20),
                         content={"kind": "rect"})
    base = TeachingSceneDocument.create_default("h", width=320, height=200, duration_frames=6)
    base = base.with_element_added(parent).with_element_added(child)
    meta = write_hierarchy_into(base.metadata, Hierarchy(parents={"chip": "grp"}))
    from dataclasses import replace
    return replace(base, metadata=meta), "grp", "chip"


def test_evaluate_composes_parent_transform_into_child_world_position() -> None:
    """A child's world position = its local translate composed with the parent's:
    moving the parent group moves the child (SG-01 acceptance)."""
    scene, _grp, _chip = _hierarchy_scene()
    ev = evaluate(scene, AnimationFrame(0, scene.fps))
    child = next(e for e in ev.elements if e.source.element_id == "chip")
    parent = next(e for e in ev.elements if e.source.element_id == "grp")
    # Parent keeps its own transform; child inherits the parent's translate.
    assert parent.transform.x == 100 and parent.transform.y == 50
    assert child.transform.x == 110 and child.transform.y == 60


def test_evaluate_hidden_ancestor_hides_descendant() -> None:
    """A hidden parent zeroes its descendants' opacity so the renderer skips them."""
    from src.features.teaching_animation.core.hierarchy import Hierarchy, write_hierarchy_into
    from dataclasses import replace
    scene, grp, chip = _hierarchy_scene()
    meta = write_hierarchy_into(scene.metadata, Hierarchy(parents={"chip": "grp"}, hidden={"grp"}))
    scene = replace(scene, metadata=meta)
    ev = evaluate(scene, AnimationFrame(0, scene.fps))
    child = next(e for e in ev.elements if e.source.element_id == "chip")
    assert child.opacity == 0.0


def test_evaluate_flat_scene_is_unchanged_by_hierarchy_path() -> None:
    """A scene with no hierarchy takes the fast path: every element's evaluated
    transform equals its local transform exactly (no composition float noise)."""
    scene = TeachingSceneDocument.create_default("flat", width=320, height=200, duration_frames=6)
    ev = evaluate(scene, AnimationFrame(0, scene.fps))
    for element in ev.elements:
        assert element.transform.x == element.source.transform.x
        assert element.transform.y == element.source.transform.y
        assert element.transform.scale_x == element.source.transform.scale_x


# --------------------------------------------------------------------------- #
# SG-03: Auto Layout render-time pass
# --------------------------------------------------------------------------- #


def _stack_scene(width: int, height: int) -> TeachingSceneDocument:
    """A scene with three cards in a vertical-stack SceneLayout."""
    from dataclasses import replace as _replace
    from src.features.teaching_animation.core.layout import (
        AlignMode, AutoLayout, LayoutDirection, SceneLayout, write_scene_layout_into,
    )
    cards = tuple(
        SceneElement(f"card{i}", "shape",
                     transform=SceneTransform(x=0, y=0, width=200, height=80),
                     content={"kind": "rect"})
        for i in range(3)
    )
    base = TeachingSceneDocument.create_default("stack", width=width, height=height, duration_frames=6)
    base = _replace(base, elements=cards)
    layout = SceneLayout(
        mode="stack", child_ids=("card0", "card1", "card2"),
        layout=AutoLayout(direction=LayoutDirection.VERTICAL, padding=(40, 40, 40, 40),
                          gap=16.0, align=AlignMode.CENTER))
    return _replace(base, metadata=write_scene_layout_into(base.metadata, layout))


def test_auto_layout_stacks_children_vertically_without_overlap() -> None:
    """A vertical-stack SceneLayout positions its children top-to-bottom with the
    configured gap, centred horizontally — no overlap (SG-03 acceptance)."""
    scene = _stack_scene(480, 270)
    ev = evaluate(scene, AnimationFrame(0, scene.fps))
    by_id = {e.source.element_id: e.transform for e in ev.elements}
    # The three cards are laid out in sequence along y, each 80 tall + 16 gap.
    assert by_id["card0"].y < by_id["card1"].y < by_id["card2"].y
    assert by_id["card1"].y >= by_id["card0"].y + by_id["card0"].height
    # Centred horizontally within the content rect (canvas 480 − 40 padding each side).
    assert by_id["card0"].x > 40


def test_auto_layout_reflows_when_canvas_changes_aspect() -> None:
    """Changing the canvas 16:9 → 9:16 re-resolves the stack: the cross-axis
    (horizontal) centering re-centres against the new width — the SG-03 'reflow'
    acceptance.  A vertical stack's main-axis (y) positions are padding+gap
    driven and don't move; the cross-axis (x) re-centres with the canvas width."""
    from dataclasses import replace as _replace
    wide = _stack_scene(480, 270)
    tall = _replace(wide, width=270, height=480)
    ev_wide = evaluate(wide, AnimationFrame(0, wide.fps))
    ev_tall = evaluate(tall, AnimationFrame(0, tall.fps))
    wide_x = {e.source.element_id: e.transform.x for e in ev_wide.elements}
    tall_x = {e.source.element_id: e.transform.x for e in ev_tall.elements}
    # Cards centre within the content rect; the narrower 9:16 canvas re-centres
    # them differently (here the 200px cards are wider than the 270-80 content
    # rect, so they pin to the start instead of centring).
    assert wide_x != tall_x, "layout did not reflow when the canvas aspect changed"
    # The wide canvas centres a 200px card in a 400px content rect (x=140).
    assert wide_x["card0"] == 140.0


def test_auto_layout_does_not_affect_non_grouped_elements() -> None:
    """An element not in the SceneLayout's child_ids keeps its absolute transform."""
    from dataclasses import replace as _replace
    from src.features.teaching_animation.core.layout import (
        AutoLayout, SceneLayout, write_scene_layout_into,
    )
    base = _stack_scene(480, 270)
    extra = SceneElement("free", "shape",
                         transform=SceneTransform(x=13, y=17, width=10, height=10),
                         content={"kind": "rect"})
    base = _replace(base, elements=base.elements + (extra,))
    ev = evaluate(base, AnimationFrame(0, base.fps))
    free = next(e for e in ev.elements if e.source.element_id == "free")
    # The free element is NOT in the layout group → absolute transform preserved.
    assert free.transform.x == 13 and free.transform.y == 17
