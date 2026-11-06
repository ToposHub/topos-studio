"""Exercise real panel payloads against the project bridge, not fake intents."""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction

from tests.app.test_color_graph_app_host import _Harness, _project_with_graph_stack
from src.app.controllers.color_graph_controller import ColorGraphIntentNormalizer, EnvelopeError
from src.features.color.ui.node_editor_state import ConnectionDraft, NodeKind
from src.shared.project.color_graph import ColorGraphData
import pytest


@pytest.fixture
def live(qapp, qtbot):
    project, stack_id, _ = _project_with_graph_stack()
    harness = _Harness(qapp, project)
    qtbot.addWidget(harness.panel)
    harness.panel.show()
    harness.host.on_clip_stack_loaded(stack_id)
    return harness, stack_id


def test_hydration_selects_a_real_corrector(live):
    harness, _ = live
    active = harness.panel.view.state.graph.active_id
    assert active in harness.panel.snapshot().nodes
    assert harness.host.panel_host.active_handle.node_id == active


def test_project_acknowledgment_keeps_zoom_and_canvas_position(live):
    harness, stack_id = live
    view = harness.panel.view
    harness.panel.zoom_slider.setValue(75)
    before = view.viewportTransform()
    node_id = view.state.graph.active_id
    view.apply_edit("label_node", lambda: view.state.set_node_fields({node_id}, label="Balance"),
                    {"node_id": node_id, "label": "Balance"})
    assert view.viewportTransform() == before
    assert harness.panel.zoom_slider.value() == 75
    graph = harness.holder["project"].color_stacks[stack_id].color_graph
    assert next(node.label for node in graph.nodes if node.node_id == node_id) == "Balance"


def test_alpha_connection_roundtrips_through_project_history(live):
    harness, stack_id = live
    view = harness.panel.view
    corrector = view.state.graph.active_id
    view.apply_edit("add_alpha_output", view.state.add_alpha_output)
    alpha = next(node.id for node in view.state.graph.nodes.values() if node.kind == NodeKind.ALPHA_OUTPUT)
    edge = ConnectionDraft(corrector, "key_out", alpha, "key_in")
    view.apply_edit("connect", lambda: view.state.connect(edge), edge)
    graph = harness.holder["project"].color_stacks[stack_id].color_graph
    assert any(connection.dst_node == alpha and connection.dst_port == "key" for connection in graph.edges)
    assert edge in view.state.graph.connections
    assert any(link.connection == edge for link in view.links)
    view.apply_edit("undo", view.state.undo)
    assert edge not in view.state.graph.connections
    view.apply_edit("redo", view.state.redo)
    assert edge in view.state.graph.connections


def test_serial_inserts_after_selected_corrector_and_keyboard_undo(live, qtbot):
    harness, stack_id = live
    view = harness.panel.view
    graph = harness.holder['project'].color_stacks[stack_id].color_graph
    anchor = next(node.node_id for node in graph.nodes if node.node_type == 'corrector')
    view.state.select({anchor}, anchor)
    view.active_node_changed.emit(anchor)
    qtbot.keyClick(view, Qt.Key.Key_S, modifier=Qt.KeyboardModifier.AltModifier)
    updated = harness.holder['project'].color_stacks[stack_id].color_graph
    new_ids = {node.node_id for node in updated.nodes} - {node.node_id for node in graph.nodes}
    assert len(new_ids) == 1
    assert any(edge.src_node == anchor and edge.dst_node in new_ids for edge in updated.edges)
    assert view.state.graph.active_id in new_ids
    assert view.project_history_available(False)
    qtbot.keyClick(view, Qt.Key.Key_Z, modifier=Qt.KeyboardModifier.ControlModifier)
    restored = harness.holder['project'].color_stacks[stack_id].color_graph
    assert restored.to_dict() == graph.to_dict()
    assert not view.project_history_available(False)
    assert view.project_history_available(True)


def test_repeated_serial_shortcut_persists_a_single_row(live, qtbot) -> None:
    harness, stack_id = live
    view = harness.panel.view
    correctors = [node for node in view.state.graph.nodes.values() if node.kind == NodeKind.CORRECTOR]
    first, last = correctors[0], correctors[-1]
    assert not harness.holder["project"].color_stacks[stack_id].color_graph.ui_state.positions
    view.state.select(set())
    view.active_node_changed.emit("")
    for _ in range(3):
        qtbot.keyClick(view, Qt.Key.Key_S, modifier=Qt.KeyboardModifier.AltModifier)
    graph = harness.holder["project"].color_stacks[stack_id].color_graph
    saved = ColorGraphData.from_dict(graph.to_dict())
    positions = saved.ui_state.positions
    node_id = first.id
    chain = []
    while node_id != saved.output_node_id:
        chain.append(positions[node_id])
        node_id = next(edge.dst_node for edge in saved.edges if edge.src_node == node_id)
    assert len(chain) == len(correctors) + 3
    assert len({y for _, y in chain}) == 1
    assert chain[0] == (first.x, first.y)
    assert chain[-1] == positions[last.id]
    assert all(right[0] - left[0] >= 96 for left, right in zip(chain, chain[1:]))
    assert all(positions[node.id] == (node.x, node.y) for node in view.state.graph.nodes.values())
    assert len(harness.cmd_stack._undo_stack) == 3


def test_canvas_reset_keeps_one_neutral_corrector_and_undoes_as_one_edit(live) -> None:
    from PyQt6.QtCore import QPoint

    harness, stack_id = live
    view = harness.panel.view
    original = harness.holder["project"].color_stacks[stack_id].color_graph
    menu = view.build_context_menu(QPoint(10, 10))
    menu.findChild(QAction, "node_action_reset_all").trigger()
    reset = harness.holder["project"].color_stacks[stack_id].color_graph
    [node] = [node for node in reset.nodes if node.node_type == "corrector"]
    assert reset.source_node_id == original.source_node_id
    assert reset.output_node_id == original.output_node_id
    assert {(edge.src_node, edge.dst_node) for edge in reset.edges} == {
        (reset.source_node_id, node.node_id), (node.node_id, reset.output_node_id),
    }
    assert node.enabled and not node.legacy_payload and "grade" not in node.params
    assert view.state.graph.active_id == node.node_id
    assert view.cards[node.node_id].node.number == 1
    assert not view.cards[node.node_id].node.tool_indicators
    assert len(harness.cmd_stack._undo_stack) == 1
    view.apply_edit("undo", view.state.undo)
    assert harness.holder["project"].color_stacks[stack_id].color_graph.to_dict() == original.to_dict()
    view.apply_edit("redo", view.state.redo)
    assert harness.holder["project"].color_stacks[stack_id].color_graph.to_dict() == reset.to_dict()


def test_real_connection_payload_disconnects_and_reconnects(live):
    harness, stack_id = live
    view = harness.panel.view
    node_id = view.state.graph.active_id
    edge = next(edge for edge in view.state.graph.connections if edge.dst_node == node_id)
    view.apply_edit('disconnect', lambda: view.state.disconnect(edge), edge)
    graph = harness.holder['project'].color_stacks[stack_id].color_graph
    assert not any(item.src_node == edge.src_node and item.dst_node == edge.dst_node for item in graph.edges)
    view.apply_edit('connect', lambda: view.state.connect(edge), edge)
    graph = harness.holder['project'].color_stacks[stack_id].color_graph
    assert any(item.src_node == edge.src_node and item.dst_node == edge.dst_node for item in graph.edges)
    assert len(harness.cmd_stack._undo_stack) == 2


def test_malformed_raw_arguments_are_contract_error_not_type_error():
    normalizer = ColorGraphIntentNormalizer('s', 'owner', 'stack', 0)
    with pytest.raises(EnvelopeError):
        normalizer.normalize('disconnect', {'scope': 'clip', 'payload': {'arguments': [ConnectionDraft('a','image_out','b','image_in')]}})


def test_compound_survives_projection_and_can_be_opened(live):
    harness, stack_id = live
    view = harness.panel.view
    selected = {view.state.graph.active_id}
    view.apply_edit('make_compound', lambda: view.state.make_compound(selected))
    compound = next(node for node in view.state.graph.nodes.values() if node.kind == NodeKind.COMPOUND)
    assert compound.children is not None
    harness.panel.open_compound(compound.id)
    assert harness.panel._parents


def test_multiple_wires_disconnect_as_one_project_command(live):
    harness, stack_id = live
    view = harness.panel.view
    edges = list(view.state.graph.connections)[:2]
    view.apply_edit('disconnect', lambda: view.state.disconnect_many(edges), edges)
    graph = harness.holder['project'].color_stacks[stack_id].color_graph
    assert len(graph.edges) == len(view.state.graph.connections)
    assert len(harness.cmd_stack._undo_stack) == 1
    harness.host.undo()
    assert all(edge in view.state.graph.connections for edge in edges)


def test_nested_edit_stays_inside_compound_and_close_is_navigation(live):
    harness, stack_id = live
    view = harness.panel.view
    selected = {view.state.graph.active_id}
    view.apply_edit('make_compound', lambda: view.state.make_compound(selected))
    compound = next(node for node in view.state.graph.nodes.values() if node.kind == NodeKind.COMPOUND)
    harness.panel.open_compound(compound.id)
    node_id = next(node.id for node in view.state.graph.nodes.values() if node.kind == NodeKind.CORRECTOR)
    view.state.select({node_id}, node_id)
    view.active_node_changed.emit(node_id)
    view.apply_edit('label_node', lambda: view.state.set_node_fields({node_id}, label='Inside'), {'node_id': node_id, 'label': 'Inside'})
    graph = harness.holder['project'].color_stacks[stack_id].color_graph
    node = next(node for node in graph.nodes if node.node_id == compound.id)
    assert next(child for child in node.compound_children.nodes if child.node_id == node_id).label == 'Inside'
    assert harness.panel._parents
    assert view.state.graph.nodes[node_id].label == 'Inside'
    depth = len(harness.cmd_stack._undo_stack)
    harness.panel.close_compound()
    assert not harness.panel._parents
    assert len(harness.cmd_stack._undo_stack) == depth
    handle = harness.host.panel_host.active_handle
    parent_active = view.state.graph.active_id
    if handle is not None:
        assert handle.node_id == parent_active
        assert handle.node_id != node_id
    assert harness.host.active_grade_node_id() != node_id


def test_missing_layout_has_unique_positions_and_display_numbers(live):
    harness, _ = live
    nodes = list(harness.panel.snapshot().nodes.values())
    assert len({(node.x, node.y) for node in nodes}) == len(nodes)
    numbered = [node.number for node in nodes if node.kind == NodeKind.CORRECTOR]
    assert numbered == list(range(1, len(numbered) + 1))


def test_multi_parameter_grade_gesture_accumulates_instead_of_last_field_only(live):
    harness, stack_id = live
    node_id = harness.panel.view.state.graph.active_id
    harness.host.begin_grade_gesture('wheel')
    assert harness.host.submit_grade_change(node_id, 'exposure', 12.0, 'wheel')
    assert harness.host.submit_grade_change(node_id, 'saturation', 1.3, 'wheel')
    assert harness.host.end_grade_gesture()
    graph = harness.holder['project'].color_stacks[stack_id].color_graph
    node = next(node for node in graph.nodes if node.node_id == node_id)
    assert node.params['grade']['exposure'] == 12.0
    assert node.params['grade']['saturation'] == 1.3
    assert len(harness.cmd_stack._undo_stack) == 1


def test_reset_discards_migrated_legacy_grade_and_primary_copy_paste_works(live):
    harness, stack_id = live
    view = harness.panel.view
    ids = [node.id for node in view.state.graph.nodes.values() if node.kind == NodeKind.CORRECTOR]
    source_id, target_id = ids[:2]
    view.state.select({source_id}, source_id)
    view.active_node_changed.emit(source_id)
    harness.host.begin_grade_gesture('source')
    assert harness.host.submit_grade_change(source_id, 'exposure', 23.0, 'source')
    assert harness.host.end_grade_gesture()
    view.copy_grade()
    view.state.select({target_id}, target_id)
    view.active_node_changed.emit(target_id)
    view.paste_grade()
    graph = harness.holder['project'].color_stacks[stack_id].color_graph
    target = next(node for node in graph.nodes if node.node_id == target_id)
    assert target.params['grade']['exposure'] == 23.0
    view.apply_edit('reset_grade', lambda: view.state.reset_grades({target_id}))
    graph = harness.holder['project'].color_stacks[stack_id].color_graph
    target = next(node for node in graph.nodes if node.node_id == target_id)
    assert not target.params.get('grade')
    assert target.legacy_payload['grading_params']['exposure'] == 0


def test_enable_and_delete_selected_nodes_commit(live):
    harness, stack_id = live
    view = harness.panel.view
    node_id = view.state.graph.active_id
    view.apply_edit('set_enabled', lambda: view.state.set_node_fields({node_id}, enabled=False), {'ids': [node_id], 'enabled': False})
    graph = harness.holder['project'].color_stacks[stack_id].color_graph
    assert not next(node for node in graph.nodes if node.node_id == node_id).enabled
    view.apply_edit('delete_nodes', lambda: view.state.delete_nodes({node_id}))
    graph = harness.holder['project'].color_stacks[stack_id].color_graph
    assert node_id not in {node.node_id for node in graph.nodes}


def test_unknown_edit_rolls_back_optimistic_ui(live):
    harness, _ = live
    view = harness.panel.view
    node_id = view.state.graph.active_id
    original_label = view.state.graph.nodes[node_id].label
    view.apply_edit('unsupported_edit', lambda: view.state.set_node_fields({node_id}, label='Phantom'))
    assert view.state.graph.nodes[node_id].label == original_label
    assert not harness.cmd_stack.can_undo()


@pytest.mark.parametrize('kind', [NodeKind.PARALLEL_MIXER, NodeKind.LAYER_MIXER, NodeKind.KEY_MIXER])
def test_indexed_mixer_port_reconnect_preserves_other_slots(live, kind):
    harness, stack_id = live
    view = harness.panel.view
    if kind == NodeKind.KEY_MIXER:
        view.apply_edit('add_key_mixer', view.state.add_key_mixer)
    else:
        action = 'add_parallel' if kind == NodeKind.PARALLEL_MIXER else 'add_layer'
        view.apply_edit(action, lambda: view.state.add_branch(kind))
    mixer = next(node for node in view.state.graph.nodes.values() if node.kind == kind)
    edge = next(edge for edge in view.state.graph.connections if edge.dst_node == mixer.id)
    original_count = len(view.state.graph.connections)
    view.apply_edit('disconnect', lambda: view.state.disconnect(edge), edge)
    view.apply_edit('connect', lambda: view.state.connect(edge), edge)
    assert edge in view.state.graph.connections
    assert len(view.state.graph.connections) == original_count
