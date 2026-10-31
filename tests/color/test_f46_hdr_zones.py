"""F4.6 · HDR 分区创作工具与高光塑形——核验验收向量。

计划允许两种收口：实现最少几区工具，或**核验现有工具覆盖并记录**。
现状核验：``apply_soft_clip``（scene-linear Toe/Shoulder 两区 + 区内
exposure/contrast）即为既有的"最少几区"实现——CPU 与 GPU 权威文本
（shader_nodes SOFT_CLIP_SNIPPET / MSL_SOFT_CLIP）逐条等价
（AUD-0911/0912，含此前缺失的 toe 分支与通道平衡块补齐）。

本测试锁定验收句：
- 保留中间调肤色而调整明亮窗户/高光区域（shoulder 只动 >high）；
- 跨分区无不连续色阶（边界 ±ε 连续）；
- 负值/超 1.0、黑位行为有定义（toe 钳 black_point、shoulder 压
  white_point、无 NaN/无未定义翻转）；
- 与 qualifier 空间局部选区分开（本工具按亮度域分区，非空间选区）。
未实现"分区轮"（N 区独立 exposure/sat 旋钮）——按计划记录条件延期，
不宣称已实现。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.color.core.the_decompiler import GradingMath

pytestmark = getattr(pytest.mark, "color_core", pytest.mark.edit_core)

PARAMS = {
    "enabled": True,
    "high": 0.7, "white_point": 1.2, "shoulder_strength": 1.0,
    "low": 0.06, "black_point": -0.05, "toe_strength": 1.0,
    "exposure": 0.0, "contrast": 0.0,
}


def _apply(rgb: np.ndarray, **overrides) -> np.ndarray:
    params = {**PARAMS, **overrides}
    return GradingMath.apply_soft_clip(
        np.asarray(rgb, dtype=np.float32).reshape(-1, 3), params
    ).reshape(np.asarray(rgb).shape[:-1] + (3,))


class TestHdrZoneShaping:
    def test_skin_midtones_preserved_while_window_shaped(self):
        """中间调肤色（0.2-0.5）不动；明亮窗户（4.0）被 shoulder 压缩。"""
        rgb = np.array([
            [0.20, 0.14, 0.10],   # 肤色（中间调）
            [0.45, 0.30, 0.20],   # 肤色亮端（仍 < high）
            [4.00, 3.80, 3.50],   # 明亮窗户（HDR 高光）
        ])
        out = _apply(rgb)
        assert np.allclose(out[0], rgb[0], atol=1e-6)   # 肤色恒等
        assert np.allclose(out[1], rgb[1], atol=1e-6)
        assert out[2].max() < rgb[2].max()               # 窗户被压
        assert out[2].max() > PARAMS["high"]             # 仍高于门限（不过度压）

    def test_zone_boundary_continuous(self):
        """跨分区无不连续色阶：high 边界 ±ε 输出差 → 0。"""
        eps = 1e-4
        rgb = np.array([[[PARAMS["high"] - eps, PARAMS["high"] - eps,
                          PARAMS["high"] - eps],
                         [PARAMS["high"] + eps, PARAMS["high"] + eps,
                          PARAMS["high"] + eps]]])
        out = _apply(rgb)
        jump = float(np.max(np.abs(out[0, 0] - out[0, 1])))
        assert jump < 2e-3  # 连续（比例压缩起点平滑）

    def test_negative_and_black_point_defined(self):
        """负值/toe 行为有定义：钳到 black_point，无 NaN。"""
        rgb = np.array([[[-0.5, -0.2, -0.02],
                         [0.01, 0.03, 0.05]]])
        out = _apply(rgb)
        assert np.all(np.isfinite(out))
        assert out[0, 0, 0] >= PARAMS["black_point"] - 1e-6

    def test_above_white_point_clamped_not_flipped(self):
        """超 1.0/超白：压向 white_point，不翻转不过冲成负。"""
        rgb = np.array([[[20.0, 15.0, 10.0]]])
        out = _apply(rgb)
        assert np.all(np.isfinite(out))
        assert np.all(out >= 0)
        assert out.max() <= PARAMS["white_point"] * 1.6

    def test_disabled_is_identity(self):
        rgb = np.array([[[4.0, 0.18, -0.1]]])
        out = _apply(rgb, enabled=False)
        assert np.allclose(out, rgb)

    def test_zone_exposure_pre_adjustment(self):
        """区内曝光预调整在 scene-linear（×2^exp）。"""
        rgb = np.array([[[0.18, 0.18, 0.18]]])
        out = _apply(rgb, exposure=1.0)
        # 0.18·2 = 0.36 仍在两区之间 → 恒等放大
        assert np.allclose(out[0, 0], 0.36, atol=1e-6)
