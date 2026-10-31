"""A1: 图层混合模式的 NumPy 实现（CPU 回退路径 + 导出 CPU 回退共用）。

与 ``src/features/edit/core/shaders/blend.glsl`` 保持公式一致：
- 输入输出均为 [0,1] 线性存储域（非预乘）float32 RGB；
- 混合发生在 RGB 三通道上，随后按 source-over 与底图合回；
- HUE/SATURATION/COLOR/LUMINOSITY 走 RGB↔HSL（无循环实现）。

公式口径（W3C compositing / PDF 透明度规范）：
- multiply:   s*d
- screen:     s+d-s*d
- overlay:    d<=0.5 ? 2*s*d : 1-2*(1-s)*(1-d)      （以 d 为底）
- darken:     min(s,d)
- lighten:    max(s,d)
- color_dodge: d/(1-s)  （s=1 → 1）
- color_burn:  1-(1-d)/s （s=0 → 0）
- hard_light:  overlay 但以 s 判断分支
- soft_light:  W3C 归一化公式（g(d) 分段）
- difference:  |d-s|
- exclusion:   s+d-2*s*d
- hue:         setLum(setSat(s, lum(d)), lum(d))
- saturation:  setLum(setSat(d, lum(s)), lum(d))
- color:       setLum(s, lum(d))
- luminosity:  setLum(d, lum(s))
"""

from __future__ import annotations

import numpy as np

from src.features.edit.core.clip_params import ClipBlendMode

__all__ = [
    "BLEND_MODE_CODES",
    "blend_rgb",
    "blend_frames_u8",
]

#: 枚举 → GLSL/NumPy 共享 mode 编号（与 ClipBlendMode.code 一致）
BLEND_MODE_CODES: dict[str, int] = {
    mode.value: mode.code for mode in ClipBlendMode
}

_EPS = 1e-6


# ---------------------------------------------------------------------------
# RGB↔HSL（无循环；lum/sat 按 W3C 非最大化定义：lum = 0.3R+0.59G+0.11B）
# ---------------------------------------------------------------------------

def _lum(c: np.ndarray) -> np.ndarray:
    return 0.3 * c[..., 0] + 0.59 * c[..., 1] + 0.11 * c[..., 2]


def _clip_color_simple(c: np.ndarray) -> np.ndarray:
    """W3C ClipColor：把超出 [0,1] 亮度范围的分量拉回。"""
    l = _lum(c)[..., None]
    mn = c.min(axis=-1)[..., None]
    mx = c.max(axis=-1)[..., None]
    low = np.where(mn < 0.0, (c - l) * l / np.maximum(l - mn, _EPS), c)
    high = np.where(mx > 1.0, (c - l) * (1.0 - l) / np.maximum(mx - l, _EPS), low)
    return np.where((mn < 0.0) | (mx > 1.0), high, c)


def _set_lum(c: np.ndarray, l: np.ndarray) -> np.ndarray:
    delta = l - _lum(c)
    return _clip_color_simple(c + delta[..., None])


def _sat(c: np.ndarray) -> np.ndarray:
    return c.max(axis=-1) - c.min(axis=-1)


def _set_sat(c: np.ndarray, s: np.ndarray) -> np.ndarray:
    """W3C SetSat：按分量大小排序重建，保持色相。

    平局约定与 W3C 伪码及 blend.glsl 对齐：max == min（纯灰）时输出
    全 0（W3C else 分支 Cmid = Cmax = 0；随后 setLum 等量加亮度 → 中性
    灰）。argsort 直接分布会把 s 塞进单个通道，产生 GPU 路径没有的偏色。
    """
    mx = c.max(axis=-1)
    mn = c.min(axis=-1)
    mid = c.sum(axis=-1) - mx - mn
    denom_mid = np.maximum(mx - mn, _EPS)
    sat_mid = np.where(mx > mn, (mid - mn) * s / denom_mid, 0.0)
    out = np.stack([
        np.zeros_like(s),            # min 分量 → 0
        np.where(mx > mn, sat_mid, 0.0),  # mid 分量线性
        s,                           # max 分量 → s
    ], axis=-1)
    # 重新按原序分布：按 min/mid/max 的索引还原
    # （各通道独立计算 min/mid/max 的位置）
    out_sorted = out
    order = np.argsort(c, axis=-1)  # 升序：min, mid, max 的通道索引
    result = np.zeros_like(c)
    np.put_along_axis(result, order, out_sorted, axis=-1)
    # max == min（纯灰）：W3C else 分支 → 全 0
    gray = (mx <= mn)[..., None]
    return np.where(gray, np.zeros_like(result), result)


# ---------------------------------------------------------------------------
# 各模式 RGB 公式（s = source/overlay，d = destination/base）
# ---------------------------------------------------------------------------

def _mul(s: np.ndarray, d: np.ndarray) -> np.ndarray:
    return s * d


def _screen(s: np.ndarray, d: np.ndarray) -> np.ndarray:
    return s + d - s * d


def _overlay(s: np.ndarray, d: np.ndarray) -> np.ndarray:
    low = 2.0 * s * d
    high = 1.0 - 2.0 * (1.0 - s) * (1.0 - d)
    return np.where(d <= 0.5, low, high)


def _hard_light(s: np.ndarray, d: np.ndarray) -> np.ndarray:
    low = 2.0 * s * d
    high = 1.0 - 2.0 * (1.0 - s) * (1.0 - d)
    return np.where(s <= 0.5, low, high)


def _color_dodge(s: np.ndarray, d: np.ndarray) -> np.ndarray:
    return np.where(s >= 1.0 - _EPS, 1.0, np.minimum(1.0, d / np.maximum(1.0 - s, _EPS)))


def _color_burn(s: np.ndarray, d: np.ndarray) -> np.ndarray:
    return np.where(s <= _EPS, 0.0, np.maximum(0.0, 1.0 - (1.0 - d) / np.maximum(s, _EPS)))


def _soft_light(s: np.ndarray, d: np.ndarray) -> np.ndarray:
    # W3C 归一化公式
    g = np.where(
        d <= 0.25,
        ((16.0 * d - 12.0) * d + 4.0) * d,
        np.sqrt(np.clip(d, 0.0, 1.0)),
    )
    return np.where(
        s <= 0.5,
        d - (1.0 - 2.0 * s) * d * (1.0 - d),
        d + (2.0 * s - 1.0) * (g - d),
    )


_MODE_FUNCS = {
    ClipBlendMode.MULTIPLY.code: _mul,
    ClipBlendMode.SCREEN.code: _screen,
    ClipBlendMode.OVERLAY.code: _overlay,
    ClipBlendMode.DARKEN.code: lambda s, d: np.minimum(s, d),
    ClipBlendMode.LIGHTEN.code: lambda s, d: np.maximum(s, d),
    ClipBlendMode.COLOR_DODGE.code: _color_dodge,
    ClipBlendMode.COLOR_BURN.code: _color_burn,
    ClipBlendMode.HARD_LIGHT.code: _hard_light,
    ClipBlendMode.SOFT_LIGHT.code: _soft_light,
    ClipBlendMode.DIFFERENCE.code: lambda s, d: np.abs(d - s),
    ClipBlendMode.EXCLUSION.code: lambda s, d: s + d - 2.0 * s * d,
}


def _hue(s: np.ndarray, d: np.ndarray) -> np.ndarray:
    return _set_lum(_set_sat(s, _sat(d)), _lum(d))


def _saturation(s: np.ndarray, d: np.ndarray) -> np.ndarray:
    return _set_lum(_set_sat(d, _sat(s)), _lum(d))


def _color(s: np.ndarray, d: np.ndarray) -> np.ndarray:
    return _set_lum(s, _lum(d))


def _luminosity(s: np.ndarray, d: np.ndarray) -> np.ndarray:
    return _set_lum(d, _lum(s))


_HSL_MODE_FUNCS = {
    ClipBlendMode.HUE.code: _hue,
    ClipBlendMode.SATURATION.code: _saturation,
    ClipBlendMode.COLOR.code: _color,
    ClipBlendMode.LUMINOSITY.code: _luminosity,
}


# ---------------------------------------------------------------------------
# 公共入口
# ---------------------------------------------------------------------------

def blend_rgb(
    base: np.ndarray,
    overlay: np.ndarray,
    mode: int | str | ClipBlendMode,
) -> np.ndarray:
    """对 [0,1] float32 RGB 应用混合模式。

    Args:
        base: 底图（已合成结果），[..., 3]，[0,1]
        overlay: 上层（当前片段已渲染纹理），形状与 base 相同
        mode: 混合模式（枚举 / 字符串值 / 整数编号）

    Returns:
        混合后的 RGB（[..., 3]，[0,1]，clip 到合法范围）
    """
    if isinstance(mode, ClipBlendMode):
        code = mode.code
    elif isinstance(mode, str):
        code = BLEND_MODE_CODES.get(mode, 0)
    else:
        code = int(mode)

    if code == ClipBlendMode.NORMAL.code:
        return np.clip(overlay, 0.0, 1.0)
    func = _MODE_FUNCS.get(code)
    if func is not None:
        return np.clip(func(overlay, base), 0.0, 1.0)
    hsl_func = _HSL_MODE_FUNCS.get(code)
    if hsl_func is not None:
        return np.clip(hsl_func(overlay, base), 0.0, 1.0)
    # 未知编号 → normal
    return np.clip(overlay, 0.0, 1.0)


def blend_frames_u8(
    base: np.ndarray,
    overlay: np.ndarray,
    mode: int | str | ClipBlendMode,
    opacity: float,
) -> np.ndarray:
    """uint8 BGR 帧的混合入口（CPU 合成路径用）。

    先在 float32 [0,1] 域按模式混合，再按 source-over 与底图合回并转回
    uint8。opacity 语义与既有 ``_alpha_blend`` 一致：overlay 权重。

    Args:
        base: 底图 uint8 BGR
        overlay: 上层 uint8 BGR（尺寸需与 base 一致，调用方先行 resize）
        mode: 混合模式
        opacity: 上层不透明度 [0,1]

    Returns:
        混合结果 uint8 BGR
    """
    base_f = base.astype(np.float32) / 255.0
    overlay_f = overlay.astype(np.float32) / 255.0
    # 调用方（CPU 预览/导出）传 BGR，而 blend_rgb/_lum 的 HSL 系公式按
    # RGB 加权（GPU 路径上传时已 swizzle 成 RGB）。这里统一翻到 RGB 混合
    # 再翻回，保证 hue/saturation/color/luminosity 与 GPU 一致且符合 W3C。
    base_rgb = base_f[..., ::-1]
    overlay_rgb = overlay_f[..., ::-1]
    blended_rgb = blend_rgb(base_rgb, overlay_rgb, mode)
    opacity = float(np.clip(opacity, 0.0, 1.0))
    out_rgb = blended_rgb * opacity + base_rgb * (1.0 - opacity)
    out_bgr = out_rgb[..., ::-1]
    return np.clip(out_bgr * 255.0 + 0.5, 0, 255).astype(np.uint8)
