"""M05.2c · HSL 曲线/soft-clip 的 CPU 图像实现回归。"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

pytestmark = pytest.mark.color_core

from src.features.color.core.advanced_grade_cpu import (
    apply_hsl_curves_image,
    apply_soft_clip_image,
)


def _identity_hsl_lut():
    """恒等 HSL LUT：Sat/Lum 乘子全 1，Hue shift 全 0。"""
    lut = np.ones((6, 256), dtype=np.float32)
    lut[1] = 0.0  # HueHue shift = 0
    return lut


class TestHslCurvesImage:
    def test_identity_lut_noop(self):
        rgb = np.random.default_rng(3).random((4, 6, 3)).astype(np.float32)
        out = apply_hsl_curves_image(rgb, _identity_hsl_lut())
        assert np.allclose(out, rgb, atol=1e-6)

    def test_sat_boost_changes_saturation(self):
        lut = _identity_hsl_lut()
        lut[0] = 1.5  # LumSat → 全局饱和 ×1.5
        rgb = np.array([[[0.5, 0.3, 0.2]]], dtype=np.float32)
        out = apply_hsl_curves_image(rgb, lut)
        # 饱和度上升（通道差增大；逐元素取均值避免数组转标量弃用）
        assert float((out[..., 0] - out[..., 2]).mean()) > float(
            (rgb[..., 0] - rgb[..., 2]).mean())

    def test_invalid_lut_passthrough(self):
        rgb = np.full((2, 2, 3), 0.5, np.float32)
        assert apply_hsl_curves_image(rgb, None) is rgb
        bad = np.ones((3, 256), np.float32)
        assert apply_hsl_curves_image(rgb, bad) is rgb


class TestSoftClipImage:
    def test_over_white_softened_not_clipped(self):
        rgb = np.array([[[2.0, 1.8, 0.2]]], dtype=np.float32)
        params = {"high": 1.0, "white_point": 1.1,
                  "shoulder_strength": 1.0, "low": 0.0,
                  "black_point": -0.1, "toe_strength": 1.0,
                  "exposure": 0.0, "contrast": 0.0}
        out = apply_soft_clip_image(rgb, params)
        # 超 high 的值被软压缩但不超过 white_point，比例保持
        assert out[..., 0].max() <= 1.1 + 1e-6
        assert out[..., 0].max() < 2.0  # 确实压缩了

    def test_toe_lifts_shadows(self):
        rgb = np.array([[[-0.05, -0.04, -0.06]]], dtype=np.float32)
        params = {"high": 1.0, "white_point": 1.1,
                  "shoulder_strength": 1.0, "low": 0.0,
                  "black_point": -0.1, "toe_strength": 1.0,
                  "exposure": 0.0, "contrast": 0.0}
        out = apply_soft_clip_image(rgb, params)
        # toe 把深黑抬升到黑点以上
        assert float(out[..., 2].min()) > -0.1

    def test_identity_params_near_noop(self):
        rgb = np.array([[[0.5, 0.3, 0.2]]], dtype=np.float32)
        params = {"high": 1.0, "white_point": 1.1,
                  "shoulder_strength": 1.0, "low": 0.0,
                  "black_point": -0.1, "toe_strength": 1.0,
                  "exposure": 0.0, "contrast": 0.0}
        out = apply_soft_clip_image(rgb, params)
        assert np.allclose(out, rgb, atol=1e-5)


# ================================================================
# M05.2 · 3D LUT 文件 CPU 加载消费（apply_lut_file_to_color 接缝）
# ================================================================

def _write_inversion_cube(path) -> str:
    """size-2 反转立方（对角变换，与 .cube 行序无关）。"""
    lines = ["TITLE \"invert\"", "LUT_3D_SIZE 2"]
    for b in (0.0, 1.0):
        for g in (0.0, 1.0):
            for r in (0.0, 1.0):
                lines.append(f"{1.0 - r:.1f} {1.0 - g:.1f} {1.0 - b:.1f}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


class TestApplyLutFileToColor:
    def test_inversion_cube_maps_gray(self, tmp_path):
        from src.features.color.core.lut_exporter import (
            apply_lut_file_to_color,
        )
        lut = _write_inversion_cube(tmp_path / "invert.cube")
        rgb = np.full((4, 4, 3), 0.25, np.float32)
        out = apply_lut_file_to_color(rgb, lut)
        assert out is not None
        # 三线性：0.75*inv(0) + 0.25*inv(1) = 0.75
        assert np.allclose(out, 0.75, atol=1e-5)

    def test_identity_corners_exact(self, tmp_path):
        from src.features.color.core.lut_exporter import (
            apply_lut_file_to_color,
        )
        lines = ["LUT_3D_SIZE 2"]
        for b in (0.0, 1.0):
            for g in (0.0, 1.0):
                for r in (0.0, 1.0):
                    lines.append(f"{r:.1f} {g:.1f} {b:.1f}")
        lut = tmp_path / "identity.cube"
        lut.write_text("\n".join(lines) + "\n", encoding="utf-8")
        corners = np.array([[[0.0, 0.0, 0.0], [1.0, 0.0, 1.0]],
                            [[0.25, 0.5, 0.75], [1.0, 1.0, 1.0]]],
                           dtype=np.float32)
        out = apply_lut_file_to_color(corners, str(lut))
        assert np.allclose(out, corners, atol=1e-5)

    def test_missing_file_returns_none(self, tmp_path):
        from src.features.color.core.lut_exporter import (
            apply_lut_file_to_color,
        )
        rgb = np.zeros((1, 1, 3), np.float32)
        # None 交还调用方决定交付语义（成片 fail-fast / 预览告警跳过）
        assert apply_lut_file_to_color(
            rgb, str(tmp_path / "gone.cube")) is None

    def test_corrupt_file_returns_none(self, tmp_path):
        from src.features.color.core.lut_exporter import (
            apply_lut_file_to_color,
        )
        bad = tmp_path / "bad.cube"
        bad.write_text("LUT_3D_SIZE 2\nnot a lut row\n", encoding="utf-8")
        assert apply_lut_file_to_color(
            np.zeros((1, 1, 3), np.float32), str(bad)) is None
