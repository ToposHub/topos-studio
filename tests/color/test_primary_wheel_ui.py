"""Rendered hue, pointer input and primary numeric edits must describe one grade."""
from __future__ import annotations

import math

import numpy as np
import pytest
from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtTest import QTest

from src.features.color.core.composition_uniforms import (
    composition_uniform_values, params_to_rgb, params_to_offset_rgb,
)
from src.features.color.core.the_decompiler import GradingMath
from src.features.color.ui.color_wheel import (
    ColorWheel, GradingPanel, offset_display_to_hsb, offset_hsb_to_display,
)


def test_immediate_version_save_flushes_throttled_wheel_and_slider_before_snapshot(qtbot):
    panel = GradingPanel()
    qtbot.addWidget(panel)
    panel.current_stack_id = "stack"
    rendered = {}
    saved = []
    panel.grading_changed.connect(lambda *args: rendered.update(gamma=args[1], temp=args[4]))
    panel.set_save_callback(lambda params: saved.append((dict(params), dict(rendered))))
    panel.temp_slider.setValue(1.25)
    panel.gamma_wheel._bright_drag.setValue(0.5)
    assert panel._grading_update_timer.isActive()
    assert panel.gamma_wheel._update_timer.isActive()
    panel.trigger_save()
    params, snapshot = saved[0]
    assert snapshot["temp"] == params["temp"] == 1.25
    assert snapshot["gamma"] == params["gamma"]
    assert not panel._grading_update_timer.isActive()
    assert not panel.gamma_wheel._update_timer.isActive()


@pytest.mark.parametrize("rotation", [0.0, 75.0, 120.0, 270.0])
def test_rendered_ring_pointer_and_rendered_grade_share_six_hue_directions(qapp, rotation):
    wheel = ColorWheel(rotation_degrees=rotation)
    wheel.resize(230, 270)
    wheel.show()
    qapp.processEvents()
    canvas = wheel._wheel_canvas
    pixels = canvas.grab().toImage()
    center, outer_radius = canvas._geometry()
    inner_radius = outer_radius * canvas._RING_RATIO
    try:
        for hue in range(0, 360, 60):
            angle = math.radians(hue + rotation)
            direction = QPointF(math.cos(angle), -math.sin(angle))
            point = center + direction * (inner_radius - 1.5)
            color = pixels.pixelColor(round(point.x()), round(point.y()))
            error = abs((color.hsvHueF() * 360.0 - hue + 180.0) % 360.0 - 180.0)
            assert color.hsvSaturationF() > 0.8
            assert error < 4.0

            canvas._update_knob(center + direction * (inner_radius * 0.2))
            assert abs((wheel.hue - hue + 180.0) % 360.0 - 180.0) < 1e-7
            factors = np.array(params_to_rgb(wheel.hue, wheel.sat, wheel.brightness))
            expected_high = np.array(params_to_rgb(hue, 0.2, 0.0)) > 1.0
            for mode in ("lift", "gamma", "gain"):
                actual = GradingMath.apply_grading(np.array([[0.35] * 3]),
                                                   {mode: (wheel.hue, wheel.sat, wheel.brightness)})[0]
                np.testing.assert_array_equal(actual > 0.35, expected_high)
            np.testing.assert_array_equal(factors > 1.0, expected_high)
    finally:
        wheel.close()
        wheel.deleteLater()


def _event(kind, point, button=Qt.MouseButton.LeftButton):
    return QMouseEvent(kind, point, point, button, button, Qt.KeyboardModifier.NoModifier)


def test_trackerball_is_relative_and_flushes_before_gesture_end(qapp):
    wheel = ColorWheel()
    wheel.resize(230, 270)
    canvas = wheel._wheel_canvas
    wheel.show()
    qapp.processEvents()
    wheel.set_hsv(60.0, 0.1, 0.0)
    before = QPointF(wheel.knob_pos)
    center, outer_radius = canvas._geometry()
    inner_radius = outer_radius * canvas._RING_RATIO
    press = center + QPointF(20, 10)
    events = []
    wheel.interaction_started.connect(lambda: events.append("start"))
    wheel.changed.connect(lambda *args: events.append("change"))
    wheel.interaction_ended.connect(lambda: events.append("end"))
    try:
        canvas.mousePressEvent(_event(QMouseEvent.Type.MouseButtonPress, press))
        assert wheel.knob_pos == before  # clicking never jumps the correction
        canvas.mouseMoveEvent(_event(QMouseEvent.Type.MouseMove, press + QPointF(12, -8)))
        expected = before + QPointF(12 / inner_radius, -8 / inner_radius)
        assert wheel.knob_pos.x() == pytest.approx(expected.x())
        assert wheel.knob_pos.y() == pytest.approx(expected.y())
        canvas.mouseReleaseEvent(_event(QMouseEvent.Type.MouseButtonRelease, press + QPointF(12, -8)))
        assert events == ["start", "change", "end"]
        assert not wheel._update_timer.isActive()
    finally:
        wheel.close()
        wheel.deleteLater()


def test_outer_ring_drag_changes_master_brightness_only(qapp):
    """外环角向拖拽：只调 Master 亮度，不改变 hue/sat（一次完整手势）。"""
    wheel = ColorWheel(mode="lift")
    wheel.resize(230, 270)
    canvas = wheel._wheel_canvas
    wheel.show()
    qapp.processEvents()
    wheel.set_hsv(215.0, 0.3, 0.0)
    center, outer_radius = canvas._geometry()
    inner_radius = outer_radius * canvas._RING_RATIO
    ring_point = center + QPointF((inner_radius + outer_radius) / 2.0, 0.0)
    events = []
    wheel.changed.connect(lambda *args: events.append(tuple(args)))
    try:
        canvas.mousePressEvent(_event(QMouseEvent.Type.MouseButtonPress, ring_point))
        assert wheel.brightness == 0.0  # 环上按下不跳变
        canvas.mouseMoveEvent(_event(QMouseEvent.Type.MouseMove,
                                     ring_point + QPointF(0.0, 30.0)))
        assert wheel.brightness > 0.0
        assert (wheel.hue, wheel.sat) == (215.0, 0.3)
        canvas.mouseReleaseEvent(_event(QMouseEvent.Type.MouseButtonRelease,
                                        ring_point + QPointF(0.0, 30.0)))
        assert events and events[-1][2] == pytest.approx(wheel.brightness)
        assert not wheel._update_timer.isActive()
        # 顺时针方向应为正（与屏幕坐标 atan2 增量一致）
        wheel.set_hsv(215.0, 0.3, 0.0)
        canvas.mousePressEvent(_event(QMouseEvent.Type.MouseButtonPress, ring_point))
        canvas.mouseMoveEvent(_event(QMouseEvent.Type.MouseMove,
                                     ring_point - QPointF(0.0, 30.0)))
        assert wheel.brightness < 0.0
        canvas.mouseReleaseEvent(_event(QMouseEvent.Type.MouseButtonRelease,
                                        ring_point - QPointF(0.0, 30.0)))
    finally:
        wheel.close()
        wheel.deleteLater()


def test_wheel_double_click_resets_chroma_and_keeps_master(qapp):
    """双击 / 右键 = 仅重置轮盘色度（保留 Master 亮度）。"""
    wheel = ColorWheel(mode="lift")
    wheel.resize(230, 270)
    canvas = wheel._wheel_canvas
    wheel.show()
    qapp.processEvents()
    wheel.set_hsv(215.0, 0.3, 0.4)
    try:
        canvas.mouseDoubleClickEvent(_event(QMouseEvent.Type.MouseButtonDblClick,
                                            canvas._geometry()[0]))
        assert (wheel.hue, wheel.sat) == (0.0, 0.0)
        assert wheel.brightness == pytest.approx(0.4)
        # 右键同语义
        wheel.set_hsv(90.0, 0.5, 0.4)
        canvas.mousePressEvent(_event(QMouseEvent.Type.MouseButtonPress,
                                      canvas._geometry()[0],
                                      button=Qt.MouseButton.RightButton))
        assert (wheel.hue, wheel.sat) == (0.0, 0.0)
        assert wheel.brightness == pytest.approx(0.4)
        # 头部重置按钮 = 全量重置
        wheel.reset_btn.click()
        assert wheel.brightness == 0.0
    finally:
        wheel.close()
        wheel.deleteLater()


@pytest.mark.parametrize("mode,neutral", [("lift", 0.0), ("gamma", 0.0), ("gain", 1.0)])
def test_numeric_channels_master_hdr_and_reset_roundtrip(qapp, mode, neutral):
    wheel = ColorWheel(mode=mode)
    controls = (wheel._bright_drag, wheel._r_drag, wheel._g_drag, wheel._b_drag)
    try:
        assert [c.value() for c in controls] == [neutral] * 4
        wheel._r_drag.setValue(neutral + 0.2)
        rgb = np.array(params_to_rgb(wheel.hue, wheel.sat, wheel.brightness))
        np.testing.assert_allclose(rgb, [1.2, 1.0, 1.0], atol=5e-5)
        assert wheel._bright_drag.value() == pytest.approx(neutral + 0.2 / 3.0, abs=5e-5)
        wheel.set_hsv(0.0, 1.0, 1.0)
        # HDR factors reach 192; the UI must display rather than clamp to 10.
        assert wheel._r_drag.value() == pytest.approx(191.0 + neutral)
        wheel._bright_drag.setValue(neutral + 1.0)
        assert wheel.brightness == pytest.approx(math.sqrt(1.0 / 6.0))
        wheel.reset()
        assert [c.value() for c in controls] == [neutral] * 4
        assert not wheel._update_timer.isActive()
    finally:
        wheel.deleteLater()


def test_offset_wheel_display_units_and_roundtrip(qapp):
    """加性轮：数值框显示偏移×100，box → h/s/b → box 精确往返。"""
    wheel = ColorWheel(mode="offset", math="add", show_master_box=False)
    controls = (wheel._r_drag, wheel._g_drag, wheel._b_drag)
    try:
        assert wheel._bright_drag is None
        assert [c.value() for c in controls] == [0.0] * 3
        # 纯红满偏（sat=1, b=0）：bias = (0.5, -0.25, -0.25) → 50 / -25 / -25
        wheel.set_hsv(0.0, 1.0, 0.0)
        assert [c.value() for c in controls] == pytest.approx([50.0, -25.0, -25.0])
        # 主亮度经外环/拨轮进入 b：+0.3 → 三通道同时平移 +30
        wheel._set_brightness(0.3)
        assert [c.value() for c in controls] == pytest.approx([80.0, 5.0, 5.0])
        # 数值框反解：R +40 → h/s/b → 显示回到 40（允许 QColor 量化误差）
        for control in controls:
            control.setValue(0.0)
        wheel._r_drag.setValue(40.0)
        hue, sat, brightness = wheel.hue, wheel.sat, wheel.brightness
        rx, gy, bz = offset_hsb_to_display(hue, sat, brightness)
        assert rx == pytest.approx(40.0, abs=1e-2)
        assert gy == pytest.approx(0.0, abs=1e-2)
        assert bz == pytest.approx(0.0, abs=1e-2)
        # 与 uniform 提取一致
        assert np.allclose(params_to_offset_rgb(hue, sat, brightness),
                           np.array([rx, gy, bz]) / 100.0, atol=1e-6)
        wheel.reset()
        assert [c.value() for c in controls] == [0.0] * 3
    finally:
        wheel.deleteLater()


def test_log_wheels_present_with_rgb_only_boxes(qapp):
    """Log 页四轮存在、仅 RGB 三框、经扩展信号下发。"""
    panel = GradingPanel()
    qtbot_panel = panel
    try:
        qtbot_panel.show()
        qapp.processEvents()
        for wheel in (panel.log_shadow_wheel, panel.log_midtone_wheel,
                      panel.log_highlight_wheel, panel.log_offset_wheel):
            assert wheel.math == "add"
            assert wheel._bright_drag is None
        panel.log_midtone_wheel._g_drag.setValue(25.0)
        panel.log_midtone_wheel.flush_pending_changes()
        params = panel.get_extended_params()
        assert params["log_midtone"][1] > 0.0
        assert params["offset"] == (0.0, 0.0, 0.0)
        assert params["lum_mix"] == 100.0
        assert params["log_low_range"] == pytest.approx(0.333)
    finally:
        qtbot_panel.close()
        qtbot_panel.deleteLater()


def test_extended_signal_emits_offset_and_pivot(qapp):
    """Offset 轮 / pivot 变化经 grading_extended_changed(dict) 下发。"""
    panel = GradingPanel()
    extended = []
    base = []
    panel.grading_extended_changed.connect(extended.append)
    panel.grading_changed.connect(lambda *args: base.append(args))
    try:
        panel.show()
        qapp.processEvents()
        # 数值框编辑路径（程序化 set_hsv 不发信号，与乘性轮契约一致）
        panel.offset_wheel._r_drag.setValue(50.0)
        panel.offset_wheel.flush_pending_changes()
        assert len(extended) == 1
        assert set(extended[0].keys()) == set(panel._EXTENDED_KEYS)
        assert extended[0]["offset"][1] > 0.0
        assert base == []  # 加性轮不触发基础 13 参信号
        panel.pivot_slider.setValue(0.25)
        panel.flush_pending_changes()
        assert extended[-1]["pivot"] == pytest.approx(0.25)
    finally:
        panel.close()
        panel.deleteLater()


def test_pivot_replaces_exposure_and_exposure_passes_through(qapp):
    """UI 无曝光参数；曝光为内部透传（set_params 写入、发射原样带回）。"""
    panel = GradingPanel()
    captured = []
    panel.grading_changed.connect(lambda *args: captured.append(args))
    try:
        panel.show()
        qapp.processEvents()
        assert not hasattr(panel, "exp_slider")
        assert panel.pivot_slider.value() == pytest.approx(0.18)
        panel.set_params((0, 0, 0), (0, 0, 0), (0, 0, 0),
                         33.5, 0, 0, 0, 0, 50, emit=True)
        assert panel._exposure_value == pytest.approx(33.5)
        assert captured[-1][3] == pytest.approx(33.5)
        assert panel.get_current_params()["exposure"] == pytest.approx(33.5)
        assert panel.get_current_params()["pivot"] == pytest.approx(0.18)
    finally:
        panel.close()
        panel.deleteLater()


def test_primary_display_units_and_inline_commit_preserve_engine_values(qapp):
    panel = GradingPanel()
    changes = []
    events = []
    panel.grading_changed.connect(lambda *args: (changes.append(args), events.append("change")))
    panel.interaction_started.connect(lambda: events.append("start"))
    panel.interaction_ended.connect(lambda: events.append("end"))
    try:
        panel.show()
        qapp.processEvents()
        assert panel.contrast_slider.spin.value() == 1.0
        assert panel.sat_slider.spin.value() == 50.0
        assert panel.hue_slider.spin.value() == 50.0
        panel.sat_slider.spin._begin_edit()
        panel.sat_slider.spin._edit.setText("65.00")
        panel.sat_slider.spin._commit_edit()
        assert panel.get_current_params()["saturation"] == 30.0
        assert changes[-1][8] == 30.0
        assert len(changes[-1]) == 13
        assert events == ["start", "change", "end"]
        events.clear()
        QTest.mouseClick(panel.sat_slider.spin, Qt.MouseButton.MiddleButton)
        assert panel.get_current_params()["saturation"] == 0.0
        assert events == ["start", "change", "end"]
        panel.highlights_slider.setValue(45.0)
        panel.reset_all()
        assert panel.get_current_params()["highlights"] == 0.0
        assert panel.get_current_params()["lum_mix"] == 100.0
        assert panel.get_current_params()["pivot"] == pytest.approx(0.18)
        assert not panel._grading_update_timer.isActive()
        assert not panel._extended_update_timer.isActive()
    finally:
        panel.close()
        panel.deleteLater()


def test_narrow_panel_keeps_all_channel_fields_inside_their_wheel(qapp):
    panel = GradingPanel()
    panel.resize(920, 405)
    panel.show()
    qapp.processEvents()
    try:
        for wheel in (panel.lift_wheel, panel.gamma_wheel, panel.gain_wheel,
                      panel.offset_wheel):
            first_box = (wheel._bright_drag if wheel._bright_drag is not None
                         else wheel._r_drag)
            first = first_box.mapTo(wheel, first_box.rect().topLeft())
            last = wheel._b_drag.mapTo(wheel, wheel._b_drag.rect().bottomRight())
            assert first.x() >= 0
            assert last.x() < wheel.width()
            assert last.y() < wheel.height()
    finally:
        panel.close()
        panel.deleteLater()


def test_wheel_canvas_radius_follows_panel_and_caps_at_100(qapp):
    """色轮随面板缩放，封顶 100px（与 dock 高度上限联动）。"""
    panel = GradingPanel()
    panel.resize(1260, 405)
    panel.show()
    qapp.processEvents()
    try:
        radius = panel.lift_wheel._wheel_canvas._geometry()[1]
        assert radius == 100.0
        panel.resize(1260, 470)
        qapp.processEvents()
        assert panel.lift_wheel._wheel_canvas._geometry()[1] == 100.0
        panel.resize(700, 300)
        qapp.processEvents()
        smaller = panel.lift_wheel._wheel_canvas._geometry()[1]
        assert 0.0 < smaller < 100.0
    finally:
        panel.close()
        panel.deleteLater()


@pytest.mark.parametrize("mode", ["lift", "gamma", "gain"])
def test_master_dial_drag_only_changes_its_own_wheel_and_flushes(qapp, mode: str) -> None:
    panel = GradingPanel()
    panel.resize(1260, 405)
    panel.show()
    qapp.processEvents()
    wheel = getattr(panel, mode + "_wheel")
    wheel.set_hsv(215.0, 0.17, 0.0)
    dial = wheel.master_jog
    events = []
    panel.interaction_started.connect(lambda: events.append("start"))
    panel.grading_changed.connect(lambda *args: events.append("change"))
    panel.interaction_ended.connect(lambda: events.append("end"))
    try:
        press = QPointF(20.0, 7.0)
        dial.mousePressEvent(_event(QMouseEvent.Type.MouseButtonPress, press))
        assert wheel.brightness == 0.0  # no absolute jump on a trough press
        dial.mouseMoveEvent(_event(QMouseEvent.Type.MouseMove, press + QPointF(40.0, 0.0)))
        dial.mouseReleaseEvent(_event(QMouseEvent.Type.MouseButtonRelease, press + QPointF(50.0, 0.0)))
        assert wheel.brightness == pytest.approx(0.05)
        assert (wheel.hue, wheel.sat) == (215.0, 0.17)
        expected_master = 2.0 ** (wheel.brightness ** 2 * 6.0) - (mode != "gain")
        assert wheel._bright_drag.value() == pytest.approx(expected_master)
        assert events == ["start", "change", "end"]
        assert not wheel._update_timer.isActive()
        for other in {"lift", "gamma", "gain"} - {mode}:
            assert panel.get_current_params()[other] == (0.0, 0.0, 0.0)

        events.clear()
        dial.mouseDoubleClickEvent(_event(QMouseEvent.Type.MouseButtonDblClick, press))
        assert wheel.brightness == 0.0
        assert (wheel.hue, wheel.sat) == (215.0, 0.17)
        assert events == ["start", "change", "end"]
    finally:
        panel.close()
        panel.deleteLater()


def test_master_dial_sync_from_numbers_backend_and_reset_is_silent(qapp) -> None:
    wheel = ColorWheel(mode="gamma")
    changed = []
    wheel.changed.connect(lambda *args: changed.append(args))
    try:
        wheel.set_hsv(60.0, 0.25, -0.123456)
        assert wheel._bright_drag.value() == pytest.approx(
            2.0 ** ((-0.123456 ** 2) * 6.0) - 1.0, abs=1e-6)
        assert wheel.brightness == -0.123456  # display precision cannot quantize state
        assert changed == []
        assert not wheel._update_timer.isActive()
        wheel._bright_drag.setValue(1.0)
        assert wheel.brightness == pytest.approx(math.sqrt(1.0 / 6.0))
        wheel._r_drag.setValue(0.4)
        # 主亮度框始终与反解后的 HSV 状态一致
        r, g, b = ColorWheel.rgb_multipliers(wheel.hue, wheel.sat, wheel.brightness)
        assert wheel._bright_drag.value() == pytest.approx(
            (r + g + b) / 3.0 - 1.0, abs=1e-6)
        wheel.reset()
        assert wheel._bright_drag.value() == 0.0
        changed.clear()
        wheel.blockSignals(True)
        wheel.set_hsv(120.0, 0.15, 0.5)
        wheel.blockSignals(False)
        assert wheel._bright_drag.value() == pytest.approx(
            2.0 ** ((0.5 ** 2) * 6.0) - 1.0, abs=1e-6)
        assert changed == []
        assert not wheel._update_timer.isActive()
    finally:
        wheel.deleteLater()
