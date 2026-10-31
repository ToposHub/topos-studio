"""Graph Editor Separate Dimensions (T118) + Expression Graph (T119) tests.

Pure-data tests — no QWidget. Two independent contracts locked here:

T118 — Separate Dimensions: the project stores vector params as SPLIT
SCALARS (translate_x / translate_y), so 'separating' is channel METADATA,
not a data split. These tests lock the channel detector, the sibling
grouping, the separability gate, and the fixed axis colours, plus that
build_graph_series now populates GraphPoint/GraphSeries.channel.

T119 — Reference/Post-Expression Graph: sample_expression_curves produces
the pre curve (keyframe/static eval) and the post curve (expression
result) aligned frame-for-frame, with a has_error flag for the fallback
case. Keyframe diamonds always come from the pre curve; the post curve is
a derived overlay.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.ui.graph_model import (
    GraphPoint, GraphSeries,
    build_graph_series,
    channel_color, find_sibling_channels, is_separable_param,
    param_base_name, param_channel,
    sample_expression_curves, sample_value_curve,
)
from src.features.composite.nodes.transform_node import TransformNode
from src.shared.keyframe import Keyframe, KeyframeTrack


pytestmark = pytest.mark.composite_core


# ── helpers ───────────────────────────────────────────────────────────


def _point(frame, value):
    return GraphPoint(row_id="param:n:p", node_id="n", parameter_name="p",
                      channel=None, frame=float(frame), value=float(value))


def _series(param="rotation", kfs=((0, 0.0), (50, 90.0), (100, 0.0))):
    node = TransformNode()
    track = KeyframeTrack(param_name=param)
    for f, v in kfs:
        track.add_keyframe(Keyframe(f, v))
    node.set_keyframe_track(param, track)
    return build_graph_series(node, param), node


# ── T118: param_channel ───────────────────────────────────────────────


class TestParamChannel:
    def test_x_suffix(self):
        assert param_channel("translate_x") == "x"

    def test_y_suffix(self):
        assert param_channel("scale_y") == "y"

    def test_z_suffix(self):
        assert param_channel("point_z") == "z"

    def test_bare_x(self):
        assert param_channel("x") == "x"

    def test_scalar_returns_none(self):
        assert param_channel("rotation") is None
        assert param_channel("opacity") is None

    def test_empty_returns_none(self):
        assert param_channel("") is None

    def test_case_insensitive(self):
        assert param_channel("Translate_X") == "x"

    def test_non_axis_suffix_returns_none(self):
        # 'max_xy' ends in 'y' but via the _y check? No — it ends in 'xy'.
        assert param_channel("max_xy") is None
        # 'radius' has no axis suffix.
        assert param_channel("radius") is None


# ── T118: param_base_name ─────────────────────────────────────────────


class TestParamBaseName:
    def test_strips_x(self):
        assert param_base_name("translate_x") == "translate"

    def test_strips_y(self):
        assert param_base_name("scale_y") == "scale"

    def test_no_axis_unchanged(self):
        assert param_base_name("rotation") == "rotation"

    def test_empty_unchanged(self):
        assert param_base_name("") == ""

    def test_bare_axis(self):
        assert param_base_name("x") == "x"


# ── T118: find_sibling_channels ───────────────────────────────────────


class TestFindSiblingChannels:
    def test_finds_xy_siblings(self):
        params = ["translate_x", "translate_y", "rotation"]
        sibs = find_sibling_channels("translate_x", params)
        assert sibs == ["translate_x", "translate_y"]

    def test_finds_xyz_siblings(self):
        params = ["point_x", "point_y", "point_z", "opacity"]
        sibs = find_sibling_channels("point_y", params)
        assert sibs == ["point_x", "point_y", "point_z"]

    def test_scalar_returns_self_only(self):
        params = ["translate_x", "translate_y", "rotation"]
        sibs = find_sibling_channels("rotation", params)
        assert sibs == ["rotation"]

    def test_ordered_by_axis(self):
        # Siblings should come back in X, Y, Z order regardless of input.
        params = ["scale_z", "scale_x", "scale_y"]
        sibs = find_sibling_channels("scale_z", params)
        assert sibs == ["scale_x", "scale_y", "scale_z"]

    def test_dedupes(self):
        params = ["translate_x", "translate_x", "translate_y"]
        sibs = find_sibling_channels("translate_x", params)
        assert sibs == ["translate_x", "translate_y"]

    def test_includes_self_even_if_alone(self):
        params = ["scale_x"]  # no scale_y
        sibs = find_sibling_channels("scale_x", params)
        assert sibs == ["scale_x"]


# ── T118: is_separable_param ──────────────────────────────────────────


class TestIsSeparableParam:
    def test_xy_is_separable(self):
        params = ["translate_x", "translate_y", "rotation"]
        assert is_separable_param("translate_x", params) is True

    def test_xyz_is_separable(self):
        params = ["point_x", "point_y", "point_z"]
        assert is_separable_param("point_y", params) is True

    def test_scalar_not_separable(self):
        params = ["translate_x", "translate_y", "rotation"]
        assert is_separable_param("rotation", params) is False

    def test_alone_not_separable(self):
        params = ["scale_x"]  # no sibling
        assert is_separable_param("scale_x", params) is False

    def test_same_axis_only_not_separable(self):
        # Two params on the SAME axis aren't separable dimensions.
        params = ["foo_x", "bar_x"]
        # Both base 'foo'/'bar' differ, so each is alone in its base.
        assert is_separable_param("foo_x", params) is False


# ── T118: channel_color ───────────────────────────────────────────────


class TestChannelColor:
    def test_x_color(self):
        assert channel_color("x") == "#ff8a3d"

    def test_y_color(self):
        assert channel_color("y") == "#3dd97a"

    def test_z_color(self):
        assert channel_color("z") == "#3d9cff"

    def test_none_uses_fallback(self):
        assert channel_color(None) == "#bdbdbd"
        assert channel_color(None, fallback="#ff0000") == "#ff0000"

    def test_unknown_uses_fallback(self):
        assert channel_color("q") == "#bdbdbd"

    def test_case_insensitive(self):
        assert channel_color("X") == "#ff8a3d"


# ── T118: build_graph_series populates channel ────────────────────────


class TestBuildGraphSeriesChannel:
    def test_translate_x_gets_x_channel(self):
        node = TransformNode()
        track = KeyframeTrack(param_name="translate_x")
        track.add_keyframe(Keyframe(0, 10.0))
        node.set_keyframe_track("translate_x", track)
        s = build_graph_series(node, "translate_x")
        assert s.channel == "x"
        assert all(p.channel == "x" for p in s.points)

    def test_translate_y_gets_y_channel(self):
        node = TransformNode()
        track = KeyframeTrack(param_name="translate_y")
        track.add_keyframe(Keyframe(0, 20.0))
        node.set_keyframe_track("translate_y", track)
        s = build_graph_series(node, "translate_y")
        assert s.channel == "y"

    def test_scalar_param_gets_none_channel(self):
        node = TransformNode()
        track = KeyframeTrack(param_name="rotation")
        track.add_keyframe(Keyframe(0, 0.0))
        node.set_keyframe_track("rotation", track)
        s = build_graph_series(node, "rotation")
        assert s.channel is None
        assert all(p.channel is None for p in s.points)

    def test_channel_color_applied_when_default_grey(self):
        node = TransformNode()
        track = KeyframeTrack(param_name="translate_x")
        track.add_keyframe(Keyframe(0, 10.0))
        node.set_keyframe_track("translate_x", track)
        # Default node_color is '#bdbdbd' → channel colour kicks in.
        s = build_graph_series(node, "translate_x")
        assert s.color == "#ff8a3d"

    def test_explicit_color_preserved(self):
        node = TransformNode()
        track = KeyframeTrack(param_name="translate_x")
        track.add_keyframe(Keyframe(0, 10.0))
        node.set_keyframe_track("translate_x", track)
        # Explicit colour overrides the channel colour.
        s = build_graph_series(node, "translate_x", node_color="#ff0000")
        assert s.color == "#ff0000"


# ── T119: sample_expression_curves ────────────────────────────────────


class TestSampleExpressionCurves:
    def _ctx(self):
        from src.features.composite.utils.expression_parser import \
            ExpressionContext
        return ExpressionContext()

    def _parser(self):
        from src.features.composite.utils.expression_parser import \
            ExpressionParser
        return ExpressionParser()

    def test_no_expression_returns_pre_mirrored(self):
        """With no expression, post mirrors pre and has_error is False."""
        pts = [_point(0, 0.0), _point(10, 100.0)]
        pre, post, err = sample_expression_curves(
            pts, "", 0, 10, pixels_per_frame=10.0,
            context=self._ctx(), parser=self._parser())
        assert err is False
        assert pre == post

    def test_no_context_returns_pre_mirrored(self):
        """With no context, post mirrors pre (pre-only view)."""
        pts = [_point(0, 0.0), _point(10, 100.0)]
        pre, post, err = sample_expression_curves(
            pts, "value * 2", 0, 10, pixels_per_frame=10.0)
        assert err is False
        assert pre == post

    def test_static_value_flat_line_no_keyframes(self):
        """No keyframes → pre is a flat line at static_value."""
        pre, post, err = sample_expression_curves(
            [], "value * 2", 0, 10, pixels_per_frame=10.0,
            static_value=42.0, context=self._ctx(),
            parser=self._parser())
        assert err is False
        assert pre[0] == (0.0, 42.0)
        assert pre[-1] == (10.0, 42.0)

    def test_pre_curve_matches_sample_value_curve(self):
        """The pre curve equals sample_value_curve for keyframed params."""
        pts = [_point(0, 0.0), _point(10, 100.0)]
        expected = sample_value_curve(pts, 0, 10, pixels_per_frame=10.0)
        pre, _post, _err = sample_expression_curves(
            pts, "value", 0, 10, pixels_per_frame=10.0,
            context=self._ctx(), parser=self._parser())
        assert pre == expected

    def test_post_curve_double_value(self):
        """Expression 'value * 2' doubles the pre value at each frame."""
        pts = [_point(0, 0.0), _point(10, 100.0)]
        ctx = self._ctx()
        pre, post, err = sample_expression_curves(
            pts, "value * 2", 0, 10, pixels_per_frame=10.0,
            context=ctx, parser=self._parser())
        assert err is False
        # At each shared frame, post == 2 * pre.
        for (f1, v1), (f2, v2) in zip(pre, post):
            assert f1 == pytest.approx(f2)
            assert v2 == pytest.approx(v1 * 2, rel=1e-6)

    def test_post_curve_uses_frame(self):
        """Expression 'frame' returns the frame number."""
        pts = [_point(0, 0.0), _point(10, 100.0)]
        ctx = self._ctx()
        _pre, post, err = sample_expression_curves(
            pts, "frame", 0, 10, pixels_per_frame=10.0,
            context=ctx, parser=self._parser())
        assert err is False
        # Each post value equals its frame (rounded).
        for f, v in post:
            assert v == pytest.approx(round(f), abs=1.0)

    def test_error_expression_falls_back_and_flags(self):
        """A syntactically-invalid expression falls back to the pre curve
        and sets has_error True at every sample."""
        pts = [_point(0, 0.0), _point(10, 100.0)]
        ctx = self._ctx()
        # 'value +' is a syntax error → fails at every sample.
        pre, post, err = sample_expression_curves(
            pts, "value +", 0, 10, pixels_per_frame=10.0,
            context=ctx, parser=self._parser())
        assert err is True
        # Fallback: post mirrors pre.
        for (f1, v1), (f2, v2) in zip(pre, post):
            assert v1 == pytest.approx(v2)

    def test_partial_error_flags_and_falls_back_per_sample(self):
        """If the expression succeeds at some frames and fails at others,
        has_error is True and failed frames use the pre value."""
        pts = [_point(0, 0.0), _point(10, 100.0)]
        ctx = self._ctx()
        # 'sqrt(value - 60)' → fails when value < 60 (negative radicand),
        # succeeds when value >= 60. With a 0..100 ramp this spans both.
        pre, post, err = sample_expression_curves(
            pts, "sqrt(value - 60)", 0, 10, pixels_per_frame=10.0,
            context=ctx, parser=self._parser())
        assert err is True  # at least one sample failed

    def test_post_aligned_frame_for_frame_with_pre(self):
        """Post samples share the exact frames of pre samples."""
        pts = [_point(0, 0.0), _point(10, 100.0)]
        ctx = self._ctx()
        pre, post, _err = sample_expression_curves(
            pts, "value + 1", 0, 10, pixels_per_frame=10.0,
            context=ctx, parser=self._parser())
        assert [f for f, _ in pre] == [f for f, _ in post]

    def test_time_variable_uses_fps(self):
        """Expression 'time' returns frame/fps."""
        pts = [_point(0, 0.0), _point(24, 100.0)]
        ctx = self._ctx()
        _pre, post, err = sample_expression_curves(
            pts, "time", 0, 24, pixels_per_frame=10.0, fps=24.0,
            context=ctx, parser=self._parser())
        assert err is False
        # At frame 24, time = 24/24 = 1.0.
        last_frame, last_val = post[-1]
        assert last_val == pytest.approx(last_frame / 24.0, rel=1e-6)
