"""F13 print_registration —— 独立 CPU 数学参考（oracle）。

与 filters/print_registration/print_registration.tglsl 逐式同构
（references/oracle_conventions.md）；网点几何沿用 plugins/Halftone_Print.tglsl 惯例：
- 三版（C/M/Y）网屏角度 0°/15°/30°（角度差 15°，const 旋转矩阵同值），
  pitch 固定 8px；胞元内 r = sqrt(1−clamp(src,0,1))、
  cov = 1 − smoothstep(r−0.06, r+0.06, d)；
- 各版采样位：C 版 q−off、M 版 q、Y 版 q+off（off = (reg_offset_x,
  reg_offset_y) px，M 版基准对称错位）；双线性采样 + clamp-to-edge；
- 分离模式：Luma=三版同源 Rec.709 luma；CMY approx=C←R、M←G、Y←B；
- 叠印（每版只吸收管辖通道，C→R/M→G/Y→B 乘性掩码）：
  paper = mix(img, 1, paper_base·ink_amount)
  out.R = paper.r·(1 − ink·cov_C)；out.G/B 同理；
- ink_amount=0 精确恒等（任意 paper_base）；clamp 仅作用于网点几何参数。

输出为纸反射域（显示域艺术效果，显式声明——HDR 输入仅经几何参数域收敛）。
"""

from __future__ import annotations

from math import cos, sin
from typing import Any, Mapping

import numpy as np

LUMA_W = np.array([0.2126, 0.7152, 0.0722], dtype=np.float64)

PITCH = 8.0
DOT_EDGE = 0.06

# 网屏旋转矩阵（列主序 [ca, sa, -sa, ca]，与 GLSL const mat2 同值）
def _rot(deg: float) -> np.ndarray:
    a = np.deg2rad(deg)
    ca, sa = cos(a), sin(a)
    return np.array([[ca, -sa], [sa, ca]], dtype=np.float64)

ROT_C = _rot(0.0)
ROT_M = _rot(15.0)
ROT_Y = _rot(30.0)

DEFAULTS: dict[str, Any] = {
    "cmyk_separation": "CMY approx",
    "reg_offset_x": 1.0,
    "reg_offset_y": 0.0,
    "paper_base": 0.9,
    "ink_amount": 0.8,
}


def _bilinear(field: np.ndarray, ys: np.ndarray, xs: np.ndarray) -> np.ndarray:
    """clamp-to-edge 双线性采样（像素中心 = 整数索引）。field (H,W) 或 (H,W,C)。"""
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


def _dot_cov(src: np.ndarray, scr_y: np.ndarray, scr_x: np.ndarray) -> np.ndarray:
    """网点覆盖率（Halftone_Print 几何）：scr_* 为旋转后网屏坐标分量。"""
    l = np.clip(src, 0.0, 1.0)
    r = np.sqrt(1.0 - l)
    fy = scr_y - np.floor(scr_y)
    fx = scr_x - np.floor(scr_x)
    d = np.sqrt((fy - 0.5) ** 2 + (fx - 0.5) ** 2)
    t = np.clip((d - (r - DOT_EDGE)) / (2.0 * DOT_EDGE), 0.0, 1.0)
    s = t * t * (3.0 - 2.0 * t)
    return 1.0 - s


def evaluate(image: np.ndarray, params: Mapping[str, Any] | None = None) -> np.ndarray:
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    img = np.asarray(image, dtype=np.float64)
    h, w = img.shape[0], img.shape[1]

    ink = float(p["ink_amount"])
    if ink < 0.001:
        return img.astype(np.float32)

    ox = float(p["reg_offset_x"])
    oy = float(p["reg_offset_y"])
    paper_base = float(p["paper_base"])
    cmyk = (p["cmyk_separation"] == "CMY approx")

    yy, xx = np.meshgrid(np.arange(h, dtype=np.float64),
                         np.arange(w, dtype=np.float64), indexing="ij")

    def plate_src(dy: float, dx: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """返回 (srcC, srcM, srcY)：各版在偏移采样位取源。"""
        sy = yy + dy
        sx = xx + dx
        if cmyk:
            s_c = _bilinear(img[..., 0], sy - oy, sx - ox)
            s_m = _bilinear(img[..., 1], sy, sx)
            s_y = _bilinear(img[..., 2], sy + oy, sx + ox)
        else:
            luma = img @ LUMA_W
            s_c = _bilinear(luma, sy - oy, sx - ox)
            s_m = _bilinear(luma, sy, sx)
            s_y = _bilinear(luma, sy + oy, sx + ox)
        return s_c, s_m, s_y

    src_c, src_m, src_y = plate_src(0.0, 0.0)

    def screen(rot: np.ndarray, dy: float, dx: float) -> tuple[np.ndarray, np.ndarray]:
        """旋转后网屏坐标分量（采样位 + 0.5)/PITCH 旋转）。"""
        qy = yy + dy + 0.5
        qx = xx + dx + 0.5
        sy = (rot[1, 0] * qx + rot[1, 1] * qy) / PITCH
        sx = (rot[0, 0] * qx + rot[0, 1] * qy) / PITCH
        return sy, sx

    # 三版：C（−off，0°）/ M（基准，15°）/ Y（+off，30°）
    scr_cy, scr_cx = screen(ROT_C, -oy, -ox)
    scr_my, scr_mx = screen(ROT_M, 0.0, 0.0)
    scr_yy_, scr_yx = screen(ROT_Y, +oy, +ox)

    cov_c = _dot_cov(src_c, scr_cy, scr_cx)
    cov_m = _dot_cov(src_m, scr_my, scr_mx)
    cov_y = _dot_cov(src_y, scr_yy_, scr_yx)

    paper = img * (1.0 - paper_base * ink) + paper_base * ink
    out = np.empty_like(img)
    out[..., 0] = paper[..., 0] * (1.0 - ink * cov_c)
    out[..., 1] = paper[..., 1] * (1.0 - ink * cov_m)
    out[..., 2] = paper[..., 2] * (1.0 - ink * cov_y)
    return out.astype(np.float32)
