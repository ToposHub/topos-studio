"""F5.1 · 显示编码合同——唯一 OETF 真值 = color_transform_provider。

三路语义（详见 docs/validation/feature_F/F5.1 ledger）：
  Composite viewer = 线性工作 + ODT（provider/LUT 真值链）；
  Edit/Color 视频预览 = 编码信号直通（comp_clip/浮点平面/常量帧的 CPU
  回退经 display_encode 单点收口到 provider）；
  本文件冻结：(a) provider sRGB/BT.709 锚点向量；(b) 各 GPU GLSL 拷贝与
  provider 的 sRGB 常数四元组一致（源文本合同，改常数即红）。
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

from src.shared.color.color_space_descriptor import LINEAR_SCENE, SRGB_IMAGE
from src.shared.color.color_transform_provider import transform_color
from src.shared.color.display_encode import (
    linear_bgr_to_srgb_display_u8,
    linear_to_srgb_display,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _oracle_srgb(lin: np.ndarray) -> np.ndarray:
    """独立 oracle：IEC 61966-2-1 分段 OETF（与实现无关地重写）。"""
    lin = np.clip(np.asarray(lin, dtype=np.float64), 0.0, 1.0)
    return np.where(
        lin <= 0.0031308,
        lin * 12.92,
        1.055 * np.power(lin, 1.0 / 2.4) - 0.055,
    )


class TestProviderSrgbAnchors:
    @pytest.mark.parametrize("lin,expected", [
        (0.0, 0.0),
        (0.0031308, 0.0031308 * 12.92),          # 拐点（两侧连续）
        (0.5, 0.7353580594062805),
        (1.0, 1.0),
        (2.5, 1.0),                               # >1 HDR 高光硬剪（现状语义）
    ])
    def test_srgb_anchor_vectors(self, lin: float, expected: float) -> None:
        got = transform_color(
            np.array([[[lin, lin, lin]]], dtype=np.float32),
            LINEAR_SCENE, SRGB_IMAGE)[0, 0, 0]
        assert got == pytest.approx(expected, abs=1e-6)

    def test_knee_continuity(self) -> None:
        """拐点两侧连续（<= 与 < 实现差异 ~1e-9 量级）。"""
        eps = 1e-9
        lo = _oracle_srgb(np.array([0.0031308 - eps]))[0]
        hi = _oracle_srgb(np.array([0.0031308 + eps]))[0]
        assert abs(hi - lo) < 1e-6


class TestDisplayEncodeWrapper:
    def test_wrapper_matches_oracle(self) -> None:
        samples = np.array(
            [0.0, 0.001, 0.0031308, 0.01, 0.18, 0.5, 0.9, 1.0, 1.7, -0.2],
            dtype=np.float32)
        triples = np.repeat(samples[:, None], 3, axis=1)  # (n,3)
        got = linear_to_srgb_display(triples)
        expected = _oracle_srgb(triples)
        np.testing.assert_allclose(got, expected, atol=2e-6)

    def test_bgr_channel_order_preserved(self) -> None:
        bgr = np.array([[[0.2, 0.5, 0.8]]], dtype=np.float32)
        out = linear_bgr_to_srgb_display_u8(bgr)
        oracle = _oracle_srgb(np.array([0.2, 0.5, 0.8]))
        np.testing.assert_allclose(
            out[0, 0], np.round(oracle * 255.0).astype(np.uint8), atol=1)

    def test_color_triple_1d_and_shape_contract(self) -> None:
        """1-D 颜色三元组逐元素编码；非法形状显式拒绝（不静默错切）。"""
        triple = np.array([0.0, 0.5, 1.0], dtype=np.float32)
        np.testing.assert_allclose(
            linear_to_srgb_display(triple),
            [0.0, 0.7353581, 1.0], atol=2e-6)
        with pytest.raises(ValueError):
            linear_to_srgb_display(np.zeros(10, dtype=np.float32))

    def test_full_pipeline_agrees_on_3d(self) -> None:
        """完整 transform_color 管线（3D 形状）与传输函数一致——
        provider 选择（OCIO/Builtin）不改变显示编码结果。"""
        img = np.linspace(0.0, 1.0, 24, dtype=np.float32).reshape(2, 4, 3)
        via_pipeline = transform_color(
            img.copy(), LINEAR_SCENE, SRGB_IMAGE)
        via_direct = linear_to_srgb_display(img)
        # 容差对齐 provider parity 冻结值（OCIO LUT 网格 vs Builtin 解析式）
        np.testing.assert_allclose(via_pipeline, via_direct, atol=1.5e-3)

    def test_u8_output_shape_and_range(self) -> None:
        out = linear_bgr_to_srgb_display_u8(
            np.random.default_rng(7).random((4, 6, 3)).astype(np.float32) * 2.0)
        assert out.shape == (4, 6, 3) and out.dtype == np.uint8
        assert out.max() <= 255 and out.min() >= 0


class TestGlslConstantContract:
    """GPU 各 GLSL sRGB 拷贝必须与 provider 常数四元组一致（源文本合同）。"""

    GLSL_SOURCES = [
        "src/features/composite/ui/composite_viewport.py",
        "src/features/composite/core/shaders.py",
        "src/features/color/shaders/shaders.py",
        "src/shared/media/color_decode.py",
    ]

    @pytest.mark.parametrize("rel", GLSL_SOURCES)
    def test_srgb_constant_quartet_present(self, rel: str) -> None:
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        normalized = re.sub(r"\s+", "", source)
        # 阈值 / 线性段斜率 / 幂段系数 三常数为无歧义精确匹配
        for constant in ("0.0031308", "12.92", "1.055"):
            assert constant in normalized, f"{rel} 缺 sRGB 常数 {constant}"
        # 幂指数 1/2.4（允许 1.0/2.4、1/2.4、1.0/2.4 之外的等价写法）
        assert re.search(r"1(?:\.0)?/2\.4", normalized), \
            f"{rel} 缺 sRGB 幂指数 1/2.4"
