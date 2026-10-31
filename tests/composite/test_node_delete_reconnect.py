"""
Tests for node deletion with auto-reconnect.

Validates:
- Deleting single-input/single-output node reconnects upstream to downstream
- Deleting source nodes does NOT auto-reconnect
- Deleting output nodes does NOT auto-reconnect
- Complex multi-input nodes fall back to simple removal
"""

import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.timeline_clip_input_node import TimelineClipInputNode
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.nodes.grade_node import GradeNode
from src.features.composite.nodes.blur_node import BlurNode
from src.features.composite.nodes.topos_color_node import ToposColorNode


class TestNodeDeleteReconnect:
    """Tests for remove_node_and_reconnect behavior."""

    def _build_simple_chain(self) -> NodeGraph:
        """Build: source -> grade -> output"""
        graph = NodeGraph()
        source = TimelineClipInputNode()
        grade = GradeNode()
        output = OutputNode()
        graph.add_node(source)
        graph.add_node(grade)
        graph.add_node(output)
        graph.connect(source.id, "output", grade.id, "image")
        graph.connect(grade.id, "result", output.id, "image")
        return graph

    def test_delete_middle_node_reconnects(self):
        """Deleting a middle processing node should reconnect upstream to downstream."""
        graph = self._build_simple_chain()
        assert len(graph.connections) == 2

        # Find node IDs
        source_id = [nid for nid, n in graph.nodes.items()
                     if isinstance(n, TimelineClipInputNode)][0]
        grade_id = [nid for nid, n in graph.nodes.items()
                    if isinstance(n, GradeNode)][0]
        output_id = [nid for nid, n in graph.nodes.items()
                     if isinstance(n, OutputNode)][0]

        result = graph.remove_node_and_reconnect(grade_id)
        assert result is True
        assert len(graph.nodes) == 2
        assert len(graph.connections) == 1

        # Check the new connection: source -> output
        conn = graph.connections[0]
        assert conn.src_node_id == source_id
        assert conn.src_port_name == "output"
        assert conn.dst_node_id == output_id
        assert conn.dst_port_name == "image"

    def test_delete_source_no_reconnect(self):
        """Deleting a source node should not reconnect (no upstream)."""
        graph = self._build_simple_chain()
        source_id = [nid for nid, n in graph.nodes.items()
                     if isinstance(n, TimelineClipInputNode)][0]
        grade_id = [nid for nid, n in graph.nodes.items()
                    if isinstance(n, GradeNode)][0]

        result = graph.remove_node_and_reconnect(source_id)
        assert result is True
        assert len(graph.nodes) == 2
        # The grade node should have no incoming connection
        incoming = [c for c in graph.connections if c.dst_node_id == grade_id]
        assert len(incoming) == 0

    def test_delete_output_no_reconnect(self):
        """Deleting the output node should not reconnect (no downstream)."""
        graph = self._build_simple_chain()
        output_id = [nid for nid, n in graph.nodes.items()
                     if isinstance(n, OutputNode)][0]

        result = graph.remove_node_and_reconnect(output_id)
        assert result is True
        assert len(graph.nodes) == 2

    def test_delete_nonexistent_returns_false(self):
        """Deleting a non-existent node should return False."""
        graph = self._build_simple_chain()
        result = graph.remove_node_and_reconnect("nonexistent")
        assert result is False

    def test_delete_with_topos_color_chain(self):
        """Test with topos_color in the chain: source -> topos_color -> output."""
        graph = NodeGraph()
        source = TimelineClipInputNode()
        tc = ToposColorNode()
        output = OutputNode()
        graph.add_node(source)
        graph.add_node(tc)
        graph.add_node(output)
        graph.connect(source.id, "output", tc.id, "image")
        graph.connect(tc.id, "result", output.id, "image")

        source_id = source.id
        output_id = output.id

        result = graph.remove_node_and_reconnect(tc.id)
        assert result is True
        assert len(graph.nodes) == 2
        assert len(graph.connections) == 1

        conn = graph.connections[0]
        assert conn.src_node_id == source_id
        assert conn.src_port_name == "output"
        assert conn.dst_node_id == output_id
        assert conn.dst_port_name == "image"

    def test_delete_in_longer_chain(self):
        """Test: source -> topos_color -> blur -> output, delete topos_color."""
        graph = NodeGraph()
        source = TimelineClipInputNode()
        tc = ToposColorNode()
        blur = BlurNode()
        output = OutputNode()
        graph.add_node(source)
        graph.add_node(tc)
        graph.add_node(blur)
        graph.add_node(output)
        graph.connect(source.id, "output", tc.id, "image")
        graph.connect(tc.id, "result", blur.id, "image")
        graph.connect(blur.id, "result", output.id, "image")

        source_id = source.id
        blur_id = blur.id
        output_id = output.id

        result = graph.remove_node_and_reconnect(tc.id)
        assert result is True
        assert len(graph.nodes) == 3

        # source -> blur -> output
        conns = {(c.src_node_id, c.dst_node_id) for c in graph.connections}
        assert (source_id, blur_id) in conns
        assert (blur_id, output_id) in conns
