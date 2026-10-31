"""P4 tests: Spline curve tree interaction + playhead seek (plan §6)."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QMouseEvent

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.nodes.blur_node import BlurNode
from src.features.composite.ui.spline_panel import SplinePanel
from src.shared.keyframe import Keyframe, KeyframeTrack


@pytest.fixture
def panel_with_curves(qapp):
    g = NodeGraph()
    t = TransformNode()
    tr = KeyframeTrack(param_name="rotation")
    tr.add_keyframe(Keyframe(0, 0.0))
    tr.add_keyframe(Keyframe(10, 45.0))
    t.set_keyframe_track("rotation", tr)
    b = BlurNode()
    br = KeyframeTrack(param_name="radius")
    br.add_keyframe(Keyframe(0, 1.0))
    br.add_keyframe(Keyframe(20, 8.0))
    b.set_keyframe_track("radius", br)
    g.add_node(t)
    g.add_node(b)
    panel = SplinePanel()
    panel.set_node_graph(g)
    panel.resize(960, 400)
    return panel, g, t, b


# ── P4-1: curve tree selection + visibility ─────────────────────────────

def test_click_row_selects_curve(panel_with_curves):
    panel, g, t, b = panel_with_curves
    tree = panel._tree
    # Click the second row (blur radius) label area.
    ev = QMouseEvent(QMouseEvent.Type.MouseButtonPress,
                     QPointF(40.0, 26.0), Qt.MouseButton.LeftButton,
                     Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    tree.mousePressEvent(ev)
    selected = [c.curve_id for c in panel._model.curves if c.selected]
    assert len(selected) == 1


def test_click_checkbox_toggles_visibility(panel_with_curves):
    panel, g, t, b = panel_with_curves
    tree = panel._tree
    assert all(c.visible for c in panel._model.curves)
    # Click the checkbox area (x<18) of the first row.
    ev = QMouseEvent(QMouseEvent.Type.MouseButtonPress,
                     QPointF(8.0, 11.0), Qt.MouseButton.LeftButton,
                     Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    tree.mousePressEvent(ev)
    visible = [c.visible for c in panel._model.curves]
    # The clicked curve is now hidden.
    assert visible[0] is False


def test_hidden_curve_not_in_visible_curves(panel_with_curves):
    panel, g, t, b = panel_with_curves
    first = panel._model.curves[0]
    panel._model.set_visible(first.curve_id, False)
    vis = panel._model.visible_curves()
    assert all(c.curve_id != first.curve_id for c in vis)


# ── P4-2: Spline playhead drag ──────────────────────────────────────────

def test_ruler_click_seeks_playhead(panel_with_curves):
    panel, g, t, b = panel_with_curves
    view = panel._view
    received = []
    view.current_frame_changed.connect(received.append)
    # Click in the ruler band (y <= margin_top=22).
    # Find an x within the plot that maps to a known frame.
    view.fit_view()
    from PyQt6.QtCore import QPointF as QPF
    # click at plot center x
    cx = view.width() // 2
    ev = QMouseEvent(QMouseEvent.Type.MouseButtonPress,
                     QPointF(cx, 10.0), Qt.MouseButton.LeftButton,
                     Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    view.mousePressEvent(ev)
    assert received  # playhead seek emitted


def test_ruler_drag_does_not_start_box_select(panel_with_curves):
    panel, g, t, b = panel_with_curves
    view = panel._view
    cx = view.width() // 2
    # press in ruler
    view.mousePressEvent(QMouseEvent(
        QMouseEvent.Type.MouseButtonPress, QPointF(cx, 10.0),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))
    assert view._drag_mode == "playhead"
    assert view._box_start is None
