"""T118/T119 view-level integration tests.

Drives GraphEditorView through its public API (no mouse events needed
here — those are covered by the drag/marquee suites) to verify:

T118 — build_graph_series now tags series + points with the axis channel,
and the channel colour is applied when the caller used the default grey.

T119 — when a series has an expression AND GraphOptions.
show_post_expression_graph is True, _rebuild_samples populates the pre
and post_expression_samples; when the toggle is off they stay empty; and
painting the dual curves (with and without an error) completes without
crashing.
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from src.features.composite.ui.graph_editor_view import GraphEditorView
from src.features.composite.ui.graph_model import (
    GraphOptions, GraphSeries, GraphPoint, build_graph_series,
)
from src.features.composite.nodes.transform_node import TransformNode
from src.shared.keyframe import Keyframe, KeyframeTrack


pytestmark = pytest.mark.composite_core


# ── fixtures ──────────────────────────────────────────────────────────


def _keyframed_series(param="rotation", kfs=((0, 0.0), (50, 90.0), (100, 0.0))):
    node = TransformNode()
    track = KeyframeTrack(param_name=param)
    for f, v in kfs:
        track.add_keyframe(Keyframe(f, v))
    node.set_keyframe_track(param, track)
    return build_graph_series(node, param), node


@pytest.fixture
def view(qapp):
    v = GraphEditorView()
    v.resize(800, 400)
    v.set_frame_range(0, 100)
    return v


# ── T118: channel tagging reaches the view ────────────────────────────


class TestChannelInView:
    def test_translate_x_series_tagged_x(self, view, qapp):
        s, _n = _keyframed_series("translate_x", ((0, 0.0), (10, 10.0)))
        view.set_series([s])
        assert view._series[0].channel == "x"
        assert all(p.channel == "x" for p in view._series[0].points)

    def test_translate_y_series_tagged_y(self, view, qapp):
        s, _n = _keyframed_series("translate_y", ((0, 0.0), (10, 10.0)))
        view.set_series([s])
        assert view._series[0].channel == "y"

    def test_scalar_series_tagged_none(self, view, qapp):
        s, _n = _keyframed_series("rotation")
        view.set_series([s])
        assert view._series[0].channel is None

    def test_channel_colour_applied(self, view, qapp):
        # Default node_color is grey → X channel gets the orange-red.
        s, _n = _keyframed_series("translate_x", ((0, 0.0), (10, 10.0)))
        view.set_series([s])
        assert view._series[0].color == "#ff8a3d"


# ── T119: pre/post expression curves in the view ──────────────────────


class TestExpressionCurvesInView:
    def _expr_series(self, expr="value * 2", static_value=10.0):
        """Build a series for a param that has an EXPRESSION (and therefore
        no keyframe track — expressions and keyframes are mutually exclusive
        in this project, rule A-15). The pre curve is the static default."""
        node = TransformNode()
        # Set the param's static value (the pre-expression baseline).
        node.set_parameter("rotation", static_value)
        node.set_expression("rotation", expr)
        return build_graph_series(node, "rotation")

    def test_toggle_off_leaves_curves_empty(self, view, qapp):
        """Default options have show_post_expression_graph=False → no
        pre/post samples are populated even when the series has an expr."""
        s = self._expr_series()
        view.set_series([s])
        view.show()
        qapp.processEvents()
        assert view._series[0].pre_expression_samples == []
        assert view._series[0].post_expression_samples == []

    def test_toggle_on_populates_curves(self, view, qapp):
        """With the toggle on, a series with an expression gets both
        pre and post curves sampled."""
        s = self._expr_series("value * 2")
        opts = GraphOptions(show_post_expression_graph=True)
        view.set_options(opts)
        view.set_series([s])
        view.show()
        qapp.processEvents()
        assert view._series[0].pre_expression_samples  # non-empty
        assert view._series[0].post_expression_samples  # non-empty
        # post == 2 * pre at each shared frame.
        pre = view._series[0].pre_expression_samples
        post = view._series[0].post_expression_samples
        for (f1, v1), (f2, v2) in zip(pre, post):
            assert f1 == pytest.approx(f2)
            assert v2 == pytest.approx(v1 * 2, rel=1e-6)

    def test_toggle_on_no_expression_stays_empty(self, view, qapp):
        """A series WITHOUT an expression gets no pre/post curves even
        with the toggle on (the regular samples list handles it)."""
        s, _n = _keyframed_series("rotation")  # no expression
        opts = GraphOptions(show_post_expression_graph=True)
        view.set_options(opts)
        view.set_series([s])
        view.show()
        qapp.processEvents()
        assert view._series[0].pre_expression_samples == []
        assert view._series[0].post_expression_samples == []

    def test_paint_dual_curves_no_crash(self, view, qapp):
        """Painting with the toggle on + an expression must not throw."""
        s = self._expr_series("value + 10")
        opts = GraphOptions(show_post_expression_graph=True)
        view.set_options(opts)
        view.set_series([s])
        view.show()
        qapp.processEvents()
        view.repaint()  # force a paint

    def test_paint_error_curve_no_crash(self, view, qapp):
        """Painting an expression that errors at runtime must not throw and
        must flag expression_error so the error marker renders.

        Uses 'sqrt(value - 60)' which passes static validation but fails at
        runtime when value < 60 (negative radicand) — with a 0..90..0 ramp
        this spans both regimes."""
        s = self._expr_series("sqrt(value - 60)")
        opts = GraphOptions(show_post_expression_graph=True)
        view.set_options(opts)
        view.set_series([s])
        view.show()
        qapp.processEvents()
        assert view._series[0].expression_error is True
        view.repaint()  # force a paint with the error marker

    def test_toggle_off_after_on_clears_curves(self, view, qapp):
        """Turning the toggle back off clears the pre/post samples."""
        s = self._expr_series("value * 2")
        opts = GraphOptions(show_post_expression_graph=True)
        view.set_options(opts)
        view.set_series([s])
        view.show()
        qapp.processEvents()
        assert view._series[0].post_expression_samples  # populated
        # Turn the toggle off and rebuild.
        view.set_options(GraphOptions(show_post_expression_graph=False))
        view._rebuild_samples()
        assert view._series[0].pre_expression_samples == []
        assert view._series[0].post_expression_samples == []
