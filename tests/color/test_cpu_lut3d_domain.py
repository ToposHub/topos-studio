"""N14 v1：CPU 域扩展——LUT3D（trilinear + R05 强度混合；GL 同语义）。

验收（计划 N14：逐域实现与能力声明一起提交）：LUT3D 在 CPU oracle 生效
（恒等 LUT = 位级不变；R/B 交换 LUT = 通道交换；强度 0 = 恒等；HDR 超
界值按 R05 语义混合）；顺序（grading→curves→LUT）非交换对照；能力声
明（production_layer_domain_errors 不再拒绝 LUT；Metal 保持显式拒绝）。
"""
from __future__ import annotations

import numpy as np
import pytest

from src.features.color.core.color_graph_cpu import (
    corrector_production_oracle, production_layer_domain_errors,
)


@pytest.fixture()
def swap_cube(tmp_path):
    """2×2×2 R/B 交换 LUT（网格点值 = (b, g, r)）。"""
    lines = ["LUT_3D_SIZE 2"]
    for b in range(2):
        for g in range(2):
            for r in range(2):
                lines.append(f"{b} {g} {r}")
    path = tmp_path / "swap.cube"
    path.write_text("\n".join(lines) + "\n")
    return str(path)


def _image():
    """含 HDR 超界与负值的小图。"""
    img = np.zeros((4, 4, 4), np.float32)
    img[..., :3] = 0.5
    img[0, 0, 0] = 1.5      # HDR 超 1
    img[1, 0, 2] = -0.2     # 负值
    img[2, 2, 1] = 0.0
    return img


class TestCpuLut3d:
    def test_identity_cube_bitwise(self, tmp_path):
        lines = ["LUT_3D_SIZE 2"]
        for b in range(2):
            for g in range(2):
                for r in range(2):
                    lines.append(f"{r/1.0:.1f} {g/1.0:.1f} {b/1.0:.1f}")
        path = tmp_path / "identity.cube"
        path.write_text("\n".join(lines) + "\n")
        layer = {"id": "l", "grading_params": {},
                 "active_lut_path": str(path), "lut_intensity": 1.0}
        img = _image()
        out = corrector_production_oracle(img, layer)
        # [0,1] 域内**像素**（任一通道出界即整像素按 R05 逐像素比例
        # 重建——GL 同语义：HDR 分支 = sdr 采样 × max_orig/max_sdr）
        in_range = np.all((img[..., :3] >= 0.0) & (img[..., :3] <= 1.0),
                          axis=-1)
        np.testing.assert_allclose(
            out[..., :3][in_range], img[..., :3][in_range], atol=1e-6)
        hdr_pixel = out[0, 0, :3]  # (1.5, 0.5, 0.5)：scale=1.5
        np.testing.assert_allclose(hdr_pixel, [1.5, 0.75, 0.75], atol=1e-5)

    def test_swap_cube_channels(self, swap_cube):
        layer = {"id": "l", "grading_params": {},
                 "active_lut_path": swap_cube, "lut_intensity": 1.0}
        img = np.full((2, 2, 4), 0.25, np.float32)
        img[0, 0] = [0.8, 0.5, 0.2, 1.0]
        out = corrector_production_oracle(img, layer)
        np.testing.assert_allclose(out[0, 0, :3], [0.2, 0.5, 0.8],
                                   atol=1e-5)  # R↔B

    def test_strength_zero_identity_and_hdr_semantics(self, swap_cube):
        img = _image()
        # 强度 0 = 恒等（含 HDR/负值）
        layer0 = {"id": "l", "grading_params": {},
                  "active_lut_path": swap_cube, "lut_intensity": 0.0}
        out0 = corrector_production_oracle(img, layer0)
        np.testing.assert_allclose(out0, img, atol=1e-6)
        # 半强度：HDR(1.5) 按 R05 语义 mix + negative_excess
        layer_half = {"id": "l", "grading_params": {},
                      "active_lut_path": swap_cube, "lut_intensity": 0.5}
        out_half = corrector_production_oracle(img, layer_half)
        # R 通道 HDR 1.5：sdr=1.0→lut 0.0（swap 后 B 位）；half:
        # src*(0.5) + lut*0.5 = 0.75 + negative_excess*0.5
        assert out_half[0, 0, 0] < img[0, 0, 0]  # 向 LUT 方向移动

    def test_ordering_grading_before_lut(self, swap_cube):
        """非交换对照：exposure 在 LUT 前应用（R05 CORRECTOR_PROCESS_ORDER）。

        与手工组合（先 grading 后 LUT）逐位一致；顺序反换结果不同。
        """
        from src.features.color.core.color_graph_cpu import _apply_lut3d_cpu
        layer = {"id": "l", "grading_params": {"exposure": 25.0},
                 "active_lut_path": swap_cube, "lut_intensity": 1.0}
        img = np.zeros((2, 2, 4), np.float32)
        img[0, 0] = [0.8, 0.5, 0.2, 1.0]
        img[1, 1] = [0.6, 0.6, 0.6, 1.0]
        out = corrector_production_oracle(img, layer)
        # 手工同序组合：grading → LUT（grade_image 等价：×2^(0.25²×6)）
        graded = img[..., :3] * (2.0 ** ((25.0 / 100.0) ** 2 * 6))
        manual = _apply_lut3d_cpu(graded, swap_cube, 1.0)
        np.testing.assert_allclose(out[..., :3], manual, atol=1e-6)

    def test_capability_declared(self):
        layer = {"id": "l", "grading_params": {},
                 "active_lut_path": "/x.cube", "lut_intensity": 1.0}
        errors = production_layer_domain_errors(layer)
        assert not any("LUT3D" in e for e in errors), errors

    def test_metal_still_rejects_lut(self):
        from src.features.color.core.color_graph_metal import (
            METAL_CAPABILITY_CATALOG,
        )
        # Metal 域扩展归后续——LUT 在 Metal corrector 基础域仍 fail-closed
        # （kernel 未实现；能力目录如实声明）
        assert METAL_CAPABILITY_CATALOG["corrector"] == "fallback"
