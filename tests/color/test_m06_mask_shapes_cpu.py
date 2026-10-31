"""M06.4 · MaskShape 组合 CPU 模型层回归。"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

pytestmark = pytest.mark.color_core

from src.features.color.core.layer import MaskGroup, MaskShape
from src.features.color.core.mask_shapes_cpu import (
    combine_masks_cpu,
    rasterize_shape_cpu,
)

_W, _H = 64, 48


def _circle(cx=0.5, cy=0.5, r=0.4, **kw):
    s = MaskShape(MaskShape.CIRCLE)
    s.center = [cx, cy]
    s.size = [r * 2, r * 2]
    s.feather = 0
    for k, v in kw.items():
        setattr(s, k, v)
    return s


class TestRasterize:
    def test_circle_interior_exterior(self):
        arr, err = rasterize_shape_cpu(_circle(), _W, _H)
        assert err is None
        assert arr[24, 32] == 1.0            # 中心
        assert arr[0, 0] == 0.0              # 远角
        assert arr.dtype == np.float32

    def test_gradient_transitions(self):
        s = MaskShape(MaskShape.GRADIENT)
        s.grad_points = [[0.2, 0.5], [0.8, 0.5]]
        s.softness = 0.2
        arr, err = rasterize_shape_cpu(s, _W, _H)
        assert err is None
        # 过渡带 [start−s/2, end−s/2]（带端全值）；起点左/终点右
        assert arr[24, 4] > 0.9      # t<−s/2 → 1
        assert arr[24, 60] < 0.1     # t> s/2 → 0
        # 单调递减
        row = arr[24]
        assert np.all(np.diff(row) <= 1e-6)

    def test_unsupported_types_reported(self):
        curve = MaskShape(MaskShape.CURVE)
        arr, reason = rasterize_shape_cpu(curve, _W, _H)
        assert arr is None and "QPainterPath" in reason
        bitmap = MaskShape(MaskShape.BITMAP)
        arr2, reason2 = rasterize_shape_cpu(bitmap, _W, _H)
        assert arr2 is None and "bitmap" in reason2

    def test_invert_and_opacity(self):
        # engine 序：先 opacity 后 invert（外部 0→1；内部 1→0.5→0.5）
        s = _circle(invert=True, opacity=0.5)
        arr, _ = rasterize_shape_cpu(s, _W, _H)
        assert arr[0, 0] == pytest.approx(1.0)
        assert arr[24, 32] == pytest.approx(0.5)


class TestCombine:
    def test_add_union(self):
        a = _circle(cx=0.3)
        b = _circle(cx=0.7)
        b.operation = MaskShape.OP_ADD
        mask, report = combine_masks_cpu([a, b], _W, _H)
        assert mask[24, 19] == 1.0 and mask[24, 45] == 1.0
        assert len(report.evaluated) == 2 and not report.unsupported

    def test_subtract_and_intersect(self):
        a = _circle(cx=0.5, r=0.4)
        b = _circle(cx=0.5, r=0.2)
        sub = MaskShape(MaskShape.CIRCLE)
        sub.center, sub.size = [0.5, 0.5], [0.4, 0.4]
        sub.feather = 0
        sub.operation = MaskShape.OP_SUBTRACT
        mask, _ = combine_masks_cpu([a, sub], _W, _H)
        assert mask[24, 32] == 0.0          # 中心被减去
        assert mask[24, 8] == 1.0           # 环形残留
        inter = MaskShape(MaskShape.CIRCLE)
        inter.center, inter.size = [0.5, 0.5], [0.4, 0.4]
        inter.feather = 0
        inter.operation = MaskShape.OP_INTERSECT
        mask2, _ = combine_masks_cpu([a, inter], _W, _H)
        assert mask2[24, 32] == 1.0 and mask2[24, 8] == 0.0

    def test_group_recursive_opacity_invert(self):
        group = MaskGroup("G")
        inner = _circle()
        group.add_child(inner)
        group.opacity = 0.5
        mask, report = combine_masks_cpu([group], _W, _H)
        assert mask[24, 32] == pytest.approx(0.5)
        group.invert = True
        mask2, _ = combine_masks_cpu([group], _W, _H)
        # 组级：先 opacity 后 invert（engine 同序）
        assert mask2[0, 0] == pytest.approx(1.0)   # 外部 0→1
        assert mask2[24, 32] == pytest.approx(0.5)  # 内部 0.5→0.5

    def test_unsupported_shape_skipped_not_silent(self):
        good = _circle()
        curve = MaskShape(MaskShape.CURVE)
        mask, report = combine_masks_cpu([good, curve], _W, _H)
        assert mask[24, 32] == 1.0             # 可评估形状照常
        assert len(report.unsupported) == 1
        assert report.unsupported[0][1] == MaskShape.CURVE
        assert "QPainterPath" in report.unsupported[0][2]

    def test_clean_white_dilates_edge(self):
        # clean_white（膨胀）使边缘外扩：外部邻近像素被拉入
        base, _ = combine_masks_cpu(
            [_circle(r=0.2, clean_white=0.0)], _W, _H)
        grown, _ = combine_masks_cpu(
            [_circle(r=0.2, clean_white=2.0)], _W, _H)
        assert float(np.mean(grown)) > float(np.mean(base))
