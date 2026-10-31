"""UX-14 auto-key: direct manipulation writes keyframes at the current frame.

The controller is Qt-free, so the auto-key behaviour (a finalized drag at frame
N writes transform keyframes when auto-key is on, and is a pure static edit
when off) is unit-tested directly through the preview/commit handshake.
"""

from __future__ import annotations

import pytest

from src.features.teaching_animation.core.model import SceneElement, SceneTransform, TeachingSceneDocument
from src.features.teaching_animation.ui.editor_controller import SceneEditorController


def _scene_with(element: SceneElement) -> TeachingSceneDocument:
    base = TeachingSceneDocument.create_default("autokey", width=320, height=200, duration_frames=24)
    return base.with_element_added(element)


def _element(eid: str = "card", *, x: float = 10.0, y: float = 10.0) -> SceneElement:
    return SceneElement(
        element_id=eid, type="shape",
        transform=SceneTransform(x=x, y=y, width=80, height=40),
        style={}, content={"kind": "rect"})


def _drag(controller: SceneEditorController, eid: str, x: float, y: float) -> None:
    """Simulate a canvas drag: begin → preview moves → commit."""
    controller.begin_preview(eid)
    el = controller.scene.element_by_id(eid)
    from dataclasses import replace
    controller.preview_element(eid, {"transform": replace(el.transform, x=x, y=y)})
    controller.commit_preview(eid)


def test_auto_key_off_drag_is_a_static_transform_no_keyframes() -> None:
    controller = SceneEditorController(_scene_with(_element()))
    controller.set_frame(5)
    assert controller.auto_key is False
    _drag(controller, "card", x=100.0, y=50.0)
    # Transform updated to the dragged position...
    assert controller.scene.element_by_id("card").transform.x == 100.0
    # ...but no animation track was created.
    assert not any(a.element_id == "card" for a in controller.scene.animations)


def test_auto_key_on_drag_writes_x_and_y_keyframes_at_current_frame() -> None:
    controller = SceneEditorController(_scene_with(_element()))
    controller.set_frame(10)
    controller.set_auto_key(True)
    _drag(controller, "card", x=120.0, y=70.0)
    anims = {a.property: a for a in controller.scene.animations if a.element_id == "card"}
    assert "x" in anims and "y" in anims
    assert anims["x"].track.get_value(10) == pytest.approx(120.0)
    assert anims["y"].track.get_value(10) == pytest.approx(70.0)


def test_auto_key_keyframes_use_a_wide_range_so_positions_are_not_clamped() -> None:
    """A coordinate keyframe far outside [0,1] must survive get_value (no clamp)."""
    controller = SceneEditorController(_scene_with(_element()))
    controller.set_frame(0)
    controller.set_auto_key(True)
    _drag(controller, "card", x=1500.0, y=-300.0)
    x_track = next(a.track for a in controller.scene.animations
                   if a.element_id == "card" and a.property == "x")
    assert x_track.get_value(0) == pytest.approx(1500.0)


def test_auto_key_only_keyframes_properties_that_actually_moved() -> None:
    """A drag that only changes x must not also create a redundant y track."""
    controller = SceneEditorController(_scene_with(_element(x=10, y=10)))
    controller.set_frame(3)
    controller.set_auto_key(True)
    # Move only x; y stays at 10.
    controller.begin_preview("card")
    el = controller.scene.element_by_id("card")
    from dataclasses import replace
    controller.preview_element("card", {"transform": replace(el.transform, x=200.0, y=10.0)})
    controller.commit_preview("card")
    props = {a.property for a in controller.scene.animations if a.element_id == "card"}
    assert "x" in props
    assert "y" not in props


def test_auto_key_second_drag_at_another_frame_extends_the_track() -> None:
    """Dragging the same element at frame 5 then frame 15 builds one track with
    two keyframes (interpolation between them), not two separate tracks."""
    controller = SceneEditorController(_scene_with(_element()))
    controller.set_auto_key(True)
    controller.set_frame(5)
    _drag(controller, "card", x=100.0, y=0.0)
    controller.set_frame(15)
    _drag(controller, "card", x=200.0, y=0.0)
    x_anims = [a for a in controller.scene.animations
               if a.element_id == "card" and a.property == "x"]
    assert len(x_anims) == 1  # one track, extended
    track = x_anims[0].track
    assert track.get_value(5) == pytest.approx(100.0)
    assert track.get_value(15) == pytest.approx(200.0)
    # Frames between the two keyframes interpolate.
    assert track.get_value(10) == pytest.approx(150.0)
