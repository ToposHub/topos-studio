# -*- coding: utf-8 -*-
"""CW-B05:Chroma–Luma 双网格——Topos 声明的坐标、合成与 Auto Pin。

计划:`docs/plans/2026-11-12-color-warper-backend-plan.md` §4 CW-B05。

Topos 声明语义(B00 行为矩阵:Resolve 合成顺序未确认,本模块是可验证
的 Topos 目标,不宣称与私有算法一致):
---------------------------------------------------------------
1. **坐标**:chroma = HSV 饱和度(幅度域),luma = HSP 感知亮度 P
   (常数 0.299/0.587/0.114,B03 冻结)。网格 U=chroma(列=dc),
   V=luma(行=dl);luma 采样坐标钳制到 [0,1](HDR 超亮在外圈行饱和,
   声明行为,不截断图像值本身)。
2. **Axis Angle**:度,[-180,180](B01 域),在 (chroma, luma) 平面绕
   (0.5, 0.5) 旋转采样坐标;0/±180/360 恒等(180 绕心两访同点),
   90° 交换两轴。方向:正角把 chroma 轴转向 luma 轴(右手系,测试冻结)。
3. **合成顺序**:grid1 → grid2 顺序应用(每次应用都经完整
   RGB→(h,c,l)→RGB 往返);非交换,测试断言次序效应。
4. **零色度政策**:dc(色度位移)对 s<0.02 旁路 + smoothstep(0.02,0.15)
   衰减(灰阶不被染色);dl(亮度位移)对任意像素生效(灰阶亮度单调性)。
5. **输出重建**:s' = clamp01(s + dc·fade);p' = max(0, p + dl);
   RGB' = sign·hsp_to_rgb(h, s', p')(B03 齐次精确反演)。
6. **Auto Pin(Topos 声明)**:columns/rows = 选点所在列/行全列 Pin
   (空选择=全部列/行);single = 仅选点。自动生成的 Pin 带
   ``generated=auto`` 标记(extras),reset pins 一并清除(声明政策)。

线程边界:纯函数;冻结数组进出;无 Qt/GPU。
"""
from __future__ import annotations

import math

import numpy as np

from src.features.color.core.warper_color import (
    HSP_WEIGHTS,
    hsp_to_rgb,
    rgb_to_hsp,
    rgb_to_hsv,
    sample_warper_bilinear,
    smoothstep,
)
from src.features.color.core.warper_state import WarperGrid, WarperPin, WarperStateError

__all__ = ["cl_coordinates", "apply_cl_grid", "apply_cl_grids", "auto_pins"]

_CENTER = 0.5


def cl_coordinates(rgb) -> tuple[float, float, float]:
    """(hue, chroma, luma):chroma=HSV sat,luma=HSP P(幅度域,负通道 abs)。"""
    magnitude = np.abs(np.asarray(rgb, dtype=np.float64))
    if not np.isfinite(magnitude).all():
        raise ValueError("颜色必须为有限值")
    hsv = rgb_to_hsv(magnitude)
    luma = float(np.sqrt(np.dot(HSP_WEIGHTS, magnitude * magnitude)))
    return float(hsv[0]), float(hsv[1]), luma


def _rotate(u: float, v: float, angle_deg: float) -> tuple[float, float]:
    """绕 (0.5, 0.5) 旋转采样坐标;正角 chroma→luma(测试冻结方向)。"""
    theta = math.radians(angle_deg)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    du, dv = u - _CENTER, v - _CENTER
    return (du * cos_t - dv * sin_t + _CENTER, du * sin_t + dv * cos_t + _CENTER)


def apply_cl_grid(rgb, grid: WarperGrid, *, axis_angle: float = 0.0) -> np.ndarray:
    """单个 CL 网格应用到一像素(顺序合成的最小单元)。"""
    if grid.semantics[:2] != ("dc", "dl"):
        raise WarperStateError("invalid_semantics", f"CL 网格需要 (dc,dl),得到 {grid.semantics}")
    color = np.asarray(rgb, dtype=np.float64)
    sign_c = np.sign(color)
    hue, chroma, luma = cl_coordinates(color)
    # luma 采样坐标钳制(超亮 HDR 在外圈行饱和);chroma 由转换保证 [0,1]
    sample_u, sample_v = _rotate(chroma, min(max(luma, 0.0), 1.0), axis_angle)
    delta = sample_warper_bilinear(grid.offsets, sample_u, sample_v)
    # 零色度保护只作用于色度分量;亮度对灰阶完全生效
    fade = smoothstep(0.02, 0.15, chroma)
    out_chroma = min(max(chroma + float(delta[0]) * fade, 0.0), 1.0)
    out_luma = max(luma + float(delta[1]), 0.0)
    return sign_c * hsp_to_rgb(np.array([hue, out_chroma, out_luma]))


def apply_cl_grids(rgb, grid1: WarperGrid, grid2: WarperGrid,
                   *, axis_angle: float = 0.0) -> np.ndarray:
    """顺序合成 grid1 → grid2(Topos 声明,测试冻结次序)。"""
    once = apply_cl_grid(rgb, grid1, axis_angle=axis_angle)
    return apply_cl_grid(once, grid2, axis_angle=axis_angle)


def auto_pins(grid: WarperGrid, mode: str,
              selected: list[tuple[int, int]] | None = None) -> WarperGrid:
    """Auto Pin Columns / Rows / Single(Topos 声明,见模块 docstring §6)。

    自动 Pin 以 ``generated=auto`` extras 标记;与手动 Pin 取并集
    (point_id 去重,手动优先保留其 extras)。
    """
    if mode not in ("columns", "rows", "single"):
        raise WarperStateError("invalid_param", f"auto_pin mode {mode!r} 非法")
    cells = set(selected or [])
    if mode == "single":
        targets = cells
    elif mode == "columns":
        columns = {c for _r, c in cells} or set(range(grid.cols))
        targets = {(r, c) for r in range(grid.rows) for c in columns}
    else:  # rows
        rows = {r for r, _c in cells} or set(range(grid.rows))
        targets = {(r, c) for r in rows for c in range(grid.cols)}

    existing = {pin.point_id: pin for pin in grid.pins}
    for (row, col) in sorted(targets):
        point_id = f"{grid.name}:{row}:{col}"
        if point_id not in existing:
            existing[point_id] = WarperPin(
                point_id=point_id, _extra_fields={"generated": "auto"})
    return WarperGrid(
        name=grid.name, rows=grid.rows, cols=grid.cols, components=grid.components,
        semantics=grid.semantics, offsets=grid.offsets,
        pins=tuple(existing[p] for p in sorted(existing)),
        _extra_fields=grid._extra_fields)
