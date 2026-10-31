"""M05.2c · 高级调色段的 CPU 图像实现（HSL 曲线 + soft-clip）。

从 LutExportWorker 的逐项 GPU 镜像实现（lut_exporter.py，与
shader_nodes.py GLSL 一致）提取为可直接作用于**图像**的函数——
LUT 导出与视频导出共用同一份数学（此前 CPU 视频导出对这两段
只能声明 dropped）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np


def _rgb_to_hsv(rgb: np.ndarray) -> np.ndarray:
    """RGB→HSV（与 lut_exporter._rgb_to_hsv_static / GLSL 同式）。"""
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    maxc = np.maximum(np.maximum(r, g), b)
    minc = np.minimum(np.minimum(r, g), b)
    delta = maxc - minc
    v = maxc
    s = np.where(maxc > 1e-10, delta / np.maximum(maxc, 1e-10), 0.0)
    h = np.zeros_like(maxc)
    mask = delta > 1e-10
    dr = np.where(mask, (maxc - r) / np.maximum(delta, 1e-10), 0.0)
    dg = np.where(mask, (maxc - g) / np.maximum(delta, 1e-10), 0.0)
    db = np.where(mask, (maxc - b) / np.maximum(delta, 1e-10), 0.0)
    h = np.where((maxc == r) & mask, db - dg, h)
    h = np.where((maxc == g) & mask, 2.0 + dr - db, h)
    h = np.where((maxc == b) & mask, 4.0 + dg - dr, h)
    h = (h / 6.0) % 1.0
    return np.stack([h, s, v], axis=-1)


def _hsv_to_rgb(hsv: np.ndarray) -> np.ndarray:
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    i = np.floor(h * 6.0).astype(np.int32) % 6
    f = h * 6.0 - np.floor(h * 6.0)
    p = v * (1.0 - s)
    q = v * (1.0 - s * f)
    t = v * (1.0 - s * (1.0 - f))
    r = np.choose(i, [v, q, p, p, t, v])
    g = np.choose(i, [t, v, v, q, p, p])
    b = np.choose(i, [p, p, t, v, v, q])
    return np.stack([r, g, b], axis=-1)


def apply_hsl_curves_image(
    rgb: np.ndarray, hsl_curve_lut: Optional[np.ndarray],
) -> np.ndarray:
    """HSL 曲线（6×256 LUT）作用于 RGB 图像（..., 3）float。

    索引序与 lut_exporter 一致：LumSat0/HueHue1/HueSat2/HueLum3/
    SatSat4/SatLum5。None/形状不符 → 原样返回（调用方不应到达）。
    """
    if hsl_curve_lut is None or getattr(
            hsl_curve_lut, "shape", (0,))[0] != 6:
        return rgb
    result = rgb.astype(np.float32, copy=True)
    orig_shape = result.shape
    flat = result.reshape(-1, 3)

    hsv = _rgb_to_hsv(flat)
    s = hsv[..., 1]
    v = hsv[..., 2]
    luma = (0.299 * flat[..., 0] + 0.587 * flat[..., 1]
            + 0.114 * flat[..., 2])

    def lookup_curve(idx: int, domain_val: np.ndarray) -> np.ndarray:
        curve = hsl_curve_lut[idx]
        clipped = np.clip(domain_val, 0.0, 1.0)
        indices = np.clip(
            (clipped * 255.0).astype(np.int32), 0, 255)
        return curve[indices]

    total_sat = (lookup_curve(0, luma) * lookup_curve(2, hsv[..., 0])
                 * lookup_curve(4, s))
    hue_shift = lookup_curve(1, hsv[..., 0])
    total_lum = lookup_curve(3, hsv[..., 0]) * lookup_curve(5, s)

    new_h = (hsv[..., 0] + hue_shift) % 1.0
    new_s = np.clip(s * np.clip(total_sat, 0.0, 2.0), 0.0, 1.0)
    new_v = np.clip(v * np.clip(total_lum, 0.0, 2.0), 0.0, 1.0)
    out = _hsv_to_rgb(np.stack([new_h, new_s, new_v], axis=-1))
    return out.reshape(orig_shape).astype(np.float32)


def apply_soft_clip_image(rgb: np.ndarray, params: Dict[str, Any],
                          ) -> np.ndarray:
    """soft-clip（filmic tone map）作用于 RGB 图像。

    逐项镜像 shader_nodes.py SOFT_CLIP_SNIPPET（幂律 toe+暗部平衡+
    比例缩放 shoulder）——与 lut_exporter._apply_soft_clip_cpu /
    GPU 预览同源。
    """
    result = rgb.astype(np.float64, copy=True)

    high = params.get("high", 1.0)
    white_point = params.get("white_point", 1.1)
    shoulder_strength = params.get("shoulder_strength", 1.0)
    low = params.get("low", 0.0)
    black_point = params.get("black_point", -0.1)
    toe_strength = params.get("toe_strength", 1.0)
    exposure = params.get("exposure", 0.0)
    contrast = params.get("contrast", 0.0)

    if exposure != 0.0:
        result *= 2.0 ** exposure
    if contrast != 0.0:
        factor = ((1.0 + contrast) if contrast >= 0.0
                  else 1.0 / (1.0 + abs(contrast)))
        result = (np.sign(result)
                  * np.power(np.abs(result) / 0.18, factor) * 0.18)
    if black_point < low:
        k = max(toe_strength, 0.01)
        rng = low - black_point
        mask_toe = result < low
        if np.any(mask_toe):
            diff = low - result[mask_toe]
            result[mask_toe] = black_point + rng * np.power(
                1.0 + k * diff / rng, -1.0 / k)
    else:
        result = np.maximum(result, black_point)

    toe_min = result.min(axis=-1, keepdims=True)
    toe_max = result.max(axis=-1, keepdims=True)
    mask_bal = (toe_max - toe_min > 0.2) & (toe_min < low)
    if np.any(mask_bal):
        balance = np.clip((toe_max - toe_min) * 0.25, 0.0, 0.15)
        luma = np.expand_dims(
            result[..., 0] * 0.299 + result[..., 1] * 0.587
            + result[..., 2] * 0.114, axis=-1)
        w = balance * np.abs(toe_min)
        result = np.where(mask_bal, result * (1.0 - w) + luma * w, result)

    # Shoulder：按 max 通道比例缩放（保饱和度）——与 lut_exporter 同式
    max_c = result.max(axis=-1, keepdims=True)
    if white_point > high:
        mask_sh = max_c > high
        if np.any(mask_sh):
            k2 = max(shoulder_strength, 0.01)
            rng2 = white_point - high
            diff = max_c[mask_sh] - high
            mapped = white_point - rng2 * np.power(
                1.0 + k2 * diff / rng2, -1.0 / k2)
            scale = np.ones_like(max_c)
            scale[mask_sh] = mapped / max_c[mask_sh]
            result = result * scale
    else:
        mask_wp = max_c > white_point
        if np.any(mask_wp):
            scale = np.ones_like(max_c)
            scale[mask_wp] = white_point / max_c[mask_wp]
            result = result * scale
    return result.astype(np.float32)


__all__ = ["apply_hsl_curves_image", "apply_soft_clip_image"]
