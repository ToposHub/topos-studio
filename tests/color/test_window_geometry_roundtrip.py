"""Video windows retain gradient endpoints and editable curve feather data."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest
from PyQt6.QtCore import QPointF, QSize
from PyQt6.QtGui import QTransform
from PyQt6.QtWidgets import QGraphicsScene

from src.features.color.core.layer import Layer, MaskShape
from src.features.color.tracking.tracking_evaluator import (
    apply_transform_to_geometry, capture_base_geometry,
)
from src.features.color.ui.canvas import Canvas, MaskShapeItem
from tests.color.test_tracking_render_apply import make_engine_skeleton, add_translation_binding

pytestmark = pytest.mark.usefixtures("qapp")


def test_gradient_endpoint_edit_invalidates_raster_cache() -> None:
    engine = make_engine_skeleton()
    shape = MaskShape("gradient")
    shape.feather = 0.0
    before = engine._render_single_shape(shape, 500, 300, 3840)
    shape.grad_points = [[0.6, 0.5], [0.8, 0.5]]
    after = engine._render_single_shape(shape, 500, 300, 3840)
    assert np.argmin(abs(after[150] - 0.5)) - np.argmin(abs(before[150] - 0.5)) == 100


def test_gradient_tracking_moves_raster_and_overlay_together() -> None:
    engine = make_engine_skeleton()
    shape = MaskShape("gradient")
    shape.feather = 0.0
    binding = add_translation_binding(engine, shape.id, {0: 0.0, 10: 768.0})
    binding.base_geometry = capture_base_geometry(shape)
    engine.tracking_context_provider = lambda: ("clip1", 10)
    layer = Layer("L")
    layer.mask_shapes = [shape]
    canvas = SimpleNamespace(
        scene=QGraphicsScene(), logical_size=QSize(1000, 600),
        mask_items={}, active_shape_id=None, _show_mask_overlays=True,
        engine=engine, mask_shape_changed=MagicMock(),
    )
    Canvas.update_mask_overlay(canvas, layer)
    proxy = engine._tracked_shape_view(shape)
    item = canvas.mask_items[shape.id]
    for key, point in zip(("grad_start", "grad_end"), proxy.grad_points):
        scene_point = item.mapToScene(item.handles[key].pos())
        assert scene_point.x() / 1000 == pytest.approx(point[0])
        assert scene_point.y() / 600 == pytest.approx(point[1])
    assert proxy.grad_points[0] == pytest.approx([0.6, 0.5])
    assert shape.grad_points == [[0.4, 0.5], [0.6, 0.5]]
    base_mask = engine._render_single_shape(shape, 500, 300, 3840)
    tracked_mask = engine._render_mask_items_recursive([shape], 500, 300, 3840)
    # A translated linear ramp shifts its half-opacity boundary by the same 0.2.
    base_edge = np.argmin(abs(base_mask[150] - 0.5))
    track_edge = np.argmin(abs(tracked_mask[150] - 0.5))
    assert (track_edge - base_edge) / 500 == pytest.approx(0.2, abs=0.004)


def test_gradient_transform_handles_rotation_and_preserves_reference() -> None:
    shape = MaskShape("gradient")
    shape.rotation = 90.0
    base = capture_base_geometry(shape)
    assert apply_transform_to_geometry(base, np.eye(3), (1000, 600)) == base
    translation = np.array([[1.0, 0.0, 100.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
    moved = apply_transform_to_geometry(base, translation, (1000, 600))
    assert moved["rotation"] == 0.0
    assert moved["grad_points"][0] == pytest.approx([0.6, 1 / 3])
    assert moved["grad_points"][1] == pytest.approx([0.6, 2 / 3])
    assert shape.grad_points == [[0.4, 0.5], [0.6, 0.5]]
    # Legacy bindings did not contain endpoints; they must remain safe to load.
    legacy = {key: value for key, value in base.items() if key != "grad_points"}
    assert apply_transform_to_geometry(legacy, translation, (1000, 600)) == legacy


def test_curve_feather_survives_model_overlay_edit_roundtrip() -> None:
    scene = QGraphicsScene()
    canvas = SimpleNamespace(logical_size=QSize(1000, 600), active_shape_id="curve",
                             mask_shape_changed=MagicMock())
    item = MaskShapeItem("curve", "curve", canvas)
    scene.addItem(item)
    point = {"pos": [0.3, 0.4], "c1": [0.25, 0.4], "c2": [0.35, 0.4],
             "type": "smooth", "fi": 12.0, "fo": 37.0}
    item.update_from_data(QPointF(500, 300), [300, 180], 0.0, 20.0, points=[point])
    item.bezier_points[0]["pos"] += QPointF(10, 0)
    item._sync_back()
    _, data = canvas.mask_shape_changed.emit.call_args.args
    assert data["points"][0]["fi"] == 12.0
    assert data["points"][0]["fo"] == 37.0
    assert data["points"][0]["pos"] == pytest.approx([0.31, 0.4])


def test_polygon_creation_emits_corner_points_without_drag_handles() -> None:
    scene = QGraphicsScene()
    canvas = SimpleNamespace(logical_size=QSize(1000, 600), active_shape_id="poly",
                             mask_shape_changed=MagicMock(), mask_items={},
                             set_drawing_mode=MagicMock(), transform=lambda: QTransform())
    Canvas.set_mask_creation_mode(canvas, "polygon")
    item = MaskShapeItem("poly", "curve", canvas)
    scene.addItem(item)
    canvas.mask_items[item.shape_id] = item
    item.setSelected(True)
    for point in (QPointF(200, 200), QPointF(800, 200), QPointF(500, 500)):
        Canvas._handle_pen_press(canvas, point)
    assert canvas.pen_dragging is False
    for point in item.bezier_points:
        assert point["type"] == "corner"
        assert point["pos"] == point["c1"] == point["c2"]
    assert len(item.bezier_points) == 3
    with pytest.raises(ValueError):
        Canvas.set_mask_creation_mode(canvas, "invalid")


def test_history_freezes_tracking_and_restores_undo_redo(image_engine) -> None:
    engine = image_engine
    engine.img_full_original = np.zeros((60, 100, 3), dtype=np.float32)
    shape = MaskShape("rect")
    engine.layers[0].mask_shapes = [shape]
    binding = add_translation_binding(engine, shape.id, {0: 0.0, 10: 20.0})
    binding.base_geometry = capture_base_geometry(shape)
    engine.tracking_context_provider = lambda: ("clip1", 10)
    engine.history.push_state(engine.img_full_original, engine.layers, 0, copy_image=False)
    binding.base_geometry["center"][0] = 0.7
    engine.history.push_state(engine.img_full_original, engine.layers, 0, copy_image=False)
    binding.base_geometry["center"][0] = 0.9
    assert engine.undo()
    assert engine.tracking_store.get_binding("clip1", shape.id).base_geometry["center"][0] == 0.7
    assert engine.get_tracked_geometry(shape)["center"][0] == pytest.approx(0.9)
    assert engine.undo()
    assert engine.tracking_store.get_binding("clip1", shape.id).base_geometry["center"][0] == 0.5
    assert engine.get_tracked_geometry(shape)["center"][0] == pytest.approx(0.7)
    assert engine.redo()
    assert engine.tracking_store.get_binding("clip1", shape.id).base_geometry["center"][0] == 0.7
    assert engine.redo()
    assert engine.tracking_store.get_binding("clip1", shape.id).base_geometry["center"][0] == 0.9


def test_legacy_history_snapshot_keeps_existing_tracking(image_engine) -> None:
    shape = MaskShape("rect")
    binding = add_translation_binding(image_engine, shape.id, {0: 0.0})
    image_engine._restore_tracking_history_state({"layers": []})
    assert image_engine.tracking_store.get_binding("clip1", shape.id) is binding


@pytest.mark.parametrize("kind", ["curve", "gradient"])
def test_rotated_point_window_tracks_once_about_its_own_center(kind: str) -> None:
    engine = make_engine_skeleton()
    engine.img_full_original = np.zeros((600, 1000, 3), dtype=np.float32)
    shape = MaskShape(kind)
    shape.center = [0.3, 0.4]
    shape.rotation = 90.0
    shape.feather = 0.0
    shape.grad_points = [[0.2, 0.4], [0.4, 0.4]]
    shape.points = [{"pos": p, "c1": p, "c2": p, "type": "corner"}
                    for p in ([0.2, 0.3], [0.4, 0.3], [0.3, 0.6])]
    binding = add_translation_binding(engine, shape.id, {0: 0.0, 10: 100.0})
    binding.base_geometry = capture_base_geometry(shape)
    engine.tracking_context_provider = lambda: ("clip1", 10)
    proxy = engine._tracked_shape_view(shape)
    assert proxy.center == pytest.approx([0.4, 0.4])
    assert proxy.rotation == 0.0
    canvas = SimpleNamespace(scene=QGraphicsScene(), logical_size=QSize(1000, 600),
                             mask_items={}, active_shape_id=None, _show_mask_overlays=True,
                             engine=engine, mask_shape_changed=MagicMock())
    layer = Layer("L")
    layer.mask_shapes = [shape]
    Canvas.update_mask_overlay(canvas, layer)
    item = canvas.mask_items[shape.id]
    if kind == "curve":
        for node, point in zip(item.bezier_points, proxy.points):
            actual = item.mapToScene(node["pos"])
            assert [actual.x() / 1000, actual.y() / 600] == pytest.approx(point["pos"])
    else:
        for key, point in zip(("grad_start", "grad_end"), proxy.grad_points):
            actual = item.mapToScene(item.handles[key].pos())
            assert [actual.x() / 1000, actual.y() / 600] == pytest.approx(point)
    before = engine._render_single_shape(shape, 500, 300, 3840)
    after = engine._render_single_shape(proxy, 500, 300, 3840)
    # Tracked geometry and raw rotation differ only by 0.1 frame-width pan.
    assert np.mean(abs(before[:, :-50] - after[:, 50:])) < 0.015
