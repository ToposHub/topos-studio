"""Coverage tests for ``src/app/controllers/color_controller.py``.

The controller is a mixin on MainWindow.  Following the conventions of
``tests/app/test_dock_layout_controller_coverage.py`` the units under test
are exercised on a minimal ``QMainWindow + ColorControllerMixin`` host with
real offscreen Qt widgets where meaningful (status bar, progress bar, spin
boxes for the signal anti-recursion lock) and fakes / ``MagicMock`` for the
engine and panels (no GL, no engine threads, no modal dialogs):

* mask shape add / remove / duplicate / copy-to-layer, group packing and
  hierarchy restructuring (data tree + dirty flags + UI refresh calls)
* the undo snapshot commit boundary: which mutations push history state and
  which silently do not (review finding — mask add/remove never do)
* mask traversal type safety: ``_iter_trackable_shapes`` and the brush
  flows (``_iter_brush_shapes``) flatten groups with ``getattr`` guards
  (AUD-2301 / BUG-C4-02 fixed — behaviour-asserted here)
* pending-mask-update debounce timer → ``process_mask_update`` → render
  request, including mask keyframe recording semantics
* the brush panel signal anti-recursion lock (``blockSignals`` around
  programmatic UI writes, verified with real Qt spin boxes / tool buttons)
* LUT apply / intensity / reset / remove / decompile state switching
  (param change → render request routing, paused vs playing debounce)
* denoise / deblur requested-finished-error-reset flows (``FileIO``
  neutralised), crop tool flows, SAM / BiRefNet toggles
* tracker helpers and binding flows (geometry math, dock-shown fallback,
  pattern / search commit, task lifecycle slots)

Suspected src bugs found while writing these tests are recorded in
``docs/testcoverage/2026-09-28/BUGS-agent-C4.md`` (BUG-C4-xx); nothing in
src is modified here.  Where a test pins the *current* behaviour of a
suspected bug it references the matching BUG-C4-xx number in a comment so
the assertion gets updated together with the fix.
"""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Tuple

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (QApplication, QDoubleSpinBox, QMainWindow,
                             QProgressBar, QToolButton)
from unittest.mock import MagicMock

from src.app.controllers import color_controller as cc
from src.app.controllers.color_controller import ColorControllerMixin
from src.features.color.core.grade_keyframes import (
    MASK_KEYFRAME_FIELDS,
    mask_track_name,
)
from src.features.color.core.layer import Layer, MaskGroup, MaskShape
from src.features.color.tracking.types import TrackingMode
from src.shared.i18n.i18n import I18N
from src.shared.tracking.track_data import TrackingBinding
from src.shared.video.denoise.params import VideoDenoiseParams


# ═══════════════════════════════════════════════════════════════════════
# Fakes
# ═══════════════════════════════════════════════════════════════════════


class _FakeSignal:
    """Minimal Qt-signal stand-in that counts ``emit()`` calls."""

    def __init__(self) -> None:
        self.calls: int = 0

    def emit(self, *args: Any) -> None:
        self.calls += 1


class _FakeHistory:
    """HistoryManager stand-in recording ``push_state`` snapshots."""

    def __init__(self) -> None:
        self.pushed: List[Tuple[Any, Any, int, bool]] = []

    def push_state(self, img: Any, layers: Any, active_index: int,
                   copy_image: bool = True) -> None:
        self.pushed.append((img, layers, active_index, copy_image))


class _FakeDock:
    """Dock stand-in with a controllable visibility flag."""

    def __init__(self, visible: bool = False) -> None:
        self._visible: bool = visible
        self.visible_calls: List[bool] = []
        self.raised: int = 0

    def isVisible(self) -> bool:
        return self._visible

    def setVisible(self, visible: bool) -> None:
        self._visible = visible
        self.visible_calls.append(visible)

    def raise_(self) -> None:
        self.raised += 1


class _FakeVideoController:
    """Video controller stand-in for tracker frame navigation."""

    def __init__(self, current_frame: Any = None,
                 total_frames: Any = None) -> None:
        self.current_frame: Any = current_frame
        self.total_frames: Any = total_frames
        self.seeks: List[int] = []

    def seek_to_frame(self, frame: int) -> None:
        self.seeks.append(int(frame))


class _FakeSessionManager:
    """``_color_session_manager`` stand-in returning a fixed session."""

    def __init__(self, session: Any) -> None:
        self.session: Any = session

    def get_current_session(self) -> Any:
        if isinstance(self.session, Exception):
            raise self.session
        return self.session


class _FakeEngine:
    """ImageEngine stand-in: real Layers, recorded engine-side effects."""

    def __init__(self, layers: Optional[List[Layer]] = None) -> None:
        self.layers: List[Layer] = layers if layers is not None else [Layer("Main")]
        self.active_layer_index: int = 0 if self.layers else -1
        self.history: _FakeHistory = _FakeHistory()
        self.mask_shapes_changed: _FakeSignal = _FakeSignal()
        self.img_full_original: Optional[np.ndarray] = np.zeros(
            (4, 8, 3), dtype=np.float32)
        self.img_source_full: Optional[np.ndarray] = self.img_full_original
        self.current_grade_frame: Optional[int] = None
        self.current_file_path: str = ""
        self.video_denoise_params: Optional[VideoDenoiseParams] = None
        self.tracking_store: MagicMock = MagicMock()
        self.renderer: Optional[MagicMock] = MagicMock()
        self.source_bake_file_name: Optional[str] = None
        self.source_bake_type: Optional[str] = None
        self.current_raw_params: Dict[str, Any] = {"enabled": False}
        self.refresh_tracking_masks: Any = None  # set per test

        self.lut_intensity_calls: List[Any] = []
        self.apply_lut_calls: List[str] = []
        self.apply_lut_result: bool = True
        self.reset_lut_calls: int = 0
        self.preview_calls: List[Tuple[Any, ...]] = []
        self.request_birefnet_calls: int = 0
        self.sam_encode_calls: int = 0
        self.reset_sam_calls: int = 0
        self.set_reference_calls: List[Any] = []
        self.set_image_calls: List[Dict[str, Any]] = []
        self.crop_calls: List[Dict[str, Any]] = []
        self.decompile_calls: List[str] = []
        self.decompile_result: Dict[str, Any] = {}
        self.process_ai_calls: List[Tuple[int, str]] = []
        self.raw_calls: List[Dict[str, Any]] = []

    def get_active_layer(self) -> Optional[Layer]:
        if 0 <= self.active_layer_index < len(self.layers):
            return self.layers[self.active_layer_index]
        return None

    # -- recorded engine API ------------------------------------------------
    def set_lut_intensity(self, value: Any) -> None:
        self.lut_intensity_calls.append(value)

    def apply_lut(self, path: str) -> bool:
        self.apply_lut_calls.append(path)
        return self.apply_lut_result

    def reset_lut(self) -> None:
        self.reset_lut_calls += 1

    def set_mask_preview_mode(self, enabled: bool, style: Any = None) -> None:
        self.preview_calls.append((enabled, style) if style is not None else (enabled,))

    def request_birefnet(self) -> None:
        self.request_birefnet_calls += 1

    def request_sam_encode(self) -> None:
        self.sam_encode_calls += 1

    def reset_sam_points(self) -> None:
        self.reset_sam_calls += 1

    def set_reference_image(self, image: Any) -> None:
        self.set_reference_calls.append(image)

    def set_image(self, full: Any, high: Any, low: Any,
                  is_new_file: bool = True) -> None:
        self.set_image_calls.append(
            {"full": full, "high": high, "low": low, "is_new_file": is_new_file})

    def apply_crop(self, params: Dict[str, Any]) -> None:
        self.crop_calls.append(params)

    def decompile_lut(self, path: str) -> Dict[str, Any]:
        self.decompile_calls.append(path)
        return self.decompile_result

    def process_ai_plugin(self, layer_index: int, instance_id: str) -> None:
        self.process_ai_calls.append((layer_index, instance_id))

    def on_raw_params_changed(self, params: Dict[str, Any]) -> None:
        self.raw_calls.append(params)


class _ColorHost(QMainWindow, ColorControllerMixin):
    """Minimal MainWindow-shaped host exercising the color controller mixin.

    Panels / canvas are ``MagicMock`` (call recording); the engine is a
    ``_FakeEngine`` over real ``Layer`` objects.  Real Qt widgets are used
    for the status bar, progress bar and (per test) mask panel drags.
    """

    def __init__(self) -> None:
        QMainWindow.__init__(self)
        self.setWindowTitle("ColorHost")
        self.status_bar = self.statusBar()
        self.engine = _FakeEngine()
        self.canvas = MagicMock()
        self.canvas.active_shape_id = None
        self.canvas.get_crop_data.return_value = None
        self.mask_panel = MagicMock()
        self.layer_panel = MagicMock()
        self.qualifier_panel = MagicMock()
        self.crop_panel = MagicMock()
        self.crop_panel.crop_active = False
        self.grading_panel = MagicMock()
        self.grading_panel.keyframe_mode = False
        self.denoise_panel = MagicMock()
        self.deblur_panel = MagicMock()
        self.plugin_panel = MagicMock()
        self.progress_bar = QProgressBar()
        self.dock_plugin = _FakeDock(False)
        self.dock_tracker = _FakeDock(False)
        self.tracker_panel = MagicMock()
        self.tracker_panel.feature_spin.value.return_value = 12
        self.tracker_panel.get_params.return_value = {}
        self.tracking_service = MagicMock()
        self.tracking_service.task_key.side_effect = lambda c, s: f"{c}:{s}"

        self.pending_mask_updates: Dict[str, Dict[str, Any]] = {}
        self.mask_update_timer = QTimer()
        self.mask_update_timer.setSingleShot(True)
        self.mask_update_timer.setInterval(0)
        self.mask_update_timer.timeout.connect(self.process_mask_update)

        self.current_file_path: Optional[str] = None
        self.video_controller: Any = None
        self._current_timeline_sequences: Dict[str, Any] = {}
        self._color_session_manager: Any = None
        self._tracker_shape_id: Optional[str] = None
        self._tracker_params: Optional[Dict[str, Any]] = None
        self._last_color_source_frame: Any = None
        self._playing: bool = False

        # MainWindow helpers the mixin relies on --------------------------------
        self.render_calls: int = 0
        self.autosave_calls: int = 0
        self.debounce_calls: int = 0
        self.invalidate_calls: int = 0
        self.denoise_window_calls: int = 0
        self.update_canvas_calls: List[bool] = []
        self.update_from_layer_calls: List[Layer] = []
        self.keyframe_ui_refreshes: int = 0

    # -- MainWindow surface used by the mixin --------------------------------
    def request_render(self) -> None:
        self.render_calls += 1

    def _trigger_autosave(self) -> None:
        self.autosave_calls += 1

    def _is_video_playing(self) -> bool:
        return self._playing

    def _debounce_grading_render(self) -> None:
        self.debounce_calls += 1

    def _invalidate_video_render_params_if_playing(self) -> None:
        self.invalidate_calls += 1

    def _schedule_video_denoise_window(self) -> None:
        self.denoise_window_calls += 1

    def update_canvas(self, is_new_source: bool = False) -> None:
        self.update_canvas_calls.append(is_new_source)

    def update_from_layer(self, layer: Layer) -> None:
        self.update_from_layer_calls.append(layer)

    def _refresh_grade_keyframe_ui(self) -> None:
        self.keyframe_ui_refreshes += 1


@pytest.fixture
def host(qapp) -> Iterator[_ColorHost]:
    h = _ColorHost()
    yield h
    h.mask_update_timer.stop()
    h.close()
    h.deleteLater()


@pytest.fixture
def layer(host: _ColorHost) -> Layer:
    """The host engine's single active layer."""
    lyr = host.engine.get_active_layer()
    assert lyr is not None
    return lyr


def _flush_timer(qapp: QApplication, host: _ColorHost) -> None:
    """Deliver the pending 0-interval single-shot mask update timer."""
    assert host.mask_update_timer.isActive()
    for _ in range(10):
        qapp.processEvents()


def _add_shape(lyr: Layer, shape_type: str = "circle") -> MaskShape:
    shape = MaskShape(shape_type)
    lyr.mask_shapes.append(shape)
    return shape


def _make_session(clip_id: str = "", source_path: str = "",
                  source_frame: Any = None, timeline_id: str = "") -> Any:
    return SimpleNamespace(
        clip_id=clip_id, source_path=source_path,
        source_frame=source_frame, timeline_id=timeline_id)


# ═══════════════════════════════════════════════════════════════════════
# A. Mask shape add / remove
# ═══════════════════════════════════════════════════════════════════════


def test_mask_shape_added_appends_and_activates(host: _ColorHost,
                                                layer: Layer) -> None:
    host.on_mask_shape_added("circle")

    assert len(layer.mask_shapes) == 1
    shape = layer.mask_shapes[0]
    assert isinstance(shape, MaskShape) and shape.type == "circle"
    assert layer.is_dirty and layer.mask_dirty
    host.mask_panel.btn_brush.setChecked.assert_called_once_with(False)
    host.mask_panel.btn_sam.setChecked.assert_called_once_with(False)
    host.mask_panel.update_shapes_list.assert_called_once_with(layer.mask_shapes)
    host.canvas.update_mask_overlay.assert_called_once_with(layer)
    host.canvas.set_active_shape.assert_called_once_with(shape.id)
    host.mask_panel.select_shape.assert_called_once_with(shape.id)
    host.layer_panel.update_layers.assert_called_once_with(
        host.engine.layers, host.engine.active_layer_index)
    # Circle is not the pen tool → drawing mode off.
    host.canvas.set_drawing_mode.assert_called_once_with(False)
    assert host.render_calls == 1
    assert host.autosave_calls == 1


def test_mask_shape_added_curve_engages_pen_tool(host: _ColorHost,
                                                 layer: Layer) -> None:
    host.on_mask_shape_added("curve")
    host.canvas.set_drawing_mode.assert_called_once_with(True)
    assert host.status_bar.currentMessage() == "Pen Tool Active"


def test_mask_shape_added_nests_into_selected_group(host: _ColorHost,
                                                    layer: Layer) -> None:
    group = MaskGroup("G")
    layer.mask_shapes.append(group)
    host.canvas.active_shape_id = group.id

    host.on_mask_shape_added("rect")

    assert len(layer.mask_shapes) == 1, "root list must stay unchanged"
    assert len(group.children) == 1
    assert isinstance(group.children[0], MaskShape)
    host.canvas.set_active_shape.assert_called_once_with(group.children[0].id)


def test_mask_shape_added_without_layer_is_inert(host: _ColorHost) -> None:
    host.engine.active_layer_index = -1
    host.on_mask_shape_added("circle")
    assert host.engine.layers[0].mask_shapes == []
    assert host.render_calls == 0
    assert host.autosave_calls == 0


def test_mask_shape_removed_deletes_nested_and_unbinds(host: _ColorHost,
                                                       layer: Layer) -> None:
    group = MaskGroup("G")
    child = MaskShape("circle")
    group.add_child(child)
    layer.mask_shapes.append(group)
    host.canvas.active_shape_id = child.id

    host.on_mask_shape_removed(child.id)

    assert layer.mask_shapes == [group]
    assert group.children == []
    assert layer.is_dirty and layer.mask_dirty
    host.canvas.set_active_shape.assert_called_once_with(None)
    host.canvas.set_drawing_mode.assert_called_once_with(False)
    host.mask_panel.update_shapes_list.assert_called_once_with(
        list(layer.mask_shapes))
    # Tracker follows the deletion: bound shape cleared (group left → None).
    host.tracker_panel.set_bound_shape.assert_called_once_with(None)
    assert host._tracker_shape_id is None
    assert host.render_calls == 1
    assert host.autosave_calls == 1


def test_mask_shape_removed_unknown_id_is_noop(host: _ColorHost,
                                               layer: Layer) -> None:
    _add_shape(layer)
    before = list(layer.mask_shapes)

    host.on_mask_shape_removed(str(uuid.uuid4()))

    assert layer.mask_shapes == before
    assert host.render_calls == 0
    host.canvas.set_drawing_mode.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# B. Undo snapshot commit boundary (review finding)
# ═══════════════════════════════════════════════════════════════════════


def test_mask_add_remove_clear_group_hierarchy_push_undo_state(
        host: _ColorHost, layer: Layer) -> None:
    """BUG-C4-01 (fixed): structural mask edits commit undo snapshots.

    ``on_mask_shape_added`` / ``on_mask_shape_removed`` /
    ``on_all_masks_cleared`` / ``on_mask_group_added`` /
    ``on_mask_hierarchy_updated`` each push a pre-mutation snapshot
    (``copy_image=False``, same pattern as ``on_duplicate_mask_shape``)
    before mutating the mask tree, so mask creation, deletion, clearing,
    packing and hierarchy drags are all undoable.
    """
    _add_shape(layer)

    host.on_mask_shape_removed(layer.mask_shapes[0].id)
    assert len(host.engine.history.pushed) == 1, (
        "mask removal must commit an undo snapshot (BUG-C4-01)")
    assert layer.mask_shapes == []

    host.on_mask_shape_added("circle")
    assert len(host.engine.history.pushed) == 2, (
        "mask creation must commit an undo snapshot (BUG-C4-01)")
    assert len(layer.mask_shapes) == 1

    host.on_all_masks_cleared()
    assert len(host.engine.history.pushed) == 3, (
        "clearing all masks must commit an undo snapshot (BUG-C4-01)")
    assert layer.mask_shapes == []

    host.on_mask_group_added([str(uuid.uuid4())])
    assert len(host.engine.history.pushed) == 4, (
        "group creation must commit an undo snapshot (BUG-C4-01)")

    host.on_mask_hierarchy_updated([{"id": layer.mask_shapes[0].id}])
    assert len(host.engine.history.pushed) == 5, (
        "hierarchy rebuild must commit an undo snapshot (BUG-C4-01)")

    for _img, _layers, _idx, copy_image in host.engine.history.pushed:
        assert copy_image is False, (
            "mask-only snapshots must not copy the image")


def test_clear_all_masks_without_content_commits_no_snapshot(
        host: _ColorHost, layer: Layer) -> None:
    """BUG-C4-01 follow-up: an empty clear must not add a no-op undo step."""
    assert layer.mask_shapes == []

    host.on_all_masks_cleared()

    assert host.engine.history.pushed == []


def test_duplicate_mask_shape_pushes_undo_and_reassigns_ids(
        host: _ColorHost, layer: Layer) -> None:
    source = _add_shape(layer, "rect")
    source.opacity = 0.7
    source.locked = True

    host.on_duplicate_mask_shape(source.id)

    assert len(host.engine.history.pushed) == 1
    img, layers, idx, copy_image = host.engine.history.pushed[0]
    assert img is host.engine.img_full_original
    assert layers is host.engine.layers
    assert idx == host.engine.active_layer_index
    assert copy_image is False, "mask-only snapshots must not copy the image"

    assert len(layer.mask_shapes) == 2
    clone = layer.mask_shapes[1]
    assert isinstance(clone, MaskShape)
    assert clone is not source and clone.id != source.id
    assert clone.opacity == 0.7 and clone.locked is True, (
        "duplicate must preserve all attributes")
    host.canvas.set_active_shape.assert_called_once_with(clone.id)
    assert layer.is_dirty and layer.mask_dirty
    assert host.render_calls == 1


def test_duplicate_mask_shape_group_goes_to_root_with_fresh_child_ids(
        host: _ColorHost, layer: Layer) -> None:
    group = MaskGroup("G")
    child = MaskShape("circle")
    group.add_child(child)
    layer.mask_shapes.append(group)

    host.on_duplicate_mask_shape(group.id)

    assert len(layer.mask_shapes) == 2, "group clone is appended at root level"
    clone = layer.mask_shapes[1]
    assert isinstance(clone, MaskGroup)
    assert clone.id != group.id
    assert len(clone.children) == 1
    assert clone.children[0].id != child.id, "nested ids must be renewed too"
    assert len(host.engine.history.pushed) == 1


def test_duplicate_mask_shape_missing_source_is_noop(host: _ColorHost,
                                                     layer: Layer) -> None:
    host.on_duplicate_mask_shape(str(uuid.uuid4()))
    assert layer.mask_shapes == []
    assert host.engine.history.pushed == []
    assert host.render_calls == 0


def test_duplicate_nested_shape_appends_clone_to_root(
        host: _ColorHost, layer: Layer) -> None:
    """A shape nested in a group is not in the root list, so the clone lands
    at root level (``ValueError`` fallback in ``index()``)."""
    group = MaskGroup("G")
    child = MaskShape("circle")
    child.opacity = 0.42
    group.add_child(child)
    layer.mask_shapes.append(group)

    host.on_duplicate_mask_shape(child.id)

    assert group.children == [child], "the nested original stays in place"
    assert len(layer.mask_shapes) == 2
    clone = layer.mask_shapes[1]
    assert isinstance(clone, MaskShape)
    assert clone.id != child.id
    assert clone.opacity == 0.42


class _PickingAction:
    """Hashable QAction stand-in."""

    def __init__(self, text: str) -> None:
        self.text: str = text


class _PickingMenu:
    """QMenu stand-in whose ``exec`` immediately picks the first action."""

    cancel: bool = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.actions: List[_PickingAction] = []

    def addAction(self, text: str) -> _PickingAction:
        action = _PickingAction(text)
        self.actions.append(action)
        return action

    def exec(self, pos: Any) -> Any:
        if _PickingMenu.cancel:
            return None
        return self.actions[0] if self.actions else None


def test_copy_masks_to_layer_clones_every_shape(
        host: _ColorHost, monkeypatch: pytest.MonkeyPatch) -> None:
    source = Layer("Src")
    target = Layer("Dst")
    host.engine.layers = [source, target]
    host.engine.active_layer_index = 0
    s1 = _add_shape(source, "circle")
    s2 = _add_shape(source, "rect")
    monkeypatch.setattr("PyQt6.QtWidgets.QMenu", _PickingMenu)

    host.on_copy_masks_to_layer_requested()

    assert len(host.engine.history.pushed) == 1
    assert host.engine.history.pushed[0][3] is False
    assert [type(s) for s in target.mask_shapes] == [MaskShape, MaskShape]
    clone_ids = [s.id for s in target.mask_shapes]
    assert clone_ids != [s1.id, s2.id], "clones must receive fresh ids"
    assert [s.type for s in target.mask_shapes] == ["circle", "rect"]
    assert target.is_dirty and target.mask_dirty
    # Source stays the active layer → panel keeps showing the source list.
    host.mask_panel.update_shapes_list.assert_called_once_with(source.mask_shapes)
    assert "2" in host.status_bar.currentMessage()
    assert "Dst" in host.status_bar.currentMessage()
    assert host.render_calls == 1


def test_copy_masks_to_layer_menu_cancelled_commits_nothing(
        host: _ColorHost, monkeypatch: pytest.MonkeyPatch) -> None:
    source = Layer("Src")
    host.engine.layers = [source, Layer("Dst")]
    host.engine.active_layer_index = 0
    _add_shape(source, "circle")
    _PickingMenu.cancel = True
    monkeypatch.setattr("PyQt6.QtWidgets.QMenu", _PickingMenu)
    try:
        host.on_copy_masks_to_layer_requested()
    finally:
        _PickingMenu.cancel = False

    assert host.engine.history.pushed == []
    assert host.engine.layers[1].mask_shapes == []
    assert host.render_calls == 0


def test_copy_masks_to_layer_without_shapes_or_targets(
        host: _ColorHost) -> None:
    host.on_copy_masks_to_layer_requested()
    assert host.status_bar.currentMessage() == I18N.tr(
        "mask.copy_no_windows", "当前图层没有可复制的窗口")

    host.engine.layers.append(Layer("Other"))
    host.on_copy_masks_to_layer_requested()
    # Still no shapes on the active layer → same early status path.
    assert host.status_bar.currentMessage() == I18N.tr(
        "mask.copy_no_windows", "当前图层没有可复制的窗口")

    _add_shape(host.engine.layers[0])
    host.engine.layers = [host.engine.layers[0]]
    host.on_copy_masks_to_layer_requested()
    assert host.status_bar.currentMessage() == I18N.tr(
        "mask.copy_no_layers", "没有其他图层可接收窗口")
    assert host.engine.history.pushed == []
    assert host.render_calls == 0


# ═══════════════════════════════════════════════════════════════════════
# C. Mask traversal type safety (review Critical: MaskGroup has no `type`)
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("flow", [
    "brush_toggled",
    "brush_params_changed",
    "brush_stroke_added",
], ids=lambda f: f)
def test_brush_flows_are_group_safe_on_root_level_mask_group(
        host: _ColorHost, layer: Layer, flow: str) -> None:
    """AUD-2301 / BUG-C4-02 (fixed): root-level ``MaskGroup`` must not crash
    brush flows, and a brush inside a group is found recursively.

    ``MaskGroup`` does not define a ``type`` attribute (only the class
    constant ``TYPE``).  The flows now traverse via ``_iter_brush_shapes``
    (recursive flatten + ``getattr`` guard), so a brush packed into a group
    is reused instead of raising ``AttributeError`` / duplicating a fresh
    root-level brush.
    """
    group = MaskGroup("G")
    grouped_brush = MaskShape(MaskShape.BRUSH)
    group.add_child(grouped_brush)
    layer.mask_shapes.append(group)
    host.canvas.active_shape_id = grouped_brush.id

    if flow == "brush_toggled":
        host.on_brush_toggled(True)
        # Brush inside the group is reused; no duplicate at root level.
        assert len(layer.mask_shapes) == 1
        host.mask_panel.update_shapes_list.assert_not_called()
        assert host.status_bar.currentMessage().startswith("Brush Tool Active")
    elif flow == "brush_params_changed":
        host.on_brush_params_changed(
            {"size": 30, "feather": 0, "opacity": 1.0, "flow": 1.0,
             "is_eraser": False})
        assert grouped_brush.brush_size == 30
        assert grouped_brush.brush_flow == 1.0
    else:
        host.on_brush_stroke_added(
            [(0, 0, 1.0)], {"size": 30, "feather": 0, "opacity": 1.0,
                            "flow": 1.0, "is_eraser": False})
        assert len(grouped_brush.brush_strokes) == 1
        assert len(layer.mask_shapes) == 1, "no duplicate brush created"
        assert layer.mask_dirty and layer.is_dirty
        assert host.render_calls == 1
        assert host.autosave_calls == 1


def test_brush_toggled_off_is_group_safe_and_only_messages(
        host: _ColorHost, layer: Layer) -> None:
    layer.mask_shapes.append(MaskGroup("G"))
    host.on_brush_toggled(False)
    host.canvas.set_brush_mode.assert_called_once_with(False)
    assert host.status_bar.currentMessage() == "Brush Tool Deactivated"


def test_brush_toggled_on_creates_brush_shape_when_missing(
        host: _ColorHost, layer: Layer) -> None:
    host.canvas.active_shape_id = None
    host.on_brush_toggled(True)

    host.canvas.set_brush_mode.assert_called_once_with(True)
    host.mask_panel.btn_sam.setChecked.assert_called_once_with(False)
    host.qualifier_panel.set_picker_active.assert_called_once_with(False)
    assert len(layer.mask_shapes) == 1
    brush = layer.mask_shapes[0]
    assert brush.type == "brush" and brush.name == "Brush Mask"
    assert layer.mask_dirty and layer.is_dirty
    host.mask_panel.update_shapes_list.assert_called_once_with(layer.mask_shapes)
    host.canvas.set_active_shape.assert_called_once_with(brush.id)
    assert host.status_bar.currentMessage().startswith("Brush Tool Active")


def test_brush_toggled_on_reuses_existing_brush_shape(
        host: _ColorHost, layer: Layer) -> None:
    brush = _add_shape(layer, "brush")
    host.on_brush_toggled(True)
    assert len(layer.mask_shapes) == 1, "no duplicate brush shapes"
    host.mask_panel.update_shapes_list.assert_not_called()


def test_brush_params_changed_updates_active_brush_shape(
        host: _ColorHost, layer: Layer) -> None:
    brush = _add_shape(layer, "brush")
    host.canvas.active_shape_id = brush.id
    params = {"size": 33, "feather": 5, "opacity": 0.7, "flow": 0.9,
              "is_eraser": True}

    host.on_brush_params_changed(params)

    host.canvas.set_brush_params.assert_called_once_with(params)
    host.canvas.brush_cursor.set_eraser_mode.assert_called_once_with(True)
    assert brush.brush_size == 33
    assert brush.brush_feather == 5
    assert brush.brush_opacity == 0.7
    assert brush.brush_flow == 0.9


def test_brush_stroke_added_appends_stroke_and_marks_dirty(
        host: _ColorHost, layer: Layer) -> None:
    points = [(1, 2, 0.5), (3, 4, 0.8)]
    params = {"size": 21, "feather": 2, "opacity": 0.4, "flow": 0.6,
              "is_eraser": False}

    host.on_brush_stroke_added(points, params)

    assert len(layer.mask_shapes) == 1
    brush = layer.mask_shapes[0]
    assert brush.type == "brush"
    assert len(brush.brush_strokes) == 1
    stroke = brush.brush_strokes[0]
    assert stroke["points"] == points
    assert stroke["size"] == 21 and stroke["is_eraser"] is False
    assert brush.brush_size == 21 and brush.brush_flow == 0.6
    assert layer.mask_dirty and layer.is_dirty
    host.canvas.set_active_shape.assert_called_once_with(brush.id)
    assert host.render_calls == 1
    assert host.autosave_calls == 1


def test_iter_trackable_shapes_is_group_and_type_safe(
        host: _ColorHost, layer: Layer) -> None:
    """``_iter_trackable_shapes`` must flatten groups via ``getattr`` guards."""
    circle = _add_shape(layer, "circle")
    bitmap = _add_shape(layer, "bitmap")
    brush = _add_shape(layer, "brush")
    group = MaskGroup("G")
    inner = MaskShape("curve")
    nested_group = MaskGroup("N")
    nested_group.add_child(MaskShape("gradient"))
    group.add_child(inner)
    group.add_child(nested_group)
    layer.mask_shapes.extend([group, bitmap, brush])

    shapes = host._iter_trackable_shapes()

    assert [s.id for s in shapes] == [circle.id, inner.id,
                                      nested_group.children[0].id]


def test_iter_trackable_shapes_without_layer(host: _ColorHost) -> None:
    host.engine.active_layer_index = -1
    assert host._iter_trackable_shapes() == []


def test_all_masks_cleared_clears_every_layer_and_ai_mattes(
        host: _ColorHost) -> None:
    other = Layer("Other")
    host.engine.layers.append(other)
    for lyr in host.engine.layers:
        _add_shape(lyr)
        lyr.ai_matte = np.ones((2, 2), dtype=np.float32)
        lyr.ai_matte_enabled = True

    host.on_all_masks_cleared()

    for lyr in host.engine.layers:
        assert lyr.mask_shapes == []
        assert lyr.ai_matte is None
        assert lyr.ai_matte_enabled is False
        assert lyr.mask_dirty and lyr.is_dirty
    assert host.engine.mask_shapes_changed.calls == 1
    assert host.engine.reset_sam_calls == 1
    host.canvas.set_sam_mode.assert_called_once_with(False)
    host.canvas.set_active_shape.assert_called_once_with(None)
    host.canvas.set_drawing_mode.assert_called_once_with(False)
    host.tracker_panel.set_bound_shape.assert_called_once_with(None)
    host.mask_panel.update_shapes_list.assert_called_once_with([])
    assert host.render_calls == 1
    assert host.autosave_calls == 1
    assert host.status_bar.currentMessage() == "All masks cleared"


# ═══════════════════════════════════════════════════════════════════════
# D. Mask group packing & hierarchy restructuring
# ═══════════════════════════════════════════════════════════════════════


def test_mask_group_added_packs_selected_items_in_tree_order(
        host: _ColorHost, layer: Layer) -> None:
    s1 = _add_shape(layer, "circle")
    group = MaskGroup("Old")
    child = MaskShape("rect")
    group.add_child(child)
    s2 = _add_shape(layer, "curve")
    layer.mask_shapes.insert(1, group)

    # Selection order (child first) must NOT dictate the packed order.
    host.on_mask_group_added([s2.id, child.id])

    assert len(layer.mask_shapes) == 3  # s1, the old group, the new group
    assert [item.id for item in layer.mask_shapes[:2]] == [s1.id, group.id]
    new_group = layer.mask_shapes[-1]
    assert isinstance(new_group, MaskGroup)
    assert [item.id for item in new_group.children] == [child.id, s2.id], (
        "packed order follows the tree, not the selection order")
    assert layer.is_dirty and layer.mask_dirty
    host.canvas.set_active_shape.assert_called_once_with(new_group.id)
    host.mask_panel.select_shape.assert_called_once_with(new_group.id)
    assert host.render_calls == 1
    assert host.autosave_calls == 1
    assert "2 items" in host.status_bar.currentMessage()


def test_mask_group_added_empty_nests_into_selected_group(
        host: _ColorHost, layer: Layer) -> None:
    outer = MaskGroup("Outer")
    layer.mask_shapes.append(outer)
    host.canvas.active_shape_id = outer.id

    host.on_mask_group_added(None)

    assert len(layer.mask_shapes) == 1
    assert len(outer.children) == 1
    assert isinstance(outer.children[0], MaskGroup)
    assert "Outer" not in host.status_bar.currentMessage()
    assert host.render_calls == 1


def test_mask_group_added_empty_appends_to_root_without_selection(
        host: _ColorHost, layer: Layer) -> None:
    host.on_mask_group_added([])
    assert len(layer.mask_shapes) == 1
    assert isinstance(layer.mask_shapes[0], MaskGroup)
    assert "Group added" in host.status_bar.currentMessage()


def test_mask_hierarchy_updated_rebuilds_tree_and_drops_unknown_ids_bug_pin(
        host: _ColorHost, layer: Layer) -> None:
    """Hierarchy rebuild + BUG-C4-03 (fixed): ids absent from the incoming
    structure are appended back at root level instead of being silently
    dropped from the data tree."""
    s1 = _add_shape(layer, "circle")
    group = MaskGroup("G")
    child = MaskShape("rect")
    group.add_child(child)
    orphan = _add_shape(layer, "curve")
    layer.mask_shapes.append(group)

    structure = [
        {"id": s1.id},
        {"id": group.id, "children": [{"id": child.id}]},
        {"id": "nonexistent"},
    ]
    host.on_mask_hierarchy_updated(structure)

    # 缺失的 orphan 被追加回根级（保持原顺序），不再静默丢弃（BUG-C4-03）。
    assert [item.id for item in layer.mask_shapes] == [s1.id, group.id, orphan.id]
    assert [item.id for item in group.children] == [child.id]
    assert host.engine.get_active_layer().get_mask_item_by_id(orphan.id) is orphan
    assert layer.is_dirty and layer.mask_dirty
    host.mask_panel.update_shapes_list.assert_called_once_with(layer.mask_shapes)
    assert host.render_calls == 1
    assert host.autosave_calls == 1
    assert host.status_bar.currentMessage() == "Mask hierarchy updated"


def test_mask_group_added_without_layer_is_noop(host: _ColorHost) -> None:
    host.engine.active_layer_index = -1
    host.on_mask_group_added(["x"])
    host.on_mask_group_added(None)
    assert host.render_calls == 0
    assert host.engine.layers[0].mask_shapes == []


def test_mask_hierarchy_updated_without_layer_is_noop(host: _ColorHost) -> None:
    host.engine.active_layer_index = -1
    host.on_mask_hierarchy_updated([{"id": "x"}])
    assert host.render_calls == 0


# ═══════════════════════════════════════════════════════════════════════
# E. Mask selection → panel sync
# ═══════════════════════════════════════════════════════════════════════


def test_on_mask_shape_selected_shape_syncs_full_params(
        host: _ColorHost, layer: Layer) -> None:
    shape = _add_shape(layer, "gradient")
    shape.feather = 12
    shape.opacity = 0.55
    shape.blur = 3.0
    shape.clean_black = 1.0
    shape.clean_white = 2.0
    shape.center = [0.25, 0.75]
    shape.size = [0.4, 0.2]
    shape.rotation = 30.0

    host.on_mask_shape_selected(shape.id)

    host.canvas.set_active_shape.assert_called_once_with(shape.id)
    host.mask_panel.select_shape.assert_called_once_with(shape.id)
    host.mask_panel.set_params.assert_called_once_with(12, 0.55, 3.0, 1.0, 2.0)
    host.mask_panel.set_transform_params.assert_called_once_with(
        [0.25, 0.75], [0.4, 0.2], 30.0)
    host.mask_panel.set_transform_visible.assert_called_once_with("gradient")


def test_on_mask_shape_selected_group_syncs_opacity_only(
        host: _ColorHost, layer: Layer) -> None:
    group = MaskGroup("G")
    group.opacity = 0.4
    layer.mask_shapes.append(group)

    host.on_mask_shape_selected(group.id)

    host.mask_panel.set_params.assert_called_once_with(0, 0.4, 0, 0, 0)
    host.mask_panel.set_transform_visible.assert_called_once_with("group")
    host.mask_panel.set_transform_params.assert_not_called()


def test_on_panel_shape_selected_missing_id_syncs_canvas_only(
        host: _ColorHost, layer: Layer) -> None:
    host.on_panel_shape_selected("missing")
    host.canvas.set_active_shape.assert_called_once_with("missing")
    host.mask_panel.set_params.assert_not_called()


def test_on_panel_shape_selected_group_syncs_opacity_only(
        host: _ColorHost, layer: Layer) -> None:
    group = MaskGroup("G")
    group.opacity = 0.25
    layer.mask_shapes.append(group)

    host.on_panel_shape_selected(group.id)

    host.mask_panel.set_params.assert_called_once_with(0, 0.25, 0, 0, 0)
    host.mask_panel.set_transform_visible.assert_called_once_with("group")
    host.mask_panel.set_transform_params.assert_not_called()


def test_on_panel_shape_selected_shape_syncs_params_without_list_highlight(
        host: _ColorHost, layer: Layer) -> None:
    """``on_panel_shape_selected`` syncs params but must not re-select in the
    mask list (that would recurse back into the canvas)."""
    shape = _add_shape(layer, "rect")
    shape.feather = 8
    shape.opacity = 0.9

    host.on_panel_shape_selected(shape.id)

    host.mask_panel.set_params.assert_called_once_with(8, 0.9, 0.0, 0.0, 0.0)
    host.mask_panel.set_transform_params.assert_called_once_with(
        shape.center, shape.size, shape.rotation)
    host.mask_panel.set_transform_visible.assert_called_once_with("rect")
    host.mask_panel.select_shape.assert_not_called(), (
        "panel-side selection must not echo back into the list")


# ═══════════════════════════════════════════════════════════════════════
# F. Pending mask updates → process_mask_update → render
# ═══════════════════════════════════════════════════════════════════════


def test_mask_shape_updated_debounces_and_applies(
        host: _ColorHost, layer: Layer, qapp) -> None:
    shape = _add_shape(layer, "circle")
    host.canvas.active_shape_id = shape.id
    starts: List[Tuple[Any, ...]] = []
    original_start = host.mask_update_timer.start

    def _counting_start(*args: Any) -> None:
        starts.append(args)
        original_start()

    host.mask_update_timer.start = _counting_start  # type: ignore[method-assign]

    host.on_mask_shape_updated(shape.id, {"feather": 12, "opacity": 0.4})
    assert host.pending_mask_updates == {shape.id: {"feather": 12, "opacity": 0.4}}
    assert host.mask_update_timer.isActive()

    # A second queued update must not restart the running timer.
    host.on_mask_shape_updated(shape.id, {"feather": 14})
    assert len(starts) == 1
    assert host.pending_mask_updates[shape.id] == {"feather": 14}

    _flush_timer(qapp, host)

    assert shape.feather == 14
    assert shape.opacity == 1.0, "superseded params dict replaces, not merges"
    assert host.pending_mask_updates == {}
    assert layer.is_dirty and layer.mask_dirty
    assert host.render_calls == 1
    host.mask_panel.set_params.assert_called_once_with(14, 1.0, 0.0, 0.0, 0.0)


def test_process_mask_update_without_layer_is_noop(host: _ColorHost) -> None:
    host.engine.active_layer_index = -1
    host.pending_mask_updates = {"x": {"feather": 1}}
    host.process_mask_update()
    assert host.render_calls == 0


def test_process_mask_update_group_filters_unsupported_fields(
        host: _ColorHost, layer: Layer) -> None:
    group = MaskGroup("G")
    layer.mask_shapes.append(group)
    host.canvas.active_shape_id = group.id
    host.pending_mask_updates = {
        group.id: {"opacity": 0.5, "feather": 10, "bogus": 1}}

    host.process_mask_update()

    assert group.opacity == 0.5
    assert not hasattr(group, "feather"), "groups must not receive shape fields"
    assert not hasattr(group, "bogus")
    assert host.render_calls == 1
    host.mask_panel.set_params.assert_called_once_with(0, 0.5, 0, 0, 0)


def test_process_mask_update_group_applies_all_group_level_fields(
        host: _ColorHost, layer: Layer) -> None:
    group = MaskGroup("G")
    layer.mask_shapes.append(group)
    host.pending_mask_updates = {
        group.id: {"visible": False, "invert": True, "locked": True,
                   "operation": "subtract", "opacity": 0.3}}

    host.process_mask_update()

    assert group.visible is False
    assert group.invert is True
    assert group.locked is True
    assert group.operation == "subtract"
    assert group.opacity == 0.3
    assert host.render_calls == 1


def test_process_mask_update_geometry_fields_copy_lists(
        host: _ColorHost, layer: Layer) -> None:
    shape = _add_shape(layer, "rect")
    host.canvas.active_shape_id = shape.id
    center = [0.1, 0.2]
    host.pending_mask_updates = {shape.id: {"center": center, "size": [0.3, 0.4]}}

    host.process_mask_update()

    assert shape.center == [0.1, 0.2]
    assert shape.center is not center, "center must be copied, not aliased"
    assert shape.size == [0.3, 0.4]
    host.mask_panel.set_transform_params.assert_called_once_with(
        shape.center, shape.size, shape.rotation)


def test_process_mask_update_ui_sync_prefers_last_active_shape(
        host: _ColorHost, layer: Layer) -> None:
    s1 = _add_shape(layer, "circle")
    s2 = _add_shape(layer, "rect")
    host.canvas.active_shape_id = s2.id
    host.pending_mask_updates = {
        s1.id: {"feather": 11},
        s2.id: {"feather": 22},
    }

    host.process_mask_update()

    assert s1.feather == 11 and s2.feather == 22
    host.mask_panel.set_params.assert_called_once_with(22, 1.0, 0.0, 0.0, 0.0)


def test_process_mask_update_records_mask_keyframes_in_record_mode(
        host: _ColorHost, layer: Layer, qapp) -> None:
    host.engine.current_grade_frame = 7
    host.grading_panel.keyframe_mode = True
    shape = _add_shape(layer, "circle")
    host.canvas.active_shape_id = shape.id
    layer.grade_keyframes = {}

    host.on_mask_shape_updated(
        shape.id, {"feather": 33, "center": [0.25, 0.75], "visible": False})
    _flush_timer(qapp, host)

    feather_track = layer.grade_keyframes.get(mask_track_name(shape.id, "feather"))
    center_track = layer.grade_keyframes.get(mask_track_name(shape.id, "center"))
    assert feather_track is not None and center_track is not None
    assert feather_track.keyframes[0].frame == 7
    assert feather_track.keyframes[0].value == pytest.approx(33.0)
    assert tuple(center_track.keyframes[0].value) == pytest.approx((0.25, 0.75))
    assert mask_track_name(shape.id, "visible") not in layer.grade_keyframes, (
        f"'visible' is not animatable: {sorted(MASK_KEYFRAME_FIELDS)}")
    assert host.keyframe_ui_refreshes == 1


def test_process_mask_update_auto_records_when_shape_already_animated(
        host: _ColorHost, layer: Layer, qapp) -> None:
    host.engine.current_grade_frame = 9
    host.grading_panel.keyframe_mode = False
    shape = _add_shape(layer, "circle")
    host.canvas.active_shape_id = shape.id
    layer.grade_keyframes = {mask_track_name(shape.id, "feather"): object()}

    host.on_mask_shape_updated(shape.id, {"opacity": 0.6})
    _flush_timer(qapp, host)

    assert mask_track_name(shape.id, "opacity") in layer.grade_keyframes
    assert host.keyframe_ui_refreshes == 1


def test_process_mask_update_skips_keyframes_without_frame_or_tracks(
        host: _ColorHost, layer: Layer, qapp) -> None:
    shape = _add_shape(layer, "circle")
    host.canvas.active_shape_id = shape.id
    layer.grade_keyframes = {}

    # Record mode on, but static image → no current frame.
    host.grading_panel.keyframe_mode = True
    host.engine.current_grade_frame = None
    host.on_mask_shape_updated(shape.id, {"feather": 5})
    _flush_timer(qapp, host)
    assert layer.grade_keyframes == {}
    assert host.keyframe_ui_refreshes == 0

    # Frame present, but record mode off and shape not animated yet.
    host.engine.current_grade_frame = 4
    host.grading_panel.keyframe_mode = False
    host.on_mask_shape_updated(shape.id, {"feather": 6})
    _flush_timer(qapp, host)
    assert layer.grade_keyframes == {}
    assert host.keyframe_ui_refreshes == 0

    # Only non-animatable fields updated → nothing to keyframe.
    host.grading_panel.keyframe_mode = True
    host.on_mask_shape_updated(shape.id, {"visible": False})
    _flush_timer(qapp, host)
    assert layer.grade_keyframes == {}
    assert host.keyframe_ui_refreshes == 0


# ═══════════════════════════════════════════════════════════════════════
# G. Brush panel signal anti-recursion lock (real Qt widgets)
# ═══════════════════════════════════════════════════════════════════════


def test_canvas_brush_params_updated_blocks_panel_signal_recursion(
        host: _ColorHost, qapp) -> None:
    size_drag = QDoubleSpinBox()
    feather_drag = QDoubleSpinBox()
    opacity_drag = QDoubleSpinBox()
    flow_drag = QDoubleSpinBox()
    btn_eraser = QToolButton()
    btn_eraser.setCheckable(True)
    for drag in (size_drag, feather_drag, opacity_drag, flow_drag):
        drag.setRange(0.0, 200.0)
    host.mask_panel.brush_size_drag = size_drag
    host.mask_panel.brush_feather_drag = feather_drag
    host.mask_panel.brush_opacity_drag = opacity_drag
    host.mask_panel.brush_flow_drag = flow_drag
    host.mask_panel.btn_eraser = btn_eraser

    size_events: List[float] = []
    eraser_events: List[bool] = []
    size_drag.valueChanged.connect(size_events.append)
    btn_eraser.toggled.connect(eraser_events.append)

    host.on_canvas_brush_params_updated(
        {"size": 77, "feather": 12, "opacity": 0.5, "flow": 0.25,
         "is_eraser": True})

    # Values are applied to the panel...
    assert size_drag.value() == pytest.approx(77.0)
    assert feather_drag.value() == pytest.approx(12.0)
    assert opacity_drag.value() == pytest.approx(50.0)
    assert flow_drag.value() == pytest.approx(25.0)
    assert btn_eraser.isChecked() is True
    # ...but the writes were signal-blocked, so nothing re-enters the
    # controller (the anti-recursion lock).
    assert size_events == [], "panel writes must not emit valueChanged"
    assert eraser_events == [], "eraser sync must not emit toggled"
    assert not size_drag.signalsBlocked() and not btn_eraser.signalsBlocked(), (
        "blockSignals must be released afterwards")

    # Wiring sanity: unblocked writes do emit — proves the test can fail
    # if the lock is removed.
    size_drag.setValue(80.0)
    btn_eraser.setChecked(False)
    btn_eraser.setChecked(True)
    assert size_events == [80.0]
    assert eraser_events == [False, True]


# ═══════════════════════════════════════════════════════════════════════
# H. LUT state switching (param change → render request routing)
# ═══════════════════════════════════════════════════════════════════════


def test_lut_intensity_changed_paused_renders_immediately(
        host: _ColorHost) -> None:
    host._playing = False
    host.on_lut_intensity_changed(0.7)
    assert host.engine.lut_intensity_calls == [0.7]
    assert host.invalidate_calls == 1
    assert host.debounce_calls == 0
    assert host.render_calls == 1


def test_lut_intensity_changed_playing_debounces_instead_of_rendering(
        host: _ColorHost) -> None:
    host._playing = True
    host.on_lut_intensity_changed(0.3)
    assert host.engine.lut_intensity_calls == [0.3]
    assert host.invalidate_calls == 1
    assert host.debounce_calls == 1
    assert host.render_calls == 0, "playing state must defer to the debouncer"


def test_apply_lut_from_path_success_and_failure(host: _ColorHost) -> None:
    host.engine.apply_lut_result = True
    host.apply_lut_from_path("/luts/teal.cube")
    assert host.engine.apply_lut_calls == ["/luts/teal.cube"]
    assert host.render_calls == 1
    assert host.autosave_calls == 1

    host.engine.apply_lut_result = False
    host.apply_lut_from_path("/luts/broken.cube")
    assert host.engine.apply_lut_calls == ["/luts/teal.cube", "/luts/broken.cube"]
    assert host.render_calls == 1, "failed LUT application must not re-render"
    assert host.autosave_calls == 1


def test_lut_reset_and_lut_removed_path_matching(
        host: _ColorHost, layer: Layer) -> None:
    host.on_lut_reset()
    assert host.engine.reset_lut_calls == 1
    assert host.render_calls == 1
    assert host.autosave_calls == 1

    layer.active_lut_path = "/luts/a.cube"
    host.on_lut_removed("/luts/b.cube")
    assert host.engine.reset_lut_calls == 1, "foreign path removal is a no-op"

    host.on_lut_removed("/luts/a.cube")
    assert host.engine.reset_lut_calls == 2


def test_decompile_lut_requested_success_failure_and_no_image(
        host: _ColorHost) -> None:
    host.engine.decompile_result = {"lift": [0.1, 0, 0]}
    host.on_decompile_lut_requested("/luts/x.cube")
    assert host.engine.decompile_calls == ["/luts/x.cube"]
    assert host.update_from_layer_calls == [host.engine.get_active_layer()]
    assert host.render_calls == 1
    assert host.autosave_calls == 1
    assert host.status_bar.currentMessage() == "LUT Decompiled Successfully!"

    host.engine.decompile_result = {}
    host.on_decompile_lut_requested("/luts/y.cube")
    assert host.status_bar.currentMessage() == "LUT Decompile Failed."
    assert host.render_calls == 1
    assert host.autosave_calls == 1

    host.engine.img_full_original = None
    host.on_decompile_lut_requested("/luts/z.cube")
    assert host.engine.decompile_calls == ["/luts/x.cube", "/luts/y.cube"]


# ═══════════════════════════════════════════════════════════════════════
# I. Video denoise
# ═══════════════════════════════════════════════════════════════════════


def test_video_denoise_changed_sets_engine_renderer_and_schedules(
        host: _ColorHost) -> None:
    payload = {"temporal": {"enabled": True, "frames": 2, "luma": 20.0},
               "spatial": {"enabled": False}}
    host.on_video_denoise_changed(payload)

    params = host.engine.video_denoise_params
    assert isinstance(params, VideoDenoiseParams)
    assert params.temporal.luma == pytest.approx(20.0)
    assert params.temporal.is_active()
    assert host.engine.renderer.set_video_denoise_params.call_args_list[0][0] == (params,)
    assert host.render_calls == 1
    assert host.denoise_window_calls == 1, (
        "active temporal denoise must schedule the neighbour-frame window")

    # Inactive temporal → no extra scheduling.
    host.on_video_denoise_changed({"temporal": {"enabled": False}})
    assert host.denoise_window_calls == 1
    assert host.render_calls == 2


def test_video_denoise_history_reset_and_debug_overlay(
        host: _ColorHost) -> None:
    host.on_video_denoise_changed({"temporal": {"enabled": True, "luma": 5.0}})
    params = host.engine.video_denoise_params
    assert params is not None

    host.on_video_denoise_debug_overlay_toggled(True)
    host.engine.renderer.set_video_denoise_params.assert_called_with(
        params, debug_overlay=True)
    assert host.render_calls == 2

    host.on_video_denoise_history_reset()
    host.engine.renderer.reset_video_denoise_history.assert_called_once()
    assert host.render_calls == 3

    # Missing renderer → guarded no-ops (debug overlay returns before the
    # render; history reset still repaints).
    host.engine.renderer = None
    host.on_video_denoise_debug_overlay_toggled(False)
    host.on_video_denoise_history_reset()
    assert host.render_calls == 4


def test_video_denoise_renderer_failures_are_swallowed(
        host: _ColorHost) -> None:
    # Renderer without the setter: params still recorded, render continues.
    host.engine.renderer = SimpleNamespace()
    host.on_video_denoise_changed({"temporal": {"enabled": True, "luma": 4.0}})
    assert isinstance(host.engine.video_denoise_params, VideoDenoiseParams)
    assert host.render_calls == 1

    # Renderer setter raising: must not break the param-change pipeline.
    host.engine.renderer = MagicMock()
    host.engine.renderer.set_video_denoise_params.side_effect = RuntimeError(
        "GL context lost")
    host.on_video_denoise_changed({"temporal": {"enabled": True, "luma": 6.0}})
    assert host.render_calls == 2

    host.engine.renderer.reset_video_denoise_history.side_effect = RuntimeError
    host.on_video_denoise_history_reset()
    assert host.render_calls == 3


# ═══════════════════════════════════════════════════════════════════════
# J. Denoise / Deblur flows (FileIO neutralised)
# ═══════════════════════════════════════════════════════════════════════


def _neutralize_file_io(monkeypatch: pytest.MonkeyPatch) -> Dict[str, List[Any]]:
    calls: Dict[str, List[Any]] = {"saved": [], "deleted": []}
    monkeypatch.setattr(cc.FileIO, "_resize_to_fit",
                        staticmethod(lambda img, size: img))
    monkeypatch.setattr(cc.FileIO, "save_baked_layer",
                        staticmethod(lambda name, arr: calls["saved"].append(name) or True))
    monkeypatch.setattr(cc.FileIO, "delete_baked_layer",
                        staticmethod(lambda name: calls["deleted"].append(name) or True))
    return calls


def test_denoise_requested_converts_uint8_and_locks_panel(
        host: _ColorHost) -> None:
    host.engine.img_full_original = np.full((4, 4, 3), 255, dtype=np.uint8)
    params = {"strength": 5}

    host.on_denoise_requested("bm3d", params)

    assert host.status_bar.currentMessage().startswith("Processing bm3d Denoise")
    host.denoise_panel.apply_btn.setEnabled.assert_called_once_with(False)
    assert not host.progress_bar.isHidden(), "indeterminate progress must show"
    sent = host.denoise_panel.engine.apply_denoise.call_args[0][0]
    assert sent.dtype == np.float32
    assert float(sent.max()) == pytest.approx(1.0)


def test_denoise_requested_without_image_is_noop(host: _ColorHost) -> None:
    host.engine.img_full_original = None
    host.on_denoise_requested("bm3d", {})
    host.denoise_panel.engine.apply_denoise.assert_not_called()


def test_denoise_finished_pushes_undo_bakes_and_restores_panel(
        host: _ColorHost, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _neutralize_file_io(monkeypatch)
    host.current_file_path = "/proj/photo.topos"
    host.engine.source_bake_file_name = "source_bake_old.exr"
    denoised = np.ones((4, 4, 3), dtype=np.float32)

    host.on_denoise_finished(denoised)

    assert len(host.engine.history.pushed) == 1
    img, layers, idx, copy_image = host.engine.history.pushed[0]
    assert img is host.engine.img_full_original
    assert copy_image is True, "pixel bakes must snapshot the image"
    assert host.engine.set_reference_calls == [host.engine.img_full_original]
    assert len(host.engine.set_image_calls) == 1
    call = host.engine.set_image_calls[0]
    assert call["full"] is denoised and call["is_new_file"] is False
    assert calls["deleted"] == ["source_bake_old.exr"]
    assert len(calls["saved"]) == 1
    baked_name = calls["saved"][0]
    assert baked_name.startswith("source_bake_") and baked_name.endswith(".exr")
    assert host.engine.source_bake_file_name == baked_name
    assert host.engine.source_bake_type == "denoise"
    host.layer_panel.set_source_bake_type.assert_called_once_with("denoise")
    host.denoise_panel.apply_btn.setEnabled.assert_called_once_with(True)
    host.denoise_panel.set_baked_state.assert_called_once_with(True)
    assert host.status_bar.currentMessage() == "Denoise Applied & Saved to Project"
    assert host.render_calls == 1
    assert host.autosave_calls == 1


def test_denoise_error_reenables_panel_and_reports(
        host: _ColorHost, monkeypatch: pytest.MonkeyPatch) -> None:
    message_box = MagicMock()
    monkeypatch.setattr(cc, "QMessageBox", message_box)

    host.on_denoise_error("boom")

    host.denoise_panel.apply_btn.setEnabled.assert_called_once_with(True)
    message_box.critical.assert_called_once()
    assert "boom" in host.status_bar.currentMessage()


def test_denoise_reset_restores_source_and_clears_bake(
        host: _ColorHost, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _neutralize_file_io(monkeypatch)
    host.current_file_path = "/proj/photo.topos"
    host.engine.source_bake_file_name = "source_bake_now.exr"
    host.engine.current_raw_params = {"enabled": True, "temp": 5}

    host.on_denoise_reset_requested()

    assert calls["deleted"] == ["source_bake_now.exr"]
    assert host.engine.source_bake_file_name is None
    assert host.engine.source_bake_type is None
    assert host.engine.raw_calls == [{"enabled": True, "temp": 5}], (
        "reset must preserve and re-apply the RAW colour space params")
    host.layer_panel.set_source_bake_type.assert_called_once_with(None)
    host.denoise_panel.set_baked_state.assert_called_once_with(False)
    assert host.status_bar.currentMessage() == "Denoise Reset to Original"
    assert host.render_calls == 1
    assert host.autosave_calls == 1


def test_denoise_reset_without_file_or_source_is_noop(
        host: _ColorHost) -> None:
    host.on_denoise_reset_requested()
    assert host.render_calls == 0

    host.current_file_path = "/p.topos"
    host.engine.img_source_full = None
    host.on_denoise_reset_requested()
    assert host.render_calls == 0


def test_deblur_requested_converts_uint16_and_finished_bakes(
        host: _ColorHost, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _neutralize_file_io(monkeypatch)
    host.engine.img_full_original = np.full((2, 2, 3), 65535, dtype=np.uint16)

    host.on_deblur_requested("sharp", {"amount": 2})
    host.deblur_panel.apply_btn.setEnabled.assert_called_once_with(False)
    sent = host.deblur_panel.engine.apply_denoise.call_args[0][0]
    assert sent.dtype == np.float32
    assert float(sent.max()) == pytest.approx(1.0)

    host.current_file_path = "/proj/clip.topos"
    deblurred = np.full((2, 2, 3), 0.5, dtype=np.float32)
    host.on_deblur_finished(deblurred)

    assert len(host.engine.history.pushed) == 1
    assert host.engine.history.pushed[0][3] is True
    assert host.engine.set_image_calls[0]["full"] is deblurred
    assert len(calls["saved"]) == 1
    assert host.engine.source_bake_file_name == calls["saved"][0]
    assert host.engine.source_bake_type == "deblur"
    host.layer_panel.set_source_bake_type.assert_called_once_with("deblur")
    host.deblur_panel.set_baked_state.assert_called_once_with(True)
    assert host.status_bar.currentMessage() == "Deblur Applied & Saved to Project"
    assert host.render_calls == 1
    assert host.autosave_calls == 1


def test_deblur_requested_without_image_is_noop(host: _ColorHost) -> None:
    host.engine.img_full_original = None
    host.on_deblur_requested("sharp", {})
    host.deblur_panel.engine.apply_denoise.assert_not_called()


def test_deblur_error_and_reset_guards(host: _ColorHost,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    message_box = MagicMock()
    monkeypatch.setattr(cc, "QMessageBox", message_box)
    host.on_deblur_error("bad")
    host.deblur_panel.apply_btn.setEnabled.assert_called_once_with(True)
    message_box.critical.assert_called_once()

    host.on_deblur_reset_requested()
    assert host.render_calls == 0, "reset without a project file is a no-op"


def test_deblur_reset_restores_source_and_clears_bake(
        host: _ColorHost, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _neutralize_file_io(monkeypatch)
    host.current_file_path = "/proj/clip.topos"
    host.engine.source_bake_file_name = "source_bake_blur.exr"
    host.engine.current_raw_params = {"enabled": False}

    host.on_deblur_reset_requested()

    assert calls["deleted"] == ["source_bake_blur.exr"]
    assert host.engine.source_bake_file_name is None
    assert host.engine.set_image_calls[0]["full"] is not None
    assert host.engine.raw_calls == []
    host.deblur_panel.set_baked_state.assert_called_once_with(False)
    host.layer_panel.set_source_bake_type.assert_called_once_with(None)
    assert host.status_bar.currentMessage() == "Deblur Reset to Original"
    assert host.render_calls == 1
    assert host.autosave_calls == 1


# ═══════════════════════════════════════════════════════════════════════
# K. Crop
# ═══════════════════════════════════════════════════════════════════════


def test_crop_toggled_activates_and_shows_original_source(
        host: _ColorHost, monkeypatch: pytest.MonkeyPatch) -> None:
    _neutralize_file_io(monkeypatch)
    host.on_crop_toggled(True)

    host.canvas.set_crop_mode.assert_called_once_with(True)
    host.mask_panel.btn_brush.setChecked.assert_called_once_with(False)
    host.mask_panel.btn_sam.setChecked.assert_called_once_with(False)
    host.qualifier_panel.set_picker_active.assert_called_once_with(False)
    assert len(host.engine.set_image_calls) == 1
    assert host.engine.set_image_calls[0]["full"] is host.engine.img_source_full
    assert host.update_canvas_calls == [False]
    assert "Crop tool activated" in host.status_bar.currentMessage()

    host.on_crop_toggled(False)
    host.canvas.set_crop_mode.assert_called_with(False)
    assert host.status_bar.currentMessage() == "Crop tool deactivated"
    assert len(host.engine.set_image_calls) == 1, "deactivation reloads nothing"


def test_crop_applied_passes_canvas_data_to_engine(host: _ColorHost) -> None:
    crop_data = {"rect": (0, 0, 100, 100), "rotation": 5.0,
                 "center": (0.5, 0.5), "size": (1.0, 1.0)}
    host.canvas.get_crop_data.return_value = crop_data

    host.on_crop_applied(panel_params=None)

    assert host.engine.crop_calls == [{
        "rect": (0, 0, 100, 100), "rotation": 5.0,
        "center": (0.5, 0.5), "size": (1.0, 1.0)}]
    host.canvas.confirm_crop.assert_called_once()
    assert host.update_canvas_calls == [True]
    host.canvas.fit_to_window.assert_called_once()
    assert host.status_bar.currentMessage() == "Crop applied"
    assert host.render_calls == 1

    host.canvas.get_crop_data.return_value = None
    host.on_crop_applied()
    assert len(host.engine.crop_calls) == 1


def test_crop_reset_requests_activate_tool_and_reset_rect(
        host: _ColorHost) -> None:
    host.on_crop_reset_requested()
    host.crop_panel.set_crop_active.assert_called_once_with(True)
    host.canvas.reset_crop_to_image.assert_called_once()
    host.canvas.set_crop_mode.assert_called_once_with(True)

    host.on_crop_reset_all_requested()
    host.canvas.reset_crop_to_image.assert_called_with()
    host.crop_panel.rot_slider.setValue.assert_called_once_with(0)
    assert "reset" in host.status_bar.currentMessage().lower()


def test_on_crop_params_changed_forwards_to_canvas(host: _ColorHost) -> None:
    params = {"rotation": 4.0, "flip_h": True}
    host.on_crop_params_changed(params)
    host.canvas.set_crop_params.assert_called_once_with(params)


# ═══════════════════════════════════════════════════════════════════════
# L. AI masks (BiRefNet / SAM) and panel visibility
# ═══════════════════════════════════════════════════════════════════════


def test_birefnet_requested_gates_on_image_and_shows_progress(
        host: _ColorHost) -> None:
    host.engine.img_full_original = None
    host.on_birefnet_requested()
    assert host.engine.request_birefnet_calls == 0

    host.engine.img_full_original = np.zeros((2, 2, 3), dtype=np.float32)
    host.on_birefnet_requested()
    assert host.engine.request_birefnet_calls == 1
    assert not host.progress_bar.isHidden()
    assert host.status_bar.currentMessage().startswith("Removing Background")


def test_ai_sam_toggled_on_and_off(host: _ColorHost) -> None:
    host.on_ai_sam_toggled(True)
    host.canvas.set_sam_mode.assert_called_once_with(True)
    host.mask_panel.btn_brush.setChecked.assert_called_once_with(False)
    host.qualifier_panel.set_picker_active.assert_called_once_with(False)
    host.crop_panel.set_crop_active.assert_called_once_with(False)
    assert host.engine.sam_encode_calls == 1
    assert host.engine.reset_sam_calls == 1
    assert not host.progress_bar.isHidden()

    host.on_ai_sam_toggled(False)
    host.canvas.set_sam_mode.assert_called_with(False)
    assert host.progress_bar.isHidden()
    assert host.status_bar.currentMessage() == "Smart Brush Deactivated"


def test_sam_encode_and_ai_process_finished_slots(host: _ColorHost) -> None:
    host.on_sam_encode_finished()
    host.canvas.set_sam_ready.assert_called_once_with(True)
    assert host.progress_bar.isHidden()

    host.on_ai_process_finished()
    assert host.status_bar.currentMessage() == "Background Removed Successfully"


def test_masks_panel_visibility_changed_toggles_tools_not_shapes(
        host: _ColorHost) -> None:
    host._on_masks_panel_visibility_changed(False)
    host.canvas.cancel_active_tools.assert_called_once()
    host.canvas.set_overlay_visible.assert_not_called()

    host._on_masks_panel_visibility_changed(True)
    host.canvas.set_overlay_visible.assert_called_once_with(True)


def test_matte_preview_requested_routes_engine_and_panel(
        host: _ColorHost) -> None:
    host._on_matte_preview_requested("black")
    assert host.engine.preview_calls == [(True, "black")]
    host.qualifier_panel.set_matte_preview_active.assert_called_once_with(
        True, "black")

    host._on_matte_preview_requested("")
    assert host.engine.preview_calls == [(True, "black"), (False,)]
    host.qualifier_panel.set_matte_preview_active.assert_called_with(
        False, "highlight")


def test_matte_preview_without_engine_is_guarded(host: _ColorHost) -> None:
    engine = host.engine
    host.engine = None
    try:
        host._on_matte_preview_requested("highlight")
    finally:
        host.engine = engine
    assert engine.preview_calls == []


# ═══════════════════════════════════════════════════════════════════════
# M. Plugin stack
# ═══════════════════════════════════════════════════════════════════════


def _fake_plugin_manager() -> MagicMock:
    manager = MagicMock()
    manager.get_plugin.return_value = {
        "name": "Magic Glow",
        "params": [{"var_name": "amount", "default": 0.25}],
    }
    return manager


def test_add_plugin_to_current_layer_uses_manifest_defaults(
        host: _ColorHost, layer: Layer, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _fake_plugin_manager()
    monkeypatch.setattr(cc, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(
        "src.features.color.core.plugin_manager.get_plugin_manager",
        lambda: manager)

    host.add_plugin_to_current_layer("glow", "Glow")

    assert len(layer.plugins) == 1
    entry = layer.plugins[0]
    assert entry["plugin_id"] == "glow"
    assert entry["params"] == {"amount": 0.25}, (
        "defaults must come from the plugin manifest, not hard-coded zeros")
    assert layer.is_dirty
    host.plugin_panel.set_layer.assert_called_once_with(layer)
    host.layer_panel.update_layers.assert_called_once_with(
        host.engine.layers, host.engine.active_layer_index)
    assert host.dock_plugin.visible_calls == [True]
    assert host.dock_plugin.raised == 1
    assert host.render_calls == 1
    assert host.autosave_calls == 1
    assert host.status_bar.currentMessage() == "Added effect: Glow"


def test_add_plugin_guards_for_missing_layer_and_plugin(
        host: _ColorHost, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _fake_plugin_manager()
    monkeypatch.setattr(cc, "get_plugin_manager", lambda: manager)

    host.engine.active_layer_index = -1
    host.add_plugin_to_current_layer("glow", "Glow")
    assert host.status_bar.currentMessage() == "No layer selected. Please select a layer first."
    assert host.render_calls == 0

    host.engine.active_layer_index = 0
    manager.get_plugin.return_value = None
    host.add_plugin_to_current_layer("missing", "Missing")
    assert "Plugin not found" in host.status_bar.currentMessage()
    assert host.render_calls == 0


def test_ai_plugin_process_requested_dispatches_to_engine(
        host: _ColorHost, layer: Layer, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _fake_plugin_manager()
    monkeypatch.setattr(
        "src.features.color.core.plugin_manager.get_plugin_manager",
        lambda: manager)
    instance_id = layer.add_plugin("glow", {"amount": 0.1})

    host._on_ai_plugin_process_requested(instance_id)

    assert host.engine.process_ai_calls == [(0, instance_id)]
    assert host.status_bar.currentMessage() == "Processing AI effect: Magic Glow..."


def test_ai_plugin_process_requested_guards(
        host: _ColorHost, layer: Layer) -> None:
    host.engine.active_layer_index = -1
    host._on_ai_plugin_process_requested("whatever")
    assert host.status_bar.currentMessage() == "No layer selected."
    assert host.engine.process_ai_calls == []

    host.engine.active_layer_index = 0
    host._on_ai_plugin_process_requested("missing-instance")
    assert host.engine.process_ai_calls == []


# ═══════════════════════════════════════════════════════════════════════
# N. Tracker geometry helpers
# ═══════════════════════════════════════════════════════════════════════


def test_region_from_geometry_clamps_to_unit_square() -> None:
    region = ColorControllerMixin._region_from_geometry([0.5, 0.5], [0.3, 0.3])
    assert region == pytest.approx((0.35, 0.35, 0.3, 0.3))

    # Oversized window clamped to the frame.
    assert ColorControllerMixin._region_from_geometry([0.5, 0.5], [2.0, 2.0]) == \
        pytest.approx((0.0, 0.0, 1.0, 1.0))
    # Degenerate size floors at 0.001.
    assert ColorControllerMixin._region_from_geometry([0.0, 0.0], [0.0, 0.0])[2:] == \
        pytest.approx((0.001, 0.001))


def test_tracker_search_from_pattern_uses_multiplier(host: _ColorHost) -> None:
    host._tracker_params = {"search_multiplier": 3.0}
    search = host._tracker_search_from_pattern((0.4, 0.4, 0.2, 0.2))
    assert search == pytest.approx((0.2, 0.2, 0.6, 0.6))

    # No cached params → falls back to the panel's current value.
    host._tracker_params = None
    host.tracker_panel.get_params.return_value = {"search_multiplier": 1.0}
    assert host._tracker_search_from_pattern((0.4, 0.4, 0.2, 0.2)) == \
        pytest.approx((0.4, 0.4, 0.2, 0.2))


def test_tracker_region_prefers_tracked_geometry(
        host: _ColorHost, layer: Layer) -> None:
    shape = _add_shape(layer, "circle")
    host.engine.get_tracked_geometry = lambda s: {
        "center": [0.2, 0.6], "size": [0.5, 0.2]}
    assert host._tracker_region_from_shape(shape) == pytest.approx(
        (0.0, 0.5, 0.5, 0.2))

    # Malformed tracked geometry must not raise into the caller.
    host.engine.get_tracked_geometry = lambda s: {"center": None}
    shape2 = _add_shape(layer, "rect")
    host._tracker_shape_id = shape2.id
    host.tracker_panel.get_params.return_value = {"search_multiplier": 1.0}
    host._on_tracker_search_committed((0.1, 0.1, 0.4, 0.4))
    host.tracker_panel.set_params.assert_not_called()


def test_tracker_display_name_fallbacks() -> None:
    name = ColorControllerMixin._tracker_display_name
    assert name(SimpleNamespace(name="Hero")) == "Hero"
    assert name(SimpleNamespace(name=None, type="circle")) == "Circle"
    assert name(SimpleNamespace(name=None)) == "Window"
    assert name(MaskGroup("G")) == "G"


def test_first_trackable_shape_id(host: _ColorHost, layer: Layer) -> None:
    group = MaskGroup("G")
    inner = MaskShape("curve")
    group.add_child(inner)
    layer.mask_shapes.extend([group, MaskShape(MaskShape.BITMAP)])
    assert host._first_trackable_shape_id() == inner.id

    layer.mask_shapes.clear()
    assert host._first_trackable_shape_id() is None


# ═══════════════════════════════════════════════════════════════════════
# O. Tracker binding / dock / overlay flows
# ═══════════════════════════════════════════════════════════════════════


def test_tracker_shape_selected_binds_leaf_with_name(
        host: _ColorHost, layer: Layer) -> None:
    shape = _add_shape(layer, "circle")
    shape.name = "Hero"
    host._color_session_manager = _FakeSessionManager(_make_session())

    host._on_tracker_shape_selected(shape.id)

    assert host._tracker_shape_id == shape.id
    host.tracker_panel.set_bound_shape.assert_called_once_with("Hero")
    host.tracker_panel.toolbar.set_targets.assert_called_once_with(
        [(shape.id, "Hero")], shape.id)
    host.tracker_panel.set_tracking_binding.assert_called_once_with(None)
    host.tracker_panel.set_current_frame.assert_not_called()
    host.canvas.show_tracker_overlay.assert_not_called(), (
        "dock hidden → overlay must not be drawn")


def test_tracker_shape_selected_unbinds_group_and_missing(
        host: _ColorHost, layer: Layer) -> None:
    group = MaskGroup("G")
    group.add_child(MaskShape("circle"))
    layer.mask_shapes.append(group)

    host._on_tracker_shape_selected(group.id)
    host.tracker_panel.set_bound_shape.assert_called_once_with(None)
    assert host._tracker_shape_id is None
    # Targets still list the trackable leaves, just unselected.
    pairs = host.tracker_panel.toolbar.set_targets.call_args[0][0]
    assert pairs == [(group.children[0].id, "Circle")]
    assert host.tracker_panel.toolbar.set_targets.call_args[0][1] is None

    host._on_tracker_shape_selected("missing")
    assert host.tracker_panel.set_bound_shape.call_count == 2


def test_load_tracker_binding_restores_from_store(host: _ColorHost) -> None:
    sentinel = object()
    host._color_session_manager = _FakeSessionManager(
        _make_session(clip_id="c1", source_frame=7))
    host.engine.tracking_store.get_binding.return_value = sentinel
    host._tracker_shape_id = "s1"

    host._load_tracker_binding("s1")

    host.engine.tracking_store.get_binding.assert_called_once_with("c1", "s1")
    host.tracker_panel.set_tracking_binding.assert_called_once_with(sentinel)
    host.tracker_panel.set_current_frame.assert_called_once_with(7)


def test_tracker_dock_shown_binds_active_or_falls_back(
        host: _ColorHost, layer: Layer) -> None:
    # Hidden dock: overlay teardown only.
    host.canvas.active_shape_id = "x"
    host._on_tracker_dock_shown(False)
    host.canvas.hide_tracker_overlay.assert_called_once()
    host.tracker_panel.set_bound_shape.assert_not_called()

    first = _add_shape(layer, "circle")
    host.dock_tracker = _FakeDock(True)
    host._color_session_manager = _FakeSessionManager(_make_session())

    # No active shape → auto-bind the first trackable window.
    host.canvas.active_shape_id = None
    host._on_tracker_dock_shown(True)
    assert host._tracker_shape_id == first.id
    host.tracker_panel.set_bound_shape.assert_called_once_with("Circle")

    # Active shape is a group → fall back to the first trackable leaf.
    group = MaskGroup("G")
    second = MaskShape("rect")
    group.add_child(second)
    layer.mask_shapes.append(group)
    host.canvas.active_shape_id = group.id
    host._on_tracker_dock_shown(True)
    assert host._tracker_shape_id == first.id, (
        "untrackable selection falls back to the first trackable window")


def test_tracker_dock_shown_unchanged_binding_only_resyncs_overlay(
        host: _ColorHost, layer: Layer) -> None:
    shape = _add_shape(layer, "circle")
    host.dock_tracker = _FakeDock(True)
    host._tracker_shape_id = shape.id
    host.canvas.active_shape_id = shape.id
    host._color_session_manager = _FakeSessionManager(_make_session())

    host._on_tracker_dock_shown(True)

    host.tracker_panel.set_bound_shape.assert_not_called()
    host.canvas.show_tracker_overlay.assert_called_once()
    args = host.canvas.show_tracker_overlay.call_args[0]
    assert args[:2] == (8, 4), "overlay uses the original image dimensions"
    assert args[2] == pytest.approx((0.35, 0.35, 0.3, 0.3))  # pattern
    assert args[3] == pytest.approx((0.2, 0.2, 0.6, 0.6))    # search ×2


def test_tracker_target_changed_rebinds_and_ignores_stale(
        host: _ColorHost, layer: Layer) -> None:
    shape = _add_shape(layer, "circle")
    host._tracker_shape_id = shape.id
    host._color_session_manager = _FakeSessionManager(_make_session())

    host._on_tracker_target_changed(shape.id)  # same binding → no-op
    host.canvas.set_active_shape.assert_not_called()

    host._on_tracker_target_changed("")
    host.canvas.set_active_shape.assert_not_called()

    host._on_tracker_target_changed("ghost-id")  # unresolvable → ignored
    host.canvas.set_active_shape.assert_not_called()

    other = _add_shape(layer, "rect")
    host._on_tracker_target_changed(other.id)
    host.canvas.set_active_shape.assert_called_once_with(other.id)
    host.mask_panel.select_shape.assert_called_once_with(other.id)
    assert host._tracker_shape_id == other.id


def test_tracker_params_changed_copies_params_and_syncs_overlay(
        host: _ColorHost, layer: Layer) -> None:
    shape = _add_shape(layer, "circle")
    host._tracker_shape_id = shape.id
    host.dock_tracker = _FakeDock(True)
    params = {"search_multiplier": 2.0, "feature_count": 8}

    host._on_tracker_params_changed(params)
    params["search_multiplier"] = 9.9  # mutation must not leak

    assert host._tracker_params == {"search_multiplier": 2.0,
                                    "feature_count": 8}
    host.canvas.show_tracker_overlay.assert_called_once()

    # Group bound → params stored, overlay untouched.
    host.canvas.show_tracker_overlay.reset_mock()
    group = MaskGroup("G")
    layer.mask_shapes.append(group)
    host._tracker_shape_id = group.id
    host._on_tracker_params_changed({"feature_count": 4})
    host.canvas.show_tracker_overlay.assert_not_called()
    assert host._tracker_params == {"feature_count": 4}


def test_tracker_pattern_committed_writes_back_geometry(
        host: _ColorHost, layer: Layer) -> None:
    shape = _add_shape(layer, "circle")
    host._tracker_shape_id = shape.id

    host._on_tracker_pattern_committed((0.1, 0.2, 0.4, 0.2))

    assert shape.center == pytest.approx([0.3, 0.3])
    assert shape.size == pytest.approx([0.4, 0.2])
    assert layer.is_dirty and layer.mask_dirty
    host.mask_panel.set_transform_params.assert_called_once_with(
        shape.center, shape.size, shape.rotation)
    host.canvas.update_mask_overlay.assert_called_once_with(layer)
    assert host.render_calls == 1
    assert host.autosave_calls == 1

    # Group / malformed region guards.
    group = MaskGroup("G")
    layer.mask_shapes.append(group)
    host._tracker_shape_id = group.id
    host._on_tracker_pattern_committed((0.1, 0.2, 0.4, 0.2))
    assert not hasattr(group, "center")
    host._tracker_shape_id = shape.id
    host._on_tracker_pattern_committed((None, None, None, None))  # type: ignore[arg-type]
    assert host.render_calls == 1


def test_tracker_search_committed_updates_multiplier(
        host: _ColorHost, layer: Layer) -> None:
    shape = _add_shape(layer, "circle")
    shape.center = [0.5, 0.5]
    shape.size = [0.2, 0.2]
    host._tracker_shape_id = shape.id
    host.tracker_panel.get_params.return_value = {"search_multiplier": 1.0}

    # Search box twice the pattern size → multiplier 2.0.
    host._on_tracker_search_committed((0.3, 0.3, 0.4, 0.4))
    host.tracker_panel.set_params.assert_called_once_with(
        {"search_multiplier": 2.0})
    assert host._tracker_params is not None
    assert host._tracker_params["search_multiplier"] == 2.0

    # Same multiplier → early return, panel untouched.
    host.tracker_panel.set_params.reset_mock()
    host.tracker_panel.get_params.return_value = {"search_multiplier": 2.0}
    host._on_tracker_search_committed((0.3, 0.3, 0.4, 0.4))
    host.tracker_panel.set_params.assert_not_called()

    # Degenerate region → guarded no-op.
    host._on_tracker_search_committed((0.0, 0.0, 0.0, 0.0))
    host.tracker_panel.set_params.assert_not_called()


def test_send_to_tracker_requires_selection_then_shows_dock(
        host: _ColorHost, layer: Layer) -> None:
    host.canvas.active_shape_id = None
    host.on_send_to_tracker_requested()
    assert host.status_bar.currentMessage() == "请先选择一个蒙版窗口"
    assert host.dock_tracker.visible_calls == []

    shape = _add_shape(layer, "circle")
    host._color_session_manager = _FakeSessionManager(_make_session())
    host.canvas.active_shape_id = shape.id
    host.on_send_to_tracker_requested()
    assert host.dock_tracker.visible_calls == [True]
    assert host.dock_tracker.raised == 1
    assert host._tracker_shape_id == shape.id


# ═══════════════════════════════════════════════════════════════════════
# P. Tracker frame context / task lifecycle
# ═══════════════════════════════════════════════════════════════════════


def test_tracker_render_context_frame_fallback_chain(
        host: _ColorHost) -> None:
    host._color_session_manager = _FakeSessionManager(
        _make_session(clip_id="c1"))

    host.video_controller = _FakeVideoController(current_frame=12)
    assert host._tracker_render_context() == ("c1", 12)

    host.video_controller = _FakeVideoController(current_frame=None)
    host._last_color_source_frame = 5
    assert host._tracker_render_context() == ("c1", 5)

    host._last_color_source_frame = None
    host._color_session_manager = _FakeSessionManager(
        _make_session(clip_id="c2", source_frame=3))
    assert host._tracker_render_context() == ("c2", 3)

    # Non-numeric frames degrade to 0 instead of raising.
    host.video_controller = _FakeVideoController(current_frame="abc")
    assert host._tracker_render_context() == ("c2", 0)


def test_tracker_frame_context_resolves_timeline_and_total_frames(
        host: _ColorHost) -> None:
    host._color_session_manager = _FakeSessionManager(
        _make_session(clip_id="c1", source_path="/m.mov", source_frame=3))
    host.video_controller = _FakeVideoController(total_frames=11)
    # BUG-C4-04 (fixed): without a timeline clip the tracker range now falls
    # back to video total_frames (frame_end is a -1 sentinel until a clip or
    # the fallback resolves it), so the range is [0, 10] and the reference
    # frame is preserved instead of being clamped to 0.
    assert host._tracker_frame_context() == ("c1", "/m.mov", 3, 0, 10)

    timeline = MagicMock()
    timeline.get_clip_by_id.return_value = SimpleNamespace(
        source_start=0, source_duration=11)
    host._current_timeline_sequences = {"t1": timeline}
    host._color_session_manager = _FakeSessionManager(_make_session(
        clip_id="c1", source_path="/m.mov", source_frame=3, timeline_id="t1"))
    assert host._tracker_frame_context() == ("c1", "/m.mov", 3, 0, 10)

    # Reference frame clamped up to a later clip start.
    timeline.get_clip_by_id.return_value = SimpleNamespace(
        source_start=4, source_duration=6)
    host._color_session_manager = _FakeSessionManager(_make_session(
        clip_id="c1", source_path="/m.mov", source_frame=1, timeline_id="t1"))
    assert host._tracker_frame_context() == ("c1", "/m.mov", 4, 4, 9)


def test_color_tracking_frame_changed_render_and_sync_rules(
        host: _ColorHost) -> None:
    host.engine.refresh_tracking_masks = lambda frame: 2
    host.dock_tracker = _FakeDock(True)

    host._on_color_tracking_frame_changed(9)
    assert host.render_calls == 1
    host.tracker_panel.set_current_frame.assert_called_once_with(9)

    host._playing = True
    host._on_color_tracking_frame_changed(10)
    assert host.render_calls == 1, "playing seeks rely on the frame pipeline"

    def _boom(frame: int) -> int:
        raise ValueError("bad frame")

    host.engine.refresh_tracking_masks = _boom
    host._on_color_tracking_frame_changed(11)
    assert host.render_calls == 1

    host.engine.refresh_tracking_masks = None
    host._on_color_tracking_frame_changed(12)
    assert host.render_calls == 1


def test_tracker_task_finished_applies_results_and_refreshes(
        host: _ColorHost, layer: Layer) -> None:
    shape = _add_shape(layer, "circle")
    host._active_tracker_task = ("c1", shape.id)
    from src.features.color.tracking.tracker_2d import TrackDirection
    host._active_tracker_directions = (TrackDirection.FORWARD,)
    binding = TrackingBinding(
        binding_id="b1", clip_id="c1", shape_id=shape.id, shape_name="W")
    binding.apply_result = MagicMock()  # type: ignore[method-assign]
    host.engine.tracking_store.get_binding.return_value = binding
    host.engine.refresh_tracking_masks = lambda: True
    host.tracking_service.is_running.return_value = False
    result = SimpleNamespace(
        transforms={10: "tf"}, observations={10: "obs"})

    host._on_tracker_task_finished("c1:" + shape.id, 1, result)

    binding.apply_result.assert_called_once_with("tf", "obs")
    assert binding.has_forward is True and binding.has_backward is False
    host.tracker_panel.set_tracking_binding.assert_called_once_with(binding)
    assert host.render_calls == 1
    host.tracker_panel.set_state.assert_not_called(), (
        "finished task with nothing left running must not re-enter RUNNING")

    # Stale task key → ignored entirely.
    binding.apply_result.reset_mock()
    host._on_tracker_task_finished("other:task", 1, result)
    binding.apply_result.assert_not_called()


def test_tracker_task_progress_started_failed_cancelled(
        host: _ColorHost) -> None:
    from src.features.color.ui.tracker_status import PanelState
    host._active_tracker_task = ("c1", "s1")

    host._on_tracker_task_started("c1:s1", 1)
    host.tracker_panel.set_state.assert_called_once_with(PanelState.RUNNING)
    host.tracker_panel.set_state.reset_mock()

    host._on_tracker_task_progress("c1:s1", 1, 3, 10)
    host.tracker_panel.set_progress.assert_called_once_with(3, 10, 0, 12)

    host._on_tracker_task_progress("stale:key", 1, 3, 10)
    host.tracker_panel.set_progress.assert_called_once()

    host._on_tracker_task_failed("c1:s1", 1, "engine blew up")
    host.tracker_panel.set_failure_reason.assert_called_once_with("engine blew up")

    host._on_tracker_task_cancelled("c1:s1", 1)
    host.tracker_panel.set_state.assert_called_with(PanelState.EMPTY)


def test_tracker_task_matches_and_cancel(host: _ColorHost) -> None:
    assert host._tracker_task_matches("c1:s1") is False
    host._active_tracker_task = ("c1", "s1")
    assert host._tracker_task_matches("c1:s1") is True
    assert host._tracker_task_matches("c2:s1") is False

    host._cancel_tracker_tracking()
    host.tracking_service.cancel.assert_called_once_with("c1", "s1")

    host._active_tracker_task = None
    host.tracking_service.cancel.reset_mock()
    host._cancel_tracker_tracking()
    host.tracking_service.cancel.assert_not_called()


def test_tracker_nav_requested_and_seek_helpers(host: _ColorHost) -> None:
    vc = _FakeVideoController(current_frame=5, total_frames=11)
    host.video_controller = vc
    # A timeline clip gives the nav buttons a real [0, 10] frame range.
    timeline = MagicMock()
    timeline.get_clip_by_id.return_value = SimpleNamespace(
        source_start=0, source_duration=11)
    host._current_timeline_sequences = {"t1": timeline}
    host._color_session_manager = _FakeSessionManager(
        _make_session(clip_id="c1", source_frame=5, timeline_id="t1"))

    for action, want in [("first", 0), ("prev", 4), ("next", 6),
                         ("last", 10), ("junk", 5)]:
        host._on_tracker_nav_requested(action)
        assert vc.seeks[-1] == want, f"action {action!r} must seek to {want}"
    host.tracker_panel.set_current_frame.assert_called_with(5)

    # Frame context failure → no seek at all.
    host._color_session_manager = _FakeSessionManager(ValueError("no session"))
    before = len(vc.seeks)
    host._on_tracker_nav_requested("next")
    assert len(vc.seeks) == before

    # Graph click / failed marker both seek; a missing seek method is safe.
    host.video_controller = None
    host._on_tracker_graph_frame_clicked(3)
    host.tracker_panel.set_current_frame.assert_called_with(3)
    host._on_tracker_failed_marker_clicked(8)
    host.tracker_panel.set_current_frame.assert_called_with(8)


def test_export_corner_pin_guards_and_writes_json(
        host: _ColorHost, layer: Layer, monkeypatch: pytest.MonkeyPatch,
        tmp_path) -> None:
    shape = _add_shape(layer, "circle")
    file_dialog = MagicMock()
    monkeypatch.setattr("PyQt6.QtWidgets.QFileDialog", file_dialog)

    host._tracker_shape_id = None
    host._active_tracker_task = None
    host._export_tracker_corner_pin()
    assert host.status_bar.currentMessage() == "当前没有可导出的跟踪绑定"
    file_dialog.getSaveFileName.assert_not_called()

    host.engine.img_full_original = None
    host._tracker_shape_id = "s1"
    host.engine.tracking_store.get_binding.return_value = object()
    host._export_tracker_corner_pin()
    file_dialog.getSaveFileName.assert_not_called()

    # Happy path: real binding + real dialog target → JSON written.
    host.engine.img_full_original = np.zeros((4, 8, 3), dtype=np.float32)
    binding = TrackingBinding(
        binding_id="b1", clip_id="c1", shape_id=shape.id,
        pattern_region=(0.4, 0.4, 0.2, 0.2),
        search_region=(0.3, 0.3, 0.4, 0.4), reference_frame=3)
    host.engine.tracking_store.get_binding.return_value = binding
    host._tracker_shape_id = shape.id
    host._active_tracker_task = ("c1", shape.id)
    out = tmp_path / "corner_pin.json"
    file_dialog.getSaveFileName.return_value = (str(out), "*.json")

    host._export_tracker_corner_pin()

    assert out.is_file(), "corner pin export must write the chosen file"
    assert "Corner Pin 已导出" in host.status_bar.currentMessage()


def test_tracker_params_to_engine_factory_builds_tracker(
        host: _ColorHost) -> None:
    host._tracker_params_to_engine_factory(
        {"feature_count": 20, "failure_policy": "hold", "min_confidence": 0.5})

    host.tracking_service.set_tracker_factory.assert_called_once()
    factory = host.tracking_service.set_tracker_factory.call_args[0][0]
    tracker = factory()
    from src.features.color.tracking.tracker_2d import Tracker2D
    assert isinstance(tracker, Tracker2D)


def test_ensure_tracker_binding_creates_and_updates(
        host: _ColorHost, layer: Layer) -> None:
    shape = _add_shape(layer, "circle")
    shape.center = [0.5, 0.5]
    shape.size = [0.2, 0.2]
    timeline = MagicMock()
    timeline.get_clip_by_id.return_value = SimpleNamespace(
        source_start=0, source_duration=11)
    host._current_timeline_sequences = {"t1": timeline}
    host._color_session_manager = _FakeSessionManager(_make_session(
        clip_id="c1", source_path="/m.mov", source_frame=3, timeline_id="t1"))
    host.video_controller = _FakeVideoController(total_frames=11)
    host.tracker_panel.get_params.return_value = {"mode": "trs"}
    host.engine.tracking_store.get_binding.return_value = None

    binding, context = host._ensure_tracker_binding(shape)

    assert context == ("c1", shape.id, "/m.mov", 3, 0, 10)
    assert isinstance(binding, TrackingBinding)
    host.engine.tracking_store.add_binding.assert_called_once_with(binding)
    assert binding.pattern_region == pytest.approx((0.4, 0.4, 0.2, 0.2))
    assert binding.search_region == pytest.approx((0.3, 0.3, 0.4, 0.4))
    assert binding.reference_frame == 3
    assert binding.source_frame_start == 0 and binding.source_frame_end == 10
    assert binding.mode == TrackingMode.TRANSLATION_ROTATION_SCALE
    assert binding.base_geometry is not None
    assert binding.base_geometry["type"] == "circle"
    host.tracker_panel.set_tracking_binding.assert_called_once_with(binding)
    host.tracker_panel.set_current_frame.assert_called_once_with(3)

    # Existing binding with stale results: geometry change wipes them.
    host.engine.tracking_store.get_binding.return_value = binding
    binding.transforms = {2: object()}
    cleared: List[Tuple[Tuple[Any, ...], Dict[str, Any]]] = []
    original_clear = binding.clear_results

    def _recording_clear(*args: Any, **kwargs: Any) -> None:
        cleared.append((args, kwargs))
        original_clear(*args, **kwargs)

    binding.clear_results = _recording_clear  # type: ignore[method-assign]
    shape.center = [0.6, 0.5]

    binding2, _ = host._ensure_tracker_binding(shape)

    assert binding2 is binding
    assert cleared == [((), {"keep_manual": True})]
    assert binding.pattern_region == pytest.approx((0.5, 0.4, 0.2, 0.2))
    assert host.engine.tracking_store.add_binding.call_count == 1


def test_ensure_tracker_binding_requires_clip_context(
        host: _ColorHost, layer: Layer) -> None:
    shape = _add_shape(layer, "circle")
    host._color_session_manager = _FakeSessionManager(
        _make_session(source_path="/m.mov"))
    with pytest.raises(ValueError):
        host._ensure_tracker_binding(shape)

    host._color_session_manager = _FakeSessionManager(_make_session(clip_id="c1"))
    with pytest.raises(ValueError):
        host._ensure_tracker_binding(shape)


# ═══════════════════════════════════════════════════════════════════════
# Q. Tracker start / retrack / reset flows
# ═══════════════════════════════════════════════════════════════════════


def _bind_trackable_shape(host: _ColorHost, layer: Layer) -> MaskShape:
    """Give the host a trackable shape with a resolvable timeline context."""
    shape = _add_shape(layer, "circle")
    shape.center = [0.5, 0.5]
    shape.size = [0.2, 0.2]
    timeline = MagicMock()
    timeline.get_clip_by_id.return_value = SimpleNamespace(
        source_start=0, source_duration=11)
    host._current_timeline_sequences = {"t1": timeline}
    host._color_session_manager = _FakeSessionManager(_make_session(
        clip_id="c1", source_path="/m.mov", source_frame=3, timeline_id="t1"))
    host.video_controller = _FakeVideoController(total_frames=11)
    host.tracker_panel.get_params.return_value = {"mode": "trs"}
    host.engine.tracking_store.get_binding.return_value = None
    host._tracker_shape_id = shape.id
    return shape


def test_start_tracker_tracking_forwards_service_call(
        host: _ColorHost, layer: Layer) -> None:
    from src.features.color.tracking.frame_sampler import AnalysisQuality
    from src.features.color.tracking.tracker_2d import TrackDirection
    from src.features.color.ui.tracker_status import PanelState
    shape = _bind_trackable_shape(host, layer)
    host.tracker_panel.get_params.return_value = {
        "mode": "trs", "quality": "high", "feature_count": 12}
    started: Dict[str, Any] = {}
    host.tracking_service.start.side_effect = lambda **kw: started.update(kw)

    host._start_tracker_tracking("forward")

    assert started["clip_id"] == "c1"
    assert started["shape_id"] == shape.id
    assert started["media_path"] == "/m.mov"
    assert started["reference_frame"] == 3
    assert started["frame_start"] == 0
    assert started["frame_end"] == 10
    assert started["directions"] == (TrackDirection.FORWARD,)
    assert started["quality"] == AnalysisQuality.HIGH
    assert host._active_tracker_task == ("c1", shape.id)
    host.tracker_panel.set_state.assert_called_once_with(PanelState.RUNNING)


def test_start_tracker_auto_picks_backward_when_playhead_after_reference(
        host: _ColorHost, layer: Layer) -> None:
    from src.features.color.tracking.tracker_2d import TrackDirection
    _bind_trackable_shape(host, layer)
    host.video_controller.current_frame = 8  # right of reference frame 3
    started: Dict[str, Any] = {}
    host.tracking_service.start.side_effect = lambda **kw: started.update(kw)

    host._start_tracker_auto_tracking()

    assert started["directions"] == (
        TrackDirection.BACKWARD, TrackDirection.FORWARD)


def test_start_tracker_single_frame_limits_request_range(
        host: _ColorHost, layer: Layer) -> None:
    _bind_trackable_shape(host, layer)
    started: Dict[str, Any] = {}
    host.tracking_service.start.side_effect = lambda **kw: started.update(kw)

    host._start_tracker_tracking("forward", single_frame=True)

    assert (started["frame_start"], started["frame_end"]) == (3, 4)

    host._start_tracker_tracking("backward", single_frame=True)
    assert (started["frame_start"], started["frame_end"]) == (2, 3)


def test_start_tracker_tracking_failure_recovers_to_failed_state(
        host: _ColorHost, layer: Layer) -> None:
    from src.features.color.ui.tracker_status import PanelState
    _bind_trackable_shape(host, layer)
    # No clip context → _ensure_tracker_binding raises ValueError.
    host._color_session_manager = _FakeSessionManager(_make_session())

    host._start_tracker_tracking("forward")

    host.tracking_service.start.assert_not_called()
    host.tracker_panel.set_failure_reason.assert_called_once()
    host.tracker_panel.set_state.assert_called_once_with(PanelState.FAILED)
    assert "启动跟踪失败" in host.status_bar.currentMessage()


def test_start_tracker_without_trackable_selection_only_messages(
        host: _ColorHost, layer: Layer) -> None:
    group = MaskGroup("G")
    layer.mask_shapes.append(group)
    host._tracker_shape_id = group.id
    host._start_tracker_tracking("forward")
    host.tracking_service.start.assert_not_called()
    assert host.status_bar.currentMessage() == "请先选择一个可跟踪的蒙版窗口"

    host._tracker_shape_id = None
    host._start_tracker_tracking("forward")
    host.tracking_service.start.assert_not_called()


def test_retrack_failed_frames_or_report_none(
        host: _ColorHost, layer: Layer) -> None:
    from src.features.color.tracking.tracker_2d import TrackDirection
    from src.features.color.ui.tracker_status import PanelState
    shape = _bind_trackable_shape(host, layer)

    # Fresh binding: no failed frames → status message, no service call.
    binding = TrackingBinding(
        binding_id="b1", clip_id="c1", shape_id=shape.id)
    host.engine.tracking_store.get_binding.return_value = binding
    host._retrack_tracker_failed()
    host.tracking_service.retrack_failed.assert_not_called()
    assert host.status_bar.currentMessage() == "当前没有需要重跟踪的失败帧"

    # With failed frames → service retracks both directions.
    failed_binding = MagicMock()
    failed_binding.failed_frames.return_value = [5, 6]
    host.engine.tracking_store.get_binding.return_value = failed_binding
    called: Dict[str, Any] = {}
    host.tracking_service.retrack_failed.side_effect = (
        lambda **kw: called.update(kw))

    host._retrack_tracker_failed()

    assert called["clip_id"] == "c1" and called["shape_id"] == shape.id
    assert called["binding_transforms"] == failed_binding.transforms
    assert host._active_tracker_directions == (
        TrackDirection.FORWARD, TrackDirection.BACKWARD)
    host.tracker_panel.set_state.assert_called_once_with(PanelState.RUNNING)

    # Unusable selection → silent no-op.
    host._tracker_shape_id = None
    host._retrack_tracker_failed()
    assert host.tracking_service.retrack_failed.call_count == 1


def test_reset_tracker_current_frame_clears_manual_correction(
        host: _ColorHost, layer: Layer) -> None:
    host.tracker_panel.graph.view._current_frame = 4
    binding = MagicMock()
    host.engine.tracking_store.get_binding.return_value = binding

    # Guards: no shape id, no clip context, no binding.
    host._tracker_shape_id = None
    host._active_tracker_task = None
    host._reset_tracker_current_frame()
    binding.clear_manual_correction.assert_not_called()

    host._tracker_shape_id = "s1"
    host._color_session_manager = _FakeSessionManager(_make_session())
    host._reset_tracker_current_frame()
    binding.clear_manual_correction.assert_not_called()

    host._active_tracker_task = ("c1", "s1")
    host.engine.tracking_store.get_binding.return_value = None
    host._reset_tracker_current_frame()
    host.tracker_panel.set_tracking_binding.assert_not_called()

    host.engine.tracking_store.get_binding.return_value = binding
    host._reset_tracker_current_frame()
    binding.clear_manual_correction.assert_called_once_with(4)
    host.tracker_panel.set_tracking_binding.assert_called_once_with(binding)
