"""B02 图 schema 测试 —— N01 纯数据模型 + E06 集成。

计划验收：空图、合法串行图、future fields、未知节点、NaN/非法数字/
重复 UUID/超大输入；roundtrip 无损、拒绝 malformed 不污染实例；
布局/渲染 revision 分离；with_* 工厂保留新字段。
"""

from __future__ import annotations

import copy
import math
import uuid

import pytest

from src.shared.project.color_graph import (
    COLOR_GRAPH_SCHEMA_VERSION,
    MAX_COMPOUND_DEPTH,
    MAX_EDGES,
    MAX_NODES,
    ColorEdgeData,
    ColorGraphData,
    ColorGraphUiState,
    ColorNodeData,
    GraphSchemaError,
    NodeType,
    PortDirection,
    PortKind,
    SharedDefinitionData,
    freeze_params,
)
from src.shared.project.color_stack import ColorStackData


def _serial_graph(n_correctors: int = 1) -> ColorGraphData:
    """Source → C0 → … → Cn-1 → Output 的合法串行图。"""
    source = ColorNodeData(node_id=str(uuid.uuid4()),
                           node_type=NodeType.SOURCE.value, label="Source")
    output = ColorNodeData(node_id=str(uuid.uuid4()),
                           node_type=NodeType.OUTPUT.value, label="Output")
    nodes = [source]
    edges = []
    prev = source.node_id
    for i in range(n_correctors):
        node = ColorNodeData(
            node_id=str(uuid.uuid4()), node_type=NodeType.CORRECTOR.value,
            label=f"C{i}",
            params={"grading": {"exposure": 0.1 * i}})
        nodes.append(node)
        edges.append(ColorEdgeData(
            edge_id=str(uuid.uuid4()), src_node=prev, src_port="image",
            dst_node=node.node_id, dst_port="image"))
        prev = node.node_id
    nodes.append(output)
    edges.append(ColorEdgeData(
        edge_id=str(uuid.uuid4()), src_node=prev, src_port="image",
        dst_node=output.node_id, dst_port="image"))
    return ColorGraphData(
        nodes=tuple(nodes), edges=tuple(edges),
        source_node_id=source.node_id, output_node_id=output.node_id)


# ── 空图 / 合法图 ────────────────────────────────────────────────────────

class TestBasicGraphs:
    def test_empty_graph_is_constructible_and_invalid(self):
        graph = ColorGraphData()
        assert graph.nodes == () and graph.edges == ()
        errors = graph.validate()
        assert any(e.code == "terminal_count" for e in errors)

    def test_create_serial_is_valid(self):
        graph = ColorGraphData.create_serial()
        assert graph.is_valid()

    def test_multi_corrector_serial_valid(self):
        assert _serial_graph(4).is_valid()


# ── 端口类型 / 方向 / 单连接 ────────────────────────────────────────────

class TestTypedPorts:
    def test_image_to_key_rejected(self):
        graph = _serial_graph(1)
        corrector = next(n for n in graph.nodes
                         if n.node_type == NodeType.CORRECTOR.value)
        key_mixer = ColorNodeData(
            node_id=str(uuid.uuid4()), node_type=NodeType.KEY_MIXER.value)
        bad = ColorEdgeData(
            edge_id=str(uuid.uuid4()),
            src_node=corrector.node_id, src_port="image",
            dst_node=key_mixer.node_id, dst_port="key")
        graph = ColorGraphData(
            graph_id=graph.graph_id,
            nodes=tuple(graph.nodes) + (key_mixer,),
            edges=tuple(graph.edges) + (bad,),
            source_node_id=graph.source_node_id,
            output_node_id=graph.output_node_id)
        errors = graph.validate()
        assert any(e.code == "port_kind" for e in errors)

    def test_corrector_key_edge_is_legal(self):
        graph = _serial_graph(2)
        c_nodes = [n for n in graph.nodes
                   if n.node_type == NodeType.CORRECTOR.value]
        key_edge = ColorEdgeData(
            edge_id=str(uuid.uuid4()),
            src_node=c_nodes[0].node_id, src_port="key_out",
            dst_node=c_nodes[1].node_id, dst_port="key_in")
        graph = ColorGraphData(
            graph_id=graph.graph_id, nodes=graph.nodes,
            edges=tuple(graph.edges) + (key_edge,),
            source_node_id=graph.source_node_id,
            output_node_id=graph.output_node_id)
        assert graph.is_valid()

    def test_single_input_port_double_connection_rejected(self):
        graph = _serial_graph(1)
        corrector = next(n for n in graph.nodes
                         if n.node_type == NodeType.CORRECTOR.value)
        dup = ColorEdgeData(
            edge_id=str(uuid.uuid4()),
            src_node=graph.source_node_id, src_port="image",
            dst_node=corrector.node_id, dst_port="image")
        graph = ColorGraphData(
            graph_id=graph.graph_id, nodes=graph.nodes,
            edges=tuple(graph.edges) + (dup,),
            source_node_id=graph.source_node_id,
            output_node_id=graph.output_node_id)
        errors = graph.validate()
        assert any(e.code == "port_multi" for e in errors)

    def test_cycle_detected_including_key_edges(self):
        graph = _serial_graph(2)
        c_nodes = [n for n in graph.nodes
                   if n.node_type == NodeType.CORRECTOR.value]
        # c1.image → c0.key_in：key_in 是独立输入端口（无双连接问题），
        # 但与 image 链共同构成 c0→c1→c0 环（image+key 联合检测）
        back = ColorEdgeData(
            edge_id=str(uuid.uuid4()),
            src_node=c_nodes[1].node_id, src_port="image",
            dst_node=c_nodes[0].node_id, dst_port="key_in")
        graph = ColorGraphData(
            graph_id=graph.graph_id, nodes=graph.nodes,
            edges=tuple(graph.edges) + (back,),
            source_node_id=graph.source_node_id,
            output_node_id=graph.output_node_id)
        errors = graph.validate()
        assert any(e.code == "cycle" for e in errors)


# ── 参数冻结 ────────────────────────────────────────────────────────────

class TestParamFreezing:
    def test_external_mutation_blocked_after_construction(self):
        src = {"grading": {"exposure": 0.5}, "tags": ["a", "b"]}
        node = ColorNodeData(node_id="n1", node_type=NodeType.CORRECTOR.value,
                             params=src)
        src["grading"]["exposure"] = 99.0
        src["tags"].append("c")
        assert node.params["grading"]["exposure"] == 0.5
        assert list(node.params["tags"]) == ["a", "b"]
        with pytest.raises(TypeError):
            node.params["grading"]["exposure"] = 1.0  # mappingproxy 只读

    def test_nan_and_inf_rejected(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(GraphSchemaError) as ei:
                ColorNodeData(node_id="n1", node_type=NodeType.CORRECTOR.value,
                              params={"exposure": bad})
            assert ei.value.code == "non_finite_number"

    def test_absolute_path_rejected(self):
        with pytest.raises(GraphSchemaError) as ei:
            ColorNodeData(node_id="n1", node_type=NodeType.CORRECTOR.value,
                          params={"lut_path": "/Users/x/luts/a.cube"})
        assert ei.value.code == "absolute_path"
        with pytest.raises(GraphSchemaError):
            ColorNodeData(node_id="n1", node_type=NodeType.CORRECTOR.value,
                          params={"lut_path": "~/luts/a.cube"})
        # 工程相对引用合法
        ColorNodeData(node_id="n1", node_type=NodeType.CORRECTOR.value,
                      params={"lut_ref": "assets/luts/a.cube"})

    def test_non_serializable_rejected(self):
        class QtLike:
            pass
        with pytest.raises(GraphSchemaError):
            ColorNodeData(node_id="n1", node_type=NodeType.CORRECTOR.value,
                          params={"obj": QtLike()})

    def test_recursion_depth_limited(self):
        deep = current = {}
        for _ in range(20):
            current["child"] = {}
            current = current["child"]
        with pytest.raises(GraphSchemaError) as ei:
            ColorNodeData(node_id="n1", node_type=NodeType.CORRECTOR.value,
                          params=deep)
        assert ei.value.code == "recursion_limit"


# ── 身份 / 大小 / 端子 ──────────────────────────────────────────────────

class TestIdentityAndLimits:
    def test_duplicate_node_id_rejected(self):
        node = ColorNodeData(node_id="dup", node_type=NodeType.SOURCE.value)
        with pytest.raises(GraphSchemaError) as ei:
            ColorGraphData(nodes=(node, node))
        assert ei.value.code == "duplicate_node_id"

    def test_duplicate_edge_id_rejected(self):
        edge = ColorEdgeData(edge_id="e1", src_node="a", src_port="image",
                             dst_node="b", dst_port="image")
        with pytest.raises(GraphSchemaError) as ei:
            ColorGraphData(edges=(edge, edge))
        assert ei.value.code == "duplicate_edge_id"

    def test_node_count_limit(self):
        nodes = tuple(ColorNodeData(node_id=str(uuid.uuid4()),
                                    node_type=NodeType.CORRECTOR.value)
                      for _ in range(MAX_NODES + 1))
        with pytest.raises(GraphSchemaError) as ei:
            ColorGraphData(nodes=nodes)
        assert ei.value.code == "size_limit"

    def test_compound_depth_limit(self):
        # create_serial 深度记 1；每包一层 compound 深度 +1。
        # 合法上限：深度 == MAX_COMPOUND_DEPTH；再包一层必须 recursion_limit。
        graph: ColorGraphData = ColorGraphData.create_serial()  # depth 1
        for _ in range(MAX_COMPOUND_DEPTH - 1):
            compound_node = ColorNodeData(
                node_id=str(uuid.uuid4()), node_type=NodeType.COMPOUND.value,
                compound_children=graph)
            graph = ColorGraphData(nodes=(compound_node,))  # depth +1 至 MAX
        with pytest.raises(GraphSchemaError) as ei:
            ColorGraphData(nodes=(ColorNodeData(
                node_id=str(uuid.uuid4()), node_type=NodeType.COMPOUND.value,
                compound_children=graph),))
        assert ei.value.code == "recursion_limit"

    def test_two_outputs_rejected_by_validate(self):
        graph = _serial_graph(0)
        out2 = ColorNodeData(node_id=str(uuid.uuid4()),
                             node_type=NodeType.OUTPUT.value)
        graph2 = ColorGraphData(
            nodes=tuple(graph.nodes) + (out2,), edges=graph.edges,
            source_node_id=graph.source_node_id,
            output_node_id=graph.output_node_id)
        errors = graph2.validate()
        assert any(e.code == "terminal_count" for e in errors)


# ── roundtrip / future fields / 未知节点 ────────────────────────────────

class TestRoundtripAndFutureFields:
    def test_roundtrip_lossless(self):
        graph = _serial_graph(3)
        data = graph.to_dict()
        restored = ColorGraphData.from_dict(data)
        assert restored.to_dict() == data
        assert restored.is_valid()

    def test_unknown_node_type_preserved(self):
        graph = _serial_graph(1)
        mystery = {
            "node_id": "future-node",
            "node_type": "neural_relight",
            "type_version": 3,
            "params": {"strength": 0.4, "future_blob": {"x": 1}},
            "enabled": True,
            "vendor_field": "keep-me",
        }
        data = graph.to_dict()
        data["nodes"].append(mystery)
        restored = ColorGraphData.from_dict(data)
        node = next(n for n in restored.nodes if n.node_id == "future-node")
        assert node.node_type == "neural_relight"
        assert node.params["future_blob"]["x"] == 1
        assert node.to_dict()["vendor_field"] == "keep-me"  # 节点级未知字段保留
        # 未知类型不产生端口 → 连它的边报 port_not_found
        assert node.ports() == ()
        assert restored.to_dict()["nodes"][-1] == mystery  # 不丢字段

    def test_future_graph_schema_rejected_cleanly(self):
        graph = _serial_graph(0)
        data = graph.to_dict()
        data["schema_version"] = COLOR_GRAPH_SCHEMA_VERSION + 1
        data["mystery_section"] = {"a": 1}
        with pytest.raises(GraphSchemaError) as ei:
            ColorGraphData.from_dict(data)
        assert "schema" in str(ei.value)

    def test_unknown_top_level_fields_preserved(self):
        graph = _serial_graph(1)
        data = graph.to_dict()
        data["future_section"] = {"keep": [1, 2, 3]}
        restored = ColorGraphData.from_dict(data)
        assert restored.to_dict()["future_section"] == {"keep": [1, 2, 3]}

    def test_malformed_payload_rejected_without_partial_instance(self):
        with pytest.raises(GraphSchemaError):
            ColorGraphData.from_dict({"nodes": [{"no_id": True}]})
        with pytest.raises(GraphSchemaError):
            ColorGraphData.from_dict({"edges": [{"edge_id": "e"}]})
        with pytest.raises(GraphSchemaError):
            ColorGraphData.from_dict("not-a-dict")

    def test_unknown_node_type_sets_minimum_reader_version(self):
        graph = _serial_graph(0)
        data = graph.to_dict()
        data["nodes"].append({"node_id": "x1", "node_type": "future_type",
                              "params": {}, "enabled": True})
        restored = ColorGraphData.from_dict(data)
        assert restored.to_dict()["minimum_reader_version"] > COLOR_GRAPH_SCHEMA_VERSION

    def test_ui_state_future_fields_preserved(self):
        data = ColorGraphUiState().to_dict()
        data["future_ui_key"] = 7
        restored = ColorGraphUiState.from_dict(data)
        assert restored.to_dict()["future_ui_key"] == 7


# ── 布局/渲染 revision 分离（B02.3） ────────────────────────────────────

class TestRevisionSeparation:
    def test_layout_change_keeps_render_revision(self):
        graph = _serial_graph(2)
        rev0 = graph.graph_revision
        moved = graph.with_layout(replace_ui(graph, layout_revision=5))
        assert moved.graph_revision == rev0
        assert moved.ui_state.layout_revision == 5
        bumped = graph.bump_revision()
        assert bumped.graph_revision == rev0 + 1
        assert bumped.ui_state.layout_revision == graph.ui_state.layout_revision

    def test_deepcopy_isolates_undo_baseline(self):
        graph = _serial_graph(1)
        baseline = graph.deepcopy()
        node = baseline.nodes[1]
        object.__setattr__(node, "params", freeze_params({"exposure": 9.0}))
        assert graph.nodes[1].params != node.params


def replace_ui(graph: ColorGraphData, layout_revision: int) -> ColorGraphUiState:
    from dataclasses import replace
    return replace(graph.ui_state, layout_revision=layout_revision)


# ── 共享定义 ────────────────────────────────────────────────────────────

class TestSharedDefinitions:
    def test_definition_reference_valid(self):
        graph = _serial_graph(0)
        definition = SharedDefinitionData(
            definition_id="def-1", name="Look A",
            params={"grading": {"saturation": 1.2}})
        node = ColorNodeData(
            node_id=str(uuid.uuid4()),
            node_type=NodeType.SHARED_CORRECTOR.value,
            shared_definition_id="def-1")
        graph2 = ColorGraphData(
            graph_id=graph.graph_id,
            nodes=tuple(graph.nodes) + (node,),
            edges=graph.edges,
            shared_definitions=(definition,),
            source_node_id=graph.source_node_id,
            output_node_id=graph.output_node_id)
        assert graph2.is_valid()
        assert graph2.to_dict()["shared_definitions"][0]["params"] == {
            "grading": {"saturation": 1.2}}

    def test_dangling_definition_reference_reported(self):
        graph = _serial_graph(0)
        node = ColorNodeData(
            node_id=str(uuid.uuid4()),
            node_type=NodeType.SHARED_CORRECTOR.value,
            shared_definition_id="missing-def")
        graph2 = ColorGraphData(
            graph_id=graph.graph_id,
            nodes=tuple(graph.nodes) + (node,),
            edges=graph.edges,
            source_node_id=graph.source_node_id,
            output_node_id=graph.output_node_id)
        errors = graph2.validate()
        assert any(e.code == "node_not_found" for e in errors)


# ── E06 ColorStackData 集成（B02.2） ────────────────────────────────────

class TestColorStackIntegration:
    def test_stack_carries_graph_roundtrip(self):
        stack = ColorStackData.create(asset_id="a1", name="s")
        graph = _serial_graph(2)
        stack2 = stack.with_color_graph(graph)
        assert stack2.color_graph is not None
        assert stack2.color_graph.graph_id == graph.graph_id

        data = stack2.to_dict()
        assert data["color_graph"]["schema_version"] == COLOR_GRAPH_SCHEMA_VERSION
        restored = ColorStackData.from_dict(data)
        assert restored.color_graph is not None
        assert restored.color_graph.to_dict() == graph.to_dict()

    def test_legacy_stack_without_graph_still_loads(self):
        stack = ColorStackData.create(asset_id="a1", name="old")
        data = stack.to_dict()
        assert "color_graph" not in data
        restored = ColorStackData.from_dict(data)
        assert restored.color_graph is None

    def test_graph_survives_with_factories(self):
        """新字段经既有 with_* 工厂更新仍保留（B02.4）。"""
        stack = ColorStackData.create(asset_id="a1", name="s")
        graph = _serial_graph(1)
        stack = stack.with_color_graph(graph)
        stack = stack.with_name("renamed")
        stack = stack.with_grading_params({"exposure": 0.3})
        stack = stack.with_layers([], active_layer_index=0)
        assert stack.color_graph is not None
        assert stack.color_graph.graph_id == graph.graph_id

    def test_corrupt_graph_payload_raises_not_silent(self):
        stack = ColorStackData.create(asset_id="a1", name="s")
        data = stack.to_dict()
        data["color_graph"] = {"schema_version": COLOR_GRAPH_SCHEMA_VERSION,
                               "nodes": "corrupt"}
        with pytest.raises(GraphSchemaError):
            ColorStackData.from_dict(data)

    def test_video_stabilization_untouched_by_graph_field(self):
        stack = ColorStackData.create(asset_id="a1", name="s")
        stack = stack.with_trackings({"schema_version": 1, "tracks": {}})
        graph = _serial_graph(0)
        stack = stack.with_color_graph(graph)
        data = stack.to_dict()
        assert data.get("trackings")
        restored = ColorStackData.from_dict(data)
        # 现有字段不因新字段丢失
        assert restored.name == "s"
        assert restored.trackings == stack.trackings
        assert restored.grading_params == stack.grading_params
