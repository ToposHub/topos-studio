# -*- coding: utf-8 -*-
"""CW-B02:Warper 命令适配器测试(面板命令 → 服务手势/去重/收口)。"""
from __future__ import annotations

import numpy as np
import pytest

from src.features.color.adapters.warper_command_adapter import WarperCommandAdapter
from src.features.color.core.layer import Layer
from src.features.color.core.warper_commands import WarperCommandService
from src.features.color.core.warper_state import legacy_from_ndarray


class Host:
    """服务回调记录 + 可切换的活动层解析。"""

    def __init__(self, layer: Layer) -> None:
        self.pushes: list[str] = []
        self.discards = 0
        self.applied: list[object] = []
        self.finished: list[bool] = []
        self.layer = layer
        self.service = WarperCommandService(
            history_push=lambda l: self.pushes.append(l.id),
            history_discard=lambda: setattr(self, "discards", self.discards + 1),
            on_applied=lambda l, s: self.applied.append(s),
            gesture_finished=lambda l, c: self.finished.append(c),
        )
        self.adapter = WarperCommandAdapter(self.service, lambda: self.layer)

    def submit(self, action: str, payload: dict | None = None, *,
               mode: str = "hue_saturation", state: dict | None = None) -> dict:
        return self.adapter.submit({
            "mode": mode, "action": action, "payload": payload or {},
            "state": state if state is not None else self._state(mode)})

    @staticmethod
    def _state(mode: str) -> dict:
        if mode == "chroma_warp":
            return {"mode": "chroma_warp", "selected": None, "strokes": [], "pins": []}
        return {"mode": mode, "resolution": [6, 6], "active_grid": 0,
                "color_space": "HSV",
                "grids": {"0": {"selected": [], "pins": []}}}


def _host_with_legacy() -> Host:
    layer = Layer("L")
    layer.warper_state = legacy_from_ndarray(np.zeros((6, 6, 2), np.float32))
    return Host(layer)


def _host_with_cl() -> Host:
    from src.features.color.core.warper_state import (
        WARPER_SCHEMA_VERSION, CURRENT_ALGORITHM, WarperGrid, WarperStateV2)

    layer = Layer("CL")
    layer.warper_state = WarperStateV2(
        schema_version=WARPER_SCHEMA_VERSION, algorithm_version=CURRENT_ALGORITHM,
        mode="chroma_luma", color_space="hsv", axis_angle=0.0,
        grids={"grid1": WarperGrid.identity("grid1", 6, 6, ("dc", "dl")),
               "grid2": WarperGrid.identity("grid2", 6, 6, ("dc", "dl"))})
    return Host(layer)


# ---- 分类 ----

def test_ui_only_actions_never_touch_service() -> None:
    host = _host_with_legacy()
    for action in ("tool", "active_grid", "select", "invert", "falloff_add",
                   "auto_lock", "range", "reset_selection"):
        result = host.submit(action, {"tool": "pin"})
        assert result["reason"] == "ui_only" and not result["handled"]
    assert host.pushes == [] and host.service.revision(host.layer) == 0


def test_component_value_deduped_when_legacy_published() -> None:
    host = _host_with_legacy()
    result = host.submit("component", {"component": "hue", "value": 0.2})
    # HS 方阵 + HSV:同一编辑已走 legacy warper_changed,命令层不再提交
    assert result["reason"] == "legacy_dedup" and not result["handled"]
    assert host.pushes == []


def test_component_in_new_space_uses_b04_solver() -> None:
    # CW-B04 起,legacy 无法发布的新空间由网格求解操作真实处理
    host = _host_with_legacy()
    state = _hsp_state([[2, 2]])
    result = host.submit("component", {"component": "hue", "value": 0.2}, state=state)
    assert result["ok"] and result["handled"] and result["dirty"]


def test_cw_actions_need_stable_ids() -> None:
    host = Host(Layer("cw"))
    for action, payload in (("delete", {}), ("stroke_parameter", {"key": "exposure", "value": 1.0}),
                            ("reset_pin", {}), ("stroke", {})):
        result = host.submit(action, payload, mode="chroma_warp")
        assert not result["ok"] and result["reason"] == "op_unimplemented", action


# ---- CW-B04:component 手势 → 求解操作 ----

def _hsp_state(selected: list) -> dict:
    state = Host._state("hue_saturation")
    state["color_space"] = "HSP"
    state["falloff"] = 0.5
    state["auto_lock"] = True
    state["lock_points"] = 2
    state["grids"]["0"]["selected"] = selected
    return state


def test_component_with_selection_maps_to_solver() -> None:
    host = _host_with_legacy()
    result = host.submit("component", {"component": "hue", "value": 0.2},
                         state=_hsp_state([[2, 2]]))
    assert result["ok"] and result["handled"]
    grid = host.layer.warper_state.grids["hs"]
    assert grid.offsets[2, 2, 0] == pytest.approx(0.2)   # absolute 模式:选点=目标值
    # falloff 0.5:切比雪夫 1 邻域向目标值插值(0.1);AutoLock 2 锁曼哈顿 ≤2
    assert grid.offsets[2, 1, 0] == pytest.approx(0.0)
    # (4,4):切比雪夫 2 → 权重 0.25;曼哈顿 4 > AutoLock 2 → 未锁
    assert grid.offsets[4, 4, 0] == pytest.approx(0.05)


def test_component_smooth_maps_to_smooth_op() -> None:
    host = _host_with_legacy()
    state = _hsp_state([[1, 0], [1, 7]])
    result = host.submit("component", {"component": "sat", "value": 0.0, "smooth": True},
                         state=state)
    assert result["ok"] and result["handled"]


def test_component_without_selection_is_noop() -> None:
    host = _host_with_legacy()
    result = host.submit("component", {"component": "hue", "value": 0.2},
                         state=_hsp_state([]))
    assert result["reason"] == "ui_only_no_selection" and not result["handled"]
    assert host.pushes == []


def test_component_luma_upgrades_to_third_component() -> None:
    host = _host_with_legacy()
    result = host.submit("component", {"component": "luma", "value": 0.1},
                         state=_hsp_state([[3, 3]]))
    assert result["ok"]
    grid = host.layer.warper_state.grids["hs"]
    assert grid.semantics == ("dh", "ds", "dl")
    assert grid.offsets[3, 3, 2] == pytest.approx(0.1)
    assert host.layer.warper_params is None  # 三分量不再可 legacy 表达


# ---- 离散命令 = 原子手势 ----

def test_resolution_command_is_atomic_single_undo() -> None:
    host = _host_with_legacy()
    result = host.submit("resolution", {"columns": 12, "rows": 12})
    assert result["ok"] and result["handled"]
    assert host.pushes == [host.layer.id]           # 恰好一条基线
    assert host.service.revision(host.layer) == 1   # 已 commit
    assert host.finished == [True]
    grid = host.layer.warper_state.grids["hs"]
    assert (grid.rows, grid.cols) == (12, 12)
    assert host.service.session_for(host.layer) is None


def test_color_space_switch_maps_to_activate_mode() -> None:
    host = _host_with_legacy()
    result = host.submit("color_space", {"value": "HSP"})
    assert result["ok"]
    assert host.layer.warper_state.color_space == "hsp"
    assert host.layer.warper_state.mode == "hue_saturation"  # 保留当前模式
    assert host.layer.warper_params is None  # legacy 无法表达 → 投影清空


def test_reset_and_reset_all_clear_state_atomically() -> None:
    host = _host_with_legacy()
    host.submit("resolution", {"columns": 8, "rows": 8})
    assert host.layer.warper_state is not None
    cleared = host.submit("reset_all")
    assert cleared["ok"]
    assert host.layer.warper_state is None and host.layer.warper_params is None


def test_convert_pin_derives_stable_point_ids() -> None:
    host = _host_with_legacy()
    state = Host._state("hue_saturation")
    state["grids"]["0"]["selected"] = [[1, 2], [3, 4]]
    result = host.submit("convert_pin", state=state)
    assert result["ok"]
    pins = [p.point_id for p in host.layer.warper_state.grids["hs"].pins]
    assert pins == ["hs:1:2", "hs:3:4"]


def test_convert_pin_without_selection_targeting_unsupported() -> None:
    host = _host_with_legacy()
    state = Host._state("hue_saturation")
    state["grids"]["0"]["selected"] = ["junk"]
    result = host.submit("convert_pin", state=state)
    assert not result["ok"] and result["reason"] == "unsupported_targeting"


def test_no_active_layer_reports_no_target() -> None:
    host = _host_with_legacy()
    host.layer = None
    result = host.submit("resolution", {"columns": 12, "rows": 12})
    assert not result["ok"] and result["reason"] == "no_target"


# ---- 连续命令手势合并 ----

def test_continuous_axis_angle_one_gesture_one_undo() -> None:
    host = _host_with_cl()
    first = host.submit("axis_angle", {"value": 10.0}, mode="chroma_luma")
    assert first["ok"]
    for value in (20.0, 35.0, 44.0):
        again = host.submit("axis_angle", {"value": value}, mode="chroma_luma")
        assert again["ok"]
    assert host.pushes == [host.layer.id]  # 仍是一个手势一条基线
    host.adapter.flush()
    assert host.service.revision(host.layer) == 1
    assert host.finished.count(True) == 1
    assert host.layer.warper_state.axis_angle == pytest.approx(44.0)


def test_different_signature_flushes_previous_gesture() -> None:
    host = _host_with_cl()
    host.submit("axis_angle", {"value": 10.0}, mode="chroma_luma")
    # 换一个离散命令 → 旧手势先收口
    host.submit("resolution", {"columns": 8, "rows": 8}, mode="chroma_luma")
    assert host.service.revision(host.layer) == 2
    assert host.finished.count(True) == 2
    assert host.service.session_for(host.layer) is None


def test_cancel_hook_restores_and_closes() -> None:
    host = _host_with_cl()
    host.submit("axis_angle", {"value": 30.0}, mode="chroma_luma")
    host.adapter.cancel()  # 宿主 undo/切层钩子
    assert host.service.session_for(host.layer) is None
    assert host.discards == 1
    assert host.layer.warper_state.axis_angle == pytest.approx(0.0)  # 基线恢复
    # 取消后 flush 无副作用
    host.adapter.flush()
    assert host.service.revision(host.layer) == 0
