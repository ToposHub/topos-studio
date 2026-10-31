"""
Tests for NodeGraph engine — pure logic, no GL required.

Covers:
- Default graph creation (COLOR → Output)
- Adding/removing nodes
- Connection management (add, replace input, disconnect)
- Cycle detection
- Generation counter increments
- Serialization round-trip (to_dict / from_dict)
- Topological sort
"""

import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.color_node import COLORNode
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.nodes.blur_node import BlurNode
from src.features.composite.nodes.blend_node import BlendNode
from src.features.composite.nodes.grade_node import GradeNode
from src.features.composite.nodes.invert_node import InvertNode
from src.features.composite.nodes.constant_node import ConstantNode


class TestDefaultGraph:
    """Default graph: TimelineClipInput → Topos Color → Output."""

    def test_create_default_graph(self):
        g = NodeGraph()
        out_id = g.create_default_graph()

        assert len(g.nodes) == 3
        assert out_id in g.nodes
        assert isinstance(g.nodes[out_id], OutputNode)

        # Find the Topos Color node
        from src.features.composite.nodes.topos_color_node import ToposColorNode
        tc_nodes = [n for n in g.nodes.values() if isinstance(n, ToposColorNode)]
        assert len(tc_nodes) == 1

        # Find the TimelineClipInput node
        from src.features.composite.nodes.timeline_clip_input_node import TimelineClipInputNode
        src_nodes = [n for n in g.nodes.values() if isinstance(n, TimelineClipInputNode)]
        assert len(src_nodes) == 1

    def test_default_graph_has_two_connections(self):
        g = NodeGraph()
        g.create_default_graph()
        assert len(g.connections) == 2

    def test_default_graph_output_has_input(self):
        g = NodeGraph()
        out_id = g.create_default_graph()
        assert g.has_connected_output()

    def test_default_graph_topological_order(self):
        g = NodeGraph()
        g.create_default_graph()
        order = g.topological_sort()
        assert len(order) == 3
        # TimelineClipInput → Topos Color → Output
        from src.features.composite.nodes.timeline_clip_input_node import TimelineClipInputNode
        from src.features.composite.nodes.topos_color_node import ToposColorNode
        src_id = [nid for nid, n in g.nodes.items() if isinstance(n, TimelineClipInputNode)][0]
        tc_id = [nid for nid, n in g.nodes.items() if isinstance(n, ToposColorNode)][0]
        out_id = [nid for nid, n in g.nodes.items() if isinstance(n, OutputNode)][0]
        assert order.index(src_id) < order.index(tc_id)
        assert order.index(tc_id) < order.index(out_id)


class TestNodeManagement:
    """Add, remove, get nodes."""

    def test_add_node_increments_generation(self):
        g = NodeGraph()
        gen0 = g.generation
        g.add_node(COLORNode())
        assert g.generation == gen0 + 1

    def test_remove_node(self):
        g = NodeGraph()
        color = COLORNode()
        g.add_node(color)
        assert len(g.nodes) == 1

        removed = g.remove_node(color.id)
        assert removed is color
        assert len(g.nodes) == 0
        assert len(g.connections) == 0

    def test_remove_node_increments_generation(self):
        g = NodeGraph()
        color = COLORNode()
        g.add_node(color)
        gen0 = g.generation
        g.remove_node(color.id)
        assert g.generation == gen0 + 1

    def test_remove_nonexistent_node(self):
        g = NodeGraph()
        result = g.remove_node("nonexistent")
        assert result is None

    def test_remove_node_cleans_connections(self):
        g = NodeGraph()
        g.create_default_graph()
        assert len(g.connections) == 2

        # Remove Topos Color node — both its connections should disappear
        from src.features.composite.nodes.topos_color_node import ToposColorNode
        tc_id = [nid for nid, n in g.nodes.items() if isinstance(n, ToposColorNode)][0]
        g.remove_node(tc_id)
        assert len(g.connections) == 0

    def test_get_node(self):
        g = NodeGraph()
        color = COLORNode()
        g.add_node(color)
        assert g.get_node(color.id) is color
        assert g.get_node("nonexistent") is None

    def test_set_node_parameter(self):
        g = NodeGraph()
        grade = GradeNode()
        g.add_node(grade)

        gen0 = g.generation
        result = g.set_node_parameter(grade.id, "lift_r", 0.5)
        assert result is True
        assert grade.parameters["lift_r"] == 0.5
        assert g.generation == gen0 + 1

    def test_set_parameter_nonexistent_node(self):
        g = NodeGraph()
        result = g.set_node_parameter("nonexistent", "key", "val")
        assert result is False

    def test_set_node_disabled(self):
        g = NodeGraph()
        blur = BlurNode()
        g.add_node(blur)

        gen0 = g.generation
        assert g.set_node_disabled(blur.id, True) is True
        assert blur.disabled is True
        assert g.generation == gen0 + 1


class TestConnections:
    """Connect, disconnect, input replacement."""

    def test_connect_two_nodes(self):
        g = NodeGraph()
        color = COLORNode()
        output = OutputNode()
        g.add_node(color)
        g.add_node(output)

        assert g.connect(color.id, "result", output.id, "image") is True
        assert len(g.connections) == 1
        assert g.connections[0].src_node_id == color.id
        assert g.connections[0].dst_port_name == "image"

    def test_connect_increments_generation(self):
        g = NodeGraph()
        color = COLORNode()
        output = OutputNode()
        g.add_node(color)
        g.add_node(output)
        gen0 = g.generation

        g.connect(color.id, "result", output.id, "image")
        assert g.generation == gen0 + 1

    def test_connect_replaces_existing_input(self):
        """Connecting to an already-connected input port replaces the old connection."""
        g = NodeGraph()
        color1 = COLORNode()
        color2 = COLORNode()
        output = OutputNode()
        g.add_node(color1)
        g.add_node(color2)
        g.add_node(output)

        # First connection
        assert g.connect(color1.id, "result", output.id, "image") is True
        assert len(g.connections) == 1

        # Replace with second COLOR node
        assert g.connect(color2.id, "result", output.id, "image") is True
        assert len(g.connections) == 1  # Still 1, old one replaced
        assert g.connections[0].src_node_id == color2.id

    def test_connect_nonexistent_node(self):
        g = NodeGraph()
        color = COLORNode()
        g.add_node(color)
        assert g.connect(color.id, "result", "nonexistent", "image") is False

    def test_connect_nonexistent_port(self):
        g = NodeGraph()
        color = COLORNode()
        output = OutputNode()
        g.add_node(color)
        g.add_node(output)
        assert g.connect(color.id, "nonexistent", output.id, "image") is False

    def test_disconnect(self):
        g = NodeGraph()
        g.create_default_graph()
        from src.features.composite.nodes.topos_color_node import ToposColorNode
        tc_id = [nid for nid, n in g.nodes.items() if isinstance(n, ToposColorNode)][0]
        out_id = [nid for nid, n in g.nodes.items() if isinstance(n, OutputNode)][0]

        assert g.disconnect(tc_id, "result", out_id, "image") is True
        assert len(g.connections) == 1  # source->topos_color still connected

    def test_disconnect_nonexistent(self):
        g = NodeGraph()
        assert g.disconnect("a", "result", "b", "image") is False

    def test_disconnect_increments_generation(self):
        g = NodeGraph()
        g.create_default_graph()
        from src.features.composite.nodes.topos_color_node import ToposColorNode
        tc_id = [nid for nid, n in g.nodes.items() if isinstance(n, ToposColorNode)][0]
        out_id = [nid for nid, n in g.nodes.items() if isinstance(n, OutputNode)][0]
        gen0 = g.generation

        g.disconnect(tc_id, "result", out_id, "image")
        assert g.generation == gen0 + 1

    def test_disconnect_by_dst(self):
        g = NodeGraph()
        color = COLORNode()
        blur = BlurNode()
        g.add_node(color)
        g.add_node(blur)
        g.connect(color.id, "result", blur.id, "image")
        assert len(g.connections) == 1

        gen0 = g.generation
        g.disconnect_by_dst(blur.id, "image")
        assert len(g.connections) == 0
        assert g.generation == gen0 + 1

    def test_get_connections_for_node(self):
        g = NodeGraph()
        g.create_default_graph()
        from src.features.composite.nodes.topos_color_node import ToposColorNode
        tc_id = [nid for nid, n in g.nodes.items() if isinstance(n, ToposColorNode)][0]
        conns = g.get_connections_for_node(tc_id)
        assert len(conns) == 2  # input from source + output to output

    def test_get_input_connections(self):
        g = NodeGraph()
        g.create_default_graph()
        out_id = [nid for nid, n in g.nodes.items() if isinstance(n, OutputNode)][0]
        conns = g.get_input_connections(out_id)
        assert len(conns) == 1
        assert conns[0].dst_port_name == "image"

    def test_get_output_connections(self):
        g = NodeGraph()
        g.create_default_graph()
        from src.features.composite.nodes.timeline_clip_input_node import TimelineClipInputNode
        src_id = [nid for nid, n in g.nodes.items() if isinstance(n, TimelineClipInputNode)][0]
        conns = g.get_output_connections(src_id)
        assert len(conns) == 1
        assert conns[0].src_port_name == "output"


class TestCycleDetection:
    """_would_create_cycle prevents cycles."""

    def _make_chain(self, g, count):
        """Create a chain of blur nodes: blur0 → blur1 → ... → blurN."""
        blurs = [BlurNode() for _ in range(count)]
        for b in blurs:
            g.add_node(b)
        for i in range(count - 1):
            g.connect(blurs[i].id, "result", blurs[i + 1].id, "image")
        return blurs

    def test_self_loop_detected(self):
        g = NodeGraph()
        blur = BlurNode()
        g.add_node(blur)
        assert g.connect(blur.id, "result", blur.id, "image") is False

    def test_simple_cycle_detected(self):
        g = NodeGraph()
        b1 = BlurNode()
        b2 = BlurNode()
        g.add_node(b1)
        g.add_node(b2)

        assert g.connect(b1.id, "result", b2.id, "image") is True
        # Reverse connection would create cycle
        assert g.connect(b2.id, "result", b1.id, "image") is False

    def test_longer_cycle_detected(self):
        g = NodeGraph()
        blurs = self._make_chain(g, 4)

        # Close the loop: blur3 → blur0 (would create cycle)
        assert g.connect(blurs[3].id, "result", blurs[0].id, "image") is False

    def test_no_cycle_allows_connection(self):
        g = NodeGraph()
        color = COLORNode()
        b1 = BlurNode()
        b2 = BlurNode()
        g.add_node(color)
        g.add_node(b1)
        g.add_node(b2)

        assert g.connect(color.id, "result", b1.id, "image") is True
        assert g.connect(color.id, "result", b2.id, "image") is True  # Fan-out is fine

    def test_cycle_detection_preserves_existing_connections(self):
        """If a connection would create a cycle, existing connections remain unchanged."""
        g = NodeGraph()
        b1 = BlurNode()
        b2 = BlurNode()
        g.add_node(b1)
        g.add_node(b2)
        g.connect(b1.id, "result", b2.id, "image")
        assert len(g.connections) == 1

        # Try to create cycle
        g.connect(b2.id, "result", b1.id, "image")
        # Original connection should still exist
        assert len(g.connections) == 1
        assert g.connections[0].src_node_id == b1.id


class TestTopologicalSort:
    """Kahn's algorithm correctness."""

    def test_linear_chain(self):
        g = NodeGraph()
        color = COLORNode()
        blur = BlurNode()
        output = OutputNode()
        g.add_node(color)
        g.add_node(blur)
        g.add_node(output)
        g.connect(color.id, "result", blur.id, "image")
        g.connect(blur.id, "result", output.id, "image")

        order = g.topological_sort()
        assert order.index(color.id) < order.index(blur.id)
        assert order.index(blur.id) < order.index(output.id)

    def test_diamond_shape(self):
        """COLOR → BlurA → Merge, COLOR → BlurB → Merge"""
        g = NodeGraph()
        color = COLORNode()
        blur_a = BlurNode()
        blur_b = BlurNode()
        merge = BlendNode()
        g.add_node(color)
        g.add_node(blur_a)
        g.add_node(blur_b)
        g.add_node(merge)

        g.connect(color.id, "result", blur_a.id, "image")
        g.connect(color.id, "result", blur_b.id, "image")
        g.connect(blur_a.id, "result", merge.id, "A")
        g.connect(blur_b.id, "result", merge.id, "B")

        order = g.topological_sort()
        # COLOR before both blurs, both blurs before merge
        assert order.index(color.id) < order.index(blur_a.id)
        assert order.index(color.id) < order.index(blur_b.id)
        assert order.index(blur_a.id) < order.index(merge.id)
        assert order.index(blur_b.id) < order.index(merge.id)

    def test_cycle_raises(self):
        """Manually injected cycle should cause topological_sort to raise."""
        g = NodeGraph()
        b1 = BlurNode()
        b2 = BlurNode()
        g.add_node(b1)
        g.add_node(b2)
        # Manually add connections that form a cycle (bypass _would_create_cycle)
        from src.features.composite.nodes.base_node import Connection
        g.connections.append(Connection(b1.id, "result", b2.id, "image"))
        g.connections.append(Connection(b2.id, "result", b1.id, "image"))

        with pytest.raises(ValueError, match="cycle"):
            g.topological_sort()

    def test_remove_node_bumps_topology_revision(self):
        """Removal must invalidate render-scheduler input connection tracking."""
        g = NodeGraph()
        color = COLORNode()
        output = OutputNode()
        g.add_node(color)
        g.add_node(output)
        g.connect(color.id, "result", output.id, "image")
        before = g.snapshot().topology_revision

        g.remove_node(color.id)

        assert g.snapshot().topology_revision == before + 1


class TestSerialization:
    """to_dict / from_dict round-trip."""

    def test_default_graph_roundtrip(self):
        g = NodeGraph()
        g.create_default_graph()
        data = g.to_dict()

        g2 = NodeGraph.from_dict(data)
        assert len(g2.nodes) == 3
        assert len(g2.connections) == 2

        # Check node types survived
        types = {n.node_type for n in g2.nodes.values()}
        assert "timeline_clip_input" in types
        assert "topos_color" in types
        assert "output" in types

    def test_complex_graph_roundtrip(self):
        g = NodeGraph()
        color = COLORNode()
        blur = BlurNode()
        grade = GradeNode()
        merge = BlendNode()
        output = OutputNode()
        for n in [color, blur, grade, merge, output]:
            g.add_node(n)

        g.connect(color.id, "result", blur.id, "image")
        g.connect(color.id, "result", grade.id, "image")
        g.connect(blur.id, "result", merge.id, "A")
        g.connect(grade.id, "result", merge.id, "B")
        g.connect(merge.id, "result", output.id, "image")

        # Modify some params
        g.set_node_parameter(blur.id, "radius", 20.0)
        g.set_node_parameter(grade.id, "lift_r", 0.3)
        g.set_node_parameter(merge.id, "blend_mode", "screen")

        data = g.to_dict()
        g2 = NodeGraph.from_dict(data)

        # Standalone-image graph (no timeline_clip_input): migration is
        # skipped (babcd9aa) — a migrated timeline_clip_input would have no
        # timeline backing and render empty. 5 nodes stay 5.
        assert len(g2.nodes) == 5
        assert len(g2.connections) == 5

        # Check parameters survived
        blur2 = g2.get_node(blur.id)
        assert blur2 is not None
        assert blur2.parameters["radius"] == 20.0

        grade2 = g2.get_node(grade.id)
        assert grade2 is not None
        assert grade2.parameters["lift_r"] == 0.3

        merge2 = g2.get_node(merge.id)
        assert merge2 is not None
        assert merge2.parameters["blend_mode"] == "screen"

    def test_color_input_migrates_with_timeline_source(self):
        """带 timeline_clip_input 的图：color_input 迁移为 topos_color 链。"""
        from src.features.composite.nodes.timeline_clip_input_node import (
            TimelineClipInputNode)
        g = NodeGraph()
        color = COLORNode()
        blur = BlurNode()
        source = TimelineClipInputNode()
        for n in [source, color, blur]:
            g.add_node(n)
        g.connect(color.id, "result", blur.id, "image")

        g2 = NodeGraph.from_dict(g.to_dict())

        # color_input(1) 被移除，插入 topos_color(1)：
        # source + blur + tc = 3 节点；source->tc, tc->blur = 2 连接
        assert len(g2.nodes) == 3
        assert len(g2.connections) == 2
        types = {n.node_type for n in g2.nodes.values()}
        assert "color_input" not in types
        assert "topos_color" in types

    def test_disabled_state_survives_roundtrip(self):
        g = NodeGraph()
        blur = BlurNode()
        g.add_node(blur)
        g.set_node_disabled(blur.id, True)

        data = g.to_dict()
        g2 = NodeGraph.from_dict(data)

        blur2 = g2.get_node(blur.id)
        assert blur2 is not None
        assert blur2.disabled is True

    def test_node_positions_survive_roundtrip(self):
        g = NodeGraph()
        blur = BlurNode()
        blur.position = (250.0, 400.0)
        g.add_node(blur)

        data = g.to_dict()
        g2 = NodeGraph.from_dict(data)

        blur2 = g2.get_node(blur.id)
        assert blur2 is not None
        assert blur2.position == (250.0, 400.0)

    def test_missing_param_uses_node_default(self):
        """If a saved param is missing, the node's __init__ default is used."""
        g = NodeGraph()
        output = OutputNode()
        g.add_node(output)
        data = g.to_dict()

        # Remove 'height' from saved data to simulate an older save
        for node_data in data["nodes"]:
            if node_data.get("type") == "output":
                del node_data["parameters"]["height"]

        g2 = NodeGraph.from_dict(data)
        out2 = g2.get_node(output.id)
        # OutputNode sets height=1080 in __init__, and from_dict doesn't override
        # what's missing — so height stays at the node's default (1080)
        assert out2.parameters.get("height") == 1080


class TestValidation:
    """NodeGraph.validate()."""

    def test_valid_default_graph(self):
        g = NodeGraph()
        g.create_default_graph()
        errors = g.validate()
        assert errors == []

    def test_output_without_input_is_error(self):
        g = NodeGraph()
        g.add_node(OutputNode())
        errors = g.validate()
        assert len(errors) == 1
        assert "no input" in errors[0].lower() or "Output" in errors[0]


class TestUserLabel:
    """B4: per-node optional user_label (canvas rename)."""

    def test_display_title_prefers_user_label(self):
        g = NodeGraph()
        g.create_default_graph()
        node = next(iter(g.nodes.values()))
        auto_title = node.display_title  # e.g. "Blur" / "Blur1"
        node.user_label = "My Grade"
        assert node.display_title == "My Grade"
        node.user_label = None
        assert node.display_title == auto_title

    def test_user_label_round_trip(self):
        g = NodeGraph()
        g.create_default_graph()
        node = next(iter(g.nodes.values()))
        node.user_label = "已命名节点"
        data = g.to_dict()
        g2 = NodeGraph.from_dict(data)
        assert g2.nodes[node.id].user_label == "已命名节点"
        assert g2.nodes[node.id].display_title == "已命名节点"

    def test_old_graph_without_user_label_loads(self):
        g = NodeGraph()
        g.create_default_graph()
        data = g.to_dict()
        for nd in data["nodes"]:
            nd.pop("user_label", None)
        g2 = NodeGraph.from_dict(data)
        for node in g2.nodes.values():
            assert node.user_label is None

    def test_saved_version_is_current(self):
        g = NodeGraph()
        g.create_default_graph()
        data = g.to_dict()
        from src.features.composite.core.graph_migration import COMPOSITE_SCHEMA_VERSION
        assert data["composite_schema_version"] == COMPOSITE_SCHEMA_VERSION


class TestUserLabelMigration:
    """v10 → v11 migration normalises user_label values."""

    def test_migrate_v10_to_v11_keeps_valid_label(self):
        from src.features.composite.core.graph_migration import migrate_v10_to_v11
        data = {"nodes": [{"id": "a", "type": "blur", "user_label": "Keep Me"}]}
        out = migrate_v10_to_v11(data)
        assert out["nodes"][0]["user_label"] == "Keep Me"
        # Input not mutated
        assert "user_label" in data["nodes"][0]

    def test_migrate_v10_to_v11_drops_invalid_label(self):
        from src.features.composite.core.graph_migration import migrate_v10_to_v11
        data = {"nodes": [
            {"id": "a", "type": "blur", "user_label": "   "},
            {"id": "b", "type": "blur", "user_label": 42},
            {"id": "c", "type": "blur"},
        ]}
        out = migrate_v10_to_v11(data)
        assert all("user_label" not in nd for nd in out["nodes"])

    def test_migrate_graph_bumps_v10_to_current(self):
        from src.features.composite.core.graph_migration import (
            migrate_graph, COMPOSITE_SCHEMA_VERSION)
        data = {
            "composite_schema_version": 10,
            "nodes": [{"id": "a", "type": "blur", "user_label": "X"}],
        }
        out = migrate_graph(data)
        assert out["composite_schema_version"] == COMPOSITE_SCHEMA_VERSION
        assert out["nodes"][0]["user_label"] == "X"
