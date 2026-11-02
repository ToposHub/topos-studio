"""Presentation scopes, compound navigation and panel controls (no GPU)."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QMainWindow, QMessageBox, QRadioButton

from src.features.color.ui.node_editor_panel import ColorNodePanel
from src.features.color.ui.node_editor_state import NodeKind


@pytest.fixture
def panel(qtbot: Any) -> ColorNodePanel:
    panel = ColorNodePanel()
    panel.resize(720, 440)
    qtbot.addWidget(panel)
    panel.show()
    return panel


def action(menu: Any, key: str) -> QAction:
    result = menu.findChild(QAction, "node_action_" + key)
    assert result is not None
    return result


def test_scope_navigation_preserves_independent_histories(panel: ColorNodePanel) -> None:
    original = panel.view.state
    node_id = panel.view.apply_edit("add_serial", panel.view.state.add_serial)
    panel.set_scope("timeline")
    assert panel.scope == "timeline"
    assert node_id not in panel.view.state.graph.nodes
    assert not panel.view.state.can_undo
    panel.set_scope("clip")
    assert panel.view.state is original
    assert node_id in panel.view.state.graph.nodes
    panel.view.apply_edit("undo", panel.view.state.undo)
    assert node_id not in panel.view.state.graph.nodes


def test_group_modes_follow_membership(panel: ColorNodePanel) -> None:
    panel._build_scope_menu()
    groups = [a for a in panel.scope_menu.actions() if "group_" in a.objectName()]
    assert len(groups) == 2 and all(not a.isEnabled() for a in groups)
    panel.set_scope("group_pre_clip")
    assert panel.scope == "clip"
    panel.set_group_available(True)
    panel.set_scope("group_pre_clip")
    assert panel.scope == "group_pre_clip"
    panel.set_group_available(False)
    assert panel.scope == "clip"


def test_clicking_scope_radios_selects_target_scope(panel: ColorNodePanel, qtbot: Any) -> None:
    assert isinstance(panel.clip_radio, QRadioButton)
    assert isinstance(panel.timeline_radio, QRadioButton)
    qtbot.mouseClick(panel.timeline_radio, Qt.MouseButton.LeftButton)
    assert panel.scope == "timeline"
    assert panel.timeline_radio.isChecked() and not panel.clip_radio.isChecked()
    qtbot.mouseClick(panel.timeline_radio, Qt.MouseButton.LeftButton)
    assert panel.timeline_radio.isChecked() and panel.scope == "timeline"
    qtbot.mouseClick(panel.clip_radio, Qt.MouseButton.LeftButton)
    assert panel.scope == "clip"


def test_load_graph_is_projection_not_edit(panel: ColorNodePanel) -> None:
    edits: list[str] = []
    panel.edit_requested.connect(lambda name, _: edits.append(name))
    graph = panel.snapshot()
    graph.nodes[graph.active_id].label = "Loaded"
    panel.load_graph(graph)
    assert panel.snapshot().nodes[graph.active_id].label == "Loaded"
    assert not edits
    assert not panel.view.state.can_undo


def test_intent_envelope_has_scope_path_and_draft(panel: ColorNodePanel) -> None:
    edits: list[tuple[str, dict[str, Any]]] = []
    panel.edit_requested.connect(lambda name, data: edits.append((name, data)))
    node_id = panel.view.apply_edit("add_serial", panel.view.state.add_serial)
    name, data = edits[-1]
    assert name == "add_serial"
    assert data["scope"] == "clip"
    assert data["compound_path"] == []
    assert node_id in data["payload"]["draft"].nodes
    assert data["payload"]["result"] == node_id


def _compound(panel: ColorNodePanel) -> tuple[str, str]:
    first = panel.view.state.graph.active_id
    second = panel.view.state.add_serial()
    compound = panel.view.state.make_compound({first, second})
    panel.view.rebuild()
    return compound, first


def test_compound_edits_commit_on_back_and_are_undoable(panel: ColorNodePanel) -> None:
    compound, child_id = _compound(panel)
    panel.open_compound(compound)
    assert panel.breadcrumb.isVisible()
    panel.view.apply_edit("label_node", lambda: panel.view.state.set_node_fields({child_id}, label="Skin"))
    assert panel.close_compound()
    node = panel.view.state.graph.nodes[compound]
    assert node.children.nodes[child_id].label == "Skin"
    assert not panel.breadcrumb.isVisible()
    panel.view.apply_edit("undo", panel.view.state.undo)
    assert panel.view.state.graph.nodes[compound].children.nodes[child_id].label == ""


def test_compound_boundary_rejection_does_not_loop_on_scope_change(
    panel: ColorNodePanel, monkeypatch: Any,
) -> None:
    compound, _ = _compound(panel)
    panel.open_compound(compound)
    child_ids = set(panel.view.state.graph.nodes)
    panel.view.apply_edit("delete_nodes", lambda: panel.view.state.delete_nodes(child_ids))
    messages: list[str] = []
    monkeypatch.setattr(QMessageBox, "information", lambda *args: messages.append(str(args[2])))
    panel.set_scope("timeline")
    assert panel.scope == "clip"
    assert panel._parents[-1][1] == compound
    assert len(messages) == 1
    panel.view.apply_edit("undo", panel.view.state.undo)
    assert panel.close_compound()


def test_user_scope_switch_finishes_label_in_departing_scope(panel: ColorNodePanel, qtbot: Any) -> None:
    node_id = panel.view.state.graph.active_id
    panel.view.label_selected()
    qtbot.keyClicks(panel.view, "Balance")
    panel.set_scope("timeline")
    qtbot.wait(1)
    panel.set_scope("clip")
    assert panel.view.state.graph.nodes[node_id].label == "Balance"


def test_shared_node_can_be_made_local_while_locked(panel: ColorNodePanel) -> None:
    view = panel.view
    node_id = view.state.graph.active_id
    action(view.build_context_menu(), "shared").trigger()
    assert view.state.graph.nodes[node_id].shared_id
    assert view.state.graph.nodes[node_id].locked
    action(view.build_context_menu(), "local").trigger()
    assert not view.state.graph.nodes[node_id].shared_id
    assert not view.state.graph.nodes[node_id].locked


def test_alpha_output_can_be_removed_with_undo(panel: ColorNodePanel) -> None:
    view = panel.view
    node_id = view.apply_edit("add_alpha_output", view.state.add_alpha_output)
    view.state.select({node_id}, node_id)
    action(view.build_context_menu(), "remove_alpha_output").trigger()
    assert node_id not in view.state.graph.nodes
    view.apply_edit("undo", view.state.undo)
    assert view.state.graph.nodes[node_id].kind == NodeKind.ALPHA_OUTPUT


def test_options_resets_and_visibility_controls(panel: ColorNodePanel) -> None:
    view = panel.view
    first = view.state.graph.active_id
    view.state.set_node_fields({first}, grade={"exposure": 1.0})
    view.state.add_serial()
    action(panel.build_options_menu(), "reset_keep_nodes").trigger()
    assert len(view.state.graph.nodes) == 4
    assert not view.state.graph.nodes[first].grade
    action(panel.build_options_menu(), "show_grid").trigger()
    assert not view.show_grid
    action(panel.build_options_menu(), "show_thumbnails").trigger()
    assert not view.show_thumbnails
    action(panel.build_options_menu(), "reset_all").trigger()
    assert len(view.state.graph.nodes) == 3
    action(panel.build_options_menu(), "undo").trigger()
    assert len(view.state.graph.nodes) == 4


def test_zoom_slider_sync_does_not_recurse(panel: ColorNodePanel) -> None:
    emitted: list[float] = []
    panel.view.zoom_changed.connect(emitted.append)
    panel.zoom_slider.setValue(150)
    assert panel.view.transform().m11() == pytest.approx(1.5)
    panel.view.set_zoom(0.5)
    assert panel.zoom_slider.value() == 50
    assert len(emitted) == 2


def test_hand_mode_changes_navigation_only(panel: ColorNodePanel, qtbot: Any) -> None:
    before = panel.snapshot()
    qtbot.mouseClick(panel.hand_button, Qt.MouseButton.LeftButton)
    assert panel.view._hand_mode
    qtbot.mouseClick(panel.pointer_button, Qt.MouseButton.LeftButton)
    assert not panel.view._hand_mode
    assert panel.snapshot() == before


def test_noop_does_not_emit_backend_intent(panel: ColorNodePanel) -> None:
    edits: list[str] = []
    panel.edit_requested.connect(lambda name, _: edits.append(name))
    panel.view.apply_edit("undo", panel.view.state.undo)
    assert not edits


def test_selecting_node_does_not_emit_edit(panel: ColorNodePanel) -> None:
    edits: list[str] = []
    panel.edit_requested.connect(lambda name, _: edits.append(name))
    first = panel.view.state.graph.active_id
    panel.view.state.select({first}, first)
    panel.view.rebuild()
    assert not edits


def test_reference_toolbar_centers_scope_dots_and_keeps_full_height_dividers(panel: ColorNodePanel, qapp: Any) -> None:
    for width in (720, 1008, 1400):
        panel.resize(width, 440)
        qapp.processEvents()
        first = panel.clip_radio.mapTo(panel.toolbar, panel.clip_radio.rect().center()).x()
        second = panel.timeline_radio.mapTo(panel.toolbar, panel.timeline_radio.rect().center()).x()
        assert abs((first + second) / 2 - width / 2) <= 2
        assert panel.toolbar.height() == 30
        assert panel.tool_separator.width() == panel.menu_separator.width() == 1
        assert panel.tool_separator.height() == panel.menu_separator.height() == 28
        assert panel.tool_separator.mapTo(panel.toolbar, panel.tool_separator.rect().topLeft()).x() == 64
        assert panel.menu_separator.mapTo(panel.toolbar, panel.menu_separator.rect().topLeft()).x() == width - 36
