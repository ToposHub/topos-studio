"""Exercise resource interactions without an engine, project, or renderer."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
from PyQt6.QtWidgets import QApplication, QWidget

from src.app.controllers.color_resource_preview import ColorResourcePreview
from src.features.color.ui.effects_browser_panel import ColorEffectsBrowser
from src.features.color.ui.lut_browser_panel import ColorLutBrowser
from src.features.color.ui.node_editor_panel import ColorNodePanel
from src.features.color.ui.node_editor_state import NodeKind
from src.features.color.ui.node_resource_mime import resource_mime_data


CATALOG = [
    {'id': 'grain', 'kind': 'effect', 'name': 'Film Grain', 'category': 'Texture',
     'parameters': [{'var_name': 'amount', 'label': 'Amount', 'type': 'slider', 'min': 0.0, 'max': 1.0, 'default': 0.2}]},
    {'id': 'warm', 'kind': 'lut', 'name': 'Warm', 'category': 'Film'},
]


def setup_host(qtbot: Any) -> tuple[Any, ColorResourcePreview]:
    host = QWidget()
    qtbot.addWidget(host)
    host.color_node_panel = ColorNodePanel(host)
    host.color_effects_browser = ColorEffectsBrowser(host)
    host.color_lut_browser = ColorLutBrowser(host)
    host.color_graph_host = SimpleNamespace(_coordinator=SimpleNamespace(session_id='clip-a', generation=0, scope_owner_id='a'), _bound_stack_id='stack-a')
    presenter = ColorResourcePreview(host, resources=CATALOG)
    return host, presenter


def test_effect_add_edit_order_reset_remove_are_local_only(qtbot: Any) -> None:
    host, presenter = setup_host(qtbot)
    graph = host.color_node_panel.view.state.snapshot()
    edits = []
    host.color_node_panel.edit_requested.connect(lambda *args: edits.append(args))
    presenter.activate(CATALOG[0])
    presenter.activate(CATALOG[0])
    draft = presenter._current_draft()
    assert len(draft['instances']) == 2
    first = draft['instances'][0]['instance_id']
    presenter.edit('set_parameter', {'instance_id': first, 'name': 'amount', 'value': 0.8})
    assert draft['instances'][0]['params']['amount'] == 0.8
    presenter.edit('set_enabled', {'instance_id': first, 'enabled': False})
    presenter.edit('move_instance', {'instance_id': first, 'index': 1})
    assert draft['instances'][1]['instance_id'] == first
    assert not draft['instances'][1]['enabled']
    presenter.edit('reset_instance', {'instance_id': first})
    assert draft['instances'][1]['params']['amount'] == 0.2
    presenter.edit('remove_instance', {'instance_id': first})
    assert len(draft['instances']) == 1
    assert host.color_node_panel.view.state.snapshot() == graph
    assert edits == []


def test_drafts_are_isolated_across_nodes_and_clip_sessions(qtbot: Any) -> None:
    host, presenter = setup_host(qtbot)
    view = host.color_node_panel.view
    first = view.state.graph.active_id
    presenter.activate(CATALOG[0])
    second = view.state.add_serial()
    view.rebuild()
    presenter.refresh_target()
    assert presenter._current_draft()['instances'] == []
    presenter.drop(first, {'kind': 'lut', 'resource_id': 'warm'})
    assert len(presenter._current_draft()['instances']) == 1
    assert presenter._current_draft()['lut_id'] == 'warm'
    host.color_graph_host._coordinator.session_id = 'clip-b'
    host.color_graph_host._coordinator.generation += 1
    # Even before hydration, stale identity is refused.
    assert presenter._current_draft() is None
    presenter.refresh_target()
    assert presenter._current_draft()['instances'] == []
    assert presenter._current_draft()['lut_id'] is None
    assert second != first


def test_locked_terminal_and_unknown_resources_do_not_apply(qtbot: Any) -> None:
    host, presenter = setup_host(qtbot)
    view = host.color_node_panel.view
    nid = view.state.graph.active_id
    view.state.graph.nodes[nid].locked = True
    presenter.refresh_target()
    presenter.activate(CATALOG[0])
    presenter.drop(nid, {'kind': 'effect', 'resource_id': 'grain'})
    assert presenter._drafts[presenter._active_key]['instances'] == []
    view.state.graph.nodes[nid].locked = False
    presenter.refresh_target()
    presenter.drop(nid, {'kind': 'effect', 'resource_id': 'missing'})
    assert presenter._current_draft()['instances'] == []
    terminal = next(n.id for n in view.state.graph.nodes.values() if n.kind == NodeKind.SOURCE)
    presenter.drop(terminal, {'kind': 'effect', 'resource_id': 'grain'})
    assert view.state.graph.active_id == nid


def test_real_qt_drag_highlights_and_drops_without_graph_edit(qtbot: Any) -> None:
    host, presenter = setup_host(qtbot)
    view = host.color_node_panel.view
    view.setParent(None)
    qtbot.addWidget(view)
    view.resize(900, 400)
    view.show()
    QApplication.processEvents()
    nid = view.state.graph.active_id
    card = view.cards[nid]
    point = view.mapFromScene(card.mapToScene(card.body_rect().center()))
    mime = resource_mime_data(CATALOG[0])
    edits = []
    view.edit_requested.connect(lambda *args: edits.append(args))
    enter = QDragEnterEvent(point, Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(view.viewport(), enter)
    assert enter.isAccepted()
    move = QDragMoveEvent(point, Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(view.viewport(), move)
    assert card.resource_drop_hover
    drop = QDropEvent(QPointF(point), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(view.viewport(), drop)
    assert drop.isAccepted()
    assert not card.resource_drop_hover
    assert len(presenter._current_draft()['instances']) == 1
    assert edits == []


def test_existing_node_payload_is_read_without_mutation(qtbot: Any) -> None:
    host, presenter = setup_host(qtbot)
    node = host.color_node_panel.view.state.graph.nodes[presenter._target_id]
    node.backend_payload = {'legacy_payload': {'plugins': [{'instance_id': 'saved', 'plugin_id': 'grain', 'enabled': False, 'params': {'amount': 0.7}}]}}
    presenter._drafts.clear()
    presenter.refresh_target()
    assert presenter._current_draft()['instances'][0]['params']['amount'] == 0.7
    presenter.edit('set_parameter', {'instance_id': 'saved', 'name': 'amount', 'value': 0.9})
    assert node.backend_payload['legacy_payload']['plugins'][0]['params']['amount'] == 0.7
