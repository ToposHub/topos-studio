"""C13 stmap —— 独立 CPU 数学参考（oracle，双源签名）。

遵守 references/oracle_conventions.md：float64 内部、float32 接口、
无随机流、防御回退。L00 台账 C13 行：无绝对 UV 重映射——与 C12
（相对位移）区分：**UV map 是绝对采样坐标**（uv=(0,0) → 图像原点角，
uv=(1,1) → 对角），非增量。**首个多输入子门与 C12 同批**。

语义（钉定口径）：
    px = u·(W−1)，py = v·(H−1) 或 (1−v)·(H−1)   （像素中心 = 整数索引）
    origin=BottomLeft：v=0 是图像底行（numpy 行 H−1）→ py = (1−v)·(H−1)
    origin=TopLeft：  v=0 是图像顶行（numpy 行 0）  → py = v·(H−1)
    channels=RG：u=map[...,0]、v=map[...,1]；BA：u=map[...,2]、v=map[...,3]
    invalid=Clamp：      UV ∈ [0,1] 外 → clamp-half-texel 边缘钳制采样
    invalid=Passthrough：UV ∈ [0,1] 外 → 该像素输出未位移原位内容
    filter=Nearest：floor(pos+0.5)；Linear：clamp 双线性

防御：map 通道不足（BA 缺 A/缺 B）→ 缺失通道按 0.0（原点角）；
分辨率不一致 → 同索引钳制读取（不重采样——接线期 GPU 声明）。
全通道（含 α）随 UV 重定位（整图重采样）；内容色不 clamp（HDR 透传）。
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

CHANNEL_ORDERS = ("RG", "BA")
ORIGINS = ("BottomLeft", "TopLeft")
INVALID_POLICIES = ("Clamp", "Passthrough")
FILTERS = ("Nearest", "Linear")

DEFAULTS: dict[str, Any] = {
    "channels": "RG",
    "origin": "BottomLeft",
    "invalid": "Clamp",
    "filter": "Nearest",   # oracle 钉 Nearest；Linear 双线性同构实现
}


def _bilinear(field: np.ndarray, ys: np.ndarray, xs: np.ndarray) -> np.ndarray:
    """clamp-to-edge 双线性采样。field (H,W[,C])；像素中心 = 整数索引。"""
    h, w = field.shape[0], field.shape[1]
    ys = np.clip(ys, 0.0, float(h - 1))
    xs = np.clip(xs, 0.0, float(w - 1))
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


def _clamp_read(field: np.ndarray, h: int, w: int) -> np.ndarray:
    """同索引读取（越界钳制）：map 与 image 分辨率可不同时的防御口径。"""
    mi = np.clip(np.arange(h), 0, field.shape[0] - 1)
    mj = np.clip(np.arange(w), 0, field.shape[1] - 1)
    return field[np.ix_(mi, mj)]


def evaluate(image: np.ndarray, uv_map: np.ndarray,
             params: Mapping[str, Any] | None = None) -> np.ndarray:
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    channels = str(p["channels"])
    origin = str(p["origin"])
    invalid = str(p["invalid"])
    filt = str(p["filter"])
    for name, value, allowed in (
            ("channels", channels, CHANNEL_ORDERS),
            ("origin", origin, ORIGINS),
            ("invalid", invalid, INVALID_POLICIES),
            ("filter", filt, FILTERS)):
        if value not in allowed:
            raise ValueError(f"{name} 必须是 {list(allowed)}，收到 {value!r}")

    img = np.asarray(image, dtype=np.float64)
    mp = np.asarray(uv_map, dtype=np.float64)
    h, w = img.shape[0], img.shape[1]

    # 通道映射（防御：缺失通道按 0.0 = 原点角；单通道图按灰度复用）
    if mp.ndim == 2:
        mp = mp[..., None]
    idx = {"RG": (0, 1), "BA": (2, 3)}[channels]

    def _chan(k: int) -> np.ndarray:
        if mp.shape[-1] > k:
            c = mp[..., k]
        elif mp.shape[-1] == 1:
            c = mp[..., 0]
        else:
            c = np.zeros(mp.shape[:2], dtype=np.float64)
        if c.shape != (h, w):
            c = _clamp_read(c, h, w)
        return c

    u = _chan(idx[0])
    v = _chan(idx[1])

    px = u * (w - 1)
    py = (1.0 - v) * (h - 1) if origin == "BottomLeft" else v * (h - 1)

    yy, xx = np.meshgrid(np.arange(h, dtype=np.float64),
                         np.arange(w, dtype=np.float64), indexing="ij")

    if invalid == "Passthrough":
        inside = ((u >= 0.0) & (u <= 1.0) & (v >= 0.0) & (v <= 1.0))
        if filt == "Nearest":
            yi = np.clip(np.floor(py + 0.5).astype(np.int64), 0, h - 1)
            xi = np.clip(np.floor(px + 0.5).astype(np.int64), 0, w - 1)
            sampled = img[yi, xi]
        else:
            sampled = _bilinear(img, py, px)
        out = np.where(inside[..., None], sampled, img)
        return out.astype(np.float32)

    # Clamp：UV 域外由采样边界钳制（clamp-half-texel 同构）
    if filt == "Nearest":
        yi = np.clip(np.floor(py + 0.5).astype(np.int64), 0, h - 1)
        xi = np.clip(np.floor(px + 0.5).astype(np.int64), 0, w - 1)
        return img[yi, xi].astype(np.float32)
    return _bilinear(img, py, px).astype(np.float32)
