"""Rendered hue, pointer input and primary numeric edits must describe one grade."""
from __future__ import annotations

import math

import numpy as np
import pytest
from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtGui import QFont, QMouseEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QSplitter, QToolButton, QWidget

from src.features.color.core.composition_uniforms import (
    composition_uniform_values, params_to_rgb, params_to_offset_rgb,
)
from src.features.color.core.the_decompiler import GradingMath
from src.features.color.core.layer import Layer
from src.features.color.core.primary_calibration import (
    PRIMARY_CALIBRATION_CURRENT_VERSION, PRIMARY_CALIBRATION_VERSION_KEY, calibrated_grade_inputs,
)
from src.features.color.ui.color_wheel import (
    ColorWheel, GradingPanel, KnurledJogWheel, offset_display_to_hsb, offset_hsb_to_display,
)
from src.shared.ui.style_sheet import DARK_THEME


def test_scalar_value_strip_and_text_fit_at_supported_panel_widths(qtbot, qapp) -> None:
    panel = GradingPanel()
    qtbot.addWidget(panel)
    panel.setStyleSheet(DARK_THEME)
    panel.resize(624, 392)
    panel.show()
    qapp.processEvents()

    row = panel.lum_mix_slider
    assert row.isHidden()
    panel.resize(900, 392)
    qapp.processEvents()
    assert row.isVisible()
    spin = row.spin
    value = spin._value_label
    strip = spin._accent_bar
    assert value.text() == "100.00"
    assert value.fontMetrics().horizontalAdvance(value.text()) <= value.contentsRect().width()
    assert value.font().weight() < QFont.Weight.Bold
    assert strip.geometry().top() == value.geometry().bottom() + 1
    field = value.geometry().united(strip.geometry())
    assert abs(spin.y() + field.center().y() - row.label.geometry().center().y()) <= 1
    assert panel.rect().contains(strip.mapTo(panel, strip.rect().bottomRight()))

    qtbot.mouseDClick(spin, Qt.MouseButton.LeftButton, pos=spin.rect().center())
    assert spin._edit.isVisible()
    assert strip.geometry().top() == spin._edit.geometry().bottom() + 1
    qtbot.keyClick(spin._edit, Qt.Key.Key_Escape)

    panel.resize(624, 392)
    panel.set_extended_params({PRIMARY_CALIBRATION_VERSION_KEY: PRIMARY_CALIBRATION_CURRENT_VERSION})
    panel.temp_slider.setValue(-100.0)
    qapp.processEvents()
    temperature = panel.temp_slider.spin._value_label
    assert temperature.text() == "-10000.0"
    assert temperature.fontMetrics().horizontalAdvance(temperature.text()) <= temperature.contentsRect().width()


def test_measured_grade_uses_native_display_values_and_round_trips(qtbot) -> None:
    panel = GradingPanel()
    qtbot.addWidget(panel)
    panel.set_extended_params({PRIMARY_CALIBRATION_VERSION_KEY: PRIMARY_CALIBRATION_CURRENT_VERSION})
    assert panel.get_current_params()[PRIMARY_CALIBRATION_VERSION_KEY] == PRIMARY_CALIBRATION_CURRENT_VERSION
    assert panel.pivot_slider.spin.value() == pytest.approx(0.435)
    assert panel.log_offset_wheel._r_drag.value() == pytest.approx(25.0)
    panel.pivot_slider.spin.setValue(0.20)
    assert panel.pivot_slider.spin.value() == pytest.approx(0.20)
    assert panel._scalar_rows["pivot"][1].spin.value() == pytest.approx(0.20)

    panel.temp_slider.spin.setValue(500.0)
    panel.color_bars_panel.offset._nums["R"].setValue(26.0)
    panel.flush_pending_changes()
    assert panel.temp_slider.value() == pytest.approx(5.0)
    assert panel.offset_wheel._r_drag.value() == pytest.approx(26.0, abs=0.001)
    assert panel.log_offset_wheel._r_drag.value() == pytest.approx(26.0, abs=0.001)

    panel.log_offset_wheel._g_drag.setValue(24.0)
    panel.flush_pending_changes()
    assert panel.color_bars_panel.offset._nums["G"].value() == pytest.approx(24.0, abs=0.001)
    panel.log_shadow_wheel._r_drag.setValue(0.10)
    panel.flush_pending_changes()

    layer = Layer("Calibrated")
    layer.grading_params.update(panel.get_current_params())
    reloaded = Layer.from_dict(layer.to_dict())
    assert reloaded.grading_params[PRIMARY_CALIBRATION_VERSION_KEY] == PRIMARY_CALIBRATION_CURRENT_VERSION
    assert reloaded.grading_params["log_offset"] == (0.0, 0.0, 0.0)
    grid, remaining = calibrated_grade_inputs(reloaded)
    assert grid is not None and abs(grid).max() > 0.0
    assert remaining["temp"] == 0.0
    assert remaining["log_shadow"] == (0.0, 0.0, 0.0)

    panel.set_extended_params({PRIMARY_CALIBRATION_VERSION_KEY: 1, "pivot": 0.18})
    assert panel.get_current_params()[PRIMARY_CALIBRATION_VERSION_KEY] == 1
    assert panel.pivot_slider.spin.value() == pytest.approx(0.435)
    panel.set_extended_params(Layer("Legacy").grading_params)
    assert panel.log_offset_wheel._r_drag.value() == pytest.approx(0.0)


def test_video_primary_profile_follows_context_only_before_a_grade_is_authored(qtbot) -> None:
    use_measured = True
    panel = GradingPanel(use_measured_primaries=lambda: use_measured)
    qtbot.addWidget(panel)
    neutral = Layer("Neutral").grading_params
    assert not any(button.text() == "709" for button in panel.findChildren(QToolButton))
    assert panel.pending_primary_calibration_version == PRIMARY_CALIBRATION_CURRENT_VERSION
    assert panel.get_current_params()[PRIMARY_CALIBRATION_VERSION_KEY] == 0
    assert panel.pivot_slider.spin.value() == pytest.approx(0.435)

    panel.load_extended_params({**neutral, "temp": 5.0})
    assert panel.pending_primary_calibration_version == 0
    assert panel.pivot_slider.spin.value() == pytest.approx(0.18)
    panel.load_extended_params(neutral, animated=True)
    assert panel.pending_primary_calibration_version == 0

    panel.load_extended_params(neutral)
    assert panel.pending_primary_calibration_version == PRIMARY_CALIBRATION_CURRENT_VERSION
    use_measured = False
    panel.refresh_primary_calibration()
    assert panel.pivot_slider.spin.value() == pytest.approx(0.18)
    assert panel.pending_primary_calibration_version == 0

    log_offset = (30.0, 0.2, 0.0)
    panel.load_extended_params({**neutral, PRIMARY_CALIBRATION_VERSION_KEY: 1,
                                "log_offset": log_offset})
    assert panel.pivot_slider.spin.value() == pytest.approx(0.435)
    assert panel.get_extended_params()["log_offset"] == log_offset
    assert panel.get_extended_params()[PRIMARY_CALIBRATION_VERSION_KEY] == 1
    assert panel.pending_primary_calibration_version == 0


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
    pixel_ratio = pixels.devicePixelRatio()
    center, outer_radius = canvas._geometry()
    inner_radius = canvas._inner_radius(outer_radius)
    try:
        for hue in range(0, 360, 60):
            angle = math.radians(hue + rotation)
            direction = QPointF(math.cos(angle), -math.sin(angle))
            for radius_fraction, saturation_range in (
                (0.975, (0.8, 1.0)),  # outer part of the vivid ring
                (0.94, (0.8, 1.0)),  # inner part of the vivid ring
                (0.88, (0.3, 0.55)),  # muted disk near the ring
            ):
                point = center + direction * (inner_radius * radius_fraction)
                color = pixels.pixelColor(round(point.x() * pixel_ratio),
                                          round(point.y() * pixel_ratio))
                error = abs((color.hsvHueF() * 360.0 - hue + 180.0) % 360.0 - 180.0)
                assert saturation_range[0] <= color.hsvSaturationF() <= saturation_range[1]
                assert error < 5.0

            point = center + direction * (inner_radius * 0.2)
            color = pixels.pixelColor(round(point.x() * pixel_ratio),
                                      round(point.y() * pixel_ratio))
            assert color.hsvSaturationF() < 0.15

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


def test_hydration_discards_the_previous_scalar_drag_baseline(qtbot) -> None:
    panel = GradingPanel()
    qtbot.addWidget(panel)
    scalar = panel.temp_slider.spin
    press = QPointF(18.0, 8.0)
    scalar.mousePressEvent(_event(QMouseEvent.Type.MouseButtonPress, press))
    scalar.mouseMoveEvent(_event(QMouseEvent.Type.MouseMove, press + QPointF(24.0, 0.0)))
    assert panel.temp_slider.value() > 0.0
    panel.set_params(*(((0.0, 0.0, 0.0),) * 3 + (0.0,) * 9))
    panel.current_stack_id = "replacement"
    with qtbot.waitSignal(panel.interaction_ended):
        scalar.mouseReleaseEvent(_event(
            QMouseEvent.Type.MouseButtonRelease, press + QPointF(48.0, 0.0)))
    assert panel.temp_slider.value() == 0.0


def test_trackerball_is_relative_and_flushes_before_gesture_end(qapp):
    wheel = ColorWheel()
    wheel.resize(230, 270)
    canvas = wheel._wheel_canvas
    wheel.show()
    qapp.processEvents()
    wheel.set_hsv(60.0, 0.1, 0.0)
    before = QPointF(wheel.knob_pos)
    center, outer_radius = canvas._geometry()
    inner_radius = canvas._inner_radius(outer_radius)
    press = center + QPointF(20, 10)
    events = []
    wheel.interaction_started.connect(lambda: events.append("start"))
    wheel.changed.connect(lambda *args: events.append("change"))
    wheel.interaction_ended.connect(lambda: events.append("end"))
    try:
        canvas.mousePressEvent(_event(QMouseEvent.Type.MouseButtonPress, press))
        assert wheel.knob_pos == before  # clicking never jumps the correction
        canvas.mouseMoveEvent(_event(QMouseEvent.Type.MouseMove, press + QPointF(12, -8)))
        expected = before + QPointF(1.2 / inner_radius, -0.8 / inner_radius)
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
    inner_radius = canvas._inner_radius(outer_radius)
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
    panel.resize(1260, 400)
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
        spin = panel.sat_slider.spin
        cell = spin._value_label
        start = cell.mapTo(panel, cell.rect().center())
        QTest.mouseDClick(panel.windowHandle(), Qt.MouseButton.LeftButton, pos=start)
        assert spin.isEditing()
        panel.sat_slider.spin._edit.setText("65.00")
        QTest.keyClick(spin._edit, Qt.Key.Key_Return)
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


@pytest.mark.parametrize(("attribute", "mode", "dx", "expected"), [
    ("temp_slider", "wheels", 120, 1200.0),
    ("tint_slider", "wheels", 120, 60.0),
    ("contrast_slider", "wheels", 120, 1.24),
    ("pivot_slider", "wheels", 120, 0.675),
    ("mid_detail_slider", "wheels", 120, 60.0),
    ("vibrance_slider", "wheels", 120, 60.0),
    ("shadow_slider", "wheels", 120, 60.0),
    ("highlights_slider", "wheels", 120, 60.0),
    ("sat_slider", "wheels", 120, 74.0),
    ("hue_slider", "wheels", 120, 74.0),
    ("lum_mix_slider", "wheels", -120, 76.0),
    ("log_low_range_slider", "log", 120, 0.573),
    ("log_high_range_slider", "log", 120, 0.790),
])
def test_primary_scalar_drag_uses_display_units_at_the_actual_value_cell(
    qtbot, attribute: str, mode: str, dx: int, expected: float,
) -> None:
    panel = GradingPanel()
    qtbot.addWidget(panel)
    panel.set_params(*(((0.0, 0.0, 0.0),) * 3 + (0.0,) * 9))
    panel.resize(1260, 400)
    panel.show()
    panel.set_extended_params({PRIMARY_CALIBRATION_VERSION_KEY: PRIMARY_CALIBRATION_CURRENT_VERSION})
    panel._switch_mode(mode)
    qtbot.waitUntil(panel.isVisible)
    spin = getattr(panel, attribute).spin
    cell = spin._value_label
    start = cell.mapTo(panel, cell.rect().center())
    assert panel.childAt(start) is spin
    window = panel.windowHandle()
    events = []
    panel.interaction_started.connect(lambda: events.append("start"))
    panel.grading_changed.connect(lambda *_: events.append("change"))
    panel.grading_extended_changed.connect(lambda _: events.append("change"))
    panel.interaction_ended.connect(lambda: events.append("end"))

    QTest.mousePress(window, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseRelease(window, Qt.MouseButton.LeftButton, pos=start + QPoint(dx, 0))

    assert spin.value() == pytest.approx(expected)
    assert events == ["start", "change", "end"]


@pytest.mark.parametrize(("mode", "wheel", "channel", "expected"), [
    ("wheels", "lift", "Y", 0.10),
    ("wheels", "gamma", "R", 0.05),
    ("wheels", "gain", "R", 1.26),
    ("wheels", "offset", "R", 34.60),
    ("bars", "offset", "R", 32.20),
    ("log", "log_shadow", "R", 0.22),
    ("log", "log_midtone", "R", 0.05),
    ("log", "log_highlight", "R", 0.22),
])
def test_calibrated_channel_number_drag_matches_resolve_readback(
    qtbot, mode: str, wheel: str, channel: str, expected: float,
) -> None:
    panel = GradingPanel()
    qtbot.addWidget(panel)
    panel.resize(1260, 450)
    panel.show()
    panel.set_extended_params({PRIMARY_CALIBRATION_VERSION_KEY: PRIMARY_CALIBRATION_CURRENT_VERSION})
    panel._switch_mode(mode)
    if mode == "bars":
        spin = getattr(panel.color_bars_panel, wheel)._nums[channel]
    else:
        field = "_bright_drag" if channel == "Y" else "_r_drag"
        spin = getattr(getattr(panel, wheel + "_wheel"), field)
    cell = spin._value_label
    pos = cell.mapTo(panel, cell.rect().center())
    assert panel.childAt(pos) is spin
    QTest.mousePress(panel.windowHandle(), Qt.MouseButton.LeftButton, pos=pos)
    QTest.mouseRelease(panel.windowHandle(), Qt.MouseButton.LeftButton, pos=pos + QPoint(120, 0))
    assert spin.value() == pytest.approx(expected)


@pytest.mark.parametrize(("attribute", "signal", "timer_name", "target"), [
    ("sat_slider", "grading_changed", "_grading_update_timer", 74.0),
    ("pivot_slider", "grading_extended_changed", "_extended_update_timer", 0.42),
])
def test_scalar_continuous_drag_coalesces_without_postponing_preview(
    qtbot, monkeypatch, attribute: str, signal: str, timer_name: str, target: float,
) -> None:
    panel = GradingPanel()
    qtbot.addWidget(panel)
    panel.resize(1260, 400)
    panel.show()
    timer = getattr(panel, timer_name)
    timer.setInterval(100)
    real_start = timer.start
    starts = []
    spin = getattr(panel, attribute).spin

    def start_timer() -> None:
        starts.append(True)
        real_start()

    monkeypatch.setattr(timer, "start", start_timer)
    changes = []
    getattr(panel, signal).connect(lambda *_: changes.append(spin.value()))
    window = panel.windowHandle()
    start = spin._value_label.mapTo(panel, spin._value_label.rect().center())

    QTest.mousePress(window, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(window, start + QPoint(30, 0))
    QTest.mouseMove(window, start + QPoint(60, 0))
    assert starts == [True]
    qtbot.waitUntil(lambda: bool(changes))
    assert len(changes) == 1
    QTest.mouseRelease(window, Qt.MouseButton.LeftButton, pos=start + QPoint(120, 0))
    assert changes[-1] == pytest.approx(target)
    assert len(changes) == 2


def test_scalar_fine_modifier_affects_only_the_remaining_drag(qtbot) -> None:
    panel = GradingPanel()
    qtbot.addWidget(panel)
    panel.resize(1260, 400)
    panel.show()
    spin = panel.sat_slider.spin
    window = panel.windowHandle()
    start = spin._value_label.mapTo(panel, spin._value_label.rect().center())

    QTest.mousePress(window, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(window, start + QPoint(40, 0))
    assert spin.value() == pytest.approx(58.0)
    QTest.mouseRelease(window, Qt.MouseButton.LeftButton,
                       Qt.KeyboardModifier.MetaModifier, start + QPoint(120, 0))
    assert spin.value() == pytest.approx(59.6)


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
        canvas = panel.lift_wheel._wheel_canvas
        radius = canvas._geometry()[1]
        assert radius == 100.0
        assert radius - canvas._inner_radius(radius) == 4.0
        panel.resize(1260, 470)
        qapp.processEvents()
        assert canvas._geometry()[1] == 100.0
        panel.resize(700, 300)
        qapp.processEvents()
        smaller = canvas._geometry()[1]
        assert 0.0 < smaller < 100.0
        assert smaller - canvas._inner_radius(smaller) == 4.0
    finally:
        panel.close()
        panel.deleteLater()


def test_master_dials_align_with_fixed_value_groups_in_all_modes(qtbot, qapp) -> None:
    panel = GradingPanel()
    qtbot.addWidget(panel)
    panel.show()
    qapp.processEvents()
    minimum_width = panel.minimumSizeHint().width()
    def wheel_controls(wheel: ColorWheel) -> tuple[QWidget, QWidget, QWidget]:
        first = wheel._bright_drag if wheel._bright_drag is not None else wheel._r_drag
        return wheel.master_jog, first, wheel._b_drag

    controls_by_mode = {
        "wheels": tuple(wheel_controls(wheel) for wheel in (
            panel.lift_wheel, panel.gamma_wheel, panel.gain_wheel, panel.offset_wheel)),
        "bars": tuple((column._dial, column._nums[column._channels[0]], column._nums["B"])
                      for column in panel.color_bars_panel.columns()),
        "log": tuple(wheel_controls(wheel) for wheel in (
            panel.log_shadow_wheel, panel.log_midtone_wheel,
            panel.log_highlight_wheel, panel.log_offset_wheel)),
    }
    for width in (minimum_width, 974, 975, 1300):
        panel.resize(width, 380)
        dial_positions = set()
        for mode, controls in controls_by_mode.items():
            panel._switch_mode(mode)
            qapp.processEvents()
            for dial, first, last in controls:
                assert dial.width() == dial.minimumWidth() == dial.maximumWidth()
                assert dial.height() == 14
                if dial.isVisible():
                    origin = dial.mapTo(panel, dial.rect().topLeft())
                    assert origin.x() == first.mapTo(panel, first.rect().topLeft()).x()
                    assert origin.x() + dial.width() - 1 == last.mapTo(panel, last.rect().bottomRight()).x()
                    dial_positions.add(origin.y())
        assert len(dial_positions) == 1


def test_master_dial_has_dark_ends_and_a_brighter_center(qtbot) -> None:
    dial = KnurledJogWheel(174)
    qtbot.addWidget(dial)
    image = dial.grab().toImage()
    ratio = image.devicePixelRatio()
    center_gap = dial.width() / 2.0 + 1.5
    middle_y = int(dial.height() / 2.0 * ratio)
    left, center, right = (
        image.pixelColor(int((center_gap + offset) * ratio), middle_y)
        for offset in (-54.0, 0.0, 54.0)
    )
    assert center.lightness() - max(left.lightness(), right.lightness()) >= 10
    assert abs(left.lightness() - right.lightness()) <= 2
    assert image.pixelColor(int(center_gap * ratio), int(2.5 * ratio)) == center


def test_all_offset_columns_fit_at_the_same_width_and_preserve_grade(qtbot, qapp) -> None:
    splitter = QSplitter()
    qtbot.addWidget(splitter)
    panel = GradingPanel()
    splitter.addWidget(panel)
    splitter.addWidget(QWidget())
    splitter.resize(1600, 380)
    splitter.show()
    qapp.processEvents()

    def set_panel_width(width: int) -> None:
        splitter.setSizes([width, splitter.width() - splitter.handleWidth() - width])
        qapp.processEvents()
        assert panel.width() == width

    offset = panel.offset_wheel
    rows = {
        "wheels": (panel._wheel_page, offset),
        "bars": (panel.color_bars_panel, panel.color_bars_panel.offset),
        "log": (panel._log_page, panel.log_offset_wheel),
    }
    set_panel_width(650)
    assert all(column.isHidden() for _, column in rows.values())
    minimum_width = panel.minimumSizeHint().width()
    set_panel_width(974)
    assert all(not column.isHidden() for _, column in rows.values())
    threshold = (max(row.minimumSizeHint().width() for row, _ in rows.values())
                 + panel.width() - panel._wheel_page.width())
    set_panel_width(threshold - 1)
    assert all(column.isHidden() for _, column in rows.values())
    assert panel.lum_mix_slider.isHidden()
    set_panel_width(threshold)
    for mode, (row, column) in rows.items():
        panel._mode_buttons[mode].click()
        qapp.processEvents()
        assert column.isVisible()
        assert panel.lum_mix_slider.isVisible() == (mode != "log")
        layout = row.layout()
        for index in range(layout.count()):
            widget = layout.itemAt(index).widget()
            assert widget.width() >= widget.minimumSizeHint().width()
            assert row.rect().contains(widget.geometry())
    assert panel.minimumSizeHint().width() == minimum_width

    offset._r_drag.setValue(40.0)
    panel.log_offset_wheel._g_drag.setValue(25.0)
    panel.lum_mix_slider.setValue(75.0)
    panel.flush_pending_changes()
    grade = panel.get_extended_params()
    red_value = offset._r_drag.value()
    assert grade["offset"][1] > 0.0
    assert grade["log_offset"][1] > 0.0
    updates = []
    panel.grading_extended_changed.connect(updates.append)
    set_panel_width(minimum_width)
    assert all(column.isHidden() for _, column in rows.values())
    assert panel.lum_mix_slider.isHidden()
    panel._switch_mode("bars")
    set_panel_width(threshold)
    assert panel.color_bars_panel.offset.isVisible()
    panel._switch_mode("wheels")
    qapp.processEvents()
    assert offset.isVisible()
    assert offset._r_drag.value() == red_value
    assert panel.lum_mix_slider.isVisible()
    assert panel.lum_mix_slider.value() == 75.0
    assert panel.get_extended_params() == grade
    panel.flush_pending_changes()
    assert updates == []


@pytest.mark.parametrize("mode,delta_y", [("lift", 0.08), ("gamma", 0.04), ("gain", 0.16)])
def test_master_dial_drag_only_changes_its_own_wheel_and_flushes(qapp, mode: str, delta_y: float) -> None:
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
        dial.mouseMoveEvent(_event(QMouseEvent.Type.MouseMove, press + QPointF(100.0, 0.0)))
        dial.mouseReleaseEvent(_event(QMouseEvent.Type.MouseButtonRelease, press + QPointF(120.0, 0.0)))
        assert (wheel.hue, wheel.sat) == (215.0, 0.17)
        expected_master = delta_y + (mode == "gain")
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


@pytest.mark.parametrize("mode,point,expected,tolerance", [
    ("lift", (0.120, 0.0), (0.0, -0.01, 0.08), 0.006),
    ("lift", (-0.120, 0.0), (0.0, 0.01, -0.08), 0.006),
    ("gamma", (0.085, -0.085), (0.02, -0.01, 0.03), 0.006),
    ("gain", (0.0, -0.122), (1.22, 0.93, 1.0), 0.006),
    ("gain", (0.0, 0.122), (0.78, 1.07, 1.0), 0.006),
    ("offset", (0.085, -0.085), (29.82, 22.96, 30.68), 0.17),
    ("log_shadow", (0.085, 0.085), (-0.11, 0.02, 0.13), 0.008),
    ("log_midtone", (0.085, 0.085), (-0.02, 0.0, 0.03), 0.008),
    ("log_highlight", (0.085, 0.085), (-0.11, 0.02, 0.13), 0.008),
])
def test_calibrated_wheel_tracks_resolve_direction_and_round_trips(
    qtbot, mode: str, point: tuple[float, float], expected: tuple[float, ...], tolerance: float,
) -> None:
    panel = GradingPanel()
    qtbot.addWidget(panel)
    panel.set_extended_params({PRIMARY_CALIBRATION_VERSION_KEY: 1})
    wheel = getattr(panel, mode + "_wheel")
    wheel._edit_knob(QPointF(*point))
    wheel.flush_pending_changes()
    values = tuple(field.value() for field in (wheel._r_drag, wheel._g_drag, wheel._b_drag))
    assert values == pytest.approx(expected, abs=tolerance)
    if wheel._bright_drag is not None:
        assert wheel._bright_drag.value() == pytest.approx(float(mode == "gain"))
    values = wheel.hue, wheel.sat, wheel.brightness
    wheel.set_hsv(*values)
    assert (wheel.knob_pos.x(), wheel.knob_pos.y()) == pytest.approx(point, abs=1e-12)
    wheel.set_rotation_degrees(210.0)
    assert (wheel.knob_pos.x(), wheel.knob_pos.y()) == pytest.approx((point[1], -point[0]))
    assert (wheel.hue, wheel.sat, wheel.brightness) == values


def test_calibrated_master_moves_channels_together_in_wheels_bars_and_log(qtbot) -> None:
    panel = GradingPanel()
    qtbot.addWidget(panel)
    panel.set_extended_params({PRIMARY_CALIBRATION_VERSION_KEY: 1})
    wheel = panel.lift_wheel
    wheel._edit_knob(QPointF(0.12, -0.09))
    panel.flush_pending_changes()
    color = QPointF(wheel.knob_pos)
    initial = wheel._wheel_rgb()

    wheel._on_jog_delta(120.0)
    panel.flush_pending_changes()
    assert wheel._wheel_rgb() == pytest.approx(tuple(value + 0.08 for value in initial))
    assert (wheel.knob_pos.x(), wheel.knob_pos.y()) == pytest.approx((color.x(), color.y()))
    panel.color_bars_panel.lift._on_dial_delta(-120.0)
    panel.flush_pending_changes()
    assert wheel._wheel_rgb() == pytest.approx(initial)
    assert panel.color_bars_panel.lift._nums["Y"].value() == pytest.approx(0.0)

    panel.log_midtone_wheel._on_jog_delta(-120.0)
    panel.offset_wheel._on_jog_delta(60.0)
    panel.flush_pending_changes()
    assert panel.log_midtone_wheel._r_drag.value() == pytest.approx(-0.16)
    assert panel.log_offset_wheel._r_drag.value() == pytest.approx(27.0)
    panel.color_bars_panel.offset._on_dial_delta(60.0)
    panel.flush_pending_changes()
    assert panel.offset_wheel._r_drag.value() == pytest.approx(35.0)


def test_calibrated_drag_respects_existing_chroma_and_master_limits(qtbot) -> None:
    panel = GradingPanel()
    qtbot.addWidget(panel)
    panel.set_extended_params({PRIMARY_CALIBRATION_VERSION_KEY: 1})

    offset = panel.offset_wheel
    offset.set_hsv(0.0, 0.0, 1.0)
    offset._edit_knob(QPointF(0.12, 0.0))
    assert offset._master_value() == pytest.approx(1.0)
    assert not offset._update_timer.isActive()

    gain = panel.gain_wheel
    gain.set_hsv(0.0, 1.0, 0.5)
    master = gain._master_value()
    gain._on_jog_delta(-120.0)
    assert gain._master_value() == pytest.approx(master - 0.16)
    assert min(gain._wheel_rgb()) >= 0.0
    assert gain.hue == pytest.approx(0.0)


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
