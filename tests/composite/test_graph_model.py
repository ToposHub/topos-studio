"""Graph Editor model tests (T102/T103/T104).

Pure-data tests — no QWidget. Locks the GraphPoint / GraphSeries /
GraphViewport dataclass shapes, the property→series builder (T103), and
the auto-graph-type decision (T104).

These are the foundation tests the Graph Editor view (T105+) and the
graph-canvas host (T101) build on. Keeping them GUI-free means the data
contract is locked even before any painting exists.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.nodes.constant_node import ConstantNode
from src.features.composite.ui.graph_model import (
    GraphOptions, GraphPoint, GraphSeries, GraphViewport,
    auto_graph_type, build_graph_series, compute_speeds,
    compute_value_bounds, frame_to_x, is_spatial_param,
    sample_speed_curve, sample_value_curve, summarize_selection,
    value_to_y, x_to_frame, y_to_value,
)
from src.shared.keyframe import Keyframe, KeyframeTrack


pytestmark = pytest.mark.composite_core


# ── T102: dataclass shapes ────────────────────────────────────────────


class TestDataclassShapes:
    def test_graph_point_defaults(self):
        p = GraphPoint(
            row_id="param:n:rotation", node_id="n",
            parameter_name="rotation", channel=None, frame=10, value=5.0)
        assert p.speed is None
        assert p.keyframe_index is None
        assert p.selected is False
        assert p.interpolation_in == "linear"
        assert p.interpolation_out == "linear"
        assert p.handle_left is None
        assert p.handle_right is None
        assert p.tangent_mode == "smooth"

    def test_graph_series_defaults(self):
        s = GraphSeries(row_id="param:n:rotation", display_name="Rotation",
                        channel=None, color="#fff", is_spatial=False,
                        has_keyframes=True, has_expression=False)
        assert s.points == []
        assert s.samples == []
        assert s.graph_type == "value"

    def test_graph_viewport_defaults(self):
        v = GraphViewport()
        assert v.auto_zoom_time is True
        assert v.auto_zoom_value is True
        assert v.graph_type == "auto"

    def test_graph_options_defaults_all_off(self):
        o = GraphOptions()
        # Transform box, reference graph, expression editor default off.
        assert o.show_transform_box is False
        assert o.show_reference_graph is False
        assert o.show_expression_editor is False
        assert o.show_post_expression_graph is False
        # Layer in/out + markers default on (plan §22.3 overlays).
        assert o.show_layer_in_out is True
        assert o.show_markers is True


# ── T104: auto graph type ─────────────────────────────────────────────


class TestAutoGraphType:
    @pytest.mark.parametrize("name,expected", [
        ("translate_x", "speed"),
        ("translate_y", "speed"),
        ("anchor_x", "speed"),
        ("pivot", "speed"),
        ("point_of_interest", "speed"),
        ("Position", "speed"),       # case-insensitive
        ("rotation", "value"),
        ("opacity", "value"),
        ("blur_radius", "value"),
        ("hue", "value"),
        ("intensity", "value"),
        ("", "value"),               # empty → value (safe default)
        ("unknown_param", "value"),
    ])
    def test_auto_graph_type(self, name, expected):
        assert auto_graph_type(name) == expected

    def test_is_spatial_param_case_insensitive(self):
        assert is_spatial_param("TRANSLATE_X") is True
        assert is_spatial_param("Rotation") is False

    def test_explicit_graph_type_overrides_auto(self):
        """build_graph_series with graph_type='speed' forces speed even for
        a scalar param; 'value' forces value even for a spatial param."""
        node = TransformNode()
        track = KeyframeTrack(param_name="rotation")
        track.add_keyframe(Keyframe(0, 0.0))
        node.set_keyframe_track("rotation", track)
        s_speed = build_graph_series(node, "rotation", graph_type="speed")
        s_value = build_graph_series(node, "rotation", graph_type="value")
        assert s_speed.graph_type == "speed"
        assert s_value.graph_type == "value"
        # is_spatial still reflects the param, not the graph type.
        assert s_speed.is_spatial is False


# ── T103: build_graph_series ──────────────────────────────────────────


def _node_with_track(param="rotation",
                     kfs=((0, 0.0), (25, 90.0), (50, 180.0))):
    node = TransformNode()
    track = KeyframeTrack(param_name=param,
                          min_val=-360.0, max_val=360.0)
    for f, v in kfs:
        track.add_keyframe(Keyframe(f, v))
    node.set_keyframe_track(param, track)
    return node


class TestBuildGraphSeries:
    def test_builds_one_point_per_keyframe(self):
        node = _node_with_track()
        s = build_graph_series(node, "rotation")
        assert len(s.points) == 3
        assert s.has_keyframes is True
        assert s.has_expression is False

    def test_point_frames_and_values_match_track(self):
        node = _node_with_track()
        s = build_graph_series(node, "rotation")
        assert [p.frame for p in s.points] == [0.0, 25.0, 50.0]
        assert [p.value for p in s.points] == [0.0, 90.0, 180.0]

    def test_point_carries_node_id_and_param(self):
        node = _node_with_track()
        s = build_graph_series(node, "rotation")
        for p in s.points:
            assert p.node_id == node.id
            assert p.parameter_name == "rotation"
            assert p.row_id == f"param:{node.id}:rotation"

    def test_point_preserves_interpolation(self):
        node = _node_with_track()
        # Mutate the track's first keyframe to bezier via the node API.
        track = node.keyframe_tracks["rotation"]
        track["keyframes"][0]["interpolation"] = "bezier"
        s = build_graph_series(node, "rotation")
        assert s.points[0].interpolation_in == "bezier"
        assert s.points[0].interpolation_out == "bezier"

    def test_point_preserves_bezier_handles(self):
        """handle_left/handle_right (or bezier_in/out aliases) flow through
        so the graph editor can draw the same handles as the Spline."""
        node = _node_with_track()
        track = node.keyframe_tracks["rotation"]
        track["keyframes"][0]["bezier_in"] = [-2.0, -5.0]
        track["keyframes"][0]["bezier_out"] = [3.0, 10.0]
        s = build_graph_series(node, "rotation")
        assert s.points[0].handle_left == (-2.0, -5.0)
        assert s.points[0].handle_right == (3.0, 10.0)

    def test_empty_track_produces_no_points(self):
        """A param with no keyframes → empty points list, has_keyframes=False."""
        node = TransformNode()  # no track set
        s = build_graph_series(node, "rotation")
        assert s.points == []
        assert s.has_keyframes is False

    def test_expression_detected(self):
        node = _node_with_track()
        node._expressions = {"rotation": "value + 10"}
        s = build_graph_series(node, "rotation")
        assert s.has_expression is True

    def test_no_expression_when_param_absent(self):
        node = _node_with_track()
        node._expressions = {"other_param": "x"}
        s = build_graph_series(node, "rotation")
        assert s.has_expression is False

    def test_display_name_humanized(self):
        """The param name is title-cased with underscores → spaces for the
        display name (matches the Keyframes tree label convention)."""
        node = _node_with_track(param="blur_radius")
        s = build_graph_series(node, "blur_radius")
        assert s.display_name == "Blur Radius"

    def test_color_inherited_from_node(self):
        node = _node_with_track()
        s = build_graph_series(node, "rotation", node_color="#ff0000")
        assert s.color == "#ff0000"

    def test_auto_graph_type_resolved_in_builder(self):
        """build_graph_series with graph_type='auto' (the default) resolves
        to speed/value per the param name."""
        node_t = _node_with_track(param="translate_x")
        node_r = _node_with_track(param="rotation")
        st = build_graph_series(node_t, "translate_x")
        sr = build_graph_series(node_r, "rotation")
        assert st.graph_type == "speed"
        assert sr.graph_type == "value"

    def test_keyframe_index_assigned_in_order(self):
        node = _node_with_track()
        s = build_graph_series(node, "rotation")
        assert [p.keyframe_index for p in s.points] == [0, 1, 2]


# ── T105: value-graph coordinate transforms ───────────────────────────


class TestValueGraphTransforms:
    @pytest.fixture
    def viewport(self):
        return GraphViewport(
            frame_start=0.0, frame_end=100.0,
            value_min=0.0, value_max=100.0,
            pixels_per_frame=10.0, pixels_per_value=2.0,
            graph_type="value",
        )

    def test_value_to_y_inverts_axis(self, viewport):
        """Higher value → smaller y (screen up). At value_min, y == bottom."""
        assert value_to_y(0.0, viewport, graph_bottom=200) == 200
        assert value_to_y(100.0, viewport, graph_bottom=200) == 0
        # 50 → 200 - 50*2 = 100
        assert value_to_y(50.0, viewport, graph_bottom=200) == 100

    def test_y_to_value_round_trips(self, viewport):
        for v in (0.0, 25.0, 50.0, 75.0, 100.0):
            y = value_to_y(v, viewport, graph_bottom=200)
            assert y_to_value(y, viewport, graph_bottom=200) == pytest.approx(v)

    def test_frame_to_x(self, viewport):
        assert frame_to_x(0, viewport, graph_left=50) == 50
        assert frame_to_x(10, viewport, graph_left=50) == 150
        assert frame_to_x(100, viewport, graph_left=50) == 1050

    def test_x_to_frame_round_trips(self, viewport):
        for f in (0, 10, 50, 100):
            x = frame_to_x(f, viewport, graph_left=50)
            assert x_to_frame(x, viewport, graph_left=50) == pytest.approx(f)

    def test_zero_pixels_per_value_does_not_divide_by_zero(self, viewport):
        """Defensive: a zero ppv (bad viewport) must not crash the transform."""
        viewport.pixels_per_value = 0.0
        # Should return a finite value (clamped to 1e-9 inside).
        y = value_to_y(50.0, viewport, graph_bottom=200)
        assert isinstance(y, float)


# ── T105 helper: value bounds ─────────────────────────────────────────


class TestComputeValueBounds:
    def test_empty_returns_default(self):
        lo, hi = compute_value_bounds([], [])
        assert (lo, hi) == (0.0, 1.0)

    def test_single_value_gets_visible_band(self):
        """One distinct value shouldn't produce a zero-height graph — give
        it a small band so the curve is visible."""
        pts = [GraphPoint(row_id="r", node_id="n",
                          parameter_name="p", channel=None,
                          frame=0, value=5.0)]
        lo, hi = compute_value_bounds([], pts)
        assert lo < 5.0 < hi
        assert hi - lo > 0

    def test_padding_is_10_percent(self):
        """Plan §22.4: 8-12 % padding. We use 10 %."""
        pts = [GraphPoint(row_id="r", node_id="n", parameter_name="p",
                          channel=None, frame=f, value=float(f))
               for f in (0, 100)]
        lo, hi = compute_value_bounds([], pts)
        # range is 100, pad is 10 each side.
        assert lo == pytest.approx(-10.0)
        assert hi == pytest.approx(110.0)

    def test_uses_both_samples_and_points(self):
        """Bounds consider densely-sampled curve values too, not just
        keyframe points — a bezier overshoot must extend the bounds."""
        samples = [(0, 0.0), (50, 200.0), (100, 0.0)]  # overshoots to 200
        pts = [GraphPoint(row_id="r", node_id="n", parameter_name="p",
                          channel=None, frame=0, value=0.0),
               GraphPoint(row_id="r", node_id="n", parameter_name="p",
                          channel=None, frame=100, value=0.0)]
        lo, hi = compute_value_bounds(samples, pts)
        assert hi > 200.0  # the 200 sample + padding


# ── T107: value-graph sampling ────────────────────────────────────────


def _pts(frame_values, interp="linear"):
    return [GraphPoint(row_id="r", node_id="n", parameter_name="p",
                       channel=None, frame=float(f), value=float(v),
                       interpolation_in=interp, interpolation_out=interp)
            for f, v in frame_values]


class TestSampleValueCurve:
    def test_empty_when_fewer_than_two_points(self):
        assert sample_value_curve([], 0, 100, 10.0) == []
        assert sample_value_curve(_pts([(0, 0.0)]), 0, 100, 10.0) == []

    def test_linear_interpolation_is_straight_line(self):
        pts = _pts([(0, 0.0), (100, 100.0)])
        samples = sample_value_curve(pts, 0, 100, pixels_per_frame=10.0)
        # First sample is the keyframe, last is the keyframe.
        assert samples[0] == (0.0, 0.0)
        assert samples[-1] == (100.0, 100.0)
        # Mid-sample near frame 50 should be near value 50.
        mid = [v for f, v in samples if 49 <= f <= 51]
        assert any(abs(v - 50.0) < 5.0 for v in mid)

    def test_hold_is_flat_then_jump(self):
        """Hold segment: speed 0 across [k0, k1), then vertical jump at k1.
        Plan §22.4: 'Hold 区间速度为 0，并且曲线不能画出虚假的斜坡'."""
        pts = _pts([(0, 10.0), (50, 90.0)], interp="hold")
        samples = sample_value_curve(pts, 0, 50, pixels_per_frame=2.0)
        # All samples before frame 50 hold value 10.
        flat = [v for f, v in samples if f < 50]
        assert all(abs(v - 10.0) < 1e-6 for v in flat)
        # The jump to 90 appears at frame 50.
        assert (50.0, 10.0) in samples  # just before the jump
        assert (50.0, 90.0) in samples  # the jump itself

    def test_bezier_uses_handles(self):
        """Bezier segment with explicit handles flows through the control
        points (not a straight line)."""
        pts = [
            GraphPoint(row_id="r", node_id="n", parameter_name="p",
                       channel=None, frame=0, value=0.0,
                       interpolation_in="bezier", interpolation_out="bezier",
                       handle_right=(10.0, 100.0)),
            GraphPoint(row_id="r", node_id="n", parameter_name="p",
                       channel=None, frame=100, value=0.0,
                       interpolation_in="bezier", interpolation_out="bezier",
                       handle_left=(-10.0, -100.0)),
        ]
        samples = sample_value_curve(pts, 0, 100, pixels_per_frame=2.0)
        # The middle of a bezier with up-then-down handles should overshoot
        # above both endpoints (the handles pull it up).
        mid_values = [v for f, v in samples if 40 <= f <= 60]
        assert max(mid_values) > 0.0  # overshoots above the 0 endpoints

    def test_samples_clipped_to_visible_range(self):
        """Samples outside [frame_start, frame_end] are dropped."""
        pts = _pts([(0, 0.0), (100, 100.0)])
        samples = sample_value_curve(pts, 20, 80, pixels_per_frame=10.0)
        for f, _v in samples:
            assert 20 <= f <= 80

    def test_sample_density_scales_with_zoom(self):
        """Higher pixels_per_frame (zoomed in) → smaller step_frames → more
        samples. Lower ppf (zoomed out) → larger step → fewer samples.
        This keeps the curve smooth without oversampling at high zoom."""
        pts = _pts([(0, 0.0), (100, 100.0)])
        zoomed_in = sample_value_curve(pts, 0, 100, pixels_per_frame=20.0)
        zoomed_out = sample_value_curve(pts, 0, 100, pixels_per_frame=2.0)
        # Zoomed in → denser sampling (more samples over the same frame span).
        assert len(zoomed_in) > len(zoomed_out)
        # Both produce a non-trivial curve (not empty, not one sample).
        assert len(zoomed_out) > 1


# ── T106: speed computation ───────────────────────────────────────────


class TestComputeSpeeds:
    def test_empty_returns_empty(self):
        assert compute_speeds([]) == []

    def test_linear_segment_constant_speed(self):
        """A linear segment from value 0 to 100 over 50 frames at 24 fps:
        speed = 100 / (50/24) = 48 per second."""
        pts = _pts([(0, 0.0), (50, 100.0)])
        speeds = compute_speeds(pts, fps=24.0)
        assert len(speeds) == 2
        assert speeds[0] == pytest.approx(48.0)
        # Last keyframe has no segment — mirrors the prior speed.
        assert speeds[1] == pytest.approx(48.0)

    def test_hold_segment_zero_speed(self):
        pts = _pts([(0, 0.0), (50, 100.0)], interp="hold")
        speeds = compute_speeds(pts, fps=24.0)
        assert speeds[0] == 0.0

    def test_negative_speed_for_descending_value(self):
        """A segment that decreases in value has negative speed."""
        pts = _pts([(0, 100.0), (50, 0.0)])
        speeds = compute_speeds(pts, fps=24.0)
        assert speeds[0] < 0.0


class TestSampleSpeedCurve:
    def test_empty_when_fewer_than_two_points(self):
        assert sample_speed_curve([], 0, 100, 10.0) == []

    def test_linear_segment_flat_speed(self):
        """A linear value segment has constant speed across the segment."""
        pts = _pts([(0, 0.0), (50, 100.0)])
        samples = sample_speed_curve(pts, 0, 50, pixels_per_frame=10.0,
                                     fps=24.0)
        assert samples
        # Every sample's speed equals 100/(50/24) = 48.
        for _f, v in samples:
            assert v == pytest.approx(48.0, abs=1e-6)

    def test_hold_segment_zero_speed(self):
        pts = _pts([(0, 0.0), (50, 100.0)], interp="hold")
        samples = sample_speed_curve(pts, 0, 50, pixels_per_frame=10.0)
        for _f, v in samples:
            assert v == 0.0


# ── T122: selection summary ───────────────────────────────────────────


class TestSummarizeSelection:
    def _series_with_selection(self, sel_indices=(1,)):
        """Build a series with 3 points; mark the given indices selected."""
        node = _node_with_track()
        s = build_graph_series(node, "rotation")
        for i, kp in enumerate(s.points):
            kp.selected = i in sel_indices
        return s

    def test_empty_selection_returns_count_zero(self):
        s = self._series_with_selection(sel_indices=())
        summary = summarize_selection([s])
        assert summary["count"] == 0
        assert summary["frame"] is None
        assert summary["value"] is None

    def test_single_selection_returns_full_detail(self):
        s = self._series_with_selection(sel_indices=(1,))
        summary = summarize_selection([s], fps=24.0)
        assert summary["count"] == 1
        assert summary["property"] == "Rotation"
        assert summary["frame"] == 25   # middle keyframe
        assert summary["value"] == 90.0
        assert summary["interpolation"] == "linear"
        # Speed from the segment starting at frame 25: (180-90)/((50-25)/24)
        assert summary["speed"] == pytest.approx(90.0 / (25.0 / 24.0))

    def test_last_keyframe_speed_is_none(self):
        """The last keyframe has no following segment → speed stays None."""
        s = self._series_with_selection(sel_indices=(2,))
        summary = summarize_selection([s])
        assert summary["count"] == 1
        assert summary["speed"] is None

    def test_hold_segment_speed_zero(self):
        """A hold segment has zero speed."""
        node = _node_with_track()
        s = build_graph_series(node, "rotation")
        # Force the first segment to hold.
        s.points[0].interpolation_out = "hold"
        s.points[0].selected = True
        summary = summarize_selection([s])
        assert summary["speed"] == 0.0

    def test_multi_selection_returns_count_only(self):
        """Multiple selected keyframes → count > 1, no single-point detail."""
        s = self._series_with_selection(sel_indices=(0, 1))
        summary = summarize_selection([s])
        assert summary["count"] == 2
        assert summary["frame"] is None
        assert summary["value"] is None
        # Property is still populated (the first selected series' name).
        assert summary["property"] == "Rotation"

    def test_multi_series_single_selection(self):
        """One selected keyframe across multiple series → that point's detail."""
        s1 = self._series_with_selection(sel_indices=())
        s2 = self._series_with_selection(sel_indices=(1,))
        summary = summarize_selection([s1, s2])
        assert summary["count"] == 1
        assert summary["frame"] == 25

