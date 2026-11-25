"""Direct-position mouse scrubbing shared by the Edit and Color monitors."""

from __future__ import annotations

from collections.abc import Callable
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QSlider


_DRAG_PROPERTY = "toposMonitorScrubberDrag"


def scrubber_track_bounds(slider: QSlider, margin: float | None = None) -> tuple[float, float]:
    """Use the same inset for drawing and mouse-to-frame conversion."""
    if margin is None:
        margin = 20.0 if slider.width() >= 240 else 12.0
    left = min(margin, max(0.0, (slider.width() - 1.0) / 2.0))
    return left, max(left, slider.width() - 1.0 - left)


def _mouse_value(slider: QSlider, x: float, margin: float | None) -> int:
    left, right = scrubber_track_bounds(slider, margin)
    fraction = max(0.0, min(1.0, (x - left) / (right - left))) if right > left else 0.0
    if slider.invertedAppearance():
        fraction = 1.0 - fraction
    return slider.minimum() + round(fraction * (slider.maximum() - slider.minimum()))


def cancel_monitor_scrub(slider: QSlider) -> None:
    """End an interrupted drag once, including Qt's already-released path."""
    slider.setProperty(_DRAG_PROPERTY, False)
    if slider.isSliderDown():
        slider.setSliderDown(False)


def scrubber_mouse_press(
    slider: QSlider, event: QMouseEvent, *, margin: float | None = None,
    start_at_pointer: bool = False,
    on_start: Callable[[int], None] | None = None,
) -> bool:
    """Start one direct seek gesture before delivering its first frame value."""
    if not slider.isEnabled() or event.button() != Qt.MouseButton.LeftButton:
        return False
    if slider.orientation() != Qt.Orientation.Horizontal:
        return False
    value = _mouse_value(slider, event.position().x(), margin)
    if not slider.property(_DRAG_PROPERTY) or not slider.isSliderDown():
        slider.setProperty(_DRAG_PROPERTY, True)
        slider.setFocus(Qt.FocusReason.MouseFocusReason)
        if on_start is not None:
            on_start(value)
        if start_at_pointer:
            slider.setValue(value)
        slider.setSliderDown(True)
    slider.setValue(value)
    event.accept()
    return True


def scrubber_mouse_move(
    slider: QSlider, event: QMouseEvent, *, margin: float | None = None,
) -> bool:
    """Chase a held left-button gesture, retaining QSlider's signal behavior."""
    if not slider.isEnabled() or not slider.property(_DRAG_PROPERTY):
        return False
    if not event.buttons() & Qt.MouseButton.LeftButton:
        return False
    slider.setValue(_mouse_value(slider, event.position().x(), margin))
    event.accept()
    return True


def scrubber_mouse_release(
    slider: QSlider, event: QMouseEvent, *, margin: float | None = None,
) -> bool:
    """Finalize the value before emitting the existing sliderReleased signal."""
    if event.button() != Qt.MouseButton.LeftButton:
        return False
    if not slider.isEnabled():
        was_dragging = bool(slider.property(_DRAG_PROPERTY) or slider.isSliderDown())
        cancel_monitor_scrub(slider)
        if was_dragging:
            event.accept()
        return was_dragging
    if not slider.property(_DRAG_PROPERTY):
        return False
    slider.setValue(_mouse_value(slider, event.position().x(), margin))
    cancel_monitor_scrub(slider)
    event.accept()
    return True
