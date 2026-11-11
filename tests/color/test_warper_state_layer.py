# -*- coding: utf-8 -*-
"""CW-B01:Layer 持久化/复制/粘贴对新版 warper_state 的集成测试。

覆盖:序列化 round-trip、深拷贝独立性、剪贴板域整体粘贴、reset 清空、
未知 schema 原文保留且不崩、legacy warper_params=None 防御、
读失败不影响本层其余字段(旧 fixture 载入渲染基线不变由既有渲染测试覆盖)。
"""
from __future__ import annotations

import numpy as np
import pytest

from src.features.color.core.layer import Layer
from src.features.color.core.warper_state import (
    WARPER_SCHEMA_VERSION,
    WarperStateError,
    WarperStateV2,
    legacy_from_ndarray,
)


def _layer_with_v2_state() -> tuple[Layer, WarperStateV2]:
    layer = Layer("v2")
    state = legacy_from_ndarray(np.full((6, 6, 2), 0.02, np.float32))
    assert state is not None
    layer.warper_state = state
    return layer, state


def test_layer_round_trip_keeps_v2_state_and_legacy() -> None:
    layer, state = _layer_with_v2_state()
    layer.warper_params = np.full((6, 6, 2), 0.01, np.float32)
    data = layer.to_dict(None)
    assert "warper_state" in data and "warper_params" in data
    restored = Layer.from_dict(data)
    assert restored.warper_state is not None
    np.testing.assert_array_equal(
        restored.warper_state.grids["hs"].offsets, state.grids["hs"].offsets)
    assert restored.warper_params is not None
    np.testing.assert_array_equal(restored.warper_params, layer.warper_params)


def test_layer_copy_params_independent() -> None:
    source, _ = _layer_with_v2_state()
    target = Layer("target")
    target.copy_params_from(source)
    assert target.warper_state is not None
    offsets = target.warper_state.grids["hs"].mutable_offsets()
    offsets[0, 0, 0] = 0.9
    assert source.warper_state.grids["hs"].offsets[0, 0, 0] == pytest.approx(0.02)


def test_grade_patch_applies_and_rejects_v2_state() -> None:
    layer, state = _layer_with_v2_state()
    payload = state.to_dict()
    payload["grids"]["hs"]["offsets"][1][1][0] = 0.125
    layer.apply_grade_patch({"warper_state": payload}, categories=("warper",))
    assert layer.warper_state is not None
    assert layer.warper_state.grids["hs"].offsets[1, 1, 0] == pytest.approx(0.125)

    corrupt = dict(payload)
    corrupt["schema_version"] = WARPER_SCHEMA_VERSION + 5
    before = layer.warper_state.to_dict()
    with pytest.raises(WarperStateError):
        layer.apply_grade_patch({"warper_state": corrupt}, categories=("warper",))
    # 失败不得半途覆盖现值
    assert layer.warper_state.to_dict() == before


def test_grade_patch_none_clears_v2_state() -> None:
    layer, _ = _layer_with_v2_state()
    layer.apply_grade_patch({"warper_state": None}, categories=("warper",))
    assert layer.warper_state is None and layer.warper_state_unparsed is None


def test_reset_clears_v2_state() -> None:
    layer, _ = _layer_with_v2_state()
    layer.warper_state_unparsed = {"schema_version": 99}
    layer.reset_params()
    assert layer.warper_state is None
    assert layer.warper_state_unparsed is None
    assert layer.warper_params is None


def test_unknown_schema_version_preserved_verbatim() -> None:
    layer, _ = _layer_with_v2_state()
    future = {"schema_version": WARPER_SCHEMA_VERSION + 1, "mode": "hue_saturation",
              "future_blob": {"anything": [1, 2]}}
    data = layer.to_dict(None)
    data["warper_state"] = future
    restored = Layer.from_dict(data)
    assert restored.warper_state is None  # 不可解析:不降级
    assert restored.warper_state_unparsed == future  # 原文保留
    # 保存不丢:再次 to_dict 原样回写
    assert Layer.from_dict(restored.to_dict(None)).warper_state_unparsed == future
    # has_grade 如实报告(不静默丢效果)
    assert restored.has_grading_effects()


def test_corrupt_warper_state_does_not_break_layer_load() -> None:
    layer = Layer("plain")
    layer.grading_params["exposure"] = 0.5
    data = layer.to_dict(None)
    data["warper_state"] = "完全不是dict"
    restored = Layer.from_dict(data)
    assert restored.grading_params["exposure"] == 0.5
    assert restored.warper_state is None
    assert restored.warper_state_unparsed == "完全不是dict"


def test_legacy_warper_params_none_no_longer_crashes() -> None:
    layer = Layer("legacy")
    data = layer.to_dict(None)
    data["warper_params"] = None
    restored = Layer.from_dict(data)  # 旧版 np.array(None) 直接崩
    assert restored.warper_params is None
    # 病态数组同样按无 warper 处理,不阻断整层
    data["warper_params"] = [[["x"]]]
    restored = Layer.from_dict(data)
    assert restored.warper_params is None


def test_paste_categories_carry_v2_state(tmp_path) -> None:
    from src.features.color.core.grade_clipboard import GradeClipboard

    source, state = _layer_with_v2_state()
    clipboard = GradeClipboard()
    clipboard.copy_grade([source], categories={"warper"})
    target = Layer("target")
    clipboard.paste_onto_layers([target], {"warper"})
    assert target.warper_state is not None
    np.testing.assert_array_equal(
        target.warper_state.grids["hs"].offsets, state.grids["hs"].offsets)
