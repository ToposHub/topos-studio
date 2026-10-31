"""阶段 5 测试：Tracker 面板状态机、控件启禁用与曲线图规格。

覆盖计划 §5.1-5.8 的可测试项：
- 四态状态机（空/运行/成功/失败）下的控件启禁用
- 进度文本格式 `Frame N / M · P% · x/y points`
- 云跟踪下拉禁用态 + tooltip（不做假按钮）
- 工具栏 3D 摄像机禁用态
- 曲线图布局常量、曲线色值、纯函数值域/帧域/降采样
"""

import pytest
from types import SimpleNamespace

pytestmark = pytest.mark.usefixtures("qapp")

from PyQt6.QtCore import QPoint, QRect, Qt
from PyQt6.QtGui import QColor

from src.features.color.ui.tracker_graph import (
    CHECKBOX_KEYS,
    CURVE_COMPONENTS,
    TIME_RULER_H,
    VALUE_AXIS_W,
    TrackerGraph,
    downsample,
    frame_range,
    value_range,
)
from src.features.color.ui.tracker_panel import EMPTY_HINT, TrackerPanel
from src.features.color.ui.tracker_status import LIGHT_COLORS, PanelState
from src.features.color.ui.tracker_toolbar import TrackerToolBar


def make_panel() -> TrackerPanel:
    panel = TrackerPanel()
    panel.show()  # 使子控件 isVisible() 反映自身可见性标志
    return panel


# ================================================================
# 状态机与控件启禁用
# ================================================================

class TestPanelStates:
    def test_empty_state_defaults(self, qapp):
        panel = make_panel()
        assert panel.state == PanelState.EMPTY
        assert panel.empty_hint.isVisible()
        assert panel.empty_hint.text() == EMPTY_HINT
        assert not panel.graph.isVisible()
        assert not panel.btn_forward.isEnabled()
        assert not panel.btn_backward.isEnabled()
        assert not panel.btn_stop.isEnabled()
        for widget in panel._param_widgets:
            assert not widget.isEnabled()

    def test_bound_shape_success_enables_controls(self, qapp):
        panel = make_panel()
        panel.set_bound_shape("Window 1")
        panel.set_state(PanelState.SUCCESS)
        assert panel.shape_name_label.text() == "Window 1"
        assert not panel.empty_hint.isVisible()
        assert panel.graph.isVisible()
        assert panel.btn_forward.isEnabled()
        assert panel.btn_backward.isEnabled()
        assert not panel.btn_stop.isEnabled()
        for widget in panel._param_widgets:
            assert widget.isEnabled()

    def test_davinci_layout_controls_and_parameter_drawer(self, qapp):
        panel = make_panel()
        assert set(panel.channel_boxes) == {"tx", "ty", "scale", "rotation"}
        assert panel.segment_buttons["clip"].isChecked()
        assert not panel.params_area.isVisible()
        assert not hasattr(panel, "frame_label")
        action_buttons = [
            panel.btn_backward, panel.btn_backward_one, panel.btn_auto,
            panel.btn_forward_one, panel.btn_forward,
        ]
        assert {(button.width(), button.height()) for button in action_buttons} == {
            (30, 30)
        }

        panel.set_bound_shape("Window 1")
        panel.btn_params.click()
        assert panel.params_area.isVisible()
        panel.channel_boxes["tx"].click()
        assert not panel.graph.view._visible["tx"]
        panel.segment_buttons["frame"].click()
        assert panel.graph.view._mode == "frame"

    def test_narrow_channel_layout_does_not_overlap(self, qapp):
        """窄 Dock 下通道与 Clip/Frame 必须保持独立几何区域。"""
        panel = make_panel()
        panel.set_bound_shape("Window 1")
        controls = [
            *panel.channel_boxes.values(), panel.channel_3d,
            panel.segment_buttons["clip"], panel.segment_buttons["frame"],
        ]

        for width in (442, 320):
            panel.resize(width, 420)
            qapp.processEvents()
            rects = [
                QRect(
                    widget.mapTo(panel._control_area, QPoint(0, 0)),
                    widget.size(),
                )
                for widget in controls
            ]
            for index, rect in enumerate(rects):
                assert rect.width() >= controls[index].sizeHint().width()
                assert all(
                    not rect.intersects(other)
                    for other_index, other in enumerate(rects)
                    if other_index != index
                )

    def test_running_disables_params_and_actions(self, qapp):
        panel = make_panel()
        panel.set_bound_shape("Window 1")
        panel.set_state(PanelState.RUNNING)
        assert not panel.btn_forward.isEnabled()
        assert not panel.btn_backward.isEnabled()
        assert not panel.btn_both.isEnabled()
        assert panel.btn_stop.isEnabled()
        for widget in panel._param_widgets:
            assert not widget.isEnabled()
        for btn in panel.nav_buttons.values():
            assert not btn.isEnabled()

    def test_progress_label_format(self, qapp):
        panel = make_panel()
        panel.set_bound_shape("Window 1")
        panel.set_state(PanelState.RUNNING)
        panel.set_progress(132, 480, 8, 12)
        assert panel.progress_label.text() == "Frame 132 / 480 · 27% · 8/12 points"

    def test_running_clears_progress_on_finish(self, qapp):
        panel = make_panel()
        panel.set_bound_shape("Window 1")
        panel.set_state(PanelState.RUNNING)
        panel.set_progress(10, 100, 4, 4)
        panel.set_state(PanelState.SUCCESS)
        assert panel.progress_label.text() == ""

    def test_success_stats(self, qapp):
        panel = make_panel()
        panel.set_bound_shape("Window 1")
        panel.set_state(PanelState.SUCCESS)
        panel.set_result_stats(0, 124, 0.87)
        assert "0-124" in panel.stats_label.text()
        assert "0.87" in panel.stats_label.text()

    def test_failure_reason_shown(self, qapp):
        panel = make_panel()
        panel.set_bound_shape("Window 1")
        panel.set_state(PanelState.FAILED)
        panel.set_failure_reason("特征不足")
        assert "特征不足" in panel.stats_label.text()
        # 失败态状态灯为红色
        assert panel.status_light._color == LIGHT_COLORS["failed"]

    def test_partial_failed_light_yellow(self, qapp):
        panel = make_panel()
        panel.set_bound_shape("Window 1")
        panel.set_state(PanelState.PARTIAL_FAILED)
        assert panel.status_light._color == LIGHT_COLORS["weak"]

    def test_retrack_only_after_results(self, qapp):
        panel = make_panel()
        panel.set_bound_shape("Window 1")
        # 成功态提供“重新跟踪”（计划 §5.8）
        panel.set_state(PanelState.SUCCESS)
        assert panel.btn_retrack.isEnabled()
        panel.set_state(PanelState.PARTIAL_FAILED)
        assert panel.btn_retrack.isEnabled()

    def test_unbind_returns_empty(self, qapp):
        panel = make_panel()
        panel.set_bound_shape("Window 1")
        panel.set_state(PanelState.SUCCESS)
        panel.set_bound_shape(None)
        assert panel.state == PanelState.EMPTY
        assert panel.empty_hint.isVisible()

    def test_width_bounds(self, qapp):
        panel = make_panel()
        assert panel.minimumWidth() == 320
        assert panel.maximumWidth() == 900


# ================================================================
# 参数读写
# ================================================================

class TestParams:
    def test_get_params_defaults(self, qapp):
        panel = make_panel()
        params = panel.get_params()
        assert params["mode"] == "trs"
        assert params["quality"] == "standard"
        assert params["feature_count"] == 12
        assert params["search_multiplier"] == pytest.approx(2.0)
        assert params["min_confidence"] == pytest.approx(0.35)
        assert params["failure_policy"] == "stop"

    def test_set_get_roundtrip(self, qapp):
        panel = make_panel()
        panel.set_params({
            "mode": "homography",
            "quality": "high",
            "feature_count": 24,
            "search_multiplier": 3.0,
            "min_confidence": 0.6,
            "failure_policy": "recapture",
        })
        params = panel.get_params()
        assert params["mode"] == "homography"
        assert params["quality"] == "high"
        assert params["feature_count"] == 24
        assert params["search_multiplier"] == pytest.approx(3.0)
        assert params["min_confidence"] == pytest.approx(0.6)
        assert params["failure_policy"] == "recapture"

    def test_tracking_binding_populates_curves_and_markers(self, qapp):
        panel = make_panel()
        status = SimpleNamespace(value="valid")
        failed = SimpleNamespace(value="failed")
        transform = lambda frame, tx, state: SimpleNamespace(
            frame=frame,
            translation=(tx, tx / 2.0),
            scale=(1.0, 1.0),
            rotation=tx,
            matrix=(1.0, 0.0, tx, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
            confidence=0.9 if state == status else 0.0,
            status=state,
        )
        binding = SimpleNamespace(
            shape_name="Window 1",
            reference_frame=2,
            transforms={
                1: transform(1, 1.0, status),
                2: transform(2, 2.0, status),
                3: transform(3, 0.0, failed),
            },
            manual_corrections={},
        )

        panel.set_tracking_binding(binding)

        assert panel.graph.view._series["tx"]
        assert panel.graph.view._reference_frame == 2
        assert panel.graph.view._failed_frames == [3]

    def test_bound_param_edit_emits_params_changed(self, qapp):
        panel = make_panel()
        panel.set_bound_shape("Window 1")
        seen = []
        panel.params_changed.connect(seen.append)

        panel.feature_spin.setValue(24)

        assert seen
        assert seen[-1]["feature_count"] == 24

    def test_3d_mode_item_disabled(self, qapp):
        panel = make_panel()
        idx = panel.mode_combo.findData("3d")
        assert idx >= 0
        item = panel.mode_combo.model().item(idx)
        assert not (item.flags() & Qt.ItemFlag.ItemIsEnabled)


# ================================================================
# 云跟踪下拉（不做假按钮）
# ================================================================

class TestCloudCombo:
    def test_cloud_disabled_with_tooltip(self, qapp):
        panel = make_panel()
        assert not panel.cloud_available
        item = panel.cloud_combo.model().item(1)
        assert not (item.flags() & Qt.ItemFlag.ItemIsEnabled)
        assert "本地跟踪" in panel.cloud_combo.toolTip()
        assert panel.cloud_combo.currentIndex() == 0  # 默认本地跟踪

    def test_cloud_enabled_when_available(self, qapp):
        panel = make_panel()
        panel.set_cloud_available(True)
        item = panel.cloud_combo.model().item(1)
        assert item.flags() & Qt.ItemFlag.ItemIsEnabled


# ================================================================
# 工具栏
# ================================================================

class TestToolbar:
    def test_camera3d_disabled(self, qapp):
        bar = TrackerToolBar()
        assert not bar.tool_button("camera3d").isEnabled()
        assert bar.tool_button("region").isEnabled()
        assert bar.current_tool() == "region"

    def test_tool_switch(self, qapp):
        bar = TrackerToolBar()
        seen = []
        bar.tool_changed.connect(seen.append)
        bar.tool_button("planar").click()
        assert bar.current_tool() == "planar"
        assert seen == ["planar"]
        assert bar.tool_button("planar").isChecked()
        assert not bar.tool_button("region").isChecked()

    def test_button_size(self, qapp):
        bar = TrackerToolBar()
        btn = bar.tool_button("point")
        assert btn.width() == 30 and btn.height() == 30
        assert btn.toolTip()  # 有 tooltip


# ================================================================
# 曲线图
# ================================================================

class TestGraphSpec:
    def test_layout_constants(self):
        assert TIME_RULER_H == 22
        assert VALUE_AXIS_W == 38

    def test_curve_colors(self):
        assert CURVE_COMPONENTS["tx"][0] == QColor(0x00, 0xE0, 0xB0)
        assert CURVE_COMPONENTS["ty"][0] == QColor(0x4D, 0xA3, 0xFF)
        assert CURVE_COMPONENTS["scale"][0] == QColor(0xFF, 0x38, 0xA8)
        assert CURVE_COMPONENTS["rotation"][0] == QColor(0xFF, 0xD4, 0x3B)
        assert CURVE_COMPONENTS["perspective"][0] == QColor(0xC7, 0x7D, 0xFF)
        assert CURVE_COMPONENTS["depth"][0] == QColor(0xFF, 0x9F, 0x43)

    def test_value_range(self):
        series = {"tx": [(0, 1.0), (1, 3.0)], "ty": [(0, -1.0), (1, 0.0)]}
        vmin, vmax = value_range(series)
        assert vmin < -1.0  # 10% padding
        assert vmax > 3.0
        assert value_range({}) == (0.0, 1.0)
        # 常数值序列有保护带宽
        lo, hi = value_range({"tx": [(0, 2.0), (1, 2.0)]})
        assert lo < 2.0 < hi

    def test_frame_range(self):
        series = {"tx": [(5, 1.0), (10, 2.0)], "ty": [(3, 0.0)]}
        assert frame_range(series) == (3, 10)
        assert frame_range({}) == (0, 0)

    def test_downsample_keeps_endpoints(self):
        pts = [(i, float(i)) for i in range(5000)]
        sampled = downsample(pts, 1500)
        assert len(sampled) <= 1501
        assert sampled[0] == pts[0]
        assert sampled[-1] == pts[-1]

    def test_checkbox_components(self, qapp):
        graph = TrackerGraph()
        assert set(CHECKBOX_KEYS) == set(graph._checkboxes.keys())
        assert graph.checkbox("tx").isChecked()

    def test_frame_mapping_roundtrip(self, qapp):
        graph = TrackerGraph()
        view = graph.view
        view.resize(400, 200)
        view.set_series({"tx": [(i, float(i) / 10.0) for i in range(100)]})
        view.set_mode("clip")
        x = view.frame_to_x(50)
        assert abs(view.x_to_frame(x) - 50) < 1e-6

    def test_frame_mode_window(self, qapp):
        graph = TrackerGraph()
        view = graph.view
        view.resize(400, 200)
        view.set_reference_frame(62)
        view.set_mode("frame")
        lo, hi = view._frame_extent()
        assert lo == 32 and hi == 92

    def test_fit_and_home(self, qapp):
        graph = TrackerGraph()
        view = graph.view
        view.set_reference_frame(62)
        view.go_to_reference()
        assert view._view_lo == 32 and view._view_hi == 92
        view.fit_all()
        assert view._view_lo is None and view._view_hi is None


# ================================================================
# 信号发射
# ================================================================

class TestSignals:
    def test_track_buttons_emit(self, qapp):
        panel = make_panel()
        panel.set_bound_shape("Window 1")
        panel.set_state(PanelState.SUCCESS)
        events = []
        panel.track_forward_requested.connect(lambda: events.append("fwd"))
        panel.track_backward_requested.connect(lambda: events.append("bwd"))
        panel.track_both_requested.connect(lambda: events.append("both"))
        panel.cancel_requested.connect(lambda: events.append("cancel"))
        panel.btn_forward.click()
        panel.btn_backward.click()
        panel.btn_both.click()
        panel.set_state(PanelState.RUNNING)
        panel.btn_stop.click()
        assert events == ["fwd", "bwd", "both", "cancel"]

    def test_nav_buttons_emit(self, qapp):
        panel = make_panel()
        panel.set_bound_shape("Window 1")
        seen = []
        panel.nav_requested.connect(seen.append)
        panel.nav_buttons["first"].click()
        panel.nav_buttons["last"].click()
        assert seen == ["first", "last"]

    def test_graph_frame_clicked(self, qapp):
        graph = TrackerGraph()
        seen = []
        graph.frame_clicked.connect(seen.append)
        graph.view.frame_clicked.emit(42)
        assert seen == [42]
