"""Batch 5 tests: Time/Value axis display modes driven by the footer combos.

Validates that switching the X-axis combo (Frame/Seconds/Timecode) and the
Y-axis combo (Value/Normalized) actually changes how ticks are formatted,
and that the SplinePanel wiring routes the combo to the view.
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.ui.spline_model import SplineModel
from src.features.composite.ui.spline_view import SplineView, _frame_to_timecode
from src.features.composite.ui.spline_panel import SplinePanel
from src.shared.keyframe import Keyframe, KeyframeTrack


@pytest.fixture
def view(qapp):
    g = NodeGraph()
    t = TransformNode()
    tr = KeyframeTrack(param_name="rotation", min_val=0.0, max_val=360.0)
    for f, v in [(0, 0.0), (24, 180.0), (48, 360.0)]:
        tr.add_keyframe(Keyframe(f, v))
    t.set_keyframe_track("rotation", tr)
    g.add_node(t)
    m = SplineModel()
    m.rebuild(g)
    for c in m.curves:
        c.selected = True
    view = SplineView(m)
    view.resize(960, 400)
    view.fit_view()
    return view, m


# ── time mode formatting ────────────────────────────────────────────────

def test_frame_mode_default(view):
    view, _ = view
    assert view._time_mode == "frame"
    assert view._fmt_frame_label(42) == "42"


def test_seconds_mode_formats_as_seconds(view):
    view, _ = view
    view.set_fps(24.0)
    view.set_time_mode("seconds")
    # Frame 48 → 2.00s
    assert view._fmt_frame_label(48) == "2.00s"


def test_timecode_mode_formats_as_smpte(view):
    view, _ = view
    view.set_fps(24.0)
    view.set_time_mode("timecode")
    # Frame 48 at 24fps → 00:00:02:00
    assert view._fmt_frame_label(48) == "00:00:02:00"
    # Frame 25 at 24fps → 00:00:01:01
    assert view._fmt_frame_label(25) == "00:00:01:01"


def test_frame_to_timecode_helper():
    assert _frame_to_timecode(0, 24) == "00:00:00:00"
    assert _frame_to_timecode(23, 24) == "00:00:00:23"
    assert _frame_to_timecode(24, 24) == "00:00:01:00"
    # 1 minute at 30fps
    assert _frame_to_timecode(1800, 30) == "00:01:00:00"


def test_set_time_mode_ignores_invalid(view):
    view, _ = view
    view.set_time_mode("bogus")
    assert view._time_mode == "frame"


# ── value mode formatting ───────────────────────────────────────────────

def test_value_mode_default(view):
    view, m = view
    curve = m.curves[0]
    assert view._value_mode == "value"
    # Raw value pass-through.
    assert view._fmt_value_label(180.0, curve) == "180"


def test_normalized_mode_maps_to_0_1(view):
    view, m = view
    curve = m.curves[0]  # min 0, max 360
    view.set_value_mode("normalized")
    # Value 0 → 0.00, value 180 → 0.50, value 360 → 1.00
    assert view._fmt_value_label(0.0, curve) == "0.00"
    assert view._fmt_value_label(180.0, curve) == "0.50"
    assert view._fmt_value_label(360.0, curve) == "1.00"


def test_normalized_without_curve_falls_back(view):
    view, _ = view
    view.set_value_mode("normalized")
    # No reference curve → numeric fallback.
    assert view._fmt_value_label(123.0, None) == "123"


# ── panel wiring ────────────────────────────────────────────────────────

def test_panel_time_combo_routes_to_view(qapp):
    g = NodeGraph()
    t = TransformNode()
    tr = KeyframeTrack(param_name="rotation", min_val=0.0, max_val=360.0)
    tr.add_keyframe(Keyframe(0, 0.0))
    t.set_keyframe_track("rotation", tr)
    g.add_node(t)
    panel = SplinePanel()
    panel.set_node_graph(g)
    panel.resize(960, 400)
    # Switch to Timecode.
    panel._time_combo.setCurrentText("Timecode")
    assert panel._view._time_mode == "timecode"
    # Switch to Seconds.
    panel._time_combo.setCurrentText("Seconds")
    assert panel._view._time_mode == "seconds"


def test_panel_value_combo_routes_to_view(qapp):
    g = NodeGraph()
    t = TransformNode()
    tr = KeyframeTrack(param_name="rotation", min_val=0.0, max_val=360.0)
    tr.add_keyframe(Keyframe(0, 0.0))
    t.set_keyframe_track("rotation", tr)
    g.add_node(t)
    panel = SplinePanel()
    panel.set_node_graph(g)
    panel.resize(960, 400)
    panel._value_combo.setCurrentText("Normalized")
    assert panel._view._value_mode == "normalized"
    panel._value_combo.setCurrentText("Value")
    assert panel._view._value_mode == "value"
