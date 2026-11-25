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
from dataclasses import replace
from typing import Any

import numpy as np

from src.features.color.core.layer import Layer
from src.features.color.core.warper_color import WARPER_SPACE_INDEX
from src.features.color.core.warper_commands import WarperCommandResult, WarperCommandService
from src.features.color.core.warper_state import (
    CL_GRID_NAMES,
    CURRENT_ALGORITHM,
    HS_GRID_NAME,
    LEGACY_ALGORITHM,
    RESOLVE_HS_ALGORITHMS,
    RESOLVE_HS_ALGORITHM,
    RESOLVE_HS_ALGORITHM_V2,
    RESOLVE_HS_ALGORITHM_V3,
    WARPER_SCHEMA_VERSION,
    WarperCommandRequest,
    WarperGrid,
    WarperPin,
    WarperStateError,
    WarperStateV2,
    XGradeState,
    legacy_from_ndarray,
    make_point_id,
    resolve_hs_render_space,
)

__all__ = ["WarperCommandAdapter"]

#: 纯 UI 动作:不触碰模型、不建历史(选择/查看/显示开关/等待 B04 的
#: 求解参数显示)。
UI_ONLY_ACTIONS = frozenset({
    "tool", "active_grid", "select", "draw", "invert", "column", "row", "ring",
    "all", "reset_selection", "falloff_add", "falloff_sub", "auto_lock", "range",
})

#: 有后端语义但尚未实现(B06 接入后迁移到映射表)。
UNIMPLEMENTED_ACTIONS = frozenset({
    "delete", "reset_pin", "stroke_parameter",
    "stroke", "point_stroke", "add_pin", "smooth",
})

#: CW-B04:面板分量名 → 网格分量语义。
_COMPONENT_SEMANTICS = {"hue": "dh", "sat": "ds", "luma": "dl", "chroma": "dc"}

#: 连续数值动作:同一签名合并为一个手势,flush() 收口。
CONTINUOUS_ACTIONS = frozenset({"axis_angle"})

_GRID_BY_INDEX = {0: CL_GRID_NAMES[0], 1: CL_GRID_NAMES[1]}
WARPER_PALETTE_GESTURES = frozenset({"calibrated_mesh", "palette_edit", "x_grade_edit"})


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
        self._open_layer: Layer | None = None

    # ---- 对外 ----

    def submit(self, command: Mapping[str, Any]) -> dict[str, Any]:
        """处理一条面板命令。返回 {ok, reason, handled, revision, dirty}。"""
        action = command.get("action")
        if action in UI_ONLY_ACTIONS:
            return {"ok": True, "reason": "ui_only", "handled": False,
                    "revision": None, "dirty": False}
        if action == "component":
            if _legacy_published(command):
                # legacy 已发布同一次编辑:命令层去重,不再二次提交
                return {"ok": True, "reason": "legacy_dedup", "handled": False,
                    "revision": None, "dirty": False}
            # 工具无选择时不改模型(计划 CW-B04 验证项)
            if not self._selection_cells(command.get("state") or {}):
                return {"ok": True, "reason": "ui_only_no_selection", "handled": False,
                        "revision": None, "dirty": False}
        if action in UNIMPLEMENTED_ACTIONS:
            return {"ok": False, "reason": "op_unimplemented", "handled": True,
                    "revision": None, "dirty": False}

        layer = self._resolve_layer()
        if self._open_layer is not None and self._open_layer is not layer:
            self._finish_open("cancel")
            return {"ok": False, "reason": "stale_target", "handled": True,
                    "revision": self._service.revision(layer) if layer is not None else None, "dirty": False}
        if layer is None:
            return {"ok": False, "reason": "no_target", "handled": True,
                    "revision": None, "dirty": False}

        calibrated_phase = None
        if action in WARPER_PALETTE_GESTURES:
            calibrated_phase = (command.get("payload") or {}).get("phase")
            if calibrated_phase in {"commit", "cancel"}:
                return self._finish_calibrated(calibrated_phase, layer, action)
            if calibrated_phase not in {"update", "atomic"}:
                return {"ok": False, "reason": "invalid_mesh_phase", "handled": True,
                        "revision": self._service.revision(layer), "dirty": False}

        try:
            operation, payload = self._translate(action, command, layer)
        except WarperStateError as exc:
            return {"ok": False, "reason": f"rejected: {exc.code}", "handled": True,
                    "revision": self._service.revision(layer), "dirty": False}
        if operation is None:
            return {"ok": False, "reason": "unsupported_targeting", "handled": True,
                    "revision": None, "dirty": False}

        continuous = action in CONTINUOUS_ACTIONS or calibrated_phase == "update"
        signature = (operation, action) if calibrated_phase else (operation, self._target_key(payload))
        if continuous and signature == self._open_signature:
            # 同一手势的后续 update
            request = self._request("update", operation, payload, layer,
                                    gesture_id=self._open_gesture_id)
            result = self._service.handle(request, layer)
            if calibrated_phase and not result.ok:
                self._finish_calibrated("cancel", layer, action)
        else:
            self.flush()  # 不同签名:先收口旧手势,保证一次手势一次 undo
            gesture_id = f"warper-{next(self._gesture_ids)}"
            begun = self._service.handle(
                self._request("begin", operation, payload, layer, gesture_id=gesture_id), layer)
            if not begun.ok:
                return self._result_dict(begun, handled=True)
            result = self._service.handle(
                self._request("update", operation, payload, layer, gesture_id=gesture_id), layer)
            if continuous and result.ok:
                self._open_signature = signature
                self._open_gesture_id = gesture_id
                self._open_layer = layer
                return self._result_dict(result, handled=True)
            finish = "commit" if result.ok else "cancel"
            finished = self._service.handle(
                self._request(finish, operation, payload, layer, gesture_id=gesture_id), layer)
            if calibrated_phase and result.ok:
                result = finished
        return self._result_dict(result, handled=True)

    def flush(self) -> None:
        """收口打开的连续手势(宿主静默定时器/生命周期钩子调用)。"""
        if self._open_layer is not None:
            self._finish_open("commit" if self._resolve_layer() is self._open_layer else "cancel")

    def cancel(self) -> None:
        """取消打开的手势(切层/undo/reset/关面板钩子)。"""
        self._open_gesture_id = None
        self._open_signature = None
        self._open_layer = None
        self._service.cancel_all("host_lifecycle")

    # ---- 内部 ----

    def _finish_calibrated(self, phase: str, layer: Layer, action: str) -> dict[str, Any]:
        result = self._finish_open(phase) if self._open_signature == ("replace_state", action) else None
        if result is None:
            return {"ok": True, "reason": "no_mesh_edit", "handled": False,
                    "revision": self._service.revision(layer), "dirty": False}
        return self._result_dict(result, handled=True)

    def _finish_open(self, phase: str) -> WarperCommandResult | None:
        layer = self._open_layer
        gesture_id = self._open_gesture_id
        self._open_signature = self._open_gesture_id = None
        self._open_layer = None
        if layer is None or gesture_id is None:
            return None
        request = self._request(phase, "replace_state", {}, layer, gesture_id=gesture_id)
        return self._service.handle(request, layer)

    @staticmethod
    def _calibrated_state(payload: Mapping[str, Any], layer: Layer) -> WarperStateV2:
        offsets = np.asarray(payload["offsets"], dtype=np.float32)
        rows, cols, _ = offsets.shape
        previous = layer.warper_state.for_mode("hue_saturation") if layer.warper_state is not None else None
        if previous is None or previous.algorithm_version not in RESOLVE_HS_ALGORITHMS:
            previous = WarperStateV2(
                schema_version=WARPER_SCHEMA_VERSION,
                algorithm_version=payload.get("algorithm_version", RESOLVE_HS_ALGORITHM_V2),
                mode="hue_saturation", color_space="hsp", axis_angle=0.0,
                grids={HS_GRID_NAME: WarperGrid.identity(HS_GRID_NAME, rows, cols, ("dh", "ds", "dl"))})
        grid = previous.grids[HS_GRID_NAME]
        old_pins = {pin.point_id: pin for pin in grid.pins}
        pins = tuple(old_pins.get(point_id) or WarperPin(point_id, _extra_fields={"affects_response": True})
                     for point_id in payload.get("pins", ()))
        if (grid.rows, grid.cols) != (rows, cols):
            resized = WarperGrid.identity(HS_GRID_NAME, rows, cols, ("dh", "ds", "dl"))
            grid = replace(grid, rows=rows, cols=cols, offsets=resized.offsets)
        white = payload.get("white_point", previous.white_point)
        if not isinstance(white, (list, tuple)):
            raise WarperStateError("invalid_param", "Invalid Hue–Saturation white point")
        return replace(previous.with_grid(replace(grid.with_offsets(offsets), pins=pins)), white_point=tuple(white))

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

    @staticmethod
    def _x_grade_base(layer: Layer, *, with_legacy: bool = True) -> WarperStateV2:
        """Wrap a raw legacy mesh without changing its renderer or coordinates."""
        raw = layer.warper_params if with_legacy else None
        if raw is None:
            from src.features.color.core.warper_state import empty_x_grade_carrier

            return empty_x_grade_carrier()
        space = layer.warper_space
        legacy = next((name for name, index in WARPER_SPACE_INDEX.items() if index == space), None)
        calibrated = next((version for version in RESOLVE_HS_ALGORITHMS if resolve_hs_render_space(version) == space), None)
        semantics = ("dh", "ds", "dl") if calibrated is not None else ("dh", "ds")
        if (legacy is None and calibrated is None) or raw.ndim != 3 or raw.shape[2] != len(semantics):
            raise WarperStateError("unsupported_targeting", "The legacy Warper response needs versioned controls")
        rows, columns, _ = raw.shape
        grid = WarperGrid.identity(HS_GRID_NAME, rows, columns, semantics).with_offsets(raw)
        algorithm = calibrated or (LEGACY_ALGORITHM if legacy == "hsv" else CURRENT_ALGORITHM)
        return WarperStateV2(WARPER_SCHEMA_VERSION, algorithm, "hue_saturation", legacy or "hsp", 0.0,
                             {HS_GRID_NAME: grid})

    def _translate(self, action: str, command: Mapping[str, Any],
                   layer: Layer) -> tuple[str | None, dict[str, Any]]:
        payload_in = command.get("payload") or {}
        state = command.get("state") or {}
        mode = command.get("mode")

        if action == "x_grade_edit":
            state = layer.warper_state
            if layer.warper_state_unparsed is not None or (state is not None and "x_grade" in state._extra_fields):
                raise WarperStateError("schema_unsupported", "The layer has unreadable Warper controls")
            grade = XGradeState.from_dict(payload_in.get("state"))
            if not grade.strokes and not grade.pin_points and grade.protect_neutral and not grade._extra_fields:
                grade = None
            if grade is not None:
                state = replace(state or self._x_grade_base(layer), x_grade=grade)
            elif state is not None:
                from src.features.color.core.warper_state import is_empty_x_grade_carrier

                if is_empty_x_grade_carrier(state):
                    return "replace_state", {"state": None}
                state = replace(state, x_grade=None)
            else:
                return "replace_state", {"state": None, "preserve_legacy": True}
            return "replace_state", {"state": state.to_dict()}

        if action in WARPER_PALETTE_GESTURES:
            previous = layer.warper_state
            if previous is None and layer.warper_params is not None and layer.warper_space == 0:
                raw = layer.warper_params
                if raw.ndim == 3 and raw.shape[0] == raw.shape[1] and raw.shape[2] == 2:
                    previous = legacy_from_ndarray(raw)
            if action == "palette_edit":
                raw = payload_in.get("state")
                state = WarperStateV2.from_dict(raw) if isinstance(raw, Mapping) else None
                if (state is not None and previous is None and not state.strokes and not state.pin_points
                        and state.is_identity() and not any(grid.pins for grid in state.grids.values())):
                    state = None
            elif payload_in.get("upgrade_to_v2") or payload_in.get("upgrade_to_v3"):
                old = previous.for_mode("hue_saturation") if previous is not None else None
                old_version = RESOLVE_HS_ALGORITHM if payload_in.get("upgrade_to_v2") else RESOLVE_HS_ALGORITHM_V2
                new_version = RESOLVE_HS_ALGORITHM_V2 if payload_in.get("upgrade_to_v2") else RESOLVE_HS_ALGORITHM_V3
                if (payload_in.get("phase") != "atomic" or previous is None
                        or old is None or old.algorithm_version != old_version):
                    return None, {}
                state = replace(old, algorithm_version=new_version)
            else:
                state = self._calibrated_state(payload_in, layer) if payload_in.get("enabled") else None
            if state is not None and previous is not None:
                state = previous.replacing_mode(state)
            elif state is None and previous is not None and (
                    previous.x_grade is not None or "x_grade" in previous._extra_fields):
                identity = self._x_grade_base(layer, with_legacy=False)
                state = previous.replacing_mode(identity)
                state = replace(state, other_modes=())
            return "replace_state", {"state": state.to_dict() if state is not None else None}

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

        if action == "component":
            # CW-B04:数值/平滑手势 → 求解操作(falloff/Auto Lock 随快照携带)
            semantic = _COMPONENT_SEMANTICS.get(str(payload_in.get("component", "")))
            if semantic is None:
                return None, {}
            grid = self._grid_name(mode, state)
            if grid is None:
                return None, {}
            selected = self._selection_cells(state)
            auto_lock = int(state.get("lock_points", 1)) if state.get("auto_lock") else 0
            common = {"grid": grid, "selected": selected}
            if payload_in.get("smooth"):
                return "smooth_component", {**common, "component": semantic}
            return "move_points", {**common, "deltas": {semantic: float(payload_in.get("value", 0.0))},
                                   "absolute": True, "falloff": float(state.get("falloff", 0.0)),
                                   "auto_lock": auto_lock}

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
            previous = layer.warper_state
            if previous is not None and (previous.x_grade is not None or "x_grade" in previous._extra_fields):
                identity = self._x_grade_base(layer, with_legacy=False)
                state = replace(previous.replacing_mode(identity), other_modes=())
                return "replace_state", {"state": state.to_dict()}
            return "replace_state", {"state": None}

        return None, {}

    @staticmethod
    def _grid_name(mode: str | None, state: Mapping[str, Any]) -> str | None:
        if mode == "chroma_luma":
            return _GRID_BY_INDEX.get(int(state.get("active_grid", 0)))
        return HS_GRID_NAME

    @staticmethod
    def _selection_cells(state: Mapping[str, Any]) -> list[list[int]]:
        """活动网格快照的选点 (row, col) 列表;结构不符返回空(按无选择处理)。"""
        grids = state.get("grids") or {}
        index = str(int(state.get("active_grid", 0) or 0))
        snapshot = grids.get(index) or {}
        selected = snapshot.get("selected") or []
        cells = []
        for item in selected:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                cells.append([int(item[0]), int(item[1])])
        return cells

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
