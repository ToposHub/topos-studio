"""P10.1b WorkflowGraphEdit —— 冻结模型变更层（plan §10 P10）。

> :class:`WorkflowGraph` 全 ``frozen=True``（``workflow_models.py:268``），``nodes`` /
> ``edges`` 是 tuple，无任何 mutation API。画布编辑（拖拽/连线/参数）需要改 graph。
> 本服务用 ``dataclasses.replace`` 产**新** graph（原 graph 不变），复用
> ``WorkflowGraph.__post_init__`` 与 ``WorkflowEdge.__post_init__`` 的校验。

设计：

- 纯 Python 服务，**不 import Qt / moderngl / composite**（在 ai_workflow.core 层）。
- 所有方法纯函数式：``(graph, ...) -> WorkflowGraph``（新实例）；原 graph 不变。
- ``move_node`` / ``move_nodes``：改 ``WorkflowNode.ui`` 的 ``x`` / ``y``。
- ``set_node_param``：改 ``WorkflowNode.params``；value 必须 JSON-safe（否则 ValueError）。
- ``add_edge``：构造 ``WorkflowEdge`` 加进 ``edges``；重复边 / 自连 / ghost 由
  ``WorkflowEdge.__post_init__`` + 本层显式重复校验 + ``WorkflowGraph.__post_init__``
  统一拒绝（抛 ValueError）。
- ``remove_edge``：过滤 ``edges``；不存在的边 no-op（返回内容等价的新 graph）。

红线（plan §1.4.1 / §2.2）：

- 纯 Python；不引入 ``moderngl`` / ``composite.core`` / Qt（源码扫描）。
- 不直接写 timeline（只改 WorkflowGraph）。
- 冻结模型不变：不修改原 graph 的任何字段。
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Dict, Tuple

from src.features.ai_workflow.core.workflow_models import (
    PortRef, WorkflowEdge, WorkflowGraph, WorkflowNode,
)


def _is_json_safe(value: Any) -> bool:
    """递归判断 value 是否 JSON-safe（对齐 ``JsonValue`` 联合类型）。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        return True
    if isinstance(value, (list, tuple)):
        return all(_is_json_safe(v) for v in value)
    if isinstance(value, dict):
        return all(
            isinstance(k, str) and _is_json_safe(v)
            for k, v in value.items()
        )
    return False


class WorkflowGraphEdit:
    """冻结 WorkflowGraph 的纯函数式变更层。

    无状态；每个方法返回新 graph（原 graph 不变）。非法操作抛 ``KeyError``（节点
    不存在）/ ``ValueError``（重复边 / 自连 / ghost / 非 JSON-safe value）。
    """

    # ------------------------------------------------------------------ #
    # 节点增删
    # ------------------------------------------------------------------ #

    def add_node(self, graph: WorkflowGraph, node: WorkflowNode) -> WorkflowGraph:
        """加一个节点。重复 node_id 抛 ValueError（靠 WorkflowGraph.__post_init__）。"""
        existing = {n.node_id for n in graph.nodes}
        if node.node_id in existing:
            raise ValueError(f"duplicate node_id: {node.node_id!r}")
        new_nodes = graph.nodes + (node,)
        return replace(graph, nodes=new_nodes)

    # ------------------------------------------------------------------ #
    # 节点位置
    # ------------------------------------------------------------------ #

    def move_node(
        self, graph: WorkflowGraph, node_id: str,
        x: float, y: float,
    ) -> WorkflowGraph:
        """移动单个节点（改 ``ui.x`` / ``ui.y``）。"""
        node = graph.require_node(node_id)  # KeyError if missing
        new_ui: Dict[str, Any] = dict(node.ui)
        new_ui["x"] = float(x)
        new_ui["y"] = float(y)
        new_node = replace(node, ui=new_ui)
        return self._replace_node(graph, node_id, new_node)

    def move_nodes(
        self, graph: WorkflowGraph,
        moves: Dict[str, Tuple[float, float]],
    ) -> WorkflowGraph:
        """批量移动多节点（一次重建 nodes tuple）。"""
        if not moves:
            # 无移动：仍返回新实例（保持"返回新 graph"契约），内容等价。
            return replace(graph, nodes=tuple(graph.nodes))
        # 先校验所有 node_id 存在（fail-fast，避免部分应用）。
        for nid in moves:
            graph.require_node(nid)
        move_map = {nid: (float(x), float(y)) for nid, (x, y) in moves.items()}
        new_nodes = []
        for n in graph.nodes:
            if n.node_id in move_map:
                nx, ny = move_map[n.node_id]
                new_ui = dict(n.ui)
                new_ui["x"] = nx
                new_ui["y"] = ny
                new_nodes.append(replace(n, ui=new_ui))
            else:
                new_nodes.append(n)
        return replace(graph, nodes=tuple(new_nodes))

    # ------------------------------------------------------------------ #
    # 节点参数
    # ------------------------------------------------------------------ #

    def set_node_param(
        self, graph: WorkflowGraph, node_id: str,
        key: str, value: Any,
    ) -> WorkflowGraph:
        """设置节点参数（改 ``params[key]``）。value 必须 JSON-safe。"""
        if not _is_json_safe(value):
            raise ValueError(
                f"param value must be JSON-safe (got {type(value).__name__})"
            )
        node = graph.require_node(node_id)
        new_params: Dict[str, Any] = dict(node.params)
        new_params[key] = value
        new_node = replace(node, params=new_params)
        return self._replace_node(graph, node_id, new_node)

    # ------------------------------------------------------------------ #
    # 边
    # ------------------------------------------------------------------ #

    def add_edge(
        self, graph: WorkflowGraph,
        from_node: str, from_port: str,
        to_node: str, to_port: str,
    ) -> WorkflowGraph:
        """加一条边。重复边 / 自连 / ghost 抛 ValueError。"""
        # 显式重复校验（model 的 __post_init__ 不查重复边）。
        for e in graph.edges:
            if (
                e.from_port.node_id == from_node
                and e.from_port.port == from_port
                and e.to_port.node_id == to_node
                and e.to_port.port == to_port
            ):
                raise ValueError(
                    f"duplicate edge: {from_node}:{from_port} -> {to_node}:{to_port}"
                )
        edge = WorkflowEdge(
            from_port=PortRef(node_id=from_node, port=from_port),
            to_port=PortRef(node_id=to_node, port=to_port),
        )  # WorkflowEdge.__post_init__ 查自连
        new_edges = graph.edges + (edge,)
        # WorkflowGraph.__post_init__ 查 ghost node（node 不存在）
        return replace(graph, edges=new_edges)

    def remove_edge(
        self, graph: WorkflowGraph,
        from_node: str, from_port: str,
        to_node: str, to_port: str,
    ) -> WorkflowGraph:
        """移除一条边；不存在的边 no-op（返回内容等价的新 graph）。"""
        remaining = tuple(
            e for e in graph.edges
            if not (
                e.from_port.node_id == from_node
                and e.from_port.port == from_port
                and e.to_port.node_id == to_node
                and e.to_port.port == to_port
            )
        )
        return replace(graph, edges=remaining)

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    @staticmethod
    def _replace_node(
        graph: WorkflowGraph, node_id: str, new_node: WorkflowNode,
    ) -> WorkflowGraph:
        """用 new_node 替换 graph 中同 node_id 的节点。"""
        new_nodes = tuple(
            new_node if n.node_id == node_id else n for n in graph.nodes
        )
        return replace(graph, nodes=new_nodes)


__all__ = ["WorkflowGraphEdit"]
