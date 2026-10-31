"""Pure-math contract for DragValue gesture conversion (R-10 remediation).

The coordinate-to-value conversion used to depend on the widget's actual
pixel width, so the same drag produced different values under offscreen
platforms, different DPRs or panel widths.  The conversion is now a pure
function over (value span, modifiers) with a fixed 200px reference width;
these tests pin the formula itself, independent of any QWidget.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import Qt

from src.shared.ui.widgets.drag_value import (
    _DRAG_REFERENCE_WIDTH,
    _FINE_FLOOR,
    drag_speed_per_pixel,
)

NO = Qt.KeyboardModifier.NoModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier
CTRL = Qt.KeyboardModifier.ControlModifier
META = Qt.KeyboardModifier.MetaModifier


def test_reference_width_is_fixed_not_widget_width():
    assert _DRAG_REFERENCE_WIDTH == 200.0


def test_default_tier_is_the_fine_handfeel():
    # span 100 → base 0.5 → default ×0.1 = 0.05/px (matches the panel tests)
    assert drag_speed_per_pixel(100.0, NO) == pytest.approx(0.05)


def test_shift_tier_sweeps_the_full_range_per_reference_width():
    # span 1000 → base 5 → Shift ×1.0 = 5/px
    assert drag_speed_per_pixel(1000.0, SHIFT) == pytest.approx(5.0)


def test_ctrl_tier_is_proportional_and_tiered():
    # span 1000 → base 5 ≥ 0.1 → floor binds: max(0.05, 0.1) = 0.1/px
    assert drag_speed_per_pixel(1000.0, CTRL) == pytest.approx(0.1)
    assert drag_speed_per_pixel(1000.0, META) == pytest.approx(0.1)  # ⌘ == Ctrl


def test_ctrl_floor_binds_only_when_base_reaches_the_floor():
    # span 360 → base 1.8 ≥ 0.1 → floor binds: max(0.018, 0.1) = 0.1/px
    assert drag_speed_per_pixel(360.0, CTRL) == pytest.approx(_FINE_FLOOR)
    # span 16384 → base 81.92 → proportional 0.8192 already above the floor
    assert drag_speed_per_pixel(16384.0, CTRL) == pytest.approx(0.8192)


def test_ctrl_floor_never_skips_a_small_range():
    # span 4.99 → base 0.02495 < 0.1 → no floor, stays proportional so one
    # pixel cannot jump across the whole range
    assert drag_speed_per_pixel(4.99, CTRL) == pytest.approx(0.0002495, abs=1e-9)


def test_tiers_are_ordered_for_every_span_where_the_floor_defers():
    # For base ≥ 1.0 (span ≥ 200) the micro tier stays strictly below the
    # default tier; on smaller spans the 0.1 floor can legitimately exceed
    # base×0.1, which is why those spans are not listed here.
    for span in (360.0, 1000.0, 16384.0):
        speeds = [drag_speed_per_pixel(span, m) for m in (CTRL, NO, SHIFT)]
        assert speeds[0] < speeds[1] < speeds[2], span


def test_negative_span_uses_magnitude():
    assert drag_speed_per_pixel(-100.0, NO) == pytest.approx(0.05)


def test_widget_delegates_to_the_pure_function(qapp):
    from src.shared.ui.widgets.drag_value import DragValue
    dv = DragValue("", -180.0, 180.0, 0.0, decimals=3)
    assert dv._drag_sensitivity(CTRL) == pytest.approx(0.1)
    # Same result whatever the widget's actual width is.
    dv.resize(40, 24)
    assert dv._drag_sensitivity(CTRL) == pytest.approx(0.1)
