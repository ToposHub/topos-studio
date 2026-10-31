"""P1 tests: Spline curve ↔ evaluator interpolation consistency (plan §3).

The single most important correctness property: the curve the user sees and
edits in the Spline panel MUST be the curve the renderer actually interpolates.
These tests verify that ``bezier_value_at_frame()`` (used by the evaluator via
KeyframeTrack.get_value) matches the Spline ``sample_segment()`` output, and
that handle edits survive save/load.
"""

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from PyQt6.QtCore import QPointF

from src.shared.keyframe import (
    Keyframe, KeyframeTrack, InterpolationType,
    bezier_value_at_frame, interpolate)
from src.features.composite.ui.spline_model import (
    SplineKey, SplineSegment, SplineCurve, build_segments, sample_segment,
    cubic_bezier_xy, INTERP_BEZIER, INTERP_EASE_IN, INTERP_EASE_OUT,
    INTERP_EASE_IN_OUT)


pytestmark = pytest.mark.composite_core


# ── P1-2: evaluator bezier matches Spline sampling ──────────────────────

def _curve_with_handles(h0_right, h1_left):
    """Build a 2-key bezier curve with given absolute handles."""
    return SplineCurve(
        curve_id="n:p", node_id="n", param_name="p", label="p", color="#fff",
        keys=[
            SplineKey(0, 0.0, INTERP_BEZIER,
                      handle_left=QPointF(-3, 0), handle_right=h0_right),
            SplineKey(20, 100.0, INTERP_BEZIER,
                      handle_left=h1_left, handle_right=QPointF(23, 100)),
        ])


def test_evaluator_value_matches_spline_sample_midframe():
    """At a mid frame, evaluator bezier_value_at_frame == Spline sample y."""
    # asymmetric handles so x matters
    curve = _curve_with_handles(QPointF(2, 0), QPointF(18, 100))
    segs = build_segments(curve)
    assert len(segs) == 1
    seg = segs[0]

    # Build equivalent Keyframes with absolute handles for the evaluator.
    kf0 = Keyframe(0, 0.0, InterpolationType.BEZIER,
                   handle_left=(-3.0, 0.0), handle_right=(2.0, 0.0))
    kf1 = Keyframe(20, 100.0, InterpolationType.BEZIER,
                   handle_left=(-2.0, 0.0), handle_right=(3.0, 0.0))

    for frame in (1, 3, 7, 10, 13, 17, 19):
        # Spline sample at this frame: solve t for x=frame on the segment.
        spline_pts = sample_segment(seg, 256)
        # find the sampled point whose x is closest to `frame`
        best = min(spline_pts, key=lambda p: abs(p.x() - frame))
        spline_y = best.y()
        eval_y = bezier_value_at_frame(frame, kf0, kf1)
        assert abs(spline_y - eval_y) < 2.0, (
            f"frame {frame}: spline={spline_y} evaluator={eval_y}")


def test_evaluator_value_matches_interpolate_at_endpoints():
    """interpolate() (used by KeyframeTrack.get_value) returns key values at
    the exact key frames."""
    kf0 = Keyframe(0, 5.0, InterpolationType.BEZIER)
    kf1 = Keyframe(10, 15.0, InterpolationType.BEZIER)
    assert interpolate(0, kf0, kf1, 0.0, 100.0) == 5.0
    assert interpolate(10, kf0, kf1, 0.0, 100.0) == 15.0


def test_asymmetric_x_handles_change_value_distribution():
    """Non-symmetric x handles must produce a non-linear time mapping (the
    whole point of 2D cubic — old y-only bezier could not do this)."""
    kf0 = Keyframe(0, 0.0, InterpolationType.BEZIER,
                   handle_right=(1.0, 0.0))   # handle very close to kf0 → slow start
    kf1 = Keyframe(20, 100.0, InterpolationType.BEZIER,
                   handle_left=(-1.0, 0.0))
    # At frame 5 (25% of interval), a slow-start curve should be BELOW the
    # linear 25.0 value. (bezier_value_at_frame does not clamp.)
    v = bezier_value_at_frame(5, kf0, kf1)
    assert v < 25.0


def test_multi_key_bezier_handles_make_ease_in_out_segments():
    """Several explicit handles must produce a real ease-in/ease-out path."""
    values = [(0, 0.0), (20, 100.0), (40, 0.0), (60, 100.0), (80, 0.0)]
    curve = SplineCurve(
        curve_id="n:ease", node_id="n", param_name="ease", label="ease",
        color="#fff",
        keys=[
            SplineKey(
                frame, value, INTERP_BEZIER, "broken",
                handle_left=QPointF(frame - 6, value),
                handle_right=QPointF(frame + 6, value),
            )
            for frame, value in values
        ],
    )
    segments = build_segments(curve)
    assert len(segments) == 4
    assert all(segment.interpolation == INTERP_BEZIER
               for segment in segments)

    # Horizontal handles at both ends create a slow start and slow finish.
    for start, end in zip(values, values[1:]):
        k0 = Keyframe(
            start[0], start[1], InterpolationType.BEZIER,
            handle_right=(6.0, 0.0),
        )
        k1 = Keyframe(
            end[0], end[1], InterpolationType.BEZIER,
            handle_left=(-6.0, 0.0),
        )
        quarter = bezier_value_at_frame(start[0] + 5, k0, k1)
        three_quarters = bezier_value_at_frame(start[0] + 15, k0, k1)
        delta = end[1] - start[1]
        # Compared with linear progress, the first quarter has not travelled
        # as far and the last quarter has travelled farther (for either sign).
        assert abs(quarter - start[1]) < abs(delta) * 0.25
        assert abs(three_quarters - start[1]) > abs(delta) * 0.75


def test_handle_x_outside_segment_does_not_reverse_time():
    """Handles are clamped during resolution so x(t) stays monotonic — every
    frame maps to exactly one t (bisection is valid, time never reverses).

    We verify this indirectly: bezier_value_at_frame returns a finite, real
    number for every frame in the segment, and the result at the endpoints
    equals the key values (the curve does not 'jump' to a wrong t-branch)."""
    kf0 = Keyframe(0, 0.0, InterpolationType.BEZIER,
                   handle_right=(100.0, 50.0))  # x way past kf1 → must clamp
    kf1 = Keyframe(10, 100.0, InterpolationType.BEZIER,
                   handle_left=(-100.0, 50.0))
    for frame in range(0, 11):
        v = bezier_value_at_frame(float(frame), kf0, kf1)
        assert math.isfinite(v)
    # Endpoints must hit the key values (no wrong-branch solve). Bisection
    # precision is ~1e-5 after 24 iterations, so allow a small tolerance.
    assert abs(bezier_value_at_frame(0.0, kf0, kf1) - 0.0) < 1e-4
    assert abs(bezier_value_at_frame(10.0, kf0, kf1) - 100.0) < 1e-4


def test_hold_and_linear_unaffected_by_bezier_path():
    """hold/linear interpolations must keep their old behaviour."""
    kf0_h = Keyframe(0, 1.0, InterpolationType.HOLD)
    kf1_h = Keyframe(10, 9.0, InterpolationType.HOLD)
    assert interpolate(5, kf0_h, kf1_h, 0.0, 100.0) == 1.0  # holds first value

    kf0_l = Keyframe(0, 0.0, InterpolationType.LINEAR)
    kf1_l = Keyframe(10, 10.0, InterpolationType.LINEAR)
    assert abs(interpolate(5, kf0_l, kf1_l, 0.0, 100.0) - 5.0) < 1e-6


@pytest.mark.parametrize("mode, enum_mode", [
    (INTERP_EASE_IN, InterpolationType.EASE_IN),
    (INTERP_EASE_OUT, InterpolationType.EASE_OUT),
    (INTERP_EASE_IN_OUT, InterpolationType.EASE_IN_OUT),
])
def test_spline_ease_preview_matches_renderer_interpolation(mode, enum_mode):
    """Ease curves must use the same progress function in UI and playback."""
    curve = SplineCurve(
        curve_id="n:p", node_id="n", param_name="p", label="p", color="#fff",
        keys=[SplineKey(0, 0.0, mode), SplineKey(100, 100.0, mode)])
    segment = build_segments(curve)[0]
    points = sample_segment(segment, 101)
    kf0 = Keyframe(0, 0.0, enum_mode)
    kf1 = Keyframe(100, 100.0, InterpolationType.LINEAR)

    for frame in (25, 50, 75):
        spline_value = points[frame].y()
        render_value = interpolate(frame, kf0, kf1, 0.0, 100.0)
        assert spline_value == pytest.approx(render_value, abs=1e-6)


# ── P1-1: handle format round-trip + preservation ───────────────────────

def test_keyframe_handle_round_trip():
    """handle_left/right + tangent_mode survive to_dict/from_dict."""
    kf = Keyframe(10, 5.0, InterpolationType.BEZIER,
                  tangent_mode="smooth",
                  handle_left=(-3.0, -1.0), handle_right=(3.0, 1.0))
    d = kf.to_dict()
    assert d["tangent_mode"] == "smooth"
    assert d["handle_left"] == [-3.0, -1.0]
    assert d["handle_right"] == [3.0, 1.0]

    kf2 = Keyframe.from_dict(d)
    assert kf2.tangent_mode == "smooth"
    assert kf2.handle_left == (-3.0, -1.0)
    assert kf2.handle_right == (3.0, 1.0)


def test_legacy_bezier_in_out_still_loads():
    """Old project files with only bezier_in/out load without handles set."""
    d = {"frame": 5, "value": 2.0, "interpolation": "bezier",
         "bezier_in": [0.3, 0.1], "bezier_out": [0.7, 0.9]}
    kf = Keyframe.from_dict(d)
    assert kf.bezier_in == (0.3, 0.1)
    assert kf.handle_left is None  # not present → auto-derive
    assert kf.handle_right is None


def test_spline_handle_preserved_on_rebuild():
    """P1-1 #4: a user-edited handle survives a SplineModel rebuild (no auto
    overwrite) when tangent_mode is not 'auto'."""
    from src.features.composite.ui.spline_model import SplineModel
    from src.features.composite.core.node_engine import NodeGraph
    from src.features.composite.nodes.transform_node import TransformNode
    from src.shared.keyframe import KeyframeTrack

    g = NodeGraph()
    t = TransformNode()
    # Build a track where the key has explicit saved handles + smooth mode.
    kf = Keyframe(0, 0.0, InterpolationType.BEZIER,
                  tangent_mode="smooth",
                  handle_left=(-3.0, -1.0), handle_right=(3.0, 1.0))
    track = KeyframeTrack(param_name="rotation")
    track.add_keyframe(kf)
    track.add_keyframe(Keyframe(20, 90.0, InterpolationType.BEZIER,
                                tangent_mode="smooth",
                                handle_left=(-3.0, 0.0), handle_right=(3.0, 0.0)))
    t.set_keyframe_track("rotation", track)
    g.add_node(t)

    m = SplineModel()
    m.rebuild(g)
    c = m.curve_by_id(f"{t.id}:rotation")
    k0 = c.keys[0]
    # The saved right handle (3,1) must survive, not be overwritten by auto.
    assert abs(k0.handle_right.x() - 3.0) < 1e-6
    assert abs(k0.handle_right.y() - 1.0) < 1e-6


def test_keyframe_panel_move_preserves_handles():
    """P1-1 #6: moving a keyframe in the Keyframes panel keeps handle fields."""
    from src.features.composite.ui.keyframes_model import KeyframesModel, KeyframePointVisual
    # A visual point carrying saved handle fields in `extra`.
    pts = [KeyframePointVisual(frame=0, value=0.0, interpolation="bezier",
                               extra={"handle_left": [-3.0, 0.0],
                                      "handle_right": [3.0, 0.0],
                                      "tangent_mode": "smooth"}),
           KeyframePointVisual(frame=10, value=5.0, interpolation="bezier")]
    # Moving frame 0 → 2 should preserve the handle fields in output.
    pts[0].frame = 2
    out = pts[0].to_keyframe_dict()
    assert out["handle_left"] == [-3.0, 0.0]
    assert out["handle_right"] == [3.0, 0.0]
    assert out["tangent_mode"] == "smooth"
    assert out["frame"] == 2
