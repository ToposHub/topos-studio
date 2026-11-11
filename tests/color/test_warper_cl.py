# -*- coding: utf-8 -*-
"""CW-B05:Chroma–Luma 双网格测试(坐标/合成/Auto Pin/角度/单调性)。"""
from __future__ import annotations

import numpy as np
import pytest

from src.features.color.core.warper_cl import (
    apply_cl_grid,
    apply_cl_grids,
    auto_pins,
    cl_coordinates,
)
from src.features.color.core.warper_state import (
    CURRENT_ALGORITHM,
    WARPER_SCHEMA_VERSION,
    WarperGrid,
    WarperPin,
    WarperStateError,
    WarperStateV2,
    make_point_id,
)


def _cl_grid(rows: int = 6, cols: int = 6, dc: float = 0.0, dl: float = 0.0,
             name: str = "grid1") -> WarperGrid:
    base = WarperGrid.identity(name, rows, cols, ("dc", "dl"))
    if dc == 0.0 and dl == 0.0:
        return base
    offsets = base.mutable_offsets()
    offsets[:, :, 0] = dc
    offsets[:, :, 1] = dl
    return base.with_offsets(offsets)


# ---- 坐标 ----

def test_cl_coordinates_gray_has_zero_chroma() -> None:
    for value in (0.0, 0.25, 0.5, 1.0, 10.0):
        hue, chroma, luma = cl_coordinates((value, value, value))
        assert chroma == 0.0
        assert hue == 0.0
        assert luma == pytest.approx(value)


def test_cl_coordinates_colored() -> None:
    hue, chroma, luma = cl_coordinates((0.2, 0.4, 0.1))
    assert 0.0 < chroma < 1.0
    assert luma > 0.0
    hue2, chroma2, _ = cl_coordinates((-0.2, 0.4, 0.1))  # 负通道:幅度域
    assert hue2 == pytest.approx(hue) and chroma2 == pytest.approx(chroma)


# ---- 恒等与应用 ----

def test_identity_grids_are_identity() -> None:
    grid = _cl_grid()
    for color in ((0.2, 0.4, 0.1), (0.5, 0.5, 0.5), (2.0, 1.5, 0.5),
                  (-0.5, 0.0, 0.2), (10.0, 10.0, 10.0), (0.0, 0.0, 0.0)):
        out = apply_cl_grids(color, grid, _cl_grid(name="grid2"))
        np.testing.assert_allclose(out, color, rtol=1e-9, atol=1e-9)


def test_wrong_semantics_rejected() -> None:
    hs_grid = WarperGrid.identity("hs", 6, 6, ("dh", "ds"))
    with pytest.raises(WarperStateError):
        apply_cl_grid((0.5, 0.5, 0.5), hs_grid)


# ---- 灰阶亮度单调性 / 分量解耦(B05 验证核心) ----

def test_gray_luma_monotonic_and_never_colored() -> None:
    grid = _cl_grid(dl=0.15)
    previous = -1.0
    for value in np.linspace(0.05, 0.95, 10):
        out = apply_cl_grid((value, value, value), grid)
        # 仍是灰:零色度政策,dc 不给灰阶染色
        assert out.max() - out.min() < 1e-9
        luma = out[0]
        assert luma > previous  # 亮度单调递增
        previous = luma
        # dl 以 float32 网格存储,精度即 float32 量级
        assert luma == pytest.approx(value + 0.15, abs=1e-6)


def test_dl_below_zero_clamps_to_black_not_negative() -> None:
    grid = _cl_grid(dl=-1.0)
    out = apply_cl_grid((0.1, 0.1, 0.1), grid)
    assert np.all(out >= 0.0)


def test_dc_only_grid_leaves_grays_alone() -> None:
    grid = _cl_grid(dc=0.5)
    out = apply_cl_grid((0.5, 0.5, 0.5), grid)
    np.testing.assert_allclose(out, (0.5, 0.5, 0.5), atol=1e-9)


def test_dc_changes_chroma_on_colored_pixels() -> None:
    grid = _cl_grid(dc=0.3)
    color = (0.2, 0.4, 0.1)  # sat = d/(max+e) ≈ 0.75
    out = apply_cl_grid(color, grid)
    _, chroma_before, _ = cl_coordinates(color)
    _, chroma_after, _ = cl_coordinates(out)
    assert chroma_after == pytest.approx(min(1.0, chroma_before + 0.3), abs=1e-7)


# ---- Axis Angle ----

def test_axis_angle_zero_and_full_circle_are_identity() -> None:
    # 非均匀网格:角度不同结果不同,但 0 与 360(与 -360)必须一致
    base = WarperGrid.identity("grid1", 6, 6, ("dc", "dl"))
    offsets = base.mutable_offsets()
    offsets[:, 3:, 1] = 0.2   # 右半(高 chroma 区)加亮度
    grid = base.with_offsets(offsets)
    color = (0.6, 0.3, 0.2)
    base = apply_cl_grid(color, grid, axis_angle=0.0)
    full = apply_cl_grid(color, grid, axis_angle=360.0)
    negative = apply_cl_grid(color, grid, axis_angle=-360.0)
    np.testing.assert_allclose(base, full, atol=1e-9)
    np.testing.assert_allclose(base, negative, atol=1e-9)


def test_axis_angle_90_swaps_axes() -> None:
    # 左半网格加亮度:0° 时作用于低 chroma;90° 旋转后作用于低 luma 区
    base = WarperGrid.identity("grid1", 4, 4, ("dc", "dl"))
    offsets = base.mutable_offsets()
    offsets[:, :2, 1] = 0.25  # 低 chroma 半区
    grid = base.with_offsets(offsets)
    low_chroma_high_luma = (0.3, 0.28, 0.26)   # sat≈0.13, luma≈0.28
    high_chroma_low_luma = (0.05, 0.02, 0.30)  # sat≈0.93, luma≈0.12
    out = apply_cl_grid(low_chroma_high_luma, grid, axis_angle=0.0)
    assert cl_coordinates(out)[2] > cl_coordinates(low_chroma_high_luma)[2]  # 命中左半
    out_rotated = apply_cl_grid(high_chroma_low_luma, grid, axis_angle=90.0)
    assert cl_coordinates(out_rotated)[2] > cl_coordinates(high_chroma_low_luma)[2]  # 90° 后命中


# ---- 合成顺序 ----

def test_composition_order_grid1_then_grid2_frozen() -> None:
    # 非均匀网格制造交互:dc 随 luma 行变化 → 先动 luma 会改变后续采样行
    base = WarperGrid.identity("grid1", 6, 6, ("dc", "dl"))
    offsets = base.mutable_offsets()
    offsets[:, :, 0] = np.linspace(0.0, 0.4, 6)[:, None]  # 行(luma)相关 dc
    grid_luma_dependent_chroma = base.with_offsets(offsets)
    grid_luma = _cl_grid(dl=-0.35, name="grid2")
    color = (0.6, 0.3, 0.2)
    forward = apply_cl_grids(color, grid_luma_dependent_chroma, grid_luma)
    backward = apply_cl_grids(color, grid_luma, grid_luma_dependent_chroma)
    assert not np.allclose(forward, backward, atol=1e-6)  # 次序可分辨
    # 显式次序断言:合成 = grid2(grid1(color)) 逐层应用
    manual = apply_cl_grid(apply_cl_grid(color, grid_luma_dependent_chroma), grid_luma)
    np.testing.assert_allclose(forward, manual, atol=1e-12)


# ---- Auto Pin ----

def test_auto_pin_modes() -> None:
    grid = WarperGrid.identity("grid1", 4, 4, ("dc", "dl"))
    columns = auto_pins(grid, "columns", [(1, 2)])
    ids = {p.point_id for p in columns.pins}
    assert ids == {f"grid1:{r}:2" for r in range(4)}
    rows = auto_pins(grid, "rows", [(1, 2)])
    assert {p.point_id for p in rows.pins} == {f"grid1:1:{c}" for c in range(4)}
    single = auto_pins(grid, "single", [(1, 2), (3, 0)])
    assert {p.point_id for p in single.pins} == {"grid1:1:2", "grid1:3:0"}


def test_auto_pin_empty_selection_means_all() -> None:
    grid = WarperGrid.identity("grid1", 3, 3, ("dc", "dl"))
    columns = auto_pins(grid, "columns", None)
    assert len(columns.pins) == 9
    rows = auto_pins(grid, "rows", None)
    assert len(rows.pins) == 9


def test_auto_pin_unions_with_manual_and_marks_generated() -> None:
    grid = WarperGrid(
        name="grid1", rows=3, cols=3, components=2, semantics=("dc", "dl"),
        offsets=WarperGrid.identity("grid1", 3, 3, ("dc", "dl")).offsets,
        pins=(WarperPin(point_id=make_point_id("grid1", 0, 0)),))
    result = auto_pins(grid, "single", [(0, 0), (1, 1)])
    by_id = {p.point_id: p for p in result.pins}
    assert set(by_id) == {"grid1:0:0", "grid1:1:1"}
    assert by_id["grid1:0:0"]._extra_fields == {}            # 手动 Pin 保持无标记
    assert by_id["grid1:1:1"]._extra_fields == {"generated": "auto"}


def test_auto_pin_generated_marker_survives_round_trip() -> None:
    grid = auto_pins(WarperGrid.identity("grid1", 3, 3, ("dc", "dl")), "columns", [(0, 1)])
    state = WarperStateV2(
        schema_version=WARPER_SCHEMA_VERSION, algorithm_version=CURRENT_ALGORITHM,
        mode="chroma_luma", color_space="hsv", axis_angle=0.0,
        grids={"grid1": grid,
               "grid2": WarperGrid.identity("grid2", 3, 3, ("dc", "dl"))})
    restored = WarperStateV2.from_dict(state.to_dict())
    pin = restored.grids["grid1"].pins[0]
    assert pin.to_dict()["generated"] == "auto"


def test_auto_pin_invalid_mode_rejected() -> None:
    grid = WarperGrid.identity("grid1", 3, 3, ("dc", "dl"))
    with pytest.raises(WarperStateError):
        auto_pins(grid, "diagonal", [(0, 0)])


def test_service_auto_pin_and_reset_clears_all() -> None:
    from src.features.color.core.layer import Layer
    from src.features.color.core.warper_commands import WarperCommandService
    from src.features.color.core.warper_state import WarperCommandRequest

    layer = Layer("cl")
    state = WarperStateV2(
        schema_version=WARPER_SCHEMA_VERSION, algorithm_version=CURRENT_ALGORITHM,
        mode="chroma_luma", color_space="hsv", axis_angle=0.0,
        grids={"grid1": WarperGrid.identity("grid1", 3, 3, ("dc", "dl")),
               "grid2": WarperGrid.identity("grid2", 3, 3, ("dc", "dl"))})
    layer.warper_state = state
    service = WarperCommandService()
    service.handle(WarperCommandRequest(
        request_id="a", phase="begin", operation="auto_pin", payload={},
        gesture_id="g", layer_id=layer.id), layer)
    result = service.handle(WarperCommandRequest(
        request_id="b", phase="update", operation="auto_pin",
        payload={"grid": "grid1", "mode": "columns", "selected": [[0, 2]]},
        gesture_id="g", layer_id=layer.id), layer)
    assert result.ok and result.dirty
    assert len(layer.warper_state.grids["grid1"].pins) == 3
    # reset pins 清除自动+手动(声明政策)
    service.handle(WarperCommandRequest(
        request_id="c", phase="update", operation="reset",
        payload={"scope": "pins"}, gesture_id="g", layer_id=layer.id), layer)
    assert layer.warper_state.grids["grid1"].pins == ()
