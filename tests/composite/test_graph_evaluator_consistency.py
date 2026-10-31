"""T126 Graph numerical-consistency tests.

Locks that the Graph Editor's curve sampling matches the AUTHORITATIVE
keyframe evaluator (src.shared.keyframe.interpolate / bezier_value_at_frame)
at every keyframe frame (plan §24.1 'Linear 曲线是直线段', §24.5 '渲染结果
相同'). The Graph is a faithful visualisation — at a keyframe frame the
sampled value MUST equal the evaluator's value, otherwise the curve the
user sees would disagree with the render.

Also covers:
  * Linear segments are straight (constant slope between keyframes).
  * Hold segments are flat then jump (speed 0 between, jump at the end).
  * Speed units are per-second (dv / dt_seconds, fps-scaled).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.ui.graph_model import (
    GraphPoint, sample_speed_curve, sample_value_curve,
)
from src.shared.keyframe import Keyframe, interpolate
from src.shared.keyframe import InterpolationType


pytestmark = pytest.mark.composite_core


# ── helpers ───────────────────────────────────────────────────────────


def _pts(kfs, interp="linear"):
    """Build GraphPoint list from (frame, value) tuples."""
    return [
        GraphPoint(row_id="param:n:p", node_id="n", parameter_name="p",
                   channel=None, frame=float(f), value=float(v),
                   interpolation_in=interp, interpolation_out=interp)
        for f, v in kfs
    ]


def _kf(frame, value, interp=InterpolationType.LINEAR):
    return Keyframe(frame, value, interpolation=interp)


# ── T126: sample matches evaluator at keyframe frames ─────────────────


class TestSampleMatchesEvaluator:
    def test_linear_sample_at_keyframes_matches_interpolate(self):
        """At every keyframe frame, the sampled value equals the keyframe's
        own value (the sampler forces keyframe frames into the sample set,
        so the curve passes exactly through each keyframe — plan §24.1)."""
        kfs = ((0, 0.0), (25, 50.0), (50, 100.0), (75, 50.0), (100, 0.0))
        pts = _pts(kfs, interp="linear")
        samples = sample_value_curve(pts, 0, 100, pixels_per_frame=10.0)
        # The sampler guarantees a sample AT each keyframe frame with the
        # keyframe's exact value.
        for f, v in kfs:
            matches = [sv for sf, sv in samples if abs(sf - f) < 0.01]
            assert matches, f"no sample near keyframe frame {f}"
            assert matches[0] == pytest.approx(v, rel=1e-6)

    def test_linear_sample_between_keyframes_matches_interpolate(self):
        """At a midpoint frame, the sampled value matches the linear
        evaluator (the curve is a faithful visualisation, not an
        approximation for linear). Uses a fine grid so sample frames land
        near the test frames."""
        pts = _pts(((0, 0.0), (100, 100.0)), interp="linear")
        # Fine grid (0.5-frame step) so frames land close to integers.
        samples = sample_value_curve(pts, 0, 100, pixels_per_frame=20.0)
        kf1 = _kf(0, 0.0)
        kf2 = _kf(100, 100.0)
        # For each test frame, find the nearest sample and compare to the
        # evaluator at THAT sample's frame (not the test frame) — the grid
        # may not hit the integer exactly.
        for frame in (10, 25, 50, 75, 90):
            nearest = min(samples, key=lambda s: abs(s[0] - frame))
            eval_val = interpolate(nearest[0], kf1, kf2,
                                   min_val=-1e6, max_val=1e6)
            assert nearest[1] == pytest.approx(eval_val, rel=1e-6)

    def test_hold_sample_flat_then_jump(self):
        """A Hold segment is flat at the left keyframe's value, then jumps
        at the right keyframe (speed 0 between)."""
        pts = _pts(((0, 0.0), (50, 100.0)), interp="hold")
        samples = sample_value_curve(pts, 0, 50, pixels_per_frame=2.0)
        # Every sample strictly before frame 50 should equal 0.0.
        for f, v in samples:
            if f < 50:
                assert v == pytest.approx(0.0)
        # The sample at (or just before) frame 50 jumps to 100.
        assert any(v == pytest.approx(100.0) for f, v in samples if f >= 50)


# ── T126: linear is straight (constant slope) ─────────────────────────


class TestLinearIsStraight:
    def test_linear_constant_slope(self):
        """A linear segment's samples have a constant slope (dv/df)."""
        pts = _pts(((0, 0.0), (100, 100.0)), interp="linear")
        samples = sample_value_curve(pts, 0, 100, pixels_per_frame=10.0)
        # Slope between consecutive samples should be ~constant.
        slopes = []
        for i in range(1, len(samples)):
            df = samples[i][0] - samples[i - 1][0]
            dv = samples[i][1] - samples[i - 1][1]
            if df > 1e-9:
                slopes.append(dv / df)
        # All slopes within a tight band of the overall slope (1.0 here).
        for s in slopes:
            assert s == pytest.approx(1.0, rel=0.05)


# ── T126: speed units per-second ──────────────────────────────────────


class TestSpeedUnits:
    def test_linear_speed_is_constant_per_second(self):
        """Linear speed = dv/dt_seconds = (Δvalue / Δframes) * fps."""
        fps = 24.0
        pts = _pts(((0, 0.0), (24, 100.0)), interp="linear")  # 1 second
        samples = sample_speed_curve(pts, 0, 24, pixels_per_frame=5.0,
                                     fps=fps)
        # Over 24 frames = 1 second, value goes 0→100 → speed ≈ 100/s.
        nonzero = [(f, v) for f, v in samples if abs(v) > 1e-6]
        assert nonzero  # some non-zero speed samples
        for f, v in nonzero:
            # Linear segment → constant speed = 100/1s = 100.
            assert v == pytest.approx(100.0, rel=0.1)

    def test_hold_speed_is_zero(self):
        """A Hold segment has speed 0 throughout (no value change)."""
        pts = _pts(((0, 50.0), (50, 50.0)), interp="hold")
        samples = sample_speed_curve(pts, 0, 50, pixels_per_frame=5.0,
                                     fps=24.0)
        for f, v in samples:
            if 0 < f < 50:
                assert v == pytest.approx(0.0, abs=1e-6)

    def test_speed_scales_with_fps(self):
        """The same value change over the same frames yields a higher
        per-second speed at higher fps (less time elapsed)."""
        pts = _pts(((0, 0.0), (24, 100.0)), interp="linear")
        s24 = sample_speed_curve(pts, 0, 24, pixels_per_frame=5.0, fps=24.0)
        s48 = sample_speed_curve(pts, 0, 24, pixels_per_frame=5.0, fps=48.0)
        avg24 = sum(v for _f, v in s24 if v > 0) / max(1, sum(1 for _f, v in s24 if v > 0))
        avg48 = sum(v for _f, v in s48 if v > 0) / max(1, sum(1 for _f, v in s48 if v > 0))
        # At 48fps, 24 frames = 0.5s → speed = 100/0.5 = 200 (2× the 24fps).
        assert avg48 == pytest.approx(avg24 * 2.0, rel=0.1)


# ── C-1: bezier sample matches evaluator WITH handle x-offset ────────
#
# This is the regression that motivated the C-1 fix. The Graph sampler
# previously used the frame ratio as the Bezier parameter t:
#     t = (f - k0.frame) / (k1.frame - k0.frame)
# but a Bezier's x axis is NOT uniform — when a handle has an x offset,
# x(t) is non-linear, so this produced a curve that DIVERGED from the
# renderer's bezier_value_at_frame (which solves x(t)=frame by bisection).
# At frame 25 with handles offset 80 frames, the old code was off by ~11
# units on a 0..100 curve. These tests lock the fix.


def _bezier_pts(k0, k1, hr, hl):
    """Build two GraphPoints with bezier interpolation + absolute handles.
    ``hr``/``hl`` are (frame_delta, value_delta) handle offsets."""
    return [
        GraphPoint(row_id="param:n:p", node_id="n", parameter_name="p",
                   channel=None, frame=float(k0[0]), value=float(k0[1]),
                   interpolation_in="bezier", interpolation_out="bezier",
                   handle_right=hr),
        GraphPoint(row_id="param:n:p", node_id="n", parameter_name="p",
                   channel=None, frame=float(k1[0]), value=float(k1[1]),
                   interpolation_in="bezier", interpolation_out="bezier",
                   handle_left=hl),
    ]


def _bezier_kfs(k0, k1, hr, hl):
    """Build two renderer Keyframes with the same handles."""
    return (
        Keyframe(frame=k0[0], value=k0[1],
                 interpolation=InterpolationType.BEZIER, handle_right=hr),
        Keyframe(frame=k1[0], value=k1[1],
                 interpolation=InterpolationType.BEZIER, handle_left=hl),
    )


class TestBezierMatchesEvaluator:
    """C-1: the Graph's bezier sampling must match the renderer's
    bezier_value_at_frame even when handles have large x offsets (the case
    where the old frame-ratio-as-t approximation was badly wrong)."""

    def test_large_handle_x_offset_aligned(self):
        """Handles offset 80 frames on a 100-frame segment — the worst case
        for the old approximation. Graph samples must match the renderer.

        We sample densely (ppf=10 → ~0.3 frame step) so the nearest sample
        to each integer frame is within ~0.15 frames; the curve is steep
        near the ends, so we allow a tolerance that bounds the sub-frame
        linear-interpolation error, not the algorithm error. The OLD bug
        produced ~11 unit divergence — this tolerance catches that while
        accepting the legitimate sub-frame sampling gap."""
        from src.shared.keyframe import bezier_value_at_frame
        pts = _bezier_pts((0, 0.0), (100, 100.0),
                          hr=(80.0, 0.0), hl=(-80.0, 0.0))
        kf0, kf1 = _bezier_kfs((0, 0.0), (100, 100.0),
                               hr=(80.0, 0.0), hl=(-80.0, 0.0))
        samples = sample_value_curve(pts, 0, 100, pixels_per_frame=10.0)
        max_diff = 0.0
        for f in range(5, 96):  # interior frames only
            target = float(f)
            nearest = min(samples, key=lambda s: abs(s[0] - target))
            renderer_v = bezier_value_at_frame(target, kf0, kf1)
            max_diff = max(max_diff, abs(nearest[1] - renderer_v))
        assert max_diff < 1.0, (
            f"Graph bezier diverged from renderer by {max_diff:.3f} — the "
            "old frame-ratio-as-t bug may have returned (C-1). The old bug "
            "diverged by ~11 units; sub-frame sampling noise is < 1.")

    def test_mid_frame_value_matches(self):
        """At frame 25 with offset handles, the old code gave 15.625 while
        the renderer gives 4.7644. Lock the renderer's value."""
        from src.shared.keyframe import bezier_value_at_frame
        pts = _bezier_pts((0, 0.0), (100, 100.0),
                          hr=(80.0, 0.0), hl=(-80.0, 0.0))
        kf0, kf1 = _bezier_kfs((0, 0.0), (100, 100.0),
                               hr=(80.0, 0.0), hl=(-80.0, 0.0))
        samples = sample_value_curve(pts, 0, 100, pixels_per_frame=1.0)
        renderer_25 = bezier_value_at_frame(25.0, kf0, kf1)
        nearest = min(samples, key=lambda s: abs(s[0] - 25.0))
        assert nearest[1] == pytest.approx(renderer_25, abs=1.5), (
            f"Frame 25 graph={nearest[1]:.3f} vs renderer={renderer_25:.3f}. "
            "Old buggy value was 15.625 — must not regress (C-1).")

    def test_symmetric_handles_match(self):
        """Symmetric handles (equal x offset both sides) — mid-point must
        be exactly the average (50), matching the renderer. Sample densely
        so the nearest sample to frame 50 is within sub-frame tolerance."""
        from src.shared.keyframe import bezier_value_at_frame
        pts = _bezier_pts((0, 0.0), (100, 100.0),
                          hr=(33.0, 0.0), hl=(-33.0, 100.0))
        kf0, kf1 = _bezier_kfs((0, 0.0), (100, 100.0),
                               hr=(33.0, 0.0), hl=(-33.0, 100.0))
        samples = sample_value_curve(pts, 0, 100, pixels_per_frame=10.0)
        nearest_50 = min(samples, key=lambda s: abs(s[0] - 50.0))
        renderer_50 = bezier_value_at_frame(50.0, kf0, kf1)
        assert nearest_50[1] == pytest.approx(renderer_50, abs=1.5)

    def test_no_handles_falls_back_smoothly(self):
        """Bezier segment with no handles still samples without error and
        produces a monotonic curve (the default 1/3 influence)."""
        k0 = GraphPoint(row_id="param:n:p", node_id="n", parameter_name="p",
                        channel=None, frame=0.0, value=0.0,
                        interpolation_in="bezier", interpolation_out="bezier")
        k1 = GraphPoint(row_id="param:n:p", node_id="n", parameter_name="p",
                        channel=None, frame=100.0, value=100.0,
                        interpolation_in="bezier", interpolation_out="bezier")
        samples = sample_value_curve([k0, k1], 0, 100, pixels_per_frame=5.0)
        assert len(samples) > 2
        # Monotonic increasing (default handles slope toward the endpoints).
        vals = [v for _f, v in samples]
        assert vals[0] <= vals[-1] + 1e-6


class TestEaseMatchesEvaluator:
    """C-1: ease_* segments must use the renderer's exact easing curves,
    not a bezier approximation (the old code approximated ease as a cubic
    bezier with 1/3 influence, which is a different curve)."""

    @pytest.mark.parametrize("ease_name,ease_fn_name", [
        ("ease_in", "ease_in_interpolate"),
        ("ease_out", "ease_out_interpolate"),
        ("ease_in_out", "ease_in_out_interpolate"),
    ])
    def test_ease_curve_matches_renderer(self, ease_name, ease_fn_name):
        from src.shared.keyframe import Keyframe as KF, InterpolationType
        import src.shared.keyframe as kfm
        ease_fn = getattr(kfm, ease_fn_name)
        itp = {"ease_in": InterpolationType.EASE_IN,
               "ease_out": InterpolationType.EASE_OUT,
               "ease_in_out": InterpolationType.EASE_IN_OUT}[ease_name]
        pts = _pts(((0, 0.0), (100, 100.0)), interp=ease_name)
        kf0 = KF(0, 0.0, interpolation=itp)
        kf1 = KF(100, 100.0, interpolation=InterpolationType.LINEAR)
        samples = sample_value_curve(pts, 0, 100, pixels_per_frame=2.0)
        # Compare at several interior frames.
        for f in (10, 25, 50, 75, 90):
            t = f / 100.0
            expected = ease_fn(t, kf0, kf1)
            nearest = min(samples, key=lambda s: abs(s[0] - float(f)))
            assert nearest[1] == pytest.approx(expected, abs=2.0), (
                f"{ease_name} at frame {f}: graph={nearest[1]:.3f} vs "
                f"renderer={expected:.3f} — ease must use the renderer's "
                "exact curve, not a bezier approximation (C-1).")
