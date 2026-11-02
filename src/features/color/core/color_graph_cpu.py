"""CPU oracle —— 调色节点算子的参考实现（N09，B10/B12）。

与 ``docs/color_nodes_mixer_spec.md``（v1 冻结）逐条对应；float32 精确
语义，供 GL/Metal 等价对照与手算验证。**不做隐式近似**：不支持的
Corrector 参数域显式声明（``UnsupportedNode``）。

Corrector 两层（B12.2 起）：

- ``corrector_oracle``：构造用简化域（exposure 档数乘子 + 1.0 基准
  saturation），供 mixer/key golden 构造（B10 起）；
- ``corrector_production_oracle``：**生产参数域**——逐条移植
  ``shader_nodes.GRADING_SNIPPET``/``CURVES_SNIPPET``（GLSL 权威），
  参数经 ``composition_uniform_values`` 同一提取路径，覆盖点态基础
  调色（exposure/WB/contrast/色轮/shadows/highlights/tonal sat/
  vibrance/hue/saturation）与 256 点 curves LUT。非点态（mid_detail/
  sharpness 邻域）、插件、mixer/warper/LUT3D/fx 等显式 unsupported。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from src.features.color.core.color_graph_compiler import (
    ColorGraphRenderPlan,
    RenderPass,
)


class UnsupportedNode(NotImplementedError):
    """CPU oracle 未实现的算子/参数域（显式声明，禁止静默近似）。"""


# ── 单算子公式（spec §1–§6） ─────────────────────────────────────────────

def parallel_mix(base: np.ndarray, branches: Sequence[np.ndarray],
                 keys: Sequence[np.ndarray | None]) -> np.ndarray:
    """spec §1：base 依序被各分支按有效 key lerp。"""
    out = base.copy()
    for branch, key in zip(branches, keys):
        k = np.ones_like(out[..., :1]) if key is None else key[..., :1]
        k = np.clip(k, 0.0, 1.0)
        out[..., :3] = out[..., :3] * (1.0 - k) + branch[..., :3] * k
    return out


_BLEND_MODES_CPU = {
    "Normal": lambda a, b, op: a * (1 - op) + b * op,
    "Add": lambda a, b, op: a + b * op,
    "Subtract": lambda a, b, op: a - b * op,
    "Multiply": lambda a, b, op: a * (1 - op + op * b),
    "Screen": lambda a, b, op: 1.0 - (1.0 - a) * (1.0 - b * op),
}

# v1.1（spec §9）：剩余模式 = mix(bottom, apply_blend_mode(bottom, top),
# opacity)——与 v1 内嵌式逐模式代数等价。权威 =
# BLEND_MODES_SNIPPET.apply_blend_mode（b=bottom，a=top）。HSV 四模式按
# 权威文本无符号防护。
_BLEND_EPS = 1e-3


def _overlay(b, a):
    return np.where(b < 0.5, 2.0 * b * a,
                    1.0 - 2.0 * (1.0 - b) * (1.0 - a))


def _soft_light(b, a):
    return np.where(a < 0.5,
                    2.0 * b * a + b * b * (1.0 - 2.0 * a),
                    np.sqrt(np.maximum(b, 0.0)) * (2.0 * a - 1.0)
                    + 2.0 * b * (1.0 - a))


def _hsv_recompose(b, a, h_from_a, s_from_a, v_from_a):
    """HSV 四模式（无符号防护，与权威一致）。"""
    hsv_b = _rgb_to_hsv(b)
    hsv_a = _rgb_to_hsv(a)
    h = hsv_a[..., 0] if h_from_a else hsv_b[..., 0]
    s = hsv_a[..., 1] if s_from_a else hsv_b[..., 1]
    v = hsv_a[..., 2] if v_from_a else hsv_b[..., 2]
    return _hsv_to_rgb(np.stack([h, s, v], axis=-1))


def _apply_blend_mode_cpu(b, a, mode):
    """BLEND_MODES_SNIPPET.apply_blend_mode 的 numpy 移植（逐条同式）。"""
    if mode == "Multiply":
        return b * a
    if mode == "Screen":
        return 1.0 - (1.0 - b) * (1.0 - a)
    if mode == "Overlay":
        return _overlay(b, a)
    if mode == "Soft Light":
        return _soft_light(b, a)
    if mode == "Hard Light":
        return _overlay(a, b)
    if mode == "Color Dodge":
        return b / np.maximum(_BLEND_EPS, 1.0 - a)
    if mode == "Color Burn":
        return 1.0 - (1.0 - b) / np.maximum(_BLEND_EPS, a)
    if mode == "Darken":
        return np.minimum(b, a)
    if mode == "Lighten":
        return np.maximum(b, a)
    if mode == "Difference":
        return np.abs(b - a)
    if mode == "Exclusion":
        return b + a - 2.0 * b * a
    if mode == "Hue":
        return _hsv_recompose(b, a, True, False, False)
    if mode == "Saturation":
        return _hsv_recompose(b, a, False, True, False)
    if mode == "Color":
        return _hsv_recompose(b, a, True, True, False)
    if mode == "Luminosity":
        return _hsv_recompose(b, a, False, False, True)
    if mode == "Addition":
        return b + a
    if mode == "Subtract (Clamped)":
        return np.maximum(b - a, 0.0)
    if mode == "Divide":
        return b / np.maximum(_BLEND_EPS, a)
    return a  # Normal / 未知（与 GLSL mode<=0 同路径）


def layer_mix(slots: Sequence[np.ndarray], opacities: Sequence[float],
              modes: Sequence[str]) -> np.ndarray:
    """spec §2/§9（v1.1）：由下到上折叠（slot 0 为底）。

    v1 五模式走内嵌式；v1.1 新模式走 ``mix(bottom, apply_blend_mode(
    bottom, top), opacity)``（两者代数等价，见 spec §9）。
    """
    if not slots:
        raise ValueError("layer_mix 需要至少一个 slot")
    out = slots[0].copy()
    for slot, opacity, mode in zip(slots[1:], opacities, modes):
        formula = _BLEND_MODES_CPU.get(mode)
        if formula is not None:
            out[..., :3] = formula(out[..., :3], slot[..., :3], float(opacity))
            continue
        if mode in _BLEND_MODES_V11:
            op = float(opacity)
            blended = _apply_blend_mode_cpu(out[..., :3], slot[..., :3],
                                            mode)
            out[..., :3] = (out[..., :3] * (1.0 - op) + blended * op)
            continue
        raise UnsupportedNode(f"Layer 模式 {mode} 未在规格冻结")
    return out


#: v1.1 新增模式集（spec §9 表）
_BLEND_MODES_V11 = frozenset({
    "Overlay", "Soft Light", "Hard Light", "Color Dodge", "Color Burn",
    "Darken", "Lighten", "Difference", "Exclusion", "Hue", "Saturation",
    "Color", "Luminosity", "Addition", "Subtract (Clamped)", "Divide",
})


def key_mix(keys: Sequence[np.ndarray], mode: str = "add") -> np.ndarray:
    """spec §3：key 布尔混合（有界 0-1）。"""
    if not keys:
        raise ValueError("key_mix 需要至少一路 key")
    out = np.clip(keys[0][..., :1], 0.0, 1.0)
    for key in keys[1:]:
        key = np.clip(key[..., :1], 0.0, 1.0)
        if mode == "add":
            out = np.maximum(out, key)
        elif mode == "subtract":
            out = np.clip(out - key, 0.0, 1.0)
        elif mode == "intersect":
            out = out * key
        else:
            raise UnsupportedNode(f"Key Mixer 模式 {mode} 未冻结")
    return out


def outside_key(referenced_key: np.ndarray) -> np.ndarray:
    """spec §4：补集。"""
    return np.clip(1.0 - referenced_key[..., :1], 0.0, 1.0)


def split_rgb(image: np.ndarray) -> dict[str, np.ndarray]:
    """spec §5：R/G/B 灰度载体（alpha 随 R 通道）。"""
    out = {}
    for channel in ("r", "g", "b"):
        value = image[..., "rgb".index(channel)]
        plane = np.empty_like(image)
        plane[..., :3] = value[..., None]
        plane[..., 3:] = image[..., 3:]
        out[channel] = plane
    return out


def combine_rgb(r: np.ndarray, g: np.ndarray, b: np.ndarray) -> np.ndarray:
    """spec §5：三合（alpha 取 R 载体）。"""
    out = np.empty_like(r)
    out[..., 0] = r[..., 0]
    out[..., 1] = g[..., 1]
    out[..., 2] = b[..., 2]
    out[..., 3:] = r[..., 3:]
    return out


def corrector_oracle(image: np.ndarray, params: Mapping[str, Any],
                     mask: np.ndarray | None = None) -> np.ndarray:
    """受限 Corrector oracle：neutral / exposure / saturation（B10 声明）。

    完整 Corrector CPU 实现（曲线/LUT/qualifier/窗口/插件）在 B12；
    其他参数出现即 ``UnsupportedNode``（不静默忽略）。
    """
    allowed = {"key_source_mode", "exposure", "saturation", "grade",
               "opacity", "blend_mode", "grade_patch",
               "visible", "name", "node_id"}
    unknown = set(params) - allowed
    if unknown:
        raise UnsupportedNode(f"Corrector 参数域未支持: {sorted(unknown)}")
    grade = params.get("grade") or {}
    exposure = float(grade.get("exposure", params.get("exposure", 0.0)) or 0.0)
    saturation = float(grade.get("saturation",
                                 params.get("saturation", 1.0)) or 1.0)
    graded = image.astype(np.float32) * (2.0 ** exposure)
    if saturation != 1.0:
        luma = (graded[..., :3] * (0.2126, 0.7152, 0.0722)).sum(axis=-1,
                                                                keepdims=True)
        graded[..., :3] = luma + (graded[..., :3] - luma) * saturation
    if mask is not None:
        k = np.clip(mask[..., :1], 0.0, 1.0)
        graded = image * (1.0 - k) + graded * k
    return graded


# ── 生产域 Corrector（B12.2）：GRADING/CURVES SNIPPET 的 numpy 移植 ──────

_F32 = np.float32
_LUMA_601 = (_F32(0.299), _F32(0.587), _F32(0.114))


def _luma601(color: np.ndarray) -> np.ndarray:
    return (color[..., 0] * _LUMA_601[0] + color[..., 1] * _LUMA_601[1]
            + color[..., 2] * _LUMA_601[2])


def _smoothstep_np(e0: float, e1: float, x: np.ndarray) -> np.ndarray:
    t = np.clip((x - _F32(e0)) / _F32(_F32(e1) - _F32(e0)), _F32(0.0),
                _F32(1.0))
    return t * t * (_F32(3.0) - _F32(2.0) * t)


def _rgb_to_hsv(color: np.ndarray) -> np.ndarray:
    """COMMON_DEFINITIONS.rgb2hsv 的 numpy 移植（逐条同序）。

    输入 (..., 3) 非负（hue shift 前置 abs 防护与 GLSL 一致）。
    """
    c_r, c_g, c_b = color[..., 0], color[..., 1], color[..., 2]
    # p = mix(vec4(c.bg, K.wz), vec4(c.gb, K.xy), step(c.b, c.g))
    sel = (c_b <= c_g).astype(_F32)  # step(c.b, c.g)：g>=b 取 1
    p_x = c_b * (1 - sel) + c_g * sel          # p.x（A=c.b / B=c.g）
    p_y = c_g * (1 - sel) + c_b * sel          # p.y（A=c.g / B=c.b）
    # A 尾部 = K.wz = (-1, 2/3)；B 尾部 = K.xy = (0, -1/3)
    p_z = _F32(-1.0) * (1 - sel) + _F32(0.0) * sel
    p_w = _F32(2.0 / 3.0) * (1 - sel) + _F32(-1.0 / 3.0) * sel
    # q = mix(vec4(p.xyw, c.r), vec4(c.r, p.yzx), step(p.x, c.r))
    sel2 = (p_x <= c_r).astype(_F32)  # step(p.x, c.r)
    q_x = p_x * (1 - sel2) + c_r * sel2
    q_y = p_y * (1 - sel2) + p_y * sel2            # q.y = p.y 两支同值
    q_z = p_w * (1 - sel2) + p_z * sel2            # q.z = p.w / p.z
    q_w = c_r * (1 - sel2) + p_x * sel2            # q.w = c.r / p.x
    d = q_x - np.minimum(q_w, q_y)
    e = _F32(1.0e-10)
    h = np.abs(q_z + (q_w - q_y) / (_F32(6.0) * d + e))
    s = d / (q_x + e)
    return np.stack([h, s, q_x], axis=-1)


def _hsv_to_rgb(hsv: np.ndarray) -> np.ndarray:
    """COMMON_DEFINITIONS.hsv2rgb 的 numpy 移植（逐条同序）。"""
    k_xyz = np.array([1.0, 2.0 / 3.0, 1.0 / 3.0], dtype=_F32)
    k_www = _F32(3.0)
    shifted = hsv[..., 0][..., None] + k_xyz       # c.xxx + K.xyz
    fract = shifted - np.floor(shifted)            # fract(...)
    p = np.abs(fract * _F32(6.0) - k_www)
    p = np.clip(p - np.array([1.0, 1.0, 1.0], dtype=_F32), _F32(0.0),
                _F32(1.0))
    v = hsv[..., 2][..., None]
    s = hsv[..., 1][..., None]
    kxxx = np.array([1.0, 1.0, 1.0], dtype=_F32)
    return v * (kxxx * (_F32(1.0) - s) + p * s)


def apply_grading_uniforms_cpu(color: np.ndarray,
                               values: Mapping[str, Any]) -> np.ndarray:
    """GRADING_SNIPPET.apply_grading 的 numpy 移植（基础点态域，逐条同序）。

    ``values`` 为 ``composition_uniform_values`` 的产物（与 GL 同一
    提取路径）。邻域参数（mid_detail/sharpness）非零时显式
    :class:`UnsupportedNode`（非点态，CPU oracle 不近似）。
    """
    if float(values.get("u_mid_detail", 0.0)) != 0.0 or float(
            values.get("u_sharpness", 0.0)) != 0.0:
        raise UnsupportedNode(
            "mid_detail/sharpness 为邻域采样，不在 CPU 点态基础域")
    c = np.ascontiguousarray(color, dtype=_F32)

    # 1. Exposure
    exposure = _F32(values.get("u_exposure", 0.0))
    exp_stops = _F32((exposure / 100.0) * (exposure / 100.0)) * _F32(6.0)
    if exposure < 0:
        exp_stops = -exp_stops
    c = c * _F32(np.power(_F32(2.0), exp_stops))

    # 2. White Balance
    temp = _F32(values.get("u_temp", 0.0))
    tint = _F32(values.get("u_tint", 0.0))
    c[..., 0] *= _F32(1.0 + temp / 200.0)
    c[..., 1] *= _F32(1.0 - tint / 200.0)
    c[..., 2] *= _F32(1.0 - temp / 200.0)

    # 3. Contrast（pivot 0.18）
    contrast = _F32(values.get("u_contrast", 0.0))
    if contrast != 0.0:
        factor = _F32(1.0 + contrast / 100.0)
        c = (np.sign(c) * np.power(np.abs(c) / _F32(0.18), factor)
             * _F32(0.18)).astype(_F32)

    # 4. Lift / Gamma / Gain（色轮已由 params_to_rgb 转为 RGB 增益）
    lift = np.asarray(values.get("u_lift", (1.0, 1.0, 1.0)), dtype=_F32)
    gamma = np.asarray(values.get("u_gamma", (1.0, 1.0, 1.0)), dtype=_F32)
    gain = np.asarray(values.get("u_gain", (1.0, 1.0, 1.0)), dtype=_F32)
    l = (lift - _F32(1.0)) * _F32(0.2)
    c = c * gain * (_F32(1.0) - l) + l
    safe_gamma = np.maximum(gamma, _F32(0.01))
    gm = _F32(1.0) / safe_gamma          # pow(2, -log2(g)) == 1/g
    x = c
    res = np.where((x > 0.0) & (x <= 1.0), np.power(np.maximum(x, _F32(1e-20)),
                                                    gm),
                   np.where(x > 1.0, gm * (x - _F32(1.0)) + _F32(1.0),
                            gm * x)).astype(_F32)
    c = res

    # 安全修复：暗部负值软性保护（luma 在压缩前计算，与 GLSL 同序）
    luma = _luma601(c)
    neg = luma < 0.0
    if neg.any():
        compressed = np.where(c < -0.5,
                              _F32(-0.5) - (np.abs(c + _F32(0.5))
                                            * _F32(0.1)), c).astype(_F32)
        min_c = np.minimum(np.minimum(compressed[..., 0], compressed[..., 1]),
                           compressed[..., 2])
        max_c = np.maximum(np.maximum(compressed[..., 0], compressed[..., 1]),
                           compressed[..., 2])
        need = neg & ((max_c - min_c) > _F32(0.15)) & (min_c < 0.0)
        balance = np.clip(np.abs(min_c) * _F32(0.25), _F32(0.0), _F32(0.15))
        mixed = (compressed * (_F32(1.0) - balance)[..., None]
                 + luma[..., None] * balance[..., None])
        c = np.where(need[..., None], mixed, compressed).astype(_F32)

    # 5. Shadows（power 曲线，0/1 不动点）
    shadow = _F32(values.get("u_shadow", 0.0))
    if shadow != 0.0:
        shadow_factor = _F32(1.0 + shadow / 100.0)
        if shadow_factor > 0.0:
            clamped = np.clip(c, _F32(0.0), _F32(1.0))
            c = (np.power(clamped, _F32(1.0) / shadow_factor)
                 + (c - clamped)).astype(_F32)

    # 6. Highlights（Reinhard 压缩/提升）
    highlights = _F32(values.get("u_highlights", 0.0))
    if highlights != 0.0:
        max_c = np.maximum(np.maximum(c[..., 0], c[..., 1]), c[..., 2])
        gate = max_c > _F32(0.1)
        if highlights < 0.0:
            k = np.abs(highlights / 100.0) * _F32(2.0)
            mapped = _F32(0.1) + (max_c - _F32(0.1)) / (
                _F32(1.0) + k * (max_c - _F32(0.1)))
        else:
            k = (highlights / 100.0) * _F32(0.5)
            mapped = _F32(0.1) + (max_c - _F32(0.1)) * (_F32(1.0) + k)
        scale = np.where(gate, mapped / np.where(gate, max_c, _F32(1.0)),
                         _F32(1.0)).astype(_F32)
        c = (c * scale[..., None]).astype(_F32)

    # 6.5 色调区间饱和度
    ls = _F32(values.get("u_lift_saturation", 0.0))
    gs = _F32(values.get("u_gamma_saturation", 0.0))
    hs_ = _F32(values.get("u_gain_saturation", 0.0))
    if ls != 0.0 or gs != 0.0 or hs_ != 0.0:
        tonal_lum = np.clip(_luma601(c), _F32(0.0), _F32(1.0))
        w_shadow = _F32(1.0) - _smoothstep_np(0.0, 0.5, tonal_lum)
        w_mid = (_smoothstep_np(0.0, 0.5, tonal_lum)
                 * (_F32(1.0) - _smoothstep_np(0.5, 1.0, tonal_lum)))
        w_high = _smoothstep_np(0.5, 1.0, tonal_lum)
        amount = ls * w_shadow + gs * w_mid + hs_ * w_high
        if np.any(amount != 0.0):
            gray = _luma601(c)
            f = (_F32(1.0) + amount / _F32(100.0))[..., None]
            c = (gray[..., None] * (_F32(1.0) - f) + c * f).astype(_F32)

    # 7. Vibrance
    vibrance = _F32(values.get("u_vibrance", 0.0))
    if vibrance != 0.0:
        gray = _luma601(c)
        max_c = np.maximum(np.maximum(c[..., 0], c[..., 1]), c[..., 2])
        sat = max_c - gray
        boost = (vibrance / _F32(100.0)) * (
            _F32(1.0) - np.clip(sat, _F32(0.0), _F32(1.0)))
        # mix(color, gray, -boost) = color + boost*(color - gray)
        c = (c + boost[..., None] * (c - gray[..., None])).astype(_F32)

    # 9. Hue Shift（符号防护 HSV 旋转）
    hue_shift = _F32(values.get("u_hue_shift", 0.0))
    if hue_shift != 0.0:
        sign_c = np.sign(c)
        hsv = _rgb_to_hsv(np.abs(c))
        hsv[..., 0] = hsv[..., 0] + hue_shift / _F32(360.0)
        hsv[..., 0] -= np.floor(hsv[..., 0])
        c = (sign_c * _hsv_to_rgb(hsv)).astype(_F32)

    # 10. Saturation
    saturation = _F32(values.get("u_saturation", 0.0))
    if saturation != 0.0:
        gray = _luma601(c)
        f = _F32(1.0 + saturation / 100.0)
        c = (gray[..., None] * (_F32(1.0) - f) + c * f).astype(_F32)
    return c


def apply_curves_cpu(color: np.ndarray, lut: np.ndarray) -> np.ndarray:
    """CURVES_SNIPPET.apply_curves 的 numpy 移植（256×3 LUT）。

    GL 经 LINEAR 纹理在 texel 中心采样（数学上等价最近邻取整）；区间外
    线性外推 + 暗部/高光软性保护 + 通道平衡（与 GLSL 逐条同序）。
    """
    lut = np.asarray(lut, dtype=_F32).reshape(256, 3)
    res = np.empty_like(color)
    v0, v1 = lut[0], lut[1]
    v254, v255 = lut[254], lut[255]
    slope_low = (v1 - v0) * _F32(255.0)
    slope_high = (v255 - v254) * _F32(255.0)
    for ch in range(3):
        x = color[..., ch]
        idx = np.clip((x * _F32(255.0) + _F32(0.5)).astype(np.int64), 0, 255)
        sampled = lut[idx, ch]
        out = np.where(x < 0.0, v0[ch] + x * slope_low[ch],
                       np.where(x > 1.0,
                                v255[ch] + (x - _F32(1.0)) * slope_high[ch],
                                sampled)).astype(_F32)
        # 暗部/高光软性保护
        out = np.where(out < -0.5,
                       _F32(-0.5) - (np.abs(out + _F32(0.5)) * _F32(0.1)),
                       out).astype(_F32)
        out = np.where(out > 2.0,
                       _F32(2.0) + (out - _F32(2.0)) * _F32(0.1),
                       out).astype(_F32)
        res[..., ch] = out
    # 通道平衡
    min_c = np.minimum(np.minimum(res[..., 0], res[..., 1]), res[..., 2])
    max_c = np.maximum(np.maximum(res[..., 0], res[..., 1]), res[..., 2])
    need = ((max_c - min_c) > _F32(0.15)) & (min_c < 0.0)
    balance = np.clip(np.abs(min_c) * _F32(0.3), _F32(0.0), _F32(0.2))
    mixed = res - (res - min_c[..., None]) * balance[..., None]
    return np.where(need[..., None], mixed, res).astype(_F32)


#: 生产基础域允许的 grading 参数键（点态）；其余键出现即 unsupported
CPU_GRADING_BASIC_KEYS = frozenset({
    "exposure", "temp", "tint", "contrast", "saturation", "vibrance",
    "shadow", "highlights", "mid_detail", "sharpness", "hue_shift",
    "lift_saturation", "gamma_saturation", "gain_saturation",
    "lift", "gamma", "gain",
})

#: GL composition 忽略的别名死键（Layer 默认 dict 携带；CPU 同样忽略，
#: 与 GL 有效域一致——不因死键拒绝可执行层）
_CPU_DEAD_GRADING_KEYS = frozenset({"temperature", "shadows"})

#: fx/blur 组的中性值（enabled/blur_enabled 关闭且幅度全零 = GL no-op）
def _fx_params_neutral(fx: Mapping[str, Any]) -> bool:
    if fx.get("enabled") or fx.get("blur_enabled"):
        return False
    for key, value in fx.items():
        if key in ("enabled", "blur_enabled", "mode", "iterations"):
            continue
        try:
            if float(value) != 0.0:
                return False
        except (TypeError, ValueError):
            return False
    return True


def production_layer_domain_errors(layer: Mapping[str, Any]) -> list[str]:
    """生产域 Corrector 的能力检查（CPU/Metal 共用，B12.1 矩阵依据）。

    返回 unsupported 原因列表（空 = 基础域可执行）。**fail-closed** 且
    **与 GL 有效域一致**：GL 忽略的别名死键同样忽略；fx/blur 组在中性
    值（关闭 + 幅度零）时等价 no-op，不拒绝。插件、非中性 mixer/warper/
    LUT3D/fx/hsl 曲线/soft-clip 曲线、邻域参数、非 Normal 混合、
    curves_data 未预构建 LUT 都显式列出，不静默近似。
    """
    errors: list[str] = []
    grading = layer.get("grading_params")
    grading = grading if isinstance(grading, Mapping) else {}
    unknown = sorted(set(grading) - CPU_GRADING_BASIC_KEYS
                     - _CPU_DEAD_GRADING_KEYS)
    if unknown:
        errors.append(f"grading 参数未支持: {unknown}")
    if float(grading.get("mid_detail") or 0.0) != 0.0 or float(
            grading.get("sharpness") or 0.0) != 0.0:
        errors.append("mid_detail/sharpness 为邻域采样（非点态基础域）")
    if layer.get("plugins"):
        errors.append("插件无 CPU/Metal 实现（显式 unsupported）")
    if layer.get("_plugin_bindings"):
        errors.append("执行器级插件绑定仅 GL 路径支持")
    mixer = layer.get("mixer_params")
    if isinstance(mixer, Mapping) and mixer:
        errors.append("mixer_params 不在基础域")
    if layer.get("warper_params") is not None:
        errors.append("warper_params 不在基础域")
    if layer.get("active_lut_path"):
        errors.append("LUT3D 不在基础域")
    hsl_data = layer.get("hsl_curve_data")
    if layer.get("hsl_curve_lut") is not None or (
            isinstance(hsl_data, Mapping) and hsl_data):
        errors.append("HSL 曲线不在基础域")
    soft_clip = layer.get("curves_soft_clip")
    if isinstance(soft_clip, Mapping) and soft_clip:
        from src.features.color.core.curves_soft_clip import (
            default_curves_soft_clip,
        )
        if dict(soft_clip) != default_curves_soft_clip():
            errors.append("curves_soft_clip 非默认（不在基础域）")
    fx = layer.get("effects_params")
    if isinstance(fx, Mapping) and fx and not _fx_params_neutral(fx):
        errors.append("fx/blur 组非中性（不在基础域）")
    if layer.get("curves_data") and layer.get("curves_lut") is None:
        errors.append("curves_data 未预构建 curves_lut（GL 侧在 "
                      "prepare_corrector 构建；CPU/Metal 要求显式 LUT）")
    blend = str(layer.get("blend_mode", "Normal"))
    if blend != "Normal":
        errors.append(f"blend_mode={blend} 未在 CPU/Metal 基础域冻结")
    return errors


def corrector_production_oracle(image: np.ndarray, layer: Mapping[str, Any],
                                mask: np.ndarray | None = None,
                                *, width: int = 0, height: int = 0
                                ) -> np.ndarray:
    """生产域 Corrector CPU oracle（GL executor 同一参数路径）。

    参数提取复用 ``composition_uniform_values``（与 GL uniform 同源），
    数学逐条移植 GRADING/CURVES SNIPPET；epilogue 为 composition main
    的 ``mix(input, blend(input, graded), mask*opacity)``（Normal）。
    基础域外参数经 :func:`production_layer_domain_errors` 显式拒绝。
    """
    errors = production_layer_domain_errors(layer)
    if errors:
        raise UnsupportedNode("; ".join(errors))
    from src.features.color.core.composition_uniforms import (
        composition_uniform_values,
    )
    h, w = image.shape[:2]
    values = composition_uniform_values(
        dict(layer), width=width or w, height=height or h)
    src = np.ascontiguousarray(image, dtype=_F32)
    graded = apply_grading_uniforms_cpu(src[..., :3], values)
    lut = layer.get("curves_lut")
    if lut is not None:
        graded = apply_curves_cpu(graded, lut)
    opacity = _F32(values.get("u_opacity", 1.0))
    k = opacity if mask is None else (
        np.clip(mask[..., :1], _F32(0.0), _F32(1.0)).astype(_F32) * opacity)
    out = src.copy()
    out[..., :3] = (src[..., :3] * (_F32(1.0) - k) + graded * k).astype(_F32)
    return out


#: CPU 后端能力目录（B12.1 矩阵数据源；op → exact/fallback/unsupported）
CPU_CAPABILITY_CATALOG: Mapping[str, str] = {
    "source": "exact",
    "output": "exact",
    "corrector": "fallback",   # 基础点态域 exact；插件/邻域/LUT 显式拒绝
    "shared_corrector": "fallback",
    "parallel_mixer": "exact",
    "layer_mixer": "exact",
    "key_mixer": "exact",
    "outside": "fallback",
    "splitter": "exact",
    "combiner": "exact",
    "alpha_output": "exact",
    "compound": "unsupported",
}


# ── plan 级 CPU 执行（串行 + 分支） ─────────────────────────────────────

@dataclass
class CpuExecution:
    output: np.ndarray
    alpha_output: np.ndarray | None = None


def _corrector_for_params(params: Mapping[str, Any]):
    """corrector 分派：生产形态（grading_params/curves_lut）走生产 oracle，
    否则 B10 构造用简化域（corrector_oracle）。"""
    if "grading_params" in params or "curves_lut" in params:
        return corrector_production_oracle
    return corrector_oracle


def run_plan_cpu(plan: ColorGraphRenderPlan, source: np.ndarray,
                 params_of: Callable[[RenderPass], Mapping[str, Any]]
                 ) -> CpuExecution:
    """按 plan 在 CPU 执行（生产形态 Corrector 走生产 oracle）。"""
    outputs: dict[str, np.ndarray] = {}
    alpha_result: np.ndarray | None = None

    def _first(port_name: str):
        for port, ref in render_pass.inputs:
            if port == port_name and ref is not None:
                return outputs.get(ref.node_id)
        return None

    for render_pass in plan.passes:
        params = params_of(render_pass) or {}

        if render_pass.op == "source":
            outputs[render_pass.node_id] = source.astype(np.float32).copy()
        elif render_pass.op in ("corrector", "shared_corrector"):
            if params.get("visible") is False:
                outputs[render_pass.node_id] = _first("image")
                continue
            base = _first("image")
            if base is None:
                base = source
            outputs[render_pass.node_id] = _corrector_for_params(params)(
                base, params)
        elif render_pass.op == "parallel_mixer":
            base = _first("base")
            if base is None:
                base = source
            branches = [outputs[ref.node_id]
                        for port, ref in render_pass.inputs
                        if port == "branch" and ref is not None]
            keys = [None] * len(branches)
            outputs[render_pass.node_id] = parallel_mix(base, branches, keys)
        elif render_pass.op == "layer_mixer":
            slots = [outputs[ref.node_id]
                     for _port, ref in sorted(
                         render_pass.inputs,
                         key=lambda pr: (pr[1].slot_id if pr[1] else ""))
                     if ref is not None]
            from src.features.color.core.color_graph_param_source import (
                mixer_composite_params,
            )
            # mixer 的合成参数来自节点 params（provider 只服务 Corrector
            # DTO；混用 provider 载荷会把所有 mixer 模式静默回退 Normal）
            blend_mode, opacity = mixer_composite_params(render_pass.params)
            opacities = [opacity] * max(0, len(slots) - 1)
            modes = [blend_mode] * max(0, len(slots) - 1)
            outputs[render_pass.node_id] = layer_mix(slots, opacities, modes)
        elif render_pass.op == "key_mixer":
            keys = [outputs[ref.node_id]
                    for _port, ref in render_pass.inputs if ref is not None]
            outputs[render_pass.node_id] = key_mix(
                keys, str(render_pass.params.get("mode", "add")))
        elif render_pass.op == "outside":
            base = _first("image")
            if base is None:
                base = source
            referenced = _first("key_in")
            complement = (outside_key(referenced) if referenced is not None
                          else np.zeros_like(source[..., :1]))
            outputs[render_pass.node_id] = _corrector_for_params(params)(
                base, params, mask=complement)
        elif render_pass.op == "splitter":
            base = _first("image")
            if base is None:
                base = source
            planes = split_rgb(base)
            outputs[render_pass.node_id] = planes   # dict 载体（r/g/b）
        elif render_pass.op == "combiner":
            splitter_out = None
            for port, ref in render_pass.inputs:
                if ref is not None:
                    splitter_out = outputs.get(ref.node_id)
                    break
            if isinstance(splitter_out, dict):
                outputs[render_pass.node_id] = combine_rgb(
                    splitter_out["r"], splitter_out["g"], splitter_out["b"])
            else:
                outputs[render_pass.node_id] = source.copy()
        elif render_pass.op == "output":
            ref = next((outputs[r.node_id]
                        for _p, r in render_pass.inputs if r is not None),
                       source)
            outputs[render_pass.node_id] = np.array(ref, copy=True)
        elif render_pass.op == "alpha_output":
            key = next((outputs[r.node_id]
                        for _p, r in render_pass.inputs if r is not None),
                       None)
            alpha_result = (np.clip(key[..., :1], 0.0, 1.0)
                            if key is not None else None)
            outputs[render_pass.node_id] = source.copy()
        else:
            raise UnsupportedNode(f"CPU oracle 未实现算子 {render_pass.op}")

    final = plan.passes[-1]
    result = outputs.get(final.node_id, source)
    return CpuExecution(output=result, alpha_output=alpha_result)
