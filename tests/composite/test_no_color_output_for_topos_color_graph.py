"""
Tests for ColorOutput conditional request logic.

Validates:
- Default graph (TimelineClipInput -> Output) does not require ColorOutput
- ToposColor graph (TimelineClipInput -> ToposColor -> Output) does not require ColorOutput
- Legacy graph with color_input node still requires ColorOutput
"""

import pytest
from unittest.mock import MagicMock

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.ui.composite_page import CompositePage
from src.features.composite.nodes.color_node import COLORNode
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.nodes.topos_color_node import ToposColorNode
from src.features.composite.nodes.timeline_clip_input_node import TimelineClipInputNode


class TestRequiresColorOutput:
    """requires_color_output_input() correctly distinguishes graph types."""

    def test_default_graph_does_not_require_color_output(self):
        """Default graph TimelineClipInput -> Output: requires_color_output_input() == False"""
        graph = NodeGraph()
        graph.create_default_graph()

        # Simulate the check that requires_color_output_input() performs
        has_color_input = any(
            getattr(node, "node_type", "") == "color_input"
            for node in graph.nodes.values()
        )
        assert has_color_input is False

        # Also verify the default graph contains the expected nodes
        types = {n.node_type for n in graph.nodes.values()}
        assert "timeline_clip_input" in types
        assert "output" in types
        assert "color_input" not in types

    def test_topos_color_graph_does_not_require_color_output(self):
        """ToposColor graph does not have color_input, so no ColorOutput needed."""
        graph = NodeGraph()
        source = TimelineClipInputNode(node_id="src_1")
        tc = ToposColorNode(node_id="tc_1")
        output = OutputNode(node_id="out_1")
        graph.add_node(source)
        graph.add_node(tc)
        graph.add_node(output)
        graph.connect("src_1", "output", "tc_1", "image")
        graph.connect("tc_1", "result", "out_1", "image")

        has_color_input = any(
            getattr(node, "node_type", "") == "color_input"
            for node in graph.nodes.values()
        )
        assert has_color_input is False

        # Verify ToposColorNode uses "topos_color", never "color_input"
        assert tc.node_type == "topos_color"
        assert tc.node_type != "color_input"

    def test_legacy_color_node_type_is_color_input(self):
        """Legacy COLOR node has node_type == 'color_input'."""
        node = COLORNode()
        assert node.node_type == "color_input"

    def test_legacy_graph_requires_color_output(self):
        """Legacy graph with color_input node triggers requires_color_output_input() == True."""
        graph = NodeGraph()
        color_node = COLORNode(node_id="color_1")
        output_node = OutputNode(node_id="output_1")
        graph.add_node(color_node)
        graph.add_node(output_node)
        graph.connect("color_1", "result", "output_1", "image")

        has_color_input = any(
            getattr(node, "node_type", "") == "color_input"
            for node in graph.nodes.values()
        )
        assert has_color_input is True

    def test_requires_color_output_method_on_page(self):
        """CompositePage.requires_color_output_input() returns correct results for each graph."""
        # Test with a default graph (no color_input)
        page = MagicMock(spec=CompositePage)

        # Simulate default graph: no color_input nodes
        mock_graph = MagicMock()
        mock_node = MagicMock()
        mock_node.node_type = "timeline_clip_input"
        mock_graph.nodes = {"src": mock_node}
        page._node_graph = mock_graph

        result = CompositePage.requires_color_output_input(page)
        assert result is False

        # Simulate legacy graph: has color_input node
        mock_node2 = MagicMock()
        mock_node2.node_type = "color_input"
        mock_graph.nodes = {"color": mock_node2}
        page._node_graph = mock_graph

        result = CompositePage.requires_color_output_input(page)
        assert result is True
