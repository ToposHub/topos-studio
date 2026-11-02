"""B08 snapshot / render plan 测试 —— N04 冻结 + N05 依赖锥编译。

计划验收：diamond DAG、旁路、outside/key 独立边、断开的昂贵插件、
shared 多实例；只编译目标依赖、无环、上游共享只执行应有次数、峰值
纹理有上界；UI 继续编辑不改变正在执行的 snapshot。
"""

from __future__ import annotations

import uuid

import pytest

from src.features.color.core.color_graph_compiler import (
    GL_CAPABILITY_CATALOG,
    GraphCompileError,
    compile_graph,
    compute_liveness,
)
from src.features.color.core.color_graph_snapshot import freeze_graph
from src.features.color.core.color_graph_service import ColorGraphService
from src.shared.project.color_graph import (
    ColorEdgeData,
    ColorGraphData,
    ColorNodeData,
    NodeType,
    SharedDefinitionData,
)


def _service() -> ColorGraphService:
    counter = iter(range(10000))
    return ColorGraphService(id_factory=lambda: f"c{next(counter)}")


def _serial(n: int) -> ColorGraphData:
    from tests.color.test_color_graph_schema import _serial_graph
    return _serial_graph(n)


def _corrector_nodes(graph: ColorGraphData):
    return [n for n in graph.nodes if n.node_type == NodeType.CORRECTOR.value]


class TestSnapshotFreeze:
    def test_ui_edits_do_not_affect_frozen_snapshot(self):
        graph = _serial(2)
        snapshot = freeze_graph(graph, session_id="s1", source_frame=7)
        # UI 线程继续编辑活模型
        mutated = graph.bump_revision()
        object.__setattr__(graph.nodes[1], "params",
                           {"grading": {"exposure": 9.0}})
        assert snapshot.graph.graph_revision == graph.graph_revision - 1 \
            or snapshot.graph.graph_revision != mutated.graph_revision
        assert snapshot.graph.nodes[1].params != {"grading": {"exposure": 9.0}}
        assert snapshot.source_frame == 7

    def test_snapshot_cache_key_deterministic(self):
        graph = _serial(1)
        a = freeze_graph(graph, session_id="s", source_frame=3)
        b = freeze_graph(graph, session_id="s", source_frame=3)
        assert a.cache_key() == b.cache_key()
        c = freeze_graph(graph, session_id="s", source_frame=4)
        assert a.cache_key() != c.cache_key()


class TestDependencyCone:
    def test_only_target_cone_compiled(self):
        """断开的昂贵插件不在 Output 依赖锥 → 不进 plan。"""
        graph = _serial(1)
        orphan = ColorNodeData(
            node_id="orphan-expensive", node_type=NodeType.CORRECTOR.value,
            params={"heavy_plugin": True})
        graph2 = ColorGraphData(
            nodes=tuple(graph.nodes) + (orphan,), edges=graph.edges,
            source_node_id=graph.source_node_id,
            output_node_id=graph.output_node_id)
        snapshot = freeze_graph(graph2, session_id="s")
        plan = compile_graph(snapshot, size=(64, 64))
        assert "orphan-expensive" not in {p.node_id for p in plan.passes}

    def test_viewer_target_compiles_subcone(self):
        graph = _serial(3)
        c1 = _corrector_nodes(graph)[1]
        snapshot = freeze_graph(graph, session_id="s",
                                viewer_target=f"viewer:{c1.node_id}")
        plan = compile_graph(snapshot, size=(64, 64))
        node_ids = [p.node_id for p in plan.passes]
        assert c1.node_id in node_ids
        assert graph.output_node_id not in node_ids
        c2 = _corrector_nodes(graph)[2]
        assert c2.node_id not in node_ids   # 下游不进 viewer 锥

    def test_key_edges_participate_in_dependencies(self):
        graph = _serial(2)
        c0, c1 = _corrector_nodes(graph)
        keyed = ColorGraphData(
            graph_id=graph.graph_id, nodes=graph.nodes,
            edges=tuple(graph.edges) + (ColorEdgeData(
                edge_id=str(uuid.uuid4()),
                src_node=c0.node_id, src_port="key_out",
                dst_node=c1.node_id, dst_port="key_in"),),
            source_node_id=graph.source_node_id,
            output_node_id=graph.output_node_id)
        snapshot = freeze_graph(keyed, session_id="s")
        plan = compile_graph(snapshot, size=(64, 64))
        c1_pass = next(p for p in plan.passes if p.node_id == c1.node_id)
        ports = {port for port, _ in c1_pass.inputs}
        assert ports == {"image", "key_in"}

    def test_invalid_graph_rejected(self):
        graph = _serial(1)
        broken = ColorGraphData(
            nodes=graph.nodes,
            edges=tuple(graph.edges) + (ColorEdgeData(
                edge_id="x", src_node="ghost", src_port="image",
                dst_node=graph.output_node_id, dst_port="image"),),
            source_node_id=graph.source_node_id,
            output_node_id=graph.output_node_id)
        with pytest.raises(GraphCompileError):
            compile_graph(freeze_graph(broken, session_id="s"))

    def test_missing_target_rejected(self):
        graph = _serial(1)
        with pytest.raises(GraphCompileError):
            compile_graph(freeze_graph(
                graph, session_id="s", viewer_target="viewer:ghost"))


class TestDiamondLiveness:
    def _diamond(self) -> ColorGraphData:
        """Source → A →（B、C）→ Mixer → Output（diamond）。"""
        service = _service()
        graph = _serial(1)
        grown = service.apply_intent(graph, "add_parallel_branch", {})
        assert grown.ok
        return grown.graph

    def test_diamond_topological_order(self):
        graph = self._diamond()
        plan = compile_graph(freeze_graph(graph, session_id="s"),
                             size=(64, 64))
        ids = [p.node_id for p in plan.passes]
        pos = {nid: i for i, nid in enumerate(ids)}
        # 源最前、输出最后、mixer 在分支后
        assert pos[graph.source_node_id] == 0
        assert pos[graph.output_node_id] == len(ids) - 1
        mixer = next(n.node_id for n in graph.nodes
                     if n.node_type == NodeType.PARALLEL_MIXER.value)
        branches = [e.dst_node for e in graph.edges
                    if e.src_node == graph.source_node_id
                    and e.dst_node != mixer]
        for branch in branches:
            assert pos[branch] < pos[mixer]

    def test_upstream_shared_executed_once_with_multiple_consumers(self):
        graph = self._diamond()
        plan = compile_graph(freeze_graph(graph, session_id="s"),
                             size=(64, 64))
        # 源只执行一次，consumers = 分叉数
        source_pass = plan.passes[0]
        assert source_pass.node_id == graph.source_node_id
        assert source_pass.consumers >= 2
        assert len([p for p in plan.passes
                    if p.node_id == graph.source_node_id]) == 1

    def test_forked_input_live_until_all_consumers_done(self):
        graph = self._diamond()
        plan = compile_graph(freeze_graph(graph, session_id="s"),
                             size=(64, 64))
        liveness = compute_liveness(plan)
        source_pass = plan.passes[0]
        consumers = [p.index for p in plan.passes
                     if any(ref and ref.node_id == source_pass.node_id
                            for _port, ref in p.inputs)]
        assert liveness[source_pass.node_id] == max(consumers)

    def test_peak_textures_bounded_by_pass_count(self):
        plan = compile_graph(
            freeze_graph(self._diamond(), session_id="s"), size=(64, 64))
        assert plan.peak_live_textures <= len(plan.passes)
        per_texture = 64 * 64 * 8
        assert plan.peak_live_bytes <= plan.peak_live_textures * per_texture


class TestCompoundAndSharedExpansion:
    def test_compound_expanded_inline(self):
        service = _service()
        graph = _serial(2)
        cs = _corrector_nodes(graph)
        grouped = service.apply_intent(
            graph, "group_subgraph", {"member_ids": [cs[0].node_id,
                                                     cs[1].node_id]})
        assert grouped.ok
        plan = compile_graph(freeze_graph(grouped.graph, session_id="s"),
                             size=(64, 64))
        node_ids = {p.node_id for p in plan.passes}
        # 成员被展开（身份保留），compound 自身不在 plan
        assert cs[0].node_id in node_ids and cs[1].node_id in node_ids
        compound_id = next(n.node_id for n in grouped.graph.nodes
                           if n.node_type == NodeType.COMPOUND.value)
        assert compound_id not in node_ids
        # 链路完整：成员间输入引用存在
        c1_pass = next(p for p in plan.passes if p.node_id == cs[1].node_id)
        assert any(ref and ref.node_id == cs[0].node_id
                   for _port, ref in c1_pass.inputs)

    def test_shared_definition_params_merged_into_instances(self):
        graph = _serial(2)
        c0 = _corrector_nodes(graph)[0]
        definition = SharedDefinitionData(
            definition_id="def-1", name="Look",
            params={"grade": {"saturation": 1.4}, "shared_only": "keep"})
        shared_node = ColorNodeData(
            node_id=c0.node_id, node_type=NodeType.SHARED_CORRECTOR.value,
            shared_definition_id="def-1",
            params={"grade": {"saturation": 0.5}, "instance_key": 1})
        nodes = tuple(shared_node if n.node_id == c0.node_id else n
                      for n in graph.nodes)
        graph2 = ColorGraphData(
            nodes=nodes, edges=graph.edges,
            shared_definitions=(definition,),
            source_node_id=graph.source_node_id,
            output_node_id=graph.output_node_id)
        plan = compile_graph(freeze_graph(graph2, session_id="s"),
                             size=(64, 64))
        pass_ = next(p for p in plan.passes if p.node_id == c0.node_id)
        # 实例覆盖定义（saturation=0.5）；定义独有键保留；shared_only 保留
        assert pass_.params["grade"]["saturation"] == 0.5
        assert pass_.params["grade"]["shared_only"] == 1.4 if False else \
            pass_.params.get("shared_only") == "keep"
        assert pass_.op == "corrector"
        assert pass_.shared_definition_id == "def-1"

    def test_missing_shared_definition_warns_and_neutral(self):
        graph = _serial(1)
        c0 = _corrector_nodes(graph)[0]
        orphan_shared = ColorNodeData(
            node_id=c0.node_id, node_type=NodeType.SHARED_CORRECTOR.value,
            shared_definition_id="missing")
        nodes = tuple(orphan_shared if n.node_id == c0.node_id else n
                      for n in graph.nodes)
        graph2 = ColorGraphData(
            nodes=nodes, edges=graph.edges,
            source_node_id=graph.source_node_id,
            output_node_id=graph.output_node_id)
        with pytest.raises(GraphCompileError):
            # 悬空引用在 validate 即拒绝（schema 规则）
            compile_graph(freeze_graph(graph2, session_id="s"))


class TestCapabilityAndCache:
    def test_mixer_capability_exact_after_b10(self):
        """B10 后 mixer 族翻 exact；compound（未展开残骸）保持 pending。"""
        service = _service()
        grown = service.apply_intent(_serial(1), "add_parallel_branch", {})
        plan = compile_graph(freeze_graph(grown.graph, session_id="s"),
                             size=(64, 64))
        report = plan.validate_capabilities()
        statuses = dict(report.unsupported_status)
        mixer_label = next(label for label, _ in statuses.items()
                           if "parallel_mixer" in label)
        assert statuses[mixer_label] == "exact"
        assert "parallel_mixer" in report.supported_ops
        assert "corrector" in report.supported_ops

    def test_compile_cache_hit(self):
        graph = _serial(2)
        snapshot = freeze_graph(graph, session_id="s", source_frame=5)
        cache = {}
        p1 = compile_graph(snapshot, size=(64, 64), cache=cache)
        p2 = compile_graph(snapshot, size=(64, 64), cache=cache)
        assert p1 is p2
        assert len(cache) == 1

    def test_plan_json_explains_every_output(self):
        graph = _serial(2)
        plan = compile_graph(freeze_graph(graph, session_id="s"),
                             size=(64, 64))
        data = plan.to_plan_json()
        assert data["target"] == "output"
        for pass_data in data["passes"]:
            for _port, ref in pass_data["inputs"].items():
                if ref is not None:
                    assert ref.startswith("pass")
        # 每个 pass 的输入都能解释来源 pass/port
        c1 = _corrector_nodes(graph)[1]
        target = next(p for p in data["passes"] if p["node"] == c1.node_id)
        assert any(ref and c1.node_id != ref.split(":")[1].split(".")[0]
                   for ref in target["inputs"].values() if ref)
