"""C12 vector_displace —— 独立 CPU 数学参考（oracle，双源签名）。

遵守 references/oracle_conventions.md：float64 内部、float32 接口、
无随机流、防御回退。L00 台账 C12 行：无图像驱动位移（mesh_warp 是 UI
手柄网格；velocity map 只用于模糊）——本组件为新算法差量，
**首个多输入子门**（image + vector map + 可选 mask，输入角色不可串线）。

语义（钉定口径）：
    dx = (vx − neutral) · kx，dy = (vy − neutral) · ky
    Pixels 单位：kx = ky = scale（像素）
    UV 单位：    kx = scale·W，ky = scale·H（画幅归一化比例）
    mask 门控：  dx_eff = dx·m，dy_eff = dy·m（m = mask α（4 通道）或
                 通道 0；不 clamp——mask 域声明 [0,1]，越界由声明域负责）
    采样位：     (x + dx_eff, y + dy_eff)，像素中心 = 整数索引，
                 clamp-to-edge 双线性（索引钳 [0, n−1]）

vector map / mask 与 image 分辨率不同时按同索引读取（越界钳制）——
map 重采样到 image 画幅属接线期 GPU 声明。防御：vector 通道不足 2 时
缺失通道按 neutral（零位移）处理；vector 缺失 R/G 均为 neutral →
精确恒等。
image 的 α 随 RGB 一起位移（整图重定位）；内容色不 clamp（HDR 透传）。
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

import numpy as np

UNITS = ("Pixels", "UV")

DEFAULTS: dict[str, Any] = {
    "scale": 50.0,     # 像素（Pixels）或画幅比例（UV）
    "neutral": 0.5,    # 向量图零位移灰值
    "units": "Pixels",
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
    """同索引读取（越界钳制）：map/mask 与 image 分辨率可不同时的防御
    口径——小图边缘延展、大图裁剪，不做重采样（接线期 GPU 声明）。"""
    mi = np.clip(np.arange(h), 0, field.shape[0] - 1)
    mj = np.clip(np.arange(w), 0, field.shape[1] - 1)
    return field[np.ix_(mi, mj)]


def _read_vector(vector: np.ndarray, neutral: float, h: int, w: int) -> tuple[np.ndarray, np.ndarray]:
    """RG 通道读位移向量；通道缺失按 neutral（零位移）防御回退，
    分辨率不一致时同索引钳制读取。"""
    if vector.ndim == 2:
        ch = vector[..., None]
    else:
        ch = vector
    vx = ch[..., 0] if ch.shape[-1] >= 1 else np.full(ch.shape[:2], float(neutral))
    vy = ch[..., 1] if ch.shape[-1] >= 2 else np.full(ch.shape[:2], float(neutral))
    if vx.shape != (h, w):
        vx = _clamp_read(vx, h, w)
    if vy.shape != (h, w):
        vy = _clamp_read(vy, h, w)
    return vx, vy


def evaluate(image: np.ndarray, vector: np.ndarray,
             params: Mapping[str, Any] | None = None,
             mask: Optional[np.ndarray] = None) -> np.ndarray:
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    units = str(p["units"])
    if units not in UNITS:
        raise ValueError(f"units 必须是 {list(UNITS)}，收到 {units!r}")
    scale = float(p["scale"])
    neutral = float(p["neutral"])

    img = np.asarray(image, dtype=np.float64)
    vec = np.asarray(vector, dtype=np.float64)
    h, w = img.shape[0], img.shape[1]

    vx, vy = _read_vector(vec, neutral, h, w)
    if units == "Pixels":
        dx = (vx - neutral) * scale
        dy = (vy - neutral) * scale
    else:  # UV：画幅归一化比例
        dx = (vx - neutral) * scale * w
        dy = (vy - neutral) * scale * h

    if mask is not None:
        msk = np.asarray(mask, dtype=np.float64)
        if msk.ndim == 2:
            m = msk
        else:
            m = msk[..., 3] if msk.shape[-1] == 4 else msk[..., 0]
        if m.shape != (h, w):
            m = _clamp_read(m, h, w)
        dx = dx * m
        dy = dy * m

    yy, xx = np.meshgrid(np.arange(h, dtype=np.float64),
                         np.arange(w, dtype=np.float64), indexing="ij")
    out = _bilinear(img, yy + dy, xx + dx)
    return out.astype(np.float32)
