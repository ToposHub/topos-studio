# -*- coding: utf-8 -*-
"""CW-B02:Warper 命令服务手势生命周期测试。

验证计划 §4 CW-B02:一次拖动一个 undo;无移动点击零 undo;Escape/cancel
干净恢复;切层/取消不覆盖新目标;stale revision 拒绝且不推进;算法操作
如实 unimplemented;legacy 桥投影。
"""
from __future__ import annotations

import numpy as np
import pytest

from src.features.color.core.layer import Layer
from src.features.color.core.warper_commands import WarperCommandService
from src.features.color.core.warper_state import (
    WarperCommandRequest,
    WarperStateV2,
    legacy_from_ndarray,
)


class Harness:
    """记录回调的服务测试台。"""

    def __init__(self) -> None:
        self.pushes: list[str] = []          # layer ids
        self.discards = 0
        self.applied: list[tuple[str, object | None]] = []
        self.finished: list[tuple[str, bool]] = []
        self.service = WarperCommandService(
            history_push=lambda layer: self.pushes.append(layer.id),
            history_discard=lambda: self.__setattr__("discards", self.discards + 1),
            on_applied=lambda layer, state: self.applied.append((layer.id, state)),
            gesture_finished=lambda layer, committed: self.finished.append((layer.id, committed)),
        )

    def request(self, phase: str, operation: str, payload: dict, *,
                gesture: str = "g1", layer: Layer, base_revision: int | None = None,
                layer_id: str | None = None) -> WarperCommandRequest:
        return WarperCommandRequest(
            request_id=f"r-{phase}-{operation}", phase=phase, operation=operation,
            payload=payload, gesture_id=gesture, base_revision=base_revision,
            layer_id=layer_id if layer_id is not None else layer.id)


def _layer_with_legacy_state() -> Layer:
    layer = Layer("warper")
    layer.warper_state = legacy_from_ndarray(np.zeros((6, 6, 2), np.float32))
    return layer


def _offsets_payload(value: float = 0.1) -> dict:
    return {"grid": "hs", "offsets": np.full((6, 6, 2), value, np.float32)}


# ---- 手势生命周期 ----

def test_one_drag_one_undo() -> None:
    h = Harness()
    layer = _layer_with_legacy_state()
    svc = h.service
    assert svc.handle(h.request("begin", "replace_state", {}, layer=layer), layer).ok
    assert h.pushes == []  # begin 不压栈
    for value in (0.01, 0.05, 0.12):
        result = svc.handle(
            h.request("update", "set_grid_offsets", _offsets_payload(value), layer=layer), layer)
        assert result.ok and result.dirty
    assert h.pushes == [layer.id]  # 首个实质变更恰好压一次基线
    result = svc.handle(h.request("commit", "replace_state", {}, layer=layer), layer)
    assert result.ok and result.dirty
    assert svc.revision(layer) == 1
    assert h.finished == [(layer.id, True)]
    assert layer.warper_state.grids["hs"].offsets[0, 0, 0] == pytest.approx(0.12)


def test_no_move_click_zero_undo() -> None:
    h = Harness()
    layer = _layer_with_legacy_state()
    svc = h.service
    svc.handle(h.request("begin", "replace_state", {}, layer=layer), layer)
    # 空载荷 update(无变化)+ commit = 零历史、零 revision
    result = svc.handle(h.request("update", "set_state_params", {}, layer=layer), layer)
    assert result.ok and not result.dirty
    result = svc.handle(h.request("commit", "replace_state", {}, layer=layer), layer)
    assert result.ok and not result.dirty
    assert h.pushes == [] and svc.revision(layer) == 0
    assert h.finished == [(layer.id, False)]


def test_update_with_identical_values_does_not_push() -> None:
    h = Harness()
    layer = _layer_with_legacy_state()
    svc = h.service
    svc.handle(h.request("begin", "replace_state", {}, layer=layer), layer)
    zeros = svc.handle(
        h.request("update", "set_grid_offsets", _offsets_payload(0.0), layer=layer), layer)
    assert zeros.ok and not zeros.dirty  # 与现值相同:非实质变更
    assert h.pushes == []


def test_cancel_restores_base_and_discards_history() -> None:
    h = Harness()
    layer = _layer_with_legacy_state()
    base = layer.warper_state.copy()
    svc = h.service
    svc.handle(h.request("begin", "replace_state", {}, layer=layer), layer)
    svc.handle(h.request("update", "set_grid_offsets", _offsets_payload(0.3), layer=layer), layer)
    assert layer.warper_state.grids["hs"].offsets[0, 0, 0] == pytest.approx(0.3)
    result = svc.handle(h.request("cancel", "replace_state", {}, layer=layer), layer)
    assert result.ok and result.reason == "cancelled"
    np.testing.assert_array_equal(layer.warper_state.grids["hs"].offsets, base.grids["hs"].offsets)
    assert h.discards == 1  # 取消的手势基线被丢弃
    assert svc.revision(layer) == 0
    assert (layer.id, False) in h.finished


def test_layer_switch_cancels_active_session() -> None:
    h = Harness()
    layer = _layer_with_legacy_state()
    svc = h.service
    svc.handle(h.request("begin", "replace_state", {}, layer=layer), layer)
    svc.handle(h.request("update", "set_grid_offsets", _offsets_payload(0.2), layer=layer), layer)
    # 宿主在切层/undo/reset 时调用:
    assert svc.cancel_active(layer) is not None
    assert svc.session_for(layer) is None
    assert layer.warper_state.grids["hs"].offsets[0, 0, 0] == pytest.approx(0.0)
    # 过期 update 不得覆盖新目标
    stale = svc.handle(h.request("update", "set_grid_offsets", _offsets_payload(0.9), layer=layer), layer)
    assert not stale.ok and stale.reason == "no_session"


def test_wrong_target_rejected() -> None:
    h = Harness()
    layer = _layer_with_legacy_state()
    result = h.service.handle(
        h.request("begin", "replace_state", {}, layer=layer, layer_id="另一个层"), layer)
    assert not result.ok and result.reason == "wrong_target"


def test_stale_revision_rejected_and_not_advanced() -> None:
    h = Harness()
    layer = _layer_with_legacy_state()
    svc = h.service
    svc.handle(h.request("begin", "replace_state", {}, layer=layer), layer)
    stale = svc.handle(
        h.request("update", "set_grid_offsets", _offsets_payload(0.1),
                  layer=layer, base_revision=99), layer)
    assert not stale.ok and stale.reason == "stale_revision"
    assert svc.revision(layer) == 0  # 失败不推进
    stale_commit = svc.handle(
        h.request("commit", "replace_state", {}, layer=layer, base_revision=99), layer)
    assert not stale_commit.ok and stale_commit.reason == "stale_revision"
    # stale commit 同时恢复基线(手势中途外部提交/撤销)
    assert layer.warper_state.grids["hs"].offsets[0, 0, 0] == pytest.approx(0.0)


def test_new_begin_supersedes_open_gesture() -> None:
    h = Harness()
    layer = _layer_with_legacy_state()
    svc = h.service
    svc.handle(h.request("begin", "replace_state", {}, gesture="g1", layer=layer), layer)
    svc.handle(h.request("update", "set_grid_offsets", _offsets_payload(0.4), gesture="g1", layer=layer), layer)
    svc.handle(h.request("begin", "replace_state", {}, gesture="g2", layer=layer), layer)  # 顶替
    assert h.discards == 1  # 旧手势干净取消
    assert layer.warper_state.grids["hs"].offsets[0, 0, 0] == pytest.approx(0.0)
    ok = svc.handle(h.request("update", "set_grid_offsets", _offsets_payload(0.05), gesture="g2", layer=layer), layer)
    assert ok.ok


def test_cancel_all_for_mode_switch_or_close() -> None:
    h = Harness()
    layer = _layer_with_legacy_state()
    svc = h.service
    svc.handle(h.request("begin", "replace_state", {}, layer=layer), layer)
    svc.handle(h.request("update", "set_grid_offsets", _offsets_payload(0.15), layer=layer), layer)
    assert svc.cancel_all("mode_switch") == 1
    assert svc.session_for(layer) is None
    assert layer.warper_state.grids["hs"].offsets[0, 0, 0] == pytest.approx(0.0)


# ---- 操作与校验 ----

def test_unimplemented_algorithm_ops_refused() -> None:
    h = Harness()
    layer = _layer_with_legacy_state()
    svc = h.service
    svc.handle(h.request("begin", "replace_state", {}, layer=layer), layer)
    # CW-B04 已实现 smooth_component;仍未实现的求解族如实拒绝
    result = svc.handle(h.request("update", "solve_strokes", {}, layer=layer), layer)
    assert not result.ok and result.reason == "op_unimplemented"
    unknown = svc.handle(h.request("update", "magic", {}, layer=layer), layer)
    assert not unknown.ok and unknown.reason == "unknown_operation"


def test_invalid_payload_rejected_without_side_effects() -> None:
    h = Harness()
    layer = _layer_with_legacy_state()
    svc = h.service
    svc.handle(h.request("begin", "replace_state", {}, layer=layer), layer)
    bad = {"grid": "hs", "offsets": np.full((4, 4, 2), np.nan, np.float32)}
    result = svc.handle(h.request("update", "set_grid_offsets", bad, layer=layer), layer)
    assert not result.ok and result.reason.startswith("rejected:")
    assert layer.warper_state is not None  # 现值未被污染
    assert layer.warper_state.grids["hs"].offsets.shape == (6, 6, 2)


def test_operations_without_state_ask_to_activate() -> None:
    h = Harness()
    layer = Layer("empty")
    svc = h.service
    svc.handle(h.request("begin", "replace_state", {}, layer=layer), layer)
    result = svc.handle(h.request("update", "set_grid_offsets", _offsets_payload(), layer=layer), layer)
    assert not result.ok and "no_state" in result.reason


def test_activate_mode_creates_default_grids() -> None:
    h = Harness()
    layer = Layer("fresh")
    svc = h.service
    svc.handle(h.request("begin", "activate_mode", {"mode": "chroma_luma"}, layer=layer), layer)
    result = svc.handle(h.request("update", "activate_mode",
                                  {"mode": "chroma_luma", "color_space": "hsv"}, layer=layer), layer)
    assert result.ok and result.dirty
    assert set(layer.warper_state.grids) == {"grid1", "grid2"}
    svc.handle(h.request("commit", "activate_mode", {}, layer=layer), layer)
    assert svc.revision(layer) == 1
    # 重复激活同模式同空间 = 无变化
    svc.handle(h.request("begin", "activate_mode", {}, gesture="g2", layer=layer), layer)
    again = svc.handle(h.request("update", "activate_mode",
                                 {"mode": "chroma_luma", "color_space": "hsv"}, gesture="g2", layer=layer), layer)
    assert again.ok and not again.dirty


def test_resolution_change_resamples_not_clears() -> None:
    h = Harness()
    layer = _layer_with_legacy_state()
    svc = h.service
    svc.handle(h.request("begin", "replace_state", {}, layer=layer), layer)
    payload = {"grid": "hs", "offsets": np.zeros((6, 6, 2), np.float32)}
    payload["offsets"][2, 3, 0] = 0.2
    svc.handle(h.request("update", "set_grid_offsets", payload, layer=layer), layer)
    result = svc.handle(h.request("update", "set_grid_resolution",
                                  {"grid": "hs", "rows": 12, "cols": 12}, layer=layer), layer)
    assert result.ok
    grid = layer.warper_state.grids["hs"]
    assert (grid.rows, grid.cols) == (12, 12)
    assert grid.offsets.any()  # 重采样而非清空


def test_set_grid_pins_and_reset_pins() -> None:
    h = Harness()
    layer = _layer_with_legacy_state()
    svc = h.service
    svc.handle(h.request("begin", "replace_state", {}, layer=layer), layer)
    result = svc.handle(h.request("update", "set_grid_pins",
                                  {"grid": "hs", "point_ids": ["hs:1:1", "hs:2:2"]}, layer=layer), layer)
    assert result.ok and result.dirty
    assert [p.point_id for p in layer.warper_state.grids["hs"].pins] == ["hs:1:1", "hs:2:2"]
    svc.handle(h.request("update", "set_grid_pins", {"grid": "hs", "point_ids": ()}, layer=layer), layer)
    assert layer.warper_state.grids["hs"].pins == ()


def test_stroke_crud_and_params() -> None:
    h = Harness()
    layer = Layer("cw")
    svc = h.service
    svc.handle(h.request("begin", "activate_mode",
                         {"mode": "chroma_warp", "color_space": "hsv"}, layer=layer), layer)
    svc.handle(h.request("update", "activate_mode",
                         {"mode": "chroma_warp", "color_space": "hsv"}, layer=layer), layer)
    stroke = {"stroke_id": "s1", "mode": "normal", "source_rgb": [0.1, 0.2, 0.3],
              "target_rgb": [0.2, 0.3, 0.4], "path": [[0.1, 0.1], [0.5, 0.5]]}
    assert svc.handle(h.request("update", "add_stroke", {"stroke": stroke}, layer=layer), layer).ok
    params = svc.handle(h.request("update", "set_stroke_params",
                                  {"stroke_id": "s1", "params": {"exposure": 1.5}}, layer=layer), layer)
    assert params.ok
    assert layer.warper_state.strokes[0].params["exposure"] == pytest.approx(1.5)
    deleted = svc.handle(h.request("update", "delete_objects", {"stroke_ids": ["s1"]}, layer=layer), layer)
    assert deleted.ok and layer.warper_state.strokes == ()
    # 越界参数被拒
    svc.handle(h.request("update", "add_stroke", {"stroke": stroke}, layer=layer), layer)
    bad = svc.handle(h.request("update", "set_stroke_params",
                               {"stroke_id": "s1", "params": {"exposure": 99.0}}, layer=layer), layer)
    assert not bad.ok


def test_strokes_rejected_outside_chroma_warp() -> None:
    h = Harness()
    layer = _layer_with_legacy_state()
    svc = h.service
    svc.handle(h.request("begin", "replace_state", {}, layer=layer), layer)
    stroke = {"stroke_id": "s1", "mode": "normal", "source_rgb": [0, 0, 0], "target_rgb": [1, 1, 1]}
    result = svc.handle(h.request("update", "add_stroke", {"stroke": stroke}, layer=layer), layer)
    assert not result.ok


# ---- legacy 渲染桥 ----

def test_legacy_bridge_updates_warper_params() -> None:
    h = Harness()
    layer = _layer_with_legacy_state()
    svc = h.service
    svc.handle(h.request("begin", "replace_state", {}, layer=layer), layer)
    svc.handle(h.request("update", "set_grid_offsets", _offsets_payload(0.07), layer=layer), layer)
    # legacy 可表达 → 旧 HSV 渲染路径拿到投影
    assert layer.warper_params is not None
    assert layer.warper_params[0, 0, 0] == pytest.approx(0.07)
    assert h.applied and h.applied[-1][0] == layer.id


def test_non_expressible_state_clears_legacy_projection() -> None:
    h = Harness()
    layer = _layer_with_legacy_state()
    svc = h.service
    svc.handle(h.request("begin", "replace_state", {}, layer=layer), layer)
    # 切到 HSP:legacy 无法表达 → 投影清空(画面保持,不伪造旧效果)
    result = svc.handle(h.request("update", "activate_mode",
                                  {"mode": "hue_saturation", "color_space": "hsp"}, layer=layer), layer)
    assert result.ok
    assert layer.warper_params is None
    assert layer.warper_state.color_space == "hsp"
