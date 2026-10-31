"""统一的 Composite 编辑协议。

这个模块是 Composite 的脚本、AI/MCP 和 UI 之间的共同领域层。它只接受
JSON-safe 的编辑计划，不把 ``Node`` / ``NodeGraph`` 的内部对象暴露给调用方。

当前协议覆盖：

* numeric 参数关键帧的增删/批量替换；
* interpolation、Bezier 手柄、tangent mode、前后外推；
* 参数静态值和表达式；
* 可重放的 plan hash 与 graph generation 乐观锁。

真正的主线程切换、用户授权、撤销记录和工程持久化由宿主负责；本服务负责
领域校验和原子地改动传入的 graph。
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any, Mapping

from src.shared.keyframe import (
    InterpolationType,
    Keyframe,
    keyframe_track_from_dict,
    make_keyframe_track,
)


PROTOCOL = "topos.composite/v1"
TANGENT_MODES = frozenset({"auto", "smooth", "broken", "linear", "flat", "step"})
EXTRAPOLATION_MODES = frozenset({"constant", "linear"})
KEYFRAME_OPS = frozenset({
    "add_keyframe", "set_keyframes", "remove_keyframe", "clear_keyframes",
    "set_interpolation", "set_bezier_handles", "set_tangent_mode",
    "set_extrapolation", "set_parameter", "set_expression",
    "remove_expression",
})
# C4: node-graph topology operations. Values mirror
# ai/composite_plan.py SUPPORTED_NODE_OPS so scripts, MCP and AI speak
# one protocol. A single plan mixing KEYFRAME_OPS and NODE_OPS is valid;
# the host records the whole plan as ONE composite undo group.
NODE_OPS = frozenset({
    "add_node", "remove_node", "connect", "disconnect", "set_param",
    # T-P1-60 批次 B：图结构 AI 操作扩展（脚本/MCP/AI 同一协议；与
    # ai/composite_plan.SUPPORTED_NODE_OPS 对齐——group/ungroup 此前
    # 协议已声明但服务层不执行，属"声明可执行应用必炸"类缺陷）
    "duplicate_node", "set_output_target", "group", "ungroup",
})


class CompositeEditError(ValueError):
    """一个可直接显示给脚本/AI 的领域校验错误。"""


@dataclass(frozen=True)
class CompositeEditPlan:
    """可序列化、可哈希、可重放的 Composite 编辑计划。"""

    plan_id: str
    operations: tuple[dict[str, Any], ...]
    composition_id: str | None = None
    base_generation: int | None = None
    title: str = "Composite edit"
    protocol: str = PROTOCOL

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CompositeEditPlan":
        if not isinstance(data, Mapping):
            raise CompositeEditError("composite plan must be an object")
        operations = data.get("operations", ())
        if not isinstance(operations, (list, tuple)):
            raise CompositeEditError("composite plan operations must be an array")
        # AUD-5608: 该类是对脚本/AI 暴露的 JSON-safe 领域边界，畸形输入
        # 必须以 CompositeEditError 报告——非 Mapping 元素的 dict(op) 与
        # 非整数 base_generation 的 int() 此前会让 TypeError/ValueError
        # 直接穿透到宿主。
        coerced_operations: list[dict[str, Any]] = []
        for index, op in enumerate(operations):
            if not isinstance(op, Mapping):
                raise CompositeEditError(
                    f"composite plan operation #{index} must be an object, "
                    f"got {type(op).__name__}")
            coerced_operations.append(dict(op))
        base_generation: int | None = None
        if data.get("base_generation") is not None:
            try:
                base_generation = int(data["base_generation"])
            except (TypeError, ValueError) as exc:
                raise CompositeEditError(
                    f"base_generation must be an integer, "
                    f"got {data['base_generation']!r}") from exc
        return cls(
            plan_id=str(data.get("plan_id") or "composite-plan"),
            operations=tuple(coerced_operations),
            composition_id=(str(data["composition_id"]) if data.get("composition_id") is not None else None),
            base_generation=base_generation,
            title=str(data.get("title") or "Composite edit"),
            protocol=str(data.get("protocol") or PROTOCOL),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "plan_id": self.plan_id,
            "composition_id": self.composition_id,
            "base_generation": self.base_generation,
            "title": self.title,
            "operations": [dict(op) for op in self.operations],
        }

    def content_hash(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CompositeDiagnostic:
    severity: str
    message: str
    operation_index: int | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {"severity": self.severity, "message": self.message}
        if self.operation_index is not None:
            data["operation_index"] = self.operation_index
        return data


@dataclass(frozen=True)
class CompositeValidationReport:
    diagnostics: tuple[CompositeDiagnostic, ...] = field(default_factory=tuple)

    @property
    def errors(self) -> tuple[CompositeDiagnostic, ...]:
        return tuple(d for d in self.diagnostics if d.severity == "error")

    @property
    def warnings(self) -> tuple[CompositeDiagnostic, ...]:
        return tuple(d for d in self.diagnostics if d.severity == "warning")

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": [d.to_dict() for d in self.errors],
            "warnings": [d.to_dict() for d in self.warnings],
        }


@dataclass(frozen=True)
class CompositeApplyResult:
    applied: bool
    plan_id: str
    plan_hash: str
    generation_before: int
    generation_after: int
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    report: CompositeValidationReport = field(default_factory=CompositeValidationReport)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.applied,
            "applied": self.applied,
            "plan_id": self.plan_id,
            "plan_hash": self.plan_hash,
            "generation_before": self.generation_before,
            "generation_after": self.generation_after,
            "report": self.report.to_dict(),
            "error": self.error,
        }


class CompositeEditService:
    """在一个已由宿主锁定的 NodeGraph 上执行领域编辑。"""

    def __init__(self, graph: Any) -> None:
        self.graph = graph

    @property
    def generation(self) -> int:
        return int(getattr(self.graph, "generation", getattr(self.graph, "_generation", 0)))

    def snapshot(self) -> dict[str, Any]:
        """生成给 AI/脚本看的脱敏结构快照。"""
        nodes = []
        for node in self.graph.nodes.values():
            param_defs = []
            for pdef in node.get_param_defs().values():
                param_defs.append({
                    "name": pdef.name,
                    "type": pdef.ptype,
                    "min": pdef.min_val,
                    "max": pdef.max_val,
                    "step": pdef.step,
                    "animated": node.is_param_animated(pdef.name),
                    "allow_expression": bool(pdef.allow_expression),
                })
            # 文件路径、绝对路径和大块资源值不送给模型；参数定义和动画轨道仍完整。
            safe_params = {}
            for name, value in node.parameters.items():
                pdef = node.get_param_defs().get(name)
                if pdef is not None and pdef.ptype in {"file", "hdri"}:
                    continue
                if _json_safe_scalar(value):
                    safe_params[name] = value
            nodes.append({
                "id": node.id,
                "type": node.node_type,
                "title": node.node_title,
                "parameters": safe_params,
                "param_defs": param_defs,
                "keyframe_tracks": _safe_copy(node.keyframe_tracks),
                "expressions": dict(getattr(node, "_expressions", {})),
                "disabled": bool(node.disabled),
            })
        connections = [
            {
                "src_node_id": c.src_node_id,
                "src_port_name": c.src_port_name,
                "dst_node_id": c.dst_node_id,
                "dst_port_name": c.dst_port_name,
            }
            for c in self.graph.connections
        ]
        return {
            "protocol": PROTOCOL,
            "generation": self.generation,
            "node_count": len(nodes),
            "nodes": nodes,
            "connections": connections,
        }

    def validate(self, plan: CompositeEditPlan | Mapping[str, Any]) -> CompositeValidationReport:
        plan = _coerce_plan(plan)
        diagnostics: list[CompositeDiagnostic] = []
        known_frames: dict[tuple[str, str], set[int]] = {}
        # C4: plan 内拓扑模拟状态（已 add 的节点 id / 已 remove 的节点 id /
        # plan 内新建的连接），让后续操作可以在同一 plan 里引用刚创建的
        # 节点或断开刚建立的连接。
        # AUD-5607: 组操作同样需要 plan 内模拟（同 plan 内 group 后 ungroup
        # 合法），记录 plan 内新建/删除的组 id。
        topo_state = {"added": set(), "removed": set(),
                      "added_types": {}, "connected": set(),
                      "groups_added": set(), "groups_removed": set()}
        if plan.protocol != PROTOCOL:
            diagnostics.append(CompositeDiagnostic("error", f"unsupported protocol: {plan.protocol}"))
        if plan.base_generation is not None and plan.base_generation != self.generation:
            diagnostics.append(CompositeDiagnostic(
                "error", f"graph generation conflict: plan={plan.base_generation}, current={self.generation}"
            ))
        if not plan.operations:
            diagnostics.append(CompositeDiagnostic("warning", "plan contains no operations"))
        for index, operation in enumerate(plan.operations):
            try:
                self._validate_operation(operation, topo_state)
                self._validate_operation_sequence(operation, known_frames)
            except CompositeEditError as exc:
                diagnostics.append(CompositeDiagnostic("error", str(exc), index))
            else:
                self._advance_operation_sequence(operation, known_frames)
        return CompositeValidationReport(tuple(diagnostics))

    def _validate_operation_sequence(
        self, operation: Mapping[str, Any], known_frames: dict[tuple[str, str], set[int]]
    ) -> None:
        """校验同一 plan 内操作的顺序依赖（例如 add 后再调手柄）。"""
        kind = str(operation.get("op") or "")
        if kind not in {"add_keyframe", "remove_keyframe", "set_interpolation", "set_bezier_handles", "set_tangent_mode"}:
            return
        node_id = str(operation.get("node_id") or "")
        param_name = str(operation.get("param_name") or "")
        key = (node_id, param_name)
        if key not in known_frames:
            node = self.graph.get_node(node_id)
            track = node.keyframe_tracks.get(param_name) if node is not None else None
            known_frames[key] = {
                int(data.get("frame"))
                for data in (track or {}).get("keyframes", [])
                if isinstance(data, Mapping) and data.get("frame") is not None
            }
        frame = int(operation["frame"])
        if kind != "add_keyframe" and frame not in known_frames[key]:
            raise CompositeEditError(f"keyframe not found: {node_id}.{param_name}@{frame}")

    def _advance_operation_sequence(
        self, operation: Mapping[str, Any], known_frames: dict[tuple[str, str], set[int]]
    ) -> None:
        kind = str(operation.get("op") or "")
        if kind not in {"add_keyframe", "set_keyframes", "remove_keyframe", "clear_keyframes"}:
            return
        key = (str(operation.get("node_id") or ""), str(operation.get("param_name") or ""))
        if kind == "add_keyframe":
            known_frames.setdefault(key, set()).add(int(operation["frame"]))
        elif kind == "set_keyframes":
            known_frames[key] = {int(data["frame"]) for data in operation["keyframes"]}
        elif kind == "remove_keyframe":
            known_frames.setdefault(key, set()).discard(int(operation["frame"]))
        else:
            known_frames[key] = set()

    def apply(self, plan: CompositeEditPlan | Mapping[str, Any]) -> CompositeApplyResult:
        plan = _coerce_plan(plan)
        before_generation = self.generation
        report = self.validate(plan)
        if not report.ok:
            return CompositeApplyResult(
                False, plan.plan_id, plan.content_hash(), before_generation, before_generation,
                report=report, error="composite plan validation failed",
            )
        before = self.graph.to_dict()
        notes: list[CompositeDiagnostic] = []
        try:
            for index, operation in enumerate(plan.operations):
                note = self._apply_operation(operation)
                if note:
                    notes.append(CompositeDiagnostic("info", note, index))
        except Exception as exc:
            # 所有操作在 validate 阶段已经通过；这里仅防止宿主/节点异常造成半套计划。
            _restore_graph_in_place(self.graph, before)
            return CompositeApplyResult(
                False, plan.plan_id, plan.content_hash(), before_generation, self.generation,
                report=report, error=f"composite plan rolled back: {exc}",
            )
        if notes:
            report = CompositeValidationReport(
                tuple(report.diagnostics) + tuple(notes))
        # AUD-5602: 关键帧/参数/表达式类操作只递增节点 param_revision，
        # 不经过会递增 graph._generation 的拓扑 API——纯关键帧 plan 成功后
        # generation_after == generation_before，validate 的 base_generation
        # 乐观锁发现不了同代冲突，to_dict 写入 headless 预览缓存的
        # graph_revision token 也不变（旧帧复用）。apply 成功后统一 bump。
        if plan.operations:
            self._bump_generation()
        return CompositeApplyResult(
            True, plan.plan_id, plan.content_hash(), before_generation, self.generation,
            before=before, after=self.graph.to_dict(), report=report,
        )

    def _bump_generation(self) -> None:
        """AUD-5602: 让任何成功的 plan 变更都能被 generation 观察到。

        优先走引擎公开的 bump_generation（持图锁递增 _generation）；
        测试替身/非引擎图退化为其 _generation 属性；两者皆无则跳过。
        """
        bump = getattr(self.graph, "bump_generation", None)
        if callable(bump):
            bump()
            return
        current = getattr(self.graph, "_generation", None)
        if isinstance(current, int):
            self.graph._generation = current + 1

    # ---------------- validation ----------------

    def _node_and_param(self, operation: Mapping[str, Any],
                        topo_state: dict[str, Any] | None = None) -> tuple[Any, Any]:
        node_id = str(operation.get("node_id") or "")
        param_name = str(operation.get("param_name") or "")
        # AUD-5603: validate/apply 状态不对称——remove_node 之后同一 plan
        # 内的操作仍可引用被删节点（validate 时节点还在图里，apply 时已被
        # 删除）。与 _resolve_node_for_topology 对齐，在 validate 阶段拒绝。
        if topo_state is not None and node_id in topo_state.get("removed", ()):
            raise CompositeEditError(f"node already removed in this plan: {node_id!r}")
        node = self.graph.get_node(node_id)
        if node is None and topo_state is not None \
                and node_id in topo_state.get("added", ()):
            # 同一 plan 里刚 add 的节点尚未入图：用临时实例校验参数语义，
            # apply 阶段节点已真实入图，走正常路径。
            node = _peek_node(topo_state.get("added_types", {}).get(node_id))
        if node is None:
            raise CompositeEditError(f"node not found: {node_id!r}")
        pdef = node.get_param_defs().get(param_name)
        if pdef is None:
            raise CompositeEditError(f"parameter not found: {node_id}.{param_name}")
        return node, pdef

    def _validate_operation(self, operation: Mapping[str, Any],
                            topo_state: dict[str, set] | None = None) -> None:
        if not isinstance(operation, Mapping):
            raise CompositeEditError("operation must be an object")
        kind = str(operation.get("op") or "")
        if kind in NODE_OPS:
            self._validate_node_operation(
                operation,
                topo_state or {"added": set(), "removed": set(),
                               "added_types": {}, "connected": set(),
                               "groups_added": set(), "groups_removed": set()})
            return
        if kind not in KEYFRAME_OPS:
            raise CompositeEditError(f"unsupported composite operation: {kind!r}")
        node, pdef = self._node_and_param(operation, topo_state)
        if kind in {"add_keyframe", "set_keyframes", "remove_keyframe", "clear_keyframes", "set_interpolation", "set_bezier_handles", "set_tangent_mode", "set_extrapolation"}:
            if not pdef.is_animatable():
                raise CompositeEditError(f"parameter is not animatable: {node.id}.{pdef.name}")
        if kind == "add_keyframe":
            _validate_frame(operation.get("frame"))
            _validate_typed_value(pdef, operation.get("value"))
            _validate_interpolation(operation.get("interpolation", "linear"))
            _validate_tangent(operation.get("tangent_mode", "auto"))
            _validate_handle(operation.get("handle_left"), "handle_left")
            _validate_handle(operation.get("handle_right"), "handle_right")
        elif kind == "set_keyframes":
            keyframes = operation.get("keyframes")
            if not isinstance(keyframes, list):
                raise CompositeEditError("set_keyframes.keyframes must be an array")
            seen: set[int] = set()
            for data in keyframes:
                if not isinstance(data, Mapping):
                    raise CompositeEditError("each keyframe must be an object")
                frame = _validate_frame(data.get("frame"))
                if frame in seen:
                    raise CompositeEditError(f"duplicate keyframe frame: {frame}")
                seen.add(frame)
                _validate_typed_value(pdef, data.get("value"))
                _validate_interpolation(data.get("interpolation", "linear"))
                _validate_tangent(data.get("tangent_mode", "auto"))
                _validate_handle(data.get("handle_left"), "handle_left")
                _validate_handle(data.get("handle_right"), "handle_right")
            _validate_extrap(operation.get("pre_extrap", "constant"), "pre_extrap")
            _validate_extrap(operation.get("post_extrap", "constant"), "post_extrap")
        elif kind in {"remove_keyframe", "set_interpolation", "set_bezier_handles", "set_tangent_mode"}:
            _validate_frame(operation.get("frame"))
            if kind == "set_interpolation":
                _validate_interpolation(operation.get("interpolation"))
            elif kind == "set_tangent_mode":
                _validate_tangent(operation.get("tangent_mode"))
            elif kind == "set_bezier_handles":
                if "handle_left" not in operation and "handle_right" not in operation:
                    raise CompositeEditError("set_bezier_handles requires handle_left or handle_right")
                _validate_handle(operation.get("handle_left"), "handle_left")
                _validate_handle(operation.get("handle_right"), "handle_right")
        elif kind == "set_extrapolation":
            _validate_extrap(operation.get("pre_extrap", "constant"), "pre_extrap")
            _validate_extrap(operation.get("post_extrap", "constant"), "post_extrap")
        elif kind == "set_parameter":
            # AUD-5605: 缺 "value" 键此前 .get() 返回 None 通过全部校验，
            # apply 阶段 operation["value"] 抛 KeyError 落入整体回滚。
            if "value" not in operation:
                raise CompositeEditError("set_parameter.value is required")
            _validate_static_param_value(pdef, operation["value"], "set_parameter.value")
        elif kind == "set_expression":
            expression = operation.get("expression")
            if not isinstance(expression, str) or not expression.strip():
                raise CompositeEditError("expression must be a non-empty string")
            if not pdef.allow_expression or not pdef.is_expression_compatible():
                raise CompositeEditError(f"parameter does not accept expressions: {node.id}.{pdef.name}")

    # ---------------- node-graph operation validation (C4) ----------------

    def _validate_node_operation(self, operation: Mapping[str, Any],
                                 topo_state: dict[str, Any]) -> None:
        """结构化校验一个拓扑操作。复用引擎自身的端口/类型/环检测语义，
        错误以 CompositeEditError 文本返回（脚本/AI 可读）。"""
        kind = str(operation.get("op") or "")
        if kind == "add_node":
            node_type = str(operation.get("node_type") or "")
            registry = _node_registry()
            # AUD-5604: NODE_REGISTRY 对可选功能模块保留 None 延迟占位
            # （如 motion_graphic）——`node_type in registry` 为真但值不可
            # 调用，此前 validate 阶段抛未捕获 TypeError 逃逸领域边界。
            node_class = registry.get(node_type)
            if node_class is None:
                if node_type in registry:
                    raise CompositeEditError(
                        f"node_type is not available in this session: {node_type!r} "
                        f"(optional feature module not enabled)")
                raise CompositeEditError(
                    f"unknown node_type: {node_type!r} (available: {sorted(registry)})")
            node_id = str(operation.get("node_id") or "")
            if node_id:
                if node_id in topo_state["added"]:
                    raise CompositeEditError(f"duplicate node_id in plan: {node_id!r}")
                if self.graph.get_node(node_id) is not None:
                    raise CompositeEditError(f"node_id already exists: {node_id!r}")
                if not _valid_node_id(node_id):
                    raise CompositeEditError(f"invalid node_id: {node_id!r}")
            # AUD-5606: position 此前完全不校验——类型错误在 apply 期才爆
            # （validate/apply 不对称），inf/nan 更会经 to_dict 写出非标准
            # JSON 的 Infinity 破坏工程持久化。
            _validate_position(operation.get("position"))
            parameters = operation.get("parameters")
            if parameters is not None:
                if not isinstance(parameters, Mapping):
                    raise CompositeEditError("add_node.parameters must be an object")
                probe = node_class(node_id="_probe")
                for name, value in parameters.items():
                    if not isinstance(name, str) or not name:
                        raise CompositeEditError("add_node.parameters keys must be non-empty strings")
                    pdef = probe.get_param_defs().get(name)
                    if pdef is None:
                        raise CompositeEditError(
                            f"parameter not found: {node_type}.{name}")
                    _validate_static_param_value(
                        pdef, value, f"add_node.parameters.{name}")
            if node_id:
                topo_state["added"].add(node_id)
                topo_state["added_types"][node_id] = node_type
            return

        if kind == "duplicate_node":
            source_id = str(operation.get("node_id") or "")
            # AUD-5603: 引用本 plan 内已删除的源节点属于 validate/apply
            # 状态不对称（validate 时仍在图，apply 时已被删）。
            if source_id in topo_state.get("removed", ()):
                raise CompositeEditError(f"node already removed in this plan: {source_id!r}")
            if source_id not in topo_state["added"] \
                    and self.graph.get_node(source_id) is None:
                raise CompositeEditError(f"node not found: {source_id!r}")
            new_id = str(operation.get("new_node_id") or "")
            if not new_id:
                raise CompositeEditError("duplicate_node.new_node_id is required")
            if not _valid_node_id(new_id):
                raise CompositeEditError(f"invalid node_id: {new_id!r}")
            if new_id in topo_state["added"] \
                    or self.graph.get_node(new_id) is not None:
                raise CompositeEditError(f"node_id already exists: {new_id!r}")
            topo_state["added"].add(new_id)
            source_type = topo_state["added_types"].get(source_id)
            if source_type is None:
                source_node = self.graph.get_node(source_id)
                source_type = type(source_node).__name__ if source_node else ""
            if source_type:
                topo_state["added_types"][new_id] = source_type
            return

        if kind == "set_output_target":
            node_id = str(operation.get("node_id") or "")
            # AUD-5603: 拒绝指向本 plan 内已删除节点的 output target——
            # apply 后 _viewer_state.output_target_node_id 会悬空。
            if node_id and node_id in topo_state.get("removed", ()):
                raise CompositeEditError(f"node already removed in this plan: {node_id!r}")
            if node_id and node_id not in topo_state["added"] \
                    and self.graph.get_node(node_id) is None:
                raise CompositeEditError(f"node not found: {node_id!r}")
            name = operation.get("name")
            if name is not None and not isinstance(name, str):
                raise CompositeEditError("set_output_target.name must be a string")
            return

        if kind in ("group", "ungroup"):
            group_id = str(operation.get("group_id") or "")
            if not group_id:
                raise CompositeEditError("group_id is required")
            # AUD-5607: 存在性必须在 validate 阶段校验——此前 group 重名 /
            # ungroup 缺失都是 validate 全绿、apply 阶段整体回滚，脚本/AI
            # 只能拿到通用 "composite plan rolled back" 而非具名诊断。
            effective_groups = self._effective_group_ids(topo_state)
            if kind == "group":
                if group_id in effective_groups:
                    raise CompositeEditError(f"group already exists: {group_id!r}")
                node_ids = operation.get("node_ids")
                if not isinstance(node_ids, (list, tuple)) or not node_ids:
                    raise CompositeEditError("node_ids must be a non-empty list")
                for nid in node_ids:
                    nid = str(nid)
                    # AUD-5603: create_group 不校验成员存在性——引用本 plan
                    # 内已删节点会把悬空 id 永久写进组。
                    if nid in topo_state.get("removed", ()):
                        raise CompositeEditError(f"node already removed in this plan: {nid!r}")
                    if nid not in topo_state["added"] \
                            and self.graph.get_node(nid) is None:
                        raise CompositeEditError(f"node not found: {nid!r}")
                topo_state.setdefault("groups_added", set()).add(group_id)
                topo_state.setdefault("groups_removed", set()).discard(group_id)
            else:
                if group_id not in effective_groups:
                    raise CompositeEditError(f"group not found: {group_id!r}")
                topo_state.setdefault("groups_removed", set()).add(group_id)
                topo_state.setdefault("groups_added", set()).discard(group_id)
            return

        if kind == "connect" or kind == "disconnect":
            from_node, from_port, to_node, to_port = _connection_endpoints(operation)
            node = self._resolve_node_for_topology(from_node, topo_state, "from_node")
            dst = self._resolve_node_for_topology(to_node, topo_state, "to_node")
            # Plan 内新建的节点尚未入图：用注册类临时实例化校验端口语义。
            if node is None:
                node = _peek_node(topo_state["added_types"].get(from_node))
            if dst is None:
                dst = _peek_node(topo_state["added_types"].get(to_node))
            if node is not None and node.get_output_port(from_port) is None:
                raise CompositeEditError(
                    f"output port not found: {from_node}.{from_port}")
            if dst is not None and dst.get_input_port(to_port) is None:
                raise CompositeEditError(
                    f"input port not found: {to_node}.{to_port}")
            if node is not None and dst is not None:
                src_port = node.get_output_port(from_port)
                dst_port = dst.get_input_port(to_port)
                if src_port is not None and dst_port is not None \
                        and src_port.data_type != dst_port.data_type:
                    raise CompositeEditError(
                        f"port type mismatch: {src_port.data_type!r} -> {dst_port.data_type!r} "
                        f"({from_node}.{from_port} -> {to_node}.{to_port})")
            if kind == "connect":
                if from_node == to_node:
                    raise CompositeEditError("self-connection is not allowed")
                # 环检测只对图内节点有意义；plan 内新节点无既有边，
                # 其组合环由 apply 阶段 graph.connect 兜底（失败整体回滚）。
                if node is not None and dst is not None \
                        and hasattr(self.graph, "_would_create_cycle"):
                    try:
                        if self.graph._would_create_cycle(from_node, to_node):
                            raise CompositeEditError(
                                f"connection would create a cycle: {from_node} -> {to_node}")
                    except CompositeEditError:
                        raise
                    except Exception:
                        pass  # 引擎不可用时交给 apply 阶段的 graph.connect 兜底
                topo_state["connected"].add(
                    (from_node, from_port, to_node, to_port))
            else:
                in_graph = any(c.src_node_id == from_node and c.src_port_name == from_port
                               and c.dst_node_id == to_node and c.dst_port_name == to_port
                               for c in self.graph.connections)
                in_plan = (from_node, from_port, to_node, to_port) in topo_state["connected"]
                if not in_graph and not in_plan:
                    raise CompositeEditError(
                        f"connection not found: {from_node}.{from_port} -> {to_node}.{to_port}")
                topo_state["connected"].discard(
                    (from_node, from_port, to_node, to_port))
            return

        if kind == "remove_node":
            node_id = str(operation.get("node_id") or "")
            self._resolve_node_for_topology(node_id, topo_state, "node_id")
            topo_state["removed"].add(node_id)
            topo_state["added"].discard(node_id)
            topo_state["added_types"].pop(node_id, None)
            return

        if kind == "set_param":
            node_id = str(operation.get("node_id") or "")
            node = self._resolve_node_for_topology(node_id, topo_state, "node_id")
            if node is None:
                node = _peek_node(topo_state["added_types"].get(node_id))
            batch = operation.get("parameters")
            if batch is not None:
                if not isinstance(batch, Mapping) or not batch:
                    raise CompositeEditError("set_param.parameters must be a non-empty object")
                pairs = list(batch.items())
            else:
                if not str(operation.get("param_name") or ""):
                    raise CompositeEditError("set_param requires param_name or parameters")
                # AUD-5605: 缺 value 键统一在 validate 阶段拒绝（同 set_parameter）。
                if "value" not in operation:
                    raise CompositeEditError("set_param.value is required")
                pairs = [(str(operation.get("param_name") or ""), operation.get("value"))]
            for param_name, value in pairs:
                pdef = node.get_param_defs().get(str(param_name)) if node is not None else None
                if pdef is None:
                    preview = node.id if node is not None else node_id
                    raise CompositeEditError(f"parameter not found: {preview}.{param_name}")
                _validate_static_param_value(
                    pdef, value, f"set_param.{param_name}")
            return

        raise CompositeEditError(f"unsupported composite operation: {kind!r}")

    def _effective_group_ids(self, topo_state: Mapping[str, Any] | None) -> set[str]:
        """AUD-5607: 图内组 id ∪ plan 内新建组 − plan 内删除组。

        供 group/ungroup 在 validate 阶段做存在性预检（apply 阶段的
        create_group/remove_group 返回 False 只会触发整体回滚）。
        """
        groups = set(self._graph_group_ids())
        if topo_state is not None:
            groups |= set(topo_state.get("groups_added", ()) or ())
            groups -= set(topo_state.get("groups_removed", ()) or ())
        return groups

    def _graph_group_ids(self) -> set[str]:
        """查询图内已存在的组 id 集合（引擎公开 API 优先）。"""
        get_all = getattr(self.graph, "get_all_groups", None)
        if callable(get_all):
            return {str(group_id) for group_id in get_all()}
        groups = getattr(self.graph, "_node_groups", None)
        if isinstance(groups, Mapping):
            return {str(group_id) for group_id in groups}
        return set()

    def _resolve_node_for_topology(self, node_id: str,
                                   topo_state: dict[str, set],
                                   field_name: str) -> Any:
        """解析拓扑操作引用的节点：图内节点，或同一 plan 里刚 add 的节点。

        返回图内真实节点（可校验端口/参数），plan 内新建的节点返回 None
        （其端口/参数在 add_node 阶段已校验，apply 时由引擎兜底）。
        """
        if not node_id:
            raise CompositeEditError(f"{field_name} is required")
        if node_id in topo_state["removed"]:
            raise CompositeEditError(f"node already removed in this plan: {node_id!r}")
        node = self.graph.get_node(node_id)
        if node is None:
            if node_id in topo_state["added"]:
                return None
            raise CompositeEditError(f"node not found: {node_id!r}")
        return node

    # ---------------- mutation ----------------

    def _track(self, node: Any, pdef: Any) -> Any:
        data = node.keyframe_tracks.get(pdef.name)
        if data:
            return keyframe_track_from_dict({
                "param_name": pdef.name,
                "keyframes": data.get("keyframes", []),
                "value_type": pdef.ptype,
                "min_val": data.get("min_val", pdef.min_val),
                "max_val": data.get("max_val", pdef.max_val),
                "pre_extrap": data.get("pre_extrap", "constant"),
                "post_extrap": data.get("post_extrap", "constant"),
            })
        return make_keyframe_track(
            pdef.name, pdef.ptype,
            min_val=pdef.min_val, max_val=pdef.max_val)

    def _apply_operation(self, operation: Mapping[str, Any]) -> str | None:
        """执行一个已通过 validate 的操作；返回给 report 的 info 备注。"""
        kind = str(operation.get("op") or "")

        # ── C4 node-graph topology ops ──
        if kind == "add_node":
            registry = _node_registry()
            node_type = str(operation.get("node_type") or "")
            node_class = registry.get(node_type)
            if node_class is None:
                raise CompositeEditError(f"unknown node_type: {node_type!r}")
            node_id = str(operation.get("node_id") or "")
            if not node_id:
                import uuid
                node_id = f"{node_type}_{uuid.uuid4().hex[:8]}"
            if self.graph.get_node(node_id) is not None:
                raise CompositeEditError(f"node_id already exists: {node_id!r}")
            node = node_class(node_id=node_id)
            position = operation.get("position")
            if isinstance(position, (list, tuple)) and len(position) == 2:
                node.position = [float(position[0]), float(position[1])]
            parameters = operation.get("parameters")
            if isinstance(parameters, Mapping):
                for name, value in parameters.items():
                    node.set_parameter(str(name), value)
            self.graph.add_node(node)
            return f"created node {node.id} ({node_type})"
        if kind == "remove_node":
            node_id = str(operation.get("node_id") or "")
            removed = self.graph.remove_node(node_id)
            if removed is None:
                raise CompositeEditError(f"node not found: {node_id!r}")
            return None
        if kind == "connect":
            from_node, from_port, to_node, to_port = _connection_endpoints(operation)
            if not self.graph.connect(from_node, from_port, to_node, to_port):
                raise CompositeEditError(
                    f"connect failed: {from_node}.{from_port} -> {to_node}.{to_port}")
            return None
        if kind == "disconnect":
            from_node, from_port, to_node, to_port = _connection_endpoints(operation)
            if not self.graph.disconnect(from_node, from_port, to_node, to_port):
                raise CompositeEditError(
                    f"disconnect failed: {from_node}.{from_port} -> {to_node}.{to_port}")
            return None
        if kind == "set_param":
            node_id = str(operation.get("node_id") or "")
            node = self.graph.get_node(node_id)
            if node is None:
                raise CompositeEditError(f"node not found: {node_id!r}")
            batch = operation.get("parameters")
            if batch is not None:
                pairs = list(batch.items())
            else:
                pairs = [(str(operation.get("param_name") or ""), operation.get("value"))]
            for param_name, value in pairs:
                node.set_parameter(str(param_name), value)
            return
        if kind == "duplicate_node":
            # T-P1-60：克隆类型与全部参数（不克隆连接——新节点默认独立，
            # 连线由后续 connect op 显式声明，保持计划可预测）
            source_id = str(operation.get("node_id") or "")
            source = self.graph.get_node(source_id)
            if source is None:
                raise CompositeEditError(f"node not found: {source_id!r}")
            registry = _node_registry()
            # 注册表键是 node_type（"blur"）而非类名（"BlurNode"）——
            # 优先取节点自述类型，否则反查注册表
            node_type = str(getattr(source, "node_type", "") or "")
            if not node_type or node_type not in registry:
                node_type = next(
                    (nt for nt, cls in registry.items()
                     if type(source) is cls), "")
            node_class = registry.get(node_type)
            if node_class is None:
                raise CompositeEditError(f"unknown node_type: {type(source).__name__!r}")
            new_id = str(operation.get("new_node_id") or "")
            node = node_class(node_id=new_id)
            position = getattr(source, "position", None)
            if isinstance(position, (list, tuple)) and len(position) == 2:
                offset = float(operation.get("position_offset", 40.0) or 40.0)
                node.position = [float(position[0]) + offset,
                                 float(position[1]) + offset]
            for name, value in (getattr(source, "parameters", {}) or {}).items():
                try:
                    node.set_parameter(str(name), value)
                except Exception:  # noqa: BLE001 — 单参数不合法不阻塞克隆
                    pass
            self.graph.add_node(node)
            return f"duplicated {source_id} -> {new_id} ({node_type})"
        if kind == "set_output_target":
            # 镜像 composite_page._set_output_target 的 viewer 状态键
            from src.features.composite.core.output_targets import (
                normalize_output_name,
            )

            node_id = str(operation.get("node_id") or "") or None
            name = normalize_output_name(
                str(operation.get("name") or "display"))
            viewer_state = dict(getattr(self.graph, "_viewer_state", {}) or {})
            viewer_state["output_target_name"] = name
            viewer_state["output_target_node_id"] = node_id
            self.graph._viewer_state = viewer_state
            return f"output target -> {name} ({node_id or 'auto'})"
        if kind == "group":
            group_id = str(operation.get("group_id") or "")
            node_ids = [str(n) for n in operation.get("node_ids") or []]
            title = str(operation.get("title") or group_id)
            if not self.graph.create_group(group_id, node_ids,
                                           {"title": title}):
                raise CompositeEditError(f"group already exists: {group_id!r}")
            return f"group {group_id}: {len(node_ids)} nodes"
        if kind == "ungroup":
            group_id = str(operation.get("group_id") or "")
            if not self.graph.remove_group(group_id):
                raise CompositeEditError(f"group not found: {group_id!r}")
            return f"ungroup {group_id} (nodes preserved)"

        node, pdef = self._node_and_param(operation)
        if kind == "set_parameter":
            node.set_parameter(pdef.name, operation["value"])
            return
        if kind == "set_expression":
            # AUD-5601: 必须走引擎正规路径（set_node_expression 内联表达式
            # 依赖管理器注册、环检测拒绝、脏传播与 generation 递增）。此前
            # 直接调 Node 层 set_expression 会绕过全部防线：同一 plan 可建
            # 立互相引用的循环表达式，且 manager 依赖图与节点 _expressions
            # 脱节，增量渲染喂出旧纹理。
            ok, err = self.graph.set_node_expression(
                node.id, pdef.name, str(operation["expression"]))
            if not ok:
                raise CompositeEditError(
                    f"invalid expression for {node.id}.{pdef.name}"
                    + (f": {err}" if err else ""))
            return
        if kind == "remove_expression":
            # AUD-5601: 同上——经引擎注销依赖图条目并递增 generation；
            # Node 层 remove_expression 不清理 manager。
            self.graph.remove_node_expression(node.id, pdef.name)
            return
        if kind == "clear_keyframes":
            node.remove_keyframe_track(pdef.name)
            return
        track = self._track(node, pdef)
        if kind == "add_keyframe":
            track.add_keyframe(Keyframe(
                frame=int(operation["frame"]),
                value=_normalize_typed_value(pdef, operation["value"]),
                interpolation=InterpolationType(operation.get("interpolation", "linear")),
                tangent_mode=operation.get("tangent_mode", "auto"),
                handle_left=_tuple_or_none(operation.get("handle_left")),
                handle_right=_tuple_or_none(operation.get("handle_right")),
            ))
        elif kind == "set_keyframes":
            track.keyframes = []
            for data in operation["keyframes"]:
                track.add_keyframe(Keyframe.from_dict(dict(data)))
            track.pre_extrap = operation.get("pre_extrap", "constant")
            track.post_extrap = operation.get("post_extrap", "constant")
        elif kind == "remove_keyframe":
            if not track.remove_keyframe(int(operation["frame"])):
                raise CompositeEditError(f"keyframe not found: {node.id}.{pdef.name}@{operation['frame']}")
        elif kind in {"set_interpolation", "set_bezier_handles", "set_tangent_mode"}:
            keyframe = track.get_keyframe_at(int(operation["frame"]))
            if keyframe is None:
                raise CompositeEditError(f"keyframe not found: {node.id}.{pdef.name}@{operation['frame']}")
            if kind == "set_interpolation":
                keyframe.interpolation = InterpolationType(operation["interpolation"])
            elif kind == "set_tangent_mode":
                keyframe.tangent_mode = operation["tangent_mode"]
            else:
                if "handle_left" in operation:
                    keyframe.handle_left = _tuple_or_none(operation["handle_left"])
                if "handle_right" in operation:
                    keyframe.handle_right = _tuple_or_none(operation["handle_right"])
        elif kind == "set_extrapolation":
            track.pre_extrap = operation.get("pre_extrap", track.pre_extrap)
            track.post_extrap = operation.get("post_extrap", track.post_extrap)
        else:
            raise CompositeEditError(f"unsupported composite operation: {kind!r}")
        node.set_keyframe_track(pdef.name, track)


def _coerce_plan(value: CompositeEditPlan | Mapping[str, Any]) -> CompositeEditPlan:
    return value if isinstance(value, CompositeEditPlan) else CompositeEditPlan.from_dict(value)


def _node_registry() -> dict[str, Any]:
    """NODE_REGISTRY，延迟导入避免模块加载环。"""
    from src.features.composite.core.node_engine import NODE_REGISTRY

    return NODE_REGISTRY


def _peek_node(node_type: str | None) -> Any:
    """临时实例化一个注册节点类用于端口语义校验（不入图、无副作用）。"""
    if not node_type:
        return None
    try:
        return _node_registry()[node_type](node_id="_peek")
    except Exception:
        return None


def _valid_node_id(node_id: str) -> bool:
    """显式 node_id 需是非空、不含控制字符的可读标识。"""
    return bool(node_id.strip()) and len(node_id) <= 128 \
        and all(ch.isprintable() for ch in node_id)


def _connection_endpoints(operation: Mapping[str, Any]) -> tuple[str, str, str, str]:
    """解析 connect/disconnect 的两端。

    协议字段：from_node/from_port/to_node/to_port。兼容把目标节点写在
    node_id（ai.composite_plan.NodeGraphOperation 的 "target node" 语义）。
    """
    from_node = str(operation.get("from_node") or operation.get("src_node") or "")
    from_port = str(operation.get("from_port") or "")
    to_node = str(operation.get("to_node") or operation.get("dst_node")
                  or operation.get("node_id") or "")
    to_port = str(operation.get("to_port") or "")
    if not from_node or not to_node:
        raise CompositeEditError(
            "connect/disconnect requires from_node and to_node")
    if not from_port or not to_port:
        raise CompositeEditError(
            "connect/disconnect requires from_port and to_port")
    return from_node, from_port, to_node, to_port


def _validate_static_param_value(pdef: Any, value: Any, label: str) -> None:
    """校验一次静态参数写入（set_parameter / set_param / add_node.parameters）。

    AUD-5605: str/file/hdri/channel 类型此前只做 JSON-safe 检查——任意
    标量（bool/int/None）都能写进字符串类参数，下游按字符串消费时在
    求值期崩溃。三类写入路径统一走本函数。
    """
    if not _json_safe_scalar(value):
        raise CompositeEditError(f"{label} must be JSON-safe")
    if pdef.ptype in {"float", "int"}:
        _validate_value(value, pdef.min_val, pdef.max_val,
                        require_int=(pdef.ptype == "int"))
    elif pdef.ptype == "color":
        _validate_color_value(value)
    elif pdef.ptype in {"vec2", "vec3", "bool", "option"}:
        _validate_typed_value(pdef, value)
    elif pdef.ptype in {"str", "file", "hdri", "channel"}:
        if not isinstance(value, str):
            raise CompositeEditError(
                f"{label} must be a string for {pdef.ptype} parameter {pdef.name}")


def _validate_position(position: Any) -> None:
    """AUD-5606: add_node.position 必须是长度 2 的有限数值序列（或 None）。"""
    if position is None:
        return
    if (not isinstance(position, (list, tuple)) or len(position) != 2
            or any(isinstance(v, bool) or not isinstance(v, (int, float))
                   or not math.isfinite(float(v)) for v in position)):
        raise CompositeEditError(
            f"position must be two finite numbers, got {position!r}")


def _validate_frame(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value or int(value) < 0:
        raise CompositeEditError(f"frame must be a non-negative integer: {value!r}")
    return int(value)


def _validate_value(value: Any, minimum: float, maximum: float,
                    require_int: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise CompositeEditError(f"keyframe value must be a finite number: {value!r}")
    if require_int and isinstance(value, float) and float(value) != int(value):
        raise CompositeEditError(
            f"int parameter requires an integral value, got {value!r}")
    if float(value) < float(minimum) or float(value) > float(maximum):
        raise CompositeEditError(f"value {value!r} outside parameter range [{minimum}, {maximum}]")


def _validate_color_value(value: Any) -> None:
    if (not isinstance(value, (list, tuple)) or len(value) != 4
            or any(isinstance(component, bool)
                   or not isinstance(component, (int, float))
                   or not math.isfinite(float(component))
                   or not 0.0 <= float(component) <= 1.0
                   for component in value)):
        raise CompositeEditError(
            "color keyframe value must be four finite RGBA values in [0, 1]")


def _validate_typed_value(pdef: Any, value: Any) -> None:
    """Validate one keyframe/static value against its declared ParamDef type."""
    ptype = str(getattr(pdef, "ptype", "float"))
    if ptype == "color":
        _validate_color_value(value)
        return
    if ptype in {"vec2", "vec3"}:
        dimension = 3 if ptype == "vec3" else 2
        if (not isinstance(value, (list, tuple)) or len(value) != dimension
                or any(isinstance(component, bool)
                       or not isinstance(component, (int, float))
                       or not math.isfinite(float(component))
                       or not float(getattr(pdef, "min_val", -math.inf))
                       <= float(component)
                       <= float(getattr(pdef, "max_val", math.inf))
                       for component in value)):
            raise CompositeEditError(
                f"{ptype} value must contain {dimension} finite components "
                f"within [{pdef.min_val}, {pdef.max_val}]")
        return
    if ptype == "bool":
        if not isinstance(value, bool):
            raise CompositeEditError("bool value must be true or false")
        return
    if ptype == "option":
        if not isinstance(value, str):
            raise CompositeEditError("option value must be a string")
        labels = getattr(pdef, "labels", None) or []
        if labels and value not in labels:
            raise CompositeEditError(
                f"option value {value!r} is not one of {list(labels)!r}")
        return
    _validate_value(value, pdef.min_val, pdef.max_val)


def _normalize_typed_value(pdef: Any, value: Any) -> Any:
    """Normalize a validated operation value before inserting a keyframe."""
    ptype = str(getattr(pdef, "ptype", "float"))
    if ptype in {"color", "vec2", "vec3"}:
        return tuple(float(component) for component in value)
    if ptype == "int":
        return int(round(float(value)))
    if ptype == "float":
        return float(value)
    return value


def _validate_interpolation(value: Any) -> None:
    try:
        InterpolationType(str(value))
    except ValueError as exc:
        raise CompositeEditError(f"invalid interpolation: {value!r}") from exc


def _validate_tangent(value: Any) -> None:
    if str(value) not in TANGENT_MODES:
        raise CompositeEditError(f"invalid tangent_mode: {value!r}")


def _validate_extrap(value: Any, name: str) -> None:
    if str(value) not in EXTRAPOLATION_MODES:
        raise CompositeEditError(f"invalid {name}: {value!r}")


def _validate_handle(value: Any, name: str) -> None:
    if value is None:
        return
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise CompositeEditError(f"{name} must be [frame_delta, value_delta]")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)) for v in value):
        raise CompositeEditError(f"{name} must contain finite numbers")


def _tuple_or_none(value: Any) -> tuple[float, float] | None:
    return tuple(float(v) for v in value) if value is not None else None


def _json_safe_scalar(value: Any) -> bool:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return not isinstance(value, float) or math.isfinite(value)
    if isinstance(value, (list, tuple)):
        return all(_json_safe_scalar(v) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _json_safe_scalar(v) for k, v in value.items())
    return False


def _safe_copy(value: Any) -> Any:
    """只复制 JSON-safe 数据，避免把内部对象泄漏到 transport。"""
    try:
        return json.loads(json.dumps(value))
    except (TypeError, ValueError):
        return {}


def _restore_graph_in_place(graph: Any, data: dict[str, Any]) -> None:
    """在异常回滚时保留原 graph identity。仅由主线程宿主调用。"""
    from src.features.composite.core.node_engine import NodeGraph

    restored = NodeGraph.from_dict(data)
    # 保留原 graph 上的锁、渲染缓存和 expression manager，只替换持久化状态。
    for key in ("nodes", "connections", "backdrops", "_sticky_notes", "_node_groups", "_viewport_state", "_viewer_state"):
        setattr(graph, key, getattr(restored, key))
    # from_dict 把恢复节点绑定到了临时 restored graph 上：GroupNode 的
    # 子图求值引用必须指回存活对象，否则组求值永远走空缓存（直通）。
    for node in graph.nodes.values():
        if hasattr(node, "set_node_graph"):
            try:
                node.set_node_graph(graph)
            except Exception:
                pass
    # 恢复后的表达式也要重新注册进 LIVE manager（from_dict 注册进了
    # restored 的 manager，随对象一起被丢弃）——否则脏传播/环检测基于
    # apply 后的陈旧依赖图，增量渲染可能喂出旧纹理。
    live_manager = getattr(graph, "_expression_manager", None)
    if live_manager is not None:
        try:
            live_manager.clear()
        except Exception:
            pass
        for node in graph.nodes.values():
            for param_name, expression in getattr(
                    node, "_expressions", {}).items():
                try:
                    live_manager.set_expression(
                        node.id, param_name, expression)
                except Exception:
                    pass
    graph._generation += 1
    graph._topology_revision += 1
    graph._force_full_eval = True
    graph._evaluation_cache.clear()


__all__ = [
    "PROTOCOL", "CompositeEditError", "CompositeEditPlan", "CompositeDiagnostic",
    "CompositeValidationReport", "CompositeApplyResult", "CompositeEditService",
]
