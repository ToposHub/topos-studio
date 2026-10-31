"""Regression tests for remediation batch D, wave 4, group 25 (wd4-g25).

Covers the 16 Major findings in the composite node-editor domain:
AUD-10403..10410 (node_editor_actions), AUD-10500..10506
(node_editor_widget), AUD-10602 (node_graph_commands).

All tests are headless (offscreen QApplication via the ``qapp`` fixture);
no OpenGL and no full CompositePage is required. Page-side signal handlers
are mirrored with the same semantics CompositePage implements.
"""

from __future__ import annotations

import json
import threading
import time
import types
from typing import Any, Dict, List, Optional, Tuple

import pytest
from PyQt6.QtCore import QEvent, QMimeData, QObject, QPoint, QPointF, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDropEvent, QDragEnterEvent, QMouseEvent

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.blend_node import BlendNode
from src.features.composite.nodes.camera3d_node import Camera3DNode
from src.features.composite.nodes.constant_node import ConstantNode
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.nodes.scene3d_node import Scene3DNode
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.ui.node_editor_actions import NodeEditorActions
from src.features.composite.ui.node_editor_widget import (
    MIME_TYPE_MEDIA_POOL_ASSETS, NodeEditorWidget,
)
from src.features.composite.ui.node_graph_commands import DeleteNodeCommand


# ── shared fakes ───────────────────────────────────────────────────────


class _Spy(QObject):
    """Signal recorder: connect(spy) then inspect .calls."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: List[Tuple[Any, ...]] = []

    def __call__(self, *args: Any) -> None:
        self.calls.append(args)


class _FakeItem:
    """Minimal NodeItem stand-in driving NodeEditorActions filters."""

    def __init__(self, node_id: str, title: str = "Node",
                 pos: Tuple[float, float] = (0.0, 0.0)) -> None:
        self.node_id = node_id
        self.node_title = title
        self._pos = QPointF(*pos)
        self._selected = False

    def pos(self) -> QPointF:
        return self._pos

    def isSelected(self) -> bool:
        return self._selected

    def set_selected(self, value: bool) -> None:
        self._selected = bool(value)

    # Alias used by QGraphicsScene-style callers on the real item.
    setSelected = set_selected


class _ConnItem:
    def __init__(self, selected: bool = False) -> None:
        self._selected = selected

    def isSelected(self) -> bool:
        return self._selected


class _FakeViewport:
    def rect(self) -> Any:
        return types.SimpleNamespace(center=lambda: QPointF(0.0, 0.0))


class _FakeEditor(QObject):
    """Duck-typed NodeEditorWidget: signals + state the actions touch."""

    node_added = pyqtSignal(str, str)
    node_about_to_be_removed = pyqtSignal(str)
    nodes_about_to_be_removed = pyqtSignal(list)
    node_removed = pyqtSignal(str)
    nodes_batch_removed = pyqtSignal(list)
    node_connected = pyqtSignal(str, str, str, str)
    node_disconnected = pyqtSignal(str, str, str, str)
    nodes_pasted = pyqtSignal(list)
    graph_changed = pyqtSignal()

    def __init__(self, graph: NodeGraph) -> None:
        super().__init__()
        self._node_graph = graph
        self._node_items: Dict[str, _FakeItem] = {}
        self._connection_items: List[Tuple[Any, str, str, str, str]] = []
        self._batch_mode = False
        self._selected_node_id: Optional[str] = None
        self._clipboard: Optional[Dict[str, Any]] = None
        self._locked_node_ids: set = set()

    # visual API -----------------------------------------------------------
    def add_node_visual(self, node: Any) -> None:
        title = getattr(node, "display_title", None) or getattr(
            node, "node_title", "Node")
        self._node_items[str(node.id)] = _FakeItem(str(node.id), title)

    def remove_node_visual(self, node_id: str) -> None:
        self._node_items.pop(node_id, None)
        self._connection_items = [
            c for c in self._connection_items
            if c[1] != node_id and c[3] != node_id]

    def add_connection_visual(self, s: str, sp: str, d: str, dp: str,
                              _data_type: str = "image") -> None:
        self._connection_items.append((_ConnItem(), s, sp, d, dp))

    def _remove_connection_item(self, item: Any) -> None:
        self._connection_items = [c for c in self._connection_items
                                  if c[0] is not item]

    def remove_connection_visual_by_dst(self, dst: str, dst_port: str) -> None:
        for conn_data in list(self._connection_items):
            if conn_data[3] == dst and conn_data[4] == dst_port:
                self._remove_connection_item(conn_data[0])
                return

    def is_node_locked(self, _nid: str) -> bool:
        return False

    # paste/preset helpers --------------------------------------------------
    def mapToScene(self, point: Any) -> QPointF:
        return QPointF(0.0, 0.0)

    def viewport(self) -> _FakeViewport:
        return _FakeViewport()


def _quad(conn: Any) -> Tuple[str, str, str, str]:
    return (conn.src_node_id, conn.src_port_name,
            conn.dst_node_id, conn.dst_port_name)


def _quads(graph: NodeGraph) -> set:
    return {_quad(c) for c in graph.connections}


def _mirror_page_handlers(editor: _FakeEditor, graph: NodeGraph) -> None:
    """Install CompositePage-equivalent signal handlers (core semantics).

    The batch-removal mirror records what the page's undo capture would see
    (the node's connections at signal time) into ``editor.mirror_captures``
    BEFORE running remove_node_and_reconnect.
    """
    def on_connect(s: str, sp: str, d: str, dp: str) -> None:
        if graph.connect(s, sp, d, dp):
            editor.add_connection_visual(s, sp, d, dp)

    def on_disconnect(s: str, sp: str, d: str, dp: str) -> None:
        if graph.disconnect(s, sp, d, dp):
            editor.remove_connection_visual_by_dst(d, dp)

    def on_batch_removed(ids: List[str]) -> None:
        # composite_page._on_nodes_batch_removed: the undo capture reads the
        # graph BEFORE remove_node_and_reconnect runs.
        for nid in ids:
            editor.mirror_captures[nid] = [
                _quad(c) for c in graph.get_connections_for_node(nid)]
            graph.remove_node_and_reconnect(nid)

    editor.mirror_captures: Dict[str, List[Tuple[str, str, str, str]]] = {}
    editor.node_connected.connect(on_connect)
    editor.node_disconnected.connect(on_disconnect)
    editor.nodes_batch_removed.connect(on_batch_removed)


def _chain_graph() -> Tuple[NodeGraph, _FakeEditor, NodeEditorActions]:
    """COLOR -> xform -> output with visuals and page mirrors wired."""
    from src.features.composite.nodes.color_node import COLORNode
    graph = NodeGraph()
    graph.add_node(COLORNode(node_id="color-1"))
    graph.add_node(TransformNode(node_id="xform-1"))
    graph.add_node(OutputNode(node_id="out-1"))
    assert graph.connect("color-1", "result", "xform-1", "image")
    assert graph.connect("xform-1", "result", "out-1", "image")

    editor = _FakeEditor(graph)
    for nid in ("color-1", "xform-1", "out-1"):
        editor.add_node_visual(graph.get_node(nid))
    editor.add_connection_visual("color-1", "result", "xform-1", "image")
    editor.add_connection_visual("xform-1", "result", "out-1", "image")

    actions = NodeEditorActions(editor, graph)
    _mirror_page_handlers(editor, graph)
    return graph, editor, actions


# ── AUD-10403: delete keeps the undo capture non-empty ─────────────────


@pytest.mark.usefixtures("qapp")
def test_aud_10403_delete_node_captures_connections_before_removal():
    graph, editor, actions = _chain_graph()

    connect_spy, disconnect_spy = _Spy(), _Spy()
    editor.node_connected.connect(connect_spy)
    editor.node_disconnected.connect(disconnect_spy)

    assert actions.delete_node("xform-1") is True

    # The undo capture saw both original cables — the regression captured [].
    assert set(editor.mirror_captures["xform-1"]) == {
        ("color-1", "result", "xform-1", "image"),
        ("xform-1", "result", "out-1", "image"),
    }
    # No per-cable undo entries: the delete is one grouped transaction.
    assert connect_spy.calls == []
    assert disconnect_spy.calls == []
    # The engine reconnected the chain around the removed node.
    assert graph.get_node("xform-1") is None
    assert ("color-1", "result", "out-1", "image") in _quads(graph)


@pytest.mark.usefixtures("qapp")
def test_aud_10403_delete_selected_nodes_capture_intact():
    graph, editor, actions = _chain_graph()
    editor._node_items["xform-1"].set_selected(True)

    connect_spy, disconnect_spy = _Spy(), _Spy()
    editor.node_connected.connect(connect_spy)
    editor.node_disconnected.connect(disconnect_spy)

    deleted = actions.delete_selected_nodes()
    assert deleted == ["xform-1"]
    assert set(editor.mirror_captures["xform-1"]) == {
        ("color-1", "result", "xform-1", "image"),
        ("xform-1", "result", "out-1", "image"),
    }
    assert connect_spy.calls == []
    assert disconnect_spy.calls == []
    assert ("color-1", "result", "out-1", "image") in _quads(graph)


# ── AUD-10404: rename really renames every reference ───────────────────


@pytest.mark.usefixtures("qapp")
def test_aud_10404_rename_node_safe_updates_all_references():
    graph, editor, actions = _chain_graph()
    editor._locked_node_ids.add("xform-1")
    editor._selected_node_id = "xform-1"

    assert actions._rename_node_safe("xform-1", "renamed-1") is True

    node = graph.get_node("renamed-1")
    assert node is not None
    assert node.id == "renamed-1"          # old code wrote node._id (no-op)
    assert "xform-1" not in graph.nodes
    assert node.to_dict()["id"] == "renamed-1"
    assert ("color-1", "result", "renamed-1", "image") in _quads(graph)
    assert ("renamed-1", "result", "out-1", "image") in _quads(graph)
    # Visual layer re-keyed + lock/selection moved to the new id.
    assert "xform-1" not in editor._node_items
    assert "renamed-1" in editor._node_items
    assert "renamed-1" in editor._locked_node_ids
    assert editor._selected_node_id == "renamed-1"

    # Conflicting / missing targets are refused.
    assert actions._rename_node_safe("renamed-1", "out-1") is False
    assert actions._rename_node_safe("missing", "whatever") is False
    assert actions._rename_node_safe("renamed-1", "renamed-1") is False


# ── AUD-10405: preset save must not clobber the clipboard ───────────────


@pytest.mark.usefixtures("qapp")
def test_aud_10405_serialize_selected_nodes_does_not_touch_clipboard():
    graph, editor, actions = _chain_graph()
    editor._node_items["xform-1"].set_selected(True)

    # User has something on the clipboard.
    editor._clipboard = {"nodes": [{"type": "kept"}], "connections": []}

    data = actions._serialize_selected_nodes()
    assert data is not None and len(data["nodes"]) == 1
    # Pure producer: the clipboard is untouched.
    assert editor._clipboard == {"nodes": [{"type": "kept"}], "connections": []}

    # copy_selected_nodes still owns the clipboard write.
    clipboard = actions.copy_selected_nodes()
    assert clipboard is not None
    assert editor._clipboard is clipboard


# ── AUD-10406: pasted topos_color carries an embedded stack ─────────────


class _FakeColorStack:
    def __init__(self) -> None:
        self.grading_params = {"exposure": 0.5}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "grading_params": dict(self.grading_params),
            "curves_data": {}, "hsl_curve_data": {}, "mixer_params": {},
            "soft_clip_params": {}, "lut_path": None, "lut_intensity": 1.0,
        }


def _topos_clipboard() -> Dict[str, Any]:
    return {
        "nodes": [{
            "id": "tc-1", "type": "topos_color", "position": [0.0, 0.0],
            "parameters": {
                "stack_source": "clip_stack", "color_stack_id": "src-stack",
                "role": "primary_clip_grade", "linked": True,
            },
        }],
        "connections": [],
    }


@pytest.mark.usefixtures("qapp")
def test_aud_10406_paste_topos_color_embeds_source_stack():
    from src.features.composite.nodes.topos_color_node import ToposColorNode
    graph = NodeGraph()
    graph.add_node(ToposColorNode(node_id="host-node"))
    editor = _FakeEditor(graph)
    editor.add_node_visual(graph.get_node("host-node"))
    actions = NodeEditorActions(editor, graph)

    host = QObject()
    host.setParent(editor)
    host._runtime_context = object()
    host._project = types.SimpleNamespace(
        color_stacks={"src-stack": _FakeColorStack()})
    # Re-parent: _find_host_composite_page walks editor.parent().
    editor.setParent(host)

    editor._clipboard = _topos_clipboard()
    pasted = actions.paste_nodes()
    assert pasted, "paste produced no nodes"

    node = graph.get_node(pasted[0])
    assert node is not None
    assert node.parameters["stack_source"] == "embedded"
    assert node.parameters["color_stack_id"] == ""   # no dangling reference
    assert node.parameters["embedded_stack"]["grading_params"] == {
        "exposure": 0.5}
    assert node.parameters["role"] == "composite_final_grade"
    assert node.parameters["linked"] is False


@pytest.mark.usefixtures("qapp")
def test_aud_10406_paste_topos_color_unresolvable_source_keeps_reference():
    from src.features.composite.nodes.topos_color_node import ToposColorNode
    graph = NodeGraph()
    graph.add_node(ToposColorNode(node_id="host-node"))
    editor = _FakeEditor(graph)
    editor.add_node_visual(graph.get_node("host-node"))
    actions = NodeEditorActions(editor, graph)
    # No host page → source stack unresolvable.

    editor._clipboard = _topos_clipboard()
    pasted = actions.paste_nodes()
    node = graph.get_node(pasted[0])
    assert node is not None
    # Keeps the real reference (round-trips in-project); the old code used
    # the unresolvable "preset_copy" source and a fabricated stack id.
    assert node.parameters["stack_source"] == "clip_stack"
    assert node.parameters["color_stack_id"] == "src-stack"
    assert "preset_copy" not in node.parameters.values()


# ── AUD-10407: one drag → one grouped undo transaction ──────────────────


@pytest.mark.usefixtures("qapp")
def test_aud_10407_create_3d_scene_uses_single_summary_signal():
    graph = NodeGraph()
    editor = _FakeEditor(graph)
    actions = NodeEditorActions(editor, graph)

    added_spy = _Spy()
    pasted_spy = _Spy()
    editor.node_added.connect(added_spy)
    editor.nodes_pasted.connect(pasted_spy)

    scene3d = actions.create_3d_scene_with_children(
        0.0, 0.0, model_path="/tmp/model.obj")
    assert scene3d is not None

    # No per-node node_added: the page pushes one undo per emission and
    # re-evaluates the whole graph each time.
    assert added_spy.calls == []
    # Exactly one summary signal carrying every created node.
    assert len(pasted_spy.calls) == 1
    payload = pasted_spy.calls[0][0]
    types_in_payload = {d["type"] for d in payload}
    assert "scene3d" in types_in_payload and "model3d" in types_in_payload
    # All payload nodes exist in the graph and the model → scene3d cable
    # landed (the page's grouped handler records it from the graph).
    for data in payload:
        assert graph.get_node(data["id"]) is not None
    assert graph.get_node(scene3d.id) is not None
    model_ids = [nid for nid, n in graph.nodes.items()
                 if n.node_type == "model3d"]
    assert model_ids, "Model3D child was not spawned"
    quads = {(c.src_node_id, c.src_port_name, c.dst_node_id, c.dst_port_name)
             for c in graph.connections}
    model_cables = [q for q in quads if q[0] == model_ids[0]]
    assert model_cables and model_cables[0][2] == scene3d.id
    assert editor._batch_mode is False


# ── AUD-10408: protection by node_type, not display title ───────────────


@pytest.mark.usefixtures("qapp")
def test_aud_10408_protection_survives_rename():
    graph, editor, actions = _chain_graph()
    # User renamed the Output node on the canvas.
    editor._node_items["out-1"].node_title = "My Render Target"
    editor._node_items["out-1"].set_selected(True)

    assert actions._is_protected_node("out-1") is True
    assert actions._is_protected_node("color-1") is True
    assert actions._is_protected_node("xform-1") is False

    removed_spy = _Spy()
    editor.nodes_batch_removed.connect(removed_spy)
    assert actions.delete_selected_nodes() == []
    assert actions.delete_node("out-1") is False
    assert actions.cut_selected_nodes()[1] == []
    assert actions.duplicate_selected_nodes() == []
    assert removed_spy.calls == []
    assert graph.get_node("out-1") is not None


# ── AUD-10409: insert-dot failure rolls back ────────────────────────────


@pytest.mark.usefixtures("qapp")
def test_aud_10409_insert_dot_on_scene_cable_rolls_back():
    graph = NodeGraph()
    graph.add_node(Camera3DNode(node_id="cam-1"))
    graph.add_node(Scene3DNode(node_id="scene-1"))
    assert graph.connect("cam-1", "display", "scene-1", "in")  # scene cable

    editor = _FakeEditor(graph)
    editor.add_node_visual(graph.get_node("cam-1"))
    editor.add_node_visual(graph.get_node("scene-1"))
    editor.add_connection_visual("cam-1", "display", "scene-1", "in")
    actions = NodeEditorActions(editor, graph)
    _mirror_page_handlers(editor, graph)

    result = actions.insert_dot_on_connection(
        "cam-1", "display", "scene-1", "in", insert_x=5.0, insert_y=5.0)

    assert result is None
    # No dangling dot left behind.
    assert all(n.node_type != "dot" for n in graph.nodes.values())
    assert len(editor._node_items) == 2
    # The original cable is restored in model and visuals.
    assert ("cam-1", "display", "scene-1", "in") in _quads(graph)
    assert len(editor._connection_items) == 1


# ── AUD-10410: load_preset validates external positions ─────────────────


class _StubPresetManager:
    def __init__(self, data: Dict[str, Any]) -> None:
        self._data = data

    def load_preset(self, _name: str) -> Dict[str, Any]:
        return self._data


@pytest.mark.usefixtures("qapp")
def test_aud_10410_load_preset_malformed_positions_do_not_raise():
    graph = NodeGraph()
    editor = _FakeEditor(graph)
    actions = NodeEditorActions(editor, graph)
    editor._preset_manager = _StubPresetManager({
        "nodes": [
            {"id": "a", "type": "blur", "position": []},
            {"id": "b", "type": "blur", "position": 100},
            {"id": "c", "type": "blur", "position": None},
            {"id": "d", "type": "blur", "position": [1]},
            {"id": "e", "type": "blur", "position": "xy"},
            {"id": "f", "type": "blur", "position": [10, 20]},
        ],
        "connections": [],
    })

    new_ids = actions.load_preset("broken", position=(100.0, 100.0))
    assert len(new_ids) == 6  # every node loaded, none raised
    positions = {tuple(graph.get_node(nid).position) for nid in new_ids}
    # ref point = min of validated positions = (0, 0) after fallbacks, so
    # the one valid node [10, 20] lands at the drop point + (10, 20).
    assert (110.0, 120.0) in positions
    assert (100.0, 100.0) in positions  # invalid ones fell back to [0, 0]


# ── widget-level fixtures ───────────────────────────────────────────────


def _built_editor(graph: NodeGraph) -> NodeEditorWidget:
    editor = NodeEditorWidget()
    editor.resize(800, 600)
    editor.build_from_graph(graph)
    return editor


def _widget_graph() -> NodeGraph:
    graph = NodeGraph()
    graph.add_node(ConstantNode(node_id="source"))
    graph.add_node(BlendNode(node_id="blend"))
    graph.add_node(OutputNode(node_id="output"))
    assert graph.connect("source", "result", "blend", "A")
    assert graph.connect("blend", "result", "output", "image")
    return graph


# ── AUD-10500: Output identity by node_type in the widget ───────────────


@pytest.mark.usefixtures("qapp")
def test_aud_10500_renamed_output_still_found_and_protected():
    graph = _widget_graph()
    editor = _built_editor(graph)
    try:
        out_item = editor._node_items["output"]
        out_item.set_title("Final Delivery")   # user rename (F2)
        out_item.set_selected(True)

        # Preview pipeline still resolves the Output node item.
        assert editor._find_output_node_item() is out_item

        # Delete key cannot delete it (title-based filter used to pass it).
        editor._delete_selected()
        assert graph.get_node("output") is not None
        assert "output" in editor._node_items

        # Inline rename is refused for protected types.
        editor.begin_node_rename("output")
        assert out_item._rename_proxy is None
    finally:
        editor.close()


# ── AUD-10501: backdrop batch delete emits graph_changed ────────────────


@pytest.mark.usefixtures("qapp")
def test_aud_10501_backdrop_batch_delete_marks_graph_dirty():
    graph = _widget_graph()
    editor = _built_editor(graph)
    try:
        editor.add_backdrop_visual({
            "id": "bd-1", "title": "T", "color": "#FFFFFF",
            "position": [0, 0], "size": [100, 100],
        })
        item = editor._backdrop_items.get("bd-1")
        assert item is not None
        item.setSelected(True)

        dirty_spy = _Spy()
        deleted_spy = _Spy()
        editor.graph_changed.connect(dirty_spy)
        editor.backdrops_deleted.connect(deleted_spy)

        editor._delete_selected()

        assert deleted_spy.calls == [(["bd-1"], {"bd-1": item.to_dict()})]
        assert dirty_spy.calls, "backdrop delete must mark the project dirty"
        assert "bd-1" not in editor._backdrop_items
    finally:
        editor.close()


# ── AUD-10502: video metadata probed off the UI thread ──────────────────


@pytest.mark.usefixtures("qapp")
def test_aud_10502_drop_event_probes_metadata_asynchronously(qapp):
    from src.features.composite.nodes.video_frame_node import VideoFrameNode
    graph = NodeGraph()
    graph.add_node(VideoFrameNode(node_id="vf-1"))
    graph.get_node("vf-1").set_parameter("file_path", "/tmp/clip.mp4")
    editor = _built_editor(graph)
    try:
        probe_threads: List[str] = []
        probe_done = threading.Event()

        def fake_probe(path: str) -> Dict[str, Any]:
            probe_threads.append(threading.current_thread().name)
            probe_done.set()
            return {"fps": 24.0, "total_frames": 240,
                    "video_width": 1920, "video_height": 1080}

        editor._probe_video_metadata = fake_probe  # type: ignore[assignment]
        backfilled = _Spy()
        editor.video_metadata_probed.connect(backfilled)

        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile("/tmp/clip.mp4")])
        event = QDropEvent(
            QPointF(50.0, 50.0), Qt.DropAction.CopyAction, mime,
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
        editor.dropEvent(event)

        node = graph.get_node("vf-1")
        assert node.parameters.get("file_path") == "/tmp/clip.mp4"

        assert probe_done.wait(timeout=5.0), "probe worker never ran"
        # The probe ran on a worker thread — never synchronously inside
        # dropEvent on the UI (Main) thread.
        assert probe_threads and probe_threads[0] != "MainThread"
        for _ in range(200):
            qapp.processEvents()
            if backfilled.calls:
                break
            time.sleep(0.01)
        assert backfilled.calls, "probed metadata never backfilled"
        assert node.parameters.get("total_frames") == 240
        assert node.parameters.get("fps") == 24.0
    finally:
        editor.close()


# ── AUD-10503: media-pool drag emits assets_dropped ─────────────────────


def _pool_mime() -> QMimeData:
    mime = QMimeData()
    mime.setData(MIME_TYPE_MEDIA_POOL_ASSETS,
                 json.dumps(["asset-1", "asset-2"]).encode("utf-8"))
    return mime


@pytest.mark.usefixtures("qapp")
def test_aud_10503_media_pool_drag_enter_and_drop():
    graph = _widget_graph()
    editor = _built_editor(graph)
    try:
        # The event does not own the mime data — keep references alive.
        mime_enter = _pool_mime()
        enter = QDragEnterEvent(
            QPoint(10, 10), Qt.DropAction.CopyAction, mime_enter,
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
        editor.dragEnterEvent(enter)
        assert enter.isAccepted(), "pool drag must be accepted by the canvas"

        dropped = _Spy()
        editor.assets_dropped.connect(dropped)
        mime_drop = _pool_mime()
        drop = QDropEvent(
            QPointF(10.0, 10.0), Qt.DropAction.CopyAction, mime_drop,
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
        scene_pos = editor.mapToScene(QPoint(10, 10))
        editor.dropEvent(drop)

        assert dropped.calls == [(
            ["asset-1", "asset-2"], (scene_pos.x(), scene_pos.y()))]
        # No file-import fallback happened for the pool drag.
        assert all(n.node_type not in ("image_input", "video_frame")
                   for n in graph.nodes.values())
    finally:
        editor.close()


@pytest.mark.usefixtures("qapp")
def test_aud_10503_plain_object_still_rejected():
    graph = _widget_graph()
    editor = _built_editor(graph)
    try:
        mime = QMimeData()  # keep alive: the event does not own it
        enter = QDragEnterEvent(
            QPoint(10, 10), Qt.DropAction.CopyAction, mime,
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
        editor.dragEnterEvent(enter)
        assert not enter.isAccepted()
    finally:
        editor.close()


# ── AUD-10504: Ctrl+click applies selection exactly once ────────────────


def _ctrl_press(editor: NodeEditorWidget, local: QPointF) -> None:
    event = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(local), QPointF(local),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.ControlModifier,
    )
    editor.mousePressEvent(event)


@pytest.mark.usefixtures("qapp")
def test_aud_10504_ctrl_click_toggles_selection_consistently():
    graph = _widget_graph()
    editor = _built_editor(graph)
    try:
        item = editor._node_items["blend"]
        item.setPos(200.0, 200.0)  # away from ports, inside node body
        local = editor.mapFromScene(QPointF(260.0, 240.0))

        selected_spy = _Spy()
        editor.nodes_selected.connect(selected_spy)
        editor.node_selected.connect(selected_spy)

        assert not item.isSelected()
        _ctrl_press(editor, local)
        # Old bug: manual toggle + Qt's toggle cancelled out → the visual
        # selection bounced back to unselected while the signal claimed it
        # had been added.
        assert item.isSelected() is True
        assert selected_spy.calls, "selection signal must report the change"

        selected_spy.calls.clear()
        _ctrl_press(editor, local)
        assert item.isSelected() is False  # toggles back off, once
    finally:
        editor.close()


# ── AUD-10505: _batch_mode restored on failure ──────────────────────────


@pytest.mark.usefixtures("qapp")
def test_aud_10505_batch_mode_survives_add_node_failure():
    graph = _widget_graph()
    editor = _built_editor(graph)
    try:
        def boom(*_a: Any, **_k: Any) -> None:
            raise RuntimeError("registry exploded")

        assert editor._actions is not None
        editor._actions.add_node = boom  # type: ignore[assignment]
        with pytest.raises(RuntimeError):
            editor._add_new_node("blur", QPointF(0.0, 0.0),
                                 source_node_id="source")
        assert editor._batch_mode is False, \
            "exception path must not leave batch mode stuck on"
    finally:
        editor.close()


@pytest.mark.usefixtures("qapp")
def test_aud_10505_outer_batch_mode_not_cleared():
    graph = _widget_graph()
    editor = _built_editor(graph)
    try:
        editor._batch_mode = True  # outer transaction already batching
        editor._add_new_node("blur", QPointF(0.0, 0.0),
                             source_node_id="source")
        assert editor._batch_mode is True, \
            "inner operation must not terminate an outer batch"
        assert any(n.node_type == "blur" for n in graph.nodes.values())
    finally:
        editor.close()


# ── AUD-10506: corrupted viewport state cannot break the build ──────────


@pytest.mark.usefixtures("qapp")
def test_aud_10506_set_viewport_state_validates_types():
    graph = _widget_graph()
    editor = _built_editor(graph)
    try:
        # Nulls / strings / lists for existing keys used to raise TypeError.
        editor.set_viewport_state({
            "zoom": None, "center_x": "bad", "center_y": [1, 2]})
        assert editor._current_zoom == 1.0

        editor.set_viewport_state({"zoom": True})   # bool rejected too
        assert editor._current_zoom == 1.0

        editor.set_viewport_state({"zoom": 2.0, "center_x": 5, "center_y": -5})
        assert editor._current_zoom == 2.0
    finally:
        editor.close()


# ── AUD-10602: measured (not predicted) reconnect links ─────────────────


@pytest.mark.usefixtures("qapp")
def test_aud_10602_delete_command_measures_reconnected_links():
    # Blend has TWO image input ports but only one connected — the engine
    # reconnects by connection count, the old port-count prediction did not.
    graph = NodeGraph()
    graph.add_node(ConstantNode(node_id="source"))
    graph.add_node(BlendNode(node_id="blend"))
    graph.add_node(OutputNode(node_id="output"))
    assert graph.connect("source", "result", "blend", "A")
    assert graph.connect("blend", "result", "output", "image")
    original = _quads(graph)

    cmd = DeleteNodeCommand(graph=graph, node_ids=["blend"])
    cmd.redo()

    assert graph.get_node("blend") is None
    assert ("source", "result", "output", "image") in _quads(graph), \
        "engine must have reconnected around the deleted node"
    # The measured record captured the engine-created link.
    assert cmd._reconnected_links == [{
        "from_node": "source", "from_port": "result",
        "to_node": "output", "to_port": "image"}]

    cmd.undo()
    # Undo restores the exact original topology — no leaked reconnection.
    assert _quads(graph) == original
    assert graph.get_node("blend") is not None


def test_aud_10602_reconnect_prediction_helper_removed():
    # The divergent port-count predictor must be gone for good.
    assert not hasattr(DeleteNodeCommand, "_calculate_reconnection_links")
