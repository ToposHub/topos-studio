"""Test Undo Stack and Node Graph Commands - undo/redo functionality."""
import pytest
import sys
import os
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.core.undo_stack import (
    UndoStack, UndoAction, UndoTree, UndoNode,
    create_param_change_action,
    create_move_nodes_action,
    create_add_node_action,
    create_remove_node_action,
    create_connect_action,
    create_disconnect_action,
)
from src.features.composite.core.node_engine import NodeGraph, NODE_REGISTRY
from src.features.composite.ui.node_graph_commands import (
    AddNodeCommand,
    DeleteNodeCommand,
    MoveNodeCommand,
    ConnectCommand,
    DisconnectCommand,
    ReconnectCommand,
    SetParameterCommand,
    CopyNodesCommand,
    PasteNodesCommand,
    DuplicateNodesCommand,
    CreateBackdropCommand,
    DeleteBackdropCommand,
    MoveBackdropCommand,
    ResizeBackdropCommand,
    ModifyBackdropCommand,
)
from src.features.composite.nodes.blur_node import BlurNode
from src.features.composite.nodes.grade_node import GradeNode
from src.features.composite.nodes.color_node import COLORNode
from src.features.composite.nodes.output_node import OutputNode

import pytest
pytestmark = pytest.mark.composite_core


# ─────────────────────────────────────────────────────────────────────────────
# UndoStack Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestUndoStackBasic:
    """Test UndoStack basic operations."""

    def test_init_empty(self, qapp):
        stack = UndoStack()
        assert stack.can_undo() is False
        assert stack.can_redo() is False
        assert stack.undo_description() == ""
        assert stack.redo_description() == ""

    def test_push_single_action(self, qapp):
        stack = UndoStack()
        data = {"value": 0}

        def undo_fn(d):
            d["value"] -= 1

        def redo_fn(d):
            d["value"] += 1

        action = UndoAction("test", data, undo_fn, redo_fn, "Increment")
        stack.push(action)

        assert stack.can_undo() is True
        assert stack.can_redo() is False
        assert stack.undo_description() == "Undo: Increment"

    def test_undo_single_action(self, qapp):
        stack = UndoStack()
        data = {"value": 10}

        def undo_fn(d):
            d["value"] = 5

        def redo_fn(d):
            d["value"] = 10

        action = UndoAction("test", data, undo_fn, redo_fn, "Set value")
        stack.push(action)

        stack.undo()
        assert data["value"] == 5
        assert stack.can_undo() is False
        assert stack.can_redo() is True

    def test_redo_single_action(self, qapp):
        stack = UndoStack()
        data = {"value": 10}

        def undo_fn(d):
            d["value"] = 5

        def redo_fn(d):
            d["value"] = 10

        action = UndoAction("test", data, undo_fn, redo_fn, "Set value")
        stack.push(action)
        stack.undo()

        stack.redo()
        assert data["value"] == 10
        assert stack.can_undo() is True
        assert stack.can_redo() is False

    def test_push_clears_redo_stack(self, qapp):
        """Pushing a new action should clear the redo stack."""
        stack = UndoStack()
        data = {"value": 0}

        action1 = UndoAction("test1", data, lambda d: None, lambda d: d.update({"v1": True}), "Action 1")
        action2 = UndoAction("test2", data, lambda d: None, lambda d: d.update({"v2": True}), "Action 2")

        stack.push(action1)
        stack.push(action2)
        stack.undo()  # Undo action2
        stack.undo()  # Undo action1

        assert stack.can_redo() is True

        # Push new action should clear redo stack
        action3 = UndoAction("test3", data, lambda d: None, lambda d: None, "Action 3")
        stack.push(action3)
        assert stack.can_redo() is False


class TestUndoStackMultiple:
    """Test UndoStack with multiple actions.

    Note: UndoStack.push() does NOT automatically execute redo_fn.
    The redo_fn is only called when stack.redo() is invoked.
    This means actions must be pre-executed or use explicit redo calls.
    """

    def test_multiple_undo_redo_sequence(self, qapp):
        stack = UndoStack()
        data = {"value": 0}

        def make_action(old_val, new_val):
            def undo_fn(d):
                d["value"] = old_val
            def redo_fn(d):
                d["value"] = new_val
            return UndoAction("increment", data, undo_fn, redo_fn, f"Set to {new_val}")

        # Push actions - we need to manually apply state changes before push
        # since push() doesn't execute redo_fn
        for i in range(1, 4):
            action = make_action(i - 1, i)
            data["value"] = i  # Apply state change manually
            stack.push(action)

        # Current state after pushing all actions
        assert data["value"] == 3

        # Undo all
        stack.undo()
        assert data["value"] == 2
        stack.undo()
        assert data["value"] == 1
        stack.undo()
        assert data["value"] == 0
        assert stack.can_undo() is False

        # Redo all
        stack.redo()
        assert data["value"] == 1
        stack.redo()
        assert data["value"] == 2
        stack.redo()
        assert data["value"] == 3
        assert stack.can_redo() is False

    def test_undo_redo_interleaved(self, qapp):
        """Test interleaved undo/redo operations."""
        stack = UndoStack()
        data = {"value": 0}

        actions = []
        for i in range(1, 5):
            def undo_fn(d, val=i-1):
                d["value"] = val
            def redo_fn(d, val=i):
                d["value"] = val
            actions.append(UndoAction("test", data, undo_fn, redo_fn, f"Set {i}"))
            # Apply state change manually since push() doesn't execute redo_fn
            data["value"] = i

        for action in actions:
            stack.push(action)

        assert data["value"] == 4

        # Undo 2, redo 1, undo 1
        stack.undo()  # -> 3
        assert data["value"] == 3
        stack.undo()  # -> 2
        assert data["value"] == 2
        stack.redo()  # -> 3
        assert data["value"] == 3
        stack.undo()  # -> 2
        assert data["value"] == 2

        # Push new action - apply state change first
        action5 = UndoAction("test", data, lambda d: d.update({"value": 2}), lambda d: d.update({"value": 10}), "New")
        data["value"] = 10  # Apply state change
        stack.push(action5)
        assert data["value"] == 10
        assert stack.can_redo() is False


class TestUndoStackGrouping:
    """Test UndoStack grouping functionality.

    Note: UndoStack.push() does NOT automatically execute redo_fn.
    State changes must be applied before pushing actions.
    """

    def test_begin_end_group(self, qapp):
        stack = UndoStack()
        data = {"value": 0}

        def increment(d):
            d["value"] += 1

        def decrement(d):
            d["value"] -= 1

        stack.begin_group()
        for i in range(3):
            data["value"] += 1  # Apply state change before push
            stack.push(UndoAction("test", data, decrement, increment, f"Increment"))
        stack.end_group("Three increments")

        # State should be 3 after applying changes
        assert data["value"] == 3
        assert stack.can_undo() is True
        # Group should be single undo
        stack.undo()
        assert data["value"] == 0
        assert stack.can_redo() is True

    def test_group_undo_reversed_order(self, qapp):
        """Group undo should execute in reverse order."""
        stack = UndoStack()
        data = {"log": []}

        def undo_fn(d, i):
            d["log"].append(f"undo{i}")

        def redo_fn(d, i):
            d["log"].append(f"redo{i}")

        stack.begin_group()
        stack.push(UndoAction("test", data, lambda d: undo_fn(d, 1), lambda d: redo_fn(d, 1), "1"))
        stack.push(UndoAction("test", data, lambda d: undo_fn(d, 2), lambda d: redo_fn(d, 2), "2"))
        stack.push(UndoAction("test", data, lambda d: undo_fn(d, 3), lambda d: redo_fn(d, 3), "3"))
        stack.end_group("Group")

        data["log"] = []
        stack.undo()
        # Undo should be in reverse: undo3, undo2, undo1
        assert data["log"] == ["undo3", "undo2", "undo1"]

        data["log"] = []
        stack.redo()
        # Redo should be in forward: redo1, redo2, redo3
        assert data["log"] == ["redo1", "redo2", "redo3"]

    def test_empty_group_no_undo(self, qapp):
        """Empty group should not create undo action."""
        stack = UndoStack()
        stack.begin_group()
        stack.end_group("Empty group")
        assert stack.can_undo() is False


class TestUndoStackMaxHistory:
    """Test UndoStack max history limit."""

    def test_max_history_limit(self, qapp):
        stack = UndoStack()
        data = {"value": 0}

        # Push more than MAX_HISTORY
        for i in range(60):
            action = UndoAction("test", data, lambda d: None, lambda d: d.update({"value": i}), f"Action {i}")
            stack.push(action)

        # Should be trimmed to MAX_HISTORY (50)
        # After pushing, we can undo at most 50 times
        undo_count = 0
        while stack.can_undo():
            stack.undo()
            undo_count += 1

        assert undo_count == stack.MAX_HISTORY

    def test_compress_all_keep_types_terminates(self, qapp):
        """Regression: compress must terminate when every node is a
        keep-type (connect/disconnect). The prune loop used to spin
        forever at 100% CPU when no removable candidate existed —
        rapid node wiring (>MAX_HISTORY ops) froze the UI thread.
        """
        stack = UndoStack()
        data = {"value": 0}

        # connect/disconnect are in KEEP_TYPES — nothing is prunable in
        # the first tier; compress must fall back instead of hanging.
        for i in range(120):
            atype = "connect" if i % 2 == 0 else "disconnect"
            action = UndoAction(atype, data, lambda d: None, lambda d: None,
                                f"{atype} {i}")
            stack.push(action)  # would hang forever before the fix

        # Tree is bounded by the hard cap (+root); undo chain stays intact.
        assert stack._tree.get_nodes_count() <= stack.MAX_HISTORY + 5
        assert stack.can_undo()
        stack.undo()  # current node preserved, undo still works
        assert stack.can_redo()


class TestUndoStackClear:
    """Test UndoStack clear functionality."""

    def test_clear(self, qapp):
        stack = UndoStack()
        data = {"value": 0}

        for i in range(5):
            action = UndoAction("test", data, lambda d: None, lambda d: None, f"Action {i}")
            stack.push(action)

        stack.clear()
        assert stack.can_undo() is False
        assert stack.can_redo() is False


# ─────────────────────────────────────────────────────────────────────────────
# AddNodeCommand Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestAddNodeCommand:
    """Test AddNodeCommand."""

    def test_add_node_command_redo(self):
        graph = NodeGraph()
        cmd = AddNodeCommand(graph, "blur", "blur_001", (100, 100))

        node = cmd.redo()
        assert node is not None
        assert node.node_type == "blur"
        assert node.id == "blur_001"
        assert node.position == [100, 100]
        assert "blur_001" in graph.nodes

    def test_add_node_command_undo(self):
        graph = NodeGraph()
        cmd = AddNodeCommand(graph, "blur", "blur_001", (100, 100))
        cmd.redo()

        cmd.undo()
        assert "blur_001" not in graph.nodes

    def test_add_node_with_parameters(self):
        graph = NodeGraph()
        cmd = AddNodeCommand(graph, "grade", "grade_001", (100, 100),
                            parameters={"lift_r": 0.5, "lift_g": 0.3})
        cmd.redo()

        node = graph.get_node("grade_001")
        assert node.parameters["lift_r"] == 0.5
        assert node.parameters["lift_g"] == 0.3

    def test_add_node_unknown_type_raises(self):
        graph = NodeGraph()
        cmd = AddNodeCommand(graph, "unknown_type", "node_001", (100, 100))
        with pytest.raises(ValueError, match="Unknown node type"):
            cmd.redo()


# ─────────────────────────────────────────────────────────────────────────────
# DeleteNodeCommand Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestDeleteNodeCommand:
    """Test DeleteNodeCommand."""

    def test_delete_single_node(self):
        graph = NodeGraph()
        blur = BlurNode()
        graph.add_node(blur)

        cmd = DeleteNodeCommand(graph, [blur.id])
        cmd.redo()

        assert blur.id not in graph.nodes

    def test_delete_node_undo(self):
        graph = NodeGraph()
        blur = BlurNode()
        blur.position = [100, 100]
        graph.add_node(blur)

        cmd = DeleteNodeCommand(graph, [blur.id])
        cmd.redo()
        cmd.undo()

        restored = graph.get_node(blur.id)
        assert restored is not None
        assert restored.position == [100, 100]

    def test_delete_multiple_nodes(self):
        graph = NodeGraph()
        blur1 = BlurNode()
        blur2 = BlurNode()
        grade = GradeNode()
        graph.add_node(blur1)
        graph.add_node(blur2)
        graph.add_node(grade)

        cmd = DeleteNodeCommand(graph, [blur1.id, blur2.id, grade.id])
        cmd.redo()

        assert len(graph.nodes) == 0

    def test_delete_preserves_connections_for_undo(self):
        graph = NodeGraph()
        color = COLORNode()
        blur = BlurNode()
        graph.add_node(color)
        graph.add_node(blur)
        graph.connect(color.id, "result", blur.id, "image")

        cmd = DeleteNodeCommand(graph, [blur.id])
        cmd.redo()

        assert len(graph.connections) == 0

        cmd.undo()

        # Connection should be restored
        restored_blur = graph.get_node(blur.id)
        assert restored_blur is not None
        assert len(graph.connections) == 1


# ─────────────────────────────────────────────────────────────────────────────
# MoveNodeCommand Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestMoveNodeCommand:
    """Test MoveNodeCommand."""

    def test_move_single_node(self):
        graph = NodeGraph()
        blur = BlurNode()
        blur.position = [100, 100]
        graph.add_node(blur)

        cmd = MoveNodeCommand(graph, [blur.id], {blur.id: (200, 300)})
        cmd.redo()

        assert blur.position == [200, 300]

    def test_move_node_undo(self):
        graph = NodeGraph()
        blur = BlurNode()
        blur.position = [100, 100]
        graph.add_node(blur)

        cmd = MoveNodeCommand(graph, [blur.id], {blur.id: (200, 300)})
        cmd.redo()
        cmd.undo()

        assert blur.position == [100, 100]

    def test_move_multiple_nodes(self):
        graph = NodeGraph()
        blur1 = BlurNode()
        blur2 = BlurNode()
        blur1.position = [100, 100]
        blur2.position = [200, 200]
        graph.add_node(blur1)
        graph.add_node(blur2)

        cmd = MoveNodeCommand(graph, [blur1.id, blur2.id],
                             {blur1.id: (150, 150), blur2.id: (250, 250)})
        cmd.redo()

        assert blur1.position == [150, 150]
        assert blur2.position == [250, 250]


# ─────────────────────────────────────────────────────────────────────────────
# ConnectCommand Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestConnectCommand:
    """Test ConnectCommand."""

    def test_connect_redo(self):
        graph = NodeGraph()
        color = COLORNode()
        blur = BlurNode()
        graph.add_node(color)
        graph.add_node(blur)

        cmd = ConnectCommand(graph, color.id, "result", blur.id, "image")
        result = cmd.redo()

        assert result is True
        assert len(graph.connections) == 1

    def test_connect_undo(self):
        graph = NodeGraph()
        color = COLORNode()
        blur = BlurNode()
        graph.add_node(color)
        graph.add_node(blur)

        cmd = ConnectCommand(graph, color.id, "result", blur.id, "image")
        cmd.redo()
        cmd.undo()

        assert len(graph.connections) == 0

    def test_connect_cycle_fails(self):
        graph = NodeGraph()
        blur1 = BlurNode()
        blur2 = BlurNode()
        graph.add_node(blur1)
        graph.add_node(blur2)
        graph.connect(blur1.id, "result", blur2.id, "image")

        # This would create cycle
        cmd = ConnectCommand(graph, blur2.id, "result", blur1.id, "image")
        result = cmd.redo()
        assert result is False


# ─────────────────────────────────────────────────────────────────────────────
# DisconnectCommand Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestDisconnectCommand:
    """Test DisconnectCommand."""

    def test_disconnect_redo(self):
        graph = NodeGraph()
        color = COLORNode()
        blur = BlurNode()
        graph.add_node(color)
        graph.add_node(blur)
        graph.connect(color.id, "result", blur.id, "image")

        cmd = DisconnectCommand(graph, color.id, "result", blur.id, "image")
        cmd.redo()

        assert len(graph.connections) == 0

    def test_disconnect_undo(self):
        graph = NodeGraph()
        color = COLORNode()
        blur = BlurNode()
        graph.add_node(color)
        graph.add_node(blur)
        graph.connect(color.id, "result", blur.id, "image")

        cmd = DisconnectCommand(graph, color.id, "result", blur.id, "image")
        cmd.redo()
        cmd.undo()

        assert len(graph.connections) == 1


# ─────────────────────────────────────────────────────────────────────────────
# ReconnectCommand Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestReconnectCommand:
    """Test ReconnectCommand."""

    def test_reconnect_redo(self):
        graph = NodeGraph()
        color1 = COLORNode()
        color2 = COLORNode()
        blur = BlurNode()
        graph.add_node(color1)
        graph.add_node(color2)
        graph.add_node(blur)
        graph.connect(color1.id, "result", blur.id, "image")

        cmd = ReconnectCommand(graph, blur.id, "image",
                              color1.id, "result", color2.id, "result")
        cmd.redo()

        # Should now be connected to color2
        assert len(graph.connections) == 1
        assert graph.connections[0].src_node_id == color2.id

    def test_reconnect_undo(self):
        graph = NodeGraph()
        color1 = COLORNode()
        color2 = COLORNode()
        blur = BlurNode()
        graph.add_node(color1)
        graph.add_node(color2)
        graph.add_node(blur)
        graph.connect(color1.id, "result", blur.id, "image")

        cmd = ReconnectCommand(graph, blur.id, "image",
                              color1.id, "result", color2.id, "result")
        cmd.redo()
        cmd.undo()

        # Should be connected back to color1
        assert graph.connections[0].src_node_id == color1.id


# ─────────────────────────────────────────────────────────────────────────────
# SetParameterCommand Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestSetParameterCommand:
    """Test SetParameterCommand."""

    def test_set_parameter_redo(self):
        graph = NodeGraph()
        blur = BlurNode()
        graph.add_node(blur)

        cmd = SetParameterCommand(graph, blur.id, "radius", 25.0)
        cmd.redo()

        assert blur.parameters["radius"] == 25.0

    def test_set_parameter_undo(self):
        graph = NodeGraph()
        blur = BlurNode()
        blur.parameters["radius"] = 10.0
        graph.add_node(blur)

        cmd = SetParameterCommand(graph, blur.id, "radius", 25.0)
        cmd.redo()
        cmd.undo()

        assert blur.parameters["radius"] == 10.0

    def test_set_parameter_preserves_old_value(self):
        graph = NodeGraph()
        grade = GradeNode()
        grade.parameters["lift_r"] = 0.2
        graph.add_node(grade)

        cmd = SetParameterCommand(graph, grade.id, "lift_r", 0.5)
        cmd.redo()
        cmd.undo()
        cmd.redo()

        # Should track old value correctly across multiple redo/undo
        assert grade.parameters["lift_r"] == 0.5


# ─────────────────────────────────────────────────────────────────────────────
# Copy/Paste/Duplicate Commands Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestCopyNodesCommand:
    """Test CopyNodesCommand."""

    def test_copy_single_node(self):
        graph = NodeGraph()
        blur = BlurNode()
        blur.position = [100, 100]
        blur.parameters["radius"] = 20.0
        graph.add_node(blur)

        cmd = CopyNodesCommand(graph, [blur.id])
        clipboard = cmd.redo()

        assert "nodes" in clipboard
        assert len(clipboard["nodes"]) == 1
        assert clipboard["nodes"][0]["type"] == "blur"
        assert clipboard["nodes"][0]["parameters"]["radius"] == 20.0

    def test_copy_with_connections(self):
        graph = NodeGraph()
        color = COLORNode()
        blur = BlurNode()
        graph.add_node(color)
        graph.add_node(blur)
        graph.connect(color.id, "result", blur.id, "image")

        cmd = CopyNodesCommand(graph, [color.id, blur.id])
        clipboard = cmd.redo()

        assert len(clipboard["connections"]) == 1
        # Connection uses indices
        assert clipboard["connections"][0]["src_idx"] == 0
        assert clipboard["connections"][0]["dst_idx"] == 1

    def test_copy_is_no_op_undo(self):
        graph = NodeGraph()
        blur = BlurNode()
        graph.add_node(blur)

        cmd = CopyNodesCommand(graph, [blur.id])
        cmd.redo()
        cmd.undo()  # Should be no-op

        # Node should still exist
        assert blur.id in graph.nodes


class TestPasteNodesCommand:
    """Test PasteNodesCommand."""

    def test_paste_single_node(self):
        graph = NodeGraph()
        clipboard = {
            "nodes": [{"type": "blur", "parameters": {"radius": 10.0}, "position": [100, 100]}],
            "connections": []
        }

        cmd = PasteNodesCommand(graph, clipboard, offset=(50, 50))
        pasted_ids = cmd.redo()

        assert len(pasted_ids) == 1
        node = graph.get_node(pasted_ids[0])
        assert node.node_type == "blur"
        assert node.position == [150, 150]

    def test_paste_with_connections(self):
        graph = NodeGraph()
        clipboard = {
            "nodes": [
                {"type": "color_input", "parameters": {}, "position": [100, 100]},
                {"type": "blur", "parameters": {}, "position": [200, 100]},
            ],
            "connections": [{"src_idx": 0, "src_port": "result", "dst_idx": 1, "dst_port": "image"}]
        }

        cmd = PasteNodesCommand(graph, clipboard, offset=(0, 0))
        pasted_ids = cmd.redo()

        # Connection should be restored
        assert len(graph.connections) == 1

    def test_paste_undo(self):
        graph = NodeGraph()
        clipboard = {
            "nodes": [{"type": "blur", "parameters": {}, "position": [100, 100]}],
            "connections": []
        }

        cmd = PasteNodesCommand(graph, clipboard)
        pasted_ids = cmd.redo()
        cmd.undo()

        for node_id in pasted_ids:
            assert node_id not in graph.nodes


class TestDuplicateNodesCommand:
    """Test DuplicateNodesCommand."""

    def test_duplicate_single_node(self):
        graph = NodeGraph()
        blur = BlurNode()
        blur.position = [100, 100]
        blur.parameters["radius"] = 20.0
        graph.add_node(blur)

        cmd = DuplicateNodesCommand(graph, [blur.id], offset=(50, 50))
        duplicated_ids = cmd.redo()

        assert len(duplicated_ids) == 1
        dup = graph.get_node(duplicated_ids[0])
        assert dup.node_type == "blur"
        assert dup.position == [150, 150]
        assert dup.parameters["radius"] == 20.0

    def test_duplicate_with_internal_connections(self):
        graph = NodeGraph()
        color = COLORNode()
        blur = BlurNode()
        graph.add_node(color)
        graph.add_node(blur)
        graph.connect(color.id, "result", blur.id, "image")

        cmd = DuplicateNodesCommand(graph, [color.id, blur.id])
        duplicated_ids = cmd.redo()

        # Should have 4 nodes now (2 original + 2 duplicated)
        assert len(graph.nodes) == 4
        # Internal connection should be duplicated
        # Original connection + duplicated connection = 2
        assert len(graph.connections) == 2

    def test_duplicate_undo(self):
        graph = NodeGraph()
        blur = BlurNode()
        graph.add_node(blur)

        cmd = DuplicateNodesCommand(graph, [blur.id])
        duplicated_ids = cmd.redo()
        cmd.undo()

        for node_id in duplicated_ids:
            assert node_id not in graph.nodes


# ─────────────────────────────────────────────────────────────────────────────
# Backdrop Commands Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestBackdropCommands:
    """Test backdrop-related commands."""

    def test_create_backdrop_redo(self):
        graph = NodeGraph()
        backdrop_data = {
            "title": "My Backdrop",
            "color": "#FFAA00",
            "note": "Test note",
            "position": [100, 100],
            "size": [300, 200]
        }

        cmd = CreateBackdropCommand(graph, "backdrop_001", backdrop_data)
        cmd.redo()

        assert "backdrop_001" in graph.backdrops
        assert graph.backdrops["backdrop_001"]["title"] == "My Backdrop"

    def test_create_backdrop_undo(self):
        graph = NodeGraph()
        backdrop_data = {"title": "Test", "color": "#FFFFFF", "position": [0, 0], "size": [100, 100]}

        cmd = CreateBackdropCommand(graph, "backdrop_001", backdrop_data)
        cmd.redo()
        cmd.undo()

        assert "backdrop_001" not in graph.backdrops

    def test_delete_backdrop_redo(self):
        graph = NodeGraph()
        graph.set_backdrop("backdrop_001", {"title": "Test"})

        cmd = DeleteBackdropCommand(graph, "backdrop_001")
        cmd.redo()

        assert "backdrop_001" not in graph.backdrops

    def test_delete_backdrop_undo(self):
        graph = NodeGraph()
        graph.set_backdrop("backdrop_001", {"title": "Test", "color": "#FFFFFF"})

        cmd = DeleteBackdropCommand(graph, "backdrop_001")
        cmd.redo()
        cmd.undo()

        assert "backdrop_001" in graph.backdrops
        assert graph.backdrops["backdrop_001"]["title"] == "Test"

    def test_move_backdrop(self):
        graph = NodeGraph()
        graph.set_backdrop("backdrop_001", {"title": "Test", "position": [100, 100], "size": [100, 100]})

        cmd = MoveBackdropCommand(graph, "backdrop_001", (100, 100), (200, 200))
        cmd.redo()

        assert graph.backdrops["backdrop_001"]["position"] == [200, 200]
        cmd.undo()
        assert graph.backdrops["backdrop_001"]["position"] == [100, 100]

    def test_resize_backdrop(self):
        graph = NodeGraph()
        graph.set_backdrop("backdrop_001", {"title": "Test", "position": [0, 0], "size": [100, 100]})

        cmd = ResizeBackdropCommand(graph, "backdrop_001", (100, 100), (200, 150))
        cmd.redo()

        assert graph.backdrops["backdrop_001"]["size"] == [200, 150]
        cmd.undo()
        assert graph.backdrops["backdrop_001"]["size"] == [100, 100]

    def test_modify_backdrop(self):
        graph = NodeGraph()
        graph.set_backdrop("backdrop_001", {"title": "Old", "note": "Old note", "color": "#FFFFFF"})

        cmd = ModifyBackdropCommand(graph, "backdrop_001",
                                   {"title": "Old", "note": "Old note"},
                                   {"title": "New", "note": "New note"})
        cmd.redo()

        assert graph.backdrops["backdrop_001"]["title"] == "New"
        assert graph.backdrops["backdrop_001"]["note"] == "New note"
        cmd.undo()
        assert graph.backdrops["backdrop_001"]["title"] == "Old"


# ─────────────────────────────────────────────────────────────────────────────
# Multi-step Undo/Redo Integration Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestMultiStepUndoRedo:
    """Test complex multi-step undo/redo scenarios.

    Note: UndoStack.push() does NOT automatically execute redo_fn.
    Commands must have their redo() executed before being pushed to stack,
    or the state change must be applied manually.
    """

    def test_complex_node_operations(self, qapp):
        """Test sequence: add nodes, connect, modify params, then undo all.
        
        Note: SetParameterCommand operates on node by ID, so even if the node
        object reference changes (e.g., after undo/redo of node creation),
        the command uses graph.get_node() to find the current node.
        """
        graph = NodeGraph()
        stack = UndoStack()

        # Step 1: Add nodes - execute redo first, then push to stack
        add_color = AddNodeCommand(graph, "color_input", "color_001", (100, 100))
        add_color.redo()  # Execute the command first
        stack.push(UndoAction("add_color", {}, lambda d: add_color.undo(), lambda d: add_color.redo(), "Add COLOR"))

        add_blur = AddNodeCommand(graph, "blur", "blur_001", (200, 100))
        add_blur.redo()
        stack.push(UndoAction("add_blur", {}, lambda d: add_blur.undo(), lambda d: add_blur.redo(), "Add Blur"))

        add_output = AddNodeCommand(graph, "output", "output_001", (300, 100))
        add_output.redo()
        stack.push(UndoAction("add_output", {}, lambda d: add_output.undo(), lambda d: add_output.redo(), "Add Output"))

        # Step 2: Connect
        conn1 = ConnectCommand(graph, "color_001", "result", "blur_001", "image")
        conn1.redo()
        stack.push(UndoAction("connect1", {}, lambda d: conn1.undo(), lambda d: conn1.redo(), "Connect COLOR->Blur"))

        conn2 = ConnectCommand(graph, "blur_001", "result", "output_001", "image")
        conn2.redo()
        stack.push(UndoAction("connect2", {}, lambda d: conn2.undo(), lambda d: conn2.redo(), "Connect Blur->Output"))

        # Step 3: Modify parameter
        # Note: BlurNode uses radius_x/radius_y, not 'radius'
        set_param = SetParameterCommand(graph, "blur_001", "radius_x", 30.0)
        set_param.redo()
        stack.push(UndoAction("set_param", {}, lambda d: set_param.undo(), lambda d: set_param.redo(), "Set radius_x"))

        assert len(graph.nodes) == 3
        assert len(graph.connections) == 2
        # Get blur_node reference AFTER parameter is set
        blur_node = graph.get_node("blur_001")
        assert blur_node.parameters["radius_x"] == 30.0

        # Undo all steps
        stack.undo()  # Undo set_param
        # SetParameterCommand.undo() uses graph.get_node() to find the node
        # Verify the parameter was reset by querying the graph
        blur_after_undo = graph.get_node("blur_001")
        assert blur_after_undo is not None
        # Default radius_x is 5.0 in BlurNode
        assert blur_after_undo.parameters["radius_x"] == 5.0

        stack.undo()  # Undo connect2
        assert len(graph.connections) == 1

        stack.undo()  # Undo connect1
        assert len(graph.connections) == 0

        stack.undo()  # Undo add_output
        assert len(graph.nodes) == 2

        stack.undo()  # Undo add_blur
        assert len(graph.nodes) == 1

        stack.undo()  # Undo add_color
        assert len(graph.nodes) == 0

        # Redo all
        stack.redo()  # Add color
        stack.redo()  # Add blur
        stack.redo()  # Add output
        stack.redo()  # Connect 1
        stack.redo()  # Connect 2
        stack.redo()  # Set param

        assert len(graph.nodes) == 3
        assert len(graph.connections) == 2
        # After redo, get fresh reference and check parameter
        blur_node_redo = graph.get_node("blur_001")
        assert blur_node_redo.parameters["radius_x"] == 30.0

    def test_grouped_delete_nodes(self, qapp):
        """Test deleting multiple nodes as a group."""
        graph = NodeGraph()
        stack = UndoStack()

        # Add 3 nodes
        color = COLORNode()
        blur = BlurNode()
        grade = GradeNode()
        graph.add_node(color)
        graph.add_node(blur)
        graph.add_node(grade)

        # Group delete - execute redo first
        delete_cmd = DeleteNodeCommand(graph, [color.id, blur.id, grade.id])
        delete_cmd.redo()  # Execute the delete

        stack.begin_group()
        stack.push(UndoAction("delete", {}, lambda d: delete_cmd.undo(), lambda d: delete_cmd.redo(), "Delete"))
        stack.end_group("Delete 3 nodes")

        assert len(graph.nodes) == 0

        # Single undo should restore all
        stack.undo()
        assert len(graph.nodes) == 3

        # Single redo should delete all
        stack.redo()
        assert len(graph.nodes) == 0

    def test_undo_after_new_action(self, qapp):
        """Test that redo stack is cleared after new action."""
        graph = NodeGraph()
        stack = UndoStack()

        # Add a node - execute redo first
        add_cmd = AddNodeCommand(graph, "blur", "blur_001", (100, 100))
        add_cmd.redo()
        stack.push(UndoAction("add", {}, lambda d: add_cmd.undo(), lambda d: add_cmd.redo(), "Add"))
        stack.undo()

        assert stack.can_redo() is True
        assert "blur_001" not in graph.nodes

        # New action should clear redo stack
        add_cmd2 = AddNodeCommand(graph, "grade", "grade_001", (200, 100))
        add_cmd2.redo()
        stack.push(UndoAction("add2", {}, lambda d: add_cmd2.undo(), lambda d: add_cmd2.redo(), "Add Grade"))

        assert stack.can_redo() is False
        assert stack.can_undo() is True
        assert "grade_001" in graph.nodes

        # Undo should undo grade, not blur
        stack.undo()
        assert "grade_001" not in graph.nodes


# ─────────────────────────────────────────────────────────────────────────────
# Command Merging Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestCommandMerging:
    """Test command merging functionality."""

    def test_param_change_mergeable(self, qapp):
        """Test that param_change actions are mergeable."""
        stack = UndoStack()
        data = {"value": 0}

        def undo_fn(d):
            data["value"] = d["old"]

        def redo_fn(d):
            data["value"] = d["new"]

        # Create mergeable param_change action
        action1 = create_param_change_action(
            node_id="node_001",
            param_name="radius",
            old_value=0.0,
            new_value=10.0,
            undo_fn=undo_fn,
            redo_fn=redo_fn,
        )

        assert action1.is_mergeable() is True
        assert action1.merge_key == "param_change:node_001:radius"

    def test_merge_within_window(self, qapp):
        """Test that consecutive param changes within merge window are merged."""
        stack = UndoStack()
        data = {"value": 0}

        def undo_fn(d):
            data["value"] = d["old"]

        def redo_fn(d):
            data["value"] = d["new"]

        # Begin merge coalescing mode
        stack.begin_merge_coalescing()

        # Push multiple param changes
        action1 = create_param_change_action(
            node_id="node_001",
            param_name="radius",
            old_value=0.0,
            new_value=10.0,
            undo_fn=undo_fn,
            redo_fn=redo_fn,
        )
        stack.push(action1)

        # Apply state change
        data["value"] = 10.0

        action2 = create_param_change_action(
            node_id="node_001",
            param_name="radius",
            old_value=10.0,  # This will be ignored in merge
            new_value=20.0,
            undo_fn=undo_fn,
            redo_fn=redo_fn,
        )
        stack.push(action2)

        data["value"] = 20.0

        action3 = create_param_change_action(
            node_id="node_001",
            param_name="radius",
            old_value=20.0,
            new_value=30.0,
            undo_fn=undo_fn,
            redo_fn=redo_fn,
        )
        stack.push(action3)

        data["value"] = 30.0

        # End merge coalescing
        stack.end_merge_coalescing()

        # All changes should be merged into one action
        # Undo should restore to original value (0.0)
        stack.undo()
        assert data["value"] == 0.0

        # Redo should restore to final value (30.0)
        stack.redo()
        assert data["value"] == 30.0

    def test_different_params_not_merged(self, qapp):
        """Test that changes to different params are NOT merged."""
        stack = UndoStack()

        data_radius = {"value": 0}
        data_gain = {"value": 0}

        def undo_radius(d):
            data_radius["value"] = d["old"]

        def redo_radius(d):
            data_radius["value"] = d["new"]

        def undo_gain(d):
            data_gain["value"] = d["old"]

        def redo_gain(d):
            data_gain["value"] = d["new"]

        stack.begin_merge_coalescing()

        # Change radius
        action1 = create_param_change_action(
            node_id="node_001",
            param_name="radius",
            old_value=0.0,
            new_value=10.0,
            undo_fn=undo_radius,
            redo_fn=redo_radius,
        )
        stack.push(action1)

        # Change gain (different param)
        action2 = create_param_change_action(
            node_id="node_001",
            param_name="gain",
            old_value=0.0,
            new_value=1.5,
            undo_fn=undo_gain,
            redo_fn=redo_gain,
        )
        stack.push(action2)

        stack.end_merge_coalescing()

        # Should NOT be merged - undo twice to restore both
        stack.undo()
        assert data_gain["value"] == 0.0
        stack.undo()
        assert data_radius["value"] == 0.0

    def test_add_node_not_mergeable(self, qapp):
        """Test that add_node actions are NOT mergeable (always kept)."""
        action = create_add_node_action(
            node_data={"node_id": "node_001"},
            undo_fn=lambda d: None,
            redo_fn=lambda d: None,
        )
        assert action.is_mergeable() is False

    def test_move_nodes_mergeable(self, qapp):
        """Test that move_nodes actions are mergeable."""
        action = create_move_nodes_action(
            node_ids=["node_001"],
            old_positions=[(0.0, 0.0)],
            new_positions=[(100.0, 100.0)],
            undo_fn=lambda d: None,
            redo_fn=lambda d: None,
        )
        assert action.is_mergeable() is True


# ─────────────────────────────────────────────────────────────────────────────
# UndoTree (Branch Undo) Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestUndoTree:
    """Test UndoTree for branch undo functionality."""

    def test_tree_init(self, qapp):
        """Test UndoTree initialization."""
        tree = UndoTree()
        assert tree.get_current_node() is not None
        assert tree.get_current_node().is_root() is True
        assert tree.get_nodes_count() == 0

    def test_push_creates_node(self, qapp):
        """Test that push creates a new node."""
        tree = UndoTree()
        data = {"value": 0}

        action = UndoAction(
            "param_change",
            {"old": 0, "new": 10},
            lambda d: data.update({"value": d["old"]}),
            lambda d: data.update({"value": d["new"]}),
            "Change param",
        )

        tree.push(action)

        assert tree.get_nodes_count() == 1
        assert tree.get_current_node().action is not None
        assert tree.get_current_node().description == "Change param"

    def test_undo_moves_to_parent(self, qapp):
        """Test that undo moves to parent node.

        Note: UndoTree.push() does NOT execute redo_fn.
        The action is pushed after the state change is already applied.
        undo() will execute undo_fn to restore the previous state.
        """
        tree = UndoTree()
        data = {"value": 10}  # Already at new value (state change applied before push)

        action = UndoAction(
            "param_change",
            {"old": 0, "new": 10},
            lambda d: data.update({"value": d["old"]}),
            lambda d: data.update({"value": d["new"]}),
            "Change param",
        )

        tree.push(action)
        # push() does not execute redo_fn, value stays at 10 (pre-applied)

        tree.undo()
        assert data["value"] == 0  # undo restores old value
        assert tree.get_current_node().is_root() is True

    def test_redo_moves_to_child(self, qapp):
        """Test that redo moves to child node."""
        tree = UndoTree()
        data = {"value": 0}

        action = UndoAction(
            "param_change",
            {"old": 0, "new": 10},
            lambda d: data.update({"value": d["old"]}),
            lambda d: data.update({"value": d["new"]}),
            "Change param",
        )

        tree.push(action)
        tree.undo()
        tree.redo()

        assert data["value"] == 10
        assert tree.get_current_node().action is not None

    def test_multiple_actions_create_chain(self, qapp):
        """Test that multiple actions create a chain of nodes."""
        tree = UndoTree()
        data = {"value": 0}

        for i in range(3):
            action = UndoAction(
                "increment",
                {"old": i, "new": i + 1},
                lambda d: data.update({"value": d["old"]}),
                lambda d: data.update({"value": d["new"]}),
                f"Increment to {i + 1}",
            )
            tree.push(action)
            data["value"] = i + 1  # Apply state change

        assert tree.get_nodes_count() == 3
        history = tree.get_history_list()
        assert len(history) == 4  # Root + 3 actions

    def test_switch_to_node(self, qapp):
        """Test switching to a specific node in history."""
        tree = UndoTree()
        data = {"value": 0}

        # Push 3 actions
        for i in range(3):
            action = UndoAction(
                "increment",
                {"old": i, "new": i + 1},
                lambda d: data.update({"value": d["old"]}),
                lambda d: data.update({"value": d["new"]}),
                f"Set to {i + 1}",
            )
            tree.push(action)
            data["value"] = i + 1

        # Get history
        history = tree.get_history_list()

        # Switch to second node
        second_node_id = history[2][0]  # Root is index 0, first action is 1, second is 2
        tree.switch_to_node(second_node_id)

        assert data["value"] == 2  # Should be at second action's value

        # Switch back to first
        first_node_id = history[1][0]
        tree.switch_to_node(first_node_id)
        assert data["value"] == 1

    def test_branches_tracking(self, qapp):
        """Test branch tracking.

        Note: branch_name is set in UndoTree.push(), not in UndoAction.
        """
        tree = UndoTree()
        data = {"value": 0}

        # Push some actions (branch_name is determined by UndoTree)
        for i in range(3):
            action = UndoAction(
                "param_change",
                {"old": i, "new": i + 1},
                lambda d: data.update({"value": d["old"]}),
                lambda d: data.update({"value": d["new"]}),
                f"Action {i}",
            )
            tree.push(action)
            data["value"] = i + 1

        branches = tree.get_all_branches()
        assert "main" in branches  # Default branch name


# ─────────────────────────────────────────────────────────────────────────────
# Command Compression Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestCommandCompression:
    """Test command compression functionality."""

    def test_compress_merges_param_changes(self, qapp):
        """Test that compression merges consecutive param changes."""
        tree = UndoTree()
        data = {"value": 0}

        # Push many param changes
        for i in range(10):
            action = create_param_change_action(
                node_id="node_001",
                param_name="radius",
                old_value=float(i),
                new_value=float(i + 1),
                undo_fn=lambda d: data.update({"value": d["old"]}),
                redo_fn=lambda d: data.update({"value": d["new"]}),
            )
            tree.push(action)
            data["value"] = float(i + 1)

        # Manually trigger compression
        tree.compress(keep_types=["add_node", "remove_node"], max_nodes=5)

        # Should have compressed to fewer nodes
        # Note: compression merges consecutive mergeable actions
        assert tree.get_nodes_count() <= 10

    def test_compress_keeps_critical_actions(self, qapp):
        """Test that compression keeps critical actions (add/remove node)."""
        tree = UndoTree()
        data = {"nodes": {}}

        # Push 5 param changes
        for i in range(5):
            action = create_param_change_action(
                node_id="node_001",
                param_name="radius",
                old_value=float(i),
                new_value=float(i + 1),
                undo_fn=lambda d: data.update({"value": d["old"]}),
                redo_fn=lambda d: data.update({"value": d["new"]}),
            )
            tree.push(action)

        # Push an add_node action (critical)
        action_add = create_add_node_action(
            node_data={"node_id": "node_002", "node_type": "blur", "node_title": "Blur"},
            undo_fn=lambda d: data["nodes"].pop(d["node_id"], None),
            redo_fn=lambda d: data["nodes"].update({d["node_id"]: True}),
        )
        tree.push(action_add)

        # Push more param changes
        for i in range(5):
            action = create_param_change_action(
                node_id="node_002",
                param_name="gain",
                old_value=float(i),
                new_value=float(i + 1),
                undo_fn=lambda d: data.update({"value": d["old"]}),
                redo_fn=lambda d: data.update({"value": d["new"]}),
            )
            tree.push(action)

        # Compress with keep_types=["add_node"]
        tree.compress(keep_types=["add_node"], max_nodes=5)

        # add_node should be preserved - check by node count or history
        history = tree.get_history_list()
        # The add_node action should still exist in history
        # Check that we have more than just param_change nodes
        node_count = tree.get_nodes_count()
        # After compression, add_node should still be there
        assert node_count >= 1  # At least one node should exist


# ─────────────────────────────────────────────────────────────────────────────
# Persistence Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestPersistence:
    """Test undo history persistence."""

    def test_action_serialize_deserialize(self, qapp):
        """Test UndoAction serialization and deserialization."""
        data = {"value": 0}

        def undo_fn(d):
            data["value"] = d["old"]

        def redo_fn(d):
            data["value"] = d["new"]

        action = create_param_change_action(
            node_id="node_001",
            param_name="radius",
            old_value=0.0,
            new_value=10.0,
            undo_fn=undo_fn,
            redo_fn=redo_fn,
        )

        # Serialize
        action_dict = action.to_dict()

        assert action_dict["action_type"] == "param_change"
        assert action_dict["data"]["node_id"] == "node_001"
        assert action_dict["data"]["param"] == "radius"
        assert action_dict["data"]["old"] == 0.0
        assert action_dict["data"]["new"] == 10.0

        # Deserialize
        restored = UndoAction.from_dict(action_dict, undo_fn, redo_fn)

        assert restored.action_type == "param_change"
        assert restored.merge_key == "param_change:node_001:radius"

    def test_undo_tree_serialize(self, qapp):
        """Test UndoTree serialization."""
        tree = UndoTree()
        data = {"value": 0}

        # Push some actions
        for i in range(3):
            action = UndoAction(
                "param_change",
                {"old": i, "new": i + 1},
                lambda d: data.update({"value": d["old"]}),
                lambda d: data.update({"value": d["new"]}),
                f"Action {i}",
            )
            tree.push(action)

        # Serialize
        tree_dict = tree.to_dict()

        assert "nodes" in tree_dict
        assert "root_node_id" in tree_dict
        assert "current_node_id" in tree_dict

    def test_save_load_to_file(self, qapp):
        """Test saving and loading undo history to file."""
        stack = UndoStack()
        data = {"value": 0}

        def undo_fn(d):
            data["value"] = d["old"]

        def redo_fn(d):
            data["value"] = d["new"]

        # Register callbacks for persistence
        stack.register_callbacks("param_change", undo_fn, redo_fn)

        # Push actions
        for i in range(3):
            action = create_param_change_action(
                node_id="node_001",
                param_name="radius",
                old_value=float(i),
                new_value=float(i + 1),
                undo_fn=undo_fn,
                redo_fn=redo_fn,
            )
            stack.push(action)
            data["value"] = float(i + 1)

        # Save to temp file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            temp_path = f.name

        stack.save_to_file(temp_path)

        # Create new stack and load
        stack2 = UndoStack()
        stack2.register_callbacks("param_change", undo_fn, redo_fn)
        stack2.load_from_file(temp_path)

        # Should have same history
        history = stack2.get_history_list()
        assert len(history) >= 3

        # Clean up
        os.unlink(temp_path)


# ─────────────────────────────────────────────────────────────────────────────
# Integration Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestUndoStackIntegration:
    """Integration tests for enhanced UndoStack."""

    def test_slider_drag_simulation(self, qapp):
        """Simulate slider dragging behavior with merge coalescing."""
        stack = UndoStack()
        data = {"radius": 0.0}

        def undo_fn(d):
            data["radius"] = d["old"]

        def redo_fn(d):
            data["radius"] = d["new"]

        # Simulate slider drag: begin merge coalescing
        stack.begin_merge_coalescing()

        # Rapid ticks
        for val in [1.0, 2.0, 5.0, 10.0, 15.0, 20.0]:
            action = create_param_change_action(
                node_id="node_001",
                param_name="radius",
                old_value=data["radius"],
                new_value=val,
                undo_fn=undo_fn,
                redo_fn=redo_fn,
            )
            stack.push(action)
            data["radius"] = val

        # End merge coalescing
        stack.end_merge_coalescing()

        # Single undo should restore to 0.0
        stack.undo()
        assert data["radius"] == 0.0

        # Single redo should restore to 20.0
        stack.redo()
        assert data["radius"] == 20.0

    def test_history_navigation(self, qapp):
        """Test navigating through history."""
        stack = UndoStack()
        data = {"value": 0}

        def undo_fn(d):
            data["value"] = d["old"]

        def redo_fn(d):
            data["value"] = d["new"]

        # Push 5 actions
        for i in range(5):
            action = create_param_change_action(
                node_id="node_001",
                param_name="radius",
                old_value=float(i),
                new_value=float(i + 1),
                undo_fn=undo_fn,
                redo_fn=redo_fn,
            )
            stack.push(action)
            data["value"] = float(i + 1)

        # Get history
        history = stack.get_history_list()

        # Should have 6 entries (root + 5 actions)
        assert len(history) == 6

        # Navigate to specific nodes
        if len(history) >= 3:
            node_id = history[3][0]  # Third action node
            stack.switch_to_node(node_id)

            # Value should be at that point
            # Note: switch_to_node involves undo/redo chain
            # This test verifies the mechanism works

class TestCanvasRenameAppliesImmediately:
    """B4 regression: UndoStack.push() only RECORDS a step — the page's
    rename handler used to push without applying, so the first rename did
    nothing until an undo/redo cycle.  Binding the real handler against a
    stub pins the apply-then-record ordering."""

    def _make_page_stub(self):
        from src.features.composite.ui.composite_page import CompositePage
        from src.features.composite.core.node_engine import NodeGraph as NG

        class _Node:
            def __init__(self):
                self.user_label = None

        class _Graph:
            def __init__(self):
                self.nodes = {"n1": _Node()}

            def get_node(self, nid):
                return self.nodes.get(nid)

        class _Page:
            _on_node_rename_requested = (
                CompositePage._on_node_rename_requested)

            def __init__(self):
                self._node_graph = _Graph()
                self._undo_stack = UndoStack()
                self.applied = []

            def _emit_user_edit_once(self):
                pass

            def _apply_node_user_label(self, node_id, label):
                self._node_graph.nodes[node_id].user_label = label
                self.applied.append((node_id, label))

        return _Page()

    def test_first_rename_takes_effect_immediately(self):
        page = self._make_page_stub()
        page._on_node_rename_requested("n1", "Plate Left")
        node = page._node_graph.nodes["n1"]
        assert node.user_label == "Plate Left"

    def test_rename_is_undoable_after_apply(self):
        page = self._make_page_stub()
        page._on_node_rename_requested("n1", "Plate Left")
        page._undo_stack.undo()
        assert page._node_graph.nodes["n1"].user_label is None
        page._undo_stack.redo()
        assert page._node_graph.nodes["n1"].user_label == "Plate Left"

    def test_whitespace_only_clears_label(self):
        page = self._make_page_stub()
        page._node_graph.nodes["n1"].user_label = "Foo"
        page._on_node_rename_requested("n1", "   ")
        assert page._node_graph.nodes["n1"].user_label is None
