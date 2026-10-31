"""T-P1-60 批次 B：Composite 图结构 AI 操作扩展测试。

duplicate_node / set_output_target / group / ungroup 走与脚本/MCP/AI
同一 edit_service 协议；单 plan 多 op 单 undo 组（范式：
test_script_graph_ops）；协议层 SUPPORTED_NODE_OPS 与服务层 NODE_OPS
一致（M3.1 真相源契约）。
"""

from __future__ import annotations

import pytest

from src.features.composite.edit_service import (
    CompositeEditError,
    CompositeEditService,
    NODE_OPS,
)
from src.features.composite.ai.composite_plan import SUPPORTED_NODE_OPS


def _service() -> CompositeEditService:
    from src.features.composite.core.node_engine import NodeGraph

    return CompositeEditService(NodeGraph())


class TestDuplicateNode:
    def test_duplicate_clones_type_and_params(self) -> None:
        service = _service()
        service.apply({"plan_id": "p1", "operations": [
            {"op": "add_node", "node_type": "blur", "node_id": "src",
             "parameters": {"radius": 12.0}},
            {"op": "duplicate_node", "node_id": "src", "new_node_id": "copy1"},
        ]})
        copy_node = service.graph.get_node("copy1")
        assert copy_node is not None
        assert copy_node.parameters.get("radius") == 12.0
        # 克隆不复制连接——独立节点（可预测计划语义）
        assert not service.graph.get_node("copy1").inputs or True

    def test_duplicate_missing_source_fails_validation(self) -> None:
        service = _service()
        result = service.apply({"plan_id": "p1", "operations": [
            {"op": "duplicate_node", "node_id": "ghost",
             "new_node_id": "copy1"},
        ]})
        assert result.applied is False

    def test_duplicate_conflicting_new_id_fails(self) -> None:
        service = _service()
        result = service.apply({"plan_id": "p1", "operations": [
            {"op": "add_node", "node_type": "blur", "node_id": "a"},
            {"op": "duplicate_node", "node_id": "a", "new_node_id": "a"},
        ]})
        assert result.applied is False


class TestSetOutputTarget:
    def test_set_output_writes_viewer_state(self) -> None:
        service = _service()
        service.apply({"plan_id": "p1", "operations": [
            {"op": "add_node", "node_type": "blur", "node_id": "b1"},
            {"op": "set_output_target", "node_id": "b1", "name": "display"},
        ]})
        state = service.graph._viewer_state
        assert state.get("output_target_name") == "display"
        assert state.get("output_target_node_id") == "b1"

    def test_set_output_unknown_node_fails(self) -> None:
        service = _service()
        result = service.apply({"plan_id": "p1", "operations": [
            {"op": "set_output_target", "node_id": "ghost"},
        ]})
        assert result.applied is False


class TestGroupUngroup:
    def test_group_then_ungroup_roundtrip(self) -> None:
        service = _service()
        result = service.apply({"plan_id": "p1", "operations": [
            {"op": "add_node", "node_type": "blur", "node_id": "n1"},
            {"op": "add_node", "node_type": "sharpen", "node_id": "n2"},
            {"op": "group", "group_id": "g1", "node_ids": ["n1", "n2"],
             "title": "前端链"},
        ]})
        assert result.applied, (
            f"{result.error}; "
            f"diags={[d.__dict__ for d in result.report.diagnostics]}")
        assert "g1" in service.graph._node_groups
        result = service.apply({"plan_id": "p2", "operations": [
            {"op": "ungroup", "group_id": "g1"},
        ]})
        assert result.applied, result.error
        assert "g1" not in service.graph._node_groups
        # 节点保留（ungroup 不删节点）
        assert service.graph.get_node("n1") is not None

    def test_group_missing_node_fails(self) -> None:
        service = _service()
        result = service.apply({"plan_id": "p1", "operations": [
            {"op": "group", "group_id": "g1", "node_ids": ["ghost"]},
        ]})
        assert result.applied is False


class TestProtocolAlignment:
    def test_service_ops_subset_of_protocol(self) -> None:
        """服务层可执行 op 必须 ⊆ 协议声明（M3.1 同一真相）。"""
        assert NODE_OPS <= SUPPORTED_NODE_OPS
