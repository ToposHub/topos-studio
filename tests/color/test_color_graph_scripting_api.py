"""B15 脚本 API 测试 —— 事务原子性/revision/兼容视图/能力可见。

验收：batch 100 编辑；参数越界/成环/stale revision 回滚；旧 layers
脚本对可投影图只读、复杂 DAG 显式错误；脚本与 UI 同 service。
"""

from __future__ import annotations

import pytest

from src.features.color.core.color_graph_migration import migrate_legacy_stack
from src.features.color.core.color_graph_service import ColorGraphService
from src.features.scripting.color_graph_api import (
    ColorGraphScriptAPI,
    GraphApiError,
)
from src.features.edit.core.project import EditProject
from tests.color.fixtures.color_graph import f03_layer_stack
from tests.color.test_color_graph_migration import _stack_with_layers


def _api_with_stack():
    project, stack_id = EditProject.create(name="api").create_color_stack(
        asset_id="a1", name="s")
    stack = project.color_stacks[stack_id]
    graph = migrate_legacy_stack(
        stack.with_layers([l.to_dict(None) for l in f03_layer_stack()]))
    holder = {"stacks": {stack_id: stack.with_color_graph(graph)}}

    api = ColorGraphScriptAPI(
        get_stack=lambda sid: holder["stacks"].get(sid),
        set_stack=lambda sid, s: holder["stacks"].__setitem__(sid, s),
        service=ColorGraphService(id_factory=_counter_factory()))
    return api, holder, stack_id, graph


def _counter_factory():
    state = {"n": 0}
    def factory():
        state["n"] += 1
        return f"api-{state['n']}"
    return factory


class TestReadOnlyApi:
    def test_get_graph_and_validate(self):
        api, holder, stack_id, graph = _api_with_stack()
        assert api.get_graph(stack_id).graph_id == graph.graph_id
        report = api.validate_graph(stack_id)
        assert report["valid"] and report["nodes"] == len(graph.nodes)

    def test_inspect_capabilities_lists_backend_status(self):
        api, _, stack_id, _ = _api_with_stack()
        caps = api.inspect_capabilities(stack_id)
        assert caps["has_graph"]
        # 能力可见：本图出现的算子逐个带后端状态
        for node_type in caps["node_types"]:
            assert node_type in caps["backend_gl"]
        assert caps["backend_gl"]["corrector"] == "exact"
        assert caps["backend_gl"]["source"] == "exact"

    def test_missing_stack_raises(self):
        api, *_ = _api_with_stack()
        with pytest.raises(GraphApiError) as ei:
            api.get_graph("ghost")
        assert ei.value.code == "stack_missing"


class TestTransactionAtomicity:
    def test_batch_of_100_edits_one_result(self):
        api, holder, stack_id, graph = _api_with_stack()
        edits = [{"command": "insert_corrector",
                  "arguments": {"active_node_id": None, "before": False}}
                 for _ in range(100)]
        outcome = api.apply_graph_edits(stack_id, edits)
        assert outcome.ok
        assert len(outcome.applied) == 100
        assert outcome.revision == graph.graph_revision + 100

    def test_failed_edit_rolls_back_whole_batch(self):
        api, holder, stack_id, graph = _api_with_stack()
        edits = [
            {"command": "insert_corrector", "arguments": {}},
            {"command": "connect_ports", "arguments": {
                "src_node": "ghost", "src_port": "image",
                "dst_node": "ghost2", "dst_port": "image"}},   # 失败
            {"command": "add_alpha_output", "arguments": {}},
        ]
        outcome = api.apply_graph_edits(stack_id, edits)
        assert not outcome.ok
        assert outcome.code == "node_not_found"
        # 整批回滚：栈上图保持原样（第一条的插入也没落）
        assert (holder["stacks"][stack_id].color_graph.to_dict()
                == graph.to_dict())

    def test_stale_revision_rejected_without_changes(self):
        api, holder, stack_id, graph = _api_with_stack()
        outcome = api.apply_graph_edits(
            stack_id, [{"command": "add_alpha_output", "arguments": {}}],
            expected_revision=graph.graph_revision + 42)
        assert not outcome.ok and outcome.code == "stale_revision"
        assert (holder["stacks"][stack_id].color_graph.to_dict()
                == graph.to_dict())

    def test_matching_revision_accepted(self):
        api, _, stack_id, graph = _api_with_stack()
        outcome = api.apply_graph_edits(
            stack_id, [{"command": "add_alpha_output", "arguments": {}}],
            expected_revision=graph.graph_revision)
        assert outcome.ok


class TestLegacyCompatibility:
    def test_projectable_graph_gets_readonly_layers_view(self):
        api, _, stack_id, _ = _api_with_stack()
        layers = api.legacy_layers_view(stack_id)
        assert len(layers) == 3
        assert layers[0]["name"] == "Print Warmer"

    def test_complex_dag_raises_explicit_error(self):
        api, holder, stack_id, graph = _api_with_stack()
        service = ColorGraphService(id_factory=_counter_factory())
        grown = service.apply_intent(graph, "add_parallel_branch", {})
        holder["stacks"][stack_id] = holder["stacks"][stack_id].with_color_graph(
            grown.graph)
        with pytest.raises(GraphApiError) as ei:
            api.legacy_layers_view(stack_id)
        assert ei.value.code == "not_projectable"

    def test_unknown_command_reports_missing(self):
        api, _, stack_id, _ = _api_with_stack()
        outcome = api.apply_graph_edits(
            stack_id, [{"command": "nonexistent", "arguments": {}}])
        assert not outcome.ok and outcome.code == "unknown_command"


class TestSameServiceAsUi:
    def test_script_edits_match_ui_command_semantics(self):
        """脚本与 UI 走同一 service：同参数产生同 revision 增量。"""
        api, holder, stack_id, graph = _api_with_stack()
        ui_result = ColorGraphService(
            id_factory=_counter_factory()).apply_intent(
                graph, "add_alpha_output", {})
        assert ui_result.ok
        script_outcome = api.apply_graph_edits(
            stack_id, [{"command": "add_alpha_output", "arguments": {}}])
        assert script_outcome.ok
        # 两者 revision 增量一致（+1）且产物结构同类
        assert (script_outcome.graph.graph_revision
                == graph.graph_revision + 1)
        assert ui_result.graph.graph_revision == graph.graph_revision + 1
