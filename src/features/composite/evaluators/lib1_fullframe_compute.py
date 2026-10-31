"""LIB1 全帧 CPU 计算核（L17：C18 distance_matte / C19 fill_holes）。

全帧声明子门的 CPU 执行侧：BFS/距离场算法不适合单 pass GLSL，
节点经 dispatch 的全帧 CPU 通道执行（回读→计算→上传）。函数体与
暂存区 references 逐字一致；暂存 *_ref.py 再导出本模块（单一
事实源，禁双份漂移）。
"""

from __future__ import annotations

from collections import deque  # noqa: F401  (fill_holes BFS 使用)
from typing import Any, Mapping

import numpy as np


# ── distance_matte ──────────────────────────────────────────────────

METRICS = ("Euclid", "Chebyshev", "Manhattan")
DM_OUTPUT_MODES = ("DistanceField", "EdgeBand", "Matte")

DISTANCE_MATTE_DEFAULTS: dict[str, Any] = {
    "metric": "Euclid",
    "radius": 8.0,      # 像素，归一化长度
    "threshold": 0.5,   # α ≥ threshold → FG
    "output_mode": "DistanceField",
}

_CHUNK = 1024   # 暴力对拍分块行数（防 O(N²) 内存尖峰）


def _pairwise_metric(a: np.ndarray, b: np.ndarray, metric: str) -> np.ndarray:
    """a (K,2)、b (M,2) 像素坐标 → (K,M) 度量距离。"""
    dy = a[:, 0][:, None] - b[:, 0][None, :]
    dx = a[:, 1][:, None] - b[:, 1][None, :]
    if metric == "Euclid":
        return np.hypot(dy, dx)
    if metric == "Chebyshev":
        return np.maximum(np.abs(dy), np.abs(dx))
    return np.abs(dy) + np.abs(dx)   # Manhattan


def distance_matte_evaluate(image: np.ndarray, params: Mapping[str, Any] | None = None) -> np.ndarray:
    p = dict(DISTANCE_MATTE_DEFAULTS)
    if params:
        p.update(params)
    metric = str(p["metric"])
    mode = str(p["output_mode"])
    if metric not in METRICS:
        raise ValueError(f"metric 必须是 {list(METRICS)}，收到 {metric!r}")
    if mode not in DM_OUTPUT_MODES:
        raise ValueError(f"output_mode 必须是 {list(DM_OUTPUT_MODES)}，收到 {mode!r}")
    radius = max(float(p["radius"]), 1e-6)
    threshold = float(p["threshold"])

    img = np.asarray(image, dtype=np.float64)
    if img.ndim != 3 or img.shape[-1] < 4:
        raise ValueError("distance_matte 需要 4 通道 RGBA 输入（α 为 matte 源）")
    alpha = img[..., 3]
    h, w = alpha.shape
    fg = alpha >= threshold

    ys, xs = np.indices((h, w))
    coords = np.stack([ys.ravel(), xs.ravel()], axis=1).astype(np.float64)
    fg_coords = coords[fg.ravel()]
    bg_coords = coords[~fg.ravel()]

    s = np.empty(h * w, dtype=np.float64)
    if fg_coords.size == 0 or bg_coords.size == 0:
        # 无对类 → 饱和（原始距离域设 ±radius，归一化后恰为 ±1）
        s[:] = radius if bg_coords.size == 0 else -radius
    else:
        is_fg = fg.ravel()
        for start in range(0, h * w, _CHUNK):
            stop = min(start + _CHUNK, h * w)
            block = coords[start:stop]
            blk_fg = is_fg[start:stop]
            # 逐块精确对拍：FG 像素对 BG 集合、BG 像素对 FG 集合
            d = np.empty(stop - start, dtype=np.float64)
            if blk_fg.any():
                d[blk_fg] = _pairwise_metric(
                    block[blk_fg], bg_coords, metric).min(axis=1)
            if (~blk_fg).any():
                d[~blk_fg] = _pairwise_metric(
                    block[~blk_fg], fg_coords, metric).min(axis=1)
            s[start:stop] = np.where(blk_fg, d, -d)
    dn = np.clip(s.reshape(h, w) / radius, -1.0, 1.0)

    if mode == "DistanceField":
        result = dn
    elif mode == "EdgeBand":
        result = np.clip(1.0 - np.abs(dn), 0.0, 1.0)
    else:   # Matte
        result = np.clip(0.5 + 0.5 * dn, 0.0, 1.0)

    out = img.copy()
    out[..., 3] = result
    return out.astype(np.float32)


# ── fill_holes ──────────────────────────────────────────────────

CONNECTIVITIES = ("4", "8")
OUTPUT_MODES = ("Fill", "Holes")

FILL_HOLES_DEFAULTS: dict[str, Any] = {
    "connectivity": "4",
    "max_area": 0.1,     # 画幅面积比例
    "threshold": 0.5,    # α ≥ threshold → FG
    "output_mode": "Fill",
}


def _bg_component_labels(bg: np.ndarray, connectivity: str) -> np.ndarray:
    """BG 连通域标注（BFS，行主序确定性种子遍历）；0=非 BG，≥1=域号。"""
    h, w = bg.shape
    if connectivity == "4":
        offsets = ((-1, 0), (1, 0), (0, -1), (0, 1))
    else:
        offsets = ((-1, 0), (1, 0), (0, -1), (0, 1),
                   (-1, -1), (-1, 1), (1, -1), (1, 1))
    labels = np.zeros((h, w), dtype=np.int64)
    current = 0
    for sy in range(h):
        for sx in range(w):
            if not bg[sy, sx] or labels[sy, sx] != 0:
                continue
            current += 1
            labels[sy, sx] = current
            queue = deque([(sy, sx)])
            while queue:
                y, x = queue.popleft()
                for dy, dx in offsets:
                    ny, nx = y + dy, x + dx
                    if (0 <= ny < h and 0 <= nx < w and bg[ny, nx]
                            and labels[ny, nx] == 0):
                        labels[ny, nx] = current
                        queue.append((ny, nx))
    return labels


def fill_holes_evaluate(image: np.ndarray, params: Mapping[str, Any] | None = None) -> np.ndarray:
    p = dict(FILL_HOLES_DEFAULTS)
    if params:
        p.update(params)
    connectivity = str(p["connectivity"])
    mode = str(p["output_mode"])
    if connectivity not in CONNECTIVITIES:
        raise ValueError(f"connectivity 必须是 {list(CONNECTIVITIES)}，收到 {connectivity!r}")
    if mode not in OUTPUT_MODES:
        raise ValueError(f"output_mode 必须是 {list(OUTPUT_MODES)}，收到 {mode!r}")
    max_area = min(max(float(p["max_area"]), 0.0), 1.0)
    threshold = float(p["threshold"])

    img = np.asarray(image, dtype=np.float64)
    if img.ndim != 3 or img.shape[-1] < 4:
        raise ValueError("fill_holes 需要 4 通道 RGBA 输入（α 为 matte 源）")
    alpha = img[..., 3]
    h, w = alpha.shape
    bg = alpha < threshold

    labels = _bg_component_labels(bg, connectivity)
    n_labels = int(labels.max())
    holes = np.zeros((h, w), dtype=bool)
    if n_labels:
        border = np.zeros(n_labels + 1, dtype=bool)
        border[labels[0, :]] = True
        border[labels[-1, :]] = True
        border[labels[:, 0]] = True
        border[labels[:, -1]] = True
        border[0] = False                       # 标签 0 = 非 BG
        areas = np.bincount(labels.ravel(), minlength=n_labels + 1)
        area_limit = max_area * float(h * w)
        for lab in range(1, n_labels + 1):
            if not border[lab] and areas[lab] <= area_limit:
                holes[labels == lab] = True

    out = img.copy()
    if mode == "Fill":
        out[..., 3] = np.where(holes, 1.0, alpha)
    else:   # Holes：孔 mask 本身（1 孔 / 0 其余）
        out[..., 3] = holes.astype(np.float64)
    return out.astype(np.float32)
