"""M06.2 · CPU（numpy）版 Qualifier keyer——时间线 CPU 导出的像素级限定器。

公式与 Color 页 GPU keyer（``src/features/color/shaders/keyer.glsl``）
逐条对应（该 shader 是权威实现；此处为其 numpy 移植，供 CPU primary
导出消费——此前 CPU 导出对 qualifier 只能显式丢弃）：

- RGB→HSL：``l = (max+min)/2``（真 HSL 亮度）；HDR 处理：``max>1``
  时先按 max 归一再 ``l *= max_orig``（keyer.glsl:26-47）；
- Hue 窗口（环形距离）：``matte *= 1 - smoothstep(half_w, half_w+h_soft, dist)``
  （keyer.glsl:55-61）；
- Sat 带：``smoothstep(s_low-s_soft, s_low, s) * (1 - smoothstep(s_high, s_high+s_soft, s))``
  （keyer.glsl:63-69）；
- Lum 带：``smoothstep(l_low-l_soft, l_low, l) * (1 - smoothstep(l_high, l_high+l_soft, l))``
  （keyer.glsl:71-77）；
- invert：``1 - matte``；最终 clamp [0,1]（keyer.glsl:79-80）。

合成（shader_compiler.py:502-503）：``out = src*(1-m·α) + graded*(m·α)``
——matte 与 layer opacity 相乘后作为 mix 权重，限定的是**整条 layer
调色**（非独立二级修正）。

**已支持**：hue/sat/lum 三带 + invert（基础 keyer）+ matte 后处理
（N02-B 切片 2：clean_black/white、blur、denoise 的 numpy 等价，
换算走 ``key_pipeline_params`` 权威表——GL/CPU 共用口径）。
**未支持（调用方显式告知用户）**：mask_shapes 组合（mask_shapes_cpu
另域处理）、ai_matte——GPU 管线专属。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np


def _smoothstep_np(e0: np.ndarray, e1: np.ndarray, x: np.ndarray) -> np.ndarray:
    """GLSL smoothstep 的 numpy 等价（e0==e1 时按半开跃迁处理）。"""
    span = e1 - e0
    t = np.where(span > 1e-12, (x - e0) / np.where(span > 1e-12, span, 1.0),
                 np.where(x >= e1, 1.0, 0.0))
    t = np.clip(t, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def rgb_to_hsl_np(rgb: np.ndarray) -> tuple:
    """RGB→(h, s, l)（keyer.glsl:26-47 的 numpy 移植，含 HDR 处理）。

    ``rgb``: (..., 3) float（线性或编码域均可——keyer 在其输入域上工作）。
    """
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    maxc = np.maximum(np.maximum(r, g), b)
    minc = np.minimum(np.minimum(r, g), b)
    l = (maxc + minc) * 0.5

    # HDR：max>1 → 先归一再 l *= max_orig（高光亮度按绝对刻度保留）
    over = maxc > 1.0
    if np.any(over):
        scale = np.where(over, maxc, 1.0)
        rn, gn, bn = r / scale, g / scale, b / scale
        maxn = np.maximum(np.maximum(rn, gn), bn)
        minn = np.minimum(np.minimum(rn, gn), bn)
        l_hdr = (maxn + minn) * 0.5 * maxc
        l = np.where(over, l_hdr, l)
        maxc, minc = np.where(over, maxn, maxc), np.where(over, minn, minc)

    delta = maxc - minc
    s = np.where(
        delta <= 1e-12, 0.0,
        np.where(l <= 0.5, delta / np.maximum(maxc + minc, 1e-12),
                 delta / np.maximum(2.0 - maxc - minc, 1e-12)))

    # Hue（环形，[0,1)）
    h = np.zeros_like(l)
    dr = np.where(delta > 1e-12, (maxc - r) / np.maximum(delta, 1e-12), 0.0)
    dg = np.where(delta > 1e-12, (maxc - g) / np.maximum(delta, 1e-12), 0.0)
    db = np.where(delta > 1e-12, (maxc - b) / np.maximum(delta, 1e-12), 0.0)
    is_r = maxc == r
    is_g = (maxc == g) & ~is_r
    is_b = ~is_r & ~is_g
    h = np.where(is_r, db - dg, h)
    h = np.where(is_g, 2.0 + dr - db, h)
    h = np.where(is_b, 4.0 + dg - dr, h)
    h = (h / 6.0) % 1.0
    h = np.where(delta <= 1e-12, 0.0, h)
    return h, s, l


def compute_qualifier_matte(rgb: np.ndarray,
                            qp: Dict[str, Any]) -> Optional[np.ndarray]:
    """计算 qualifier matte（keyer.glsl 主公式，numpy 移植）。

    Args:
        rgb: (..., 3) float——keyer 输入域的源图（Color 页为色彩管理后
            的源，导出侧为进入调色前的 float RGB）。
        qp: layer.qualifier_params（enabled 必须为 True）。

    Returns:
        (..., 1) float32 matte ∈ [0,1]；qp 禁用返回 None。
    """
    if not qp or not qp.get("enabled"):
        return None
    h, s, l = rgb_to_hsl_np(rgb[..., :3].astype(np.float32))

    matte = np.ones_like(l)

    if qp.get("hue_enabled", True):
        center = float(qp.get("hue_center", 0.5) or 0.5)
        width = float(qp.get("hue_width", 1.0) or 1.0)
        soft = float(qp.get("hue_soft", 0.1) or 0.1)
        half_w = max(width * 0.5, 0.0)
        dist = np.abs(h - center)
        dist = np.minimum(dist, 1.0 - dist)          # 环形距离
        matte *= 1.0 - _smoothstep_np(
            np.float32(half_w), np.float32(half_w + max(soft, 0.0)), dist)

    if qp.get("sat_enabled", True):
        s_low = float(qp.get("sat_low", 0.0) or 0.0)
        s_high = float(qp.get("sat_high", 1.0) or 1.0)
        s_soft_l = float(qp.get("sat_soft_low", 0.0) or 0.0)
        s_soft_h = float(qp.get("sat_soft_high", 0.0) or 0.0)
        band = _smoothstep_np(np.float32(s_low - s_soft_l),
                              np.float32(s_low), s) \
            * (1.0 - _smoothstep_np(np.float32(s_high),
                                    np.float32(s_high + s_soft_h), s))
        matte *= band

    if qp.get("lum_enabled", True):
        l_low = float(qp.get("lum_low", 0.0) or 0.0)
        l_high = float(qp.get("lum_high", 1.0) or 1.0)
        l_soft_l = float(qp.get("lum_soft_low", 0.0) or 0.0)
        l_soft_h = float(qp.get("lum_soft_high", 0.0) or 0.0)
        band = _smoothstep_np(np.float32(l_low - l_soft_l),
                              np.float32(l_low), l) \
            * (1.0 - _smoothstep_np(np.float32(l_high),
                                    np.float32(l_high + l_soft_h), l))
        matte *= band

    if qp.get("invert", False):
        matte = 1.0 - matte
    return np.clip(matte, 0.0, 1.0)[..., np.newaxis].astype(np.float32)


def qualifier_postprocess_active(qp: Dict[str, Any]) -> bool:
    """matte 后处理参数是否非零（CPU 导出不支持——调用方显式告知）。"""
    if not qp:
        return False
    return any(float(qp.get(k, 0.0) or 0.0) > 0.0
               for k in ("clean_black", "clean_white", "blur", "denoise"))


def apply_qualifier_masked_grade(src_rgb: np.ndarray, graded_rgb: np.ndarray,
                                 matte: np.ndarray,
                                 opacity: float = 1.0) -> np.ndarray:
    """matte 限定合成（shader_compiler.py:502-503 的 numpy 移植）。

    ``out = src*(1 - m·α) + graded*(m·α)``——m 为 matte，α 为 layer
    opacity（导出 primary 段 α=1）。
    """
    w = np.clip(matte * float(opacity), 0.0, 1.0)
    return (src_rgb * (1.0 - w) + graded_rgb * w).astype(np.float32)


def _box_min_max(matte: np.ndarray, radius: int, *,
                  maximum: bool) -> np.ndarray:
    """可分 box min/max（morphology.glsl 的 numpy 等价）。

    H/V 两趟 = 方形窗；边界 clamp（GL 纹理默认 CLAMP_TO_EDGE 对齐
    ndimage 的 'nearest'）。radius=0 原样返回。
    """
    if radius <= 0:
        return matte
    from scipy import ndimage
    size = 2 * int(radius) + 1
    fn = ndimage.grey_dilation if maximum else ndimage.grey_erosion
    return fn(matte, size=(size, size), mode="nearest")


def _gaussian_tap_weights(radius: int, sigma: float) -> np.ndarray:
    """截断高斯核权重（blur.glsl 同式：w=gauss(i,σ)，±r，权重归一）。"""
    xs = np.arange(-int(radius), int(radius) + 1, dtype=np.float64)
    weights = np.exp(-(xs * xs) / (2.0 * sigma * sigma))
    return (weights / weights.sum()).astype(np.float32)


def _separable_gaussian(matte: np.ndarray, radius: int, sigma: float) -> np.ndarray:
    """截断高斯可分模糊（H+V；blur.glsl 的 numpy 等价）。"""
    from scipy import ndimage
    if radius <= 0:
        return matte
    kernel = _gaussian_tap_weights(radius, sigma)
    out = ndimage.convolve1d(matte, kernel, axis=1, mode="nearest")
    return ndimage.convolve1d(out, kernel, axis=0, mode="nearest")


def apply_qualifier_postprocess_cpu(matte: np.ndarray,
                                    qp: Dict[str, Any]) -> np.ndarray:
    """层级 matte 后处理（``_apply_matte_postprocess`` 的 numpy 等价）。

    顺序与 GL 冻结一致：clean_black(erode) → clean_white(dilate) →
    blur → denoise（小模糊 + smoothstep 对比恢复）；换算全部走
    ``key_pipeline_params``（GL/CPU 共用权威表）。
    """
    from src.features.color.core.key_pipeline_params import (
        blur_radius, blur_sigma, clean_radius, denoise_edge_halfwidth,
        denoise_radius, denoise_strength,
    )
    out = np.asarray(matte, dtype=np.float32)
    # 兼容 (h, w, 1) 通道维（图执行路径的 matte 形状）
    squeezed = out.ndim == 3 and out.shape[-1] == 1
    if squeezed:
        out = out[:, :, 0]
    out = out.copy()
    r_cb = clean_radius(qp.get("clean_black"))
    if r_cb > 0:
        out = _box_min_max(out, r_cb, maximum=False)
    r_cw = clean_radius(qp.get("clean_white"))
    if r_cw > 0:
        out = _box_min_max(out, r_cw, maximum=True)
    r_blur = blur_radius(qp.get("blur"))
    if r_blur > 0:
        out = _separable_gaussian(out, r_blur, blur_sigma(r_blur))
    r_dn = denoise_radius(qp.get("denoise"))
    if r_dn > 0:
        out = _separable_gaussian(out, r_dn, blur_sigma(r_dn))
        strength = denoise_strength(qp.get("denoise"))
        k = denoise_edge_halfwidth(strength)
        out = _smoothstep_np(np.float32(0.5 - k), np.float32(0.5 + k), out)
    out = np.clip(out, 0.0, 1.0)
    return out[:, :, np.newaxis] if squeezed else out


__all__ = [
    "compute_qualifier_matte",
    "apply_qualifier_masked_grade",
    "apply_qualifier_postprocess_cpu",
    "qualifier_postprocess_active",
    "rgb_to_hsl_np",
]
