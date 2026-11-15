"""Color Bars page must mirror the wheels, share the param bars and reset."""
from __future__ import annotations

import pytest

from src.features.color.ui.color_wheel import (
    MODE_BARS, MODE_LOG, MODE_TONAL, MODE_WHEELS, GradingPanel,
    offset_hsb_to_display,
)


def test_bars_page_sits_between_wheel_and_log_pages(qtbot):
    panel = GradingPanel()
    qtbot.addWidget(panel)
    stack = panel.mode_stack
    assert stack.count() == 4
    assert stack.widget(MODE_WHEELS) is panel._wheel_page
    assert stack.widget(MODE_BARS) is panel._bars_page
    assert stack.widget(MODE_LOG) is panel._log_page
    assert stack.widget(MODE_TONAL) is panel.tonal_saturation_panel
    assert panel.color_bars_panel.parent() is panel._bars_page


def test_mode_buttons_switch_stacks_and_param_bars(qtbot, qapp):
    panel = GradingPanel()
    qtbot.addWidget(panel)
    panel.show()
    qapp.processEvents()
    panel._switch_mode("bars")
    assert panel.mode_stack.currentIndex() == MODE_BARS
    assert panel.top_bar_stack.currentIndex() == 0
    assert panel.bottom_bar_stack.currentIndex() == 0
    panel._switch_mode("log")
    assert panel.mode_stack.currentIndex() == MODE_LOG
    assert panel.top_bar_stack.currentIndex() == 1
    assert panel.bottom_bar_stack.currentIndex() == 1
    panel._switch_mode("tonal")
    assert panel.mode_stack.currentIndex() == MODE_TONAL
    assert not panel.top_bar_stack.isVisible()
    assert not panel.bottom_bar_stack.isVisible()
    panel._switch_mode("wheels")
    assert panel.mode_stack.currentIndex() == MODE_WHEELS
    assert panel.top_bar_stack.isVisible()


def test_bars_defaults_match_resolve(qtbot):
    panel = GradingPanel()
    qtbot.addWidget(panel)
    bars = panel.color_bars_panel
    for column in (bars.lift, bars.gamma):
        for ch in ("Y", "R", "G", "B"):
            assert column._nums[ch].value() == 0.0
            assert column._bars[ch].value() == 0.0
    for ch in ("Y", "R", "G", "B"):
        assert bars.gain._nums[ch].value() == 1.0
        assert bars.gain._bars[ch].value() == 1.0
    # Offset 列：仅 RGB 三条，默认全 0
    assert set(bars.offset._nums.keys()) == {"R", "G", "B"}
    assert set(bars.offset._bars.keys()) == {"R", "G", "B"}
    for ch in ("R", "G", "B"):
        assert bars.offset._nums[ch].value() == 0.0
        assert bars.offset._bars[ch].value() == 0.0


def test_wheel_edit_syncs_bars(qtbot):
    panel = GradingPanel()
    qtbot.addWidget(panel)
    panel.set_wheel_params("lift", (0.0, 0.0, 0.5), emit=True)
    expected = 2.0 ** 1.5 - 1.0
    lift = panel.color_bars_panel.lift
    assert abs(lift._nums["Y"].value() - expected) < 1e-6
    assert abs(lift._nums["R"].value() - expected) < 1e-6
    # bars clamp at their visual range ends; numbers keep the true value
    assert lift._bars["R"].value() == 1.0
    # Offset 列与加性轮同步（显示偏移×100）
    panel.set_wheel_params("offset", (0.0, 1.0, 0.0), emit=True)
    offset = panel.color_bars_panel.offset
    assert abs(offset._nums["R"].value() - 50.0) < 1e-6
    assert abs(offset._nums["G"].value() - (-25.0)) < 1e-6


def test_bars_edit_syncs_wheel_and_engine(qtbot):
    panel = GradingPanel()
    qtbot.addWidget(panel)
    captured = []
    panel.grading_changed.connect(lambda *args: captured.append(args))
    column = panel.color_bars_panel.gamma
    column._nums["R"].setValue(0.5)
    column.flush_pending_changes()
    assert captured, "bars edit must publish grading_changed"
    assert panel.gamma_wheel.sat > 0.0
    assert abs(panel.gamma_wheel._r_drag.value() - 0.5) < 1e-6
    assert abs(captured[-1][1][1] - panel.gamma_wheel.sat) < 1e-9


def test_bars_edit_publishes_extended_signal_for_offset(qtbot):
    panel = GradingPanel()
    qtbot.addWidget(panel)
    extended = []
    base = []
    panel.grading_extended_changed.connect(extended.append)
    panel.grading_changed.connect(lambda *args: base.append(args))
    column = panel.color_bars_panel.offset
    column._nums["R"].setValue(30.0)
    column.flush_pending_changes()
    assert extended, "offset bars edit must publish grading_extended_changed"
    assert extended[-1]["offset"][1] > 0.0
    assert base == []


def test_bars_drag_is_relative_and_fine(qtbot):
    panel = GradingPanel()
    qtbot.addWidget(panel)
    bar = panel.color_bars_panel.lift._bars["Y"]
    bar.resize(24, 170)
    from PyQt6.QtCore import QPointF, Qt
    from PyQt6.QtGui import QMouseEvent
    from PyQt6.QtCore import QEvent

    def press(y):
        return QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(12, y),
                           Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                           Qt.KeyboardModifier.NoModifier)

    def move(y):
        return QMouseEvent(QEvent.Type.MouseMove, QPointF(12, y),
                           Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
                           Qt.KeyboardModifier.NoModifier)

    bar.mousePressEvent(press(85))
    bar.mouseMoveEvent(move(85 - 100))
    assert abs(bar.value() - 0.1) < 1e-9
    bar.mouseReleaseEvent(move(85 - 100))


def test_bars_reset_restores_wheels(qtbot):
    panel = GradingPanel()
    qtbot.addWidget(panel)
    panel.set_wheel_params("gain", (30.0, 0.4, 0.2), emit=True)
    captured = []
    panel.grading_changed.connect(lambda *args: captured.append(args))
    panel.color_bars_panel.gain.reset()
    assert panel.gain_wheel.hue == 0.0
    assert panel.gain_wheel.sat == 0.0
    assert panel.gain_wheel.brightness == 0.0
    assert captured[-1][2] == (0.0, 0.0, 0.0)
    assert panel.color_bars_panel.gain._nums["Y"].value() == 1.0


def test_set_params_and_reset_all_update_bars(qtbot):
    panel = GradingPanel()
    qtbot.addWidget(panel)
    panel.set_params((0.0, 0.0, 0.3), (0.0, 0.0, 0.0), (30.0, 0.4, 0.2),
                     0, 0, 0, 1.0, 0, 50)
    lift = panel.color_bars_panel.lift
    assert abs(lift._nums["Y"].value() - (2.0 ** (0.3 ** 2 * 6.0) - 1.0)) < 1e-6
    assert panel.color_bars_panel.gain._nums["R"].value() > 1.0
    panel.reset_all()
    assert lift._nums["Y"].value() == 0.0
    assert panel.color_bars_panel.gain._nums["Y"].value() == 1.0
    assert panel.color_bars_panel.gain._bars["B"].value() == 1.0
    assert panel.color_bars_panel.offset._nums["R"].value() == 0.0


def test_set_extended_params_updates_offset_bars(qtbot):
    panel = GradingPanel()
    qtbot.addWidget(panel)
    panel.set_extended_params({"offset": (0.0, 0.6, 0.2),
                               "pivot": 0.4,
                               "lum_mix": 60.0})
    offset = panel.color_bars_panel.offset
    rx = offset._nums["R"].value()
    gy = offset._nums["G"].value()
    bz = offset._nums["B"].value()
    # 回填与色轮页显示单位一致（偏移×100）
    expected = offset_hsb_to_display(0.0, 0.6, 0.2)
    assert rx == pytest.approx(expected[0], abs=1e-6)
    assert gy == pytest.approx(expected[1], abs=1e-6)
    assert bz == pytest.approx(expected[2], abs=1e-6)
    assert panel.offset_wheel.brightness == pytest.approx(0.2)
    assert panel.pivot_slider.value() == pytest.approx(0.4)
    assert panel.lum_mix_slider.value() == pytest.approx(60.0)
