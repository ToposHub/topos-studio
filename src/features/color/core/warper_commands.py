# -*- coding: utf-8 -*-
"""CW-B02:Warper 命令与手势生命周期服务(纯数据层,无 Qt/GPU)。

计划:`docs/plans/2026-11-12-color-warper-backend-plan.md` §4 CW-B02。

契约
----
1. **一次手势一次 undo**:`begin` 只建会话不压栈;首个**实质改变**状态的
   `update` 先压一条手势前基线(push 回调由宿主提供,走既有
   HistoryManager 入口),此后 update 不再压栈;`commit` 收口。
   无移动点击 / 全部 update 都未改变状态的手势 = 零历史条目。
2. **cancel 干净恢复**:恢复 begin 时的基线状态(含 legacy 桥回写),
   并丢弃为该手势压入的基线条目(`HistoryManager.discard_last_snapshot`)。
3. **revision 保护**:每层一个单调 revision,仅成功 commit 递增;
   `update/commit` 携带 base_revision 与当前不符 → `stale_revision` 拒绝,
   不污染状态与自动保存。请求 layer_id 与目标不符 → `wrong_target`
   (过期请求不得覆盖新目标)。
4. **操作分级**:本阶段实现机械操作(网格位移/分辨率重采样/Pin 集合/
   笔画增删/参数写入/模式激活/重置);算法操作(smooth/pull_push/
   auto_lock 等)如实返回 `op_unimplemented`,等 B04–B06 接入。
5. **渲染桥**:状态应用后回调 `on_applied(layer, state)`;宿主把
   legacy 可表达状态投影到既有 HSV 渲染路径(`engine.set_warper_params`),
   不可表达时保持画面不变(capability 如实报告)。

线程边界:UI 主线程使用;不持锁;渲染回调由宿主投递到引擎队列。
"""
from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from src.features.color.core.layer import Layer
from src.features.color.core.warper_hs_solver import (
    apply_move,
    pull_push,
    smooth_component,
    with_luma_component,
)
from src.features.color.core.warper_state import (
    CL_GRID_NAMES,
    CURRENT_ALGORITHM,
    HS_GRID_NAME,
    WARPER_SCHEMA_VERSION,
    WarperGrid,
    WarperPin,
    WarperPinPoint,
    WarperStateError,
    WarperStateV2,
    WarperStroke,
    legacy_ndarray,
    resample_grid,
)

__all__ = ["WarperCommandResult", "WarperGestureSession", "WarperCommandService"]

#: 内部哨兵:操作不产生变化(与"清空状态"的 None 返回值区分)。
_NO_CHANGE = object()

#: B02 机械操作 + CW-B04 HS 网格求解操作。
IMPLEMENTED_OPERATIONS = frozenset({
    "replace_state", "set_grid_offsets", "set_grid_resolution", "set_grid_pins",
    "add_stroke", "delete_objects", "set_stroke_params", "set_pin_params",
    "set_state_params", "activate_mode", "reset",
    "move_points", "smooth_component", "pull_push",
})
#: 已知但后端未实现的操作(如实拒绝,不假装成功)。
UNIMPLEMENTED_OPERATIONS = frozenset({
    "auto_lock", "solve_strokes",
})


@dataclass(frozen=True)
class WarperCommandResult:
    ok: bool
    reason: str
    revision: int
    dirty: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "reason": self.reason,
                "revision": self.revision, "dirty": self.dirty}


@dataclass
class WarperGestureSession:
    """一个活动手势的会话状态(每层至多一个;持有 layer 引用供 cancel 恢复)。"""
    gesture_id: str
    layer: Layer
    base_revision: int
    base_state: WarperStateV2 | None
    working: WarperStateV2 | None
    history_pushed: bool = False
    dirty: bool = False

    @property
    def layer_id(self) -> str:
        return self.layer.id


class WarperCommandService:
    """把 UI 命令信封落到权威 Layer.warper_state,管理手势生命周期。

    回调(宿主注入,均为 UI 线程):
    - ``history_push(layer)``:压入手势前基线(镜像 on_interaction_started
      的 push_state(copy_image=False) 语义)。
    - ``history_discard()``:丢弃手势压入的最后一条基线(cancel 用);
      宿主接 HistoryManager.discard_last_snapshot。
    - ``on_applied(layer, state)``:状态写入层之后调用;宿主把 legacy 可
      表达投影写引擎并请求渲染,不可表达则只刷新能力展示。
    - ``gesture_finished(layer, committed)``:commit/cancel 收口(空闲渲染、
      自动保存触发等宿主收尾)。
    """

    def __init__(
        self,
        *,
        history_push: Callable[[Layer], None] | None = None,
        history_discard: Callable[[], None] | None = None,
        on_applied: Callable[[Layer, WarperStateV2 | None], None] | None = None,
        gesture_finished: Callable[[Layer, bool], None] | None = None,
    ) -> None:
        self._history_push = history_push or (lambda layer: None)
        self._history_discard = history_discard or (lambda: None)
        self._on_applied = on_applied or (lambda layer, state: None)
        self._gesture_finished = gesture_finished or (lambda layer, committed: None)
        self._revisions: dict[str, int] = {}
        self._sessions: dict[str, WarperGestureSession] = {}

    # ---- 查询 ----

    def revision(self, layer: Layer) -> int:
        return self._revisions.get(layer.id, 0)

    def session_for(self, layer: Layer) -> WarperGestureSession | None:
        return self._sessions.get(layer.id)

    def cancel_active(self, layer: Layer, reason: str = "cancelled") -> WarperCommandResult | None:
        """宿主钩子入口:切层/undo/reset/关面板时调用,清活动会话。"""
        session = self._sessions.get(layer.id)
        if session is None:
            return None
        request = _cancel_request(layer.id, session.gesture_id)
        return self.handle(request, layer)

    def cancel_all(self, reason: str = "cancelled") -> int:
        """取消全部活动会话(模式切换/工程重置/关闭面板)。返回取消数量。"""
        count = 0
        for session in list(self._sessions.values()):
            self._finish_cancel(session, restore=True)
            count += 1
        return count

    # ---- 主入口 ----

    def handle(self, request, layer: Layer) -> WarperCommandResult:
        """处理一条命令信封(WarperCommandRequest)。失败不推进 revision。"""
        issues = request.diagnostics()
        if issues:
            return WarperCommandResult(False, f"invalid_request: {'; '.join(issues)}",
                                       self.revision(layer))
        if request.layer_id is not None and request.layer_id != layer.id:
            return WarperCommandResult(False, "wrong_target", self.revision(layer))
        if request.operation in UNIMPLEMENTED_OPERATIONS:
            return WarperCommandResult(False, "op_unimplemented", self.revision(layer))
        if request.operation not in IMPLEMENTED_OPERATIONS:
            return WarperCommandResult(False, "unknown_operation", self.revision(layer))

        phase = request.phase
        if phase == "begin":
            return self._begin(request, layer)
        if phase == "update":
            return self._update(request, layer)
        if phase == "commit":
            return self._commit(request, layer)
        return self._cancel(request, layer)

    # ---- 阶段 ----

    def _begin(self, request, layer: Layer) -> WarperCommandResult:
        session = self._sessions.get(layer.id)
        if session is not None:
            # 新手势顶替旧手势:先干净取消旧的(恢复其基线)
            self._finish_cancel(session, restore=True)
        self._sessions[layer.id] = WarperGestureSession(
            gesture_id=request.gesture_id or "",
            layer=layer,
            base_revision=self.revision(layer),
            base_state=layer.warper_state.copy() if layer.warper_state is not None else None,
            working=layer.warper_state.copy() if layer.warper_state is not None else None,
        )
        return WarperCommandResult(True, "ok", self.revision(layer))

    def _update(self, request, layer: Layer) -> WarperCommandResult:
        session = self._sessions.get(layer.id)
        if session is None or not request.gesture_id or session.gesture_id != request.gesture_id:
            return WarperCommandResult(False, "no_session", self.revision(layer))
        if request.base_revision is not None and request.base_revision != self.revision(layer):
            return WarperCommandResult(False, "stale_revision", self.revision(layer))
        try:
            candidate = self._apply_operation(session.working, request.operation, request.payload)
        except WarperStateError as exc:
            return WarperCommandResult(False, f"rejected: {exc.code}", self.revision(layer))
        if candidate is _NO_CHANGE:
            return WarperCommandResult(True, "ok", self.revision(layer), session.dirty)
        if _states_equal(candidate, session.working):
            return WarperCommandResult(True, "ok", self.revision(layer), session.dirty)
        # 首个实质变更:先压基线(此时层上仍是手势前状态)
        if not session.history_pushed:
            self._history_push(layer)
            session.history_pushed = True
        session.working = candidate
        session.dirty = True
        self._write(layer, candidate)
        return WarperCommandResult(True, "ok", self.revision(layer), True)

    def _commit(self, request, layer: Layer) -> WarperCommandResult:
        session = self._sessions.get(layer.id)
        if session is None or not request.gesture_id or session.gesture_id != request.gesture_id:
            return WarperCommandResult(False, "no_session", self.revision(layer))
        if request.base_revision is not None and request.base_revision != self.revision(layer):
            # revision 漂移(手势中途 undo/外部提交):恢复基线,不算成功
            self._finish_cancel(session, restore=True)
            return WarperCommandResult(False, "stale_revision", self.revision(layer))
        self._sessions.pop(layer.id, None)
        if session.dirty:
            self._revisions[layer.id] = self.revision(layer) + 1
            self._gesture_finished(layer, True)
            return WarperCommandResult(True, "ok", self.revision(layer), True)
        # 无实质变更:零历史、零 revision
        self._gesture_finished(layer, False)
        return WarperCommandResult(True, "ok", self.revision(layer), False)

    def _cancel(self, request, layer: Layer) -> WarperCommandResult:
        session = self._sessions.get(layer.id)
        if session is None:
            return WarperCommandResult(True, "ok", self.revision(layer))
        if request.gesture_id and session.gesture_id != request.gesture_id:
            return WarperCommandResult(False, "no_session", self.revision(layer))
        self._finish_cancel(session, restore=True)
        return WarperCommandResult(True, "cancelled", self.revision(layer))

    def _finish_cancel(self, session: WarperGestureSession, *, restore: bool) -> None:
        layer = session.layer
        self._sessions.pop(layer.id, None)
        if not restore:
            return
        if session.dirty:
            self._write(layer, session.base_state)
        if session.history_pushed:
            self._history_discard()
        self._gesture_finished(layer, False)

    # ---- 状态写入与渲染桥 ----

    def _write(self, layer: Layer, state: WarperStateV2 | None) -> None:
        layer.warper_state = state.copy() if state is not None else None
        # legacy 投影:可表达 → 旧 HSV 路径真实渲染;不可表达 → 清 legacy,
        # 画面保持不变(capability 由宿主如实展示)
        layer.warper_params = legacy_ndarray(state)
        self._on_applied(layer, state)

    # ---- 操作实现(机械;算法操作在 B04+) ----

    def _apply_operation(
        self, working: WarperStateV2 | None, operation: str, payload: Any,
    ) -> WarperStateV2 | None:
        """返回新状态(_NO_CHANGE 表示操作不产生变化;None 表示清空状态)。"""
        if operation == "replace_state":
            raw = payload.get("state") if isinstance(payload, dict) else None
            state = WarperStateV2.from_dict(raw) if isinstance(raw, dict) else None
            return state

        if operation == "activate_mode":
            mode = payload.get("mode")
            space = payload.get("color_space", working.color_space if working else "hsv")
            if working is not None and working.mode == mode and working.color_space == space:
                return _NO_CHANGE
            grids: dict[str, WarperGrid] = {}
            if mode == "hue_saturation":
                grids[HS_GRID_NAME] = WarperGrid.identity(HS_GRID_NAME, 6, 6, ("dh", "ds"))
            elif mode == "chroma_luma":
                grids["grid1"] = WarperGrid.identity("grid1", 6, 6, ("dc", "dl"))
                grids["grid2"] = WarperGrid.identity("grid2", 6, 6, ("dc", "dl"))
            return WarperStateV2(
                schema_version=WARPER_SCHEMA_VERSION, algorithm_version=CURRENT_ALGORITHM,
                mode=mode, color_space=space,
                axis_angle=working.axis_angle if working is not None else 0.0,
                grids=grids)

        if working is None:
            # 除 replace/activate 外的操作需要已存在状态
            raise WarperStateError("no_state", "目标层尚无 warper 状态,先 activate_mode")

        if operation == "move_points":
            grid = _payload_grid(working, payload)
            selected = _payload_selection(payload)
            deltas = {str(k): float(v) for k, v in (payload.get("deltas") or {}).items()}
            if not deltas:
                return _NO_CHANGE
            # Luma 需要第三分量:首次 luma 编辑确定性升级网格语义(B04)
            if "dl" in deltas and "dl" not in grid.semantics:
                grid = with_luma_component(grid)
            moved = apply_move(
                grid, selected, deltas,
                falloff=float(payload.get("falloff", 0.0)),
                auto_lock=int(payload.get("auto_lock", 0)),
                absolute=bool(payload.get("absolute", False)))
            return _grid_or_no_change(working, moved)

        if operation == "smooth_component":
            grid = _payload_grid(working, payload)
            component = str(payload.get("component", ""))
            selected_raw = payload.get("selected")
            selected = None if selected_raw is None else _payload_selection(payload)
            smoothed = smooth_component(grid, component, selected)
            return _grid_or_no_change(working, smoothed)

        if operation == "pull_push":
            grid = _payload_grid(working, payload)
            selected = _payload_selection(payload)
            factor = float(payload.get("factor", 1.0))
            pulled = pull_push(grid, selected, factor)
            return _grid_or_no_change(working, pulled)

        if operation == "set_grid_offsets":
            name = payload.get("grid")
            grid = working.grids.get(name) if isinstance(name, str) else None
            if grid is None:
                raise WarperStateError("invalid_id", f"网格 {name!r} 不存在")
            offsets = np.asarray(payload.get("offsets"), dtype=np.float32)
            return working.with_grid(grid.with_offsets(offsets))

        if operation == "set_grid_resolution":
            name = payload.get("grid")
            grid = working.grids.get(name) if isinstance(name, str) else None
            if grid is None:
                raise WarperStateError("invalid_id", f"网格 {name!r} 不存在")
            rows, cols = int(payload.get("rows")), int(payload.get("cols"))
            if (rows, cols) == (grid.rows, grid.cols):
                return _NO_CHANGE
            return working.with_grid(resample_grid(grid, rows, cols))

        if operation == "set_grid_pins":
            name = payload.get("grid")
            grid = working.grids.get(name) if isinstance(name, str) else None
            if grid is None:
                raise WarperStateError("invalid_id", f"网格 {name!r} 不存在")
            ids = payload.get("point_ids") or ()
            pins = tuple(WarperPin(point_id=str(pid)) for pid in ids)
            if len(set(p.point_id for p in pins)) != len(pins):
                raise WarperStateError("invalid_id", "pin point_id 重复")
            if pins == grid.pins:
                return _NO_CHANGE
            new_grid = WarperGrid(name=grid.name, rows=grid.rows, cols=grid.cols,
                                  components=grid.components, semantics=grid.semantics,
                                  offsets=grid.offsets, pins=pins,
                                  _extra_fields=copy.deepcopy(grid._extra_fields))
            return working.with_grid(new_grid)

        if operation == "add_stroke":
            raw = payload.get("stroke")
            stroke = WarperStroke.from_dict(raw) if isinstance(raw, dict) else None
            if stroke is None:
                raise WarperStateError("corrupt_payload", "add_stroke 需要 stroke 载荷")
            if working.mode != "chroma_warp":
                raise WarperStateError("invalid_mode", "笔画只属于 chroma_warp 模式")
            return WarperStateV2(
                schema_version=working.schema_version, algorithm_version=working.algorithm_version,
                mode=working.mode, color_space=working.color_space, axis_angle=working.axis_angle,
                grids=working.grids, strokes=working.strokes + (stroke,),
                pin_points=working.pin_points,
                _extra_fields=copy.deepcopy(working._extra_fields))

        if operation == "delete_objects":
            stroke_ids = set(payload.get("stroke_ids") or ())
            pin_ids = set(payload.get("pin_ids") or ())
            strokes = tuple(s for s in working.strokes if s.stroke_id not in stroke_ids)
            pins = tuple(p for p in working.pin_points if p.pin_id not in pin_ids)
            if len(strokes) == len(working.strokes) and len(pins) == len(working.pin_points):
                return _NO_CHANGE
            return WarperStateV2(
                schema_version=working.schema_version, algorithm_version=working.algorithm_version,
                mode=working.mode, color_space=working.color_space, axis_angle=working.axis_angle,
                grids=working.grids, strokes=strokes, pin_points=pins,
                _extra_fields=copy.deepcopy(working._extra_fields))

        if operation == "set_stroke_params":
            stroke_id = payload.get("stroke_id")
            matches = [s for s in working.strokes if s.stroke_id == stroke_id]
            if not matches:
                raise WarperStateError("invalid_id", f"笔画 {stroke_id!r} 不存在")
            merged = dict(matches[0].params)
            merged.update(payload.get("params") or {})
            updated = WarperStroke(
                stroke_id=matches[0].stroke_id, mode=matches[0].mode,
                source_rgb=matches[0].source_rgb, target_rgb=matches[0].target_rgb,
                params=merged, path=matches[0].path,
                _extra_fields=copy.deepcopy(matches[0]._extra_fields))
            strokes = tuple(updated if s.stroke_id == stroke_id else s for s in working.strokes)
            return WarperStateV2(
                schema_version=working.schema_version, algorithm_version=working.algorithm_version,
                mode=working.mode, color_space=working.color_space, axis_angle=working.axis_angle,
                grids=working.grids, strokes=strokes, pin_points=working.pin_points,
                _extra_fields=copy.deepcopy(working._extra_fields))

        if operation == "set_pin_params":
            pin_id = payload.get("pin_id")
            matches = [p for p in working.pin_points if p.pin_id == pin_id]
            if not matches:
                raise WarperStateError("invalid_id", f"Pin {pin_id!r} 不存在")
            merged = dict(matches[0].params)
            merged.update(payload.get("params") or {})
            updated = WarperPinPoint(
                pin_id=matches[0].pin_id, position=matches[0].position, params=merged,
                _extra_fields=copy.deepcopy(matches[0]._extra_fields))
            pins = tuple(updated if p.pin_id == pin_id else p for p in working.pin_points)
            return WarperStateV2(
                schema_version=working.schema_version, algorithm_version=working.algorithm_version,
                mode=working.mode, color_space=working.color_space, axis_angle=working.axis_angle,
                grids=working.grids, strokes=working.strokes, pin_points=pins,
                _extra_fields=copy.deepcopy(working._extra_fields))

        if operation == "set_state_params":
            updates = payload or {}
            axis = updates.get("axis_angle", working.axis_angle)
            if axis == working.axis_angle:
                return _NO_CHANGE
            return WarperStateV2(
                schema_version=working.schema_version, algorithm_version=working.algorithm_version,
                mode=working.mode, color_space=working.color_space, axis_angle=float(axis),
                grids=working.grids, strokes=working.strokes, pin_points=working.pin_points,
                _extra_fields=copy.deepcopy(working._extra_fields))

        if operation == "reset":
            scope = payload.get("scope", "all") if isinstance(payload, dict) else "all"
            if scope == "all":
                if working.is_identity():
                    return _NO_CHANGE
                return WarperStateV2(
                    schema_version=working.schema_version, algorithm_version=working.algorithm_version,
                    mode=working.mode, color_space=working.color_space, axis_angle=0.0,
                    grids={name: WarperGrid.identity(g.name, g.rows, g.cols, g.semantics)
                           for name, g in working.grids.items()},
                    strokes=(), pin_points=())
            if scope == "pins":
                if all(not g.pins for g in working.grids.values()) and not working.pin_points:
                    return _NO_CHANGE
                grids = {name: WarperGrid(name=g.name, rows=g.rows, cols=g.cols,
                                          components=g.components, semantics=g.semantics,
                                          offsets=g.offsets, pins=(),
                                          _extra_fields=copy.deepcopy(g._extra_fields))
                         for name, g in working.grids.items()}
                return WarperStateV2(
                    schema_version=working.schema_version, algorithm_version=working.algorithm_version,
                    mode=working.mode, color_space=working.color_space, axis_angle=working.axis_angle,
                    grids=grids, strokes=working.strokes, pin_points=(),
                    _extra_fields=copy.deepcopy(working._extra_fields))
            raise WarperStateError("invalid_param", f"reset scope {scope!r} 未知")

        raise WarperStateError("unknown_operation", operation)  # pragma: no cover - 上面已过滤


def _states_equal(a: WarperStateV2 | None, b: WarperStateV2 | None) -> bool:
    """内容等价(用于判定 update 是否实质变更;None=清空也是状态)。"""
    if a is None or b is None:
        return a is b
    return a.to_dict() == b.to_dict()


def _payload_grid(working: WarperStateV2, payload: Mapping[str, Any]) -> WarperGrid:
    name = payload.get("grid")
    grid = working.grids.get(name) if isinstance(name, str) else None
    if grid is None:
        raise WarperStateError("invalid_id", f"网格 {name!r} 不存在")
    return grid


def _payload_selection(payload: Mapping[str, Any]) -> list[tuple[int, int]]:
    raw = payload.get("selected") or ()
    selection: list[tuple[int, int]] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise WarperStateError("corrupt_payload", f"selection 项非法: {item!r}")
        selection.append((int(item[0]), int(item[1])))
    return selection


def _grid_or_no_change(working: WarperStateV2, grid: WarperGrid) -> WarperStateV2 | Any:
    """派生网格与原网格数值一致时声明无变化(不产生历史)。"""
    original = working.grids.get(grid.name)
    if original is not None and original is not grid and \
            np.array_equal(original.offsets, grid.offsets) and \
            original.semantics == grid.semantics:
        return _NO_CHANGE
    return working.with_grid(grid)


def _cancel_request(layer_id: str, gesture_id: str):
    from src.features.color.core.warper_state import WarperCommandRequest
    return WarperCommandRequest(
        request_id=f"cancel-{layer_id}-{gesture_id}", phase="cancel",
        operation="replace_state", payload={}, gesture_id=gesture_id, layer_id=layer_id)
