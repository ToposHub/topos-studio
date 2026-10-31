"""Regression guards for 3D node deletion pre-cleanup ordering."""

from PyQt6.QtCore import QObject, pyqtSignal

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.camera3d_node import Camera3DNode
from src.features.composite.nodes.light3d_node import Light3DNode
from src.features.composite.ui.node_editor_actions import NodeEditorActions


class _FakeItem:
    def __init__(self, title: str, selected: bool = False):
        self.node_title = title
        self._selected = selected

    def isSelected(self) -> bool:
        return self._selected

    def set_selected(self, selected: bool) -> None:
        self._selected = selected


class _FakeEditor(QObject):
    node_about_to_be_removed = pyqtSignal(str)
    nodes_about_to_be_removed = pyqtSignal(list)
    node_removed = pyqtSignal(str)
    nodes_batch_removed = pyqtSignal(list)
    graph_changed = pyqtSignal()

    def __init__(self, graph: NodeGraph):
        super().__init__()
        self._node_graph = graph
        self._node_items = {}
        self._connection_items = []
        self._selected_node_id = None

    def remove_node_visual(self, node_id: str) -> None:
        self._node_items.pop(node_id, None)


def test_camera_delete_emits_about_to_remove_before_visual_teardown():
    # NOTE (wd4-g25 / AUD-10403, locked-old-behavior protocol): single-node
    # delete_node() now routes through the grouped batch-removal signals
    # (nodes_about_to_be_removed / nodes_batch_removed) so the page captures
    # the still-intact connections for undo and performs the engine-level
    # reconnect in one transaction. The locked intent is unchanged: the
    # "about" notification fires BEFORE the visual is torn down so the page
    # can detach 3D viewer state while the item still exists.
    graph = NodeGraph()
    graph.add_node(Camera3DNode("camera1"))
    editor = _FakeEditor(graph)
    editor._node_items["camera1"] = _FakeItem("Camera")
    editor._selected_node_id = "camera1"
    actions = NodeEditorActions(editor, graph)
    events = []

    editor.nodes_about_to_be_removed.connect(
        lambda node_ids: events.append(("about", tuple(node_ids), {
            node_id: node_id in editor._node_items for node_id in node_ids
        })))
    editor.nodes_batch_removed.connect(
        lambda node_ids: events.append(("removed", tuple(node_ids), {
            node_id: node_id in editor._node_items for node_id in node_ids
        })))

    assert actions.delete_node("camera1") is True

    assert events[0] == ("about", ("camera1",), {"camera1": True})
    assert events[1] == ("removed", ("camera1",), {"camera1": False})


def test_batch_delete_emits_about_to_remove_before_3d_visual_teardown():
    graph = NodeGraph()
    graph.add_node(Camera3DNode("camera1"))
    graph.add_node(Light3DNode("light1"))
    editor = _FakeEditor(graph)
    editor._node_items["camera1"] = _FakeItem("Camera", selected=True)
    editor._node_items["light1"] = _FakeItem("Light", selected=True)
    editor._selected_node_id = "camera1"
    actions = NodeEditorActions(editor, graph)
    events = []

    editor.nodes_about_to_be_removed.connect(
        lambda node_ids: events.append(("about", tuple(node_ids), {
            node_id: node_id in editor._node_items for node_id in node_ids
        })))
    editor.nodes_batch_removed.connect(
        lambda node_ids: events.append(("removed", tuple(node_ids), {
            node_id: node_id in editor._node_items for node_id in node_ids
        })))

    assert actions.delete_selected_nodes() == ["camera1", "light1"]

    assert events[0] == (
        "about",
        ("camera1", "light1"),
        {"camera1": True, "light1": True},
    )
    assert events[1] == (
        "removed",
        ("camera1", "light1"),
        {"camera1": False, "light1": False},
    )
