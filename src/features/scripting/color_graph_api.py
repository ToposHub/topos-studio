"""调色节点图公开脚本 API（N11，B15）。

带 revision/transaction 的图操作入口；不暴露 UI draft / GL 对象 /
内部渲染器。与 UI 同一 service/command 语义（一个 undo 单元）；
``expected_revision`` 乐观并发；批量原子（任一失败全回滚）。

旧 layers API（E28）不在本文件改写：可线性投影图经
``legacy_layers_view`` 提供只读兼容视图；复杂 DAG 返回显式错误。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from src.features.color.core.color_graph_migration import (
    LegacyMigrationError,
    can_project_to_legacy_layers,
    migrate_legacy_stack,
    project_legacy_layers,
)
from src.features.color.core.color_graph_service import ColorGraphService
from src.shared.project.color_graph import (
    ColorGraphData,
    GraphSchemaError,
)


class GraphApiError(ValueError):
    """脚本 API 错误（stable code + message）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code


@dataclass(frozen=True)
class GraphEditOutcome:
    """一次脚本事务的结果。"""

    ok: bool
    stack_id: str
    graph: ColorGraphData | None = None
    revision: int = -1
    applied: tuple[str, ...] = ()
    code: str | None = None
    message: str | None = None
    id_mapping: Mapping[str, str] | None = None


class ColorGraphScriptAPI:
    """面向脚本/AI 的图事务入口（纯数据；宿主注入栈存取）。"""

    def __init__(self, get_stack, set_stack,
                 service: ColorGraphService | None = None) -> None:
        """:param get_stack: stack_id → ColorStackData|None；
        :param set_stack: (stack_id, ColorStackData) → None（宿主负责
        走 command 栈；本类只产出结果栈）。"""
        self._get_stack = get_stack
        self._set_stack = set_stack
        self._service = service or ColorGraphService()

    # ── 只读 ──

    def get_graph(self, stack_id: str) -> ColorGraphData | None:
        stack = self._get_stack(stack_id)
        if stack is None:
            raise GraphApiError("stack_missing", f"栈不存在: {stack_id}")
        return getattr(stack, "color_graph", None)

    def validate_graph(self, stack_id: str) -> dict[str, Any]:
        graph = self.get_graph(stack_id)
        if graph is None:
            return {"valid": False, "code": "no_graph",
                    "errors": ["栈尚未建图"]}
        errors = graph.validate()
        return {"valid": not errors,
                "code": None if not errors else errors[0].code,
                "errors": [f"{e.code}: {e}" for e in errors],
                "revision": graph.graph_revision,
                "nodes": len(graph.nodes)}

    def inspect_capabilities(self, stack_id: str) -> dict[str, Any]:
        from src.features.color.core.color_graph_compiler import (
            GL_CAPABILITY_CATALOG,
        )
        graph = self.get_graph(stack_id)
        if graph is None:
            return {"has_graph": False}
        ops = sorted({n.node_type for n in graph.nodes})
        return {"has_graph": True,
                "node_types": ops,
                "backend_gl": {op: GL_CAPABILITY_CATALOG.get(op,
                                                             "unsupported")
                               for op in ops}}

    def legacy_layers_view(self, stack_id: str) -> list[dict[str, Any]]:
        """旧 layers 兼容视图（只读）；复杂 DAG 显式报错不压平。"""
        graph = self.get_graph(stack_id)
        if graph is None:
            raise GraphApiError("no_graph", "栈尚未建图")
        if not can_project_to_legacy_layers(graph):
            raise GraphApiError(
                "not_projectable",
                "图含分支/key/共享/compound——旧 layers API 不可表达；"
                "使用图 API（apply_graph_edits）")
        return project_legacy_layers(graph)

    # ── 事务 ──

    def apply_graph_edits(
        self, stack_id: str, edits: Sequence[Mapping[str, Any]], *,
        expected_revision: int | None = None,
        ensure_graph: bool = True,
    ) -> GraphEditOutcome:
        """原子批事务：逐条 service 操作；任一失败整批丢弃（旧栈不动）。

        edit 形态：{"command": str, "arguments": {...}}（command 即
        B01 契约 command 名）。
        """
        stack = self._get_stack(stack_id)
        if stack is None:
            return GraphEditOutcome(False, stack_id, code="stack_missing",
                                    message=f"栈不存在: {stack_id}")
        graph = getattr(stack, "color_graph", None)
        if graph is None:
            if not ensure_graph:
                return GraphEditOutcome(False, stack_id, code="no_graph",
                                        message="栈尚未建图")
            try:
                graph = migrate_legacy_stack(stack)
            except LegacyMigrationError as exc:
                return GraphEditOutcome(False, stack_id, code=exc.code,
                                        message=str(exc))
        if (expected_revision is not None
                and graph.graph_revision != expected_revision):
            return GraphEditOutcome(
                False, stack_id, code="stale_revision",
                message=f"期望 {expected_revision} != 当前 "
                        f"{graph.graph_revision}")

        current = graph
        applied: list[str] = []
        mapping: dict[str, str] = {}
        for edit in edits:
            command = edit.get("command")
            arguments = dict(edit.get("arguments") or {})
            if not command:
                return GraphEditOutcome(
                    False, stack_id, graph=graph, applied=tuple(applied),
                    code="missing_command", message="edit 缺 command")
            result = self._service.apply_intent(current, command, arguments)
            if not result.ok:
                # 原子回滚：整批丢弃（current 仍是原 graph——service
                # 失败不改图），旧栈不动
                return GraphEditOutcome(
                    False, stack_id, graph=graph, applied=tuple(applied),
                    code=result.primary_code,
                    message="; ".join(str(d) for d in result.diagnostics[:2]),
                    id_mapping=mapping)
            current = result.graph
            applied.append(str(command))
            mapping.update(dict(result.id_mapping))

        self._set_stack(stack_id, stack.with_color_graph(current))
        return GraphEditOutcome(True, stack_id, graph=current,
                                revision=current.graph_revision,
                                applied=tuple(applied), id_mapping=mapping)
