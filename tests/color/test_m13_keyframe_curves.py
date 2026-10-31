"""M13.3 · 关键帧导航与 Bezier 曲线插值回归。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

pytestmark = pytest.mark.color_core

from src.features.color.core.grade_keyframes import GradeKeyframeTrack
from src.features.color.core.keyframe_curves import (
    bezier_t_for_x,
    bezier_value,
    evaluate_bezier_segment,
    next_keyframe_frame,
)


class TestKeyframeNavigation:
    def _track(self):
        t = GradeKeyframeTrack("exposure")
        for f in (0, 24, 48, 96):
            t.set_keyframe(f, float(f))
        return t

    def test_next_prev_strict(self):
        t = self._track()
        assert next_keyframe_frame(t, 30) == 48
        assert next_keyframe_frame(t, 30, direction="prev") == 24
        assert next_keyframe_frame(t, 48) == 96
        assert next_keyframe_frame(t, 48, direction="prev") == 24

    def test_ends_none(self):
        t = self._track()
        assert next_keyframe_frame(t, 200) is None
        assert next_keyframe_frame(t, -5, direction="prev") is None

    def test_empty_track(self):
        t = GradeKeyframeTrack("x")
        assert next_keyframe_frame(t, 10) is None


class TestBezierCurves:
    SEG = {"x0": 0.0, "y0": 0.0, "x1": 0.3, "y1": 0.0,
           "x2": 0.7, "y2": 1.0, "x3": 1.0, "y3": 1.0}

    def test_endpoints_exact(self):
        v = evaluate_bezier_segment(self.SEG, 0.0)
        assert v == pytest.approx(0.0)
        v1 = evaluate_bezier_segment(self.SEG, 1.0)
        assert v1 == pytest.approx(1.0)

    def test_monotone_ease(self):
        vals = [evaluate_bezier_segment(self.SEG, t / 10)
                for t in range(11)]
        assert all(b >= a - 1e-9 for a, b in zip(vals, vals[1:]))

    def test_out_of_segment_none(self):
        assert evaluate_bezier_segment(self.SEG, -0.1) is None
        assert evaluate_bezier_segment(self.SEG, 1.1) is None

    def test_bezier_value_symmetric(self):
        # 三次贝塞尔 t=0.5 权重固定 1/8,3/8,3/8,1/8：
        # y=(0*1/8 + 0*3/8 + 1*3/8 + 1*1/8)=0.5
        v = bezier_value(0.0, 0.0, 1.0, 1.0, 0.5)
        assert v == pytest.approx(0.5)
        v2 = bezier_value(0.0, 0.0, 0.0, 1.0, 0.5)
        assert v2 == pytest.approx(0.125)

    def test_t_for_x_roundtrip(self):
        t = bezier_t_for_x(0.0, 0.3, 0.7, 1.0, 0.5)
        assert 0.0 <= t <= 1.0
        x_t = ((1 - t) ** 3 * 0.0 + 3 * (1 - t) ** 2 * t * 0.3
               + 3 * (1 - t) * t * t * 0.7 + t ** 3 * 1.0)
        assert x_t == pytest.approx(0.5, abs=1e-6)
