"""Metal 合成链的 CPU 参考实现（numpy，逐 kernel 对齐 MSL）。

用途：

1. **对拍参考**：Metal 真机结果与本实现逐像素比较（容差依据：
   RGBA16F 存储的 half 舍入 ≈ 2^-11 相对误差 + 双线性采样一致）。
2. **无 Metal 设备机器上的语义测试**：tile 无缝、alpha 语义、blend
   模式等验收用例不依赖 GPU 即可运行。

本模块与 `kernels.py` 的 MSL 逐条对应（同一套参数包、同一坐标模型），
不是对 GL 求值器的复刻——GL 对拍经现有测试套件保证，这里的目标是给
Metal 链一个可离线验证的参照。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import numpy as np

from . import graph_plan as gp
from . import roi
from .graph_plan import ExecutionPlan, FrameMeta, PlannedNode, Rect

_EPS = 1e-10


@dataclass
class CpuFrame:
    """CPU 侧的节点输出：pack 区域大小的像素数组 + 元数据。"""

    pixels: np.ndarray            # (pack_h, pack_w, 4) float32
    meta: FrameMeta
    pack: Rect


def _empty_region(region: Rect) -> np.ndarray:
    w = max(0, region[2] - region[0])
    h = max(0, region[3] - region[1])
    return np.zeros((h, w, 4), dtype=np.float32)


def _region_grid(region: Rect) -> Tuple[np.ndarray, np.ndarray]:
    """区域内的绝对画布坐标网格（像素级整数，top-down）。"""
    h = max(0, region[3] - region[1])
    w = max(0, region[2] - region[0])
    ys = np.arange(region[1], region[3], dtype=np.float32)
    xs = np.arange(region[0], region[2], dtype=np.float32)
    grid_x, grid_y = np.meshgrid(xs, ys)
    return grid_x, grid_y


# ── 采样 ─────────────────────────────────────────────────────────────────


def _gather(pixels: np.ndarray, rows: np.ndarray, cols: np.ndarray) -> np.ndarray:
    return pixels[rows, cols]


def sample_bilinear(
    pixels: np.ndarray, pack: Rect,
    abs_x: np.ndarray, abs_y: np.ndarray,
    data_rect: Optional[Rect] = None,
) -> np.ndarray:
    """硬件双线性的 numpy 等价（clamp_to_edge；data_rect 外返回透明）。

    与 MSL ``sample_linear`` / ``sample_window_linear`` 一致：uv =
    (abs - pack.xy + 0.5)/pack.wh。texel i 的中心在连续坐标 i + 0.5，
    因此采样位置 s = abs - pack.xy + 0.5；等价地在「texel 起点」坐标
    t = abs - pack.xy 上做 floor/frac 插值（像素中心处 t 为整数，
    恰好落在 texel 边界——即精确取该 texel，无插值）。
    """
    height, width = pixels.shape[:2]
    sx = abs_x - pack[0]
    sy = abs_y - pack[1]
    inside = np.ones_like(sx, dtype=bool)
    if data_rect is not None:
        inside = (
            (abs_x >= data_rect[0]) & (abs_y >= data_rect[1])
            & (abs_x < data_rect[2]) & (abs_y < data_rect[3])
        )
    x0 = np.floor(sx).astype(np.int64)
    y0 = np.floor(sy).astype(np.int64)
    fx = sx - x0
    fy = sy - y0
    xa = np.clip(x0, 0, width - 1)
    xb = np.clip(x0 + 1, 0, width - 1)
    ya = np.clip(y0, 0, height - 1)
    yb = np.clip(y0 + 1, 0, height - 1)
    top = _gather(pixels, ya, xa) * (1.0 - fx)[..., None] \
        + _gather(pixels, ya, xb) * fx[..., None]
    bottom = _gather(pixels, yb, xa) * (1.0 - fx)[..., None] \
        + _gather(pixels, yb, xb) * fx[..., None]
    result = top * (1.0 - fy)[..., None] + bottom * fy[..., None]
    result = np.where(inside[..., None], result, 0.0)
    return result.astype(np.float32)


def sample_nearest(
    pixels: np.ndarray, pack: Rect,
    abs_x: np.ndarray, abs_y: np.ndarray,
    data_rect: Optional[Rect] = None,
) -> np.ndarray:
    """nearest 采样（texel 中心取整），与 MSL ``sample_nearest`` 一致。"""
    height, width = pixels.shape[:2]
    sx = abs_x - pack[0]
    sy = abs_y - pack[1]
    inside = np.ones_like(sx, dtype=bool)
    if data_rect is not None:
        inside = (
            (abs_x >= data_rect[0]) & (abs_y >= data_rect[1])
            & (abs_x < data_rect[2]) & (abs_y < data_rect[3])
        )
    cols = np.clip(np.floor(sx).astype(np.int64), 0, width - 1)
    rows = np.clip(np.floor(sy).astype(np.int64), 0, height - 1)
    result = _gather(pixels, rows, cols)
    return np.where(inside[..., None], result, 0.0).astype(np.float32)


# ── HSV（GLSL rgb2hsv/hsv2rgb 的 numpy 版） ───────────────────────────────


def rgb_to_hsv(rgb: np.ndarray) -> np.ndarray:
    # 与 GLSL 逐分量对齐：
    #   p = mix(vec4(c.bg, K.wz), vec4(c.gb, K.xy), step(c.b, c.g))
    #   q = mix(vec4(p.xyw, c.r), vec4(c.r, p.yzx), step(p.x, c.r))
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    p = np.where(
        (g >= b)[..., None],
        np.stack([g, b, np.zeros_like(g), np.full_like(g, -1.0 / 3.0)], -1),
        np.stack([b, g, np.full_like(g, -1.0),
                  np.full_like(g, 2.0 / 3.0)], -1))
    q = np.where(
        (r >= p[..., 0])[..., None],
        np.stack([r, p[..., 1], p[..., 2], p[..., 0]], -1),
        np.stack([p[..., 0], p[..., 1], p[..., 3], r], -1))
    d = q[..., 0] - np.minimum(q[..., 3], q[..., 1])
    h = np.abs(q[..., 2] + (q[..., 3] - q[..., 1]) / (6.0 * d + _EPS))
    s = d / (q[..., 0] + _EPS)
    return np.stack([h, s, q[..., 0]], axis=-1)


def hsv_to_rgb(hsv: np.ndarray) -> np.ndarray:
    # GLSL: p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);   （K.xyz 三分量，
    #       K.www = 3.0 为标量）
    #       return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    k = np.array([1.0, 2.0 / 3.0, 1.0 / 3.0], dtype=np.float64)
    p = np.abs(np.mod(h[..., None] + k, 1.0) * 6.0 - 3.0)
    p = np.clip(p - 1.0, 0.0, 1.0)
    mixed = (1.0 - s[..., None]) + p * s[..., None]
    return (v[..., None] * mixed).astype(np.float32)


# ── 混合模式（merge_blend 的 numpy 版，索引与 BLEND_MODES 一致） ─────────


def blend_pixels(
    mode: int, fg: np.ndarray, b: np.ndarray,
    fg_col: np.ndarray, bg_col: np.ndarray,
) -> np.ndarray:
    fgc = fg_col
    bgc = bg_col
    ones = np.ones_like(fgc)
    if mode == 0:
        rgb = fg[..., :3] + b[..., :3] * (1.0 - fg[..., 3:4]); a = fg[..., 3:4] + b[..., 3:4] * (1.0 - fg[..., 3:4])
    elif mode == 1:
        rgb = b[..., :3] + fg[..., :3] * (1.0 - b[..., 3:4]); a = b[..., 3:4] + fg[..., 3:4] * (1.0 - b[..., 3:4])
    elif mode == 2:
        rgb = b[..., :3] + fg[..., :3]; a = b[..., 3:4] + fg[..., 3:4]
    elif mode == 3:
        rgb = b[..., :3] - fg[..., :3]; a = b[..., 3:4]
    elif mode == 4:
        col = fgc * bgc
        rgb = col * fg[..., 3:4] + b[..., :3] * (1.0 - fg[..., 3:4]); a = fg[..., 3:4] + b[..., 3:4] * (1.0 - fg[..., 3:4])
    elif mode == 5:
        col = 1.0 - (1.0 - fgc) * (1.0 - bgc)
        rgb = col * fg[..., 3:4] + b[..., :3] * (1.0 - fg[..., 3:4]); a = fg[..., 3:4] + b[..., 3:4] * (1.0 - fg[..., 3:4])
    elif mode == 6:
        col = np.where(bgc < 0.5, 2.0 * bgc * fgc,
                       1.0 - 2.0 * (1.0 - bgc) * (1.0 - fgc))
        rgb = col * fg[..., 3:4] + b[..., :3] * (1.0 - fg[..., 3:4]); a = fg[..., 3:4] + b[..., 3:4] * (1.0 - fg[..., 3:4])
    elif mode == 7:
        col = np.abs(bgc - fgc)
        rgb = col * fg[..., 3:4] + b[..., :3] * (1.0 - fg[..., 3:4]); a = fg[..., 3:4] + b[..., 3:4] * (1.0 - fg[..., 3:4])
    elif mode == 8:
        col = np.minimum(bgc / np.maximum(fgc, 1e-5), np.full_like(fgc, 65504.0))
        rgb = col * fg[..., 3:4] + b[..., :3] * (1.0 - fg[..., 3:4]); a = fg[..., 3:4] + b[..., 3:4] * (1.0 - fg[..., 3:4])
    elif mode == 9:
        col = np.minimum(fgc, bgc)
        rgb = col * fg[..., 3:4] + b[..., :3] * (1.0 - fg[..., 3:4]); a = fg[..., 3:4] + b[..., 3:4] * (1.0 - fg[..., 3:4])
    elif mode == 10:
        col = np.maximum(fgc, bgc)
        rgb = col * fg[..., 3:4] + b[..., :3] * (1.0 - fg[..., 3:4]); a = fg[..., 3:4] + b[..., 3:4] * (1.0 - fg[..., 3:4])
    elif mode == 11:
        col = (fgc + bgc) * 0.5
        rgb = col * fg[..., 3:4] + b[..., :3] * (1.0 - fg[..., 3:4]); a = fg[..., 3:4] + b[..., 3:4] * (1.0 - fg[..., 3:4])
    elif mode == 12:
        rgb = fg[..., :3]; a = fg[..., 3:4]
    elif mode == 13:
        rgb = b[..., :3] * fg[..., 3:4]; a = b[..., 3:4] * fg[..., 3:4]
    elif mode == 15:
        rgb = fg[..., :3] * b[..., 3:4]; a = fg[..., 3:4] * b[..., 3:4]
    elif mode in (14,):
        rgb = fg[..., :3] * b[..., 3:4]; a = fg[..., 3:4] * b[..., 3:4]
    elif mode == 16:
        rgb = fg[..., :3] * (1.0 - b[..., 3:4]); a = fg[..., 3:4] * (1.0 - b[..., 3:4])
    elif mode == 17:
        rgb = fg[..., :3] * b[..., 3:4] + b[..., :3] * (1.0 - fg[..., 3:4]); a = b[..., 3:4]
    elif mode == 18:
        rgb = fg[..., :3] * (1.0 - b[..., 3:4]) + b[..., :3] * (1.0 - fg[..., 3:4])
        a = fg[..., 3:4] * (1.0 - b[..., 3:4]) + b[..., 3:4] * (1.0 - fg[..., 3:4])
    elif mode == 19:
        col = np.minimum(bgc / np.maximum(1.0 - fgc, 1e-5),
                         np.full_like(fgc, 65504.0))
        rgb = col * fg[..., 3:4] + b[..., :3] * (1.0 - fg[..., 3:4]); a = fg[..., 3:4] + b[..., 3:4] * (1.0 - fg[..., 3:4])
    elif mode == 20:
        col = np.maximum(1.0 - (1.0 - bgc) / np.maximum(fgc, 1e-5), 0.0)
        rgb = col * fg[..., 3:4] + b[..., :3] * (1.0 - fg[..., 3:4]); a = fg[..., 3:4] + b[..., 3:4] * (1.0 - fg[..., 3:4])
    elif mode == 21:
        col = np.where(
            fgc < 0.5,
            bgc - (1.0 - 2.0 * fgc) * bgc * (1.0 - bgc),
            bgc + (2.0 * fgc - 1.0) * (np.sqrt(np.maximum(bgc, 0.0)) - bgc))
        rgb = col * fg[..., 3:4] + b[..., :3] * (1.0 - fg[..., 3:4]); a = fg[..., 3:4] + b[..., 3:4] * (1.0 - fg[..., 3:4])
    elif mode == 22:
        col = np.where(fgc < 0.5, 2.0 * fgc * bgc,
                       1.0 - 2.0 * (1.0 - fgc) * (1.0 - bgc))
        rgb = col * fg[..., 3:4] + b[..., :3] * (1.0 - fg[..., 3:4]); a = fg[..., 3:4] + b[..., 3:4] * (1.0 - fg[..., 3:4])
    elif mode == 23:
        col = np.clip(bgc + 2.0 * fgc - 1.0, 0.0, 65504.0)
        rgb = col * fg[..., 3:4] + b[..., :3] * (1.0 - fg[..., 3:4]); a = fg[..., 3:4] + b[..., 3:4] * (1.0 - fg[..., 3:4])
    elif mode in (24, 25, 26, 27):
        fg_hsv = rgb_to_hsv(fgc)
        bg_hsv = rgb_to_hsv(bgc)
        if mode == 24:
            hsv = np.stack([fg_hsv[..., 0], bg_hsv[..., 1], bg_hsv[..., 2]], -1)
        elif mode == 25:
            hsv = np.stack([bg_hsv[..., 0], fg_hsv[..., 1], bg_hsv[..., 2]], -1)
        elif mode == 26:
            hsv = np.stack([fg_hsv[..., 0], fg_hsv[..., 1], bg_hsv[..., 2]], -1)
        else:
            hsv = np.stack([bg_hsv[..., 0], bg_hsv[..., 1], fg_hsv[..., 2]], -1)
        col = hsv_to_rgb(hsv)
        rgb = col * fg[..., 3:4] + b[..., :3] * (1.0 - fg[..., 3:4]); a = fg[..., 3:4] + b[..., 3:4] * (1.0 - fg[..., 3:4])
    elif mode == 28:
        rgb = fg[..., :3] - b[..., :3]; a = np.clip(fg[..., 3:4], 0.0, 1.0)
    elif mode == 29:
        rgb = fg[..., :3] + b[..., :3] - 2.0 * fg[..., :3] * b[..., :3]; a = fg[..., 3:4] + b[..., 3:4] * (1.0 - fg[..., 3:4])
    elif mode == 30:
        a_sum = fg[..., 3:4] + b[..., 3:4]
        a = np.minimum(a_sum, 1.0)
        rgb = fg[..., :3] + b[..., :3] * np.clip((a - fg[..., 3:4]) / np.maximum(b[..., 3:4], 1e-6),
                               0.0, 1.0)
    elif mode == 31:
        a_sum = fg[..., 3:4] + b[..., 3:4]
        low = a_sum <= 1.0
        a = np.where(low, a_sum, np.maximum(fg[..., 3:4], b[..., 3:4]))
        rgb = np.where(
            low, fg[..., :3] + b[..., :3],
            fg[..., :3] + b[..., :3] * np.clip((1.0 - fg[..., 3:4]) / np.maximum(b[..., 3:4], 1e-6),
                             0.0, 1.0))
    else:
        rgb = fg[..., :3] + b[..., :3] * (1.0 - fg[..., 3:4]); a = fg[..., 3:4] + b[..., 3:4] * (1.0 - fg[..., 3:4])
    return np.concatenate([rgb, a], axis=-1).astype(np.float32)


def apply_feather(t: np.ndarray, f: float) -> np.ndarray:
    """与 _CONSTANT_GRADIENT_FRAGMENT::apply_feather 对齐。"""
    tc = np.clip(t, 0.0, 1.0)
    if abs(f) <= 0.001:
        return tc
    if f > 0.0:
        pad = min(f, 0.999) * 0.5
        x = np.clip((tc - pad) / (1.0 - 2.0 * pad), 0.0, 1.0)
        return np.where(tc <= pad, 0.0,
                        np.where(tc >= 1.0 - pad, 1.0, x * x * (3.0 - 2.0 * x)))
    a = min(-f, 0.999) * 0.5
    s = tc * tc * (3.0 - 2.0 * tc)
    return a + s * (1.0 - 2.0 * a)


# ── 各 kernel 的 CPU 版 ──────────────────────────────────────────────────


def cpu_constant(node: PlannedNode, region: Rect) -> np.ndarray:
    params = gp.constant_params(node)
    out = _empty_region(region)
    gx, gy = _region_grid(region)
    inside = ((gx >= 0) & (gy >= 0)
              & (gx < params["canvas_w"]) & (gy < params["canvas_h"]))
    w = float(params["canvas_w"])
    h = float(params["canvas_h"])
    c1 = np.asarray(params["color1"], dtype=np.float32)
    c2 = np.asarray(params["color2"], dtype=np.float32)
    mode = params["mode"]
    if mode == 0:
        value = np.broadcast_to(c1, out.shape)
        out[:] = value
    elif mode == 1:
        checker = int(params["checker_size"])
        cell = (gy.astype(np.int64) // checker) + (gx.astype(np.int64) // checker)
        first = (cell % 2) == 0
        out[:] = np.where(first[..., None], c1, c2)
    else:
        # 像素中心采样（与 GL 参考 v_uv 一致；角点采样偏移半像素）。
        uv_x = (gx + 0.5) / w
        uv_y = (gy + 0.5) / h
        if mode == 3:
            dx = gx + 0.5 - params["center_x"] * w
            dy = gy + 0.5 - params["center_y"] * h
            short_edge = min(w, h)
            t = np.sqrt(dx * dx + dy * dy) / max(
                params["radial_radius"] * short_edge, 1e-4)
        else:
            dirx = params["end_x"] - params["start_x"]
            diry = params["end_y"] - params["start_y"]
            len2 = dirx * dirx + diry * diry
            if len2 < 1e-4:
                t = np.zeros_like(gx)
            else:
                t = ((uv_x - params["start_x"]) * dirx
                     + (uv_y - params["start_y"]) * diry) / len2
        t = apply_feather(t, params["feather"])
        out[:] = c1 + (c2 - c1) * t[..., None]
    out[:] = np.where(inside[..., None], out, 0.0)
    return out.astype(np.float32)


def cpu_transform(node: PlannedNode, src: CpuFrame, region: Rect) -> np.ndarray:
    params = gp.transform_params(node, src.meta)
    gp.fill_input_pack(params, src.pack)
    gx, gy = _region_grid(region)
    ix = params["i00"] * gx + params["i01"] * gy + params["i02"]
    iy = params["i10"] * gx + params["i11"] * gy + params["i12"]
    pack = (params["in_rect_x"], params["in_rect_y"],
            params["in_rect_x"] + int(params["in_w"]),
            params["in_rect_y"] + int(params["in_h"]))
    # 包含检查 = 输入纹理边界（GLSL _TRANSFORM_FRAGMENT 的 uv∈[0,1] 检查）。
    if params["filter"] == 1:
        return sample_nearest(src.pixels, pack, ix, iy, data_rect=pack)
    return sample_bilinear(src.pixels, pack, ix, iy, data_rect=pack)


def cpu_merge(node: PlannedNode,
              frame_a: Optional[CpuFrame], frame_b: Optional[CpuFrame],
              frame_mask: Optional[CpuFrame], region: Rect) -> np.ndarray:
    params = gp.merge_params(node, frame_a.meta if frame_a else None,
                             frame_b.meta if frame_b else None,
                             frame_mask.meta if frame_mask else None)
    if frame_a is not None:
        gp.fill_merge_packs(params, frame_a.pack, frame_b.pack if frame_b else (0, 0, 0, 0),
                            frame_mask.pack if frame_mask else (0, 0, 0, 0))
    gx, gy = _region_grid(region)
    shape = gx.shape + (4,)

    if frame_a is None:
        return np.zeros(shape, dtype=np.float32)
    a = sample_bilinear(frame_a.pixels, tuple(params["pack_a"]), gx, gy,
                        tuple(params["rect_a"]))
    if params["transparent_background"] or frame_b is None:
        b = np.zeros(shape, dtype=np.float32)
    else:
        b = sample_bilinear(frame_b.pixels, tuple(params["pack_b"]), gx, gy,
                            tuple(params["rect_b"]))

    if params["a_straight"]:
        a[..., :3] *= a[..., 3:4]
    if params["b_straight"]:
        b[..., :3] *= b[..., 3:4]

    mask_val = np.ones(gx.shape, dtype=np.float32)
    if params["has_mask"] and frame_mask is not None:
        m = sample_bilinear(frame_mask.pixels, tuple(params["pack_mask"]),
                            gx, gy, tuple(params["rect_mask"]))
        channel = params["mask_channel"]
        if channel == 0:
            mask_val = (m[..., 0] * 0.2126 + m[..., 1] * 0.7152
                        + m[..., 2] * 0.0722)
            if params["layer_composite"] and params["mask_straight"]:
                mask_val *= m[..., 3]
        else:
            mask_val = m[..., channel - 1]
        if params["mask_invert"]:
            mask_val = 1.0 - mask_val
    if params["layer_composite"]:
        mask_val = np.clip(mask_val, 0.0, 1.0)
    effect = params["opacity"] * mask_val

    mode = params["blend_mode"]
    if mode in (12, 13, 14):
        fg = np.concatenate([a[..., :3], a[..., 3:4] * effect[..., None]], -1)
    else:
        fg = np.concatenate(
            [a[..., :3] * effect[..., None], a[..., 3:4] * effect[..., None]],
            -1)

    fa = fg[..., 3:4]
    fg_col = np.where(fa > 1e-5, fg[..., :3] / np.maximum(fa, 1e-5), 0.0)
    ba = b[..., 3:4]
    bg_col = np.where(ba > 1e-5, b[..., :3] / np.maximum(ba, 1e-5), 0.0)

    blended = blend_pixels(mode, fg, b, fg_col, bg_col)

    if params["layer_composite"]:
        empty_bg = ba <= 0.0
        if mode in range(4, 12) or mode in range(19, 28):
            backdrop = b[..., :3] * (1.0 - fa)
            overlap = backdrop + (blended[..., :3] - backdrop) * np.minimum(ba, 1.0)
            blended[..., :3] = np.where(empty_bg, fg[..., :3], overlap)
        blended[..., 3:4] = np.where(empty_bg, fa, blended[..., 3:4])

    if params["alpha_clamp"]:
        blended[..., 3:4] = np.clip(blended[..., 3:4], 0.0, 1.0)

    mix_amount = np.clip(
        params["mix"] * (1.0 if params["layer_composite"] else mask_val),
        0.0, 1.0)[..., None]
    blended[..., :3] = b[..., :3] + (blended[..., :3] - b[..., :3]) * mix_amount
    blended[..., 3:4] = b[..., 3:4] + (blended[..., 3:4] - b[..., 3:4]) * mix_amount

    if params["output_unpremult"]:
        oa = blended[..., 3:4]
        blended[..., :3] = np.where(
            oa > 1e-5, blended[..., :3] / np.maximum(oa, 1e-5),
            blended[..., :3])
    return blended.astype(np.float32)


def cpu_alpha_op(src: CpuFrame, region: Rect, unpremult: bool) -> np.ndarray:
    out = src.pixels.copy()
    alpha = out[..., 3:4]
    if unpremult:
        inv = 1.0 / np.maximum(alpha, 1e-5)
        rgb = np.where(alpha > 1e-5, out[..., :3] * inv, 0.0)
        out[..., :3] = rgb
    else:
        out[..., :3] *= alpha
    return out.astype(np.float32)


def cpu_crop(node: PlannedNode, src: CpuFrame, region: Rect) -> np.ndarray:
    params = gp.crop_params(node, src.meta)
    gp.fill_crop_pack(params, src.pack, region)
    gx, gy = _region_grid((0, 0, region[2] - region[0], region[3] - region[1]))
    abs_x = gx + params["abs_x0"]
    abs_y = gy + params["abs_y0"]
    uv_x = abs_x / float(params["src_w"])
    uv_y = abs_y / float(params["src_h"])
    outside = (uv_x < 0) | (uv_x > 1) | (uv_y < 0) | (uv_y > 1)
    pack = (params["pack_x"], params["pack_y"],
            params["pack_x"] + int(params["pack_w"]),
            params["pack_y"] + int(params["pack_h"]))
    if params["outside"] == 2:
        abs_x = np.where(outside, uv_x % 1.0 * params["src_w"], abs_x)
        abs_y = np.where(outside, uv_y % 1.0 * params["src_h"], abs_y)
    elif params["outside"] == 3:
        mx = np.abs(np.mod(uv_x, 2.0))
        mx = np.where(mx > 1.0, 2.0 - mx, mx)
        my = np.abs(np.mod(uv_y, 2.0))
        my = np.where(my > 1.0, 2.0 - my, my)
        abs_x = np.where(outside, mx * params["src_w"], abs_x)
        abs_y = np.where(outside, my * params["src_h"], abs_y)
    sampled = sample_bilinear(src.pixels, pack, abs_x, abs_y)
    if params["outside"] in (0, 1):
        fill = np.array([0.0, 0.0, 0.0, 1.0 if params["outside"] == 0 else 0.0],
                        dtype=np.float32)
        sampled = np.where(outside[..., None], fill, sampled)
    return sampled.astype(np.float32)


def cpu_mask(node: PlannedNode, src: Optional[CpuFrame],
             consumed_port: str, region: Rect) -> np.ndarray:
    params = gp.mask_params(node, src is not None, consumed_port)
    gx, gy = _region_grid(region)
    w = float(params["canvas_w"])
    h = float(params["canvas_h"])
    uv_x = gx / w
    uv_y = gy / h
    rot = params["rotation_deg"]
    if abs(rot) > 0.1:
        angle = math.radians(rot)
        c, s = math.cos(angle), math.sin(angle)
        ox = uv_x - params["center_x"]
        oy = uv_y - params["center_y"]
        uv_x = ox * c - oy * s + params["center_x"]
        uv_y = ox * s + oy * c + params["center_y"]
    dx = np.abs(uv_x - params["center_x"]) - params["mask_size_x"] * 0.5
    dy = np.abs(uv_y - params["center_y"]) - params["mask_size_y"] * 0.5
    dist = np.maximum(dx, dy)
    feather = params["feather"]
    if feather < 0.001:
        alpha = (dist >= 0.0).astype(np.float32)
    else:
        t = np.clip((dist + feather) / (2.0 * feather), 0.0, 1.0)
        alpha = t * t * (3.0 - 2.0 * t)
    if params["invert"]:
        alpha = 1.0 - alpha
    mode = params["output_mode"]
    if mode == 0:
        out = np.zeros(gx.shape + (4,), dtype=np.float32)
        out[..., 3] = alpha
        return out
    if mode == 1:
        out = np.zeros(gx.shape + (4,), dtype=np.float32)
        out[..., :] = alpha[..., None]
        return out
    source = src.pixels if src is not None else np.zeros(gx.shape + (4,),
                                                         dtype=np.float32)
    return (source * alpha[..., None]).astype(np.float32)


def _linear_to_srgb(x: np.ndarray) -> np.ndarray:
    return np.where(
        x <= 0.0031308, 12.92 * x,
        1.055 * np.power(np.maximum(x, 0.0), 1.0 / 2.4) - 0.055)


def cpu_output(node: PlannedNode, src: Optional[CpuFrame],
               region: Rect) -> np.ndarray:
    params = gp.output_params(node, src.meta if src else None)
    if src is None:
        return _empty_region(region)
    gp.fill_output_pack(params, src.pack)
    gx, gy = _region_grid(region)
    data_rect = (params["in_rect_x"], params["in_rect_y"],
                 params["in_rect_x"] + int(params["in_w"]),
                 params["in_rect_y"] + int(params["in_h"]))
    pack = (params["pack_x"], params["pack_y"],
            params["pack_x"] + int(params["pack_w"]),
            params["pack_y"] + int(params["pack_h"]))
    c = sample_bilinear(src.pixels, pack, gx, gy, data_rect)

    if params["bg_a"] > 0.0:
        alpha = c[..., 3:4]
        fg_premult = c[..., :3] if params["input_premult"] else c[..., :3] * alpha
        bg = np.array([params["bg_r"], params["bg_g"], params["bg_b"],
                       params["bg_a"]], dtype=np.float32)
        c = np.concatenate(
            [fg_premult + bg[:3] * bg[3] * (1.0 - alpha),
             alpha + bg[3] * (1.0 - alpha)], axis=-1)

    alpha = c[..., 3:4]
    if params["input_premult"]:
        safe_alpha = np.where(alpha > 0.001, alpha, 1.0)
        straight = np.where(alpha > 0.001, c[..., :3] / safe_alpha, c[..., :3])
    else:
        straight = c[..., :3]
    mode = params["alpha_mode"]
    if mode == 1:
        result = np.concatenate([straight * alpha, alpha], -1)
    elif mode == 2:
        result = np.concatenate([straight, alpha], -1)
    elif mode == 3:
        result = np.concatenate([straight, np.ones_like(alpha)], -1)
    else:
        result = c
    if params["encode_srgb"]:
        result = np.concatenate(
            [_linear_to_srgb(result[..., :3]), result[..., 3:4]], -1)
    return result.astype(np.float32)


# ── 计划执行 ─────────────────────────────────────────────────────────────


def execute_plan_cpu(
    plan: ExecutionPlan,
    render_region: Optional[Rect] = None,
    is_tile_pass: bool = False,
) -> np.ndarray:
    """按计划执行，返回输出画布整帧 ``(h, w, 4)`` float32。

    Args:
        render_region: 输出节点渲染的区域（默认全画布）。tile 模式下为
            扩张后的区域，输出节点只写 tile 中心（由 ``is_tile_pass``
            区分：True 时中间节点跑扩张区、Output 跑中心）。
        is_tile_pass: 是否 tile pass（影响 Transform 的 ROI 反推）。
    """
    gp.assign_execution_regions(plan, render_region or (0, 0, *plan.output_canvas),
                                is_tile_pass)
    index = plan.node_index()
    frames: Dict[str, CpuFrame] = {}

    for node in plan.nodes:
        meta = node.meta
        assert meta is not None
        inputs = {
            port: frames[src] for port, src in node.inputs.items()
            if src in frames
        }
        region = node.region
        if region[2] <= region[0] or region[3] <= region[1]:
            frames[node.node_id] = CpuFrame(
                _empty_region((0, 0, 1, 1)), meta, (0, 0, 0, 0))
            continue
        if meta.passthrough:
            src = next(iter(inputs.values()), None)
            frames[node.node_id] = src if src is not None else CpuFrame(
                _empty_region((0, 0, 1, 1)), meta, (0, 0, 0, 0))
            continue

        t = node.node_type
        if t == "constant":
            pixels = cpu_constant(node, region)
        elif t == "transform":
            pixels = cpu_transform(node, inputs["image"], region)
        elif t == "merge":
            pixels = cpu_merge(node, inputs.get("A"), inputs.get("B"),
                               inputs.get("mask"), region)
        elif t == "premult":
            pixels = cpu_alpha_op(inputs["image"], region, unpremult=False)
        elif t == "unpremult":
            pixels = cpu_alpha_op(inputs["image"], region, unpremult=True)
        elif t == "crop":
            pixels = cpu_crop(node, inputs["image"], region)
        elif t == "mask":
            pixels = cpu_mask(node, inputs.get("image"),
                              _consumed_mask_port(plan, index, node), region)
        elif t == "output":
            pixels = cpu_output(node, inputs.get("image"), region)
        else:  # pragma: no cover - 预检已拦截
            raise gp.PlanError(f"未支持的节点类型 {t!r}")
        frames[node.node_id] = CpuFrame(pixels, meta, node.pack)

    out_meta = frames[plan.output_node_id].meta
    canvas = np.zeros((out_meta.canvas_h, out_meta.canvas_w, 4),
                      dtype=np.float32)
    out_frame = frames[plan.output_node_id]
    x0, y0, x1, y1 = out_frame.pack
    if x1 > x0 and y1 > y0:
        canvas[y0:y1, x0:x1] = out_frame.pixels
    return canvas


def _consumed_mask_port(plan: ExecutionPlan, index: Dict[str, PlannedNode],
                        node: PlannedNode) -> str:
    """mask 节点被下游消费的端口（决定 matte/result 输出模式）。"""
    for other in plan.nodes:
        for port, src in other.inputs.items():
            if src == node.node_id:
                if port in ("mask", "A", "B"):
                    return "matte"
                return "result"
    return "result"


def render_graph_cpu(
    graph_data: dict,
    timeline_frame: int = 0,
    tile_size: Optional[Tuple[int, int]] = None,
) -> np.ndarray:
    """便捷入口：预检 + 计划 + CPU 执行（对拍/无 GPU 测试用）。

    未通过 Metal 能力判定的图抛 :class:`gp.PlanError`（生产回退由
    CompositeGraphRenderService 决定，本函数不做回退）。
    """
    from .node_support import evaluate_metal_support
    support = evaluate_metal_support(graph_data)
    if not support.supported:
        raise gp.PlanError(support.fallback_summary or "图不支持 Metal 链")
    plan = gp.build_execution_plan(graph_data, timeline_frame)
    if tile_size is None:
        return execute_plan_cpu(plan)
    # tile 渲染：中间节点跑扩张区，Output 跑中心，拼回整帧。
    halo = _graph_halo(graph_data, plan.output_node_id)
    canvas_w, canvas_h = plan.output_canvas
    result = np.zeros((canvas_h, canvas_w, 4), dtype=np.float32)
    for tile in roi.iter_tile_rects(canvas_w, canvas_h, *tile_size):
        expanded, _crop = roi.input_roi(tile, halo, canvas_w, canvas_h)
        frame = execute_plan_cpu(plan, render_region=expanded,
                                 is_tile_pass=True)
        tx0, ty0, tx1, ty1 = tile
        result[ty0:ty1, tx0:tx1] = frame[ty0:ty1, tx0:tx1]
    return result


def _graph_halo(graph_data: dict, output_node_id: str) -> int:
    from .roi import propagate_input_halos
    propagation = propagate_input_halos(
        graph_data.get("nodes") or [],
        graph_data.get("connections") or [],
        [output_node_id],
    )
    return propagation.node_input_halo.get(output_node_id, 0)
