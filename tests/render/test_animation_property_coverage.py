"""R2: animation property coverage and Bezier/Spring consistency.

The audit found: native CSS only animated x/y/width/height/opacity/transform,
Bezier degraded to ``linear``, spring semantics were lost on compile, and the
inspector could not explain what drives a track.  These tests pin the fixes.
"""

from src.features.teaching_animation.core.model import (
    SceneElement,
    SceneTransform,
    TeachingSceneDocument,
)
from src.features.teaching_animation.motion.motion_commands import (
    StaggerElementsCommand,
    motion_semantics,
    read_motion_plans,
)
from src.features.teaching_animation.motion.spring import SpringConfig
from src.features.teaching_animation.render.evaluator import evaluate
from src.features.teaching_animation.render.html_native_overlay import native_animation_css
from src.shared.keyframe import (
    InterpolationType,
    Keyframe,
    KeyframeTrack,
    normalized_segment_controls,
)


def _scene(*elements: SceneElement, duration: int = 24) -> TeachingSceneDocument:
    return TeachingSceneDocument(
        scene_id="r2", schema_version=3, name="r2", width=640, height=360,
        fps_num=24, fps_den=1, duration_frames=duration, background={"color": "#000000"},
        animations=(), elements=elements,
    )


def _text_element() -> SceneElement:
    return SceneElement(
        "title", "text", transform=SceneTransform(10, 10, 300, 60),
        content={"text": "hello"}, style={"font_size": 40.0, "blur": 0.0},
        local_animation_tracks=(
            KeyframeTrack("font_size", (
                Keyframe(0, 40.0, InterpolationType.BEZIER, handle_right=(6.0, 10.0)),
                Keyframe(11, 72.0),
            ), min_val=0.0, max_val=2048.0),
            KeyframeTrack("blur", (Keyframe(0, 12.0), Keyframe(11, 0.0)), min_val=0.0, max_val=100.0),
            KeyframeTrack("opacity", (Keyframe(0, 0.0), Keyframe(11, 1.0))),
        ),
    )


def test_bezier_maps_to_real_css_cubic_bezier() -> None:
    css = native_animation_css(_scene(_text_element()))
    assert "cubic-bezier(" in css
    assert 'animation-timing-function:cubic-bezier(0.545455,0.312500' in css


def test_bezier_control_point_mapping_matches_keyframe_math() -> None:
    kf1 = Keyframe(0, 0.0, InterpolationType.BEZIER, handle_right=(10.0, 20.0))
    kf2 = Keyframe(20, 100.0, InterpolationType.BEZIER, handle_left=(-5.0, 40.0))
    controls = normalized_segment_controls(kf1, kf2)
    assert controls is not None
    x1, y1, x2, y2 = controls
    assert abs(x1 - 0.5) < 1e-9          # 10/20
    assert abs(y1 - 0.2) < 1e-9          # 20/100
    assert abs(x2 - 0.75) < 1e-9         # (20-5)/20
    assert abs(y2 - 1.4) < 1e-9          # (100+40)/100 -> CSS overshoot is legal
    # x stays within the CSS-legal [0,1] because handles are segment clamped
    assert 0.0 <= x1 <= 1.0 and 0.0 <= x2 <= 1.0


def test_flat_bezier_segment_falls_back_to_linear() -> None:
    assert normalized_segment_controls(
        Keyframe(0, 5.0), Keyframe(10, 5.0)) is None


def test_font_size_and_blur_animate_in_css() -> None:
    css = native_animation_css(_scene(_text_element()))
    assert "topos-a-title-font_size" in css
    assert "font-size:40.0000px" in css
    assert "topos-a-title-blur" in css
    assert "filter:blur(12.0000px)" in css


def test_evaluator_samples_style_tracks_as_shared_truth() -> None:
    scene = _scene(_text_element())
    start = evaluate(scene, 0)
    mid = evaluate(scene, 5)
    end = evaluate(scene, 11)
    assert start.elements[0].style["font_size"] == 40.0
    assert mid.elements[0].style["font_size"] > 40.0
    assert abs(end.elements[0].style["font_size"] - 72.0) < 1e-6
    assert end.elements[0].style["blur"] == 0.0


def test_shape_fill_and_stroke_animate_via_child_selector() -> None:
    shape = SceneElement(
        "plate", "shape", transform=SceneTransform(0, 0, 120, 80),
        content={"kind": "rect"}, style={"fill": "#123456"},
        local_animation_tracks=(
            KeyframeTrack("fill_opacity", (Keyframe(0, 0.0), Keyframe(11, 1.0))),
            KeyframeTrack("stroke_width", (Keyframe(0, 0.0), Keyframe(11, 6.0)), min_val=0.0, max_val=100.0),
        ),
    )
    css = native_animation_css(_scene(shape))
    assert 'data-topos-native-element="plate"] > *' in css
    assert "fill-opacity:0.0000;" in css
    assert "stroke-width:6.0000px" in css
    # The evaluator drives the same values for the SVG/capture path.
    frame6 = evaluate(_scene(shape), 6)
    assert abs(frame6.elements[0].style["fill_opacity"] - 6.0 / 11.0) < 1e-6


def test_style_tracks_without_tracks_leave_static_output_identical() -> None:
    plain = SceneElement(
        "plain", "text", transform=SceneTransform(0, 0, 100, 40),
        content={"text": "x"}, style={"font_size": 33.0, "blur": 2.0},
    )
    evaluated = evaluate(_scene(plain), 5)
    assert evaluated.elements[0].style["font_size"] == 33.0
    assert evaluated.elements[0].style["blur"] == 2.0


def test_spring_semantics_record_settle_and_overshoot() -> None:
    semantics = motion_semantics(
        "pop", {"spring_config": SpringConfig(mass=1.0, damping=8.0, stiffness=200.0)}, fps=24.0)
    assert semantics["kind"] == "spring"
    assert semantics["damping"] == 8.0
    assert semantics["overshoot"] is True
    assert semantics["settle_frame"] > 0

    clamped = motion_semantics("pop", {"overshoot": False}, fps=24.0)
    assert clamped["overshoot"] is False

    eased = motion_semantics("fade", {"duration": 12}, fps=24.0)
    assert eased["kind"] == "easing"


def test_stagger_plan_round_trips_spring_config_through_metadata() -> None:
    import json

    scene = TeachingSceneDocument(
        scene_id="s", schema_version=3, name="s", width=640, height=360,
        fps_num=24, fps_den=1, duration_frames=90, background={"color": "#000000"},
        animations=(), elements=(
            SceneElement("a", "shape", transform=SceneTransform(0, 0, 10, 10), content={"kind": "rect"}),
        ),
    )
    cmd = StaggerElementsCommand(
        element_ids=("a",), preset_id="pop", param_name="scale_x", duration=20,
        params={"spring_config": SpringConfig(mass=1.0, damping=8.0, stiffness=200.0)},
    )
    after = cmd.apply(scene)
    # The whole metadata must stay JSON-serializable (SpringConfig is not).
    json.dumps(after.metadata)
    plan = next(iter(read_motion_plans(after).values()))
    assert plan.motion["kind"] == "spring"
    assert plan.motion["damping"] == 8.0
    # A ripple recompile rebuilds from the persisted dict-form config.
    from src.features.teaching_animation.motion.motion_commands import SetStaggerIntervalCommand
    rippled = SetStaggerIntervalCommand(plan_id=plan.plan_id, interval=8).apply(after)
    assert len(rippled.animations) == 1
    assert cmd.undo(after).metadata.get("motion_plans") is None
