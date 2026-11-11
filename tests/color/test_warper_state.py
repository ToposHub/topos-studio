# -*- coding: utf-8 -*-
"""CW-B01:WarperStateV2 版本化领域模型契约测试。

覆盖计划 §4 CW-B01 验证清单:legacy 6/8/12 方阵 round-trip、None bypass、
非方阵、双网格、空笔画、未知字段保留、未知版本显式拒绝、NaN/Inf、
越界尺寸、损坏载荷、维数/分量错误、重采样(含 Δhue 圆周)、冻结数组、
能力契约与请求 envelope。
"""
from __future__ import annotations

import numpy as np
import pytest

from src.features.color.core.warper_state import (
    CURRENT_ALGORITHM,
    LEGACY_ALGORITHM,
    WARPER_SCHEMA_VERSION,
    WarperGrid,
    WarperPin,
    WarperPinPoint,
    WarperStateError,
    WarperStateV2,
    WarperStroke,
    describe_warper_capabilities,
    legacy_from_ndarray,
    legacy_ndarray,
    make_point_id,
    resample_grid,
)


def _hs_state(rows: int = 6, cols: int = 8, space: str = "hsp") -> WarperStateV2:
    grid = WarperGrid.identity("hs", rows, cols, ("dh", "ds"))
    return WarperStateV2(
        schema_version=WARPER_SCHEMA_VERSION, algorithm_version=CURRENT_ALGORITHM,
        mode="hue_saturation", color_space=space, axis_angle=0.0, grids={"hs": grid},
    )


def _cl_state() -> WarperStateV2:
    return WarperStateV2(
        schema_version=WARPER_SCHEMA_VERSION, algorithm_version=CURRENT_ALGORITHM,
        mode="chroma_luma", color_space="hsv", axis_angle=30.0,
        grids={
            "grid1": WarperGrid.identity("grid1", 6, 6, ("dc", "dl")),
            "grid2": WarperGrid.identity("grid2", 8, 12, ("dc", "dl")),
        },
    )


# ---- legacy 桥 ----

@pytest.mark.parametrize("size", [6, 8, 12])
def test_legacy_square_round_trip(size: int) -> None:
    array = np.random.default_rng(size).normal(0, 0.05, (size, size, 2)).astype(np.float32)
    state = legacy_from_ndarray(array)
    assert state is not None
    assert state.algorithm_version == LEGACY_ALGORITHM
    assert state.mode == "hue_saturation" and state.color_space == "hsv"
    restored = legacy_ndarray(state)
    assert restored is not None
    np.testing.assert_array_equal(restored, array)


def test_legacy_none_bypass() -> None:
    assert legacy_from_ndarray(None) is None
    assert legacy_ndarray(None) is None


@pytest.mark.parametrize("shape", [(6,), (6, 6), (6, 6, 3), (6, 8, 2), "junk"])
def test_legacy_rejects_non_square(shape) -> None:
    with pytest.raises(WarperStateError):
        legacy_from_ndarray(np.zeros(shape) if not isinstance(shape, str) else shape)


def test_legacy_rejects_non_finite() -> None:
    bad = np.zeros((6, 6, 2), np.float32)
    bad[3, 3, 0] = np.nan
    with pytest.raises(WarperStateError) as info:
        legacy_from_ndarray(bad)
    assert info.value.code == "not_finite"


# ---- V2 round-trip ----

def test_v2_hs_non_square_round_trip() -> None:
    state = _hs_state(rows=8, cols=12)
    data = state.to_dict()
    restored = WarperStateV2.from_dict(data)
    assert restored.mode == "hue_saturation"
    assert restored.grids["hs"].rows == 8 and restored.grids["hs"].cols == 12
    assert restored.grids["hs"].semantics == ("dh", "ds")
    assert restored.color_space == "hsp"
    np.testing.assert_array_equal(restored.grids["hs"].offsets, state.grids["hs"].offsets)


def test_v2_cl_dual_grid_round_trip() -> None:
    state = _cl_state()
    restored = WarperStateV2.from_dict(state.to_dict())
    assert set(restored.grids) == {"grid1", "grid2"}
    assert restored.grids["grid1"].semantics == ("dc", "dl")
    assert restored.axis_angle == pytest.approx(30.0)


def test_v2_cw_empty_strokes_round_trip() -> None:
    state = WarperStateV2(
        schema_version=WARPER_SCHEMA_VERSION, algorithm_version=CURRENT_ALGORITHM,
        mode="chroma_warp", color_space="hsv", axis_angle=0.0, grids={})
    restored = WarperStateV2.from_dict(state.to_dict())
    assert restored.strokes == () and restored.pin_points == ()
    assert restored.is_identity()


def test_v2_cw_stroke_with_params_and_pins() -> None:
    stroke = WarperStroke(
        stroke_id="s1", mode="normal",
        source_rgb=(0.2, 0.4, 0.1), target_rgb=(0.25, 0.45, 0.1),
        params={"chroma_range": 0.04, "tonal_low": 1.0, "tonal_high": 1.0,
                "pivot": 0.5, "exposure": 0.0},
        path=((0.1, 0.2), (0.3, 0.4), (0.5, 0.6)))
    pin = WarperPinPoint(pin_id="p1", position=(0.4, 0.6), params=None)
    state = WarperStateV2(
        schema_version=WARPER_SCHEMA_VERSION, algorithm_version=CURRENT_ALGORITHM,
        mode="chroma_warp", color_space="hsv", axis_angle=0.0, grids={},
        strokes=(stroke,), pin_points=(pin,))
    restored = WarperStateV2.from_dict(state.to_dict())
    assert restored.strokes[0].params["chroma_range"] == pytest.approx(0.04)
    assert restored.pin_points[0].position == (0.4, 0.6)
    assert not restored.is_identity()


def test_unknown_fields_preserved_across_round_trip() -> None:
    state = _hs_state()
    data = state.to_dict()
    data["future_field"] = {"nested": [1, 2, 3]}
    data["grids"]["hs"]["future_grid_field"] = 7
    data["grids"]["hs"]["pins"] = [{"point_id": "hs:2:3", "future_pin_field": "x"}]
    restored = WarperStateV2.from_dict(data)
    out = restored.to_dict()
    assert out["future_field"] == {"nested": [1, 2, 3]}
    assert out["grids"]["hs"]["future_grid_field"] == 7
    assert out["grids"]["hs"]["pins"][0]["future_pin_field"] == "x"


def test_unknown_param_keys_preserved() -> None:
    stroke = WarperStroke.from_dict({
        "stroke_id": "s9", "mode": "normal", "source_rgb": [0, 0, 0],
        "target_rgb": [1, 1, 1], "params": {"chroma_range": 0.1, "v3_weight": 0.7}})
    assert stroke.params["v3_weight"] == 0.7
    assert "v3_weight" in stroke.to_dict()["params"]


# ---- 显式拒绝 ----

def test_unknown_schema_version_rejected() -> None:
    data = _hs_state().to_dict()
    data["schema_version"] = WARPER_SCHEMA_VERSION + 1
    with pytest.raises(WarperStateError) as info:
        WarperStateV2.from_dict(data)
    assert info.value.code == "schema_too_new"


def test_corrupt_payloads_rejected() -> None:
    with pytest.raises(WarperStateError):
        WarperStateV2.from_dict("junk")
    with pytest.raises(WarperStateError):
        WarperStateV2.from_dict({"schema_version": WARPER_SCHEMA_VERSION})  # mode 缺失
    data = _hs_state().to_dict()
    data["axis_angle"] = "not-a-number"
    with pytest.raises(WarperStateError) as info:
        WarperStateV2.from_dict(data)
    assert info.value.code == "corrupt_payload"


def test_invalid_mode_and_grid_structure() -> None:
    data = _hs_state().to_dict()
    data["mode"] = "dream-mode"
    with pytest.raises(WarperStateError):
        WarperStateV2.from_dict(data)
    # CL 模式缺 grid2
    data = _cl_state().to_dict()
    del data["grids"]["grid2"]
    with pytest.raises(WarperStateError) as info:
        WarperStateV2.from_dict(data)
    assert info.value.code == "corrupt_payload"
    # HS 模式带 CL 网格
    data = _hs_state().to_dict()
    data["grids"]["grid1"] = WarperGrid.identity("grid1", 4, 4, ("dc", "dl")).to_dict()
    with pytest.raises(WarperStateError):
        WarperStateV2.from_dict(data)


def test_wrong_dimensions_and_components() -> None:
    data = _hs_state().to_dict()
    data["grids"]["hs"]["offsets"] = np.zeros((6, 8, 3), np.float32).tolist()  # 分量数错
    with pytest.raises(WarperStateError) as info:
        WarperStateV2.from_dict(data)
    assert info.value.code == "invalid_array"
    data["grids"]["hs"]["offsets"] = np.zeros((4, 4, 2), np.float32).tolist()  # 尺寸不匹配
    with pytest.raises(WarperStateError):
        WarperStateV2.from_dict(data)


def test_nan_inf_and_oversize_rejected() -> None:
    data = _hs_state().to_dict()
    offsets = np.zeros((6, 8, 2), np.float32)
    offsets[0, 0, 0] = np.inf
    data["grids"]["hs"]["offsets"] = offsets.tolist()
    with pytest.raises(WarperStateError) as info:
        WarperStateV2.from_dict(data)
    assert info.value.code == "not_finite"

    oversized = _hs_state().to_dict()
    oversized["grids"]["hs"]["rows"] = 200
    oversized["grids"]["hs"]["cols"] = 8
    oversized["grids"]["hs"]["offsets"] = np.zeros((200, 8, 2), np.float32).tolist()
    with pytest.raises(WarperStateError) as info:
        WarperStateV2.from_dict(oversized)
    assert info.value.code == "size_exceeded"


def test_semantics_mismatch_rejected() -> None:
    data = _hs_state().to_dict()
    data["grids"]["hs"]["semantics"] = ["dc", "dl"]  # sat 不得解释成 luma
    with pytest.raises(WarperStateError) as info:
        WarperStateV2.from_dict(data)
    assert info.value.code == "invalid_semantics"


def test_duplicate_ids_rejected() -> None:
    stroke = {"stroke_id": "dup", "mode": "normal", "source_rgb": [0, 0, 0], "target_rgb": [1, 1, 1]}
    # 重复 ID 在构造期即被拒绝(带重复载荷的状态根本无法存在)
    with pytest.raises(WarperStateError) as info:
        WarperStateV2(
            schema_version=WARPER_SCHEMA_VERSION, algorithm_version=CURRENT_ALGORITHM,
            mode="chroma_warp", color_space="hsv", axis_angle=0.0, grids={},
            strokes=(WarperStroke.from_dict(stroke), WarperStroke.from_dict(dict(stroke))))
    assert info.value.code == "invalid_id"


# ---- 冻结与深拷贝 ----

def test_offsets_frozen_and_copy_independent() -> None:
    state = _hs_state(rows=6, cols=8)
    with pytest.raises(ValueError):
        state.grids["hs"].offsets[0, 0, 0] = 1.0  # type: ignore[index]
    duplicate = state.copy()
    mutable = duplicate.grids["hs"].mutable_offsets()
    mutable[2, 3, 1] = 0.5
    assert state.grids["hs"].offsets[2, 3, 1] == 0.0
    assert duplicate.grids["hs"].offsets[2, 3, 1] == 0.0


def test_with_offsets_derives_frozen_grid() -> None:
    grid = WarperGrid.identity("hs", 6, 6, ("dh", "ds"))
    replacement = grid.mutable_offsets()
    replacement[1, 1, 0] = 0.25
    derived = grid.with_offsets(replacement)
    assert derived.offsets[1, 1, 0] == pytest.approx(0.25)
    assert not derived.offsets.flags.writeable
    replacement[1, 1, 0] = -1.0  # 源数组后续修改不影响派生
    assert derived.offsets[1, 1, 0] == pytest.approx(0.25)


# ---- 重采样 ----

def test_resample_identity_grid_stays_identity() -> None:
    grid = WarperGrid.identity("hs", 6, 8, ("dh", "ds"))
    up = resample_grid(grid, 12, 24)
    assert up.rows == 12 and up.cols == 24
    assert not up.offsets.any()
    down = resample_grid(up, 6, 8)
    assert not down.offsets.any()


def test_resample_bilinear_values_and_determinism() -> None:
    grid = WarperGrid.identity("hs", 4, 4, ("dh", "ds"))
    offsets = grid.mutable_offsets()
    offsets[:, :, 1] = np.linspace(0, 1, 4)[None, :]  # sat 位移沿列线性
    grid = grid.with_offsets(offsets)
    up = resample_grid(grid, 4, 8)
    # 列 0/2/4/6 应与原 0..3 对齐(端点对齐映射)
    expected = np.linspace(0, 1, 8)
    np.testing.assert_allclose(up.offsets[0, :, 1], expected, atol=1e-6)
    again = resample_grid(grid, 4, 8)
    np.testing.assert_array_equal(up.offsets, again.offsets)


def test_resample_hue_wraparound() -> None:
    grid = WarperGrid.identity("hs", 2, 2, ("dh", "ds"))
    offsets = grid.mutable_offsets()
    offsets[0, 0, 0] = 0.49   # 接近 +0.5
    offsets[0, 1, 0] = -0.49  # 接近 -0.5 —— 圆周上是同一点,不得过 0 折叠
    grid = grid.with_offsets(offsets)
    up = resample_grid(grid, 2, 4)
    middle = up.offsets[0, 1, 0]
    assert abs(abs(middle) - 0.49) < 0.02  # 圆周均值仍在 ±0.49 附近


def test_resample_remaps_pins_deterministically() -> None:
    grid = WarperGrid.identity("hs", 6, 6, ("dh", "ds"))
    pinned = WarperGrid(
        name="hs", rows=6, cols=6, components=2, semantics=("dh", "ds"),
        offsets=grid.offsets, pins=(WarperPin(point_id=make_point_id("hs", 4, 5)),))
    up = resample_grid(pinned, 12, 12)
    # 端点对齐线性映射:4 -> 4*11/5 = 8.8 -> 最近 9;5 -> 11
    assert up.pins[0].point_id == make_point_id("hs", 9, 11)
    down = resample_grid(up, 6, 6)
    assert down.pins[0].point_id == make_point_id("hs", 4, 5)


# ---- 能力契约 ----

def test_capabilities_reflect_backend_reality() -> None:
    empty = describe_warper_capabilities(None, legacy=None, revision=3)
    assert empty.as_dict() == {"enabled": True, "reason": "bypass",
                               "supported_modes": ["hue_saturation"],
                               "backend": "none", "revision": 3}
    legacy = describe_warper_capabilities(legacy_from_ndarray(np.zeros((8, 8, 2), np.float32)))
    assert legacy.enabled and legacy.backend == "legacy_hsv_glsl"
    # CW-B08:HSP/HSL 两分量网格经 v2 管线真实可渲
    new_space = describe_warper_capabilities(_hs_state(space="hsp"))
    assert new_space.enabled and new_space.backend == "warper_v2_glsl"
    hsl_space = describe_warper_capabilities(_hs_state(space="hsl"))
    assert hsl_space.enabled and hsl_space.backend == "warper_v2_glsl"
    # 三分量(Luma)/CL/CW 仍无 GPU 后端:如实拒绝
    luma_grid = WarperGrid.identity("hs", 6, 6, ("dh", "ds", "dl"))
    luma_state = WarperStateV2(
        schema_version=WARPER_SCHEMA_VERSION, algorithm_version=CURRENT_ALGORITHM,
        mode="hue_saturation", color_space="hsv", axis_angle=0.0,
        grids={"hs": luma_grid})
    luma_caps = describe_warper_capabilities(luma_state)
    assert not luma_caps.enabled and luma_caps.reason == "backend_unimplemented"
    cl = describe_warper_capabilities(_cl_state())
    assert not cl.enabled and cl.reason == "backend_unimplemented"


def test_v2_hsv_square_is_legacy_expressible() -> None:
    state = legacy_from_ndarray(np.ones((6, 6, 2), np.float32) * 0.01)
    caps = describe_warper_capabilities(state)
    assert caps.enabled and caps.reason in ("ok", "legacy_expressible")


# ---- 请求 envelope ----

def test_command_request_diagnostics() -> None:
    from src.features.color.core.warper_state import WarperCommandRequest

    good = WarperCommandRequest(
        request_id="r1", phase="begin", operation="set_grid_offsets",
        payload={"grid": "hs"}, gesture_id="g1")
    assert good.diagnostics() == []
    bad = WarperCommandRequest(request_id="", phase="flush", operation="", payload=None)
    issues = bad.diagnostics()
    assert any("request_id" in issue for issue in issues)
    assert any("phase" in issue for issue in issues)
    assert any("payload" in issue for issue in issues)
