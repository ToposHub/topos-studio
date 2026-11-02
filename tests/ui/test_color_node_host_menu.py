"""Host node menus operate on the UI draft and keep shortcuts canvas-local."""

from __future__ import annotations

import inspect
import os
from collections.abc import Iterator
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction, QKeySequence
from PyQt6.QtWidgets import QApplication, QLineEdit, QMainWindow, QVBoxLayout, QWidget

from src.app.controllers.menu_toolbar_controller import MenuToolbarMixin
from src.features.color.ui.node_editor_panel import ColorNodePanel
from src.features.color.ui.node_editor_state import NodeKind
from src.shared.ui.shortcut_manager import ShortcutManager


class _Config:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {"global_shortcuts": True, "custom_shortcuts": {}}

    def get(self, key: str) -> Any:
        return self.values.get(key)

    def set(self, key: str, value: Any) -> None:
        self.values[key] = value

    def save_config(self) -> None:
        pass


class _Host(QMainWindow, MenuToolbarMixin):
    def __init__(self) -> None:
        super().__init__()
        self.undo_calls = 0
        self.redo_calls = 0
        self.shortcut_manager = ShortcutManager(_Config())
        central = QWidget(self)
        layout = QVBoxLayout(central)
        self.color_node_panel = ColorNodePanel(central)
        self.editor = QLineEdit("Keep typing", central)
        layout.addWidget(self.color_node_panel)
        layout.addWidget(self.editor)
        self.setCentralWidget(central)
        self.menu_edit = self.menuBar().addMenu("Edit")
        self.actions_by_name = self._create_color_node_menu(self.menu_edit)
        self._register_color_node_shortcuts(self.actions_by_name)

    @property
    def engine(self) -> Any:
        raise AssertionError("Node-menu draft actions must not access ImageEngine")

    def _forbidden_layer_callback(self, *_args: Any) -> None:
        raise AssertionError("Legacy layer-controller callback invoked")

    on_layer_added = _forbidden_layer_callback
    on_layer_removed = _forbidden_layer_callback
    on_layer_reset = _forbidden_layer_callback
    on_layer_copy = _forbidden_layer_callback
    on_layer_paste = _forbidden_layer_callback
    on_layer_duplicate = _forbidden_layer_callback
    rename_current_layer = _forbidden_layer_callback
    toggle_current_layer_visibility = _forbidden_layer_callback
    disable_all_layers = _forbidden_layer_callback

    def undo(self) -> None:
        self.undo_calls += 1

    def redo(self) -> None:
        self.redo_calls += 1


@pytest.fixture
def host(qapp: QApplication, qtbot: Any) -> Iterator[_Host]:
    widget = _Host()
    widget.resize(1000, 550)
    qtbot.addWidget(widget)
    widget.show()
    widget.activateWindow()
    qapp.processEvents()
    yield widget
    widget.close()


def test_setup_uses_node_menu_instead_of_legacy_layer_callbacks() -> None:
    source = inspect.getsource(MenuToolbarMixin.setup_menus)
    assert "self._create_color_node_menu(edit_menu)" in source
    for callback in ("on_layer_added", "on_layer_removed", "on_layer_reset",
                     "on_layer_copy", "on_layer_paste", "on_layer_duplicate",
                     "rename_current_layer", "toggle_current_layer_visibility", "disable_all_layers"):
        assert f"connect(self.{callback}" not in source
    assert 'sm.register("layer.' not in inspect.getsource(MenuToolbarMixin._register_all_shortcuts)


@pytest.mark.parametrize("action_name", [
    "add_layer_act", "del_layer_act", "rename_layer_act", "toggle_vis_act", "disable_all_act",
    "reset_layer_act", "copy_layer_act", "paste_layer_act", "duplicate_layer_act",
])
def test_main_node_menu_actions_never_touch_legacy_backend(host: _Host, qtbot: Any, action_name: str) -> None:
    view = host.color_node_panel.view
    node_id = view.state.graph.active_id
    if action_name in {"reset_layer_act", "copy_layer_act"}:
        view.state.set_node_fields({node_id}, grade={"exposure": 2.0})
        view.rebuild()
    if action_name == "paste_layer_act":
        view._copied_grade = {"exposure": 3.0}
    host.editor.setFocus()
    host.actions_by_name[action_name].trigger()
    if action_name in {"add_layer_act", "duplicate_layer_act"}:
        assert len(view.state.graph.nodes) == 4
    elif action_name == "del_layer_act":
        assert node_id not in view.state.graph.nodes
    elif action_name == "rename_layer_act":
        assert QApplication.focusWidget() is view
        assert view._label is not None
        qtbot.keyClicks(view, "Node label")
        qtbot.keyClick(view, Qt.Key.Key_Return)
        qtbot.waitUntil(lambda: view._label is None)
        assert view.state.graph.nodes[node_id].label == "Node label"
    elif action_name in {"toggle_vis_act", "disable_all_act"}:
        assert not view.state.graph.nodes[node_id].enabled
    elif action_name == "reset_layer_act":
        assert view.state.graph.nodes[node_id].grade == {}
    elif action_name == "copy_layer_act":
        assert view._copied_grade == {"exposure": 2.0}
    else:
        assert view.state.graph.nodes[node_id].grade == {"exposure": 3.0}
    assert host.undo_calls == host.redo_calls == 0


@pytest.mark.parametrize("name,kind", [
    ("parallel_node_act", NodeKind.PARALLEL_MIXER),
    ("layer_node_act", NodeKind.LAYER_MIXER),
    ("splitter_node_act", NodeKind.SPLITTER),
])
def test_branch_menu_actions_edit_only_the_draft(host: _Host, name: str, kind: NodeKind) -> None:
    host.editor.setFocus()
    host.actions_by_name[name].trigger()
    assert any(node.kind == kind for node in host.color_node_panel.view.state.graph.nodes.values())


def test_outside_and_serial_before_menu_actions(host: _Host) -> None:
    view = host.color_node_panel.view
    original = view.state.graph.active_id
    host.actions_by_name["serial_before_act"].trigger()
    inserted = view.state.graph.active_id
    assert any(edge.src_node == inserted and edge.dst_node == original for edge in view.state.graph.connections)
    host.actions_by_name["outside_node_act"].trigger()
    outside = view.state.graph.active_id
    assert view.state.graph.nodes[outside].grade["key_invert"] is True
    assert any(edge.src_node == inserted and edge.dst_node == outside and edge.src_port == "key_out"
               for edge in view.state.graph.connections)


def test_node_registry_ids_and_context_survive_global_mode(host: _Host) -> None:
    view = host.color_node_panel.view
    registered = host.shortcut_manager.get_actions_by_category("Nodes")
    assert len(registered) == 14
    assert all(action.id.startswith("color_nodes.") for action in registered)
    for global_mode in (False, True):
        host.shortcut_manager.set_global_mode(global_mode)
        for action in host.actions_by_name.values():
            assert action.property("force_widget_shortcut") is True
            assert action.shortcutContext() == Qt.ShortcutContext.WidgetShortcut
            assert action in view.actions()
    assert host.actions_by_name["add_layer_act"].shortcut() == QKeySequence("Alt+S")
    assert host.actions_by_name["del_layer_act"].shortcut() == QKeySequence("Backspace")
    assert host.actions_by_name["disable_all_act"].shortcut() == QKeySequence("Alt+Ctrl+D")
    for name in ("rename_layer_act", "reset_layer_act", "duplicate_layer_act"):
        assert host.actions_by_name[name].shortcut().isEmpty()


def test_shortcuts_leave_other_input_widgets_untouched(host: _Host, qtbot: Any) -> None:
    view = host.color_node_panel.view
    host.editor.setFocus()
    host.editor.setCursorPosition(len(host.editor.text()))
    before = set(view.state.graph.nodes)
    qtbot.keyClick(host.editor, Qt.Key.Key_Backspace)
    assert host.editor.text() == "Keep typin"
    qtbot.keyClick(host.editor, Qt.Key.Key_S, modifier=Qt.KeyboardModifier.AltModifier)
    qtbot.keyClick(host.editor, Qt.Key.Key_D, modifier=Qt.KeyboardModifier.ControlModifier)
    assert set(view.state.graph.nodes) == before
    assert view.state.graph.nodes[view.state.graph.active_id].enabled


def test_canvas_shortcut_has_one_effect_with_host_action_installed(host: _Host, qtbot: Any) -> None:
    view = host.color_node_panel.view
    view.setFocus()
    intents: list[str] = []
    view.edit_requested.connect(lambda action, payload: intents.append(action))
    qtbot.keyClick(view, Qt.Key.Key_S, modifier=Qt.KeyboardModifier.AltModifier)
    assert len(view.state.graph.nodes) == 4
    assert intents == ["add_serial"]
    node_id = view.state.graph.active_id
    qtbot.keyClick(view, Qt.Key.Key_D, modifier=Qt.KeyboardModifier.ControlModifier)
    assert not view.state.graph.nodes[node_id].enabled
    assert intents[-1] == "set_enabled"


def test_history_routes_focused_node_panel_and_falls_back_elsewhere(host: _Host) -> None:
    view = host.color_node_panel.view
    view.apply_edit("add_serial", view.state.add_serial)
    view.setFocus()
    host._route_color_node_history(False)
    assert len(view.state.graph.nodes) == 3
    assert host.undo_calls == 0
    host._route_color_node_history(True)
    assert len(view.state.graph.nodes) == 4
    assert host.redo_calls == 0
    host.editor.setFocus()
    host._route_color_node_history(False)
    host._route_color_node_history(True)
    assert (host.undo_calls, host.redo_calls) == (1, 1)
    assert len(view.state.graph.nodes) == 4


def test_menu_refresh_keeps_registered_action_identities(host: _Host) -> None:
    identities = {name: id(action) for name, action in host.actions_by_name.items()}
    view = host.color_node_panel.view
    view.scene().clearSelection()
    host.menu_layer.aboutToShow.emit()
    assert not host.actions_by_name["del_layer_act"].isEnabled()
    assert not host.actions_by_name["rename_layer_act"].isEnabled()
    assert identities == {name: id(action) for name, action in host.actions_by_name.items()}
    assert host.actions_by_name["add_layer_act"].isEnabled()


def test_edit_menu_enables_local_history_when_legacy_history_is_empty(host: _Host) -> None:
    host.undo_action_menu = QAction("Undo", host)
    host.redo_action_menu = QAction("Redo", host)
    host.undo_action_menu.setEnabled(False)
    host.redo_action_menu.setEnabled(False)
    view = host.color_node_panel.view
    view.apply_edit("add_serial", view.state.add_serial)
    view.setFocus()
    host.menu_edit.aboutToShow.emit()
    assert host.undo_action_menu.isEnabled()
    assert not host.redo_action_menu.isEnabled()
    host._route_color_node_history(False)
    host.menu_edit.aboutToShow.emit()
    assert not host.undo_action_menu.isEnabled()
    assert host.redo_action_menu.isEnabled()
