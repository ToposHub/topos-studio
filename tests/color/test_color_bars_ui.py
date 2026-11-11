"""Color Bars page must mirror the wheels, share the primary rows and reset."""
from __future__ import annotations

import math

from src.features.color.ui.color_wheel import GradingPanel


def test_bars_tab_sits_between_wheel_and_tonal(qtbot):
    panel = GradingPanel()
    qtbot.addWidget(panel)
    tabs = panel._grading_tabs
    assert tabs.count() == 3
    assert tabs.widget(0) is panel._wheel_page
    assert tabs.widget(1) is panel._bars_page
    assert tabs.widget(2) is panel.tonal_saturation_panel


def test_primary_rows_follow_wheel_and_bars_tabs(qtbot):
    panel = GradingPanel()
    qtbot.addWidget(panel)
    assert panel._prim_top_row.parent() is panel._wheel_page
    assert panel._prim_bottom_row.parent() is panel._wheel_page
    panel._grading_tabs.setCurrentIndex(1)
    assert panel._prim_top_row.parent() is panel._bars_page
    assert panel._prim_bottom_row.parent() is panel._bars_page
    panel._grading_tabs.setCurrentIndex(2)
    assert panel._prim_top_row.parent() is None
    assert panel._prim_bottom_row.parent() is None
    panel._grading_tabs.setCurrentIndex(0)
    assert panel._prim_top_row.parent() is panel._wheel_page


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
    assert lift._dial.value() == round(0.5 * 10000)


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


def test_bars_drag_is_relative_and_fine(qtbot):
    panel = GradingPanel()
    qtbot.addWidget(panel)
    bar = panel.color_bars_panel.lift._bars["Y"]
    bar.resize(14, 170)
    from PyQt6.QtCore import QPointF, Qt
    from PyQt6.QtGui import QMouseEvent
    from PyQt6.QtCore import QEvent

    def press(y):
        return QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(7, y),
                           Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                           Qt.KeyboardModifier.NoModifier)

    def move(y):
        return QMouseEvent(QEvent.Type.MouseMove, QPointF(7, y),
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