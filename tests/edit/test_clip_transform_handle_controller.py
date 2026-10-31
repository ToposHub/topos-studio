"""Edit clip Transform handle parity with the composite Transform node."""
from __future__ import annotations

from PyQt6.QtCore import QPointF

from src.features.edit.core.clip_params import TransformParams
from src.features.edit.ui.clip_transform_handle_controller import (
    ClipTransformHandleController,
)


class _ViewerStub:
    """Minimal ProgramViewer coordinate contract used by the controller."""

    def __init__(self) -> None:
        self.update_count = 0

    def width(self) -> int:
        return 1920

    def height(self) -> int:
        return 1080

    def screen(self) -> None:
        return None

    def update(self) -> None:
        self.update_count += 1

    def _calculate_display_rect(self) -> tuple[float, float, float, float]:
        return (-1.0, -1.0, 2.0, 2.0)


def _make_controller() -> ClipTransformHandleController:
    controller = ClipTransformHandleController(_ViewerStub())
    controller.set_canvas_size(1920, 1080)
    controller.set_clip("clip-1", TransformParams())
    return controller


def test_visible_centre_handle_selects_pivot_not_legacy_move() -> None:
    """The centre dot must move the rotation/scale origin, like Composite."""
    controller = _make_controller()
    handles = controller._compute_handles()
    assert handles is not None
    centre_x, centre_y = controller._ndc_to_view_px(*handles.pivot)

    assert controller.hit_test(QPointF(centre_x, centre_y)) == "pivot"


def test_dragging_visible_centre_handle_moves_pivot_without_translating() -> None:
    """A centre-dot drag switches to custom pivot and preserves translation."""
    controller = _make_controller()
    handles = controller._compute_handles()
    assert handles is not None
    centre_x, centre_y = controller._ndc_to_view_px(*handles.pivot)
    updated: list[TransformParams] = []
    controller.on_drag_updated = lambda _clip_id, transform: updated.append(transform)

    start = QPointF(centre_x, centre_y)
    assert controller.start_drag("pivot", start, 0)
    controller.update_drag(QPointF(centre_x + 75.0, centre_y + 50.0), 0)

    assert len(updated) == 1
    transform = updated[0]
    assert transform.pivot_mode == "custom"
    assert transform.pivot_x == 1035.0
    assert transform.pivot_y == 590.0
    assert transform.translate_x == 0.0
    assert transform.translate_y == 0.0


def test_dragging_handle_repaints_overlay_immediately() -> None:
    """The handle overlay must repaint before the async frame refresh."""
    viewer = _ViewerStub()
    controller = ClipTransformHandleController(viewer)
    controller.set_canvas_size(1920, 1080)
    controller.set_clip("clip-1", TransformParams())
    handles = controller._compute_handles()
    assert handles is not None
    centre_x, centre_y = controller._ndc_to_view_px(*handles.pivot)

    assert controller.start_drag(
        "pivot", QPointF(centre_x, centre_y), 0
    )
    before = viewer.update_count
    controller.update_drag(QPointF(centre_x + 25.0, centre_y), 0)

    assert viewer.update_count > before


def test_external_transform_update_repositions_handles_immediately() -> None:
    """Panel-driven TransformParams updates must change the displayed handles."""
    controller = _make_controller()
    before = controller._compute_handles()
    assert before is not None

    controller.set_transform(TransformParams(translate_x=120.0, rotation=30.0))
    after = controller._compute_handles()
    assert after is not None
    assert after.pivot != before.pivot
    assert after.rotate != before.rotate
