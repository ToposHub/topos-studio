"""F09 dust_scratches —— 独立 CPU 数学参考（oracle），静态程序版。

遵守 references/oracle_conventions.md：float64 内部、float32 接口、
scene-linear、不 import 宿主渲染/GPU 代码（禁自证）、无随机流。

与 filters/dust_scratches/dust_scratches.tglsl 逐式同构：
- cell = max(size,1) 像素格子；像素 (x,y) 格 id = floor((x+0.5, y+0.5)/cell)，
  局部 f = fract(...)；uint32 murmur3 风格散列（同 F07/F08 常量族）；
- 尘点：h0 = h(ix,iy,s) < density → 椭圆斑 c=(0.3+0.4hu, 0.3+0.4hv)
  （相位偏移 (3,11)/(7,5)/(13,2)），e = ((fx-cx)/0.20)²+((fy-cy)/0.26)²，
  t = clamp((1.2-e)/0.5)，spot = t²(3-2t)，深度 0.4+0.4hd；
- 划痕：h(ix,917,s) < density·0.05 且 scratch_length>0 → 列线
  span=clamp(scratch_length)，y0=hl·(1-span)，band [y0, y0+span]（uv.y），
  xs=0.25+0.5hxs，w=0.10+0.08hw，prof=1-q²(3-2q)（q=|fx-xs|/w），
  端部 0.03 羽化 min(smoothstep 两端)；
- atten = opacity·(0.4+0.4hd)·spot + opacity·0.35·prof·edge，钳 0.9；
  out = color·(1-atten)（乘性，HDR/黑位安全）。
- GLSL 首行早退 density < 0.001 在此镜像（逐式同构，含早退阈值）。
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

_U64 = np.uint64
_MASK32 = _U64(0xFFFFFFFF)
_C1, _C2, _C3 = _U64(0x9E3779B1), _U64(0x85EBCA77), _U64(0xC2B2AE3D)
_C4, _C5 = _U64(0x2545F491), _U64(0x9E3779B1)

DEFAULTS: dict[str, Any] = {
    "density": 0.35,
    "scratch_length": 0.6,
    "opacity": 0.6,
    "seed": 7,
    "size": 3.0,
}

# 与 GLSL 早退阈值逐字一致
_EARLY_EXIT = 0.001


def lattice_hash_u32(ix: np.ndarray, iy: np.ndarray, seed: int) -> np.ndarray:
    """整数格点 → [0,1)。uint32 murmur3 风格（与 tglsl ds_hash 逐位一致）。"""
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


def evaluate(image: np.ndarray, params: Mapping[str, Any] | None = None) -> np.ndarray:
    """对 (H, W, 3) RGB 数组应用程序灰尘/划痕，返回 float32（静态：seed 决定图案）。"""
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    img64 = np.asarray(image, dtype=np.float64)
    h, w = img64.shape[0], img64.shape[1]

    density = float(p["density"])
    if density < _EARLY_EXIT:
        return np.asarray(image, dtype=np.float32)

    scratch_length = min(max(float(p["scratch_length"]), 0.0), 1.0)
    opacity = float(p["opacity"])
    s = int(float(p["seed"]))
    cell = max(float(p["size"]), 1.0)

    ys, xs = np.meshgrid(np.arange(h, dtype=np.float64) + 0.5,
                         np.arange(w, dtype=np.float64) + 0.5, indexing="ij")
    gpx = xs / cell
    gpy = ys / cell
    ix = np.floor(gpx).astype(np.int64)
    iy = np.floor(gpy).astype(np.int64)
    fx = gpx - ix
    fy = gpy - iy

    atten = np.zeros((h, w), dtype=np.float64)

    # ===== 尘点 =====
    h0 = lattice_hash_u32(ix, iy, s)
    has_dust = h0 < density
    if has_dust.any():
        hu = lattice_hash_u32(ix + 3, iy + 11, s)
        hv = lattice_hash_u32(ix + 7, iy + 5, s)
        hd = lattice_hash_u32(ix + 13, iy + 2, s)
        cx = 0.3 + 0.4 * hu
        cy = 0.3 + 0.4 * hv
        exd = (fx - cx) / 0.20
        eyd = (fy - cy) / 0.26
        e = exd * exd + eyd * eyd
        t = np.clip((1.2 - e) / 0.5, 0.0, 1.0)
        spot = t * t * (3.0 - 2.0 * t)
        atten += np.where(has_dust, opacity * (0.4 + 0.4 * hd) * spot, 0.0)

    # ===== 划痕 =====
    if scratch_length > 0.0:
        hx = lattice_hash_u32(ix, np.full_like(ix, 917), s)
        has_scratch = hx < density * 0.05
        if has_scratch.any():
            hl = lattice_hash_u32(ix, np.full_like(ix, 511), s)
            hxs = lattice_hash_u32(ix, np.full_like(ix, 733), s)
            hw = lattice_hash_u32(ix, np.full_like(ix, 353), s)
            y0 = hl * (1.0 - scratch_length)
            yn = ys / h  # 像素中心归一 y（= uv.y）
            in_band = (yn >= y0) & (yn <= y0 + scratch_length) & has_scratch
            if in_band.any():
                xs_line = 0.25 + 0.5 * hxs
                width = 0.10 + 0.08 * hw
                dxl = np.abs(fx - xs_line)
                q = np.clip(dxl / width, 0.0, 1.0)
                prof = 1.0 - q * q * (3.0 - 2.0 * q)
                ta = np.clip((yn - y0) / 0.03, 0.0, 1.0)
                ea = ta * ta * (3.0 - 2.0 * ta)
                tb = np.clip((y0 + scratch_length - yn) / 0.03, 0.0, 1.0)
                eb = tb * tb * (3.0 - 2.0 * tb)
                edge = np.minimum(ea, eb)
                atten += np.where(in_band, opacity * 0.35 * prof * edge, 0.0)

    atten = np.minimum(atten, 0.9)
    out = img64 * (1.0 - atten)[..., None]
    return out.astype(np.float32)
