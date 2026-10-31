"""Tests for ToposClickableSlider's opt-in three-tier velocity drag.

The composite node Properties panel turns on ``enable_velocity_drag`` so that
dragging the slider *handle* honors the same default/Shift/Ctrl speed tiers as
the DragValue numeric box.  Since e3df12fe the base is width-adaptive:

    base = (maximum − minimum) / widget_width
    默认拖拽  = base × 1   （拖满控件宽度 = 完整值域）
    Shift    = base × 10  （大范围 / 粗调）
    Ctrl / ⌘ = base × 0.1（微调）

Two behaviors that must NOT regress:
- With the switch OFF (the default, used by the color page), mouse press falls
  through to the original "click groove → jump to position" path.
- Clicking the empty groove (outside the handle) still jumps, even when the
  switch is ON — only handle-drag is throttled to the three tiers.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QPointF, QEvent, Qt
from PyQt6.QtGui import QMouseEvent

from src.shared.ui.widgets.topos_controls import (
    _SLIDER_DEFAULT_SPEED, _SLIDER_LARGE_SPEED, _SLIDER_FINE_SPEED,
)
from src.shared.ui.widgets.topos_controls import ToposClickableSlider


# ── event helpers ────────────────────────────────────────────────────────────

def _press(x, y=5, button=Qt.MouseButton.LeftButton,
           mods=Qt.KeyboardModifier.NoModifier):
    return QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(x, y), button, button, mods)


def _move(x, y=5, button=Qt.MouseButton.LeftButton,
          mods=Qt.KeyboardModifier.NoModifier):
    return QMouseEvent(
        QEvent.Type.MouseMove, QPointF(x, y), button, button, mods)


def _release(x, y=5, button=Qt.MouseButton.LeftButton,
             mods=Qt.KeyboardModifier.NoModifier):
    return QMouseEvent(
        QEvent.Type.MouseButtonRelease, QPointF(x, y), button,
        Qt.MouseButton.NoButton, mods)


# ── shared fixture: a slider wide enough to have a real handle rect ──────────

@pytest.fixture
def slider(qapp):
    s = ToposClickableSlider(Qt.Orientation.Horizontal)
    s.setRange(0, 1000)        # base = 1000 / 200 = 5.0
    s.setValue(500)
    s.resize(220, 22)
    s.show()
    qapp.processEvents()
    s.enable_velocity_drag = True
    return s


def _handle_center_x(slider) -> float:
    hr = slider._handle_rect()
    return hr.center().x()


# ── opt-in default ───────────────────────────────────────────────────────────

def test_velocity_drag_disabled_by_default(qapp):
    """Color-page sliders must keep their pre-existing behavior untouched."""
    s = ToposClickableSlider(Qt.Orientation.Horizontal)
    assert s.enable_velocity_drag is False


# ── three-tier sensitivity ───────────────────────────────────────────────────

def test_drag_sensitivity_three_tiers(slider):
    """宽度自适应 base：default ×1, Shift ×10, Ctrl/Meta ×0.1。"""
    base = (slider.maximum() - slider.minimum()) / slider.width()
    assert slider._drag_sensitivity(Qt.KeyboardModifier.NoModifier) \
        == pytest.approx(base * _SLIDER_DEFAULT_SPEED)
    assert slider._drag_sensitivity(Qt.KeyboardModifier.ShiftModifier) \
        == pytest.approx(base * _SLIDER_LARGE_SPEED)
    assert slider._drag_sensitivity(Qt.KeyboardModifier.ControlModifier) \
        == pytest.approx(base * _SLIDER_FINE_SPEED)
    assert slider._drag_sensitivity(Qt.KeyboardModifier.MetaModifier) \
        == pytest.approx(base * _SLIDER_FINE_SPEED)


# ── handle-drag default tier ─────────────────────────────────────────────────

def test_drag_handle_default_tier(slider):
    """Default handle-drag moves ≈ dx × base × 1（拖满宽度 = 完整值域）."""
    start = slider.value()
    cx = _handle_center_x(slider)
    slider.mousePressEvent(_press(cx))
    assert slider._vdrag_active is True
    # width-adaptive: dx=100 → +1000/width × 100
    expected = start + (slider.maximum() - slider.minimum()) / slider.width() * 100
    slider.mouseMoveEvent(_move(cx + 100))
    assert slider.value() == pytest.approx(expected, abs=2)
    slider.mouseReleaseEvent(_release(cx + 100))
    assert slider._vdrag_active is False


def test_drag_handle_shift_tier(slider):
    """Shift handle-drag moves ≈ dx × base × 10 (10× the default)."""
    start = slider.value()
    cx = _handle_center_x(slider)
    slider.mousePressEvent(_press(cx))
    slider.mouseMoveEvent(_move(cx + 100,
                                mods=Qt.KeyboardModifier.ShiftModifier))
    # ×10 exceeds the range → clamped to maximum
    assert slider.value() == slider.maximum()
    slider.mouseReleaseEvent(_release(cx + 100,
                                      mods=Qt.KeyboardModifier.ShiftModifier))


def test_drag_handle_ctrl_tier(slider):
    """Ctrl handle-drag moves ≈ dx × base × 0.1 (微调)."""
    start = slider.value()
    cx = _handle_center_x(slider)
    slider.mousePressEvent(_press(cx))
    # width-adaptive: 0.1 × 1000/width × dx
    expected = start + (slider.maximum() - slider.minimum()) / slider.width() * 0.1 * 200
    slider.mouseMoveEvent(_move(cx + 200,
                                mods=Qt.KeyboardModifier.ControlModifier))
    assert slider.value() == pytest.approx(expected, abs=2)
    slider.mouseReleaseEvent(_release(cx + 200,
                                      mods=Qt.KeyboardModifier.ControlModifier))


# ── accumulator avoids precision loss ────────────────────────────────────────

def test_fine_drag_accumulates(slider):
    """Small per-move increments that round to <1 must still sum up over moves."""
    cx = _handle_center_x(slider)
    start = slider.value()
    slider.mousePressEvent(_press(cx))
    # Ctrl: 0.1 × base/px; 10px moves accumulate over 20 moves → 200 px total
    expected = start + (slider.maximum() - slider.minimum()) / slider.width() * 0.1 * 200
    for _ in range(20):
        cx += 10
        slider.mouseMoveEvent(_move(cx,
                                    mods=Qt.KeyboardModifier.ControlModifier))
    assert slider.value() == pytest.approx(expected, abs=2)
    slider.mouseReleaseEvent(_release(cx,
                                      mods=Qt.KeyboardModifier.ControlModifier))


# ── click on empty groove still jumps (even with velocity ON) ────────────────

def test_click_groove_jumps_when_velocity_on(slider):
    """Clicking outside the handle must keep the legacy jump behavior."""
    hr = slider._handle_rect()
    # Pick a point well to the right of the handle, inside the widget.
    click_x = min(slider.width() - 2, hr.right() + 40)
    expected = slider._pos_to_value_horizontal(click_x)
    slider.mousePressEvent(_press(click_x))
    assert slider._vdrag_active is False          # didn't start a drag session
    assert slider.value() == expected             # jumped straight to position
    slider.mouseReleaseEvent(_release(click_x))


# ── switch OFF → original behavior (no velocity session ever) ────────────────

def test_velocity_off_does_not_engage(qapp):
    """With the switch off, pressing the handle does NOT start a vdrag session
    and falls through to the groove-jump + super() path."""
    s = ToposClickableSlider(Qt.Orientation.Horizontal)
    s.setRange(0, 1000)
    s.setValue(500)
    s.resize(220, 22)
    s.show()
    qapp.processEvents()
    assert s.enable_velocity_drag is False
    cx = s._handle_rect().center().x()
    s.mousePressEvent(_press(cx))
    assert s._vdrag_active is False
