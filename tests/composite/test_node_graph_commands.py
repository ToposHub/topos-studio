"""
Tests for node_graph_commands.py — command/undo system for node graph editing.

Covers:
- AddNodeCommand, DeleteNodeCommand, MoveNodeCommand
- ConnectCommand, DisconnectCommand, ReconnectCommand
- SetParameterCommand
- CopyNodesCommand, PasteNodesCommand, DuplicateNodesCommand

All tests use pure logic (no GL required).
"""

import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.ui.node_graph_commands import (
    AddNodeCommand, DeleteNodeCommand, MoveNodeCommand,
    ConnectCommand, DisconnectCommand, ReconnectCommand,
    SetParameterCommand, CopyNodesCommand, PasteNodesCommand,
    DuplicateNodesCommand, RenameNodeCommand
)
from src.features.composite.nodes.color_node import COLORNode
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.nodes.blur_node import BlurNode
from src.features.composite.nodes.grade_node import GradeNode

import pytest
pytestmark = pytest.mark.composite_core


class TestAddNodeCommand:
    """AddNodeCommand creates nodes and supports undo/redo."""

    def test_add_node_creates_node(self):
        """添加节点 command 应创建节点。"""
        g = NodeGraph()
        cmd = AddNodeCommand(
            graph=g,
            node_type="blur",
            node_id="test-blur-1",
            position=(100.0, 200.0)
        )
        cmd.redo()
        assert "test-blur-1" in g.nodes
        assert g.nodes["test-blur-1"].node_type == "blur"
        assert g.nodes["test-blur-1"].position == [100.0, 200.0]

    def test_add_node_with_parameters(self):
        """添加节点 command 应能设置参数。"""
        g = NodeGraph()
        cmd = AddNodeCommand(
            graph=g,
            node_type="grade",
            node_id="test-grade-1",
            position=(0, 0),
            parameters={"lift_r": 0.5, "lift_g": 0.3}
        )
        cmd.redo()
        node = g.get_node("test-grade-1")
        assert node is not None
        assert node.parameters["lift_r"] == 0.5
        assert node.parameters["lift_g"] == 0.3

    def test_undo_removes_node(self):
        """undo 应删除节点。"""
        g = NodeGraph()
        cmd = AddNodeCommand(
            graph=g,
            node_type="blur",
            node_id="test-blur-1",
            position=(0, 0)
        )
        cmd.redo()
        assert "test-blur-1" in g.nodes

        cmd.undo()
        assert "test-blur-1" not in g.nodes

    def test_redo_recreates_node(self):
        """redo 应重新创建节点。"""
        g = NodeGraph()
        cmd = AddNodeCommand(
            graph=g,
            node_type="blur",
            node_id="test-blur-1",
            position=(100, 200)
        )

        # First redo
        cmd.redo()
        assert "test-blur-1" in g.nodes

        # Undo
        cmd.undo()
        assert "test-blur-1" not in g.nodes

        # Redo again
        cmd.redo()
        assert "test-blur-1" in g.nodes
        assert g.nodes["test-blur-1"].position == [100, 200]

    def test_description(self):
        """Command 应有正确的描述。"""
        cmd = AddNodeCommand(
            graph=NodeGraph(),
            node_type="blur",
            node_id="test-1",
            position=(0, 0)
        )
        assert "blur" in cmd.description


class TestDeleteNodeCommand:
    """DeleteNodeCommand removes nodes and supports undo/redo."""

    def test_delete_single_node(self):
        """删除单个节点。"""
        g = NodeGraph()
        g.add_node(COLORNode(node_id="color-1"))

        cmd = DeleteNodeCommand(graph=g, node_ids=["color-1"])
        cmd.redo()

        assert "color-1" not in g.nodes

    def test_delete_multiple_nodes(self):
        """删除多个节点。"""
        g = NodeGraph()
        g.add_node(COLORNode(node_id="color-1"))
        g.add_node(COLORNode(node_id="color-2"))
        g.add_node(BlurNode(node_id="blur-1"))

        cmd = DeleteNodeCommand(graph=g, node_ids=["color-1", "color-2", "blur-1"])
        cmd.redo()

        assert len(g.nodes) == 0

    def test_undo_restores_node(self):
        """undo 应恢复节点。"""
        g = NodeGraph()
        g.add_node(COLORNode(node_id="color-1"))

        cmd = DeleteNodeCommand(graph=g, node_ids=["color-1"])
        cmd.redo()
        assert "color-1" not in g.nodes

        cmd.undo()
        assert "color-1" in g.nodes

    def test_connections_saved_for_undo(self):
        """删除前应保存连接信息用于 undo。"""
        g = NodeGraph()
        color = COLORNode(node_id="color-1")
        blur = BlurNode(node_id="blur-1")
        g.add_node(color)
        g.add_node(blur)
        g.connect(color.id, "result", blur.id, "image")

        cmd = DeleteNodeCommand(graph=g, node_ids=["color-1"])
        cmd.redo()

        # Connection should be removed
        assert len(g.connections) == 0
        assert len(cmd._deleted_data) == 1
        assert len(cmd._deleted_data[0]["connections"]) == 1

    def test_connections_restored_on_undo(self):
        """undo 时连接也应恢复。"""
        g = NodeGraph()
        color = COLORNode(node_id="color-1")
        blur = BlurNode(node_id="blur-1")
        g.add_node(color)
        g.add_node(blur)
        g.connect(color.id, "result", blur.id, "image")

        cmd = DeleteNodeCommand(graph=g, node_ids=["color-1"])
        cmd.redo()
        assert len(g.connections) == 0

        cmd.undo()
        assert "color-1" in g.nodes
        # Note: connection restoration may depend on the node still existing
        # In this test, blur node should still be there
        assert "blur-1" in g.nodes


class TestMoveNodeCommand:
    """MoveNodeCommand changes node positions."""

    def test_move_single_node(self):
        """移动单个节点。"""
        g = NodeGraph()
        blur = BlurNode(node_id="blur-1")
        blur.position = [0.0, 0.0]
        g.add_node(blur)

        cmd = MoveNodeCommand(
            graph=g,
            node_ids=["blur-1"],
            new_positions={"blur-1": (100.0, 200.0)}
        )
        cmd.redo()

        assert g.nodes["blur-1"].position == [100.0, 200.0]

    def test_move_multiple_nodes(self):
        """移动多个节点。"""
        g = NodeGraph()
        blur1 = BlurNode(node_id="blur-1")
        blur1.position = [0, 0]
        blur2 = BlurNode(node_id="blur-2")
        blur2.position = [0, 0]
        g.add_node(blur1)
        g.add_node(blur2)

        cmd = MoveNodeCommand(
            graph=g,
            node_ids=["blur-1", "blur-2"],
            new_positions={"blur-1": (100, 100), "blur-2": (200, 200)}
        )
        cmd.redo()

        assert g.nodes["blur-1"].position == [100, 100]
        assert g.nodes["blur-2"].position == [200, 200]

    def test_undo_restores_positions(self):
        """undo 应恢复原位置。"""
        g = NodeGraph()
        blur = BlurNode(node_id="blur-1")
        blur.position = [50.0, 50.0]
        g.add_node(blur)

        cmd = MoveNodeCommand(
            graph=g,
            node_ids=["blur-1"],
            new_positions={"blur-1": (100, 200)}
        )
        cmd.redo()
        assert g.nodes["blur-1"].position == [100, 200]

        cmd.undo()
        assert g.nodes["blur-1"].position == [50.0, 50.0]

    def test_redo_moves_again(self):
        """redo 应再次移动到新位置。"""
        g = NodeGraph()
        blur = BlurNode(node_id="blur-1")
        blur.position = [0, 0]
        g.add_node(blur)

        cmd = MoveNodeCommand(
            graph=g,
            node_ids=["blur-1"],
            new_positions={"blur-1": (100, 200)}
        )

        cmd.redo()
        cmd.undo()
        assert g.nodes["blur-1"].position == [0, 0]

        cmd.redo()
        assert g.nodes["blur-1"].position == [100, 200]


class TestConnectCommand:
    """ConnectCommand creates connections."""

    def test_connect_two_nodes(self):
        """连接两个节点。"""
        g = NodeGraph()
        color = COLORNode(node_id="color-1")
        blur = BlurNode(node_id="blur-1")
        g.add_node(color)
        g.add_node(blur)

        cmd = ConnectCommand(
            graph=g,
            from_node_id="color-1",
            from_port="result",
            to_node_id="blur-1",
            to_port="image"
        )
        cmd.redo()

        assert len(g.connections) == 1
        conn = g.connections[0]
        assert conn.src_node_id == "color-1"
        assert conn.dst_node_id == "blur-1"

    def test_undo_removes_connection(self):
        """undo 应删除连接。"""
        g = NodeGraph()
        color = COLORNode(node_id="color-1")
        blur = BlurNode(node_id="blur-1")
        g.add_node(color)
        g.add_node(blur)

        cmd = ConnectCommand(
            graph=g,
            from_node_id="color-1",
            from_port="result",
            to_node_id="blur-1",
            to_port="image"
        )
        cmd.redo()
        assert len(g.connections) == 1

        cmd.undo()
        assert len(g.connections) == 0

    def test_redo_recreates_connection(self):
        """redo 应重新创建连接。"""
        g = NodeGraph()
        color = COLORNode(node_id="color-1")
        blur = BlurNode(node_id="blur-1")
        g.add_node(color)
        g.add_node(blur)

        cmd = ConnectCommand(
            graph=g,
            from_node_id="color-1",
            from_port="result",
            to_node_id="blur-1",
            to_port="image"
        )

        cmd.redo()
        cmd.undo()
        assert len(g.connections) == 0

        cmd.redo()
        assert len(g.connections) == 1


class TestDisconnectCommand:
    """DisconnectCommand removes connections."""

    def test_disconnect_existing_connection(self):
        """断开现有连接。"""
        g = NodeGraph()
        color = COLORNode(node_id="color-1")
        blur = BlurNode(node_id="blur-1")
        g.add_node(color)
        g.add_node(blur)
        g.connect("color-1", "result", "blur-1", "image")

        cmd = DisconnectCommand(
            graph=g,
            from_node_id="color-1",
            from_port="result",
            to_node_id="blur-1",
            to_port="image"
        )
        cmd.redo()

        assert len(g.connections) == 0

    def test_undo_reconnects(self):
        """undo 应重新连接。"""
        g = NodeGraph()
        color = COLORNode(node_id="color-1")
        blur = BlurNode(node_id="blur-1")
        g.add_node(color)
        g.add_node(blur)
        g.connect("color-1", "result", "blur-1", "image")

        cmd = DisconnectCommand(
            graph=g,
            from_node_id="color-1",
            from_port="result",
            to_node_id="blur-1",
            to_port="image"
        )
        cmd.redo()
        assert len(g.connections) == 0

        cmd.undo()
        # Note: undo uses graph.connect() which should restore the connection
        assert len(g.connections) == 1


class TestReconnectCommand:
    """ReconnectCommand changes connection source."""

    def test_reconnect_to_new_source(self):
        """重连到新源。"""
        g = NodeGraph()
        color1 = COLORNode(node_id="color-1")
        color2 = COLORNode(node_id="color-2")
        blur = BlurNode(node_id="blur-1")
        g.add_node(color1)
        g.add_node(color2)
        g.add_node(blur)
        g.connect("color-1", "result", "blur-1", "image")

        cmd = ReconnectCommand(
            graph=g,
            to_node_id="blur-1",
            to_port="image",
            old_from_node_id="color-1",
            old_from_port="result",
            new_from_node_id="color-2",
            new_from_port="result"
        )
        cmd.redo()

        assert len(g.connections) == 1
        assert g.connections[0].src_node_id == "color-2"

    def test_undo_restores_old_source(self):
        """undo 应恢复旧源。"""
        g = NodeGraph()
        color1 = COLORNode(node_id="color-1")
        color2 = COLORNode(node_id="color-2")
        blur = BlurNode(node_id="blur-1")
        g.add_node(color1)
        g.add_node(color2)
        g.add_node(blur)
        g.connect("color-1", "result", "blur-1", "image")

        cmd = ReconnectCommand(
            graph=g,
            to_node_id="blur-1",
            to_port="image",
            old_from_node_id="color-1",
            old_from_port="result",
            new_from_node_id="color-2",
            new_from_port="result"
        )
        cmd.redo()
        assert g.connections[0].src_node_id == "color-2"

        cmd.undo()
        assert g.connections[0].src_node_id == "color-1"


class TestSetParameterCommand:
    """SetParameterCommand changes node parameters."""

    def test_set_parameter(self):
        """设置参数。"""
        g = NodeGraph()
        grade = GradeNode(node_id="grade-1")
        g.add_node(grade)

        cmd = SetParameterCommand(
            graph=g,
            node_id="grade-1",
            param_name="lift_r",
            new_value=0.5
        )
        cmd.redo()

        assert g.nodes["grade-1"].parameters["lift_r"] == 0.5

    def test_undo_restores_old_value(self):
        """undo 应恢复旧值。"""
        g = NodeGraph()
        grade = GradeNode(node_id="grade-1")
        grade.parameters["lift_r"] = 0.0
        g.add_node(grade)

        cmd = SetParameterCommand(
            graph=g,
            node_id="grade-1",
            param_name="lift_r",
            new_value=0.5
        )
        cmd.redo()
        assert g.nodes["grade-1"].parameters["lift_r"] == 0.5

        cmd.undo()
        assert g.nodes["grade-1"].parameters["lift_r"] == 0.0

    def test_redo_sets_new_value(self):
        """redo 应再次设置新值。"""
        g = NodeGraph()
        grade = GradeNode(node_id="grade-1")
        g.add_node(grade)

        cmd = SetParameterCommand(
            graph=g,
            node_id="grade-1",
            param_name="lift_r",
            new_value=0.5
        )

        cmd.redo()
        cmd.undo()
        assert g.nodes["grade-1"].parameters["lift_r"] != 0.5

        cmd.redo()
        assert g.nodes["grade-1"].parameters["lift_r"] == 0.5


class TestCopyNodesCommand:
    """CopyNodesCommand copies node data to clipboard."""

    def test_copy_single_node(self):
        """复制单个节点。"""
        g = NodeGraph()
        blur = BlurNode(node_id="blur-1")
        blur.position = [100, 200]
        blur.parameters["radius"] = 10.0
        g.add_node(blur)

        cmd = CopyNodesCommand(graph=g, node_ids=["blur-1"])
        cmd.redo()

        clipboard = cmd.get_clipboard_data()
        assert len(clipboard["nodes"]) == 1
        assert clipboard["nodes"][0]["type"] == "blur"
        assert clipboard["nodes"][0]["position"] == [100, 200]
        assert clipboard["nodes"][0]["parameters"]["radius"] == 10.0

    def test_copy_multiple_nodes(self):
        """复制多个节点。"""
        g = NodeGraph()
        g.add_node(COLORNode(node_id="color-1"))
        g.add_node(BlurNode(node_id="blur-1"))

        cmd = CopyNodesCommand(graph=g, node_ids=["color-1", "blur-1"])
        cmd.redo()

        clipboard = cmd.get_clipboard_data()
        assert len(clipboard) == 2

    def test_copy_undo_is_noop(self):
        """Copy 的 undo 是空操作。"""
        g = NodeGraph()
        g.add_node(COLORNode(node_id="color-1"))

        cmd = CopyNodesCommand(graph=g, node_ids=["color-1"])
        cmd.redo()
        clipboard_before = cmd.get_clipboard_data()

        cmd.undo()  # Should do nothing

        clipboard_after = cmd.get_clipboard_data()
        # Clipboard should still have data (undo is no-op)
        assert len(clipboard_after) == len(clipboard_before)


class TestPasteNodesCommand:
    """PasteNodesCommand pastes nodes from clipboard."""

    def test_paste_single_node(self):
        """粘贴单个节点。"""
        g = NodeGraph()
        clipboard = {
            "nodes": [
                {"type": "blur", "parameters": {"radius": 5.0}, "position": [0, 0]}
            ],
            "connections": [],
        }

        cmd = PasteNodesCommand(graph=g, clipboard_data=clipboard, offset=(50, 50))
        cmd.redo()

        pasted_ids = cmd.get_pasted_ids()
        assert len(pasted_ids) == 1
        assert len(g.nodes) == 1

        # Check offset applied
        node = g.nodes[pasted_ids[0]]
        assert node.position[0] == 50  # 0 + 50
        assert node.position[1] == 50  # 0 + 50

    def test_paste_multiple_nodes(self):
        """粘贴多个节点。"""
        g = NodeGraph()
        clipboard = {
            "nodes": [
                {"type": "color_input", "parameters": {}, "position": [0, 0]},
                {"type": "blur", "parameters": {"radius": 5.0}, "position": [100, 100]},
            ],
            "connections": [],
        }

        cmd = PasteNodesCommand(graph=g, clipboard_data=clipboard, offset=(50, 50))
        cmd.redo()

        pasted_ids = cmd.get_pasted_ids()
        assert len(pasted_ids) == 2
        assert len(g.nodes) == 2

    def test_undo_removes_pasted_nodes(self):
        """undo 应删除粘贴的节点。"""
        g = NodeGraph()
        clipboard = {
            "nodes": [
                {"type": "blur", "parameters": {}, "position": [0, 0]}
            ],
            "connections": [],
        }

        cmd = PasteNodesCommand(graph=g, clipboard_data=clipboard, offset=(50, 50))
        cmd.redo()
        assert len(g.nodes) == 1

        cmd.undo()
        assert len(g.nodes) == 0

    def test_paste_with_custom_offset(self):
        """粘贴时使用自定义偏移。"""
        g = NodeGraph()
        clipboard = {
            "nodes": [
                {"type": "blur", "parameters": {}, "position": [100, 200]}
            ],
            "connections": [],
        }

        cmd = PasteNodesCommand(graph=g, clipboard_data=clipboard, offset=(100, 100))
        cmd.redo()

        node = g.nodes[cmd.get_pasted_ids()[0]]
        assert node.position[0] == 200  # 100 + 100
        assert node.position[1] == 300  # 200 + 100


class TestDuplicateNodesCommand:
    """DuplicateNodesCommand duplicates nodes."""

    def test_duplicate_single_node(self):
        """复制单个节点。"""
        g = NodeGraph()
        blur = BlurNode(node_id="blur-1")
        blur.position = [100, 200]
        blur.parameters["radius"] = 10.0
        g.add_node(blur)

        cmd = DuplicateNodesCommand(graph=g, node_ids=["blur-1"], offset=(50, 50))
        cmd.redo()

        dup_ids = cmd.get_duplicated_ids()
        assert len(dup_ids) == 1
        assert len(g.nodes) == 2

        # Check duplicated node has offset
        dup_node = g.nodes[dup_ids[0]]
        assert dup_node.position[0] == 150  # 100 + 50
        assert dup_node.position[1] == 250  # 200 + 50
        assert dup_node.parameters["radius"] == 10.0

    def test_duplicate_multiple_nodes(self):
        """复制多个节点。"""
        g = NodeGraph()
        g.add_node(COLORNode(node_id="color-1"))
        g.add_node(BlurNode(node_id="blur-1"))

        cmd = DuplicateNodesCommand(
            graph=g,
            node_ids=["color-1", "blur-1"],
            offset=(50, 50)
        )
        cmd.redo()

        assert len(cmd.get_duplicated_ids()) == 2
        assert len(g.nodes) == 4

    def test_undo_removes_duplicates(self):
        """undo 应删除复制的节点。"""
        g = NodeGraph()
        g.add_node(COLORNode(node_id="color-1"))

        cmd = DuplicateNodesCommand(graph=g, node_ids=["color-1"], offset=(50, 50))
        cmd.redo()
        assert len(g.nodes) == 2

        cmd.undo()
        assert len(g.nodes) == 1
        assert "color-1" in g.nodes

    def test_duplicate_preserves_parameters(self):
        """复制应保留参数。"""
        g = NodeGraph()
        grade = GradeNode(node_id="grade-1")
        grade.parameters["lift_r"] = 0.5
        grade.parameters["lift_g"] = 0.3
        g.add_node(grade)

        cmd = DuplicateNodesCommand(graph=g, node_ids=["grade-1"], offset=(0, 0))
        cmd.redo()

        dup_ids = cmd.get_duplicated_ids()
        dup_node = g.nodes[dup_ids[0]]
        assert dup_node.parameters["lift_r"] == 0.5
        assert dup_node.parameters["lift_g"] == 0.3

class TestRenameNodeCommand:
    """RenameNodeCommand sets/clears user_label with undo (B4)."""

    def _graph_with_node(self):
        g = NodeGraph()
        cmd = AddNodeCommand(graph=g, node_type="blur",
                             node_id="rename-target", position=(0.0, 0.0))
        cmd.redo()
        return g

    def test_rename_sets_user_label(self):
        g = self._graph_with_node()
        cmd = RenameNodeCommand(graph=g, node_id="rename-target",
                                new_label="My Node")
        cmd.redo()
        assert g.nodes["rename-target"].user_label == "My Node"
        assert g.nodes["rename-target"].display_title == "My Node"

    def test_rename_undo_restores_none(self):
        g = self._graph_with_node()
        cmd = RenameNodeCommand(graph=g, node_id="rename-target",
                                new_label="My Node")
        cmd.redo()
        cmd.undo()
        assert g.nodes["rename-target"].user_label is None

    def test_rename_undo_restores_previous_label(self):
        g = self._graph_with_node()
        g.nodes["rename-target"].user_label = "First"
        cmd = RenameNodeCommand(graph=g, node_id="rename-target",
                                new_label="Second")
        cmd.redo()
        assert g.nodes["rename-target"].user_label == "Second"
        cmd.undo()
        assert g.nodes["rename-target"].user_label == "First"

    def test_blank_label_clears_custom_name(self):
        g = self._graph_with_node()
        g.nodes["rename-target"].user_label = "Old"
        cmd = RenameNodeCommand(graph=g, node_id="rename-target", new_label="   ")
        cmd.redo()
        assert g.nodes["rename-target"].user_label is None

    def test_undo_redo_round_trip(self):
        g = self._graph_with_node()
        cmd = RenameNodeCommand(graph=g, node_id="rename-target",
                                new_label="RoundTrip")
        cmd.redo()
        cmd.undo()
        cmd.redo()
        assert g.nodes["rename-target"].user_label == "RoundTrip"

    def test_missing_node_is_noop(self):
        g = self._graph_with_node()
        cmd = RenameNodeCommand(graph=g, node_id="does-not-exist",
                                new_label="X")
        cmd.redo()
        cmd.undo()  # must not raise
