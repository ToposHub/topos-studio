"""Tests for the evolved DragValue numeric value box (Fusion-style).

Covers the upgraded interactions used by the composite node Properties panel:
- value clamp + valueChanged emission
- decimal formatting
- empty-label "bare value box" mode (panel places its own label beside it)
- drag-scrub with a movement threshold (so a clean double-click isn't a drag)
- inline double-click editing (Enter commits, Esc cancels) replacing the old
  modal QInputDialog
- middle-click reset to default
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QPointF, QEvent, Qt
from PyQt6.QtGui import QMouseEvent

from src.shared.ui.widgets.drag_value import DragValue


# ── event helpers ────────────────────────────────────────────────────────────

def _move_event(widget, x, button=Qt.MouseButton.LeftButton,
                mods=Qt.KeyboardModifier.NoModifier):
    return QMouseEvent(
        QEvent.Type.MouseMove, QPointF(x, 5), button, button, mods)


def _press_event(widget, x, button=Qt.MouseButton.LeftButton):
    return QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(x, 5), button, button,
        Qt.KeyboardModifier.NoModifier)


def _release_event(widget, x, button=Qt.MouseButton.LeftButton):
    return QMouseEvent(
        QEvent.Type.MouseButtonRelease, QPointF(x, 5), button, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier)


def _dclick_event(widget, x):
    return QMouseEvent(
        QEvent.Type.MouseButtonDblClick, QPointF(x, 5),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier)


# ── value / clamp / signal ───────────────────────────────────────────────────

def test_setvalue_clamps_to_range(qapp):
    dv = DragValue("", 0.0, 1.0, 0.5, decimals=2)
    assert dv.value() == 0.5
    captured = []
    dv.valueChanged.connect(captured.append)
    dv.setValue(5.0)        # above max → clamp to 1.0
    assert dv.value() == 1.0
    assert captured == [1.0]
    dv.setValue(-3.0)       # below min → clamp to 0.0
    assert dv.value() == 0.0
    assert captured[-1] == 0.0


def test_setvalue_no_signal_when_unchanged(qapp):
    dv = DragValue("", 0.0, 1.0, 0.5, decimals=2)
    captured = []
    dv.valueChanged.connect(captured.append)
    dv.setValue(0.5)        # same value → no emit
    assert captured == []


def test_decimal_formatting(qapp):
    dv = DragValue("", 0.0, 1.0, 0.0, decimals=3)
    dv.setValue(0.125)
    assert dv._format_value(dv.value()) == "0.125"
    dv.setDecimals(0)
    assert dv._format_value(dv.value()) == "0"


def test_integer_formatting_has_no_dot(qapp):
    dv = DragValue("n", 0.0, 10.0, 0.0, decimals=0)
    dv.setValue(4.0)
    assert dv._format_value(dv.value()) == "4"


# ── bare value-box mode ──────────────────────────────────────────────────────

def test_empty_label_creates_no_label_widget(qapp):
    """A bare DragValue (panel supplies its own label) has only the value cell —
    no separate parameter-name QLabel. A labeled one has two."""
    from PyQt6.QtWidgets import QLabel
    dv = DragValue("", 0.0, 1.0, 0.5, decimals=2)
    labels = dv.findChildren(QLabel)
    assert len(labels) == 1, "empty-label DragValue must have only the value cell QLabel"
    assert dv._label is None


def test_labeled_mode_still_creates_label(qapp):
    """mask_panel passes a label; that path must keep working."""
    from PyQt6.QtWidgets import QLabel
    dv = DragValue("Size", 0.0, 1.0, 0.5, decimals=2)
    labels = dv.findChildren(QLabel)
    assert any(l.text() == "Size" for l in labels)


# ── drag scrub + threshold ───────────────────────────────────────────────────

def test_drag_past_threshold_scrubs_value(qapp):
    dv = DragValue("", 0.0, 100.0, 50.0, decimals=1)
    dv.resize(120, 22)
    dv.show()
    dv.mousePressEvent(_press_event(dv, 10))
    # move less than the threshold → no change yet (click vs drag disambiguation)
    dv.mouseMoveEvent(_move_event(dv, 12))
    assert dv.value() == 50.0
    # move well past threshold → value scrubs up
    dv.mouseMoveEvent(_move_event(dv, 60))
    assert dv.value() > 50.0
    dv.mouseReleaseEvent(_release_event(dv, 60))


def test_tiny_jitter_does_not_scrub(qapp):
    """A click with sub-threshold jitter must not change the value (protects dblclick)."""
    dv = DragValue("", 0.0, 100.0, 50.0, decimals=1)
    dv.resize(120, 22)
    dv.show()
    dv.mousePressEvent(_press_event(dv, 10))
    dv.mouseMoveEvent(_move_event(dv, 11))   # 1px jitter
    dv.mouseReleaseEvent(_release_event(dv, 11))
    assert dv.value() == 50.0


# ── inline double-click editing ──────────────────────────────────────────────

def test_double_click_enters_inline_edit_mode(qapp):
    dv = DragValue("", 0.0, 1.0, 0.5, decimals=2)
    dv.show()
    assert dv.isEditing() is False
    dv.mouseDoubleClickEvent(_dclick_event(dv, 10))
    assert dv.isEditing() is True


def test_commit_edit_applies_typed_value(qapp):
    dv = DragValue("", 0.0, 1.0, 0.5, decimals=2)
    dv.show()
    dv.mouseDoubleClickEvent(_dclick_event(dv, 10))
    dv._edit.setText("0.75")
    dv._commit_edit()
    assert dv.isEditing() is False
    assert dv.value() == 0.75


def test_commit_clamps_out_of_range_typed_value(qapp):
    dv = DragValue("", 0.0, 1.0, 0.5, decimals=2)
    dv.show()
    dv.mouseDoubleClickEvent(_dclick_event(dv, 10))
    dv._edit.setText("42")
    dv._commit_edit()
    assert dv.value() == 1.0


def test_cancel_edit_restores_previous_value(qapp):
    dv = DragValue("", 0.0, 1.0, 0.5, decimals=2)
    dv.show()
    dv.mouseDoubleClickEvent(_dclick_event(dv, 10))
    dv._edit.setText("0.9")
    dv._cancel_edit()
    assert dv.isEditing() is False
    assert dv.value() == 0.5   # unchanged


# ── reset to default ─────────────────────────────────────────────────────────

def test_default_value_and_reset(qapp):
    dv = DragValue("", 0.0, 1.0, 0.25, decimals=2)
    dv.setValue(0.8)
    assert dv.defaultValue() == 0.25
    dv.setValue(dv.defaultValue())
    assert dv.value() == 0.25


# ── modifier speed tiers (default = old Shift fine; Shift = large; Ctrl = micro) ─

def test_default_drag_is_the_old_shift_fine_speed(qapp):
    """Default (no modifier) now scrubs at base×0.1 — the old Shift fine speed,
    so a drag no longer jumps a huge chunk of the range."""
    dv = DragValue("", 0.0, 100.0, 50.0, decimals=2)
    dv.resize(120, 22)
    dv.show()
    dv.mousePressEvent(_press_event(dv, 10))
    dv.mouseMoveEvent(_move_event(dv, 50))            # dx=40, no modifier
    dv.mouseReleaseEvent(_release_event(dv, 50))
    # base=0.5, default=0.05/px → dx=40 → +2.0
    assert abs(dv.value() - 52.0) < 1e-6


def test_shift_drag_is_large_range(qapp):
    """Shift = coarse / large-range sweep (base×10)."""
    dv = DragValue("", 0.0, 1000.0, 500.0, decimals=2)
    dv.resize(120, 22)
    dv.show()
    dv.mousePressEvent(_press_event(dv, 0))
    dv.mouseMoveEvent(_move_event(dv, 5, mods=Qt.KeyboardModifier.ShiftModifier))
    dv.mouseReleaseEvent(_release_event(dv, 5))
    # base=5, shift=base×1=5/px → dx=5 → +25 → 525
    assert abs(dv.value() - 525.0) < 1e-6


def test_drag_modifier_speeds_are_tiered(qapp):
    """For an identical drag, Ctrl (micro) < default < Shift (large)."""
    def run(mods):
        dv = DragValue("", 0.0, 1000.0, 500.0, decimals=3)
        dv.resize(120, 22)
        dv.show()
        dv.mousePressEvent(_press_event(dv, 0))
        dv.mouseMoveEvent(_move_event(dv, 10, mods=mods))   # dx=10
        dv.mouseReleaseEvent(_release_event(dv, 10))
        return dv.value()

    v_ctrl = run(Qt.KeyboardModifier.ControlModifier)
    v_default = run(Qt.KeyboardModifier.NoModifier)
    v_shift = run(Qt.KeyboardModifier.ShiftModifier)
    assert v_ctrl < v_default < v_shift


def test_ctrl_drag_floored_to_0_1_on_medium_range(qapp):
    """On a rotation-like range (360°), the micro tier is floored to exactly
    0.1/px so sub-0.1 wiggles never happen."""
    dv = DragValue("", -180.0, 180.0, 0.0, decimals=3)
    dv.resize(120, 22)
    dv.show()
    dv.mousePressEvent(_press_event(dv, 0))
    dv.mouseMoveEvent(_move_event(dv, 10, mods=Qt.KeyboardModifier.ControlModifier))
    dv.mouseReleaseEvent(_release_event(dv, 10))
    # base=1.8 → base×0.01=0.018, but floored to 0.1/px → dx=10 → +1.0
    assert abs(dv.value() - 1.0) < 1e-6


def test_ctrl_drag_stays_at_or_above_0_1_on_huge_range(qapp):
    """On a translate-like range (±8192), base×0.01 already exceeds 0.1, so the
    floor doesn't bind — but the per-pixel change is still ≥ 0.1."""
    dv = DragValue("", -8192.0, 8192.0, 0.0, decimals=3)
    dv.resize(120, 22)
    dv.show()
    dv.mousePressEvent(_press_event(dv, 0))
    dv.mouseMoveEvent(_move_event(dv, 10, mods=Qt.KeyboardModifier.ControlModifier))
    dv.mouseReleaseEvent(_release_event(dv, 10))
    # base=81.92 → base×0.01 = 0.8192/px → dx=10 → +8.192 (≥ 0.1 satisfied)
    assert dv.value() >= 0.1 - 1e-9
    assert abs(dv.value() - 8.192) < 1e-4


def test_ctrl_drag_proportional_on_small_range(qapp):
    """On a scale-like range (0.01–5.0) the 0.1 floor must NOT bind, otherwise
    one pixel would skip across the whole range. Micro stays proportional."""
    dv = DragValue("", 0.01, 5.0, 1.0, decimals=3)
    dv.resize(120, 22)
    dv.show()
    dv.mousePressEvent(_press_event(dv, 0))
    dv.mouseMoveEvent(_move_event(dv, 100, mods=Qt.KeyboardModifier.ControlModifier))
    dv.mouseReleaseEvent(_release_event(dv, 100))
    # base≈0.02495 → ctrl = base×0.01 ≈ 0.0002495/px → dx=100 → +0.02495
    assert abs(dv.value() - (1.0 + 0.02495)) < 1e-4
    # NOT floored to 0.1 (that would have clamped to the 5.0 ceiling)
    assert dv.value() < 2.0


def test_meta_modifier_also_means_micro(qapp):
    """⌘ Command on macOS surfaces as MetaModifier; it must act like Ctrl (micro)."""
    dv = DragValue("", 0.0, 1000.0, 500.0, decimals=3)
    dv.resize(120, 22)
    dv.show()
    dv.mousePressEvent(_press_event(dv, 0))
    dv.mouseMoveEvent(_move_event(dv, 10, mods=Qt.KeyboardModifier.MetaModifier))
    dv.mouseReleaseEvent(_release_event(dv, 10))
    ctrl = DragValue("", 0.0, 1000.0, 500.0, decimals=3)
    ctrl.resize(120, 22)
    ctrl.show()
    ctrl.mousePressEvent(_press_event(ctrl, 0))
    ctrl.mouseMoveEvent(_move_event(ctrl, 10, mods=Qt.KeyboardModifier.ControlModifier))
    ctrl.mouseReleaseEvent(_release_event(ctrl, 10))
    assert abs(dv.value() - ctrl.value()) < 1e-9
