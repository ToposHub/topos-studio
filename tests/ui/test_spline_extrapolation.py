"""Batch 7 tests: extrapolation drawing, viewport/selection preservation on
sync, and SplineKey.to_dict consistency with the authoritative format.
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from PyQt6.QtCore import QPointF

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.ui.spline_model import (
    SplineModel, SplineKey, SplineCurve, INTERP_BEZIER, TANGENT_SMOOTH)
from src.features.composite.ui.spline_view import SplineView
from src.features.composite.ui.spline_panel import SplinePanel
from src.shared.keyframe import Keyframe, KeyframeTrack


# ── SplineKey.to_dict consistency (batch 7a) ─────────────────────────────

def test_to_dict_writes_authoritative_handle_fields():
    k = SplineKey(10, 5.0, INTERP_BEZIER, TANGENT_SMOOTH,
                  handle_left=QPointF(6, 3), handle_right=QPointF(14, 7))
    d = k.to_dict()
    # New authoritative fields present.
    assert "handle_left" in d
    assert "handle_right" in d
    assert d["tangent_mode"] == TANGENT_SMOOTH
    # Deltas are relative to the key point.
    assert d["handle_left"] == [-4, -2]
    assert d["handle_right"] == [4, 2]
    # Legacy fields still emitted for old readers.
    assert "bezier_in" in d
    assert "bezier_out" in d


def test_to_dict_round_trips_through_keyframe_from_dict():
    """The dict SplineKey produces must load back via Keyframe.from_dict."""
    from src.shared.keyframe import Keyframe
    k = SplineKey(20, 0.75, INTERP_BEZIER, "broken",
                  handle_left=QPointF(15, 0.6), handle_right=QPointF(25, 0.9))
    d = k.to_dict()
    kf = Keyframe.from_dict(d)
    assert kf.frame == 20
    assert kf.value == 0.75
    assert kf.tangent_mode == "broken"
    assert tuple(kf.handle_left) == (-5, -0.15000000000000002)  # (15-20, 0.6-0.75)
    assert tuple(kf.handle_right) == (5, 0.15000000000000002)


# ── extrapolation (batch 7c) ────────────────────────────────────────────

@pytest.fixture
def view(qapp):
    g = NodeGraph()
    t = TransformNode()
    tr = KeyframeTrack(param_name="rotation", min_val=0.0, max_val=360.0)
    for f, v in [(20, 0.0), (40, 180.0), (60, 360.0)]:
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


def test_curve_has_default_constant_extrapolation(view):
    view, m = view
    assert m.curves[0].pre_extrap == "constant"
    assert m.curves[0].post_extrap == "constant"


def test_set_selected_extrapolation_changes_mode(view):
    view, m = view
    view._set_selected_extrapolation("post", "linear")
    assert m.curves[0].post_extrap == "linear"
    view._set_selected_extrapolation("pre", "linear")
    assert m.curves[0].pre_extrap == "linear"


def test_set_selected_extrapolation_emits_track_settings(view):
    view, m = view
    received = []
    view.curve_settings_changed.connect(
        lambda nid, pname, pre, post: received.append(
            (nid, pname, pre, post)))

    view._set_selected_extrapolation("post", "linear")

    assert received == [(m.curves[0].node_id, "rotation", "constant", "linear")]


def test_set_selected_extrapolation_ignores_invalid(view):
    view, m = view
    view._set_selected_extrapolation("bogus", "linear")
    view._set_selected_extrapolation("pre", "bogus")
    assert m.curves[0].pre_extrap == "constant"


def test_extrapolation_only_affects_selected_curve(view):
    view, m = view
    # Add a second curve and mark only the first selected.
    if len(m.curves) == 1:
        c2 = SplineCurve(curve_id="n:other", node_id="n", param_name="other",
                         label="other", color="#ff0000",
                         keys=[SplineKey(20, 1.0)])
        m._curves.append(c2)
    for c in m.curves:
        c.selected = False
    m.curves[0].selected = True
    view._set_selected_extrapolation("post", "linear")
    assert m.curves[0].post_extrap == "linear"
    # The unselected curve is untouched.
    assert m.curves[1].post_extrap == "constant"


# ── viewport / selection preservation on sync (batch 7b) ────────────────

def test_set_node_graph_preserves_selected_curve(qapp):
    g = NodeGraph()
    t = TransformNode()
    tr = KeyframeTrack(param_name="rotation", min_val=0.0, max_val=360.0)
    tr.add_keyframe(Keyframe(0, 0.0))
    t.set_keyframe_track("rotation", tr)
    b = TransformNode()  # second node → second curve
    br = KeyframeTrack(param_name="rotation", min_val=0.0, max_val=360.0)
    br.add_keyframe(Keyframe(0, 1.0))
    b.set_keyframe_track("rotation", br)
    g.add_node(t)
    g.add_node(b)
    panel = SplinePanel()
    panel.set_node_graph(g)
    # Select the SECOND curve, then re-sync the graph.
    for c in panel._model.curves:
        c.selected = (c.curve_id == panel._model.curves[1].curve_id)
    panel.set_node_graph(g)
    # The second curve stays selected (not reset to first).
    selected_ids = [c.curve_id for c in panel._model.curves if c.selected]
    assert len(selected_ids) == 1
    assert selected_ids[0] == panel._model.curves[1].curve_id


def test_set_node_graph_preserves_viewport_after_zoom(qapp):
    g = NodeGraph()
    t = TransformNode()
    tr = KeyframeTrack(param_name="rotation", min_val=0.0, max_val=360.0)
    tr.add_keyframe(Keyframe(0, 0.0))
    tr.add_keyframe(Keyframe(100, 1.0))
    t.set_keyframe_track("rotation", tr)
    g.add_node(t)
    panel = SplinePanel()
    panel.set_node_graph(g)
    panel.resize(960, 400)
    # Zoom in to dirty the viewport.
    vp = panel._view._ensure_viewport()
    saved_fmin = vp.frame_min
    vp.frame_min = 10.0
    vp.frame_max = 20.0  # zoomed in range
    # Re-sync.
    panel.set_node_graph(g)
    vp2 = panel._view._viewport
    assert vp2 is not None
    # Viewport was preserved, not reset to fit.
    assert vp2.frame_min == 10.0
    assert vp2.frame_max == 20.0
