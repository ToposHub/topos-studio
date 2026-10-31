"""Graph Editor transform-box + speed-drag math tests (T115/T117).

Pure-data tests — no QWidget. Locks the transform-box bounding-box,
move/scale transforms, anchor computation, and the speed-graph drag
predicate before the view consumes them (headless-testable contract).

T115 — Speed Graph single-point drag: in a speed projection the Y axis is
derived (slope), so dragging a keyframe must move only its time and never
touch its value. ``is_speed_drag`` is the predicate the view consults.

T117 — Transform Box: with ≥2 selected keyframes spanning ≥2 distinct
frames the box hugs the selection (frame min/max, value min/max); internal
drag translates, edge/corner drag scales around an anchor, and cross-
parameter scaling is forbidden (only time moves across params).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.ui.graph_model import (
    GraphPoint, GraphSeries,
    apply_move_transform, apply_scale_transform,
    is_speed_drag, selection_bbox, transform_box_anchor,
)


pytestmark = pytest.mark.composite_core


# ── helpers ───────────────────────────────────────────────────────────


def _point(frame, value, selected=True, row_id="param:n:rotation",
           param="rotation"):
    return GraphPoint(row_id=row_id, node_id="n", parameter_name=param,
                      channel=None, frame=float(frame), value=float(value),
                      selected=selected)


def _series(points, row_id="param:n:rotation", display="Rotation",
            graph_type="value"):
    return GraphSeries(row_id=row_id, display_name=display, channel=None,
                       color="#ff8a3d", is_spatial=False, has_keyframes=True,
                       has_expression=False, graph_type=graph_type,
                       points=list(points))


# ── T115: is_speed_drag ───────────────────────────────────────────────


class TestIsSpeedDrag:
    def test_value_series_is_not_speed(self):
        s = _series([_point(0, 0.0)], graph_type="value")
        assert is_speed_drag(s) is False

    def test_speed_series_is_speed(self):
        s = _series([_point(0, 0.0)], graph_type="speed")
        assert is_speed_drag(s) is True

    def test_list_mixed_reports_speed(self):
        a = _series([_point(0, 0.0)], graph_type="value")
        b = _series([_point(0, 0.0)], graph_type="speed")
        assert is_speed_drag([a, b]) is True

    def test_list_all_value_reports_not_speed(self):
        a = _series([_point(0, 0.0)], graph_type="value")
        b = _series([_point(1, 1.0)], graph_type="value")
        assert is_speed_drag([a, b]) is False

    def test_auto_resolves_to_value(self):
        # 'auto' is resolved per-series by the view; the raw 'auto' string
        # is treated as value here (the view never calls is_speed_drag on
        # an unresolved 'auto' series).
        s = _series([_point(0, 0.0)], graph_type="auto")
        assert is_speed_drag(s) is False

    def test_object_without_graph_type_defaults_value(self):
        # Defensive: a bare dict (not a real series) shouldn't crash.
        assert is_speed_drag({"graph_type": "value"}) is False
        assert is_speed_drag({}) is False


# ── T117: selection_bbox ──────────────────────────────────────────────


class TestSelectionBbox:
    def test_no_selection_returns_none(self):
        s = _series([_point(0, 0.0, selected=False),
                     _point(10, 10.0, selected=False)])
        assert selection_bbox([s]) is None

    def test_single_point_returns_none(self):
        # One point can't form a usable box (zero time span).
        s = _series([_point(5, 5.0)])
        assert selection_bbox([s]) is None

    def test_two_points_same_frame_returns_none(self):
        # Degenerate: same frame, different value → no time span.
        s = _series([_point(5, 0.0), _point(5, 10.0)])
        assert selection_bbox([s]) is None

    def test_two_points_distinct_frames_returns_bbox(self):
        s = _series([_point(10, 100.0), _point(20, 200.0)])
        bbox = selection_bbox([s])
        assert bbox == (10.0, 20.0, 100.0, 200.0)

    def test_bbox_uses_only_selected_points(self):
        s = _series([_point(0, 0.0, selected=False),
                     _point(10, 100.0),
                     _point(20, 200.0),
                     _point(30, 300.0, selected=False)])
        bbox = selection_bbox([s])
        assert bbox == (10.0, 20.0, 100.0, 200.0)

    def test_bbox_across_multiple_series(self):
        a = _series([_point(0, 5.0), _point(10, 50.0)],
                    row_id="param:n:rotation")
        b = _series([_point(5, -5.0), _point(15, 80.0)],
                    row_id="param:n:scale")
        bbox = selection_bbox([a, b])
        assert bbox == (0.0, 15.0, -5.0, 80.0)

    def test_bbox_value_order_independent_of_frame(self):
        # value can decrease as frame increases; min/max still correct.
        s = _series([_point(0, 100.0), _point(10, 0.0)])
        bbox = selection_bbox([s])
        assert bbox == (0.0, 10.0, 0.0, 100.0)


# ── T117: transform_box_anchor ────────────────────────────────────────


class TestTransformBoxAnchor:
    def test_anchor_is_box_centre(self):
        bbox = (10.0, 20.0, 100.0, 200.0)
        assert transform_box_anchor(bbox) == (15.0, 150.0)

    def test_anchor_negative_values(self):
        bbox = (-10.0, 10.0, -50.0, 50.0)
        assert transform_box_anchor(bbox) == (0.0, 0.0)

    def test_anchor_float_precision(self):
        bbox = (0.0, 3.0, 0.0, 3.0)
        af, av = transform_box_anchor(bbox)
        assert af == pytest.approx(1.5)
        assert av == pytest.approx(1.5)


# ── T117: apply_move_transform ────────────────────────────────────────


class TestApplyMoveTransform:
    def test_move_shifts_both_axes(self):
        pts = [_point(10, 100.0), _point(20, 200.0)]
        out = apply_move_transform(pts, delta_frame=5, delta_value=10.0)
        assert out[0][1] == 15.0  # new frame
        assert out[0][2] == 110.0  # new value
        assert out[1][1] == 25.0
        assert out[1][2] == 210.0

    def test_move_does_not_mutate_input(self):
        pts = [_point(10, 100.0)]
        apply_move_transform(pts, delta_frame=5, delta_value=10.0)
        assert pts[0].frame == 10.0
        assert pts[0].value == 100.0

    def test_move_negative_delta(self):
        pts = [_point(10, 100.0)]
        out = apply_move_transform(pts, delta_frame=-5, delta_value=-20.0)
        assert out[0][1] == 5.0
        assert out[0][2] == 80.0

    def test_move_clamps_frame_to_zero(self):
        pts = [_point(3, 100.0)]
        out = apply_move_transform(pts, delta_frame=-10, delta_value=0.0)
        assert out[0][1] == 0.0  # never negative
        assert out[0][2] == 100.0  # value untouched

    def test_move_value_frozen_when_disallowed(self):
        # Cross-parameter or speed-graph move: value component dropped.
        pts = [_point(10, 100.0)]
        out = apply_move_transform(pts, delta_frame=5, delta_value=50.0,
                                   allow_value=False)
        assert out[0][1] == 15.0
        assert out[0][2] == 100.0  # value unchanged

    def test_move_zero_delta_is_identity(self):
        pts = [_point(10, 100.0), _point(20, 200.0)]
        out = apply_move_transform(pts, delta_frame=0, delta_value=0.0)
        assert out[0][1] == 10.0
        assert out[0][2] == 100.0


# ── T117: apply_scale_transform ───────────────────────────────────────


class TestApplyScaleTransform:
    def test_identity_scale(self):
        pts = [_point(10, 100.0), _point(20, 200.0)]
        bbox = (10.0, 20.0, 100.0, 200.0)
        anchor = transform_box_anchor(bbox)
        out = apply_scale_transform(pts, bbox, anchor, 1.0, 1.0)
        assert out[0][1] == 10.0
        assert out[0][2] == 100.0
        assert out[1][1] == 20.0
        assert out[1][2] == 200.0

    def test_scale_time_compresses_around_anchor(self):
        pts = [_point(10, 0.0), _point(20, 0.0)]
        bbox = (10.0, 20.0, 0.0, 1.0)
        anchor = transform_box_anchor(bbox)  # (15, 0.5)
        out = apply_scale_transform(pts, bbox, anchor, scale_time=0.5,
                                    scale_value=1.0)
        # (10-15)*0.5 + 15 = -2.5 + 15 = 12.5 → 12 (rounded, clamped≥0)
        assert out[0][1] == 12.0
        # (20-15)*0.5 + 15 = 2.5 + 15 = 17.5 → 18
        assert out[1][1] == 18.0

    def test_scale_value_stretches_around_anchor(self):
        pts = [_point(0, 100.0), _point(0, 200.0)]
        # Same-frame points are allowed through scale (bbox guard is on
        # the caller; here we pass a synthetic spanned bbox).
        bbox = (0.0, 10.0, 100.0, 200.0)
        anchor = transform_box_anchor(bbox)  # (5, 150)
        out = apply_scale_transform(pts, bbox, anchor, scale_time=1.0,
                                    scale_value=2.0)
        # (100-150)*2 + 150 = -100 + 150 = 50
        assert out[0][2] == 50.0
        # (200-150)*2 + 150 = 100 + 150 = 250
        assert out[1][2] == 250.0

    def test_scale_value_frozen_when_disallowed(self):
        # Cross-parameter scaling forbidden — only time scales.
        pts = [_point(10, 100.0), _point(20, 200.0)]
        bbox = (10.0, 20.0, 100.0, 200.0)
        anchor = transform_box_anchor(bbox)
        out = apply_scale_transform(pts, bbox, anchor, scale_time=0.5,
                                    scale_value=2.0, allow_value=False)
        # Frames still scaled.
        assert out[0][1] == 12.0  # (10-15)*0.5+15 = 12.5 → 12
        assert out[1][1] == 18.0  # (20-15)*0.5+15 = 17.5 → 18
        # Values frozen at original.
        assert out[0][2] == 100.0
        assert out[1][2] == 200.0

    def test_scale_anchor_at_edge(self):
        # Anchor at the left edge: scaling time shouldn't move frame 10.
        pts = [_point(10, 0.0), _point(20, 0.0)]
        bbox = (10.0, 20.0, 0.0, 1.0)
        anchor = (10.0, 0.0)  # left edge, not centre
        out = apply_scale_transform(pts, bbox, anchor, scale_time=0.5,
                                    scale_value=1.0)
        assert out[0][1] == 10.0  # anchor frame unchanged
        # (20-10)*0.5 + 10 = 15
        assert out[1][1] == 15.0

    def test_scale_clamps_frame_to_zero(self):
        pts = [_point(2, 0.0), _point(10, 0.0)]
        bbox = (2.0, 10.0, 0.0, 1.0)
        anchor = transform_box_anchor(bbox)  # (6, 0.5)
        # scale_time=0.1 → (2-6)*0.1+6 = -0.4+6 = 5.6 → 6; (10-6)*0.1+6=6.4→6
        out = apply_scale_transform(pts, bbox, anchor, scale_time=0.1,
                                    scale_value=1.0)
        assert all(f >= 0.0 for _, f, _ in out)

    def test_scale_does_not_mutate_input(self):
        pts = [_point(10, 100.0)]
        bbox = (10.0, 20.0, 100.0, 200.0)
        anchor = transform_box_anchor(bbox)
        apply_scale_transform(pts, bbox, anchor, 2.0, 2.0)
        assert pts[0].frame == 10.0
        assert pts[0].value == 100.0

    def test_scale_expands_time(self):
        pts = [_point(10, 0.0), _point(20, 0.0)]
        bbox = (10.0, 20.0, 0.0, 1.0)
        anchor = transform_box_anchor(bbox)  # (15, 0.5)
        out = apply_scale_transform(pts, bbox, anchor, scale_time=2.0,
                                    scale_value=1.0)
        # (10-15)*2 + 15 = -10 + 15 = 5
        assert out[0][1] == 5.0
        # (20-15)*2 + 15 = 10 + 15 = 25
        assert out[1][1] == 25.0
