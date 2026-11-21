"""P3 tests: KeyframesPanel static UI + dock registration.

Validates that the panel can be instantiated, binds to a model, emits the
expected signals, and that its PanelDescriptor is registered so it appears
under Window > Panels.
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from PyQt6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PyQt6.QtGui import QDragEnterEvent, QDropEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QWidget

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.core.layer_model import BOTTOM_UP_LAYER_STACK_VERSION
from src.features.composite.core.layer_stack_controller import LayerStackController
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.nodes.constant_node import ConstantNode
from src.features.composite.ui.composite_page import CompositePage
from src.features.composite.ui.keyframes_panel import KeyframesPanel
from src.features.composite.ui.node_properties_panel import NodePropertiesPanel
from src.features.composite.ui.keyframes_model import (
    KeyframesModel, CompositeTimelineState)


@pytest.fixture
def graph():
    g = NodeGraph()
    g.add_node(TransformNode())
    g.add_node(OutputNode())
    return g


# ── panel instantiation ─────────────────────────────────────────────────

def test_panel_instantiates(qapp):
    panel = KeyframesPanel()
    assert panel.objectName() == "CompositeKeyframesPanel"
    assert panel.minimumHeight() >= 180


def test_panel_has_model_and_frame_mapper(qapp):
    panel = KeyframesPanel()
    assert isinstance(panel._model, KeyframesModel)
    assert panel._frame_mapper is not None


# ── model binding ───────────────────────────────────────────────────────

def test_panel_binds_graph(qapp, graph):
    panel = KeyframesPanel()
    panel.set_node_graph(graph)
    # model has rows for both nodes
    assert len(panel._model.rows) >= 2


def test_panel_emits_current_frame_changed(qapp, graph):
    panel = KeyframesPanel()
    panel.set_node_graph(graph)
    received = []
    panel.current_frame_changed.connect(received.append)
    panel._timeline_view.set_current_frame(42)
    panel._timeline_view.current_frame_changed.emit(42)
    assert 42 in received


def test_panel_set_current_frame_updates_label(qapp, graph):
    panel = KeyframesPanel()
    panel.set_node_graph(graph)
    panel.set_current_frame(7)
    assert panel._frame_label.text() == "7"
    assert panel._state.current_frame == 7


def test_layer_delete_key_emits_selected_layer(qapp):
    """Delete/Backspace on a selected Layers row reaches the host boundary."""
    g = NodeGraph()
    g.add_node(OutputNode(node_id="out"))
    source = ConstantNode(node_id="source")
    g.add_node(source)
    controller = LayerStackController(g, undo_stack=None)
    layer_id = controller.add_media_layer("Source", source.id)

    panel = KeyframesPanel()
    panel.set_layer_stack_controller(controller)
    panel.set_layer_view_mode(KeyframesModel.VIEW_MODE_LAYERS)
    panel.set_node_graph(g, selected_layer_id=layer_id)

    received = []
    panel.layer_delete_requested.connect(received.append)
    from PyQt6.QtCore import QEvent, Qt
    from PyQt6.QtGui import QKeyEvent
    panel._tree_view.keyPressEvent(QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_Delete,
        Qt.KeyboardModifier.NoModifier))

    assert received == [layer_id]


def test_timeline_side_maps_media_drop_to_layer_insert(qapp):
    """Dropping on the right timeline uses the same Layer insertion logic."""
    g = NodeGraph()
    g.add_node(OutputNode(node_id="out"))
    source = ConstantNode(node_id="source")
    g.add_node(source)
    controller = LayerStackController(g, undo_stack=None)
    layer_id = controller.add_media_layer("Source", source.id)

    panel = KeyframesPanel()
    panel.set_layer_stack_controller(controller)
    panel.set_layer_view_mode(KeyframesModel.VIEW_MODE_LAYERS)
    panel.set_node_graph(g)

    calls = []
    handler = lambda mime, target, index: calls.append((target, index)) or True
    panel.set_media_drop_handler(handler)
    from PyQt6.QtCore import QMimeData
    mime = QMimeData()
    mime.setData("application/x-topos-media-pool-assets", b'["asset"]')

    assert panel._handle_timeline_media_drop(handler, mime, 30.0) is True
    assert calls and calls[0][0] == layer_id
    assert calls[0][1] in (0, 1)


@pytest.mark.parametrize("surface", ["_tree_view", "_timeline_view"])
@pytest.mark.parametrize("extension, accepted", [("png", True), ("mov", True),
                                                 ("obj", True), ("wav", False)])
def test_empty_layer_canvas_accepts_supported_local_drops(
    qtbot, surface: str, extension: str, accepted: bool,
) -> None:
    panel = KeyframesPanel()
    qtbot.addWidget(panel)
    panel.set_layer_view_mode(KeyframesModel.VIEW_MODE_LAYERS)
    panel.set_composition_active(False)
    calls = []
    panel.set_media_drop_handler(lambda mime, target, index: calls.append((target, index)) or True)
    panel.resize(900, 310)
    panel.show()

    canvas = getattr(panel, surface)
    assert canvas.isEnabled() and canvas.acceptDrops()
    assert not panel._toolbar.isEnabled()
    assert panel.findChild(QWidget, "KeyframesEmptyStateOverlay") is None
    if surface == "_timeline_view":
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(150, 24))
        assert panel._state.current_frame == 0

    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(f"/tmp/drop/first.{extension}")])
    pos = QPoint(40, 80)
    event_args = (Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton,
                  Qt.KeyboardModifier.NoModifier)
    enter = QDragEnterEvent(pos, *event_args)
    QApplication.sendEvent(canvas, enter)
    assert enter.isAccepted() is accepted
    if accepted:
        drop = QDropEvent(QPointF(pos), *event_args)
        QApplication.sendEvent(canvas, drop)
        assert drop.isAccepted()
        assert calls == [(None, 0)]
    else:
        assert not calls


def test_node_material_delete_removes_layer_transform_merge_and_undoes(qapp):
    """Node-mode deletion reverse-syncs the Layers panel in one undo step."""
    page = CompositePage()
    g = NodeGraph()
    g.add_node(OutputNode(node_id="out"))
    first = ConstantNode(node_id="first")
    second = ConstantNode(node_id="second")
    g.add_node(first)
    g.add_node(second)
    controller = LayerStackController(g, undo_stack=page._undo_stack)
    first_layer = controller.add_media_layer("First", first.id)
    second_layer = controller.add_media_layer("Second", second.id)
    second_root = g.get_layer(second_layer).root_node_id
    merge_id = controller.main_stack().generated_merge_node_ids[0]

    page._node_graph = g
    page._layer_stack_controller = controller
    page._evaluate_graph = MagicMock()
    page._refresh_keyframes_panel = MagicMock()
    page._node_editor.build_from_graph(g)
    assert page._node_editor._actions.delete_node(second.id)

    assert g.get_layer(second_layer) is None
    assert g.get_node(second.id) is None
    assert g.get_node(second_root) is None
    assert g.get_node(merge_id) is None
    assert g.get_layer(first_layer) is not None

    page.undo()
    assert g.get_layer(second_layer) is not None
    assert g.get_node(second.id) is not None
    assert g.get_node(second_root) is not None
    assert g.get_node(merge_id) is not None


def test_node_transform_delete_keeps_material_layer_and_source(qapp):
    """Deleting only a layer Transform must preserve its Keyframes row."""
    page = CompositePage()
    g = NodeGraph()
    g.add_node(OutputNode(node_id="out"))
    first = ConstantNode(node_id="first")
    second = ConstantNode(node_id="second")
    g.add_node(first)
    g.add_node(second)
    controller = LayerStackController(g, undo_stack=page._undo_stack)
    first_layer = controller.add_media_layer("First", first.id)
    second_layer = controller.add_media_layer("Second", second.id)
    second_root = g.get_layer(second_layer).root_node_id
    merge_id = controller.main_stack().generated_merge_node_ids[0]

    page._node_graph = g
    page._layer_stack_controller = controller
    page._evaluate_graph = MagicMock()
    page._refresh_keyframes_panel = MagicMock()
    page._node_editor.build_from_graph(g)
    assert page._node_editor._actions.delete_node(second_root)

    assert g.get_layer(second_layer) is not None
    assert g.get_node(second.id) is not None
    assert g.get_node(second_root) is None
    assert g.get_node(merge_id) is not None
    assert g.get_layer(second_layer).root_node_id == second.id
    assert g.get_layer(first_layer) is not None

    page.undo()
    assert g.get_node(second_root) is not None
    assert g.get_layer(second_layer).root_node_id == second_root
    page.redo()
    page._node_editor._add_new_node("transform", QPointF(100, 100), second.id)
    new_root = g.get_layer(second_layer).root_node_id
    assert new_root not in (second.id, second_root)
    assert (new_root, "result", merge_id, "A") in page._graph_cable_keys()


@pytest.mark.parametrize("method", ["menu", "toolbar", "drop", "input_first", "output_first"])
def test_reinserting_transform_restores_layer_controls(qapp, method):
    page = CompositePage()
    panel = KeyframesPanel()
    properties = NodePropertiesPanel()
    page.set_properties_panel(properties)
    graph = page.node_graph
    controller = page._layer_stack_controller
    editor = page._node_editor
    page._evaluate_graph = MagicMock()
    graph.clear()
    graph.add_node(OutputNode(node_id="out"))
    graph.add_node(ConstantNode(node_id="source"))
    side = TransformNode(node_id="side")
    graph.add_node(side)
    layer_id = controller.add_media_layer("Footage", "source")
    original_id = graph.get_layer(layer_id).root_node_id
    merge = controller.layer_merge_node(layer_id)
    graph.connect("source", "result", side.id, "image")
    editor.build_from_graph(graph)
    page.set_keyframes_panel(panel)
    panel.set_view_mode("layers")
    try:
        assert controller.main_stack().version == BOTTOM_UP_LAYER_STACK_VERSION
        assert editor._actions.delete_node(original_id)
        assert graph.get_layer(layer_id).root_node_id == "source"

        if method == "menu":
            editor._add_new_node("transform", QPointF(100, 100), "source")
        elif method == "toolbar":
            editor.select_node("source")
            page._add_node_by_type(TransformNode, (100, 100))
        else:
            node = editor._actions.add_node("transform", 100, 100)
            assert node is not None
            if method == "drop":
                editor._move_start_positions = {node.id: QPointF(100, 100)}
                editor._drop_insert_nearest_point = QPointF(100, 200)
                editor._drop_insert_connection = next(
                    value for value in editor._connection_items if value[3:] == (merge.id, "A"))
                editor._execute_connection_insertion()
                assert not editor._move_start_positions
            else:
                cables = [("source", "result", node.id, "image"),
                          (node.id, "result", merge.id, "A")]
                if method == "output_first":
                    cables.reverse()
                assert editor._actions.connect(*cables[0])
                assert graph.get_layer(layer_id).root_node_id == "source"
                assert editor._actions.connect(*cables[1])

        root_id = graph.get_layer(layer_id).root_node_id
        assert root_id not in ("source", side.id, original_id)
        assert ("source", "result", side.id, "image") in page._graph_cable_keys()
        assert (root_id, "result", merge.id, "A") in page._graph_cable_keys()
        assert any(row.layer_id == layer_id and row.node_id == root_id
                   and row.param_name == "translate_x" for row in panel._model.rows)

        page.undo()
        assert graph.get_layer(layer_id).root_node_id == "source"
        assert not any(row.layer_id == layer_id and row.node_id == root_id
                       for row in panel._model.rows)
        if method in {"menu", "toolbar"}:
            assert graph.get_node(root_id) is None
        page.redo()
        assert graph.get_layer(layer_id).root_node_id == root_id

        panel.parameter_changed.emit(root_id, "translate_x", 123.0)
        page._commit_pending_edit_transactions()
        assert graph.get_node(root_id).parameters["translate_x"] == 123.0
        page.load_composite_data(page.get_composite_data())
        assert page.node_graph.get_layer(layer_id).source_node_id == "source"
        assert page.node_graph.get_layer(layer_id).root_node_id == root_id
        assert page.node_graph.get_node(root_id).parameters["translate_x"] == 123.0
        assert any(row.layer_id == layer_id and row.node_id == root_id
                   and row.param_name == "translate_x" for row in panel._model.rows)
    finally:
        page.close()
        panel.close()
        properties.close()


def test_effect_chain_head_keeps_transform_role_in_layers_view(qapp):
    """B01: with an effect above the layer Transform, the Layers view keeps
    transform semantics and the role survives a save/reload round-trip."""
    from src.features.composite.nodes.blur_node import BlurNode
    from src.features.composite.core.layer_stack_compiler import MERGE_PORT_FOREGROUND

    page = CompositePage()
    panel = KeyframesPanel()
    properties = NodePropertiesPanel()
    page.set_properties_panel(properties)
    graph = page.node_graph
    controller = page._layer_stack_controller
    editor = page._node_editor
    page._evaluate_graph = MagicMock()
    graph.clear()
    graph.add_node(OutputNode(node_id="out"))
    graph.add_node(ConstantNode(node_id="source"))
    blur = BlurNode(node_id="blur_above")
    graph.add_node(blur)
    layer_id = controller.add_media_layer("Footage", "source")
    layer = graph.get_layer(layer_id)
    transform_id = layer.transform_node_id
    merge = controller.layer_merge_node(layer_id)
    assert transform_id is not None and transform_id != layer.source_node_id

    graph.disconnect(transform_id, "result", merge.id, MERGE_PORT_FOREGROUND)
    assert graph.connect(transform_id, "result", blur.id, "image")
    assert graph.connect(blur.id, "result", merge.id, MERGE_PORT_FOREGROUND)
    assert controller.adopt_wired_layer_effect(
        layer_id, blur.id, "result", (merge.id, MERGE_PORT_FOREGROUND))
    editor.build_from_graph(graph)
    page.set_keyframes_panel(panel)
    panel.set_view_mode("layers")
    try:
        assert graph.get_layer(layer_id).root_node_id == blur.id
        # The Layers view shows the registered Transform's params under the
        # transform group, not the Blur chain head's.
        assert controller.layer_transform_node(layer_id).id == transform_id
        assert any(row.layer_id == layer_id and row.node_id == transform_id
                   and row.param_name == "translate_x"
                   for row in panel._model.rows)
        # Adding an effect through the panel splices ahead of the Transform
        # instead of wrapping the Blur in a second Transform.
        effect_id = controller.add_layer_effect(layer_id, "blur")
        assert effect_id is not None
        assert [n.id for n in graph.nodes.values()
                if n.node_type == "transform"] == [transform_id]

        page.load_composite_data(page.get_composite_data())
        restored = page.node_graph.get_layer(layer_id)
        assert restored.root_node_id == blur.id
        assert restored.transform_node_id == transform_id
        assert page._layer_stack_controller.layer_transform_node(
            layer_id).id == transform_id
    finally:
        page.close()
        panel.close()
        properties.close()


@pytest.mark.parametrize("reason", ["other_layer", "locked", "managed_conflict"])
def test_refused_transform_drop_preserves_existing_cables(qapp, reason):
    page = CompositePage()
    page._evaluate_graph = MagicMock()
    graph = page.node_graph
    editor = page._node_editor
    controller = page._layer_stack_controller
    graph.clear()
    graph.add_node(OutputNode(node_id="out"))
    graph.add_node(ConstantNode(node_id="first"))
    graph.add_node(ConstantNode(node_id="second"))
    first_layer = controller.add_media_layer("First", "first")
    second_layer = controller.add_media_layer("Second", "second")
    root_id = graph.get_layer(second_layer).root_node_id
    merge_id = controller.layer_merge_node(second_layer).id
    if reason == "other_layer":
        transform_id = graph.get_layer(first_layer).root_node_id
    else:
        transform_id = "candidate"
        graph.add_node(TransformNode(node_id=transform_id))
        if reason == "locked":
            assert controller.set_layer_locked(second_layer, True)
        else:
            graph.connect("first", "result", "out", "image")  # Conflict beyond the target wire.
    editor.build_from_graph(graph)
    try:
        edges = page._graph_cable_keys()
        editor._move_start_positions = {transform_id: QPointF(100, 100)}
        editor._drop_insert_connection = next(
            value for value in editor._connection_items if value[3:] == (merge_id, "A"))
        editor._execute_connection_insertion()
        assert editor._drop_insert_connection is None
        assert page._graph_cable_keys() == edges
        assert graph.get_layer(second_layer).root_node_id == root_id
        if reason == "locked":
            nodes = set(graph.nodes)
            editor._add_new_node("transform", QPointF(100, 100), root_id)
            assert set(graph.nodes) == nodes
            assert not editor._actions.connect(transform_id, "result", merge_id, "A")
            assert page._graph_cable_keys() == edges
    finally:
        page.close()


# ── timeline state ──────────────────────────────────────────────────────

def test_panel_accepts_timeline_state(qapp):
    panel = KeyframesPanel()
    state = CompositeTimelineState(start_frame=0, end_frame=100, current_frame=10)
    panel.set_timeline_state(state)
    assert panel._state.current_frame == 10


# ── registry ────────────────────────────────────────────────────────────

def _fresh_registry_with_composite():
    """Build a registry with CompositeModule panels registered (mirrors how
    test_panel_dock_integrity sets things up)."""
    from src.shared.panels.registry import PanelRegistry
    from src.features.composite.module import CompositeModule
    reg = PanelRegistry()
    try:
        CompositeModule().register_panels(reg)
    except Exception:
        pass
    return reg


def test_keyframes_panel_registered():
    """P3: the panel must be registered so Window > Panels lists it."""
    reg = _fresh_registry_with_composite()
    desc = reg.get("composite.keyframes")
    assert desc is not None
    assert desc.fallback_title == "Layer Timeline"
    assert desc.dock_attr == "dock_composite_keyframes"
    assert desc.category == "Composite"
    assert "Composite" in desc.page_ids


def test_scopes_panel_registered_and_visible_by_default():
    """Composite Scopes must be reachable from Window > Panels."""
    reg = _fresh_registry_with_composite()
    desc = reg.get("composite.scopes")
    assert desc is not None
    assert desc.fallback_title == "Scopes"
    assert desc.title_key == "panel.composite_scopes"
    assert desc.dock_attr == "dock_composite_scope"
    assert desc.category == "Composite"
    assert desc.default_visible is True
    assert "Composite" in desc.page_ids


def test_scopes_dock_is_added_to_existing_composite_layout_once():
    """Old Composite layouts must reveal the newly registered dock once."""
    from PyQt6.QtCore import QByteArray
    from src.app.controllers.dock_layout_controller import DockLayoutMixin

    class _Config:
        def __init__(self):
            self.data = {"page_layouts": {"Composite": "legacy-layout"}}
            self.saved = False

        def get(self, key):
            return self.data.get(key)

        def set(self, key, value, auto_save=True):
            self.data[key] = value

        def save_config(self):
            self.saved = True

    host = MagicMock()
    host.config_manager = _Config()
    host.current_page = "Composite"
    host.dock_composite_scope = MagicMock()
    host.page_layouts = {}
    host.saveState.return_value = QByteArray(b"composite-with-scopes")
    host._save_current_layout_to_store = MagicMock()

    DockLayoutMixin._migrate_composite_scopes_dock_visibility(host)

    host.dock_composite_scope.setVisible.assert_called_once_with(True)
    assert host.config_manager.data["composite_scopes_dock_layout_version"] == 1
    assert host.config_manager.data["page_layouts"]["Composite"] == \
        "636f6d706f736974652d776974682d73636f706573"
    host._save_current_layout_to_store.assert_called_once_with("Composite")
    assert host.config_manager.saved is True

    DockLayoutMixin._migrate_composite_scopes_dock_visibility(host)
    host.dock_composite_scope.setVisible.assert_called_once_with(True)


def test_spline_panel_registered():
    """Phase 3: the Spline panel is registered under Window > Panels."""
    reg = _fresh_registry_with_composite()
    desc = reg.get("composite.spline")
    assert desc is not None
    assert desc.fallback_title == "Spline"
    assert desc.dock_attr == "dock_composite_spline"
    assert desc.default_visible is False


def test_legacy_animation_panel_is_not_registered():
    """Model3D animation is owned by Keyframes, not a second dock."""
    reg = _fresh_registry_with_composite()
    assert reg.get("composite.animation") is None


def test_import_report_panel_registered():
    """F0: Model3D Import Report appears in the Composite Panels menu."""
    reg = _fresh_registry_with_composite()
    desc = reg.get("composite.import_report")
    assert desc is not None
    assert desc.fallback_title == "Import Report"
    assert desc.dock_attr == "dock_composite_import_report"
    assert desc.default_visible is False


# ── keyframe navigation ─────────────────────────────────────────────────

def test_prev_next_keyframe_navigation(qapp):
    from src.shared.keyframe import Keyframe, KeyframeTrack
    g = NodeGraph()
    t = TransformNode()
    track = KeyframeTrack(param_name="rotation")
    track.add_keyframe(Keyframe(0, 0.0))
    track.add_keyframe(Keyframe(10, 45.0))
    track.add_keyframe(Keyframe(20, 90.0))
    t.set_keyframe_track("rotation", track)
    g.add_node(t)

    panel = KeyframesPanel()
    panel.set_node_graph(g)
    panel.set_current_frame(5)
    # next keyframe after 5 is 10
    nxt = panel._nearest_keyframe(5, +1)
    assert nxt == 10
    # prev keyframe before 5 is 0
    prv = panel._nearest_keyframe(5, -1)
    assert prv == 0


def test_fit_time_range(qapp):
    panel = KeyframesPanel()
    panel.resize(800, 300)
    state = CompositeTimelineState(start_frame=0, end_frame=100)
    panel.fit_time_range()
    # after fit, the mapper should have a sensible ppf
    assert panel._frame_mapper.pixels_per_frame > 0
