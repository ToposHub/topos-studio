"""C14 tile_array —— 独立 CPU 数学参考（oracle）。

遵守 references/oracle_conventions.md：float64 内部、float32 接口、
无随机流、防御回退。L00 台账 C14 行：crop 的 outside=repeat 只是裁剪外
填充、非阵列平铺——本组件为新算法差量（**不是** Crop 边界 repeat 换名）。

语义（钉定口径，输出 bbox = 输入画幅；空隙 alpha=0）：
    格胞：tw = W/count_x、th = H/count_y（像素格）
    格胞 (i,j)：i = floor(x/tw)（钳 [0, count_x−1]）、j 同理
    内容盒：cw = tw·(1−spacing)、ch = th·(1−spacing)
            u_c = (x − i·tw − tw·spacing/2) / cw ∈ [0,1) → 瓦片内
            u_c ∉ [0,1) → 空隙（RGB=0、alpha=0，straight）
    源采样：u_src = frac(u_c + offset_x)（瓦片内环绕 repeat，正模）
            索引 = clip(floor(u_src·W), 0, W−1)（texel-block 最近邻：
            纹素 i 覆盖 [i/W, (i+1)/W)——1×1+0 恒等在此口径下精确）
    瓦片内 RGB+α = 输入采样值（HDR 透传）；spacing=0 时内容盒=格胞。

count 1×1、spacing=0、offset=0 → 精确恒等。offset 环绕（frac 正模）使
采样位永在输入画幅内 → tile_padding = 0（同画幅内重采样）。
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

DEFAULTS: dict[str, Any] = {
    "count_x": 3,      # 1–16
    "count_y": 3,      # 1–16
    "spacing": 0.1,    # 0–0.9，格胞内边距比例（四边对称）
    "offset_x": 0.0,   # −1–1，瓦片内源采样相位（环绕）
    "offset_y": 0.0,
}

_MIN_COUNT, _MAX_COUNT = 1, 16


def evaluate(image: np.ndarray, params: Mapping[str, Any] | None = None) -> np.ndarray:
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    count_x = max(_MIN_COUNT, min(int(p["count_x"]), _MAX_COUNT))
    count_y = max(_MIN_COUNT, min(int(p["count_y"]), _MAX_COUNT))
    spacing = min(max(float(p["spacing"]), 0.0), 0.9)
    off_x = float(p["offset_x"])
    off_y = float(p["offset_y"])

    img = np.asarray(image, dtype=np.float64)
    h, w = img.shape[0], img.shape[1]
    tw = w / count_x
    th = h / count_y
    cw = tw * (1.0 - spacing)
    ch = th * (1.0 - spacing)

    yy, xx = np.meshgrid(np.arange(h, dtype=np.float64),
                         np.arange(w, dtype=np.float64), indexing="ij")
    i = np.clip(np.floor(xx / tw).astype(np.int64), 0, count_x - 1)
    j = np.clip(np.floor(yy / th).astype(np.int64), 0, count_y - 1)

    u_c = (xx - i * tw - tw * spacing / 2.0) / cw
    v_c = (yy - j * th - th * spacing / 2.0) / ch

    inside = (u_c >= 0.0) & (u_c < 1.0) & (v_c >= 0.0) & (v_c < 1.0)

    # 瓦片内源采样：环绕 repeat（frac 正模）；texel-block 最近邻
    # floor(u·W)（纹素 i 覆盖 [i/W,(i+1)/W)）——1×1+0 时 u_c=x/W →
    # floor(x)=x 精确恒等（像素中心 round(u·(W−1)) 口径做不到，初版
    # 已被手算对拍否决：0.75x≠x）
    u_s = u_c + off_x - np.floor(u_c + off_x)
    v_s = v_c + off_y - np.floor(v_c + off_y)
    yi = np.clip(np.floor(v_s * h).astype(np.int64), 0, h - 1)
    xi = np.clip(np.floor(u_s * w).astype(np.int64), 0, w - 1)

    out = img[yi, xi]
    # 空隙：RGB=0、alpha=0（straight； HDR 内容仅存在于瓦片内）
    out = np.where(inside[..., None], out, np.zeros_like(out))
    return out.astype(np.float32)
