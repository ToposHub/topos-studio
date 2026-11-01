"""Rendered hue, pointer input and primary numeric edits must describe one grade."""
from __future__ import annotations

import math

import numpy as np
import pytest
from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QSlider

from src.features.color.core.composition_uniforms import params_to_rgb
from src.features.color.core.the_decompiler import GradingMath
from src.features.color.ui.color_wheel import ColorWheel, GradingPanel


def test_immediate_version_save_flushes_throttled_wheel_and_slider_before_snapshot(qtbot):
    panel = GradingPanel()
    qtbot.addWidget(panel)
    panel.current_stack_id = "stack"
    rendered = {}
    saved = []
    panel.grading_changed.connect(lambda *args: rendered.update(gamma=args[1], exposure=args[3]))
    panel.set_save_callback(lambda params: saved.append((dict(params), dict(rendered))))
    panel.exp_slider.setValue(1.25)
    panel.gamma_wheel._on_brightness_drag_changed(0.5)
    assert panel._grading_update_timer.isActive()
    assert panel.gamma_wheel._update_timer.isActive()
    panel.trigger_save()
    params, snapshot = saved[0]
    assert snapshot["exposure"] == params["exposure"] == 1.25
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
    center, radius = canvas._geometry()
    try:
        for hue in range(0, 360, 60):
            angle = math.radians(hue + rotation)
            direction = QPointF(math.cos(angle), -math.sin(angle))
            point = center + direction * (radius - 1.5)
            color = pixels.pixelColor(round(point.x()), round(point.y()))
            error = abs((color.hsvHueF() * 360.0 - hue + 180.0) % 360.0 - 180.0)
            assert color.hsvSaturationF() > 0.8
            assert error < 4.0

            canvas._update_knob(center + direction * (radius * 0.2))
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
    center, radius = canvas._geometry()
    press = center + QPointF(20, 10)
    events = []
    wheel.interaction_started.connect(lambda: events.append("start"))
    wheel.changed.connect(lambda *args: events.append("change"))
    wheel.interaction_ended.connect(lambda: events.append("end"))
    try:
        canvas.mousePressEvent(_event(QMouseEvent.Type.MouseButtonPress, press))
        assert wheel.knob_pos == before  # clicking never jumps the correction
        canvas.mouseMoveEvent(_event(QMouseEvent.Type.MouseMove, press + QPointF(12, -8)))
        expected = before + QPointF(12 / radius, -8 / radius)
        assert wheel.knob_pos.x() == pytest.approx(expected.x())
        assert wheel.knob_pos.y() == pytest.approx(expected.y())
        canvas.mouseReleaseEvent(_event(QMouseEvent.Type.MouseButtonRelease, press + QPointF(12, -8)))
        assert events == ["start", "change", "end"]
        assert not wheel._update_timer.isActive()
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
        assert set(panel._wheel_page.findChildren(QSlider)) == {
            wheel.brightness_slider for wheel in
            (panel.lift_wheel, panel.gamma_wheel, panel.gain_wheel)
        }
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
        assert not panel._grading_update_timer.isActive()
    finally:
        panel.close()
        panel.deleteLater()


def test_narrow_panel_keeps_all_channel_fields_inside_their_wheel(qapp):
    panel = GradingPanel()
    panel.resize(510, 250)
    panel.show()
    qapp.processEvents()
    try:
        for wheel in (panel.lift_wheel, panel.gamma_wheel, panel.gain_wheel):
            first = wheel._bright_drag.mapTo(wheel, wheel._bright_drag.rect().topLeft())
            last = wheel._b_drag.mapTo(wheel, wheel._b_drag.rect().bottomRight())
            assert first.x() >= 0
            assert last.x() < wheel.width()
            assert last.y() < wheel.height()
    finally:
        panel.close()
        panel.deleteLater()


@pytest.mark.parametrize("mode", ["lift", "gamma", "gain"])
def test_master_dial_drag_only_changes_its_own_wheel_and_flushes(qapp, mode: str) -> None:
    panel = GradingPanel()
    panel.resize(760, 390)
    panel.show()
    qapp.processEvents()
    wheel = getattr(panel, mode + "_wheel")
    wheel.set_hsv(215.0, 0.17, 0.0)
    dial = wheel.brightness_slider
    events = []
    panel.interaction_started.connect(lambda: events.append("start"))
    panel.grading_changed.connect(lambda *args: events.append("change"))
    panel.interaction_ended.connect(lambda: events.append("end"))
    try:
        press = QPointF(20.0, 11.0)
        dial.mousePressEvent(_event(QMouseEvent.Type.MouseButtonPress, press))
        assert wheel.brightness == 0.0  # no absolute jump on a trough press
        dial.mouseMoveEvent(_event(QMouseEvent.Type.MouseMove, press + QPointF(40.0, 0.0)))
        dial.mouseReleaseEvent(_event(QMouseEvent.Type.MouseButtonRelease, press + QPointF(40.0, 0.0)))
        assert wheel.brightness == pytest.approx(0.04)
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
        assert wheel.brightness_slider.value() == -1235
        assert wheel.brightness == -0.123456  # display precision cannot quantize state
        assert changed == []
        assert not wheel._update_timer.isActive()
        wheel._bright_drag.setValue(1.0)
        assert wheel.brightness_slider.value() == round(math.sqrt(1.0 / 6.0) * 10000)
        wheel._r_drag.setValue(0.4)
        assert wheel.brightness_slider.value() == round(wheel.brightness * 10000)
        wheel.reset()
        assert wheel.brightness_slider.value() == 0
        changed.clear()
        wheel.blockSignals(True)
        wheel.set_hsv(120.0, 0.15, 0.5)
        wheel.blockSignals(False)
        assert wheel.brightness_slider.value() == 5000
        assert changed == []
        assert not wheel._update_timer.isActive()
    finally:
        wheel.deleteLater()
