# -*- coding: utf-8 -*-
"""CW-B04:Hue–Saturation 网格求解操作(纯函数,无 Qt/GPU)。

计划:`docs/plans/2026-11-12-color-warper-backend-plan.md` §4 CW-B04。

Topos 声明的语义(B00 行为矩阵「实测/Topos 声明」分级;Resolve 精确
拓扑未确认,不宣称一致):
---------------------------------------------------------------
1. **邻域影响权重**:选点权重 1;其余格点对每个选点取
   ``falloff ** dist``(dist = 切比雪夫距离;色相列环绕计数),
   多选点取最大值。``falloff∈[0,1]``,0 表示只有选点本身移动。
2. **Auto Lock(1/2 Points)**:与任一选点曼哈顿距离 ≤ level 的格点
   锁定(选点本身除外);Pin 的格点恒锁定。锁定格权重置 0。
3. **色相接缝**:列方向环绕(列 0 与列 N−1 相邻),权重/平滑/邻域
   均跨缝连续;行(饱和度)方向钳制。
4. **Smooth**:对选点集(空选择=全网格)做一次 3×3 均值(色相列环绕),
   只作用指定分量;锁定格保持;非目标分量不动。
5. **Pull/Push**:选点集的位移向量按 ``factor`` 缩放(Pull<1 收缩,
   Push>1 扩张);需要非空选择,否则 no-op;锁定格不动。
6. **Luma 第三分量**:网格语义可为 ``(dh, ds, dl)``;两分量网格不受
   三分量操作影响,数值域/冻结/ID 规则同 B01。

线程边界:纯函数,输入输出均为冻结数组;不持有状态。
"""
from __future__ import annotations

import numpy as np

from src.features.color.core.warper_state import WarperGrid, WarperStateError, _as_offsets

__all__ = [
    "neighbor_weights",
    "locked_cells",
    "apply_move",
    "smooth_component",
    "pull_push",
    "with_luma_component",
]

_HUE_SEMANTICS = ("dh",)


def _wrap_distance(c1: int, c2: int, cols: int) -> int:
    direct = abs(c1 - c2)
    return min(direct, cols - direct)


def _chebyshev_wrap(r1: int, c1: int, r2: int, c2: int, rows: int, cols: int) -> int:
    dr = abs(r1 - r2)
    dc = _wrap_distance(c1, c2, cols)
    return max(dr, dc)


def neighbor_weights(
    grid: WarperGrid,
    selected: list[tuple[int, int]],
    falloff: float,
) -> dict[tuple[int, int], float]:
    """选点=1;邻域按 ``falloff**切比雪夫距离``(色相列环绕)取最大。"""
    if not 0.0 <= falloff <= 1.0:
        raise WarperStateError("invalid_param", f"falloff {falloff} 越界 [0,1]")
    cells = {(r, c) for r, c in selected}
    if not cells:
        return {}
    weights: dict[tuple[int, int], float] = {cell: 1.0 for cell in cells}
    if falloff == 0.0:
        return weights
    radius = 1
    while falloff ** radius > 1e-4 and radius < max(grid.rows, grid.cols):
        ring_weight = falloff ** radius
        for row in range(grid.rows):
            for col in range(grid.cols):
                if (row, col) in cells:
                    continue
                for (sr, sc) in cells:
                    if _chebyshev_wrap(row, col, sr, sc, grid.rows, grid.cols) == radius:
                        if (row, col) not in weights or weights[(row, col)] < ring_weight:
                            weights[(row, col)] = ring_weight
                        break
        radius += 1
    return weights


def locked_cells(
    grid: WarperGrid,
    selected: list[tuple[int, int]],
    auto_lock: int = 0,
) -> set[tuple[int, int]]:
    """Pin 格点 ∪ Auto Lock 邻域(曼哈顿距离 ≤ level,不含选点)。"""
    locked = set()
    for pin in grid.pins:
        try:
            _grid_name, row, col = pin.point_id.rsplit(":", 2)
            locked.add((int(row), int(col)))
        except (ValueError, IndexError):
            continue  # 非本网格坐标语法的 pin 不参与锁定
    if auto_lock > 0:
        cells = {(r, c) for r, c in selected}
        for row in range(grid.rows):
            for col in range(grid.cols):
                if (row, col) in cells:
                    continue
                for (sr, sc) in cells:
                    manhattan = abs(row - sr) + _wrap_distance(col, sc, grid.cols)
                    if manhattan <= auto_lock:
                        locked.add((row, col))
                        break
    return locked


def apply_move(
    grid: WarperGrid,
    selected: list[tuple[int, int]],
    deltas: dict[str, float],
    *,
    falloff: float,
    auto_lock: int = 0,
    absolute: bool = False,
) -> WarperGrid:
    """把位移写到选点并按权重扩散到邻域;锁定格不动。

    ``deltas`` 键为分量语义名("dh"/"ds"/"dl")。absolute=True 时选点
    分量设为给定值(数值框语义);False 时叠加(拖动语义)。
    """
    if not selected:
        return grid  # 无选择:不改模型(计划验证项)
    unknown = set(deltas) - set(grid.semantics)
    if unknown:
        raise WarperStateError("invalid_param", f"分量 {sorted(unknown)} 不在 {grid.semantics}")
    weights = neighbor_weights(grid, selected, falloff)
    locked = locked_cells(grid, selected, auto_lock)
    offsets = grid.mutable_offsets()
    for index, semantic in enumerate(grid.semantics):
        if semantic not in deltas:
            continue
        value = float(deltas[semantic])
        for (row, col), weight in weights.items():
            if (row, col) in locked:
                continue
            if absolute:
                # 选点=目标值;邻域按权重向目标值插值(保持连续场)
                offsets[row, col, index] = (
                    offsets[row, col, index] * (1.0 - weight) + value * weight)
            else:
                offsets[row, col, index] += value * weight
    return grid.with_offsets(offsets)


def smooth_component(
    grid: WarperGrid,
    component: str,
    selected: list[tuple[int, int]] | None = None,
) -> WarperGrid:
    """一次 3×3 均值(色相列环绕);锁定格(Pin)保持;只动目标分量。"""
    if component not in grid.semantics:
        raise WarperStateError("invalid_param", f"分量 {component!r} 不在 {grid.semantics}")
    index = grid.semantics.index(component)
    targets = set(selected) if selected else None
    locked = locked_cells(grid, selected or [])
    offsets = grid.mutable_offsets()
    original = offsets.copy()
    changed = False
    for row in range(grid.rows):
        for col in range(grid.cols):
            if targets is not None and (row, col) not in targets:
                continue
            if (row, col) in locked:
                continue
            values = []
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    rr = min(max(row + dr, 0), grid.rows - 1)       # 行钳制
                    cc = (col + dc) % grid.cols                      # 列环绕
                    values.append(original[rr, cc, index])
            offsets[row, col, index] = float(np.mean(values))
            changed = True
    if not changed:
        return grid
    return grid.with_offsets(offsets)


def pull_push(
    grid: WarperGrid,
    selected: list[tuple[int, int]],
    factor: float,
) -> WarperGrid:
    """选点位移向量按 factor 缩放(Pull<1 / Push>1);空选择 no-op。"""
    if not selected:
        return grid
    if factor < 0.0:
        raise WarperStateError("invalid_param", f"pull_push factor {factor} 不能为负")
    cells = set(selected)
    locked = locked_cells(grid, selected)
    offsets = grid.mutable_offsets()
    for (row, col) in cells:
        if (row, col) in locked:
            continue
        offsets[row, col, :] *= factor
    return grid.with_offsets(offsets)


def with_luma_component(grid: WarperGrid) -> WarperGrid:
    """两分量 HS 网格升级为 (dh, ds, dl);已有 dl 网格原样返回。

    Luma 通道语义独立于旧两分量纹理(B04:不能塞进旧纹理);升级后
    legacy 投影自动失效(``legacy_ndarray`` 拒绝非 2 分量)。
    """
    if "dl" in grid.semantics:
        return grid
    if grid.semantics[:2] != ("dh", "ds"):
        raise WarperStateError("invalid_semantics", f"{grid.semantics} 不支持追加 luma")
    offsets = np.zeros((grid.rows, grid.cols, 3), dtype=np.float32)
    offsets[:, :, :2] = grid.offsets
    return WarperGrid(
        name=grid.name, rows=grid.rows, cols=grid.cols, components=3,
        semantics=("dh", "ds", "dl"),
        offsets=_as_offsets(offsets, grid.rows, grid.cols, 3, f"grid {grid.name}"),
        pins=grid.pins, _extra_fields=grid._extra_fields)
