"""Regression tests for batch-A audit fixes (2026-09-29).

Locked regressions:

* AUD-10300 — clicking the Bind column of a parameter row and releasing
  without moving must emit ``binding_requested`` instead of raising
  ``UnboundLocalError`` inside ``KeyframesTreeView.mouseReleaseEvent``
  (an exception in a Qt event handler aborts the process under PyQt6).
* AUD-10400 — ``NodeEditorActions.paste_nodes`` must be able to log the
  Topos Color stack-copy diagnostics; the module-level ``logger`` has to
  exist, otherwise pasting a ``topos_color`` node raises ``NameError``
  half-way through the paste.
* AUD-10601 — ``DeleteNodeCommand.undo`` must remove auto-reconnected
  links through ``NodeGraph.disconnect`` so the mutation happens under
  the graph lock and bumps the topology revision / dirty flags, instead
  of editing ``graph.connections`` directly.
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import logging
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QMouseEvent

from src.features.composite.core import node_engine
from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.nodes.color_node import COLORNode
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.ui.keyframes_model import (
    KeyframesModel,
    KEYFRAMES_ROW_H,
)
from src.features.composite.ui.keyframes_tree_view import KeyframesTreeView
from src.features.composite.ui.node_graph_commands import DeleteNodeCommand
from src.features.composite.ui import node_editor_actions as nea_module
from src.features.composite.ui.node_editor_actions import NodeEditorActions


pytestmark = pytest.mark.composite_core


def _mouse_event(etype: QEvent.Type, x: float, y: float) -> QMouseEvent:
    """Build a left-button mouse event at widget-local (x, y)."""
    return QMouseEvent(
        etype,
        QPointF(x, y),
        QPointF(x, y),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )


# ── AUD-10300: bind-column click path ─────────────────────────────────


@pytest.fixture
def graph() -> NodeGraph:
    g = NodeGraph()
    g.add_node(TransformNode())
    return g


@pytest.fixture
def model(graph) -> KeyframesModel:
    m = KeyframesModel()
    m.rebuild(graph)
    return m


@pytest.fixture
def tree(qapp, model) -> KeyframesTreeView:
    t = KeyframesTreeView(model, header_height=30)
    t.resize(400, 300)
    return t


def _first_param_row(tree: KeyframesTreeView):
    rows = tree._model.visible_rows()
    for i, row in enumerate(rows):
        if row.row_type == "param" and row.param_name:
            return i, row
    pytest.fail("model produced no param rows; fixture setup is broken")


def _cell_center(tree: KeyframesTreeView, column: str, row_index: int):
    geom = tree._column_geometry()
    span = geom[column]
    x = (span[0] + span[1]) / 2.0
    y = tree._header_height + row_index * KEYFRAMES_ROW_H + KEYFRAMES_ROW_H / 2.0
    return x, y


class TestAud10300BindClick:
    """Plain click on the Bind cell must not raise UnboundLocalError."""

    def test_click_without_move_emits_binding_requested(self, tree):
        idx, row = _first_param_row(tree)
        x, y = _cell_center(tree, "bind", idx)

        tree.mousePressEvent(_mouse_event(QEvent.Type.MouseButtonPress, x, y))
        assert tree._binding_drag is not None
        assert tree._binding_drag_active is False

        emitted = []
        tree.binding_requested.connect(
            lambda node_id, param: emitted.append((node_id, param)))

        # Pre-fix this raised UnboundLocalError (target_node/target_param
        # were only assigned inside the drag branch).
        tree.mouseReleaseEvent(
            _mouse_event(QEvent.Type.MouseButtonRelease, x, y))

        assert emitted == [(row.node_id, row.param_name)]
        assert tree._binding_drag is None

    def test_drag_to_another_param_still_binds(self, tree):
        """The pick-whip drag path must keep working after the refactor."""
        rows = tree._model.visible_rows()
        param_indices = [i for i, r in enumerate(rows)
                         if r.row_type == "param" and r.param_name]
        source_idx, source_row = _first_param_row(tree)
        target_idx = param_indices[1]
        target_row = rows[target_idx]
        assert target_row.param_name != source_row.param_name

        x0, y0 = _cell_center(tree, "bind", source_idx)
        x1, y1 = _cell_center(tree, "bind", target_idx)

        tree.mousePressEvent(_mouse_event(QEvent.Type.MouseButtonPress, x0, y0))
        # Move >= 4px to arm the pick-whip, landing on another param row.
        tree.mouseMoveEvent(_mouse_event(QEvent.Type.MouseMove, x1, y1))
        assert tree._binding_drag_active is True

        emitted = []
        tree.parameter_binding_dragged.connect(
            lambda t_n, t_p, s_n, s_p: emitted.append((t_n, t_p, s_n, s_p)))
        tree.mouseReleaseEvent(
            _mouse_event(QEvent.Type.MouseButtonRelease, x1, y1))

        assert emitted == [(source_row.node_id, source_row.param_name,
                            target_row.node_id, target_row.param_name)]
        # A drag must not also fire the click signal.
        assert tree._binding_drag is None


# ── AUD-10400: module logger for paste_nodes ──────────────────────────


class _StubToposColorNode:
    """Minimal node stand-in accepted by NodeGraph.add_node."""

    def __init__(self, node_id: str):
        self.id = node_id
        self.node_type = "topos_color"
        self.parameters: dict = {}
        self.position = [0.0, 0.0]

    @classmethod
    def from_dict(cls, data: dict) -> "_StubToposColorNode":
        node = cls(data["id"])
        node.parameters = dict(data.get("parameters", {}))
        node.position = list(data.get("position", [0.0, 0.0]))
        return node


class TestAud10400PasteLogger:
    """Pasting a topos_color node in clip context must not raise NameError."""

    def test_module_logger_defined(self):
        assert isinstance(nea_module.logger, logging.Logger)
        assert nea_module.logger.name == nea_module.__name__

    def test_paste_topos_color_completes(self, qapp, monkeypatch):
        editor = MagicMock(name="NodeEditorWidget")
        editor.mapToScene.return_value = QPointF(100.0, 100.0)
        editor._node_items = {}
        editor._clipboard = {
            "nodes": [{
                "id": "source-node",
                "type": "topos_color",
                "parameters": {
                    "color_stack_id": "stack-abcdef123456",
                    "role": "primary_clip_grade",
                },
                "position": [0.0, 0.0],
            }],
            "connections": [],
        }

        # Walk-the-parents chain: one fake CompositePage holding a runtime
        # context, then None, so paste takes the page-hosted branch that
        # reaches the logger call.
        page = MagicMock(name="CompositePage")
        page.parent.return_value = None
        page._runtime_context.clip_id = "clip-1"
        page._runtime_context.timeline_id = "tl-1"
        page._runtime_context.asset_id = "asset-1"
        # NOTE (wd4-g25 / AUD-10406, locked-old-behavior protocol): this
        # lock previously asserted the fabricated "preset_copy" stack source
        # and the "clip-1_grade_<prefix>" id that AUD-10406 judges defective
        # (the id was never registered in the project, so the pasted node
        # silently rendered without grading). The MagicMock project exposes
        # no real color_stacks dict, so the source stack is unresolvable and
        # the paste keeps the original clip_stack reference instead. Intent
        # preserved: the paste completes without the NameError.
        page._project.timelines.items.return_value = []
        editor.parent.return_value = page

        monkeypatch.setattr(
            node_engine, "NODE_REGISTRY",
            {"topos_color": _StubToposColorNode})

        g = NodeGraph()
        actions = NodeEditorActions(editor, g)

        # Pre-fix: NameError('logger') surfaced here (Qt slot -> qFatal).
        pasted = actions.paste_nodes()

        assert len(pasted) == 1
        pasted_node = g.get_node(pasted[0])
        assert pasted_node is not None
        assert pasted_node.parameters["color_stack_id"] == "stack-abcdef123456"
        assert pasted_node.parameters["role"] == "composite_final_grade"
        assert pasted_node.parameters["linked"] is False
        editor.nodes_pasted.emit.assert_called_once()
        editor.graph_changed.emit.assert_called_once()


# ── AUD-10601: DeleteNodeCommand.undo uses the graph API ───────────────


def _conn_tuples(g: NodeGraph):
    return sorted(
        (c.src_node_id, c.src_port_name, c.dst_node_id, c.dst_port_name)
        for c in g.connections
    )


@pytest.fixture
def chain() -> NodeGraph:
    """color -> transform -> output; deleting transform auto-reconnects.

    TransformNode is used as the middle node because it exposes exactly one
    image-type input port, which is what DeleteNodeCommand's reconnection
    bookkeeping requires to record the bridged link (multi-image-input
    nodes like Blur/Grade fall back to plain removal).
    """
    g = NodeGraph()
    color = COLORNode(node_id="color-1")
    xform = TransformNode(node_id="xform-1")
    out = OutputNode(node_id="out-1")
    g.add_node(color)
    g.add_node(xform)
    g.add_node(out)
    assert g.connect("color-1", "result", "xform-1", "image")
    assert g.connect("xform-1", "result", "out-1", "image")
    return g


class TestAud10601DeleteUndoLocking:
    """DeleteNodeCommand.undo must mutate via locked graph methods."""

    def test_undo_restores_topology_and_removes_reconnect_link(self, chain):
        cmd = DeleteNodeCommand(graph=chain, node_ids=["xform-1"])
        cmd.redo()
        assert "xform-1" not in chain.nodes
        # Auto-reconnection bridged the gap.
        assert _conn_tuples(chain) == [
            ("color-1", "result", "out-1", "image")]

        cmd.undo()
        assert _conn_tuples(chain) == [
            ("color-1", "result", "xform-1", "image"),
            ("xform-1", "result", "out-1", "image"),
        ]
        assert "xform-1" in chain.nodes

    def test_undo_routes_link_removal_through_disconnect(self, chain):
        """The reconnect link removal must go through NodeGraph.disconnect
        (lock + revision/dirty), not raw list edits (AUD-10601)."""
        cmd = DeleteNodeCommand(graph=chain, node_ids=["xform-1"])
        cmd.redo()
        assert cmd._reconnected_links == [{
            "from_node": "color-1", "from_port": "result",
            "to_node": "out-1", "to_port": "image",
        }]

        with patch.object(chain, "disconnect",
                          wraps=chain.disconnect) as spy:
            cmd.undo()

        assert spy.call_count >= 1
        assert spy.call_args_list[0].args == (
            "color-1", "result", "out-1", "image")
        # The link is actually gone.
        assert ("color-1", "result", "out-1", "image") not in _conn_tuples(
            chain)

    def test_undo_bumps_topology_revision_and_marks_dirty(self, chain):
        """The link removal must be visible to the revision/dirty machinery
        that incremental rendering keys off of."""
        out = chain.get_node("out-1")
        out._dirty = False

        cmd = DeleteNodeCommand(graph=chain, node_ids=["xform-1"])
        cmd.redo()
        revision_after_delete = chain._topology_revision
        out._dirty = False  # isolate the undo pass

        cmd.undo()

        # disconnect() + connect() both bump the topology revision.
        assert chain._topology_revision > revision_after_delete
        # Downstream node was marked dirty by the API-routed mutations.
        assert out._dirty is True

    def test_undo_tolerates_missing_reconnect_link(self, chain):
        """Undo must stay safe when the auto-reconnected link is already
        gone (disconnect is a documented no-op returning False)."""
        cmd = DeleteNodeCommand(graph=chain, node_ids=["xform-1"])
        cmd.redo()
        # Simulate the user having manually un-bridged the gap.
        chain.disconnect("color-1", "result", "out-1", "image")

        cmd.undo()  # must not raise

        assert _conn_tuples(chain) == [
            ("color-1", "result", "xform-1", "image"),
            ("xform-1", "result", "out-1", "image"),
        ]

    def test_undo_redo_cycle_is_repeatable(self, chain):
        cmd = DeleteNodeCommand(graph=chain, node_ids=["xform-1"])
        original = _conn_tuples(chain)
        for _ in range(2):
            cmd.redo()
            assert _conn_tuples(chain) == [
                ("color-1", "result", "out-1", "image")]
            cmd.undo()
            assert _conn_tuples(chain) == original
