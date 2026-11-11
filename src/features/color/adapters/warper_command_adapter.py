# -*- coding: utf-8 -*-
"""CW-B02:Warper UI 命令 → WarperCommandService 适配层(纯 Python,无 Qt)。

计划:`docs/plans/2026-11-12-color-warper-backend-plan.md` §4 CW-B02。

职责
----
1. 把 ``WarperPanel.command_requested`` 的 ``{mode, action, payload, state}``
   转成 ``WarperCommandRequest`` 信封(身份/revision 由宿主在控制器层注入,
   本层只补 operation/payload/gesture_id)。
2. **手势合并策略**:离散命令(resolution/color_space/reset/reset_all/
   convert_pin…)原子执行(begin+update+commit,一次 undo);连续数值命令
   (axis_angle 等)同一签名保持一个手势,由宿主的静默定时器或
   ``flush()`` 收口——一次拖动仍是一次 undo。
3. **与 legacy 通道去重**:HS 方阵+HSV 空间下,component 数值与网格拖动
   已由旧 ``warper_changed`` 路径发布并走旧历史;同一编辑不得再经命令层
   二次提交。legacy 不支持的空间(HSP/HSL)如实返回 unimplemented,
   等 B04 接入,不假装生效。
4. **选择不进历史**:纯 UI 动作(工具切换/选择/绘制/falloff 显示等)
   跳过服务,不产生模型变更或 undo。
5. 列表索引不是持久身份:CW 笔画/Pin 的删除与参数编辑需要稳定 ID,
   UI 快照目前只有 ``(kind, list_index)`` —— 在 B06 提供 ID 前如实
   unimplemented,不把易变索引写进模型。
"""
from __future__ import annotations

import itertools
from collections.abc import Callable, Mapping
from typing import Any

from src.features.color.core.layer import Layer
from src.features.color.core.warper_commands import WarperCommandService
from src.features.color.core.warper_state import (
    CL_GRID_NAMES,
    HS_GRID_NAME,
    WarperCommandRequest,
    make_point_id,
)

__all__ = ["WarperCommandAdapter"]

#: 纯 UI 动作:不触碰模型、不建历史(选择/查看/显示开关/等待 B04 的
#: 求解参数显示)。
UI_ONLY_ACTIONS = frozenset({
    "tool", "active_grid", "select", "draw", "invert", "column", "row", "ring",
    "all", "reset_selection", "falloff_add", "falloff_sub", "auto_lock", "range",
})

#: 有后端语义但尚未实现(B04/B06 接入后迁移到映射表)。
UNIMPLEMENTED_ACTIONS = frozenset({
    "component", "delete", "reset_pin", "stroke_parameter",
    "stroke", "point_stroke", "add_pin", "smooth",
})

#: 连续数值动作:同一签名合并为一个手势,flush() 收口。
CONTINUOUS_ACTIONS = frozenset({"axis_angle"})

_GRID_BY_INDEX = {0: CL_GRID_NAMES[0], 1: CL_GRID_NAMES[1]}


def _legacy_published(command: Mapping[str, Any]) -> bool:
    """该命令对应的编辑是否已由 legacy ``warper_changed`` 路径发布。

    HS 模式 + 方阵 + HSV 空间时,网格/数值编辑走旧通道(旧历史一次 undo),
    命令层再执行一次就是双重提交。判定字段来自随命令附带的画布快照。
    """
    if command.get("mode") != "hue_saturation":
        return False
    state = command.get("state") or {}
    if str(state.get("color_space", "HSV")).upper() != "HSV":
        return False
    resolution = state.get("resolution") or [None, None]
    return len(resolution) == 2 and resolution[0] == resolution[1]


class WarperCommandAdapter:
    """command_requested → WarperCommandService;宿主提供活动层解析与
    ``flush`` 定时驱动(控制器接 QTimer/生命周期钩子)。"""

    def __init__(
        self,
        service: WarperCommandService,
        layer_resolver: Callable[[], Layer | None],
        *,
        request_context: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._service = service
        self._resolve_layer = layer_resolver
        self._request_context = request_context or (lambda: {})
        self._gesture_ids = itertools.count(1)
        self._open_signature: tuple[str, str] | None = None
        self._open_gesture_id: str | None = None

    # ---- 对外 ----

    def submit(self, command: Mapping[str, Any]) -> dict[str, Any]:
        """处理一条面板命令。返回 {ok, reason, handled, revision, dirty}。"""
        action = command.get("action")
        if action in UI_ONLY_ACTIONS:
            return {"ok": True, "reason": "ui_only", "handled": False,
                    "revision": None, "dirty": False}
        if action in UNIMPLEMENTED_ACTIONS:
            if action == "component" and _legacy_published(command):
                # legacy 已发布同一次编辑:命令层去重,不再二次提交
                return {"ok": True, "reason": "legacy_dedup", "handled": False,
                        "revision": None, "dirty": False}
            return {"ok": False, "reason": "op_unimplemented", "handled": True,
                    "revision": None, "dirty": False}

        layer = self._resolve_layer()
        if layer is None:
            return {"ok": False, "reason": "no_target", "handled": True,
                    "revision": None, "dirty": False}

        operation, payload = self._translate(action, command, layer)
        if operation is None:
            return {"ok": False, "reason": "unsupported_targeting", "handled": True,
                    "revision": None, "dirty": False}

        signature = (operation, self._target_key(payload))
        if action in CONTINUOUS_ACTIONS and signature == self._open_signature:
            # 同一手势的后续 update
            request = self._request("update", operation, payload, layer,
                                    gesture_id=self._open_gesture_id)
            result = self._service.handle(request, layer)
        else:
            self.flush()  # 不同签名:先收口旧手势,保证一次手势一次 undo
            gesture_id = f"warper-{next(self._gesture_ids)}"
            self._service.handle(self._request("begin", operation, payload, layer,
                                               gesture_id=gesture_id), layer)
            result = self._service.handle(
                self._request("update", operation, payload, layer, gesture_id=gesture_id), layer)
            if action in CONTINUOUS_ACTIONS and result.ok:
                self._open_signature = signature
                self._open_gesture_id = gesture_id
                return self._result_dict(result, handled=True)
            self._service.handle(
                self._request("commit", operation, payload, layer, gesture_id=gesture_id), layer)
            self._open_signature = None
            self._open_gesture_id = None
        return self._result_dict(result, handled=True)

    def flush(self) -> None:
        """收口打开的连续手势(宿主静默定时器/生命周期钩子调用)。"""
        if self._open_gesture_id is None:
            return
        layer = self._resolve_layer()
        gesture_id, self._open_gesture_id, self._open_signature = \
            self._open_gesture_id, None, None
        if layer is not None:
            self._service.handle(
                self._request("commit", "replace_state", {}, layer, gesture_id=gesture_id), layer)

    def cancel(self) -> None:
        """取消打开的手势(切层/undo/reset/关面板钩子)。"""
        self._open_gesture_id = None
        self._open_signature = None
        self._service.cancel_all("host_lifecycle")

    # ---- 内部 ----

    def _request(self, phase: str, operation: str, payload: Any, layer: Layer,
                 *, gesture_id: str | None) -> WarperCommandRequest:
        context = self._request_context()
        return WarperCommandRequest(
            request_id=f"warper-cmd-{next(self._gesture_ids)}",
            phase=phase, operation=operation, payload=payload,
            gesture_id=gesture_id,
            base_revision=self._service.revision(layer),
            layer_id=layer.id,
            **context)

    def _result_dict(self, result, *, handled: bool) -> dict[str, Any]:
        return {"ok": result.ok, "reason": result.reason, "handled": handled,
                "revision": result.revision, "dirty": result.dirty}

    @staticmethod
    def _target_key(payload: Mapping[str, Any]) -> str:
        return str(payload.get("grid") or payload.get("scope") or "")

    def _translate(self, action: str, command: Mapping[str, Any],
                   layer: Layer) -> tuple[str | None, dict[str, Any]]:
        payload_in = command.get("payload") or {}
        state = command.get("state") or {}
        mode = command.get("mode")

        if action == "resolution":
            grid = self._grid_name(mode, state)
            if grid is None:
                return None, {}
            rows, cols = int(payload_in.get("rows", 0)), int(payload_in.get("columns", 0))
            return "set_grid_resolution", {"grid": grid, "rows": rows, "cols": cols}

        if action == "color_space":
            space = str(payload_in.get("value", "hsv")).lower()
            current = layer.warper_state
            current_mode = current.mode if current is not None else mode
            return "activate_mode", {"mode": current_mode, "color_space": space}

        if action == "axis_angle":
            return "set_state_params", {"axis_angle": float(payload_in.get("value", 0.0))}

        if action == "convert_pin":
            grid = self._grid_name(mode, state)
            if grid is None:
                return None, {}
            point_ids = self._selected_point_ids(state, grid)
            if point_ids is None:
                return None, {}
            existing = set()
            if layer.warper_state is not None and grid in layer.warper_state.grids:
                existing = {p.point_id for p in layer.warper_state.grids[grid].pins}
            return "set_grid_pins", {"grid": grid,
                                     "point_ids": sorted(existing | set(point_ids))}

        if action == "reset":
            if layer.warper_state is None:
                # 无状态可重置:等价清空(仍是一次原子命令,进历史)
                return "replace_state", {"state": None}
            return "reset", {"scope": "all"}

        if action == "reset_all":
            return "replace_state", {"state": None}

        return None, {}

    @staticmethod
    def _grid_name(mode: str | None, state: Mapping[str, Any]) -> str | None:
        if mode == "chroma_luma":
            return _GRID_BY_INDEX.get(int(state.get("active_grid", 0)))
        return HS_GRID_NAME

    @staticmethod
    def _selected_point_ids(state: Mapping[str, Any], grid: str) -> list[str] | None:
        """从画布快照的 selection 推稳定 point_id;结构不符返回 None。"""
        grids = state.get("grids") or {}
        index = "0" if grid == HS_GRID_NAME else \
            str(int(state.get("active_grid", 0)))
        snapshot = grids.get(index) or {}
        selected = snapshot.get("selected") or []
        ids = []
        for item in selected:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                ids.append(make_point_id(grid, int(item[0]), int(item[1])))
            else:
                return None
        return ids
