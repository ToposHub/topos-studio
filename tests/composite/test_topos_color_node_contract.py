"""
Contract tests for ToposColorNode.

Validates:
- Default parameters are correct
- Ports are correct (image input, result output)
- to_dict/from_dict roundtrip preserves all fields
- NODE_REGISTRY can create the node
- Node capabilities are properly defined
"""

import pytest
from src.features.composite.nodes.topos_color_node import ToposColorNode
from src.features.composite.core.node_engine import NODE_REGISTRY, NODE_CATEGORIES
from src.features.composite.core.node_capabilities import (
    get_node_capability,
    is_effect_node_type,
    is_fusible,
    get_bypass_mapping,
)
from src.features.composite.ui.composite_page import CompositePage


class TestToposColorNodeContract:
    """Contract tests for ToposColorNode data model."""

    def test_node_type_and_title(self):
        node = ToposColorNode()
        assert node.node_type == "topos_color"
        assert node.node_title == "Topos Color"
        assert node.header_color == "#cc6600"

    def test_default_parameters(self):
        node = ToposColorNode()
        assert node.parameters["stack_source"] == "clip_stack"
        assert node.parameters["color_stack_id"] == ""
        assert node.parameters["preset_id"] == ""
        assert node.parameters["embedded_stack"] is None
        assert node.parameters["bypass"] is False
        assert node.parameters["intensity"] == 1.0

    def test_interactive_node_is_independent_final_grade(self):
        """A manually added node must not inherit the timeline clip stack."""
        node = ToposColorNode()

        CompositePage._initialize_new_topos_color_node(object(), node)

        assert node.parameters["role"] == "composite_final_grade"
        assert node.parameters["linked"] is False
        assert node.parameters["stack_source"] == "embedded"
        assert node.parameters["color_stack_id"] == ""

    def test_primary_node_role_survives_default_graph_factory(self):
        """The Comp Clip factory marks its bound node as the primary grade."""
        from src.features.edit.core.composite_graph_data import CompositeGraphData

        graph = CompositeGraphData.create_default_for_clip(
            clip_id="clip_001",
            asset_id="asset_001",
            asset_path="/path/to/clip.mp4",
            timeline_id="timeline_001",
            color_stack_id="stack_001",
        )
        node = next(n for n in graph.nodes if n["type"] == "topos_color")

        assert node["parameters"]["role"] == "primary_clip_grade"
        assert node["parameters"]["color_stack_id"] == "stack_001"
        assert node["parameters"]["source_clip_id"] == "clip_001"
        assert node["parameters"]["source_timeline_id"] == "timeline_001"
        assert node["parameters"]["linked"] is True

    def test_ports(self):
        node = ToposColorNode()
        input_names = [p.name for p in node.inputs]
        output_names = [p.name for p in node.outputs]
        assert "image" in input_names
        assert "result" in output_names
        assert len(node.inputs) == 2  # image + CP-044 通用 mask
        assert len(node.outputs) == 1

    def test_to_dict_from_dict_roundtrip(self):
        node = ToposColorNode()
        node.set_parameter("color_stack_id", "stack_123")
        node.set_parameter("intensity", 0.7)

        d = node.to_dict()
        assert d["type"] == "topos_color"
        assert d["parameters"]["color_stack_id"] == "stack_123"
        assert d["parameters"]["intensity"] == 0.7

        # Restore from dict
        restored = ToposColorNode.from_dict(d)
        assert restored.node_type == "topos_color"
        assert restored.parameters["color_stack_id"] == "stack_123"
        assert restored.parameters["intensity"] == 0.7
        assert restored.parameters["stack_source"] == "clip_stack"

    def test_registry_contains_topos_color(self):
        assert "topos_color" in NODE_REGISTRY
        assert NODE_REGISTRY["topos_color"] is ToposColorNode

    def test_registry_creates_node(self):
        node_cls = NODE_REGISTRY["topos_color"]
        node = node_cls()
        assert isinstance(node, ToposColorNode)
        assert node.node_type == "topos_color"

    def test_in_color_category(self):
        color_nodes = [nt for nt, _ in NODE_CATEGORIES.get("Color", [])]
        assert "topos_color" in color_nodes

    def test_capabilities(self):
        cap = get_node_capability("topos_color")
        assert cap is not None
        assert cap.action_type == "effect"
        assert cap.effect_type == "topos_color"
        assert cap.roi_policy == "identity"
        assert cap.owns_color_texture is True
        # P0-09: the fused mega-shader has no topos_color stage, so the
        # node cannot actually fuse; the registry now reflects that.
        assert cap.fusible is False
        assert cap.bypass_mapping == {"result": "image"}

    def test_is_effect_node(self):
        assert is_effect_node_type("topos_color") is True

    def test_is_fusible(self):
        assert is_fusible("topos_color") is False

    def test_bypass_mapping(self):
        mapping = get_bypass_mapping("topos_color")
        assert mapping == {"result": "image"}

    def test_search_aliases(self):
        assert hasattr(ToposColorNode, "SEARCH_ALIASES")
        aliases = ToposColorNode.SEARCH_ALIASES
        assert "toposcolor" in aliases
        assert "colorstack" in aliases

    def test_metadata(self):
        assert hasattr(ToposColorNode, "DESCRIPTION")
        assert hasattr(ToposColorNode, "TAGS")
        assert "color" in ToposColorNode.TAGS
