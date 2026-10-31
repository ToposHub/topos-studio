"""
Contract tests for default composite graph with Topos Color.

Validates:
- No color stack -> source -> pass-through topos_color -> output
- Has color stack -> source -> topos_color -> output
- Output port name is "image" (not "input")
- Graph reload preserves connections
"""

import pytest
from src.features.edit.core.composite_graph_data import CompositeGraphData


class TestDefaultGraphToposColorContract:
    """Tests for default graph creation with Topos Color support."""

    def test_no_color_stack_source_to_pass_through_topos_color_to_output(self):
        """Without color stack: TimelineClipInput -> pass-through Topos Color -> Output."""
        graph = CompositeGraphData.create_default_for_clip(
            clip_id="clip_001",
            asset_id="asset_001",
            asset_path="/path/to/clip.mp4",
        )

        types = {n["type"] for n in graph.nodes}
        assert "timeline_clip_input" in types
        assert "output" in types
        assert "topos_color" in types
        assert len(graph.nodes) == 3
        assert len(graph.connections) == 2

        topos_node = next(n for n in graph.nodes if n["type"] == "topos_color")
        params = topos_node["parameters"]
        assert params["stack_source"] == "clip_stack"
        assert params["color_stack_id"] == ""
        assert params["bypass"] is True  # pass-through mode when no stack

    def test_has_color_stack_source_to_topos_color_to_output(self):
        """With color stack: TimelineClipInput -> Topos Color -> Output."""
        graph = CompositeGraphData.create_default_for_clip(
            clip_id="clip_001",
            asset_id="asset_001",
            asset_path="/path/to/clip.mp4",
            color_stack_id="stack_abc123",
        )

        types = {n["type"] for n in graph.nodes}
        assert "timeline_clip_input" in types
        assert "topos_color" in types
        assert "output" in types
        assert len(graph.nodes) == 3
        assert len(graph.connections) == 2

    def test_output_port_name_is_image(self):
        """All connections must use 'image' as destination port on Output node."""
        graph = CompositeGraphData.create_default_for_clip(
            clip_id="clip_001",
            asset_id="asset_001",
            asset_path="/path/to/clip.mp4",
            color_stack_id="stack_abc123",
        )
        for conn in graph.connections:
            output_node = next(
                n for n in graph.nodes if n["type"] == "output"
            )
            if conn["dst_node_id"] == output_node["id"]:
                assert conn["dst_port_name"] == "image", \
                    f"Output port should be 'image', got '{conn['dst_port_name']}'"

    def test_topos_color_receives_from_source(self):
        """Topos Color's input comes from TimelineClipInput."""
        graph = CompositeGraphData.create_default_for_clip(
            clip_id="clip_001",
            asset_id="asset_001",
            asset_path="/path/to/clip.mp4",
            color_stack_id="stack_abc123",
        )

        source_node = next(n for n in graph.nodes if n["type"] == "timeline_clip_input")
        topos_node = next(n for n in graph.nodes if n["type"] == "topos_color")

        conn_to_topos = next(
            c for c in graph.connections
            if c["dst_node_id"] == topos_node["id"]
        )
        assert conn_to_topos["src_node_id"] == source_node["id"]
        assert conn_to_topos["src_port_name"] == "output"
        assert conn_to_topos["dst_port_name"] == "image"

    def test_topos_color_sends_to_output(self):
        """Topos Color's output goes to Output node."""
        graph = CompositeGraphData.create_default_for_clip(
            clip_id="clip_001",
            asset_id="asset_001",
            asset_path="/path/to/clip.mp4",
            color_stack_id="stack_abc123",
        )

        topos_node = next(n for n in graph.nodes if n["type"] == "topos_color")
        output_node = next(n for n in graph.nodes if n["type"] == "output")

        conn_to_output = next(
            c for c in graph.connections
            if c["dst_node_id"] == output_node["id"]
        )
        assert conn_to_output["src_node_id"] == topos_node["id"]
        assert conn_to_output["src_port_name"] == "result"

    def test_topos_color_default_parameters(self):
        """Topos Color node has correct default parameters."""
        graph = CompositeGraphData.create_default_for_clip(
            clip_id="clip_001",
            asset_id="asset_001",
            asset_path="/path/to/clip.mp4",
            color_stack_id="stack_abc123",
        )

        topos_node = next(n for n in graph.nodes if n["type"] == "topos_color")
        params = topos_node["parameters"]
        assert params["stack_source"] == "clip_stack"
        assert params["color_stack_id"] == "stack_abc123"
        assert params["bypass"] is False
        assert params["intensity"] == 1.0

    def test_input_stage_is_source_raw(self):
        """TimelineClipInput uses source_raw, not after_color."""
        graph = CompositeGraphData.create_default_for_clip(
            clip_id="clip_001",
            asset_id="asset_001",
            asset_path="/path/to/clip.mp4",
        )

        source_node = next(n for n in graph.nodes if n["type"] == "timeline_clip_input")
        assert source_node["parameters"]["input_stage"] == "source_raw"

    def test_input_stage_source_raw_with_color_stack(self):
        """Even with color stack, input_stage is source_raw."""
        graph = CompositeGraphData.create_default_for_clip(
            clip_id="clip_001",
            asset_id="asset_001",
            asset_path="/path/to/clip.mp4",
            color_stack_id="stack_abc123",
        )

        source_node = next(n for n in graph.nodes if n["type"] == "timeline_clip_input")
        assert source_node["parameters"]["input_stage"] == "source_raw"

    def test_graph_serialization_roundtrip(self):
        """Graph survives to_dict/from_dict roundtrip."""
        graph = CompositeGraphData.create_default_for_clip(
            clip_id="clip_001",
            asset_id="asset_001",
            asset_path="/path/to/clip.mp4",
            color_stack_id="stack_abc123",
        )

        d = graph.to_dict()
        restored = CompositeGraphData.from_dict(d)

        assert restored.composite_id == graph.composite_id
        assert len(restored.nodes) == 3
        assert len(restored.connections) == 2
        types = {n["type"] for n in restored.nodes}
        assert "topos_color" in types
