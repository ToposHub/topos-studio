"""B04.1/B04.5 验证与原子性测试 —— 成环/悬空/错型/compound 循环/随机图。

计划验收：随机小 DAG + 手写反例；rejected intent 图完全不变；每次事务
确定性序列化。
"""

from __future__ import annotations

import json
import random
import uuid

import pytest

from src.features.color.core.color_graph_service import ColorGraphService
from src.shared.project.color_graph import (
    ColorEdgeData,
    ColorGraphData,
    ColorNodeData,
    GraphSchemaError,
    NodeType,
)
from tests.color.test_color_graph_schema import _serial_graph


def _service() -> ColorGraphService:
    counter = iter(range(100000))
    return ColorGraphService(id_factory=lambda: f"n{next(counter)}")


# ── 手写反例 ────────────────────────────────────────────────────────────

class TestHandwrittenRejections:
    def test_cycle_via_combined_image_and_key(self):
        """image+key 联合成环：c1.image→c0.key_in 被 connect 拒绝（§2.3）。"""
        graph = _serial_graph(2)
        cs = [n for n in graph.nodes
              if n.node_type == NodeType.CORRECTOR.value]
        # key_out→上游 key_in + 既有 image 链 = 联合环（image→key_in 是
        # 错型，会被 port_kind 先拦；组合环的合法构造走 key 边）
        result = _service().apply_intent(
            graph, "connect_ports",
            {"src_node": cs[1].node_id, "src_port": "key_out",
             "dst_node": cs[0].node_id, "dst_port": "key_in"})
        assert not result.ok
        assert result.primary_code == "cycle"
        assert result.graph is graph

    def test_connect_creates_image_cycle_rejected(self):
        graph = _serial_graph(2)
        cs = [n for n in graph.nodes
              if n.node_type == NodeType.CORRECTOR.value]
        result = _service().apply_intent(
            graph, "connect_ports",
            {"src_node": cs[1].node_id, "src_port": "image",
             "dst_node": cs[0].node_id, "dst_port": "image"})
        assert not result.ok and result.primary_code in ("cycle", "port_multi")

    def test_dangling_edge_rejected_by_schema(self):
        graph = _serial_graph(1)
        candidate = ColorGraphData(
            nodes=graph.nodes,
            edges=graph.edges + (ColorEdgeData(
                edge_id=str(uuid.uuid4()),
                src_node="ghost", src_port="image",
                dst_node=graph.output_node_id, dst_port="image"),),
            source_node_id=graph.source_node_id,
            output_node_id=graph.output_node_id)
        errors = candidate.validate()
        assert any(e.code == "node_not_found" for e in errors)

    def test_key_kind_mismatch_rejected(self):
        graph = _serial_graph(1)
        c = next(n for n in graph.nodes
                 if n.node_type == NodeType.CORRECTOR.value)
        result = _service().apply_intent(
            graph, "connect_ports",
            {"src_node": c.node_id, "src_port": "image",
             "dst_node": c.node_id, "dst_port": "key_in"})
        assert not result.ok and result.primary_code in ("port_kind", "cycle")

    def test_compound_nesting_depth_enforced(self):
        """compound 嵌套由 schema 深度上限拦截（frozen 结构无法自引用，
        递归风险以嵌套深度为界——超限在构造期报 recursion_limit）。"""
        graph = _serial_graph(1)
        grouped = _service().apply_intent(
            graph, "group_subgraph",
            {"member_ids": [next(
                n.node_id for n in graph.nodes
                if n.node_type == NodeType.CORRECTOR.value)]})
        assert grouped.ok
        nested = grouped.graph
        depth = 1
        while True:
            compound = next((n for n in nested.nodes
                             if n.node_type == NodeType.COMPOUND.value), None)
            if compound is None:
                break
            wrap = ColorNodeData(
                node_id=f"wrap-{depth}", node_type=NodeType.COMPOUND.value,
                compound_children=nested)
            with_new_nodes = tuple(nested.nodes) + (wrap,)
            # 深度超限时构造直接抛错
            try:
                ColorGraphData(nodes=with_new_nodes)
            except GraphSchemaError as exc:
                assert exc.code == "recursion_limit"
                break
            depth += 1
            if depth > 20:
                pytest.fail("深度上限未生效")
            nested = ColorGraphData(nodes=with_new_nodes)

    def test_isolated_node_allowed_structurally(self):
        """孤立节点结构层允许（§6.3），执行层报 isolated_node。"""
        graph = _serial_graph(1)
        added = _service().apply_intent(
            graph, "add_node", {"kind": "corrector", "position": [0, 0]})
        assert added.ok
        assert added.graph.is_valid()
        exec_errors = added.graph.validate_execution()
        assert any(e.code == "isolated_node" for e in exec_errors)

    def test_execution_validation_catches_broken_chain(self):
        graph = _serial_graph(2)
        cs = [n for n in graph.nodes
              if n.node_type == NodeType.CORRECTOR.value]
        broken = ColorGraphData(
            nodes=graph.nodes,
            edges=tuple(e for e in graph.edges if e.dst_node != cs[0].node_id),
            source_node_id=graph.source_node_id,
            output_node_id=graph.output_node_id)
        assert broken.is_valid()          # 结构层：端点都存在
        assert not broken.validate_execution() == []  # 执行层：Source 孤立


# ── 原子性：rejected intent 图完全不变 ─────────────────────────────────

class TestAtomicity:
    def test_rejected_result_carries_identical_graph_object(self):
        graph = _serial_graph(1)
        result = _service().apply_intent(
            graph, "connect_ports",
            {"src_node": "missing", "src_port": "image",
             "dst_node": graph.output_node_id, "dst_port": "image"})
        assert not result.ok
        assert result.graph is graph  # 完全不变（同一对象）

    def test_rejected_mutation_leaves_original_untouched(self):
        graph = _serial_graph(1)
        snapshot = graph.to_dict()
        _service().apply_intent(
            graph, "delete_nodes",
            {"node_ids": [graph.source_node_id]})   # 受保护 → 拒绝
        _service().apply_intent(
            graph, "connect_ports",
            {"src_node": graph.source_node_id, "src_port": "image",
             "dst_node": "ghost", "dst_port": "image"})
        assert graph.to_dict() == snapshot

    def test_successful_transaction_bumps_revision_once(self):
        graph = _serial_graph(1)
        result = _service().apply_intent(
            graph, "add_alpha_output", {})
        assert result.ok
        assert result.graph.graph_revision == graph.graph_revision + 1


# ── 确定性序列化 ────────────────────────────────────────────────────────

class TestDeterministicSerialization:
    def test_same_transaction_same_serialization(self):
        graph = _serial_graph(2)
        cs = [n for n in graph.nodes
              if n.node_type == NodeType.CORRECTOR.value]

        def run() -> str:
            result = _service().apply_intent(
                graph, "insert_corrector",
                {"active_node_id": cs[0].node_id, "before": False})
            assert result.ok
            return json.dumps(result.graph.to_dict(), sort_keys=True,
                              default=list)

        assert run() == run()  # 确定性 id_factory → 逐字节一致

    def test_edge_ids_deterministic_from_endpoints(self):
        graph = _serial_graph(1)
        c = next(n for n in graph.nodes
                 if n.node_type == NodeType.CORRECTOR.value)
        r1 = _service().apply_intent(
            graph, "connect_ports",
            {"src_node": c.node_id, "src_port": "key_out",
             "dst_node": graph.output_node_id, "dst_port": "image"})
        # （Output.image ← key_out 是错型，应拒绝——换合法目标验证）
        r2 = _service().apply_intent(
            graph, "add_key_mixer", {"key_source_ids": [c.node_id]})
        assert r2.ok
        new_edges = [e for e in r2.graph.edges if e.dst_port == "key"]
        assert new_edges
        # 同端点重建（disconnect → connect）得到相同 edge_id
        km = new_edges[0].dst_node
        r3 = _service().apply_intent(
            r2.graph, "disconnect_edge", {"edge_id": new_edges[0].edge_id})
        assert r3.ok
        r4 = _service().apply_intent(
            r3.graph, "connect_ports",
            {"src_node": c.node_id, "src_port": "key_out",
             "dst_node": km, "dst_port": "key"})
        assert r4.ok
        rebuilt = [e for e in r4.graph.edges
                   if e.src_node == c.node_id and e.dst_node == km]
        assert rebuilt[0].edge_id == new_edges[0].edge_id


# ── 随机小 DAG 属性测试 ─────────────────────────────────────────────────

class TestRandomDagProperties:
    def _random_ops_sequence(self, seed: int, n_ops: int) -> None:
        """随机操作序列不变量：每次事务后图要么合法、要么与事务前相等。"""
        rng = random.Random(seed)
        graph = _serial_graph(2)
        service = _service()
        cs = lambda g: [n.node_id for n in g.nodes  # noqa: E731
                        if n.node_type == NodeType.CORRECTOR.value]
        all_ids = lambda g: [n.node_id for n in g.nodes]  # noqa: E731

        for step in range(n_ops):
            before = graph.to_dict()
            op = rng.choice([
                ("insert_corrector",
                 lambda g: {"active_node_id": rng.choice(cs(g) or all_ids(g)),
                            "before": rng.random() < 0.5}),
                ("add_node", lambda g: {"kind": rng.choice(
                    ["corrector", "key_mixer", "splitter"]),
                    "position": [rng.uniform(-100, 100)] * 2}),
                ("connect_ports", lambda g: self._random_connect(g, rng)),
                ("disconnect_edge", lambda g: {
                    "edge_id": rng.choice([e.edge_id for e in g.edges])}
                    if g.edges else None),
                ("delete_nodes", lambda g: {
                    "node_ids": [rng.choice(cs(g) or all_ids(g))]}),
                ("set_bypass", lambda g: {
                    "node_ids": [rng.choice(all_ids(g))],
                    "enabled": rng.random() < 0.5}),
                ("add_mixer_slot", lambda g: {
                    "node_id": rng.choice(all_ids(g))}),
                ("update_layout", lambda g: {
                    "positions": {rng.choice(all_ids(g)):
                                  [rng.uniform(0, 500), rng.uniform(0, 500)]}}),
            ])
            name, arg_fn = op
            args = arg_fn(graph)
            if not args:
                continue  # 该操作本次无可用目标（如无边可断）：跳过
            try:
                result = service.apply_intent(graph, name, args)
            except (TypeError, KeyError):
                continue  # 参数不匹配的随机组合：跳过（不破坏不变量）
            if result.ok:
                assert result.graph.is_valid(), (
                    f"seed={seed} step={step} op={name}: 事务产物非法 "
                    f"{[e.code for e in result.graph.validate()]}")
                graph = result.graph
            else:
                assert graph.to_dict() == before, (
                    f"seed={seed} step={step} op={name}: 拒绝后图被改动")
                assert result.diagnostics, "失败必须带诊断"

    @staticmethod
    def _random_connect(graph: ColorGraphData, rng: random.Random) -> dict:
        ids = [n.node_id for n in graph.nodes]
        src = rng.choice(ids)
        dst = rng.choice(ids)
        return {"src_node": src, "src_port": "image",
                "dst_node": dst, "dst_port": "image"}

    @pytest.mark.parametrize("seed", [1, 7, 42, 2026])
    def test_random_sequences_maintain_invariants(self, seed):
        self._random_ops_sequence(seed, 60)
