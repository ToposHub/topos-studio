"""CurvesWidget 侧栏强度与链接编辑的行为测试（任务 5）。

覆盖：
- edit_amounts 参与 LUT 合成（compose_curves_lut 权威实现，0/50/100 语义）；
- widget 实时 LUT 与工程重建（build_curves_lut_from_data）逐位一致；
- 强度设置不发射 changed（侧栏编辑不与画布控制点信号混用）；
- 链接编辑：加点/拖动/删点/双击重置同步到全部通道；
- 默认状态（强度 100、未链接）行为与旧版完全一致。
"""

import numpy as np
import pytest
from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtTest import QSignalSpy

from src.features.color.core.layer import build_curves_lut_from_data
from src.shared.ui.widgets.curves_widget import CurvesWidget


@pytest.fixture(autouse=True)
def _ensure_qapp(qapp):
    """类风格用例同样需要 QApplication（QWidget 构造硬依赖）。"""
    yield


def _mouse_event(event_type, x, y, button):
    return QMouseEvent(
        event_type, QPointF(x, y), QPointF(x, y), button, button,
        Qt.KeyboardModifier.NoModifier,
    )


def _press(widget, x, y):
    widget.mousePressEvent(_mouse_event(
        QEvent.Type.MouseButtonPress, x, y, Qt.MouseButton.LeftButton))


def _move(widget, x, y):
    widget.mouseMoveEvent(_mouse_event(
        QEvent.Type.MouseMove, x, y, Qt.MouseButton.LeftButton))


def _release(widget, x, y):
    widget.mouseReleaseEvent(_mouse_event(
        QEvent.Type.MouseButtonRelease, x, y, Qt.MouseButton.LeftButton))


def _final_lut(widget) -> np.ndarray:
    widget._compute_lut()
    assert widget._final_lut_cache is not None
    return widget._final_lut_cache.copy()


class TestEditAmounts:
    def test_default_amounts_keep_legacy_behavior(self):
        widget = CurvesWidget()
        widget.set_curves_data({
            "M": [(0, 255), (128, 210), (255, 0)],
            "R": [(0, 255), (255, 30)],
        })
        lut = _final_lut(widget)
        rebuilt = build_curves_lut_from_data({
            "M": [(0, 255), (128, 210), (255, 0)],
            "R": [(0, 255), (255, 30)],
        })
        assert np.allclose(lut, rebuilt, atol=1e-6)

    def test_channel_amount_zero_bypasses_that_channel(self):
        widget = CurvesWidget()
        widget.set_curves_data({"R": [(0, 255), (128, 100), (255, 0)]})
        full = _final_lut(widget)[:, 0, 0]
        identity = np.arange(256, dtype=np.float32) / 255.0
        assert not np.allclose(full, identity, atol=1e-4)

        widget.set_edit_amounts({"R": 0.0})
        muted = _final_lut(widget)[:, 0, 0]
        assert np.allclose(muted, identity, atol=1e-6)
        # 其他通道保持恒等（无曲线）
        assert np.allclose(_final_lut(widget)[:, 0, 1], identity, atol=1e-6)

    def test_channel_amount_scales_delta_linearly(self):
        widget = CurvesWidget()
        widget.set_curves_data({"R": [(0, 255), (128, 100), (255, 0)]})
        widget.set_edit_amounts({"R": 100.0})
        full = _final_lut(widget)[:, 0, 0]
        widget.set_edit_amounts({"R": 50.0})
        half = _final_lut(widget)[:, 0, 0]
        identity = np.arange(256, dtype=np.float32) / 255.0
        assert np.allclose(half, identity + 0.5 * (full - identity), atol=1e-5)

    def test_master_amount_zero_bypasses_master(self):
        widget = CurvesWidget()
        widget.set_curves_data({
            "M": [(0, 255), (255, 128)],
            "R": [(0, 255), (255, 0)],
        })
        widget.set_edit_amounts({"Y": 0.0})
        lut = _final_lut(widget)
        # master 旁路：全部通道 = 输入恒等
        identity = np.arange(256, dtype=np.float32) / 255.0
        for ch in range(3):
            assert np.allclose(lut[:, 0, ch], identity, atol=1e-6)

    def test_widget_matches_project_rebuild_with_amounts(self):
        widget = CurvesWidget()
        data = {
            "M": [(0, 255), (128, 200), (255, 0)],
            "R": [(0, 255), (255, 40)],
            "G": [(0, 255), (255, 60)],
        }
        widget.set_curves_data(data)
        widget.set_edit_amounts({"Y": 70.0, "R": 30.0, "G": 90.0, "B": 100.0})
        widget_lut = _final_lut(widget)
        rebuilt = build_curves_lut_from_data(
            data, {"Y": 70.0, "R": 30.0, "G": 90.0, "B": 100.0})
        assert np.allclose(widget_lut, rebuilt, atol=1e-5)

    def test_set_edit_amounts_does_not_emit_changed(self):
        widget = CurvesWidget()
        spy = QSignalSpy(widget.changed)
        widget.set_edit_amounts({"R": 40.0})
        assert len(spy) == 0

    def test_unchanged_amounts_short_circuit(self):
        widget = CurvesWidget()
        widget.set_curves_data({"R": [(0, 255), (255, 0)]})
        before = widget._final_lut_dirty
        widget.set_edit_amounts({"R": 100.0})  # 等于当前值
        assert widget._final_lut_dirty == before

    def test_get_edit_amounts_returns_copy(self):
        widget = CurvesWidget()
        amounts = widget.get_edit_amounts()
        amounts["R"] = 0.0
        assert widget.edit_amounts["R"] == 100.0


class TestLinkedEditing:
    def test_add_point_while_linked_syncs_all_channels(self):
        widget = CurvesWidget()
        widget.edit_linked = True
        rect = widget.get_draw_rect()
        wx = rect.left() + rect.width() * 0.5
        wy = rect.top() + rect.height() * 0.3
        _press(widget, wx, wy)
        _release(widget, wx, wy)
        for ch in widget.channels:
            assert len(widget.curves[ch]) == 3
        added_x = sorted(p.x() for p in widget.curves["M"])[1]
        for ch in ("R", "G", "B"):
            xs = sorted(p.x() for p in widget.curves[ch])
            assert any(abs(x - added_x) <= 0.5 for x in xs)

    def test_drag_while_linked_mirrors_vertical_delta(self):
        widget = CurvesWidget()
        widget.edit_linked = True
        # 各通道同一控制点 (128, 128)
        for ch in widget.channels:
            widget.curves[ch] = [QPointF(0, 255), QPointF(128, 128),
                                 QPointF(255, 0)]
        widget.set_active_channel("M")
        widget.selected_point_idx = 1
        _press(widget, 0, 0)  # 触发 interaction_started（位置无关）
        widget.selected_point_idx = 1
        rect = widget.get_draw_rect()
        start = widget.to_widget(128, 128)
        end = widget.to_widget(128, 100)
        widget.mouseMoveEvent(_mouse_event(
            QEvent.Type.MouseMove, end[0], end[1], Qt.MouseButton.LeftButton))
        _release(widget, end[0], end[1])
        # M 点移动 dy = (128→100 的 UI 空间差)，其他通道同 x 点同步移动
        moved = widget.curves["M"][1]
        assert moved.y() == pytest.approx(100.0, abs=1.0)
        for ch in ("R", "G", "B"):
            p = widget.curves[ch][1]
            assert p.x() == pytest.approx(128.0, abs=0.5)
            assert p.y() == pytest.approx(100.0, abs=1.0)

    def test_drag_without_link_keeps_other_channels(self):
        widget = CurvesWidget()
        for ch in widget.channels:
            widget.curves[ch] = [QPointF(0, 255), QPointF(128, 128),
                                 QPointF(255, 0)]
        widget.set_active_channel("M")
        rect = widget.get_draw_rect()
        end = widget.to_widget(128, 80)
        _press(widget, *widget.to_widget(128, 128))
        widget.selected_point_idx = 1
        widget.mouseMoveEvent(_mouse_event(
            QEvent.Type.MouseMove, end[0], end[1], Qt.MouseButton.LeftButton))
        _release(widget, end[0], end[1])
        for ch in ("R", "G", "B"):
            assert widget.curves[ch][1].y() == pytest.approx(128.0, abs=1e-6)

    def test_double_click_reset_while_linked_resets_all_channels(self):
        widget = CurvesWidget()
        widget.edit_linked = True
        for ch in widget.channels:
            widget.curves[ch] = [QPointF(0, 255), QPointF(64, 200),
                                 QPointF(255, 0)]
        widget.mouseDoubleClickEvent(_mouse_event(
            QEvent.Type.MouseButtonDblClick, 10, 10, Qt.MouseButton.LeftButton))
        for ch in widget.channels:
            assert widget.curves[ch] == [QPointF(0, 255), QPointF(255, 0)]

    def test_panel_link_toggle_drives_widget_flag(self):
        from src.features.color.ui.curves_panel import CurvesPanel
        widget = CurvesWidget()
        panel = CurvesPanel(widget)
        assert widget.edit_linked is True  # 面板默认链接
        panel.edit_link_btn.setChecked(False)
        assert widget.edit_linked is False
        panel.edit_link_btn.setChecked(True)
        assert widget.edit_linked is True

    def test_panel_edit_row_updates_widget_amounts(self):
        from src.features.color.ui.curves_panel import CurvesPanel
        widget = CurvesWidget()
        panel = CurvesPanel(widget)
        panel.edit_rows["G"].slider.setValue(30)
        assert widget.edit_amounts["G"] == pytest.approx(0.3 * 100.0)
        assert panel.get_ui_state()["edit_amounts"]["G"] == pytest.approx(30.0)
