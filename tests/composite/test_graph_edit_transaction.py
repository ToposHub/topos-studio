"""B02: reason-coded atomic graph edits and exact-delta undo.

``NodeGraph.apply_edit`` stages a candidate edge list, validates it
completely (port/type/capacity/cycle, stale revision, duplicate command)
and publishes once under the graph lock.  Every rejection path must leave
the graph byte-identical and return a machine-readable reason code.
Controller undo restores the exact connection order (multi-input ports).
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.core.graph_edit import (
    GraphEditCommand,
    EDIT_CAPACITY, EDIT_CYCLE, EDIT_DUPLICATE_COMMAND, EDIT_EDGE_MISSING,
    EDIT_NODE_MISSING, EDIT_PORT_MISSING, EDIT_STALE_REVISION,
    EDIT_TYPE_MISMATCH,
)
from src.features.composite.nodes.base_node import Connection
from src.features.composite.nodes.constant_node import ConstantNode
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.nodes.blur_node import BlurNode
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.nodes.scene3d_node import Scene3DNode
from src.features.composite.nodes.camera3d_node import Camera3DNode
from src.features.composite.nodes.light3d_node import Light3DNode

try:
    from src.features.composite.core.undo_stack import UndoStack
    _CAN_QT = True
except Exception:  # pragma: no cover
    _CAN_QT = False

from src.features.composite.core.layer_stack_controller import LayerStackController

pytestmark = pytest.mark.composite_core


def _graph() -> NodeGraph:
    g = NodeGraph()
    g.add_node(ConstantNode(node_id="src"))
    g.add_node(TransformNode(node_id="t"))
    g.add_node(BlurNode(node_id="b"))
    g.add_node(OutputNode(node_id="out"))
    assert g.connect("src", "result", "t", "image")
    assert g.connect("t", "result", "out", "image")
    return g


def _cables(g: NodeGraph):
    return [(c.src_node_id, c.src_port_name,
             c.dst_node_id, c.dst_port_name) for c in g.connections]


@pytest.mark.parametrize("command_builder,expected_code", [
    # Removed edge not present.
    (lambda g: GraphEditCommand(
        removed=(Connection("b", "result", "out", "image"),)),
     EDIT_EDGE_MISSING),
    # Missing endpoint node.
    (lambda g: GraphEditCommand(
        added=(Connection("ghost", "result", "t", "image2"),)),
     EDIT_NODE_MISSING),
    # Missing port.
    (lambda g: GraphEditCommand(
        added=(Connection("src", "nope", "t", "image"),)),
     EDIT_PORT_MISSING),
    # Type mismatch: image feeding a scene input.
    (lambda g: GraphEditCommand(
        added=(Connection("src", "result", "s3d", "in"),)),
     EDIT_TYPE_MISMATCH),
    # Single-input port at capacity (src already feeds t.image).
    (lambda g: GraphEditCommand(
        added=(Connection("b", "result", "t", "image"),)),
     EDIT_CAPACITY),
    # Directed cycle on the staged candidate: swap t.image's feed for t3
    # which already sits downstream of t.
    (lambda g: GraphEditCommand(
        removed=(Connection("src", "result", "t", "image"),),
        added=(Connection("t3", "result", "t", "image"),)),
     EDIT_CYCLE),
])
def test_rejections_are_reason_coded_and_leave_graph_unchanged(
        command_builder, expected_code):
    g = _graph()
    g.add_node(Scene3DNode(node_id="s3d"))
    g.add_node(TransformNode(node_id="t3"))
    assert g.connect("t", "result", "t3", "image")
    before = g.to_dict()

    result = g.apply_edit(command_builder(g))

    assert result.ok is False
    assert result.primary_code == expected_code
    assert result.issues[0].message
    # Failure paths never mutate the graph.
    assert g.to_dict() == before
    assert _cables(g) == _cables(g)


def test_stale_revision_rejected():
    g = _graph()
    before = g.to_dict()
    result = g.apply_edit(GraphEditCommand(
        removed=(Connection("src", "result", "t", "image"),),
        added=(Connection("src", "result", "b", "image"),
               Connection("b", "result", "t", "image")),
        expected_revision=g.generation + 7))
    assert result.primary_code == EDIT_STALE_REVISION
    assert g.to_dict() == before


def test_duplicate_command_id_rejected():
    g = _graph()
    command = GraphEditCommand(
        removed=(Connection("src", "result", "t", "image"),),
        added=(Connection("src", "result", "b", "image"),
               Connection("b", "result", "t", "image")),
        command_id="gesture-1")
    assert g.apply_edit(command).ok
    # Replaying the same gesture commit is a no-op rejection.
    replay = g.apply_edit(GraphEditCommand(
        removed=(Connection("src", "result", "b", "image"),),
        added=(Connection("src", "result", "t", "image"),),
        command_id="gesture-1"))
    assert replay.primary_code == EDIT_DUPLICATE_COMMAND
    assert _cables(g) == [("t", "result", "out", "image"),
                          ("src", "result", "b", "image"),
                          ("b", "result", "t", "image")]


def test_successful_edit_publishes_once_and_bumps_revisions():
    g = _graph()
    generation_before = g.generation
    result = g.apply_edit(GraphEditCommand(
        removed=(Connection("src", "result", "t", "image"),),
        added=(Connection("src", "result", "b", "image"),
               Connection("b", "result", "t", "image")),
        command_id="splice"))
    assert result.ok
    assert result.revision == g.generation == generation_before + 1
    assert _cables(g) == [("t", "result", "out", "image"),
                          ("src", "result", "b", "image"),
                          ("b", "result", "t", "image")]
    # edit_connections keeps its bool contract via the same code path.
    assert g.edit_connections(
        (Connection("src", "result", "b", "image"),
         Connection("b", "result", "t", "image")),
        (Connection("src", "result", "t", "image"),)) is True


def test_multi_input_edge_order_preserved_with_positions():
    g = _graph()
    g.add_node(Scene3DNode(node_id="s3d"))
    g.add_node(Camera3DNode(node_id="cam"))
    g.add_node(Light3DNode(node_id="light"))
    assert g.connect("cam", "display", "s3d", "in")
    assert g.connect("light", "display", "s3d", "in")
    assert _cables(g)[-2:] == [("cam", "display", "s3d", "in"),
                               ("light", "display", "s3d", "in")]
    # Re-insert the camera edge at the front of the multi-input port.
    result = g.apply_edit(GraphEditCommand(
        removed=(Connection("cam", "display", "s3d", "in"),),
        added=(Connection("cam", "display", "s3d", "in"),),
        positions=(4,)))
    assert result.ok
    assert _cables(g)[-2:] == [("light", "display", "s3d", "in"),
                               ("cam", "display", "s3d", "in")]


def test_mid_publication_exception_rolls_back(monkeypatch):
    g = _graph()
    before = g.to_dict()

    def explode(node_id):
        raise RuntimeError("injected dirty-marking failure")

    monkeypatch.setattr(g, "_mark_dirty_downstream", explode)
    with pytest.raises(RuntimeError):
        g.apply_edit(GraphEditCommand(
            removed=(Connection("src", "result", "t", "image"),),
            added=(Connection("src", "result", "b", "image"),
                   Connection("b", "result", "t", "image"))))
    # The half-applied publication is reverted.
    assert g.to_dict() == before


@pytest.mark.skipif(not _CAN_QT, reason="UndoStack requires Qt")
def test_controller_undo_restores_exact_connection_order():
    g = _graph()
    g.add_node(Scene3DNode(node_id="s3d"))
    g.add_node(Camera3DNode(node_id="cam"))
    g.add_node(Light3DNode(node_id="light"))
    assert g.connect("cam", "display", "s3d", "in")
    assert g.connect("light", "display", "s3d", "in")
    undo = UndoStack()
    controller = LayerStackController(g, undo)
    before_order = _cables(g)

    snapshot = controller.snapshot()
    assert g.apply_edit(GraphEditCommand(
        removed=(Connection("light", "display", "s3d", "in"),
                 Connection("src", "result", "t", "image")),
        added=(Connection("src", "result", "b", "image"),
               Connection("b", "result", "t", "image")))).ok
    assert g.apply_edit(GraphEditCommand(
        removed=(Connection("cam", "display", "s3d", "in"),))).ok
    assert _cables(g) != before_order

    controller._restore(snapshot)
    assert _cables(g) == before_order
