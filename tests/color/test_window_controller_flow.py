"""Exercise actual panel→controller→store→evaluator window editing paths."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest
from PyQt6.QtCore import QSize

from src.app.controllers.color_controller import ColorControllerMixin
from src.features.color.core.layer import Layer, MaskShape
from src.features.color.tracking.tracking_evaluator import TrackingEvaluator, capture_base_geometry
from src.features.color.tracking.tracking_store import TrackingStore
from src.features.color.ui.mask_panel import MaskPanel
from src.features.color.ui.tracker_panel import TrackerPanel
from src.shared.tracking.track_data import TrackingBinding
from src.shared.tracking.types import TrackStatus, TrackTransform

pytestmark = pytest.mark.usefixtures("qapp")


def controller(qapp):
    ctrl = ColorControllerMixin()
    shape = MaskShape("circle")
    layer = Layer("Window")
    layer.mask_shapes = [shape]
    store = TrackingStore()
    binding = TrackingBinding("binding", "clip", shape.id, source_frame_end=20)
    binding.base_geometry = capture_base_geometry(shape)
    for frame in range(21):
        matrix = np.eye(3)
        matrix[0, 2] = frame * 12.8
        binding.apply_result(TrackTransform.from_matrix(frame, matrix, 1.0, TrackStatus.VALID))
    store.add_binding(binding)
    evaluator = TrackingEvaluator(store)
    ctrl.frame = 5
    ctrl.engine = SimpleNamespace(
        tracking_store=store, img_full_original=np.zeros((720, 1280, 3), np.uint8),
        history=MagicMock(), layers=[layer], active_layer_index=0,
        get_active_layer=lambda: layer,
        get_tracked_geometry=lambda s: evaluator.evaluate_geometry("clip", s.id, ctrl.frame, (1280, 720)))
    ctrl.mask_panel = MaskPanel()
    ctrl.mask_panel.set_image_only_tools_visible(False)
    ctrl.mask_panel.update_shapes_list([shape])
    ctrl.mask_panel.select_shape(shape.id)
    ctrl.tracker_panel = TrackerPanel()
    ctrl.tracker_panel.set_bound_shape("Circle")
    ctrl.tracker_panel.set_tracking_binding(binding)
    ctrl.canvas = MagicMock()
    ctrl.canvas.active_shape_id = shape.id
    ctrl.canvas.logical_size = QSize(1280, 720)
    ctrl._tracker_shape_id = shape.id
    ctrl._tracker_render_context = lambda: ("clip", ctrl.frame)
    ctrl._tracker_frame_context = lambda: ("clip", "/tmp/translation.mp4", ctrl.frame, 0, 20)
    ctrl.request_render = MagicMock()
    ctrl._trigger_autosave = MagicMock()
    ctrl._maybe_record_mask_keyframes = MagicMock()
    ctrl.mask_update_timer = MagicMock()
    ctrl.pending_mask_updates = {}
    qapp.processEvents()
    return ctrl, shape, binding, evaluator


def test_frame_edit_does_not_modify_neighbor_or_reference(qapp):
    ctrl, shape, binding, evaluator = controller(qapp)
    ctrl.tracker_panel.set_params({"correction_mode": "frame"})
    ctrl.on_mask_shape_updated(shape.id, {"center": [.7, .5]})
    ctrl.on_mask_shape_updated(shape.id, {"opacity": .6})
    ctrl.process_mask_update()
    assert evaluator.evaluate_geometry("clip", shape.id, 5, (1280, 720))["center"] == pytest.approx([.7, .5])
    assert evaluator.evaluate_geometry("clip", shape.id, 6, (1280, 720))["center"] == pytest.approx([.56, .5])
    assert shape.center == [.5, .5]
    assert shape.opacity == .6
    assert ctrl._maybe_record_mask_keyframes.call_args.args[1] == [(shape.id, ["opacity"])]
    loaded = TrackingStore.from_dict(ctrl.engine.tracking_store.to_dict())
    assert TrackingEvaluator(loaded).evaluate_geometry("clip", shape.id, 5, (1280, 720))["center"] == pytest.approx([.7, .5])


def test_clip_edit_preserves_tracking_motion(qapp):
    ctrl, shape, binding, evaluator = controller(qapp)
    ctrl.tracker_panel.set_params({"correction_mode": "clip"})
    ctrl.on_mask_shape_updated(shape.id, {"center": [.65, .5]})
    ctrl.process_mask_update()
    assert evaluator.evaluate_geometry("clip", shape.id, 5, (1280, 720))["center"] == pytest.approx([.65, .5])
    assert evaluator.evaluate_geometry("clip", shape.id, 6, (1280, 720))["center"] == pytest.approx([.66, .5])
    assert len(binding.transforms) == 21


def test_restart_at_playhead_keeps_existing_results_and_basis(qapp):
    ctrl, shape, binding, evaluator = controller(qapp)
    original = dict(binding.transforms)
    original_base = dict(binding.base_geometry)
    ctrl.frame = 8
    found, context = ctrl._ensure_tracker_binding(shape)
    assert found is binding
    assert context[3] == 8
    assert binding.reference_frame == 0
    assert binding.transforms == original
    assert binding.base_geometry["center"] == original_base["center"]
    assert ctrl._tracker_reference_matrix[0, 2] == pytest.approx(102.4)


def test_pan_checkbox_changes_evaluated_motion(qapp):
    ctrl, shape, binding, evaluator = controller(qapp)
    params = ctrl.tracker_panel.get_params()
    params["channels"]["tx"] = False
    ctrl._on_tracker_params_changed(params)
    assert evaluator.evaluate_geometry("clip", shape.id, 5, (1280, 720))["center"] == pytest.approx([.5, .5])
    assert binding.transforms[5].translation[0] == pytest.approx(64)


def test_point_detector_uses_inserted_and_excluded_locations():
    from src.features.color.tracking.window_points import WindowPointDetector
    from src.shared.tracking.feature_detector import DetectorConfig
    image = np.random.default_rng(4).integers(0, 255, (100, 100), dtype=np.uint8)
    detector = WindowPointDetector(DetectorConfig(), seeds=((.5, .5),), point_mode=True)
    assert detector.detect(image, (20, 30, 40, 20)).tolist() == [[40.0, 40.0]]
    # Redetection follows the moved region instead of jumping to old pixels.
    assert detector.detect(image, (30, 35, 40, 20)).tolist() == [[50.0, 45.0]]
    excluded = WindowPointDetector(DetectorConfig(), excluded=((.5, .5),))
    points = excluded.detect(image, (20, 30, 40, 20))
    assert np.all(np.linalg.norm(points - [40, 40], axis=1) > 5)


def test_insert_and_delete_tracking_point_persist_reference_coordinates(qapp):
    ctrl, shape, binding, evaluator = controller(qapp)
    ctrl._on_tracker_feature_inserted(.7, .5)
    user_points = binding.base_geometry["_user_points"]
    fid = int(next(iter(user_points)))
    assert user_points[str(fid)] == pytest.approx([.65, .5])
    assert fid in binding.locked_feature_ids
    ctrl.canvas.tracker_overlay.selected_feature_id = fid
    ctrl._on_tracker_delete_point_requested()
    assert str(fid) not in user_points
    assert fid not in binding.locked_feature_ids
    assert fid in binding.excluded_feature_ids
    assert binding.base_geometry["_excluded_points"][0] == pytest.approx([.65, .5])


def test_tracking_pattern_drag_obeys_frame_edit_mode(qapp):
    ctrl, shape, binding, evaluator = controller(qapp)
    ctrl.tracker_panel.set_params({"correction_mode": "frame"})
    ctrl._on_tracker_pattern_committed((.3, .3, .4, .4))
    geometry = evaluator.evaluate_geometry("clip", shape.id, 5, (1280, 720))
    assert geometry["center"] == pytest.approx([.5, .5])
    assert geometry["size"] == pytest.approx([.2, .2])
    assert evaluator.evaluate_geometry("clip", shape.id, 6, (1280, 720))["center"] == pytest.approx([.56, .5])
    assert shape.size == [.3, .3]


def test_interaction_end_commits_final_mask_value_before_autosave():
    from src.app.controllers.menu_toolbar_controller import MenuToolbarMixin
    events = []
    host = SimpleNamespace(
        pending_mask_updates={"window": {"opacity": .5}},
        process_mask_update=lambda: events.append("commit"),
        idle_render_timer=SimpleNamespace(start=lambda: events.append("idle")),
        _trigger_autosave=lambda: events.append("save"))
    MenuToolbarMixin.on_interaction_ended(host)
    assert events == ["commit", "idle", "save"]


def test_undo_invalidates_worker_before_refreshing_restored_ui():
    from src.app.controllers.layer_controller import LayerOpsMixin
    events = []
    host = SimpleNamespace(
        tracking_service=SimpleNamespace(invalidate=lambda: events.append("invalidate")),
        update_canvas=lambda: events.append("canvas"),
        engine=SimpleNamespace(layers=[], active_layer_index=-1, get_active_layer=lambda: None),
        canvas=MagicMock(active_shape_id=None), layer_panel=MagicMock(),
        _on_tracker_shape_selected=lambda shape_id: events.append(("selection", shape_id)),
        _refresh_grade_keyframe_ui=lambda: None)
    LayerOpsMixin._sync_ui_after_undo_redo(host)
    assert events[0] == "invalidate"
    assert ("selection", None) in events
    assert host._tracker_single_step_target is None
