"""
Tests for GraphSnapshot — pure logic, no GL required.

Covers:
- Deep copy of parameters (mutation isolation)
- OutputNode custom params appear in snapshot
- Snapshot helpers (get_input_connections, get_output_connections)
- Generation counter captured correctly
- eval_order is topologically correct
- color_revision tracking
"""

import sys
import os
import copy
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.core.graph_snapshot import GraphSnapshot, NodeSnapshot, ConnectionSnapshot
from src.features.composite.nodes.color_node import COLORNode
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.nodes.blur_node import BlurNode
from src.features.composite.nodes.grade_node import GradeNode
from src.features.composite.nodes.blend_node import BlendNode
from src.features.composite.nodes.constant_node import ConstantNode
from src.features.composite.nodes.group_node import GroupNode

import pytest
pytestmark = pytest.mark.composite_core


class TestParameterDeepCopy:
    """Snapshot parameters must be independent of the original node."""

    def test_modifying_node_params_does_not_affect_snapshot(self):
        g = NodeGraph()
        grade = GradeNode()
        g.add_node(grade)

        snap = g.snapshot()

        # Modify the node's parameters
        grade.parameters["lift_r"] = 0.9

        # Snapshot should still have the original value
        snap_params = snap.nodes[grade.id].parameters
        assert snap_params["lift_r"] != 0.9

    def test_modifying_snapshot_params_does_not_affect_node(self):
        g = NodeGraph()
        grade = GradeNode()
        g.add_node(grade)

        snap = g.snapshot()

        # Modify the snapshot's parameters (it's a dataclass dict)
        snap.nodes[grade.id].parameters["lift_r"] = 0.9

        # Original node should be unaffected
        assert grade.parameters["lift_r"] == 0.0

    def test_deep_copy_with_nested_params(self):
        """Parameters that are lists/dicts should be deep-copied."""
        g = NodeGraph()
        output = OutputNode()
        g.add_node(output)

        # OutputNode has format_mode, width, height
        snap = g.snapshot()
        assert snap.nodes[output.id].parameters["format_mode"] == "input"
        assert snap.nodes[output.id].parameters["width"] == 1920

    def test_two_snapshots_are_independent(self):
        g = NodeGraph()
        blur = BlurNode()
        g.add_node(blur)

        snap1 = g.snapshot()
        g.set_node_parameter(blur.id, "radius_x", 50.0)
        snap2 = g.snapshot()

        assert snap1.nodes[blur.id].parameters["radius_x"] != 50.0
        assert snap2.nodes[blur.id].parameters["radius_x"] == 50.0


class TestOutputNodeParams:
    """OutputNode custom parameters (format_mode, width, height) appear in snapshot."""

    def test_default_params_in_snapshot(self):
        g = NodeGraph()
        g.create_default_graph()
        snap = g.snapshot()

        out_id = [nid for nid, n in g.nodes.items() if isinstance(n, OutputNode)][0]
        ns = snap.nodes[out_id]

        # Output node owns the authoritative composition resolution: the
        # default graph seeds it in custom mode.
        assert ns.parameters["format_mode"] == "custom"
        assert ns.parameters["width"] == 1920
        assert ns.parameters["height"] == 1080

    def test_custom_resolution_in_snapshot(self):
        g = NodeGraph()
        g.create_default_graph()
        out_id = [nid for nid, n in g.nodes.items() if isinstance(n, OutputNode)][0]

        g.set_node_parameter(out_id, "format_mode", "custom")
        g.set_node_parameter(out_id, "width", 3840)
        g.set_node_parameter(out_id, "height", 2160)

        snap = g.snapshot()
        ns = snap.nodes[out_id]

        assert ns.parameters["format_mode"] == "custom"
        assert ns.parameters["width"] == 3840
        assert ns.parameters["height"] == 2160


class TestSnapshotHelpers:
    """get_input_connections, get_output_connections on GraphSnapshot."""

    def _make_graph_with_merge(self):
        g = NodeGraph()
        color = COLORNode()
        blur = BlurNode()
        merge = BlendNode()
        output = OutputNode()
        for n in [color, blur, merge, output]:
            g.add_node(n)
        g.connect(color.id, "result", blur.id, "image")
        g.connect(blur.id, "result", merge.id, "A")
        g.connect(color.id, "result", merge.id, "B")
        g.connect(merge.id, "result", output.id, "image")
        return g, color, blur, merge, output

    def test_get_input_connections(self):
        g, color, blur, merge, output = self._make_graph_with_merge()
        snap = g.snapshot()

        # Merge has two inputs
        merge_inputs = snap.get_input_connections(merge.id)
        assert len(merge_inputs) == 2
        port_names = {c.dst_port_name for c in merge_inputs}
        assert port_names == {"A", "B"}

    def test_get_output_connections(self):
        g, color, blur, merge, output = self._make_graph_with_merge()
        snap = g.snapshot()

        # COLOR has two outputs (fan-out)
        color_outputs = snap.get_output_connections(color.id)
        assert len(color_outputs) == 2

    def test_no_connections_returns_empty(self):
        g = NodeGraph()
        blur = BlurNode()
        g.add_node(blur)
        snap = g.snapshot()

        assert snap.get_input_connections(blur.id) == []
        assert snap.get_output_connections(blur.id) == []


class TestSnapshotGeneration:
    """Generation counter is captured in snapshot."""

    def test_generation_matches_graph(self):
        g = NodeGraph()
        g.create_default_graph()
        snap = g.snapshot()
        assert snap.generation == g.generation

    def test_generation_advances_with_mutations(self):
        g = NodeGraph()
        g.create_default_graph()
        snap1 = g.snapshot()

        blur = BlurNode()
        g.add_node(blur)
        snap2 = g.snapshot()

        assert snap2.generation > snap1.generation


class TestSnapshotEvalOrder:
    """eval_order in snapshot is topologically correct."""

    def test_eval_order_is_topological(self):
        g = NodeGraph()
        color = COLORNode()
        blur = BlurNode()
        output = OutputNode()
        g.add_node(color)
        g.add_node(blur)
        g.add_node(output)
        g.connect(color.id, "result", blur.id, "image")
        g.connect(blur.id, "result", output.id, "image")

        snap = g.snapshot()
        assert len(snap.eval_order) == 3
        assert snap.eval_order.index(color.id) < snap.eval_order.index(blur.id)
        assert snap.eval_order.index(blur.id) < snap.eval_order.index(output.id)


class TestGroupSnapshotBoundaries:
    """Group ports are flattened in the immutable render topology."""

    def test_group_boundary_rewrites_input_and_output_edges(self):
        g = NodeGraph()
        source = ConstantNode(node_id="source")
        member = BlurNode(node_id="member")
        group = GroupNode(node_id="group")
        output = OutputNode(node_id="output")
        for node in (source, member, group, output):
            g.add_node(node)

        group.add_member(member.id)
        group.add_input_port("source", member.id, "image")
        group.add_output_port("result", member.id, "result")
        assert g.connect(source.id, "result", group.id, "source")
        assert g.connect(group.id, "result", output.id, "image")

        snapshot = g.snapshot()
        group_snapshot = snapshot.nodes[group.id]

        assert group_snapshot.group_member_node_ids == (member.id,)
        assert group_snapshot.group_input_mappings == {
            "source": (member.id, "image")}
        assert group_snapshot.group_output_mappings == {
            "result": (member.id, "result")}
        assert any(
            c.src_node_id == source.id and c.src_port_name == "result"
            and c.dst_node_id == member.id and c.dst_port_name == "image"
            for c in snapshot.connections)
        assert any(
            c.src_node_id == member.id and c.src_port_name == "result"
            and c.dst_node_id == output.id and c.dst_port_name == "image"
            for c in snapshot.connections)
        assert not any(c.src_node_id == group.id or c.dst_node_id == group.id
                        for c in snapshot.connections)
        assert snapshot.eval_order.index(source.id) < snapshot.eval_order.index(member.id)
        assert snapshot.eval_order.index(member.id) < snapshot.eval_order.index(output.id)


class TestSnapshotColorState:
    """COLOR node texture state (color_texture_id, color_revision) in snapshot."""

    def test_default_color_state(self):
        g = NodeGraph()
        g.create_default_graph()
        snap = g.snapshot()

        assert snap.color_texture_id == 0
        assert snap.color_width == 0
        assert snap.color_height == 0
        assert snap.color_revision == 0

    def test_custom_color_state(self):
        g = NodeGraph()
        g.create_default_graph()
        snap = g.snapshot(color_texture_id=42, color_width=1920,
                          color_height=1080, color_revision=5)

        assert snap.color_texture_id == 42
        assert snap.color_width == 1920
        assert snap.color_revision == 5


class TestNodeSnapshotFields:
    """NodeSnapshot captures all expected fields."""

    def test_node_snapshot_fields(self):
        g = NodeGraph()
        grade = GradeNode()
        g.add_node(grade)

        snap = g.snapshot()
        ns = snap.nodes[grade.id]

        assert ns.node_id == grade.id
        assert ns.node_type == "grade"
        assert isinstance(ns.parameters, dict)
        assert ns.disabled is False
        assert isinstance(ns.input_ports, list)
        assert isinstance(ns.output_ports, list)

    def test_input_port_tuples(self):
        g = NodeGraph()
        blur = BlurNode()
        g.add_node(blur)

        snap = g.snapshot()
        ns = snap.nodes[blur.id]

        # Blur has one input port "image" of type "image"
        assert ("image", "image") in ns.input_ports

    def test_output_port_tuples(self):
        g = NodeGraph()
        blur = BlurNode()
        g.add_node(blur)

        snap = g.snapshot()
        ns = snap.nodes[blur.id]

        # Blur has one output port "result" of type "image"
        assert ("result", "image") in ns.output_ports


class TestConnectionSnapshot:
    """ConnectionSnapshot fields."""

    def test_connection_fields(self):
        g = NodeGraph()
        g.create_default_graph()
        snap = g.snapshot()

        assert len(snap.connections) == 2
        # Check the connection to the output node (second connection)
        c = snap.connections[1]
        assert isinstance(c, ConnectionSnapshot)
        assert c.src_port_name == "result"
        assert c.dst_port_name == "image"


class TestDisabledInSnapshot:
    """Disabled state is captured in snapshot."""

    def test_disabled_node_in_snapshot(self):
        g = NodeGraph()
        blur = BlurNode()
        g.add_node(blur)
        g.set_node_disabled(blur.id, True)

        snap = g.snapshot()
        assert snap.nodes[blur.id].disabled is True
