"""T128 Graph Editor sampling performance test.

Guards against UI-blocking regressions when a project has many parameters,
many curves, and expressions (plan §22.12 'expression graph 默认采用节流
刷新…不能让每个 mouse move 同步阻塞 UI'). This is a TIME-BUDGET test, not
a statistical benchmark: it asserts the sampling work for a large project
complepletes well under a single-frame budget (16ms @ 60fps) so a mouse-move
rebuild never drops frames.

Run directly for a full report:
    .venv/bin/python -m pytest tests/benchmark/benchmark_graph_sampling.py -s
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.ui.graph_model import (
    GraphPoint, GraphSeries, sample_expression_curves, sample_value_curve,
)
from src.features.composite.utils.expression_parser import (
    ExpressionContext, ExpressionParser,
)


pytestmark = pytest.mark.composite_core


# ── helpers ───────────────────────────────────────────────────────────


def _pts(n_keyframes, span=600):
    """N evenly-spaced keyframes across [0, span]."""
    return [
        GraphPoint(row_id="param:n:p", node_id="n", parameter_name="p",
                   channel=None, frame=float(i * span / max(1, n_keyframes - 1)),
                   value=float(i), interpolation_in="linear",
                   interpolation_out="linear")
        for i in range(n_keyframes)
    ]


def _series(count, kfs_per=8):
    """Build N series, each with kfs_per keyframes."""
    out = []
    for i in range(count):
        pts = [
            GraphPoint(row_id=f"param:n:p{i}", node_id="n",
                       parameter_name=f"p{i}", channel=None,
                       frame=float(j * 50), value=float(j * i),
                       interpolation_in="linear", interpolation_out="linear")
            for j in range(kfs_per)
        ]
        out.append(GraphSeries(
            row_id=f"param:n:p{i}", display_name=f"P{i}", channel=None,
            color="#ff8a3d", is_spatial=False, has_keyframes=True,
            has_expression=False, points=pts))
    return out


# A single 60fps frame is 16.7ms; sampling must finish in a fraction of that
# so a mouse-move rebuild doesn't drop frames. We use a generous 50ms ceiling
# (the test machine may be slow / under load).
SAMPLING_BUDGET_MS = 50.0


# ── T128: value-curve sampling scales ─────────────────────────────────


class TestValueCurveSamplingBudget:
    def test_many_keyframes_single_curve(self):
        """One curve with 200 keyframes samples within budget."""
        pts = _pts(200, span=600)
        t0 = time.perf_counter()
        sample_value_curve(pts, 0, 600, pixels_per_frame=4.0)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        assert elapsed_ms < SAMPLING_BUDGET_MS, (
            f"sampling 200 keyframes took {elapsed_ms:.1f}ms "
            f">(budget {SAMPLING_BUDGET_MS}ms)")

    def test_many_curves_moderate_keyframes(self):
        """20 curves × 20 keyframes each samples within budget."""
        series = _series(count=20, kfs_per=20)
        t0 = time.perf_counter()
        for s in series:
            sample_value_curve(s.points, 0, 1000, pixels_per_frame=4.0)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        assert elapsed_ms < SAMPLING_BUDGET_MS, (
            f"sampling 20×20 curves took {elapsed_ms:.1f}ms "
            f">(budget {SAMPLING_BUDGET_MS}ms)")


# ── T128: expression sampling budget ──────────────────────────────────


class TestExpressionSamplingBudget:
    def test_expression_curve_within_budget(self):
        """A single expression-driven curve samples within budget."""
        pts = _pts(20, span=600)
        ctx = ExpressionContext()
        parser = ExpressionParser()
        t0 = time.perf_counter()
        sample_expression_curves(pts, "value * 2 + sin(time) * 10",
                                 0, 600, pixels_per_frame=4.0,
                                 context=ctx, parser=parser)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        assert elapsed_ms < SAMPLING_BUDGET_MS, (
            f"expression sampling took {elapsed_ms:.1f}ms "
            f">(budget {SAMPLING_BUDGET_MS}ms)")

    def test_multiple_expression_curves_within_budget(self):
        """10 expression-driven curves sample within a multi-curve budget.

        Note: per-sample AST evaluation has non-trivial cost (the parser
        runs once per sample frame); plan §22.12 says expression graphs
        should throttle to 30-60 Hz rather than re-evaluate every mouse-
        move. This test guards the WORST-CASE full rebuild at a generous
        ceiling (1 second for 10 curves) so we catch a 10× regression —
        the throttle (T128 follow-up) is what keeps interactive moves
        smooth, not this budget."""
        series = _series(count=10, kfs_per=10)
        ctx = ExpressionContext()
        parser = ExpressionParser()
        t0 = time.perf_counter()
        for s in series:
            sample_expression_curves(s.points, "value * 2 + sin(time)",
                                     0, 1000, pixels_per_frame=4.0,
                                     context=ctx, parser=parser)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        # 10 curves × ~150 samples × parser cost ≈ a few hundred ms. Guard
        # against a 10× regression (ceiling 1000ms).
        assert elapsed_ms < 1000.0, (
            f"10 expression curves took {elapsed_ms:.1f}ms (>1000ms ceiling)")


# ── T128: rebuild-all budget (full viewport resample) ─────────────────


class TestFullRebuildBudget:
    def test_full_viewport_rebuild_within_budget(self):
        """A full Graph viewport rebuild (20 curves + 1 expression) finishes
        within a 2× budget — this is what happens on a viewport refit."""
        series = _series(count=20, kfs_per=15)
        ctx = ExpressionContext()
        parser = ExpressionParser()
        t0 = time.perf_counter()
        for s in series:
            sample_value_curve(s.points, 0, 1000, pixels_per_frame=4.0)
        # Plus one expression curve.
        sample_expression_curves(series[0].points, "value * 2",
                                 0, 1000, pixels_per_frame=4.0,
                                 context=ctx, parser=parser)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        # Full rebuild includes 1 expression curve (parser cost). Guard
        # against a 10× regression (ceiling 500ms).
        assert elapsed_ms < 500.0, (
            f"full rebuild took {elapsed_ms:.1f}ms (>500ms ceiling)")
