"""M05.2c · 高级调色段的 CPU 图像实现（HSL 曲线 + soft-clip）。

从 LutExportWorker 的逐项 GPU 镜像实现（lut_exporter.py，与
shader_nodes.py GLSL 一致）提取为可直接作用于**图像**的函数——
LUT 导出与视频导出共用同一份数学（此前 CPU 视频导出对这两段
只能声明 dropped）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np

from src.features.color.core.curves_soft_clip import (
    SOFT_CLIP_CALIBRATION_VERSION, soft_clip_calibration_version,
)


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
    rgb: np.ndarray, hsl_curve_lut: Optional[np.ndarray], *, encoded_input: bool = False,
) -> np.ndarray:
    """HSL 曲线（6×256 LUT）作用于 RGB 图像（..., 3）float。

    索引序与 lut_exporter 一致：LumSat0/HueHue1/HueSat2/HueLum3/
    SatSat4/SatLum5。None/形状不符 → 原样返回（调用方不应到达）。
    """
    if hsl_curve_lut is None:
        return rgb
    hsl_curve_lut = np.asarray(hsl_curve_lut, dtype=np.float32)
    from src.features.color.core.hsl_curve_calibration import (
        VALID_HSL_LUT_SHAPES,
        apply_calibrated_hsl_curves,
        is_calibrated_hsl_lut,
    )
    if hsl_curve_lut.shape not in VALID_HSL_LUT_SHAPES:
        return rgb
    if is_calibrated_hsl_lut(hsl_curve_lut):
        return apply_calibrated_hsl_curves(rgb, hsl_curve_lut, encoded_input=encoded_input)
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


def _quadratic_toe(values: np.ndarray, edge: float, width: float) -> np.ndarray:
    """Join a constant floor to unit slope across a two-sided knee."""
    if width <= 0.0:
        return np.maximum(values, edge)
    t = np.clip((values - edge + width) / (2.0 * width), 0.0, 1.0)
    return np.where(values < edge + width, edge + width * t * t, values)


def _resolve_toe(values: np.ndarray, edge: float, width: float) -> np.ndarray:
    if width <= 0.0 or edge + width <= 0.5:
        return _quadratic_toe(values, edge, width)
    # A knee reaching past mid-gray keeps its floor but joins the identity
    # at mid-gray with Hermite slopes of zero and one at the two ends.
    start = edge - width
    span = 0.5 - start
    t = np.clip((values - start) / span, 0.0, 1.0)
    knee = edge + t * t * ((0.5 - edge) * (3.0 - 2.0 * t) + (t - 1.0) * span)
    return np.where(values < 0.5, knee, values)


def _apply_calibrated_soft_clip(
    values: np.ndarray, low: float, low_knee: float, high: float, high_knee: float,
    *, encoded_input: bool, independent: bool,
) -> np.ndarray:
    """Evaluate the fitted knees in Gamma 2.4 code values."""
    encoded = values if encoded_input else np.copysign(np.power(np.abs(values), 1.0 / 2.4), values)
    # The measured interior has slope 1023/1024 for an active channel.
    # Extend the endpoint response continuously into signed/HDR values.
    bias = 0.5 - 1.5 * (low - low_knee) + 0.5 * (high - 1.0 + high_knee)
    encoded -= (np.clip(encoded, 0.0, 1.0) + bias) / 1024.0
    toe = _resolve_toe if independent else _quadratic_toe
    if low != 0.0 or low_knee > 0.0:
        encoded = toe(encoded, low, low_knee)
    if high_knee > 0.0:
        encoded = 1.0 - toe(1.0 - encoded, 1.0 - high, high_knee)
    elif high != 1.0:
        span = high - low + low_knee if independent else np.clip(high, 0.0, 1.0)
        encoded = np.minimum(encoded, high - span / 1024.0)
    return encoded if encoded_input else np.copysign(np.power(np.abs(encoded), 2.4), encoded)


def apply_curves_soft_clip_image(
    rgb: np.ndarray, params: Dict[str, Any], *, encoded_input: bool = False,
) -> np.ndarray:
    """曲线面板四参数软剪裁（指数软膝）作用于 RGB 图像。

    逐式镜像 curves_soft_clip.apply_soft_clip_channel（= 移动版
    soft_clip.frag / GLSL CURVES_SOFT_CLIP_SNIPPET / MSL 同式）。默认
    参数下严格恒等：超白与负 RGB 原样保留。校准版在编码域处理。
    非有限像素原样直通（软膝只定义在有限域上）。
    """
    result = rgb.astype(np.float64, copy=True)
    channels = params.get("channels") if isinstance(params, dict) else None
    if not isinstance(channels, dict):
        return result.astype(np.float32)

    version = soft_clip_calibration_version(params)
    independent = version == SOFT_CLIP_CALIBRATION_VERSION
    finite = np.isfinite(result)
    for idx, name in ((0, "R"), (1, "G"), (2, "B")):
        values = channels.get(name)
        if not isinstance(values, dict):
            continue
        low_clip = float(values.get("low_clip", 0.0))
        low_soft = float(values.get("low_soft", 0.0))
        high_clip = float(values.get("high_clip", 1.0))
        high_soft = float(values.get("high_soft", 0.0))

        gap = max(high_clip - low_clip, 0.0)
        if not independent and gap == 0.0:
            floor = low_clip
            if version and not encoded_input:
                floor = np.copysign(abs(low_clip) ** 2.4, low_clip)
            result[..., idx] = np.where(finite[..., idx], floor,
                                        result[..., idx])
            continue
        low_knee = low_soft if independent else min(max(low_soft, 0.0), gap)
        high_knee = high_soft if independent else min(max(high_soft, 0.0), max(gap - low_knee, 0.0))

        col = result[..., idx]
        col_finite = finite[..., idx]
        if version:
            if (low_clip, low_knee, high_clip, high_knee) != (0.0, 0.0, 1.0, 0.0):
                col[col_finite] = _apply_calibrated_soft_clip(
                    col[col_finite], low_clip, low_knee, high_clip, high_knee,
                    encoded_input=encoded_input, independent=independent)
            continue
        if low_knee > 0.0:
            low_start = low_clip + low_knee
            mask = col_finite & (col < low_start)
            if np.any(mask):
                col[mask] = low_clip + low_knee * np.exp(
                    (col[mask] - low_start) / low_knee)
        elif low_clip != 0.0:
            mask = col_finite & (col < low_clip)
            if np.any(mask):
                col[mask] = low_clip

        if high_knee > 0.0:
            high_start = high_clip - high_knee
            mask = col_finite & (col > high_start)
            if np.any(mask):
                col[mask] = high_clip - high_knee * np.exp(
                    -(col[mask] - high_start) / high_knee)
        elif high_clip != 1.0:
            mask = col_finite & (col > high_clip)
            if np.any(mask):
                col[mask] = high_clip
        result[..., idx] = col
    return result.astype(np.float32)


__all__ = ["apply_curves_soft_clip_image", "apply_hsl_curves_image",
           "apply_soft_clip_image"]
