"""P11 tests: Spline bezier math — cubic bezier, sampling, tangents, fit.

Covers the mandatory bezier test cases from plan §11.11. Pure-math, no GUI.
"""

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from PyQt6.QtCore import QPointF

from src.features.composite.ui.spline_model import (
    SplineKey, SplineSegment, SplineCurve,
    cubic_bezier_xy, sample_segment, build_segments,
    auto_slope, apply_auto_tangent, apply_smooth_opposite,
    distance_point_to_segment, fit_bounds,
    INTERP_LINEAR, INTERP_BEZIER, INTERP_HOLD, INTERP_EASE_IN_OUT,
    TANGENT_SMOOTH, TANGENT_BROKEN,
)


def _approx_q(p: QPointF, x: float, y: float, tol=1e-6):
    return abs(p.x() - x) < tol and abs(p.y() - y) < tol


# ── cubic bezier endpoints (§11.11) ─────────────────────────────────────

def test_bezier_t0_returns_p0():
    p0, p1, p2, p3 = QPointF(0, 0), QPointF(1, 2), QPointF(3, 2), QPointF(4, 0)
    assert _approx_q(cubic_bezier_xy(p0, p1, p2, p3, 0.0), 0.0, 0.0)


def test_bezier_t1_returns_p3():
    p0, p1, p2, p3 = QPointF(0, 0), QPointF(1, 2), QPointF(3, 2), QPointF(4, 0)
    assert _approx_q(cubic_bezier_xy(p0, p1, p2, p3, 1.0), 4.0, 0.0)


def test_bezier_colinear_points_stay_on_line():
    """All four control points on a line → samples are on that line."""
    p0, p1, p2, p3 = QPointF(0, 0), QPointF(2, 2), QPointF(4, 4), QPointF(6, 6)
    for t in (0.0, 0.25, 0.5, 0.75, 1.0):
        pt = cubic_bezier_xy(p0, p1, p2, p3, t)
        assert abs(pt.x() - pt.y()) < 1e-6


def test_bezier_midpoint():
    p0, p1, p2, p3 = QPointF(0, 0), QPointF(0, 0), QPointF(4, 4), QPointF(4, 4)
    pt = cubic_bezier_xy(p0, p1, p2, p3, 0.5)
    # symmetric S-curve, midpoint should be at (2, 2)
    assert _approx_q(pt, 2.0, 2.0)


# ── hold / linear segment sampling (§11.11) ─────────────────────────────

def test_hold_segment_is_step_line():
    """hold segment: horizontal then vertical jump, no cubicTo."""
    seg = SplineSegment(
        p0=QPointF(0, 1.0), p1=QPointF(0, 1.0), p2=QPointF(10, 5.0),
        p3=QPointF(10, 5.0), interpolation=INTERP_HOLD)
    pts = sample_segment(seg, 5)
    # All interior samples hold p0's value; last point jumps to p3.
    for p in pts[:-1]:
        assert abs(p.y() - 1.0) < 1e-6
    assert abs(pts[-1].y() - 5.0) < 1e-6


def test_linear_segment_is_straight_line():
    seg = SplineSegment(
        p0=QPointF(0, 0.0), p1=QPointF(0, 0.0), p2=QPointF(10, 10.0),
        p3=QPointF(10, 10.0), interpolation=INTERP_LINEAR)
    pts = sample_segment(seg, 11)
    for p in pts:
        assert abs(p.x() - p.y()) < 1e-6  # on y=x line


# ── handle x clamping (§11.11) ──────────────────────────────────────────

def test_handle_x_dragged_outside_segment_is_clamped():
    """A handle whose x is outside [k0.frame, k1.frame] gets clamped back."""
    curve = SplineCurve(curve_id="n:p", node_id="n", param_name="p",
                        label="p", color="#fff",
                        keys=[
                            SplineKey(0, 0.0, INTERP_BEZIER,
                                      handle_left=QPointF(-5, 0),
                                      handle_right=QPointF(50, 5)),
                            SplineKey(10, 10.0, INTERP_BEZIER,
                                      handle_left=QPointF(-30, 5),
                                      handle_right=QPointF(15, 10)),
                        ])
    segs = build_segments(curve)
    assert len(segs) == 1
    s = segs[0]
    # p1.x must be within [0, 10]
    assert 0.0 <= s.p1.x() <= 10.0
    # p2.x must be within [0, 10]
    assert 0.0 <= s.p2.x() <= 10.0


# ── auto tangent at local extremum (§11.11) ─────────────────────────────

def test_auto_tangent_flat_at_local_extremum():
    """A key that is a local max/min gets slope 0 (flat tangent)."""
    prev_k = SplineKey(0, 0.0)
    k = SplineKey(5, 10.0)   # local max
    next_k = SplineKey(10, 0.0)
    assert auto_slope(prev_k, k, next_k) == 0.0


def test_auto_tangent_nonzero_on_monotonic():
    prev_k = SplineKey(0, 0.0)
    k = SplineKey(5, 5.0)
    next_k = SplineKey(10, 10.0)
    # slope = (10 - 0) / (10 - 0) = 1.0
    assert abs(auto_slope(prev_k, k, next_k) - 1.0) < 1e-6


def test_apply_auto_tangent_sets_handles():
    prev_k = SplineKey(0, 0.0)
    k = SplineKey(6, 3.0)
    next_k = SplineKey(12, 6.0)
    apply_auto_tangent(prev_k, k, next_k)
    # left_dx = (6-0)/3 = 2; slope=0.5 → handle_left.x = 6-2 = 4, y = 3-1 = 2
    assert _approx_q(k.handle_left, 4.0, 2.0)
    assert _approx_q(k.handle_right, 8.0, 4.0)


# ── smooth opposite handle (§11.8) ──────────────────────────────────────

def test_smooth_drag_left_updates_right_collinear():
    """Dragging the left handle in smooth mode mirrors the right."""
    k = SplineKey(10, 5.0, INTERP_BEZIER, TANGENT_SMOOTH,
                  handle_left=QPointF(6, 4), handle_right=QPointF(14, 6))
    # Drag left to (5, 3)
    k.handle_left = QPointF(5, 3)
    apply_smooth_opposite(k, "left")
    # right should now point opposite to (5,3)-(10,5) = (-5,-2)
    kp = QPointF(10, 5)
    drag = QPointF(5 - 10, 3 - 5)   # (-5, -2)
    opp = k.handle_right - kp
    # opp should be roughly in direction (5, 2)
    assert opp.x() > 0 and opp.y() > 0


# ── distance to segment (§11.8) ─────────────────────────────────────────

def test_distance_point_to_segment_endpoint():
    a = QPointF(0, 0)
    b = QPointF(10, 0)
    assert distance_point_to_segment(QPointF(0, 0), a, b) == 0.0
    assert distance_point_to_segment(QPointF(-2, 0), a, b) == 2.0


def test_distance_point_to_segment_perpendicular():
    a = QPointF(0, 0)
    b = QPointF(10, 0)
    assert abs(distance_point_to_segment(QPointF(5, 3), a, b) - 3.0) < 1e-6


# ── fit bounds (§11.9 / §11.11) ─────────────────────────────────────────

def test_fit_bounds_includes_key_points():
    curve = SplineCurve(curve_id="n:p", node_id="n", param_name="p",
                        label="p", color="#fff",
                        keys=[SplineKey(0, 0.0), SplineKey(10, 10.0)])
    bounds = fit_bounds([curve])
    assert bounds is not None
    fmin, fmax, vmin, vmax = bounds
    assert fmin <= 0 and fmax >= 10
    assert vmin <= 0 and vmax >= 10


def test_fit_bounds_includes_overshoot_samples():
    """A bezier that overshoots must be covered by fit bounds (§11.9)."""
    curve = SplineCurve(curve_id="n:p", node_id="n", param_name="p",
                        label="p", color="#fff",
                        keys=[
                            SplineKey(0, 0.0, INTERP_BEZIER,
                                      handle_right=QPointF(3, 20.0)),
                            SplineKey(10, 0.0, INTERP_BEZIER,
                                      handle_left=QPointF(7, 20.0)),
                        ])
    bounds = fit_bounds([curve], sample_count=64)
    assert bounds is not None
    _, _, vmin, vmax = bounds
    # The overshoot handle pulls the curve above value 0.
    assert vmax > 5.0


def test_fit_bounds_none_for_empty():
    assert fit_bounds([]) is None
