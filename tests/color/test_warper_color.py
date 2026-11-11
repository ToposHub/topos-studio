# -*- coding: utf-8 -*-
"""CW-B03:色彩空间/坐标/HDR 的 CPU 参考规范测试。

冻结 hue 原点与方向、六扇区、环绕、零色度政策、HSP 常数、HDR 规则与
legacy apply 语义。fixture 覆盖计划要求的六主/次色、灰阶、黑、负通道、
RGB>1、极大/极小有限值、近零色度。
"""
from __future__ import annotations

import numpy as np
import pytest

from src.features.color.core.warper_color import (
    HSP_WEIGHTS,
    apply_warper_legacy_hsv,
    hsl_to_rgb,
    hsp_to_rgb,
    hsv_to_rgb,
    rgb_to_hsl,
    rgb_to_hsp,
    rgb_to_hsv,
    sample_warper_bilinear,
)

#: B03 冻结 fixture(计划 §4 CW-B03 验证清单;golden 由本测试锁定)。
COLORS = [
    (1.0, 0.0, 0.0), (1.0, 1.0, 0.0), (0.0, 1.0, 0.0), (0.0, 1.0, 1.0),
    (0.0, 0.0, 1.0), (1.0, 0.0, 1.0),                       # 六扇区边界
    (1.0, 0.5, 0.0), (0.5, 1.0, 0.0), (0.0, 1.0, 0.5),
    (0.0, 0.5, 1.0), (0.5, 0.0, 1.0), (1.0, 0.0, 0.5),       # 六次色中点
    (0.0, 0.0, 0.0), (0.25, 0.25, 0.25), (0.5, 0.5, 0.5),
    (0.75, 0.75, 0.75), (1.0, 1.0, 1.0),                     # 灰阶与黑/白
    (-0.5, 0.0, 0.2), (0.3, -1.0, 0.0), (0.0, 0.0, -2.0),    # 负通道
    (2.0, 1.5, 0.5), (10.0, 10.0, 10.0), (100.0, 0.01, -3.0),  # HDR/极值
    (1e-8, 0.0, 0.0), (0.5, 0.5, 0.5 + 1e-6),                # 极小/近零色度
    (0.2, 0.4, 0.1), (0.8, 0.6, 0.3), (0.1, 0.2, 0.5),       # 一般色
]

SECTOR_HUES = {
    (1.0, 0.0, 0.0): 0.0, (1.0, 1.0, 0.0): 1 / 6, (0.0, 1.0, 0.0): 1 / 3,
    (0.0, 1.0, 1.0): 0.5, (0.0, 0.0, 1.0): 2 / 3, (1.0, 0.0, 1.0): 5 / 6,
}


# ---- 坐标与扇区 ----

@pytest.mark.parametrize("color,hue", sorted(SECTOR_HUES.items()))
def test_hue_origin_and_direction_frozen(color, hue) -> None:
    hsv = rgb_to_hsv(color)
    # 容差 = GLSL 分母防护 e=1e-10 的固有失真量级(镜像而非重定义)
    assert hsv[0] == pytest.approx(hue, abs=1e-10)
    assert hsv[1] == pytest.approx(1.0)
    assert hsv[2] == pytest.approx(1.0)


def test_hue_wraparound_both_sides_of_seam() -> None:
    left = rgb_to_hsv((1.0, 0.001, 0.0))[0]     # 黄侧贴近 0
    right = rgb_to_hsv((1.0, 0.0, 0.001))[0]    # 洋红侧贴近 1
    assert left < 0.01 and right > 0.99
    # 环绕距离:两侧应彼此相邻(圆周距离 < 0.01)
    assert min(abs(left - right), 1 - abs(left - right)) < 0.01


def test_zero_chroma_policy() -> None:
    for gray in ((0.0, 0.0, 0.0), (0.5, 0.5, 0.5), (1.0, 1.0, 1.0), (10.0, 10.0, 10.0)):
        hsv = rgb_to_hsv(gray)
        assert hsv[0] == 0.0 and hsv[1] == 0.0  # 定义值,非噪声
    near = rgb_to_hsv((0.5, 0.5, 0.5 + 1e-6))
    assert near[1] < 1e-5  # 近零色度不产生大幅色相


# ---- round-trip(预先声明阈值) ----

@pytest.mark.parametrize("space", ["hsv", "hsl", "hsp"])
def test_round_trip_within_declared_threshold(space: str) -> None:
    for color in COLORS:
        color = np.array(color)
        # 三个空间的转换域均为非负幅度(与 GLSL abs(color) 后一致);
        # 负通道由符号包装覆盖(见 sign/zero-channel 测试)。
        # HSL 额外声明上限域:mx+mn>2(超高亮 HDR)无连续定义,显式拒绝。
        magnitude = np.abs(color)
        if (space == "hsl" and magnitude.max() + magnitude.min() >= 2.0
                and magnitude.max() != magnitude.min()):
            # 非灰的超亮(L≥1)才拒绝;纯灰走早退路径,任意幅度合法
            with pytest.raises(ValueError, match="HSL 域"):
                rgb_to_hsl(magnitude)
            continue
        if space == "hsv":
            back = np.sign(color) * hsv_to_rgb(rgb_to_hsv(magnitude))
        elif space == "hsl":
            back = np.sign(color) * hsl_to_rgb(rgb_to_hsl(magnitude))
        else:
            back = np.sign(color) * hsp_to_rgb(rgb_to_hsp(magnitude))
        np.testing.assert_allclose(back, color, rtol=1e-9, atol=1e-9,
                                   err_msg=f"{space} round-trip {color}")


def test_raw_conversions_declare_nonnegative_domain() -> None:
    with pytest.raises(ValueError):
        rgb_to_hsv((-0.5, 0.0, 0.2))
    with pytest.raises(ValueError):
        rgb_to_hsl((-0.5, 0.0, 0.2))
    # 包装路径不受影响:apply 内部 sign/abs(HSP 同域声明)
    with pytest.raises(ValueError):
        rgb_to_hsp((-0.5, 0.0, 0.2))
    apply_warper_legacy_hsv((-0.5, 0.0, 0.2), np.zeros((6, 6, 2)))


def test_hsp_weights_and_homogeneity_frozen() -> None:
    assert HSP_WEIGHTS == (0.299, 0.587, 0.114)
    base = rgb_to_hsp((0.2, 0.4, 0.1))[2]
    doubled = rgb_to_hsp((0.4, 0.8, 0.2))[2]
    assert doubled == pytest.approx(2 * base, rel=1e-12)  # 平方和齐次性
    # 灰的 P = 灰值(权重和为 1)
    assert rgb_to_hsp((0.5, 0.5, 0.5))[2] == pytest.approx(0.5, abs=1e-12)


# ---- HDR 政策 ----

def test_hdr_not_clamped_by_conversions() -> None:
    hsv = rgb_to_hsv((2.0, 1.5, 0.5))
    assert hsv[2] == pytest.approx(2.0)  # 保留 >1
    back = hsv_to_rgb(hsv)
    np.testing.assert_allclose(back, (2.0, 1.5, 0.5), rtol=1e-9)


def test_negative_channels_sign_preserved() -> None:
    for color in ((-0.5, 0.0, 0.2), (0.3, -1.0, 0.0), (0.0, 0.0, -2.0)):
        result = apply_warper_legacy_hsv(color, np.zeros((6, 6, 2)))
        # 恒等网格不改图(容差=GLSL epsilon 失真量级)
        np.testing.assert_allclose(result, color, rtol=1e-9, atol=1e-9)
        sign = np.sign(result)
        assert (sign == np.sign(color)).all() or result.max() == result.min() == 0


def test_non_finite_rejected() -> None:
    with pytest.raises(ValueError):
        rgb_to_hsv((np.nan, 0.0, 0.0))
    with pytest.raises(ValueError):
        apply_warper_legacy_hsv((np.inf, 0.0, 0.0), np.zeros((6, 6, 2)))


# ---- legacy apply 语义 ----

def _grid(rows: int, cols: int, dh: float = 0.0, ds: float = 0.0) -> np.ndarray:
    grid = np.zeros((rows, cols, 2))
    grid[:, :, 0] = dh
    grid[:, :, 1] = ds
    return grid


def test_identity_grid_is_identity_for_all_fixtures() -> None:
    grid = _grid(6, 6)
    for color in COLORS:
        np.testing.assert_allclose(
            apply_warper_legacy_hsv(color, grid), color, rtol=1e-9, atol=1e-9)


def test_low_saturation_bypass_and_fade() -> None:
    grid = _grid(6, 6, dh=0.3)
    # sat < 0.02 → 原样返回
    gray = apply_warper_legacy_hsv((0.5, 0.5, 0.5), grid)
    np.testing.assert_array_equal(gray, (0.5, 0.5, 0.5))
    # sat 0.03:效果存在但被 smoothstep 衰减到远小于全量
    hsv = rgb_to_hsv((0.7, 0.72, 0.7))
    slight = apply_warper_legacy_hsv((0.7, 0.72, 0.7), grid)
    moved = abs(rgb_to_hsv(slight)[0] - hsv[0])
    assert 0.0 < moved < 0.3 * 0.05  # 衰减后的量级


def test_sat_clamp_and_hue_wrap_are_legacy_semantics() -> None:
    grid = _grid(6, 6, ds=5.0)
    out = apply_warper_legacy_hsv((0.2, 0.4, 0.1), grid)
    assert rgb_to_hsv(out)[1] == pytest.approx(1.0)  # clamp [0,1](legacy 声明)

    grid = _grid(6, 6, dh=0.23)
    near_seam = rgb_to_hsv((1.0, 0.0, 0.2))  # 洋红端,hue≈0.967
    assert near_seam[0] > 0.9
    wrapped = apply_warper_legacy_hsv((1.0, 0.0, 0.2), grid)
    expected_hue = (near_seam[0] + 0.23) % 1.0
    assert expected_hue < 0.2  # fract 环绕回低色相
    # legacy 符号技巧:输入为 0 的通道(G=0 → sign=0)输出恒为 0
    expected_rgb = np.sign((1.0, 0.0, 0.2)) * hsv_to_rgb(np.array([expected_hue, 1.0, 1.0]))
    np.testing.assert_allclose(wrapped, expected_rgb, atol=1e-9)


def test_zero_channel_stays_zero_legacy_artifact() -> None:
    # GLSL sign(color)*hsv2rgb(...) 的固有行为(冻结为 legacy 语义):
    # 输入为 0 的通道,即使 hue 位移后本应非零,输出也保持 0。
    # 这不是 bug 修复对象——修改它会改变旧工程画面(违反 B03 兼容目标)。
    grid = _grid(6, 6, dh=0.5)
    out = apply_warper_legacy_hsv((1.0, 0.0, 0.2), grid)
    assert out[1] == 0.0


def test_bilinear_sampler_mirrors_clamp_to_edge_and_centers() -> None:
    grid = np.zeros((4, 4, 2))
    grid[:, :, 0] = np.arange(4)[None, :] / 3.0  # 列线性 0..1,行同值
    # 纹素中心精确命中:4 宽纹理的纹素中心在 u=(i+0.5)/4
    assert sample_warper_bilinear(grid, 0.375, 0.5)[0] == pytest.approx(1.0 / 3.0)
    # 两中心之间双线性:u=0.5 位于纹素 1(0.375)与 2(0.625)正中
    assert sample_warper_bilinear(grid, 0.5, 0.5)[0] == pytest.approx(0.5)
    # U 越界 → clamp 到边缘纹素,不环绕
    assert sample_warper_bilinear(grid, -0.5, 0.5)[0] == pytest.approx(0.0)
    assert sample_warper_bilinear(grid, 1.5, 0.5)[0] == pytest.approx(1.0)


def test_seam_takes_edge_texels_not_wrapped_average() -> None:
    # legacy 特性:接缝两侧各自取边缘纹素(CLAMP_TO_EDGE),已记录不修改
    grid = np.zeros((2, 2, 2))
    grid[:, 0, 0] = -0.4   # hue≈0 侧(两行同值,排除 V 轴混合干扰)
    grid[:, 1, 0] = 0.4    # hue≈1 侧
    left = sample_warper_bilinear(grid, 0.001, 0.5)[0]
    right = sample_warper_bilinear(grid, 0.999, 0.5)[0]
    assert left == pytest.approx(-0.4, abs=1e-6)
    assert right == pytest.approx(0.4, abs=1e-6)
