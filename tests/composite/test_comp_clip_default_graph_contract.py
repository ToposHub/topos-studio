"""Tests for default composite graph connection contracts.

Validates that the default graph data produced by CompositeGraphData factory
methods produces connections that are valid against the real NodeGraph engine.

Key contract:
- OutputNode input port is named "image" (NOT "input")
- ConstantNode output port is named "result" (NOT "output")
- TimelineClipInputNode output port is named "output"
"""
import pytest
from src.features.edit.core.composite_graph_data import CompositeGraphData
from src.features.composite.core.node_engine import NodeGraph, NODE_REGISTRY


def _build_graph_from_data(graph_data) -> NodeGraph:
    """Build a NodeGraph from a CompositeGraphData instance or dict.

    Handles CompositeGraphData (frozen dataclass) by extracting .nodes and
    .connections attributes, or plain dicts with "nodes"/"connections" keys.
    """
    if isinstance(graph_data, dict):
        nodes_list = graph_data.get("nodes", [])
        connections_list = graph_data.get("connections", [])
    else:
        # CompositeGraphData dataclass
        nodes_list = graph_data.nodes
        connections_list = graph_data.connections

    graph = NodeGraph()
    for node_dict in nodes_list:
        node_type = node_dict.get("type") or node_dict.get("node_type")
        node_cls = NODE_REGISTRY.get(node_type)
        if node_cls:
            node = node_cls.from_dict(node_dict)
            graph.add_node(node)
    return graph


class TestDefaultGraphForClip:
    """Tests for CompositeGraphData.create_default_for_clip()."""

    def test_create_default_for_clip_connects_successfully(self):
        data = CompositeGraphData.create_default_for_clip(
            clip_id="clip-1",
            asset_id="asset-1",
            asset_path="/tmp/test.exr",
        )

        graph = _build_graph_from_data(data)

        # Attempt all connections from the serialized data
        for conn in data.connections:
            result = graph.connect(
                conn["src_node_id"], conn["src_port_name"],
                conn["dst_node_id"], conn["dst_port_name"],
            )
            assert result is True, (
                f"Connection failed: {conn['src_node_id']}.{conn['src_port_name']} -> "
                f"{conn['dst_node_id']}.{conn['dst_port_name']}"
            )

        assert graph.has_connected_output(), "Output node must have a connected input"

    def test_create_default_for_clip_has_three_nodes(self):
        """Phase 4: timeline-backed graph has TimelineClipInput + ToposColor + Output."""
        data = CompositeGraphData.create_default_for_clip(
            clip_id="clip-1",
            asset_id="asset-1",
            asset_path="/tmp/test.exr",
        )
        # Phase 4: Always have 3 nodes (TimelineClipInput -> ToposColor -> Output)
        assert len(data.nodes) == 3
        assert len(data.connections) == 2
        
        # Verify node types
        node_types = [n["type"] for n in data.nodes]
        assert "timeline_clip_input" in node_types
        assert "topos_color" in node_types
        assert "output" in node_types

    def test_create_default_for_clip_output_port_name_is_image(self):
        data = CompositeGraphData.create_default_for_clip(
            clip_id="clip-1",
            asset_id="asset-1",
            asset_path="/tmp/test.exr",
        )
        conn = data.connections[0]
        assert conn["dst_port_name"] == "image", (
            f"Expected dst_port_name='image', got '{conn['dst_port_name']}'"
        )


class TestDefaultGraphEmptyCompClip:
    """Tests for CompositeGraphData.create_default_empty_comp_clip()."""

    def test_create_default_empty_comp_clip_connects_successfully(self):
        data = CompositeGraphData.create_default_empty_comp_clip(
            composite_id="test-comp2",
            name="Test Comp",
        )

        graph = _build_graph_from_data(data)

        for conn in data.connections:
            result = graph.connect(
                conn["src_node_id"], conn["src_port_name"],
                conn["dst_node_id"], conn["dst_port_name"],
            )
            assert result is True, (
                f"Connection failed: {conn['src_node_id']}.{conn['src_port_name']} -> "
                f"{conn['dst_node_id']}.{conn['dst_port_name']}"
            )

        assert graph.has_connected_output(), "Output node must have a connected input"

    def test_create_default_empty_comp_clip_has_two_nodes(self):
        data = CompositeGraphData.create_default_empty_comp_clip(
            composite_id="test-comp2",
            name="Test Comp",
        )
        assert len(data.nodes) == 2
        assert len(data.connections) == 1

    def test_create_default_empty_comp_clip_constant_port_is_result(self):
        data = CompositeGraphData.create_default_empty_comp_clip(
            composite_id="test-comp2",
            name="Test Comp",
        )
        conn = data.connections[0]
        assert conn["src_port_name"] == "result", (
            f"Expected src_port_name='result' (ConstantNode output), "
            f"got '{conn['src_port_name']}'"
        )

    def test_create_default_empty_comp_clip_output_port_name_is_image(self):
        data = CompositeGraphData.create_default_empty_comp_clip(
            composite_id="test-comp2",
            name="Test Comp",
        )
        conn = data.connections[0]
        assert conn["dst_port_name"] == "image", (
            f"Expected dst_port_name='image', got '{conn['dst_port_name']}'"
        )
