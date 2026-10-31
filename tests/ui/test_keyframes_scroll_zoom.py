"""P4-3 tests: Keyframes panel wheel scroll + zoom (plan §6 P4-3)."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtGui import QWheelEvent

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.ui.keyframes_panel import KeyframesPanel
from src.shared.keyframe import Keyframe, KeyframeTrack


@pytest.fixture
def panel(qapp):
    g = NodeGraph()
    t = TransformNode()
    tr = KeyframeTrack(param_name="rotation")
    tr.add_keyframe(Keyframe(0, 0.0))
    tr.add_keyframe(Keyframe(10, 45.0))
    t.set_keyframe_track("rotation", tr)
    g.add_node(t)
    p = KeyframesPanel()
    p.set_node_graph(g)
    p.resize(800, 300)
    return p


def _wheel(panel, mods=Qt.KeyboardModifier.NoModifier, delta=120):
    ev = QWheelEvent(QPointF(400, 150), QPointF(0, 0),
                     QPoint(0, 0), QPoint(0, delta),
                     Qt.MouseButton.NoButton, mods,
                     Qt.ScrollPhase.NoScrollPhase, False)
    panel.wheelEvent(ev)


def test_plain_wheel_vertical_scroll(panel):
    before = panel._timeline_view._scroll_y
    _wheel(panel, delta=120)
    after = panel._timeline_view._scroll_y
    assert after != before  # vertical scroll applied


def test_ctrl_wheel_zooms(panel):
    before = panel._frame_mapper.pixels_per_frame
    _wheel(panel, mods=Qt.KeyboardModifier.ControlModifier, delta=120)
    after = panel._frame_mapper.pixels_per_frame
    assert after > before  # zoomed in


def test_shift_wheel_horizontal_pan(panel):
    before = panel._frame_mapper.scroll_offset
    _wheel(panel, mods=Qt.KeyboardModifier.ShiftModifier, delta=120)
    after = panel._frame_mapper.scroll_offset
    assert after != before  # panned horizontally


def test_ctrl_wheel_negative_zooms_out(panel):
    before = panel._frame_mapper.pixels_per_frame
    _wheel(panel, mods=Qt.KeyboardModifier.ControlModifier, delta=-120)
    after = panel._frame_mapper.pixels_per_frame
    assert after < before  # zoomed out
