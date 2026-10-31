"""
Tests for batch-export graph normalization.

背景：旧版本 migrate_color_input_to_topos_color 会把独立图片图的 color_input
错误迁移成"空的 timeline_clip_input + topos_color"。批量导出没有 timeline
provider，这种空 source 节点求值时返回空 dict，导致整条图断流、composite
效果丢失。

_normalize_standalone_graph_for_batch 的职责：把这种"空的 timeline_clip_input"
（timeline_id/clip_id/asset_id 全空）就地替换为 color_input 节点，使批量导出
的 color_texture 注入机制能重新生效。只改内存临时图，不碰磁盘 .topos。
"""

import types

import pytest

from src.app.controllers.deliver_controller import DeliverControllerMixin
from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.color_node import COLORNode
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.nodes.timeline_clip_input_node import TimelineClipInputNode
from src.features.composite.nodes.topos_color_node import ToposColorNode


def _make_controller_stub():
    """构造一个最小 stub 供调用 mixin 方法（方法内部不访问 self 其他属性）。"""
    return types.SimpleNamespace()


def _build_migrated_standalone_graph() -> NodeGraph:
    """模拟旧版本错误迁移后的独立图片图结构。

    timeline_clip_input(空) -> topos_color(bypass) -> output
    """
    graph = NodeGraph()
    src = TimelineClipInputNode(node_id="migrated_src_1")
    # 空 source：timeline_id/clip_id/asset_id 全空（migrate 生成的占位节点）
    tc = ToposColorNode(node_id="migrated_tc_1")
    tc.set_parameter("bypass", True)  # 迁移时 bypass=True（无 color_stack_id）
    out = OutputNode(node_id="output_1")
    graph.add_node(src)
    graph.add_node(tc)
    graph.add_node(out)
    graph.connect("migrated_src_1", "output", "migrated_tc_1", "image")
    graph.connect("migrated_tc_1", "result", "output_1", "image")
    return graph


class TestBatchExportGraphNormalize:
    """_normalize_standalone_graph_for_batch 的回归测试。"""

    def test_empty_timeline_source_replaced_by_color_input(self):
        """空 timeline_clip_input 必须被替换为 color_input，保留下游连接。"""
        graph = _build_migrated_standalone_graph()
        stub = _make_controller_stub()

        DeliverControllerMixin._normalize_standalone_graph_for_batch(stub, graph)

        types_set = {n.node_type for n in graph.nodes.values()}
        # 空 source 已被替换为 color_input
        assert "color_input" in types_set
        assert "timeline_clip_input" not in types_set
        # topos_color / output 保留不变
        assert "topos_color" in types_set
        assert "output" in types_set

        # 新 color_input 节点 id 应与旧 source 一致（保持 id 稳定）
        color_nodes = [n for n in graph.nodes.values() if n.node_type == "color_input"]
        assert len(color_nodes) == 1
        assert color_nodes[0].id == "migrated_src_1"

    def test_downstream_connection_preserved_with_correct_port(self):
        """替换后 color_input.result -> topos_color.image 的连接必须正确重建。

        color_input 输出端口是 "result"，而旧 timeline_clip_input 是 "output"，
        连接的 src_port 必须同步改成 "result"，否则求值时取不到上游纹理。
        """
        graph = _build_migrated_standalone_graph()
        stub = _make_controller_stub()

        DeliverControllerMixin._normalize_standalone_graph_for_batch(stub, graph)

        # 找 color_input -> topos_color 的连接
        color_to_tc = [
            c for c in graph.connections
            if c.src_node_id == "migrated_src_1"
            and c.dst_node_id == "migrated_tc_1"
        ]
        assert len(color_to_tc) == 1
        assert color_to_tc[0].src_port_name == "result"
        assert color_to_tc[0].dst_port_name == "image"

        # topos_color -> output 的连接应保持不变
        tc_to_out = [
            c for c in graph.connections
            if c.src_node_id == "migrated_tc_1"
            and c.dst_node_id == "output_1"
        ]
        assert len(tc_to_out) == 1

    def test_non_empty_timeline_source_not_touched(self):
        """带真实 clip_id 的 timeline_clip_input 不能被替换（有 timeline backing）。"""
        graph = NodeGraph()
        src = TimelineClipInputNode(node_id="real_src")
        src.set_parameter("timeline_id", "tl_1")
        src.set_parameter("clip_id", "clip_1")
        src.set_parameter("asset_id", "asset_1")
        out = OutputNode(node_id="output_1")
        graph.add_node(src)
        graph.add_node(out)
        graph.connect("real_src", "output", "output_1", "image")
        stub = _make_controller_stub()

        DeliverControllerMixin._normalize_standalone_graph_for_batch(stub, graph)

        types_set = {n.node_type for n in graph.nodes.values()}
        assert "timeline_clip_input" in types_set
        assert "color_input" not in types_set

    def test_partial_source_id_not_treated_as_empty(self):
        """只有 asset_id 也不算空（说明有真实引用）。"""
        graph = NodeGraph()
        src = TimelineClipInputNode(node_id="asset_src")
        src.set_parameter("asset_id", "asset_xyz")  # 只设了 asset_id
        out = OutputNode(node_id="output_1")
        graph.add_node(src)
        graph.add_node(out)
        graph.connect("asset_src", "output", "output_1", "image")
        stub = _make_controller_stub()

        DeliverControllerMixin._normalize_standalone_graph_for_batch(stub, graph)

        types_set = {n.node_type for n in graph.nodes.values()}
        assert "timeline_clip_input" in types_set
        assert "color_input" not in types_set

    def test_pure_color_input_graph_unchanged(self):
        """本来就是 color_input 的正确独立图片图，不应被改动。"""
        graph = NodeGraph()
        color_node = COLORNode(node_id="color_1")
        out = OutputNode(node_id="output_1")
        graph.add_node(color_node)
        graph.add_node(out)
        graph.connect("color_1", "result", "output_1", "image")
        stub = _make_controller_stub()

        DeliverControllerMixin._normalize_standalone_graph_for_batch(stub, graph)

        # 图结构完全不变
        assert len(graph.nodes) == 2
        types_set = {n.node_type for n in graph.nodes.values()}
        assert types_set == {"color_input", "output"}
