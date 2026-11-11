# -*- coding: utf-8 -*-
"""CW-B06:Chroma Warp 笔画/Pin 求解器(纯函数,Topos 声明语义)。

计划:`docs/plans/2026-11-12-color-warper-backend-plan.md` §4 CW-B06。

Topos 声明语义(B00:Resolve 私有求解不可考,本模块是可验证的 Topos
目标,不宣称逐位一致):
---------------------------------------------------------------
1. **约束**:每笔画/Pin 是色度平面 (u,v) 上的位移约束。色度平面把
   (hue θ, sat c) 映射为笛卡尔 ``(u,v) = (c·cos2πθ, c·sin2πθ)``;
   c=0(灰)映射到原点,hue 无定义但不产生噪声。
2. **权重** ``w = w_chroma · w_tonal``:
   - ``w_chroma = exp(−(d/r)²)``,r = max(chroma_range, 1e-6)(默认
     0.040 实测值);d = 像素到约束中心的欧氏距离 —— Normal 中心=源色度
     点;Point-to-Point 中心=源→目标线段的最近点(两端与之间均受影响);
     Pin 中心=其 (hue, sat) 直接换算的平面坐标(不经 RGB 往返重投影)。
   - ``w_tonal``:亮度窗口 ``[pivot − low, pivot + high]``(low/high 单位
     = luma 距离,域 [0,1];默认 0.5/1/1 → [−0.5,1.5] 全域生效),窗口内
     1、边缘 0.1 luma 宽 smoothstep 滚降。
3. **位移** ``D = Σwᵢ·(tᵢ−sᵢ) / Σwᵢ``(u/v/luma 三通道加权平均);
   Σw < 1e-6 时恒等(远离一切约束)。Pin = source==target 的零位移约束,
   天然把邻域平均拉回不动 —— "Pin 保护"。
4. **Exposure**:目标亮度 ``t_p' = t_p · 2^exposure``(域 [−8,8],默认 0);
   作用在约束的 luma 位移上,不改源。
5. **输出**:sat' = clamp(c', 0, 1);p' = max(0, p + D_p);
   ``RGB' = where(c<0, −rgb', rgb')``(B03 反演;负通道翻转,但**不用**
   legacy 的 sign 乘法——那个会把输入为 0 的通道清零,是新算法不需要的
   legacy 固有行为);负通道/HDR 不提前截断(p>1 保留)。
6. **确定性与退化**:纯函数,同状态重复求解逐位一致;重复约束(同源同
     目标)与单条等价;零长度笔画(source==target)零位移;矛盾约束
   (同源异目标)按权重平均,可预测;诊断见 ``diagnose_constraints``
   (INFO 级,不失败)。
7. **路径只用于交互**:`path` 不参与求解语义(B01 模型已如此)。

线程边界:纯函数;逐像素求解;GPU/异步化归 CW-B08。
"""
from __future__ import annotations

import math

import numpy as np

from src.features.color.core.warper_color import hsp_to_rgb, smoothstep
from src.features.color.core.warper_state import (
    STROKE_PARAM_DOMAINS,
    WarperStateV2,
)

__all__ = [
    "chroma_plane",
    "stroke_weight",
    "solve_chroma_warp_pixel",
    "solve_chroma_warp",
    "diagnose_constraints",
]

_TAU = 2.0 * math.pi
_MIN_WEIGHT = 1e-6
_TONAL_ROLLOFF = 0.1


def chroma_plane(rgb) -> tuple[float, float, float]:
    """RGB → (u, v, luma):色度平面坐标 + HSP 亮度(幅度域)。"""
    from src.features.color.core.warper_color import rgb_to_hsp

    magnitude = np.abs(np.asarray(rgb, dtype=np.float64))
    if not np.isfinite(magnitude).all():
        raise ValueError("颜色必须为有限值")
    hue, sat, luma = rgb_to_hsp(magnitude)
    angle = _TAU * hue
    return float(sat * math.cos(angle)), float(sat * math.sin(angle)), float(luma)


def _to_cartesian(hue: float, sat: float) -> tuple[float, float]:
    angle = _TAU * hue
    return sat * math.cos(angle), sat * math.sin(angle)


def _nearest_on_segment(px: float, py: float, ax: float, ay: float,
                        bx: float, by: float) -> tuple[float, float]:
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq <= 1e-24:
        return ax, ay
    t = ((px - ax) * dx + (py - ay) * dy) / length_sq
    t = min(max(t, 0.0), 1.0)
    return ax + t * dx, ay + t * dy


def _chroma_weight(pixel_u: float, pixel_v: float,
                   center_u: float, center_v: float, params: dict) -> float:
    """高斯色度权重:中心是约束在色度平面上的位置(距像素的欧氏距离)。"""
    distance = math.hypot(pixel_u - center_u, pixel_v - center_v)
    radius = max(float(params.get("chroma_range",
                                   STROKE_PARAM_DOMAINS["chroma_range"][2])), 1e-6)
    return math.exp(-((distance / radius) ** 2))


def _tonal_weight(pixel_luma: float, params: dict) -> float:
    """亮度窗口权重:``[pivot−low, pivot+high]`` 内 1,边缘 0.1 luma 滚降。"""
    pivot = float(params.get("pivot", 0.5))
    low = float(params.get("tonal_low", 1.0))
    high = float(params.get("tonal_high", 1.0))
    lower, upper = pivot - low, pivot + high
    if lower <= pixel_luma <= upper:
        return 1.0
    if pixel_luma < lower:
        return float(smoothstep(lower - _TONAL_ROLLOFF, lower, pixel_luma))
    return 1.0 - float(smoothstep(upper, upper + _TONAL_ROLLOFF, pixel_luma))


def stroke_weight(
    pixel_u: float, pixel_v: float, pixel_luma: float,
    source_rgb, target_rgb, params: dict, mode: str,
) -> float:
    """一条笔画约束对像素的组合权重(chroma × tonal)。"""
    s_u, s_v, _ = chroma_plane(source_rgb)
    if mode == "point_to_point":
        t_u, t_v, _ = chroma_plane(target_rgb)
        center_u, center_v = _nearest_on_segment(pixel_u, pixel_v, s_u, s_v, t_u, t_v)
    else:
        center_u, center_v = s_u, s_v
    return (_chroma_weight(pixel_u, pixel_v, center_u, center_v, params)
            * _tonal_weight(pixel_luma, params))


def _constraint_terms(pixel_u: float, pixel_v: float, pixel_luma: float,
                      state: WarperStateV2):
    """展开 (weight, du, dv, dp) 迭代:笔画与 Pin 统一为位移约束。"""
    for stroke in state.strokes:
        s_u, s_v, s_p = chroma_plane(stroke.source_rgb)
        t_u, t_v, t_p = chroma_plane(stroke.target_rgb)
        exposure = float(stroke.params.get("exposure", 0.0))
        yield (stroke_weight(pixel_u, pixel_v, pixel_luma,
                             stroke.source_rgb, stroke.target_rgb,
                             stroke.params, stroke.mode),
               t_u - s_u, t_v - s_v, t_p * (2.0 ** exposure) - s_p)
    for pin in state.pin_points:
        pin_u, pin_v = _to_cartesian(pin.position[0], pin.position[1])
        # Pin:零位移约束,权重中心=自身色度平面位置(polar→笛卡尔即是
        # 求解平面坐标,不得再经 RGB 往返重投影)。tonal 只看像素亮度
        # 与参数,与笔画同一窗口函数。
        yield (_chroma_weight(pixel_u, pixel_v, pin_u, pin_v, pin.params)
               * _tonal_weight(pixel_luma, pin.params),
               0.0, 0.0, 0.0)


def solve_chroma_warp_pixel(rgb, state: WarperStateV2) -> np.ndarray:
    color = np.asarray(rgb, dtype=np.float64)
    if not state.strokes and not state.pin_points:
        return color.copy()
    u, v, luma = chroma_plane(color)
    weight_sum = 0.0
    du = dv = dp = 0.0
    for w, term_u, term_v, term_p in _constraint_terms(u, v, luma, state):
        if w <= _MIN_WEIGHT:
            continue
        weight_sum += w
        du += w * term_u
        dv += w * term_v
        dp += w * term_p
    if weight_sum <= _MIN_WEIGHT:
        return color.copy()
    du /= weight_sum
    dv /= weight_sum
    dp /= weight_sum

    from src.features.color.core.warper_color import rgb_to_hsp
    hue, _sat, _p = rgb_to_hsp(np.abs(color))
    out_u, out_v = u + du, v + dv
    out_sat = min(max(math.hypot(out_u, out_v), 0.0), 1.0)
    if out_sat <= 0.0:
        out_hue = 0.0  # 零色度政策:定义值
    else:
        out_hue = (math.atan2(out_v, out_u) / _TAU) % 1.0
    out_luma = max(luma + dp, 0.0)
    reconstructed = hsp_to_rgb(np.array([out_hue, out_sat, out_luma]))
    return np.where(color < 0.0, -reconstructed, reconstructed)


def solve_chroma_warp(rgb, state: WarperStateV2) -> np.ndarray:
    """单像素/整图入口(H×W×3 ndarray 或 3 向量)。"""
    array = np.asarray(rgb, dtype=np.float64)
    if array.ndim == 1:
        return solve_chroma_warp_pixel(array, state)
    if array.ndim != 3 or array.shape[2] != 3:
        raise ValueError(f"图像需要 H×W×3,得到 {array.shape}")
    result = np.empty_like(array)
    for row in range(array.shape[0]):
        for col in range(array.shape[1]):
            result[row, col] = solve_chroma_warp_pixel(array[row, col], state)
    return result


def diagnose_constraints(state: WarperStateV2) -> list[str]:
    """病态约束的 INFO 级诊断(不阻断求解;求解对它们有稳定退化)。"""
    findings: list[str] = []
    seen: dict[tuple, str] = {}
    for stroke in state.strokes:
        key = (stroke.source_rgb, stroke.target_rgb)
        if stroke.source_rgb == stroke.target_rgb:
            findings.append(f"zero_length:{stroke.stroke_id}")
        if key in seen:
            findings.append(f"duplicate:{seen[key]}+{stroke.stroke_id}")
        seen.setdefault(key, stroke.stroke_id)
    by_source: dict[tuple, list[str]] = {}
    for stroke in state.strokes:
        by_source.setdefault(stroke.source_rgb, []).append(stroke.stroke_id)
    for source, ids in by_source.items():
        targets = {s.target_rgb for s in state.strokes if s.stroke_id in ids}
        if len(targets) > 1:
            findings.append("conflicting:" + ",".join(sorted(ids)))
    return findings
