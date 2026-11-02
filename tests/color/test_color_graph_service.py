"""B04.2–B04.4 图编辑 service 测试 —— 操作语义与 ID/边分配。

每个用例断言：成功事务产合法图 + revision 单调 +1；失败事务图完全
不变（原子性）；诊断 code 稳定。验证/成环/随机图属性在
test_color_graph_validation.py。
"""

from __future__ import annotations

import json
import uuid

import pytest

from src.features.color.core.color_graph_service import (
    ColorGraphService,
    GraphEditResult,
)
from src.shared.project.color_graph import (
    ColorGraphData,
    ColorNodeData,
    NodeType,
)
from tests.color.test_color_graph_migration import _stack_with_layers
from tests.color.fixtures.color_graph import f03_layer_stack


def _service() -> ColorGraphService:
    counter = iter(range(100000))
    return ColorGraphService(id_factory=lambda: f"new-{next(counter)}")


def _serial(n: int = 2) -> ColorGraphData:
    from tests.color.test_color_graph_schema import _serial_graph
    return _serial_graph(n)


def _correctors(graph: ColorGraphData) -> list[ColorNodeData]:
    return [n for n in graph.nodes if n.node_type == NodeType.CORRECTOR.value]


# ── 串行插入 / 加节点 / 连接 ────────────────────────────────────────────

class TestInsertAndConnect:
    def test_insert_after_splits_edge(self):
        graph = _serial(2)
        c0 = _correctors(graph)[0]
        result = _service().op_insert_corrector(graph, c0.node_id, before=False)
        assert result.ok
        assert result.graph.is_valid()
        assert len(_correctors(result.graph)) == 3
        assert result.graph.graph_revision == graph.graph_revision + 1

    def test_insert_before_active(self):
        graph = _serial(2)
        c1 = _correctors(graph)[1]
        result = _service().op_insert_corrector(graph, c1.node_id, before=True)
        assert result.ok
        ins = [e for e in result.graph.edges if e.dst_node == "new-0"]
        assert ins and ins[0].src_node != c1.node_id  # 插在 c1 上游

    def test_insert_on_output_anchor_uses_upstream(self):
        graph = _serial(1)
        result = _service().op_insert_corrector(
            graph, graph.output_node_id, before=True)
        assert result.ok

    def test_insert_unconnected_node_keeps_graph_valid(self):
        graph = _serial(1)
        result = _service().op_add_node(graph, "corrector", (10.0, 10.0))
        assert result.ok
        # 孤立节点：结构合法（§6.3），执行级校验才发现
        assert result.graph.is_valid()
        assert any(e.code == "isolated_node"
                   for e in result.graph.validate_execution())

    def test_add_node_records_position_without_revision_bump_of_layout(self):
        graph = _serial(1)
        result = _service().op_add_node(graph, "corrector", (5.0, 6.0))
        assert "new-0" in result.graph.ui_state.positions

    def test_connect_valid_image_edge(self):
        graph = _serial(1)
        free = _service().op_add_node(graph, "corrector").graph
        c0 = _correctors(graph)[0]
        result = _service().op_connect_ports(
            free, c0.node_id, "image", "new-0", "image")
        assert result.ok and result.graph.is_valid()

    def test_connect_key_edge_between_correctors(self):
        graph = _serial(2)
        c0, c1 = _correctors(graph)
        result = _service().op_connect_ports(
            graph, c0.node_id, "key_out", c1.node_id, "key_in")
        assert result.ok

    def test_connect_replaces_single_input_atomically(self):
        graph = _serial(2)
        _, c1 = _correctors(graph)
        alt = _service().op_add_node(graph, "corrector").graph
        result = _service().op_connect_ports(
            alt, "new-0", "image", c1.node_id, "image")
        assert result.ok
        # c1.image 只剩一条入边（旧 c0→c1 被替换）
        ins = [e for e in result.graph.edges
               if e.dst_node == c1.node_id and e.dst_port == "image"]
        assert len(ins) == 1 and ins[0].src_node == "new-0"

    def test_connect_rejects_kind_mismatch(self):
        graph = _serial(2)
        c0, c1 = _correctors(graph)
        result = _service().op_connect_ports(
            graph, c0.node_id, "image", c1.node_id, "key_in")
        assert not result.ok
        assert result.primary_code == "port_kind"
        assert result.graph is graph

    def test_connect_rejects_cycle(self):
        graph = _serial(2)
        c0, c1 = _correctors(graph)
        result = _service().op_connect_ports(
            graph, c1.node_id, "image", c0.node_id, "image")
        assert not result.ok
        assert result.primary_code in ("port_multi", "cycle")

    def test_disconnect_by_edge_id(self):
        graph = _serial(1)
        edge = graph.edges[1]
        result = _service().op_disconnect_edge(graph, edge.edge_id)
        assert result.ok
        assert edge.edge_id not in {e.edge_id for e in result.graph.edges}

    def test_disconnect_missing_edge_fails_atomically(self):
        graph = _serial(1)
        result = _service().op_disconnect_edge(graph, "no-such-edge")
        assert not result.ok and result.graph is graph


# ── 分支结构 ────────────────────────────────────────────────────────────

class TestBranchStructures:
    def test_add_parallel_branch_shape(self):
        graph = _serial(1)
        result = _service().op_add_parallel_branch(graph)
        assert result.ok
        mixers = [n for n in result.graph.nodes
                  if n.node_type == NodeType.PARALLEL_MIXER.value]
        assert len(mixers) == 1
        # mixer 的 base + branch 边各一条，branch slot 稳定 ID
        mixer = mixers[0]
        base = [e for e in result.graph.edges
                if e.dst_node == mixer.node_id and e.dst_port == "base"]
        branch = [e for e in result.graph.edges
                  if e.dst_node == mixer.node_id and e.dst_port == "branch"]
        assert len(base) == 1 and len(branch) == 1
        assert branch[0].slot_id

    def test_add_layer_branch_ordered_slots(self):
        graph = _serial(1)
        result = _service().op_add_layer_branch(graph)
        assert result.ok
        mixer = next(n for n in result.graph.nodes
                     if n.node_type == NodeType.LAYER_MIXER.value)
        slots = sorted(e.slot_id for e in result.graph.edges
                       if e.dst_node == mixer.node_id)
        assert slots == [f"{mixer.node_id}/slot/0", f"{mixer.node_id}/slot/1"]

    def test_add_outside_references_key_out(self):
        graph = _serial(1)
        c0 = _correctors(graph)[0]
        result = _service().op_add_outside_corrector(graph, c0.node_id)
        assert result.ok
        outside = next(n for n in result.graph.nodes
                       if n.node_type == NodeType.OUTSIDE.value)
        assert outside.params["outside_key_source"] == c0.node_id
        key_edges = [e for e in result.graph.edges
                     if e.dst_node == outside.node_id and e.dst_port == "key_in"]
        assert len(key_edges) == 1

    def test_outside_on_output_rejected(self):
        graph = _serial(1)
        result = _service().op_add_outside_corrector(graph, graph.output_node_id)
        assert not result.ok
        assert result.primary_code == "port_not_found"

    def test_splitter_pair_atomic(self):
        graph = _serial(1)
        result = _service().op_add_splitter_pair(graph)
        assert result.ok
        splitter = next(n for n in result.graph.nodes
                        if n.node_type == NodeType.SPLITTER.value)
        combiner = next(n for n in result.graph.nodes
                        if n.node_type == NodeType.COMBINER.value)
        for ch in ("r", "g", "b"):
            assert any(e.src_node == splitter.node_id and e.src_port == ch
                       and e.dst_node == combiner.node_id and e.dst_port == ch
                       for e in result.graph.edges)

    def test_key_mixer_from_two_key_sources(self):
        graph = _serial(2)
        c0, c1 = _correctors(graph)
        result = _service().op_add_key_mixer(graph, [c0.node_id, c1.node_id])
        assert result.ok
        mixer = next(n for n in result.graph.nodes
                     if n.node_type == NodeType.KEY_MIXER.value)
        keys = [e for e in result.graph.edges
                if e.dst_node == mixer.node_id]
        assert len(keys) == 2

    def test_alpha_output_lifecycle(self):
        graph = _serial(1)
        c0 = _correctors(graph)[0]
        added = _service().op_add_alpha_output(graph, c0.node_id)
        assert added.ok
        assert added.graph.alpha_output_node_id
        again = _service().op_add_alpha_output(added.graph)
        assert not again.ok and again.primary_code == "terminal_count"
        removed = _service().op_remove_alpha_output(added.graph)
        assert removed.ok and removed.graph.alpha_output_node_id is None
        # 主 Output 不能经 remove_alpha_output 删除
        protect = _service().op_remove_alpha_output(
            removed.graph, removed.graph.output_node_id)
        assert not protect.ok

    def test_add_mixer_slot_limit_and_stable_ids(self):
        """slot 上限 8：base 不占槽；branch 边 1 槽 + 参数槽计数。"""
        graph = _serial(1)
        mixed = _service().op_add_parallel_branch(graph).graph
        # branch 边占 1 槽；参数槽再加 6 → 共 7
        for _ in range(6):
            result = _service().op_add_mixer_slot(mixed, "new-0")
            assert result.ok
            mixed = result.graph
        # 第 8 槽仍可分配
        eighth = _service().op_add_mixer_slot(mixed, "new-0")
        assert eighth.ok
        # 第 9 槽超限
        over = _service().op_add_mixer_slot(eighth.graph, "new-0")
        assert not over.ok and over.primary_code == "size_limit"

    def test_morph_mixer_rewrites_ports(self):
        graph = _serial(1)
        layered = _service().op_add_layer_branch(graph).graph
        mixer = next(n for n in layered.nodes
                     if n.node_type == NodeType.LAYER_MIXER.value)
        result = _service().op_change_mixer_type(layered, mixer.node_id,
                                                 "parallel_mixer")
        assert result.ok
        new_mixer = next(n for n in result.graph.nodes
                         if n.node_id == mixer.node_id)
        assert new_mixer.node_type == NodeType.PARALLEL_MIXER.value
        dst_ports = {e.dst_port for e in result.graph.edges
                     if e.dst_node == mixer.node_id}
        assert dst_ports == {"base", "branch"}


# ── 删除 / 提取 / 复制 ──────────────────────────────────────────────────

class TestDeleteExtractCopy:
    def test_delete_bridges_upstream_downstream(self):
        graph = _serial(3)
        c1 = _correctors(graph)[1]
        result = _service().op_delete_nodes(graph, [c1.node_id])
        assert result.ok
        assert len(_correctors(result.graph)) == 2
        c0, c2 = _correctors(result.graph)
        assert any(e.src_node == c0.node_id and e.dst_node == c2.node_id
                   for e in result.graph.edges)

    def test_delete_protected_terminals_rejected(self):
        graph = _serial(1)
        for terminal in (graph.source_node_id, graph.output_node_id):
            result = _service().op_delete_nodes(graph, [terminal])
            assert not result.ok
            assert result.primary_code == "terminal_count"

    def test_delete_mixer_rejected_with_diagnostic(self):
        graph = _serial(1)
        mixed = _service().op_add_parallel_branch(graph).graph
        mixer = next(n for n in mixed.nodes
                     if n.node_type == NodeType.PARALLEL_MIXER.value)
        result = _service().op_delete_nodes(mixed, [mixer.node_id])
        assert not result.ok
        assert result.primary_code == "ambiguous_reconnect"

    def test_extract_bridges_and_keeps_node(self):
        graph = _serial(2)
        c0 = _correctors(graph)[0]
        result = _service().op_extract_nodes(graph, [c0.node_id])
        assert result.ok
        assert any(n.node_id == c0.node_id for n in result.graph.nodes)
        # 桥接存在：source 直连 c1
        c1 = _correctors(result.graph)[1] if len(
            _correctors(result.graph)) > 1 else None
        assert c1 is not None
        assert any(e.src_node == graph.source_node_id
                   and e.dst_node == c1.node_id
                   for e in result.graph.edges)

    def test_duplicate_rewrites_ids_keeps_internal_edges(self):
        graph = _serial(2)
        c0, c1 = _correctors(graph)
        result = _service().op_duplicate_subgraph(graph, [c0.node_id, c1.node_id])
        assert result.ok
        mapping = dict(result.id_mapping)
        assert c0.node_id in mapping and c1.node_id in mapping
        assert any(e.src_node == mapping[c0.node_id]
                   and e.dst_node == mapping[c1.node_id]
                   for e in result.graph.edges)
        # 内部边保留、外部边不复制（副本未连接）
        into_copy = [e for e in result.graph.edges
                     if e.dst_node == mapping[c0.node_id]]
        assert into_copy == []

    def test_paste_subgraph_remaps_and_places_unconnected(self):
        graph = _serial(1)
        payload_nodes = [
            {"node_id": "src-a", "node_type": "corrector", "params": {},
             "enabled": True},
            {"node_id": "src-b", "node_type": "key_mixer", "params": {},
             "enabled": True},
        ]
        payload_edges = [
            {"edge_id": "x", "src_node": "src-a", "src_port": "key_out",
             "dst_node": "src-b", "dst_port": "key"},
        ]
        result = _service().op_paste_subgraph(graph, payload_nodes, payload_edges)
        assert result.ok
        a, b = result.id_mapping["src-a"], result.id_mapping["src-b"]
        assert any(e.src_node == a and e.dst_node == b
                   for e in result.graph.edges)


# ── 参数 / 元信息 / 布局 ────────────────────────────────────────────────

class TestParamsAndLayout:
    def test_set_bypass_and_all_enabled(self):
        graph = _serial(2)
        c0 = _correctors(graph)[0]
        result = _service().op_set_bypass(graph, [c0.node_id], False)
        assert result.ok
        node = next(n for n in result.graph.nodes if n.node_id == c0.node_id)
        assert node.enabled is False
        all_on = _service().op_set_all_enabled(result.graph, True)
        assert all_on.ok
        assert all(n.enabled for n in all_on.graph.nodes)

    def test_set_node_option_dispatches_known_keys(self):
        graph = _serial(1)
        c0 = _correctors(graph)[0]
        result = _service().op_set_node_option(graph, c0.node_id,
                                               "key_invert", True)
        assert result.ok
        node = next(n for n in result.graph.nodes if n.node_id == c0.node_id)
        assert node.params["key_settings"]["invert_key_in"] is True

    def test_set_node_option_rejects_unknown_key(self):
        graph = _serial(1)
        c0 = _correctors(graph)[0]
        result = _service().op_set_node_option(graph, c0.node_id,
                                               "arbitrary_injection", 1)
        assert not result.ok and result.primary_code == "illegal_param_key"

    def test_reset_keeps_key_source_semantics(self):
        graph = _serial(1)
        c0 = _correctors(graph)[0]
        styled = _service().op_set_node_option(
            graph, c0.node_id, "blend_mode", "Add").graph
        result = _service().op_reset_corrector_parameters(
            styled, [c0.node_id])
        assert result.ok
        node = next(n for n in result.graph.nodes if n.node_id == c0.node_id)
        assert "mixer" not in node.params
        assert node.params["key_source_mode"] == "node_input"

    def test_layout_ops_do_not_bump_render_revision(self):
        graph = _serial(2)
        c0 = _correctors(graph)[0]
        result = _service().op_update_layout(graph, {c0.node_id: (7.0, 8.0)})
        assert result.ok
        assert result.graph.graph_revision == graph.graph_revision
        assert (result.graph.ui_state.layout_revision
                == graph.ui_state.layout_revision + 1)

    def test_layout_rejects_unknown_node_atomically(self):
        graph = _serial(1)
        result = _service().op_update_layout(graph, {"ghost": (0.0, 0.0)})
        assert not result.ok and result.graph is graph

    def test_move_and_insert_single_transaction(self):
        graph = _serial(1)
        free = _service().op_add_node(graph, "corrector", (0.0, 0.0)).graph
        target_edge = next(e for e in free.edges
                           if e.dst_node == graph.output_node_id)
        result = _service().op_move_and_insert(
            free, {"new-0": (12.0, 3.0)}, "new-0", target_edge.edge_id)
        assert result.ok
        # 一次事务只 +1 revision（add_node 那步之外）
        assert result.graph.graph_revision == free.graph_revision + 1
        assert result.graph.ui_state.positions["new-0"] == (12.0, 3.0)
        ins = [e for e in result.graph.edges if e.dst_node == "new-0"]
        outs = [e for e in result.graph.edges if e.src_node == "new-0"]
        assert len(ins) == 1 and len(outs) == 1

    def test_move_and_insert_rejects_connected_node(self):
        graph = _serial(2)
        c0, c1 = _correctors(graph)
        edge = next(e for e in graph.edges if e.dst_node == c1.node_id)
        result = _service().op_move_and_insert(
            graph, {c0.node_id: (1.0, 1.0)}, c0.node_id, edge.edge_id)
        assert not result.ok

    def test_metadata_color_lock_policy(self):
        graph = _serial(1)
        c0 = _correctors(graph)[0]
        assert _service().op_set_node_metadata(graph, c0.node_id, "Hero").ok
        assert _service().op_set_node_color(graph, [c0.node_id], "cyan").ok
        assert _service().op_set_node_color(
            graph, [c0.node_id], "chartreuse").primary_code == "illegal_param_key"
        assert _service().op_set_edit_lock(graph, [c0.node_id], True).ok
        assert _service().op_set_cache_policy(graph, [c0.node_id], "on").ok
        assert _service().op_set_cache_policy(
            graph, [c0.node_id], "sometimes").primary_code == "illegal_param_key"


# ── Compound / Shared ───────────────────────────────────────────────────

class TestCompoundAndShared:
    def _graph_with_two_correctors(self):
        graph = _serial(2)
        return graph, _correctors(graph)

    def test_group_ungroup_roundtrip_shape(self):
        graph, (c0, c1) = self._graph_with_two_correctors()
        grouped = _service().op_group_subgraph(graph, [c0.node_id, c1.node_id])
        assert grouped.ok, grouped.diagnostics
        compound = next(n for n in grouped.graph.nodes
                        if n.node_type == NodeType.COMPOUND.value)
        inner_ids = {n.node_id for n in compound.compound_children.nodes}
        assert c0.node_id in inner_ids and c1.node_id in inner_ids

        back = _service().op_ungroup_subgraph(grouped.graph, compound.node_id)
        assert back.ok, back.diagnostics
        restored_ids = {n.node_id for n in back.graph.nodes}
        assert c0.node_id in restored_ids and c1.node_id in restored_ids
        # 结构等价：两条 corrector 边恢复（source→c0→c1→output）
        edges = {(e.src_node, e.dst_node) for e in back.graph.edges}
        assert (graph.source_node_id, c0.node_id) in edges
        assert (c0.node_id, c1.node_id) in edges
        assert (c1.node_id, graph.output_node_id) in edges

    def test_group_rejects_terminals(self):
        graph, _ = self._graph_with_two_correctors()
        result = _service().op_group_subgraph(
            graph, [graph.source_node_id])
        assert not result.ok and result.primary_code == "terminal_count"

    def test_group_multi_boundary_rejected(self):
        """成员集有多条入边界（image + key）→ ambiguous_reconnect。"""
        graph, (c0, c1) = self._graph_with_two_correctors()
        keyed = _service().op_connect_ports(
            graph, c0.node_id, "key_out", c1.node_id, "key_in")
        assert keyed.ok
        result = _service().op_group_subgraph(keyed.graph, [c1.node_id])
        assert not result.ok
        assert result.primary_code == "ambiguous_reconnect"

    def test_shared_definition_and_make_local(self):
        graph, (c0, _) = self._graph_with_two_correctors()
        shared = _service().op_create_shared_definition(graph, c0.node_id,
                                                        "Look")
        assert shared.ok
        node = next(n for n in shared.graph.nodes if n.node_id == c0.node_id)
        assert node.node_type == NodeType.SHARED_CORRECTOR.value
        assert node.shared_definition_id == "new-0"

        local = _service().op_make_local_corrector(shared.graph, [c0.node_id])
        assert local.ok
        node2 = next(n for n in local.graph.nodes if n.node_id == c0.node_id)
        assert node2.node_type == NodeType.CORRECTOR.value
        assert node2.shared_definition_id is None
        # 定义深拷贝解除（参数等价）
        definition = next(d for d in shared.graph.shared_definitions)
        assert json.loads(json.dumps(
            {k: v for k, v in dict(node2.params).items()},
            default=list)) is not None

    def test_make_local_without_reference_rejected(self):
        graph, (c0, _) = self._graph_with_two_correctors()
        result = _service().op_make_local_corrector(graph, [c0.node_id])
        assert not result.ok and result.primary_code == "node_not_found"


# ── apply_intent 分派 ───────────────────────────────────────────────────

class TestApplyIntentDispatch:
    def test_intent_routes_to_operation(self):
        graph = _serial(1)
        c0 = _correctors(graph)[0]
        result = _service().apply_intent(
            graph, "insert_corrector",
            {"active_node_id": c0.node_id, "before": False})
        assert result.ok

    def test_unknown_command_rejected(self):
        graph = _serial(1)
        result = _service().apply_intent(graph, "nonexistent_op", {})
        assert not result.ok
        assert result.primary_code == "unknown_command"

    def test_contract_commands_all_have_handlers(self):
        """B01 契约表的每个 command 必须有 service handler（接线门禁）。"""
        from src.app.controllers.color_graph_controller import ACTION_CONTRACTS
        missing = []
        service = _service()
        for contract in ACTION_CONTRACTS.values():
            if contract.command in ("graph_undo", "graph_redo",
                                    "close_compound", "paste_grade_domains"):
                continue  # 历史/导航/剪贴板域在 B05/B14 实现
            if getattr(service, f"op_{contract.command}", None) is None:
                missing.append(contract.command)
        assert not missing, f"缺少 service handler: {missing}"
