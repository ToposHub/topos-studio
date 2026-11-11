# -*- coding: utf-8 -*-
"""CW-B04:HS 网格求解操作测试(权重/AutoLock/接缝/Pin/平滑/Pull-Push/Luma)。"""
from __future__ import annotations

import numpy as np
import pytest

from src.features.color.core.warper_hs_solver import (
    apply_move,
    locked_cells,
    neighbor_weights,
    pull_push,
    smooth_component,
    with_luma_component,
)
from src.features.color.core.warper_state import (
    WarperGrid,
    WarperPin,
    WarperStateError,
    make_point_id,
    legacy_ndarray,
    resample_grid,
)


def _grid(rows: int = 6, cols: int = 6, pins: tuple[str, ...] = ()) -> WarperGrid:
    return WarperGrid(
        name="hs", rows=rows, cols=cols, components=2, semantics=("dh", "ds"),
        offsets=WarperGrid.identity("hs", rows, cols, ("dh", "ds")).offsets,
        pins=tuple(WarperPin(point_id=p) for p in pins))


# ---- 邻域权重 ----

def test_falloff_weights_exact_and_zero() -> None:
    grid = _grid()
    weights = neighbor_weights(grid, [(2, 2)], falloff=0.5)
    assert weights[(2, 2)] == 1.0
    assert weights[(2, 3)] == pytest.approx(0.5)   # 切比雪夫 1(含对角)
    assert weights[(3, 3)] == pytest.approx(0.5)   # 对角仍为切比雪夫 1
    assert weights[(4, 4)] == pytest.approx(0.25)  # 切比雪夫 2
    assert weights[(2, 5)] == pytest.approx(0.125) # 切比雪夫 3
    assert (0, 0) in weights and weights[(0, 0)] == pytest.approx(0.25)  # 切比雪夫 2
    only = neighbor_weights(grid, [(1, 1)], falloff=0.0)
    assert set(only) == {(1, 1)}


def test_weights_wrap_across_hue_seam() -> None:
    # 列 0 与列 N-1 相邻:接缝两侧编辑连续
    grid = _grid(4, 8)
    weights = neighbor_weights(grid, [(1, 0)], falloff=0.5)
    assert weights[(1, 7)] == pytest.approx(0.5)  # 跨缝邻接 = 距离 1
    assert weights[(1, 1)] == pytest.approx(0.5)  # 与近侧对称
    assert weights[(1, 6)] == pytest.approx(0.25) # 跨缝距离 2


def test_multi_selection_takes_max_weight() -> None:
    grid = _grid(6, 6)
    weights = neighbor_weights(grid, [(1, 1), (3, 3)], falloff=0.5)
    assert weights[(2, 2)] == pytest.approx(0.5)  # 与两选点均距离 1,取最大
    assert weights[(1, 1)] == 1.0 and weights[(3, 3)] == 1.0


# ---- Auto Lock 与 Pin ----

def test_auto_lock_neighborhood_exact() -> None:
    grid = _grid(6, 6)
    selected = [(2, 2)]
    level1 = locked_cells(grid, selected, auto_lock=1)
    assert (2, 1) in level1 and (1, 2) in level1 and (2, 3) in level1 and (3, 2) in level1
    assert (1, 1) not in level1  # 曼哈顿 2 > 1(对角不锁)
    assert (2, 2) not in level1  # 选点本身不锁
    level2 = locked_cells(grid, selected, auto_lock=2)
    assert (1, 1) in level2      # 曼哈顿 2
    assert (0, 2) in level2 and (4, 2) in level2
    assert (4, 4) not in level2  # 曼哈顿 4


def test_auto_lock_wraps_columns() -> None:
    grid = _grid(4, 8)
    locked = locked_cells(grid, [(1, 0)], auto_lock=1)
    assert (1, 7) in locked  # 跨缝曼哈顿 1


def test_pins_always_locked() -> None:
    grid = _grid(pins=(make_point_id("hs", 2, 3),))
    locked = locked_cells(grid, [(2, 2)], auto_lock=0)
    assert (2, 3) in locked  # 无 Auto Lock,仅 Pin


def test_apply_move_respects_locks_and_falloff() -> None:
    grid = _grid(pins=(make_point_id("hs", 2, 3),))
    moved = apply_move(grid, [(2, 2)], {"dh": 0.1}, falloff=0.5, auto_lock=1)
    assert moved.offsets[2, 2, 0] == pytest.approx(0.1)      # 选点全量
    assert moved.offsets[2, 1, 0] == pytest.approx(0.0)      # AutoLock 1 级锁
    assert moved.offsets[2, 4, 0] == pytest.approx(0.025)    # 切比雪夫 2 → 0.1*0.25
    assert moved.offsets[2, 3, 0] == pytest.approx(0.0)      # Pin 格不动
    assert moved.offsets[2, 2, 1] == 0.0                     # 未提及分量不动


def test_apply_move_absolute_interpolates_neighborhood() -> None:
    grid = _grid()
    grid = grid.with_offsets(np.full((6, 6, 2), 0.2, np.float32))
    moved = apply_move(grid, [(2, 2)], {"dh": 0.8}, falloff=0.5, absolute=True)
    assert moved.offsets[2, 2, 0] == pytest.approx(0.8)      # 选点=目标值
    assert moved.offsets[2, 3, 0] == pytest.approx(0.2 * 0.5 + 0.8 * 0.5)
    assert moved.offsets[2, 4, 0] == pytest.approx(0.2 * 0.75 + 0.8 * 0.25)


def test_empty_selection_changes_nothing() -> None:
    grid = _grid()
    moved = apply_move(grid, [], {"dh": 0.5}, falloff=0.8)
    assert moved is grid  # 同一对象:无模型变更
    pulled = pull_push(grid, [], 2.0)
    assert pulled is grid


def test_invalid_inputs_rejected() -> None:
    grid = _grid()
    with pytest.raises(WarperStateError):
        neighbor_weights(grid, [(0, 0)], falloff=1.5)
    with pytest.raises(WarperStateError):
        apply_move(grid, [(0, 0)], {"zz": 0.1}, falloff=0.0)
    with pytest.raises(WarperStateError):
        pull_push(grid, [(0, 0)], -0.5)
    with pytest.raises(WarperStateError):
        smooth_component(grid, "zz")


# ---- 平滑 ----

def test_smooth_component_wraps_and_keeps_other_components() -> None:
    grid = _grid(4, 8)
    offsets = grid.mutable_offsets()
    offsets[1, 0, 0] = 0.8          # 接缝列的尖峰
    offsets[1, 1, 1] = 0.4          # 另一分量,不得被平滑
    grid = grid.with_offsets(offsets)
    smoothed = smooth_component(grid, "dh", selected=[(1, 0)])
    # 3×3 均值:列环绕使列 7 参与平均 → (0.8 + 0 + ... 9 格中含跨缝两格)
    expected = 0.8 / 9.0
    assert smoothed.offsets[1, 0, 0] == pytest.approx(expected, abs=1e-7)
    assert smoothed.offsets[1, 1, 1] == pytest.approx(0.4)  # ds 不动


def test_smooth_keeps_pins() -> None:
    grid = _grid(pins=(make_point_id("hs", 2, 2),))
    offsets = grid.mutable_offsets()
    offsets[:, :, 0] = 0.1
    offsets[2, 2, 0] = 0.9   # Pin 格尖峰
    grid = grid.with_offsets(offsets)
    smoothed = smooth_component(grid, "dh")  # 空选择=全网格
    assert smoothed.offsets[2, 2, 0] == pytest.approx(0.9)   # Pin 格保持
    assert smoothed.offsets[2, 1, 0] == pytest.approx((0.9 + 8 * 0.1) / 9, abs=1e-7)
    assert smoothed.offsets[0, 0, 0] == pytest.approx(0.1, abs=1e-7)  # 远离尖峰不受影响


def test_smooth_across_seam_is_symmetric() -> None:
    grid = _grid(4, 8)
    offsets = grid.mutable_offsets()
    offsets[2, 0, 0] = 0.9
    offsets[2, 7, 0] = 0.9        # 接缝两侧同值尖峰
    grid = grid.with_offsets(offsets)
    smoothed = smooth_component(grid, "dh", selected=[(2, 0), (2, 7)])
    left = smoothed.offsets[2, 0, 0]
    right = smoothed.offsets[2, 7, 0]
    # 两侧输入对称 → 输出相等(接缝连续性的直接断言)
    assert left == pytest.approx(right, abs=1e-7)


# ---- Pull / Push ----

def test_pull_push_scales_selected_offsets() -> None:
    grid = _grid()
    offsets = grid.mutable_offsets()
    offsets[1, 1, 0] = 0.4
    offsets[1, 1, 1] = -0.2
    offsets[3, 3, 0] = 0.6
    grid = grid.with_offsets(offsets)
    pulled = pull_push(grid, [(1, 1)], 0.5)
    assert pulled.offsets[1, 1, 0] == pytest.approx(0.2)
    assert pulled.offsets[1, 1, 1] == pytest.approx(-0.1)
    assert pulled.offsets[3, 3, 0] == pytest.approx(0.6)  # 非选点不动


# ---- Luma 第三分量 ----

def test_with_luma_component_upgrade_and_legacy_invalidation() -> None:
    grid = _grid()
    offsets = grid.mutable_offsets()
    offsets[2, 2, 0] = 0.1
    grid = grid.with_offsets(offsets)
    upgraded = with_luma_component(grid)
    assert upgraded.semantics == ("dh", "ds", "dl")
    np.testing.assert_array_equal(upgraded.offsets[:, :, :2], grid.offsets)
    assert not upgraded.offsets[:, :, 2].any()
    assert with_luma_component(upgraded) is upgraded  # 幂等
    # 升级后 legacy 投影失效(不能把 dl 塞进旧两分量纹理)
    from src.features.color.core.warper_state import legacy_from_ndarray
    state = legacy_from_ndarray(np.zeros((6, 6, 2), np.float32))
    state = state.with_grid(upgraded)
    assert legacy_ndarray(state) is None


def test_luma_edit_via_upgrade_path() -> None:
    # 自动升级发生在命令服务层(move_points 检测 dl → with_luma_component)
    from src.features.color.core.layer import Layer
    from src.features.color.core.warper_commands import WarperCommandService
    from src.features.color.core.warper_state import WarperCommandRequest, legacy_from_ndarray

    layer = Layer("L")
    layer.warper_state = legacy_from_ndarray(np.zeros((6, 6, 2), np.float32))
    service = WarperCommandService()
    service.handle(WarperCommandRequest(
        request_id="r", phase="begin", operation="move_points", payload={},
        gesture_id="g", layer_id=layer.id), layer)
    result = service.handle(WarperCommandRequest(
        request_id="r2", phase="update", operation="move_points",
        payload={"grid": "hs", "selected": [[1, 1]], "deltas": {"dl": 0.3}},
        gesture_id="g", layer_id=layer.id), layer)
    assert result.ok and result.dirty
    grid = layer.warper_state.grids["hs"]
    assert grid.semantics == ("dh", "ds", "dl")
    assert grid.offsets[1, 1, 2] == pytest.approx(0.3)
    assert grid.offsets[1, 1, 0] == 0.0 and grid.offsets[1, 1, 1] == 0.0
    # 裸求解器对两分量网格的 dl 仍显式拒绝(升级只经命令层)
    with pytest.raises(WarperStateError):
        apply_move(_grid(), [(1, 1)], {"dl": 0.3}, falloff=0.0)


# ---- 与重采样的组合(B04 验证:Pin 在重采样后按约定固定) ----

def test_pins_survive_resample_after_edits() -> None:
    grid = _grid(pins=(make_point_id("hs", 3, 3),))
    moved = apply_move(grid, [(1, 1)], {"dh": 0.2}, falloff=0.6)
    up = resample_grid(moved, 12, 12)
    # 端点对齐映射:3 → 3*11/5 = 6.6 → 最近 7
    assert [p.point_id for p in up.pins] == [make_point_id("hs", 7, 7)]
    down = resample_grid(up, 6, 6)
    assert [p.point_id for p in down.pins] == [make_point_id("hs", 3, 3)]


@pytest.mark.parametrize("rows,cols", [(6, 6), (8, 12), (24, 24)])
def test_dense_grids_accept_edits_and_reset(rows: int, cols: int) -> None:
    grid = WarperGrid.identity("hs", rows, cols, ("dh", "ds"))
    moved = apply_move(grid, [(rows - 2, cols - 2)], {"dh": 0.15, "ds": -0.1},
                       falloff=0.7)
    assert moved.offsets.any()
    reset = WarperGrid.identity("hs", rows, cols, ("dh", "ds"))
    assert not reset.offsets.any()
