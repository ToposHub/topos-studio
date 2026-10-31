"""F06 multiscale_halation —— 独立 CPU 数学参考（oracle）。

与 filters/multiscale_halation/README.md 的**真多尺度**算法定义逐式同构
（references/oracle_conventions.md）：
- 软膝高光提取（点态）：m = smoothstep(thr, thr+0.35, luma)，E = img * m；
- 两尺度扩散（均为固定 12-tap 黄金角环采样，等面积距离 sqrt((i+0.5)/12)、
  方向 i*黄金角）：大尺度半径 = 2R 权重 0.6，小尺度 = R 权重 0.4，
  halo = 0.6*big + 0.4*small；
- 红色散射重着色：w = red_scatter，scatter = (halo.r*(1+w), halo.g,
  halo.b*(1-0.5*w))；
- 加性叠加：out = img + scatter * strength；strength=0 精确恒等。
- 半径映射沿用 Halation_Extended 口径：R_px = max(2, W*(0.004+0.026*radius))。

多 pass 语义：E 场提取 → 双尺度扩散是天然多 pass 结构（E 是独立 pass 的
中间场）；本 oracle 即该结构的 float64 串行化。另附
``evaluate_degraded_single_scale``：单尺度（小尺度 R）逐 tap 门控的降级
模式——与暂存 .tglsl 草稿逐式同构（降级模式与多 pass 版仅在软膝过渡带
内逐 tap 门控 ≠ E 场采样时有差异）。

边界：双线性采样 + clamp-to-edge（坐标钳到 [0, n-1]，与 GL LINEAR +
半纹素钳制等价）；无随机流。
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

LUMA_W = np.array([0.2126, 0.7152, 0.0722], dtype=np.float64)

GOLDEN_ANGLE = 2.399963229728653   # 137.50776°（弧度）
N_TAPS = 12
KNEE = 0.35                         # 软膝宽度（scene-linear）
BIG_WEIGHT = 0.6                    # 大尺度（2R）权重
SMALL_WEIGHT = 0.4                  # 小尺度（R）权重

DEFAULTS: dict[str, Any] = {
    "strength": 0.5,
    "radius": 0.5,
    "red_scatter": 0.5,
    "threshold": 0.55,
}


def _smoothstep(a: float, b: float, x: np.ndarray) -> np.ndarray:
    t = np.clip((x - a) / (b - a), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _bilinear(field: np.ndarray, ys: np.ndarray, xs: np.ndarray) -> np.ndarray:
    """clamp-to-edge 双线性采样。field (H,W) 或 (H,W,C)；ys/xs 浮点像素序
    坐标（像素中心 = 整数索引），同形；返回按 field 通道结构。"""
    h, w = field.shape[0], field.shape[1]
    ys = np.clip(ys, 0.0, h - 1.0)
    xs = np.clip(xs, 0.0, w - 1.0)
    y0 = np.floor(ys).astype(np.int64)
    x0 = np.floor(xs).astype(np.int64)
    y1 = np.minimum(y0 + 1, h - 1)
    x1 = np.minimum(x0 + 1, w - 1)
    fy = ys - y0
    fx = xs - x0
    if field.ndim == 3:
        fy = fy[..., None]
        fx = fx[..., None]
    return (field[y0, x0] * (1.0 - fx) * (1.0 - fy)
            + field[y0, x1] * fx * (1.0 - fy)
            + field[y1, x0] * (1.0 - fx) * fy
            + field[y1, x1] * fx * fy)


def _ring_offsets(radius_px: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """12-tap 黄金角环：返回 (dy_unit, dx_unit, dist) 各形 (12,)。"""
    idx = np.arange(N_TAPS)
    ang = idx * GOLDEN_ANGLE
    dist = radius_px * np.sqrt((idx + 0.5) / N_TAPS)
    return np.sin(ang), np.cos(ang), dist


def _diffuse(field: np.ndarray, radius_px: float) -> np.ndarray:
    """对 (H,W,3) 场做 12-tap 黄金角环平均（等权重，边界 clamp）。"""
    h, w = field.shape[0], field.shape[1]
    sy, cx, dist = _ring_offsets(radius_px)
    acc = np.zeros((h, w, 3), dtype=np.float64)
    yy, xx = np.meshgrid(np.arange(h, dtype=np.float64),
                         np.arange(w, dtype=np.float64), indexing="ij")
    for i in range(N_TAPS):
        acc += _bilinear(field, yy + sy[i] * dist[i], xx + cx[i] * dist[i])
    return acc / N_TAPS


def _radius_px(width: int, radius: float) -> float:
    return max(2.0, width * (0.004 + 0.026 * radius))


def _params(params: Mapping[str, Any] | None) -> dict[str, Any]:
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    return p


def evaluate(image: np.ndarray, params: Mapping[str, Any] | None = None) -> np.ndarray:
    """真多尺度实现（canonical）：E 场提取 → 双尺度扩散 → 红重着色 → 加性。"""
    p = _params(params)
    img = np.asarray(image, dtype=np.float64)
    h, w = img.shape[0], img.shape[1]

    strength = float(p["strength"])
    if strength < 0.001:
        return img.astype(np.float32)

    # pass1（点态）：软膝高光能量场
    luma = img @ LUMA_W
    m = _smoothstep(float(p["threshold"]), float(p["threshold"]) + KNEE, luma)
    energy = img * m[..., None]

    # pass2/pass3：双尺度 12-tap 黄金角环扩散
    r_small = _radius_px(w, float(p["radius"]))
    big = _diffuse(energy, 2.0 * r_small)
    small = _diffuse(energy, r_small)
    halo = BIG_WEIGHT * big + SMALL_WEIGHT * small

    # 红色散射重着色 + 加性叠加
    wgt = float(p["red_scatter"])
    scatter = np.stack([halo[..., 0] * (1.0 + wgt),
                        halo[..., 1],
                        halo[..., 2] * (1.0 - 0.5 * wgt)], axis=-1)
    return (img + scatter * strength).astype(np.float32)


def evaluate_degraded_single_scale(
    image: np.ndarray, params: Mapping[str, Any] | None = None
) -> np.ndarray:
    """单尺度（小尺度 R）逐 tap 软膝门控——与暂存 .tglsl 草稿逐式同构。"""
    p = _params(params)
    img = np.asarray(image, dtype=np.float64)
    h, w = img.shape[0], img.shape[1]

    strength = float(p["strength"])
    if strength < 0.001:
        return img.astype(np.float32)

    r_small = _radius_px(w, float(p["radius"]))
    sy, cx, dist = _ring_offsets(r_small)
    yy, xx = np.meshgrid(np.arange(h, dtype=np.float64),
                         np.arange(w, dtype=np.float64), indexing="ij")
    acc = np.zeros((h, w, 3), dtype=np.float64)
    for i in range(N_TAPS):
        s = _bilinear(img, yy + sy[i] * dist[i], xx + cx[i] * dist[i])
        m = _smoothstep(float(p["threshold"]),
                        float(p["threshold"]) + KNEE, s @ LUMA_W)
        acc += s * m[..., None]
    halo = acc / N_TAPS

    wgt = float(p["red_scatter"])
    scatter = np.stack([halo[..., 0] * (1.0 + wgt),
                        halo[..., 1],
                        halo[..., 2] * (1.0 - 0.5 * wgt)], axis=-1)
    return (img + scatter * strength).astype(np.float32)
