"""C17 lib1_edge_extend —— 根据 alpha 把前景 RGB 延伸到透明区（试点）。

差量（L00 台账 C17 行）：erode_dilate 的 edge_mode "extend" 是采样钳制策略
非 RGB 延伸；light_wrap 是往边缘叠背景光。本节点是 alpha 子门首个试点。

算法（references/edge_extend_ref.py 同构）：
- 前源判定：alpha >= threshold 的像素是"已实"像素（RGB 可信源）；
- 迭代扩张 radius 轮：每轮把与已实像素 4-邻接的未实像素的 RGB 置为其已实
  邻居 RGB 的均值，并标记为新实（一轮扩张 1px，radius 即最大扩张半径）；
- 输出模式：
  - color_only（默认）：**保留原 alpha**，仅替换透明侧 RGB（去黑边/白边的
    预乘前置步骤）；
  - premultiplied：延伸后 RGB 乘回原 alpha（直出 premult 合成）。
- radius=0 恒等；radius 上限 8（有界常量，GPU 侧常量循环上限）。

纯 numpy float64、无随机流；输入 RGBA straight alpha（(H,W) 灰度 alpha
也接受——按 alpha=自身处理，RGB 通道缺省取自身）。
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

DEFAULTS: dict[str, Any] = {
    "radius": 4,             # 0–8 像素
    "threshold": 0.5,        # alpha ≥ threshold 视为前景源
    "output_mode": "color_only",  # color_only | premultiplied
}

_MODES = frozenset({"color_only", "premultiplied"})
_MAX_RADIUS = 8

_NEIGHBOR_SHIFTS = ((-1, 0), (1, 0), (0, -1), (0, 1))


def _shift(mask: np.ndarray, dy: int, dx: int, fill: bool = False) -> np.ndarray:
    out = np.full_like(mask, fill)
    ys_src = slice(max(0, -dy), mask.shape[0] - max(0, dy))
    ys_dst = slice(max(0, dy), mask.shape[0] - max(0, -dy))
    xs_src = slice(max(0, -dx), mask.shape[1] - max(0, dx))
    xs_dst = slice(max(0, dx), mask.shape[1] - max(0, -dx))
    out[ys_dst, xs_dst] = mask[ys_src, xs_src]
    return out


def evaluate(image_rgba: np.ndarray, params: Mapping[str, Any] | None = None) -> np.ndarray:
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    radius = int(p["radius"])
    radius = max(0, min(radius, _MAX_RADIUS))
    threshold = float(p["threshold"])
    mode = str(p["output_mode"])
    if mode not in _MODES:
        raise ValueError(f"output_mode 必须是 {sorted(_MODES)}，收到 {mode!r}")

    img = np.asarray(image_rgba, dtype=np.float64)
    if img.ndim == 2:  # 灰度 alpha：RGB 取自身
        img = np.stack([img, img, img, img], axis=-1)
    h, w = img.shape[0], img.shape[1]
    rgb = img[..., :3].copy()
    alpha = img[..., 3]

    solid = alpha >= threshold
    if radius == 0 or bool(solid.all()):
        out = img.copy()
    else:
        for _ in range(radius):
            frontier = np.zeros((h, w), dtype=bool)
            acc = np.zeros((h, w, 3), dtype=np.float64)
            cnt = np.zeros((h, w), dtype=np.int64)
            for dy, dx in _NEIGHBOR_SHIFTS:
                nb_solid = _shift(solid, dy, dx)
                nb_rgb = np.concatenate(
                    [_shift(rgb[..., c], dy, dx)[..., None] for c in range(3)], axis=-1)
                take = nb_solid & ~solid
                frontier |= take
                acc += np.where(take[..., None], nb_rgb, 0.0)
                cnt += take.astype(np.int64)
            newly = frontier & (cnt > 0)
            if not bool(newly.any()):
                break
            rgb = np.where(newly[..., None], acc / np.maximum(cnt, 1)[..., None], rgb)
            solid = solid | newly
        out = np.concatenate([rgb, alpha[..., None]], axis=-1)

    if mode == "premultiplied":
        out[..., :3] = out[..., :3] * out[..., 3:4]
    return out.astype(np.float32)
