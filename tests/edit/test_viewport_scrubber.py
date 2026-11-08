"""Monitor seek gestures preserve SourceSlider's original signal lifecycle."""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtTest import QTest

from src.features.edit.ui.source_viewer import SourceSlider
from src.features.edit.ui.viewport_scrubber import (
    MonitorScrubber, cancel_monitor_scrub, scrubber_mouse_move, scrubber_mouse_press,
    scrubber_mouse_release,
)


def _mouse(kind: QEvent.Type, x: float, button: Qt.MouseButton, buttons: Qt.MouseButton) -> QMouseEvent:
    return QMouseEvent(kind, QPointF(x, 12.0), QPointF(x, 12.0), button, buttons, Qt.KeyboardModifier.NoModifier)


@pytest.mark.parametrize("slider_type", [MonitorScrubber, SourceSlider])
def test_seek_starts_before_value_change_and_finishes_once(qapp, widget_shelter, slider_type) -> None:
    slider = slider_type(widget_shelter)
    slider.resize(240, 24)
    slider.setRange(0, 100)
    events: list[tuple[str, int]] = []
    slider.sliderPressed.connect(lambda: events.append(("pressed", slider.value())))
    slider.valueChanged.connect(lambda value: events.append(("value", value)))
    slider.sliderReleased.connect(lambda: events.append(("released", slider.value())))
    assert scrubber_mouse_press(slider, _mouse(QEvent.Type.MouseButtonPress, 120.0, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton))
    assert slider.isSliderDown()
    assert events == [("pressed", 0), ("value", 50)]
    assert scrubber_mouse_move(slider, _mouse(QEvent.Type.MouseMove, 180.0, Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton))
    assert scrubber_mouse_release(slider, _mouse(QEvent.Type.MouseButtonRelease, 219.0, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton))
    assert events == [("pressed", 0), ("value", 50), ("value", 80), ("value", 100), ("released", 100)]
    assert not slider.isSliderDown()
    assert not scrubber_mouse_release(slider, _mouse(QEvent.Type.MouseButtonRelease, 219.0, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton))


def test_seek_clamps_outside_track_and_handles_nonzero_range(qapp, widget_shelter) -> None:
    slider = MonitorScrubber(widget_shelter)
    slider.resize(300, 24)
    slider.setRange(100, 199)
    scrubber_mouse_press(slider, _mouse(QEvent.Type.MouseButtonPress, -100.0, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton))
    assert slider.value() == 100
    scrubber_mouse_release(slider, _mouse(QEvent.Type.MouseButtonRelease, 900.0, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton))
    assert slider.value() == 199


def test_disabled_and_nonleft_gestures_leave_slider_untouched(qapp, widget_shelter) -> None:
    slider = MonitorScrubber(widget_shelter)
    slider.resize(300, 24)
    slider.setRange(0, 100)
    slider.setValue(33)
    assert not scrubber_mouse_press(slider, _mouse(QEvent.Type.MouseButtonPress, 150.0, Qt.MouseButton.RightButton, Qt.MouseButton.RightButton))
    slider.setEnabled(False)
    assert not scrubber_mouse_press(slider, _mouse(QEvent.Type.MouseButtonPress, 150.0, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton))
    assert slider.value() == 33
    assert not slider.isSliderDown()


def test_single_frame_scrub_and_marks_render_without_division_by_zero(qapp, widget_shelter) -> None:
    slider = MonitorScrubber(widget_shelter)
    slider.resize(200, 24)
    slider.setRange(0, 0)
    slider.set_in_out_points(0, 0)
    scrubber_mouse_press(slider, _mouse(QEvent.Type.MouseButtonPress, 120.0, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton))
    scrubber_mouse_release(slider, _mouse(QEvent.Type.MouseButtonRelease, 180.0, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton))
    assert slider.value() == 0
    image = slider.grab().toImage()
    assert image.height() == 24
    assert image.pixelColor(100, 1).name() == "#202024"


def test_monitor_scrubber_keeps_qslider_keyboard_navigation(qapp, widget_shelter) -> None:
    slider = MonitorScrubber(widget_shelter)
    slider.setRange(0, 100)
    slider.setValue(10)
    QTest.keyClick(slider, Qt.Key.Key_Right)
    assert slider.value() == 11
    assert not slider.isSliderDown()


def test_played_segment_stays_dark_and_only_marked_range_is_filled(qapp, widget_shelter) -> None:
    slider = MonitorScrubber(widget_shelter)
    slider.resize(300, 24)
    slider.setRange(0, 100)
    slider.setValue(75)
    image = slider.grab().toImage()
    assert image.pixelColor(85, 13).name() == "#151518"
    slider.set_in_out_points(20, 60)
    marked = slider.grab().toImage()
    assert marked.pixelColor(100, 13).name() == "#707074"
    assert marked.pixelColor(200, 13).name() == "#151518"


@pytest.mark.parametrize("slider_type", [MonitorScrubber, SourceSlider])
def test_disable_then_enable_starts_a_fresh_gesture(qapp, widget_shelter, slider_type) -> None:
    slider = slider_type(widget_shelter)
    slider.setProperty("resolveMonitorScrubber", True)
    slider.resize(300, 24)
    events: list[str] = []
    slider.sliderPressed.connect(lambda: events.append("pressed"))
    slider.sliderReleased.connect(lambda: events.append("released"))
    press = lambda: scrubber_mouse_press(slider, _mouse(QEvent.Type.MouseButtonPress, 120.0, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton))
    assert press()
    slider.setEnabled(False)
    assert not slider.isSliderDown()
    assert not slider.property("toposMonitorScrubberDrag")
    assert events == ["pressed", "released"]
    slider.setEnabled(True)
    assert press()
    assert slider.isSliderDown()
    scrubber_mouse_release(slider, _mouse(QEvent.Type.MouseButtonRelease, 150.0, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton))
    assert events == ["pressed", "released", "pressed", "released"]


@pytest.mark.parametrize("event_type", [QEvent.Type.Hide, QEvent.Type.UngrabMouse])
@pytest.mark.parametrize("slider_type", [MonitorScrubber, SourceSlider])
def test_interruption_cancels_drag_once(qapp, widget_shelter, event_type, slider_type) -> None:
    from PyQt6.QtWidgets import QApplication

    slider = slider_type(widget_shelter)
    slider.setProperty("resolveMonitorScrubber", True)
    slider.resize(300, 24)
    released: list[bool] = []
    slider.sliderReleased.connect(lambda: released.append(True))
    scrubber_mouse_press(slider, _mouse(QEvent.Type.MouseButtonPress, 120.0, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton))
    QApplication.sendEvent(slider, QEvent(event_type))
    assert not slider.isSliderDown()
    assert not slider.property("toposMonitorScrubberDrag")
    assert released == [True]
    cancel_monitor_scrub(slider)
    assert released == [True]
    assert not scrubber_mouse_release(slider, _mouse(QEvent.Type.MouseButtonRelease, 150.0, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton))


def test_press_recovers_stale_property_on_existing_source_slider(qapp, widget_shelter) -> None:
    slider = SourceSlider(widget_shelter)
    slider.resize(300, 24)
    pressed: list[bool] = []
    slider.sliderPressed.connect(lambda: pressed.append(True))
    slider.setProperty("toposMonitorScrubberDrag", True)
    assert not slider.isSliderDown()
    scrubber_mouse_press(slider, _mouse(QEvent.Type.MouseButtonPress, 120.0, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton))
    assert slider.isSliderDown()
    assert pressed == [True]


def test_disabled_release_cleans_stale_drag_without_duplicate_release(qapp, widget_shelter) -> None:
    slider = SourceSlider(widget_shelter)
    slider.resize(300, 24)
    released: list[bool] = []
    slider.sliderReleased.connect(lambda: released.append(True))
    scrubber_mouse_press(slider, _mouse(QEvent.Type.MouseButtonPress, 120.0, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton))
    slider.setEnabled(False)
    assert released == [True]
    value = slider.value()
    scrubber_mouse_release(slider, _mouse(QEvent.Type.MouseButtonRelease, 290.0, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton))
    assert not slider.property("toposMonitorScrubberDrag")
    assert slider.value() == value
    assert released == [True]
