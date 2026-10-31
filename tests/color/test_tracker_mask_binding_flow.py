"""Tracker 与 Masks 面板联动修复测试。

用户场景：在 Masks 面板画形状 → 切换到 Tracker 面板。
此前存在两个缺陷：
1. 隐藏 Masks 面板会把画布上所有窗口形状一并隐藏，并清空选中形状，
   Tracker 打开后读到 active_shape_id=None 直接解绑，永久停留空态；
2. Tracker 工具栏目标下拉只有一个占位名，无法列出/切换跟踪目标。

本文件验证：
- Masks 面板隐藏不再隐藏画布形状/清空选中（画布工具重置保留选择）
- Tracker dock 显示时对"无选中/群组选中"回落到第一个可跟踪窗口
- 目标下拉列出全部可跟踪形状、target_changed 按 shape_id 重绑
"""
from unittest.mock import MagicMock

import pytest

from src.features.color.core.layer import Layer, MaskGroup, MaskShape
from src.features.color.ui.tracker_panel import TrackerPanel
from src.features.color.ui.tracker_toolbar import TrackerToolBar

pytestmark = pytest.mark.usefixtures("qapp")


# ---------------------------------------------------------------------------
# 控制器测试桩（bare mixin + MagicMock，模式同 test_power_windows）
# ---------------------------------------------------------------------------

def _controller(layers, active_index=0):
    from src.app.controllers.color_controller import ColorControllerMixin

    ctrl = ColorControllerMixin.__new__(ColorControllerMixin)
    ctrl.engine = MagicMock()
    ctrl.engine.layers = layers
    ctrl.engine.active_layer_index = active_index
    ctrl.engine.get_active_layer.return_value = layers[active_index]
    ctrl.engine.img_full_original = None
    ctrl.engine.tracking_store = MagicMock()
    ctrl.engine.tracking_store.get_binding.return_value = None
    ctrl.canvas = MagicMock()
    ctrl.canvas.active_shape_id = None
    ctrl.mask_panel = MagicMock()
    ctrl.tracker_panel = TrackerPanel()
    ctrl._color_session_manager = None
    # MainWindow __init__ 中初始化的跟踪器状态（桩需对齐）
    ctrl._tracker_shape_id = None
    ctrl._tracker_params = None
    ctrl._active_tracker_task = None
    return ctrl


def _layer_with_shapes():
    layer = Layer("L")
    circle = MaskShape(MaskShape.CIRCLE)
    rect = MaskShape(MaskShape.RECT)
    bitmap = MaskShape(MaskShape.BITMAP)
    layer.mask_shapes = [circle, rect, bitmap]
    return layer, circle, rect, bitmap


# ---------------------------------------------------------------------------
# 可跟踪形状枚举
# ---------------------------------------------------------------------------

class TestTrackableShapes:
    def test_tracks_leaf_shapes_only(self):
        layer, circle, rect, _bitmap = _layer_with_shapes()
        ctrl = _controller([layer])
        ids = [s.id for s in ctrl._iter_trackable_shapes()]
        # 位图蒙版无几何区域，不参与跟踪
        assert ids == [circle.id, rect.id]

    def test_recurses_into_groups(self):
        layer, circle, rect, _ = _layer_with_shapes()
        group = MaskGroup("G")
        inner = MaskShape(MaskShape.CURVE)
        group.add_child(inner)
        layer.mask_shapes = [group, circle, rect]
        ctrl = _controller([layer])
        ids = [s.id for s in ctrl._iter_trackable_shapes()]
        assert inner.id in ids and circle.id in ids

    def test_display_name_matches_mask_panel(self):
        layer, circle, _, _ = _layer_with_shapes()
        ctrl = _controller([layer])
        assert ctrl._tracker_display_name(circle) == "Circle"
        circle.name = "车牌窗口"
        assert ctrl._tracker_display_name(circle) == "车牌窗口"


# ---------------------------------------------------------------------------
# 绑定生命周期：选中/解绑刷新目标下拉
# ---------------------------------------------------------------------------

class TestShapeSelectionBinding:
    def test_bind_populates_targets_and_selects_bound(self):
        layer, circle, rect, _ = _layer_with_shapes()
        ctrl = _controller([layer])

        ctrl._on_tracker_shape_selected(rect.id)

        assert ctrl._tracker_shape_id == rect.id
        assert ctrl.tracker_panel._bound_shape == "Rect"
        combo = ctrl.tracker_panel.toolbar.target_combo
        assert combo.count() == 2
        assert combo.currentData() == rect.id

    def test_unbind_clears_bound_but_keeps_target_list(self):
        layer, *_ = _layer_with_shapes()
        ctrl = _controller([layer])

        ctrl._on_tracker_shape_selected(None)

        assert ctrl._tracker_shape_id is None
        assert ctrl.tracker_panel._bound_shape is None
        # 形状仍在图层里：下拉仍列出，便于用户直接选目标
        assert ctrl.tracker_panel.toolbar.target_combo.count() == 2

    def test_group_selection_unbinds(self):
        layer, circle, _, _ = _layer_with_shapes()
        group = MaskGroup("G")
        layer.mask_shapes = [group, circle]
        ctrl = _controller([layer])

        ctrl._on_tracker_shape_selected(group.id)

        assert ctrl._tracker_shape_id is None
        assert ctrl.tracker_panel._bound_shape is None


# ---------------------------------------------------------------------------
# Tracker dock 显示：兜底绑定（用户主诉场景）
# ---------------------------------------------------------------------------

class TestTrackerDockShownFallback:
    def test_no_active_shape_binds_first_trackable(self):
        layer, circle, *_ = _layer_with_shapes()
        ctrl = _controller([layer])
        ctrl.canvas.active_shape_id = None
        ctrl.dock_tracker = MagicMock()
        ctrl.dock_tracker.isVisible.return_value = True

        ctrl._on_tracker_dock_shown(True)

        assert ctrl._tracker_shape_id == circle.id
        assert ctrl.tracker_panel._bound_shape == "Circle"

    def test_group_active_falls_back_to_first_leaf(self):
        layer, circle, *_ = _layer_with_shapes()
        group = MaskGroup("G")
        group.add_child(circle)
        layer.mask_shapes = [group]
        ctrl = _controller([layer])
        ctrl.canvas.active_shape_id = group.id
        ctrl.dock_tracker = MagicMock()
        ctrl.dock_tracker.isVisible.return_value = True

        ctrl._on_tracker_dock_shown(True)

        assert ctrl._tracker_shape_id == circle.id

    def test_stale_active_id_falls_back(self):
        layer, circle, *_ = _layer_with_shapes()
        ctrl = _controller([layer])
        ctrl.canvas.active_shape_id = "deleted-shape-id"
        ctrl.dock_tracker = MagicMock()
        ctrl.dock_tracker.isVisible.return_value = True

        ctrl._on_tracker_dock_shown(True)

        assert ctrl._tracker_shape_id == circle.id

    def test_no_shapes_stays_unbound(self):
        layer = Layer("L")
        ctrl = _controller([layer])
        ctrl.dock_tracker = MagicMock()
        ctrl.dock_tracker.isVisible.return_value = True

        ctrl._on_tracker_dock_shown(True)

        assert ctrl._tracker_shape_id is None
        assert ctrl.tracker_panel._bound_shape is None

    def test_hide_does_not_unbind(self):
        layer, circle, *_ = _layer_with_shapes()
        ctrl = _controller([layer])
        ctrl._tracker_shape_id = circle.id
        ctrl.tracker_panel.set_bound_shape("Circle")
        ctrl.dock_tracker = MagicMock()

        ctrl._on_tracker_dock_shown(False)

        # 隐藏 dock 只移除画布跟踪覆盖层，绑定数据保留
        ctrl.canvas.hide_tracker_overlay.assert_called_once()
        assert ctrl._tracker_shape_id == circle.id


# ---------------------------------------------------------------------------
# 目标下拉切换重绑
# ---------------------------------------------------------------------------

class TestTargetChanged:
    def test_target_changed_rebinds_by_id(self):
        layer, circle, rect, _ = _layer_with_shapes()
        ctrl = _controller([layer])
        ctrl._on_tracker_shape_selected(circle.id)

        ctrl._on_tracker_target_changed(rect.id)

        assert ctrl._tracker_shape_id == rect.id
        assert ctrl.tracker_panel._bound_shape == "Rect"
        ctrl.canvas.set_active_shape.assert_called_with(rect.id)
        ctrl.mask_panel.select_shape.assert_called_with(rect.id)

    def test_unknown_target_ignored(self):
        layer, circle, *_ = _layer_with_shapes()
        ctrl = _controller([layer])
        ctrl._on_tracker_shape_selected(circle.id)

        ctrl._on_tracker_target_changed("missing-id")

        assert ctrl._tracker_shape_id == circle.id

    def test_same_target_no_rebind(self):
        layer, circle, *_ = _layer_with_shapes()
        ctrl = _controller([layer])
        ctrl._on_tracker_shape_selected(circle.id)
        ctrl.canvas.set_active_shape.reset_mock()

        ctrl._on_tracker_target_changed(circle.id)

        ctrl.canvas.set_active_shape.assert_not_called()


# ---------------------------------------------------------------------------
# Masks 面板可见性：不再隐藏画布形状
# ---------------------------------------------------------------------------

class TestMasksPanelVisibility:
    def test_hide_keeps_overlay_and_selection(self):
        layer, circle, *_ = _layer_with_shapes()
        ctrl = _controller([layer])

        ctrl._on_masks_panel_visibility_changed(False)

        # 只重置工具状态；不隐藏形状 overlay
        ctrl.canvas.cancel_active_tools.assert_called_once()
        ctrl.canvas.set_overlay_visible.assert_not_called()

    def test_show_ensures_overlay_visible(self):
        layer, *_ = _layer_with_shapes()
        ctrl = _controller([layer])

        ctrl._on_masks_panel_visibility_changed(True)

        ctrl.canvas.set_overlay_visible.assert_called_once_with(True)
        ctrl.canvas.cancel_active_tools.assert_not_called()


# ---------------------------------------------------------------------------
# 画布：cancel_active_tools 保留选中形状（真实 Canvas）
# ---------------------------------------------------------------------------

class TestCanvasSelectionPreserved:
    def test_cancel_active_tools_keeps_selection(self):
        from PyQt6.QtWidgets import QGraphicsScene

        from src.features.color.ui.canvas import Canvas, MaskShapeItem

        canvas = Canvas()
        scene = QGraphicsScene()
        item = MaskShapeItem("s1", "circle", canvas)
        scene.addItem(item)
        canvas.scene = scene
        canvas.mask_items = {"s1": item}
        canvas.active_shape_id = "s1"
        item.setSelected(True)

        canvas.cancel_active_tools()

        assert canvas.active_shape_id == "s1"
        assert item.isSelected()

    def test_mask_overlay_default_visible(self):
        """形状显示默认开启：会话恢复的窗口无需先开 Masks 面板即可见。"""
        from src.features.color.ui.canvas import Canvas

        canvas = Canvas()
        assert canvas._show_mask_overlays is True


# ---------------------------------------------------------------------------
# 工具栏目标下拉（id 承载）
# ---------------------------------------------------------------------------

class TestToolbarTargets:
    def test_set_targets_populates_and_selects(self, qapp):
        bar = TrackerToolBar()
        bar.set_targets([("id1", "Circle"), ("id2", "Rect")], "id2")
        assert bar.target_combo.count() == 2
        assert bar.current_target_id() == "id2"

    def test_target_changed_emits_shape_id(self, qapp):
        bar = TrackerToolBar()
        bar.set_targets([("id1", "Circle"), ("id2", "Rect")], "id1")
        seen = []
        bar.target_changed.connect(seen.append)

        bar.target_combo.setCurrentIndex(1)

        assert seen == ["id2"]

    def test_same_name_targets_distinguishable(self, qapp):
        """两个同名窗口：切换时按 id 区分（此前按文本无法区分）。"""
        bar = TrackerToolBar()
        bar.set_targets([("id1", "Circle"), ("id2", "Circle")], "id1")
        seen = []
        bar.target_changed.connect(seen.append)

        bar.target_combo.setCurrentIndex(1)

        assert seen == ["id2"]
        assert bar.current_target_id() == "id2"

    def test_empty_targets_show_placeholder(self, qapp):
        bar = TrackerToolBar()
        bar.set_targets([], None)
        assert bar.target_combo.count() == 1
        assert bar.current_target_id() == ""
