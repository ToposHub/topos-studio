"""P11 tests: SplineModel — keyframe_tracks → SplineCurve conversion.

Covers color sync, visible/selected, build_segments wiring, and the
fit-bounds integration with the model (plan §11.2/§11.11).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.nodes.blur_node import BlurNode
from src.features.composite.ui.spline_model import (
    SplineModel, SplineCurve, build_segments, fit_bounds, INTERP_LINEAR)
from src.shared.keyframe import Keyframe, KeyframeTrack


pytestmark = pytest.mark.composite_core


def _graph_with_two_animated():
    g = NodeGraph()
    t = TransformNode()
    tr = KeyframeTrack(param_name="rotation", min_val=-360.0, max_val=360.0)
    tr.add_keyframe(Keyframe(0, 0.0))
    tr.add_keyframe(Keyframe(10, 45.0))
    t.set_keyframe_track("rotation", tr)
    b = BlurNode()
    br = KeyframeTrack(param_name="radius", min_val=0.0, max_val=100.0)
    br.add_keyframe(Keyframe(0, 1.0))
    br.add_keyframe(Keyframe(20, 8.0))
    b.set_keyframe_track("radius", br)
    g.add_node(t)
    g.add_node(b)
    return g, t, b


# ── conversion ──────────────────────────────────────────────────────────

def test_model_builds_one_curve_per_animated_param():
    g, t, b = _graph_with_two_animated()
    m = SplineModel()
    m.rebuild(g)
    ids = {c.curve_id for c in m.curves}
    assert f"{t.id}:rotation" in ids
    assert f"{b.id}:radius" in ids


def test_curve_keys_carry_frame_and_value():
    g, t, b = _graph_with_two_animated()
    m = SplineModel()
    m.rebuild(g)
    c = m.curve_by_id(f"{t.id}:rotation")
    assert c is not None
    assert len(c.keys) == 2
    assert c.keys[0].frame == 0
    assert c.keys[1].frame == 10
    assert abs(c.keys[1].value - 45.0) < 1e-6


def test_curve_segments_built_from_keys():
    g, t, b = _graph_with_two_animated()
    m = SplineModel()
    m.rebuild(g)
    c = m.curve_by_id(f"{t.id}:rotation")
    segs = build_segments(c)
    assert len(segs) == 1  # two keys → one segment


# ── color sync (§11.10) ─────────────────────────────────────────────────

def test_curve_color_follows_node_color():
    g, t, b = _graph_with_two_animated()
    t.custom_header_color = "#abcdef"
    m = SplineModel()
    m.rebuild(g)
    c = m.curve_by_id(f"{t.id}:rotation")
    assert c.color == "#abcdef"


def test_color_change_reflected_after_rebuild():
    g, t, b = _graph_with_two_animated()
    m = SplineModel()
    m.rebuild(g)
    t.custom_header_color = "#111111"
    m.rebuild(g)
    c = m.curve_by_id(f"{t.id}:rotation")
    assert c.color == "#111111"


# ── visible / selected ──────────────────────────────────────────────────

def test_curve_visible_by_default():
    g, t, b = _graph_with_two_animated()
    m = SplineModel()
    m.rebuild(g)
    assert all(c.visible for c in m.curves)


def test_toggle_visible_filters_visible_curves():
    g, t, b = _graph_with_two_animated()
    m = SplineModel()
    m.rebuild(g)
    m.set_visible(f"{t.id}:rotation", False)
    vis = m.visible_curves()
    assert all(c.curve_id != f"{t.id}:rotation" for c in vis)


# ── fit integration ─────────────────────────────────────────────────────

def test_model_curves_fit_bounds():
    g, t, b = _graph_with_two_animated()
    m = SplineModel()
    m.rebuild(g)
    bounds = fit_bounds(m.visible_curves())
    assert bounds is not None
    fmin, fmax, vmin, vmax = bounds
    assert fmin <= 0
    assert fmax >= 20  # blur radius reaches frame 20


# ── min/max carried through ─────────────────────────────────────────────

def test_curve_carries_param_bounds():
    g, t, b = _graph_with_two_animated()
    m = SplineModel()
    m.rebuild(g)
    c = m.curve_by_id(f"{t.id}:rotation")
    assert c.min_value == -360.0
    assert c.max_value == 360.0
