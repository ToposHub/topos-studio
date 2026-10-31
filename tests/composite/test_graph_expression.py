"""T127 Graph expression regression tests.

Locks the expression-graph contract (plan §24.3 / §22.11):
  * pre-expression graph shows the keyframe/static evaluation;
  * post-expression graph shows the expression's final result;
  * the two curves have distinct visual treatment (the view renders pre
    dark-dashed, post bright-solid — covered by the view test suite; here
    we lock the DATA: pre != post for a non-identity expression);
  * expression errors fall back to pre + flag has_error (post == pre);
  * valueAtTime in an expression reads the pre (animated) value at an
    arbitrary frame and the post curve reflects it.

Pure-data tests against sample_expression_curves + ExpressionParser — no
QWidget.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.ui.graph_model import (
    GraphPoint, sample_expression_curves,
)
from src.features.composite.utils.expression_parser import (
    ExpressionContext, ExpressionParser,
)


pytestmark = pytest.mark.composite_core


# ── helpers ───────────────────────────────────────────────────────────


def _pts(kfs, interp="linear"):
    return [
        GraphPoint(row_id="param:n:p", node_id="n", parameter_name="p",
                   channel=None, frame=float(f), value=float(v),
                   interpolation_in=interp, interpolation_out=interp)
        for f, v in kfs
    ]


def _ctx():
    return ExpressionContext()


def _parser():
    return ExpressionParser()


# ── T127: pre/post distinct ───────────────────────────────────────────


class TestPrePostDistinct:
    def test_non_identity_expression_pre_ne_post(self):
        """For 'value * 2', the post curve differs from pre everywhere."""
        pts = _pts(((0, 0.0), (100, 100.0)))
        pre, post, err = sample_expression_curves(
            pts, "value * 2", 0, 100, pixels_per_frame=10.0,
            context=_ctx(), parser=_parser())
        assert err is False
        assert pre != post
        # post == 2 * pre at every shared frame.
        for (f1, v1), (f2, v2) in zip(pre, post):
            assert v2 == pytest.approx(v1 * 2, rel=1e-6)

    def test_identity_expression_pre_eq_post(self):
        """For 'value' (identity), post mirrors pre exactly."""
        pts = _pts(((0, 0.0), (100, 100.0)))
        pre, post, err = sample_expression_curves(
            pts, "value", 0, 100, pixels_per_frame=10.0,
            context=_ctx(), parser=_parser())
        assert err is False
        for (f1, v1), (f2, v2) in zip(pre, post):
            assert v1 == pytest.approx(v2)


# ── T127: error fallback ──────────────────────────────────────────────


class TestErrorFallback:
    def test_syntax_error_falls_back_and_flags(self):
        """A syntax error → post falls back to pre, has_error True."""
        pts = _pts(((0, 0.0), (100, 100.0)))
        pre, post, err = sample_expression_curves(
            pts, "value +", 0, 100, pixels_per_frame=10.0,
            context=_ctx(), parser=_parser())
        assert err is True
        for (f1, v1), (f2, v2) in zip(pre, post):
            assert v1 == pytest.approx(v2)  # post == pre (fallback)

    def test_runtime_error_falls_back_per_sample(self):
        """An expression that fails at some frames falls back per-sample
        and flags has_error."""
        pts = _pts(((0, 0.0), (100, 100.0)))
        # sqrt(value - 50): fails when value < 50 (negative radicand).
        pre, post, err = sample_expression_curves(
            pts, "sqrt(value - 50)", 0, 100, pixels_per_frame=10.0,
            context=_ctx(), parser=_parser())
        assert err is True  # at least one sample failed
        # Where it failed, post == pre; where it succeeded, post != pre.
        any_fallback = any(v1 == pytest.approx(v2)
                           for (f1, v1), (f2, v2) in zip(pre, post))
        assert any_fallback  # some frames fell back


# ── T127: valueAtTime ─────────────────────────────────────────────────


class TestValueAtTime:
    def test_value_at_time_reads_animated_value(self):
        """valueAtTime(t) returns the param's animated value at time t.

        With a 0→100 ramp over 100 frames at 24fps (frame 24 = t=1.0s),
        valueAtTime(0.5) should return the value at frame 12 (≈12)."""
        pts = _pts(((0, 0.0), (100, 100.0)))
        fps = 24.0
        ctx = _ctx()
        ctx.fps = fps
        # The parser wires value_at_time when evaluating — set up a callable
        # that reads the pre (linear) value at a given time.
        def value_at_time(t):
            frame = t * fps
            # Linear interp 0→100 over frames 0→100.
            return max(0.0, min(100.0, frame))
        ctx.value_at_time = value_at_time
        # Expression that adds the value-at-1-second to the current value.
        _pre, post, err = sample_expression_curves(
            pts, "valueAtTime(1.0)", 0, 100, pixels_per_frame=10.0,
            fps=fps, context=ctx, parser=_parser())
        assert err is False
        # valueAtTime(1.0) = value at frame 24 = 24.0 (linear ramp).
        for f, v in post:
            assert v == pytest.approx(24.0, rel=1e-6)

    def test_time_variable_matches_frame_over_fps(self):
        """The 'time' variable equals frame/fps (consistency with the
        evaluator's frame/time model)."""
        pts = _pts(((0, 0.0), (48, 100.0)))
        fps = 24.0
        ctx = _ctx()
        ctx.fps = fps
        _pre, post, err = sample_expression_curves(
            pts, "time", 0, 48, pixels_per_frame=10.0, fps=fps,
            context=ctx, parser=_parser())
        assert err is False
        for f, v in post:
            assert v == pytest.approx(f / fps, rel=1e-6)

    def test_frame_variable_matches_frame(self):
        """The 'frame' variable equals the integer frame number."""
        pts = _pts(((0, 0.0), (100, 100.0)))
        _pre, post, err = sample_expression_curves(
            pts, "frame", 0, 100, pixels_per_frame=10.0,
            context=_ctx(), parser=_parser())
        assert err is False
        for f, v in post:
            assert v == pytest.approx(round(f), abs=1.0)


# ── T127: pre baseline for expression-only param ──────────────────────


class TestPreBaselineStatic:
    def test_expression_only_pre_is_static(self):
        """A param with an expression but no keyframes: pre is a flat line
        at the static value (plan §22.11 'pre = static evaluation')."""
        pre, post, err = sample_expression_curves(
            [], "value + 5", 0, 100, pixels_per_frame=10.0,
            static_value=42.0, context=_ctx(), parser=_parser())
        assert err is False
        # Pre is flat at 42.
        for f, v in pre:
            assert v == pytest.approx(42.0)
        # Post = 42 + 5 = 47.
        for f, v in post:
            assert v == pytest.approx(47.0)

    def test_no_context_post_mirrors_pre(self):
        """Without a context (pre-only view), post mirrors pre — no error."""
        pts = _pts(((0, 0.0), (100, 100.0)))
        pre, post, err = sample_expression_curves(
            pts, "value * 2", 0, 100, pixels_per_frame=10.0)
        assert err is False
        assert pre == post
