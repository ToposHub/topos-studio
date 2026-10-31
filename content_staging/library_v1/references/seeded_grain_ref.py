"""F07 seeded_grain —— 独立 CPU 数学参考（oracle），静态版。

遵守 references/oracle_conventions.md：float64 内部、float32 接口、
scene-linear、不 import 宿主渲染/GPU 代码（禁自证）、无随机流。

与 filters/seeded_grain/seeded_grain.tglsl 逐式同构：
- 格点整数散列：murmur3 风格 uint32（乘法异或移位雪崩，与库 fixtures
  _lattice_hash 同式、位宽 32 以与 GLSL 330 uint 逐位一致）；
  seed 以 s = uint(int(seed)) 进入第三乘子通道；彩噪相位 s+1001 / s+2002；
- cell = max(grain_size, 1) 像素格子；像素 (x, y) 的 gp =
  ((x+0.5)/W, (y+0.5)/H) * (W, H) / cell = ((x+0.5)/cell, (y+0.5)/cell)；
  4-tap 平滑 value noise（smoothstep fade，双线性）→ n∈[0,1) → 2n-1；
- 亮度加权（clamp 仅权重域）：weight = 4l(1-l) + (1-l)^2·shadow_bias + l^2·highlight_bias；
- 加性颗粒（不 clamp）：amp = strength·0.12；
  R += (n0+n1·chroma)·amp·weight；G += n0·amp·weight；B += (n0+n2·chroma)·amp·weight。
- GLSL 首行早退 strength < 0.001 在此镜像（逐式同构，含早退阈值）。
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

_U64 = np.uint64
_MASK32 = _U64(0xFFFFFFFF)
_C1, _C2, _C3 = _U64(0x9E3779B1), _U64(0x85EBCA77), _U64(0xC2B2AE3D)
_C4, _C5 = _U64(0x2545F491), _U64(0x9E3779B1)

DEFAULTS: dict[str, Any] = {
    "strength": 0.35,
    "grain_size": 1.5,
    "seed": 7,
    "chroma": 0.25,
    "shadow_bias": 0.25,
    "highlight_bias": 0.15,
}

# 与 GLSL 早退阈值逐字一致
_EARLY_EXIT = 0.001


def lattice_hash_u32(ix: np.ndarray, iy: np.ndarray, seed: int) -> np.ndarray:
    """整数格点 → [0,1)。uint32 murmur3 风格（与 tglsl sg_hash 逐位一致）。"""
    x = ix.astype(np.int64).astype(_U64) & _MASK32
    y = iy.astype(np.int64).astype(_U64) & _MASK32
    s = _U64(int(seed) & 0xFFFFFFFF)
    h = (x * _C1) ^ (y * _C2) ^ (s * _C3)
    h &= _MASK32
    h ^= h >> _U64(15)
    h = (h * _C4) & _MASK32
    h ^= h >> _U64(13)
    h = (h * _C5) & _MASK32
    h ^= h >> _U64(16)
    return h.astype(np.float64) / 4294967296.0


def _value_noise(gp_x: np.ndarray, gp_y: np.ndarray, seed: int) -> np.ndarray:
    """4-tap 平滑 value noise（与 tglsl sg_noise 逐式同构），输入为格坐标。"""
    x0 = np.floor(gp_x)
    y0 = np.floor(gp_y)
    fx = gp_x - x0
    fy = gp_y - y0
    fade_x = fx * fx * (3.0 - 2.0 * fx)
    fade_y = fy * fy * (3.0 - 2.0 * fy)
    xi = x0.astype(np.int64)
    yi = y0.astype(np.int64)
    n00 = lattice_hash_u32(xi, yi, seed)
    n10 = lattice_hash_u32(xi + 1, yi, seed)
    n01 = lattice_hash_u32(xi, yi + 1, seed)
    n11 = lattice_hash_u32(xi + 1, yi + 1, seed)
    a = n00 + (n10 - n00) * fade_x
    b = n01 + (n11 - n01) * fade_x
    return a + (b - a) * fade_y


def evaluate(image: np.ndarray, params: Mapping[str, Any] | None = None) -> np.ndarray:
    """对 (H, W, 3) RGB 数组应用可复现颗粒，返回 float32（静态：seed 决定图案）。"""
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    img64 = np.asarray(image, dtype=np.float64)
    h, w = img64.shape[0], img64.shape[1]

    strength = float(p["strength"])
    if strength < _EARLY_EXIT:
        return np.asarray(image, dtype=np.float32)

    cell = max(float(p["grain_size"]), 1.0)
    s = int(float(p["seed"]))
    chroma_amt = float(p["chroma"])
    shadow_bias = float(p["shadow_bias"])
    highlight_bias = float(p["highlight_bias"])

    # 像素中心格坐标：gp = ((x+0.5)/cell, (y+0.5)/cell)
    ys, xs = np.meshgrid(np.arange(h, dtype=np.float64),
                         np.arange(w, dtype=np.float64), indexing="ij")
    gp_x = (xs + 0.5) / cell
    gp_y = (ys + 0.5) / cell

    n0 = _value_noise(gp_x, gp_y, s) * 2.0 - 1.0
    n1 = _value_noise(gp_x, gp_y, s + 1001) * 2.0 - 1.0
    n2 = _value_noise(gp_x, gp_y, s + 2002) * 2.0 - 1.0

    # 亮度加权（clamp 仅权重域，不触碰输出色）
    luma = np.clip(img64 @ np.array([0.2126, 0.7152, 0.0722]), 0.0, 1.0)
    mid_w = 4.0 * luma * (1.0 - luma)
    shadow_w = (1.0 - luma) * (1.0 - luma)
    highlight_w = luma * luma
    weight = mid_w + shadow_w * shadow_bias + highlight_w * highlight_bias

    amp = strength * 0.12
    mod = amp * weight  # (H, W) 公共幅度场
    out = img64.copy()
    out[..., 0] += (n0 + n1 * chroma_amt) * mod
    out[..., 1] += n0 * mod
    out[..., 2] += (n0 + n2 * chroma_amt) * mod
    return out.astype(np.float32)
