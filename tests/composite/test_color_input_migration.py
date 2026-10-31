"""
Tests for migrating old color_input graphs to topos_color graphs.

Validates:
- Standalone-image color_input -> Output is NOT migrated (no timeline backing)
- Timeline color_input -> Output migrates to timeline_clip_input -> Topos Color -> Output
- Timeline color_input -> Output migrates to timeline_clip_input -> Output (no color_stack_id)
- Non-color_input nodes are untouched
"""

import pytest
from src.features.composite.core.node_engine import NodeGraph, NODE_CATEGORIES, NODE_REGISTRY
from src.features.composite.nodes.color_node import COLORNode
from src.features.composite.nodes.output_node import OutputNode


class TestColorInputMigration:
    """Tests for color_input graph migration."""

    def test_standalone_image_not_migrated(self):
        """Standalone-image color_input -> Output must NOT be migrated.

        A color_input with no timeline_clip_input in the graph is a
        single-image composite where color_input bridges to the Color page
        output. Migrating it to timeline_clip_input would break it (no
        timeline backing), so migration must be skipped.
        """
        graph = NodeGraph()
        color_node = COLORNode(node_id="color_1")
        output_node = OutputNode(node_id="output_1")
        graph.add_node(color_node)
        graph.add_node(output_node)
        graph.connect("color_1", "result", "output_1", "image")

        migrated = graph.migrate_color_input_to_topos_color(color_stack_id="stack_abc")

        assert migrated is False
        types = {n.node_type for n in graph.nodes.values()}
        # color_input is preserved unchanged
        assert "color_input" in types
        assert "output" in types
        assert "timeline_clip_input" not in types
        assert "topos_color" not in types
        assert len(graph.nodes) == 2

    def test_migrate_timeline_color_input_with_color_stack(self):
        """Timeline color_input migrates to timeline_clip_input -> topos_color -> output."""
        from src.features.composite.nodes.timeline_clip_input_node import TimelineClipInputNode

        graph = NodeGraph()
        # A timeline graph that still has a legacy color_input node
        color_node = COLORNode(node_id="color_1")
        source_node = TimelineClipInputNode(node_id="src_1")
        output_node = OutputNode(node_id="output_1")
        graph.add_node(color_node)
        graph.add_node(source_node)
        graph.add_node(output_node)
        graph.connect("color_1", "result", "output_1", "image")

        migrated = graph.migrate_color_input_to_topos_color(color_stack_id="stack_abc")

        assert migrated is True
        types = {n.node_type for n in graph.nodes.values()}
        assert "color_input" not in types
        assert "topos_color" in types
        assert "timeline_clip_input" in types
        assert "output" in types

        # Check topos_color params
        tc_node = next(n for n in graph.nodes.values() if n.node_type == "topos_color")
        assert tc_node.parameters["color_stack_id"] == "stack_abc"
        assert tc_node.parameters["stack_source"] == "clip_stack"

    def test_migrate_timeline_color_input_without_color_stack(self):
        """Timeline color_input migrates to timeline_clip_input -> topos_color(bypass) -> output."""
        from src.features.composite.nodes.timeline_clip_input_node import TimelineClipInputNode

        graph = NodeGraph()
        color_node = COLORNode(node_id="color_1")
        source_node = TimelineClipInputNode(node_id="src_1")
        output_node = OutputNode(node_id="output_1")
        graph.add_node(color_node)
        graph.add_node(source_node)
        graph.add_node(output_node)
        graph.connect("color_1", "result", "output_1", "image")

        migrated = graph.migrate_color_input_to_topos_color(color_stack_id="")

        assert migrated is True
        types = {n.node_type for n in graph.nodes.values()}
        assert "color_input" not in types
        # topos_color is always inserted; bypass=True when no stack
        assert "topos_color" in types
        assert "timeline_clip_input" in types

        # Check topos_color is in bypass mode
        tc_node = next(n for n in graph.nodes.values() if n.node_type == "topos_color")
        assert tc_node.parameters["bypass"] is True
        assert tc_node.parameters["color_stack_id"] == ""

    def test_no_migration_needed(self):
        """Graph without color_input should not be changed."""
        graph = NodeGraph()
        from src.features.composite.nodes.timeline_clip_input_node import TimelineClipInputNode
        source_node = TimelineClipInputNode(node_id="src_1")
        output_node = OutputNode(node_id="out_1")
        graph.add_node(source_node)
        graph.add_node(output_node)
        graph.connect("src_1", "output", "out_1", "image")

        migrated = graph.migrate_color_input_to_topos_color(color_stack_id="stack_123")

        assert migrated is False
        assert len(graph.nodes) == 2

    def test_color_input_hidden_from_categories(self):
        """color_input should not be in NODE_CATEGORIES (hidden from search)."""
        input_types = [nt for nt, _ in NODE_CATEGORIES.get("Input", [])]
        assert "color_input" not in input_types

    def test_color_input_still_in_registry(self):
        """color_input should still be in NODE_REGISTRY for loading old graphs."""
        assert "color_input" in NODE_REGISTRY
