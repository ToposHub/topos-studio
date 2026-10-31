"""A-11 tests: node color persistence + sync.

Verifies that custom node colors survive save/reload (P1) and that the
node-level color store is the single source of truth that
effective_header_color resolves.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.blur_node import BlurNode
from src.features.composite.nodes.transform_node import TransformNode


pytestmark = pytest.mark.composite_core


def test_default_color_round_trips_as_absent():
    """A node with no override saves no node_color and reloads with the class default."""
    node = BlurNode()
    default = node.header_color
    data = node.to_dict()
    assert "node_color" not in data

    restored = BlurNode.from_dict(data)
    assert restored.custom_header_color is None
    assert restored.header_color == default
    assert restored.effective_header_color() == default


def test_override_round_trips():
    node = BlurNode()
    node.custom_header_color = "#a1b2c3"
    data = node.to_dict()
    assert data["node_color"] == "#a1b2c3"

    restored = BlurNode.from_dict(data)
    assert restored.custom_header_color == "#a1b2c3"
    assert restored.header_color == "#a1b2c3"
    assert restored.effective_header_color() == "#a1b2c3"


def test_graph_round_trip_preserves_colors():
    g = NodeGraph()
    a = BlurNode()
    a.custom_header_color = "#111111"
    b = TransformNode()
    b.custom_header_color = "#222222"
    g.add_node(a)
    g.add_node(b)

    g2 = NodeGraph.from_dict(g.to_dict())
    assert g2.get_node(a.id).effective_header_color() == "#111111"
    assert g2.get_node(b.id).effective_header_color() == "#222222"


def test_snapshot_reports_effective_color():
    g = NodeGraph()
    node = BlurNode()
    node.custom_header_color = "#deadbe"
    g.add_node(node)
    snap = g.snapshot()
    # Snapshot exposes the resolved color so the Keyframes panel range bar
    # does not need to know about the override/default distinction.
    assert snap.nodes[node.id].node_color == "#deadbe"


def test_two_nodes_keep_distinct_colors():
    g = NodeGraph()
    a = BlurNode()
    b = BlurNode()
    a.custom_header_color = "#aaaaaa"
    g.add_node(a)
    g.add_node(b)
    # Setting a's color must not bleed into b.
    assert a.effective_header_color() == "#aaaaaa"
    assert b.effective_header_color() == b.header_color  # class default
