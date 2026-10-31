"""M06.2 · CPU（numpy）qualifier keyer 单元测试。

公式与 keyer.glsl 逐条对应（平滑带边界/环形 hue/HDR 亮度/invert/
matte 限定合成）。
"""

from __future__ import annotations

import numpy as np
import pytest

pytestmark = pytest.mark.color_core

from src.features.color.core.qualifier_cpu import (
    apply_qualifier_masked_grade,
    compute_qualifier_matte,
    qualifier_postprocess_active,
    rgb_to_hsl_np,
)


class TestMatte:
    def test_lum_band_hard_edges(self):
        # lum 带 [0.4, 0.9]（软边 0）：带内 1、带外 0
        rgb = np.array([[[0.706, 0.706, 0.706],   # l=0.706 带内
                         [0.098, 0.098, 0.098],   # l=0.098 带外
                         [0.95, 0.95, 0.95]]],    # l=0.95 带外（上界外）
                       dtype=np.float32)
        qp = {"enabled": True, "lum_low": 0.4, "lum_high": 0.9,
              "lum_soft_low": 0.0, "lum_soft_high": 0.0,
              "hue_enabled": False, "sat_enabled": False}
        m = compute_qualifier_matte(rgb, qp)
        assert m[0, 0, 0] == pytest.approx(1.0)
        assert m[0, 1, 0] == pytest.approx(0.0)
        assert m[0, 2, 0] == pytest.approx(0.0)

    def test_lum_soft_edges_monotonic(self):
        # 软边：带边界两侧单调过渡（0→1→0）
        ls = np.linspace(0.0, 1.0, 21, dtype=np.float32)
        rgb = np.stack([np.stack([np.full(3, v, np.float32)] * 1)
                        for v in ls])
        qp = {"enabled": True, "lum_low": 0.4, "lum_high": 0.9,
              "lum_soft_low": 0.1, "lum_soft_high": 0.1,
              "hue_enabled": False, "sat_enabled": False}
        m = compute_qualifier_matte(rgb, qp)[:, 0, 0]
        # 单峰：升→峰→降（带语义），峰值 1，两端 0
        peak = int(np.argmax(m))
        assert np.all(np.diff(m[:peak + 1]) >= -1e-6)
        assert np.all(np.diff(m[peak:]) <= 1e-6)
        assert m.max() == pytest.approx(1.0)
        assert m[0] == pytest.approx(0.0) and m[-1] == pytest.approx(0.0)

    def test_invert(self):
        rgb = np.full((1, 1, 3), 0.5, dtype=np.float32)
        qp = {"enabled": True, "lum_low": 0.0, "lum_high": 1.0,
              "hue_enabled": False, "sat_enabled": False,
              "invert": True}
        m = compute_qualifier_matte(rgb, qp)
        assert m[0, 0, 0] == pytest.approx(0.0)  # 全通带反相 → 全 0

    def test_disabled_returns_none(self):
        assert compute_qualifier_matte(
            np.zeros((2, 2, 3), np.float32), {"enabled": False}) is None

    def test_hsl_luminance_definition(self):
        # l = (max+min)/2（真 HSL，非 HSV value）
        rgb = np.array([[[1.0, 0.0, 0.0]]], dtype=np.float32)
        h, s, l = rgb_to_hsl_np(rgb)
        assert l[0, 0] == pytest.approx(0.5)

    def test_hdr_luminance_scaled(self):
        # max>1：先归一再 l *= max_orig（keyer.glsl:29-45）
        rgb = np.array([[[4.0, 0.0, 0.0]]], dtype=np.float32)
        h, s, l = rgb_to_hsl_np(rgb)
        assert l[0, 0] == pytest.approx(0.5 * 4.0)


class TestMaskedApplication:
    def test_mix_formula(self):
        src = np.full((1, 1, 3), 0.2, np.float32)
        graded = np.full((1, 1, 3), 0.8, np.float32)
        out = apply_qualifier_masked_grade(
            src, graded, np.array([[[0.25]], [[1.0]], [[0.0]]], np.float32))
        assert out[0, 0, 0] == pytest.approx(0.2 * 0.75 + 0.8 * 0.25)
        assert out[1, 0, 0] == pytest.approx(0.8)
        assert out[2, 0, 0] == pytest.approx(0.2)

    def test_postprocess_detection(self):
        assert qualifier_postprocess_active(
            {"enabled": True, "blur": 0.0}) is False
        assert qualifier_postprocess_active(
            {"enabled": True, "blur": 2.0}) is True
        assert qualifier_postprocess_active(
            {"enabled": True, "denoise": 0.6}) is True
