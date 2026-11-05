"""N14-B：CPU 高级域接线（HSL 曲线 / soft_clip / curves_soft_clip /
非 Normal blend）。

- 中性恒等：默认参数（soft_clip.enabled=False、curves_soft_clip 默认）
  不改变像素（GL 编译期门控同语义）；
- 各域非中性生效（方向性/数值断言）；
- Metal / pointwise 后端维持 fail-closed（逐域恢复，不一次性放开）。
"""
from __future__ import annotations

import numpy as np
import pytest

from src.features.color.core.color_graph_cpu import (
    corrector_production_oracle, production_layer_domain_errors,
)
from src.features.color.core.curves_soft_clip import default_curves_soft_clip


def _ramp(size: int = 32) -> np.ndarray:
    x = np.linspace(0.05, 0.95, size, dtype=np.float32)
    img = np.zeros((size, size, 4), dtype=np.float32)
    img[..., 0] = x[None, :]
    img[..., 1] = 0.5
    img[..., 2] = 1.0 - x[None, :]
    img[..., 3] = 1.0
    return img


def test_neutral_defaults_are_identity():
    """默认 soft_clip_params（enabled=False）与默认 curves_soft_clip
    恒等——不判 enabled 会破坏中性层（本轮修复的回归锚）。"""
    image = _ramp()
    layer = {"grading_params": {},
             "soft_clip_params": {"enabled": False, "high": 1.0},
             "curves_soft_clip": default_curves_soft_clip(),
             "blend_mode": "Normal"}
    out = corrector_production_oracle(image, layer)
    np.testing.assert_allclose(out, image, atol=1e-6)


def test_soft_clip_applies_when_enabled():
    image = _ramp()
    enabled = {"enabled": True, "high": 0.9, "white_point": 1.05,
               "shoulder_strength": 0.2, "low": 0.0, "black_point": 0.0,
               "toe_strength": 0.0, "exposure": 0.0, "contrast": 0.0}
    layer = {"grading_params": {}, "soft_clip_params": enabled}
    out = corrector_production_oracle(image, layer)
    # 超白 shoulder 生效：高亮通道被压（不再等于原值）
    highlights = image[..., 0] > 0.9  # shoulder 起点（high=0.9）以上
    assert highlights.any()
    assert (out[..., 0][highlights] < image[..., 0][highlights]).all()


def test_curves_soft_clip_applies_non_default():
    image = _ramp()
    csc = default_curves_soft_clip()
    csc = dict(csc)
    csc["channels"] = {ch: {"low_clip": 0.05, "low_soft": 0.1,
                            "high_clip": 0.9, "high_soft": 0.1}
                       for ch in "RGB"}
    layer = {"grading_params": {}, "curves_soft_clip": csc}
    out = corrector_production_oracle(image, layer)
    assert np.abs(out[..., :3] - image[..., :3]).max() > 1e-3


def test_hsl_curves_applied_from_data():
    """hsl_curve_data（未预构建 LUT）在 CPU oracle 内重建并应用。"""
    image = _ramp()
    hsl_data = {"sat_sat": {"points": [(0, 0), (0.5, 0.4), (1, 1)]}}
    layer = {"grading_params": {}, "hsl_curve_data": hsl_data}
    out = corrector_production_oracle(image, layer)
    # SatSat 曲线在中间调压饱和：中低饱和像素（G=0.5 常量）应向灰靠拢
    mid = image[..., 1]
    assert np.abs(out[..., 1][mid == 0.5] - 0.5).max() > 0 or \
        np.abs(out[..., :3] - image[..., :3]).max() > 1e-4


def test_non_normal_blend_epilogue():
    image = _ramp()
    layer = {"grading_params": {"exposure": 1.0},
             "blend_mode": "Multiply"}
    out = corrector_production_oracle(image, layer)
    normal = corrector_production_oracle(image, {
        "grading_params": {"exposure": 1.0}, "blend_mode": "Normal"})
    assert np.abs(out - normal).max() > 1e-3
    # Multiply 不超过源与调色结果的乘积（方向性：更暗）
    assert out[..., :3].max() <= normal[..., :3].max() + 1e-6


def test_metal_and_pointwise_still_reject_new_domains():
    """Metal kernel 参数位未扩、LUT 导出点态语义：新四域维持拒绝。"""
    layer_hsl = {"grading_params": {},
                 "hsl_curve_data": {"sat_sat": {"points": [(0, 0), (1, 1)]}}}
    layer_sc = {"grading_params": {},
                "soft_clip_params": {"enabled": True}}
    layer_csc = {"grading_params": {},
                 "curves_soft_clip": {"low_clip": 0.05}}
    layer_blend = {"grading_params": {}, "blend_mode": "Screen"}
    for layer, marker in ((layer_hsl, "HSL"), (layer_sc, "soft_clip_params"),
                          (layer_csc, "curves_soft_clip"),
                          (layer_blend, "blend_mode")):
        assert production_layer_domain_errors(layer, backend="metal"), marker
        assert production_layer_domain_errors(layer,
                                              backend="pointwise"), marker
        # CPU 默认放行
        assert not production_layer_domain_errors(layer), marker
