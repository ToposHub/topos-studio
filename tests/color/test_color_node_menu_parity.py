"""Actual node menu payloads preserve domain data and projection state."""
from dataclasses import replace

import pytest

from tests.color.test_color_graph_live_ui import live
from src.app.controllers.color_graph_controller import graph_payload_to_draft
from src.features.color.core.color_graph_service import ColorGraphService
from src.features.color.ui.node_editor_state import NodeKind


def test_node_menu_color_roundtrip_uses_resolve_palette(live):
    harness, _ = live
    view = harness.panel.view
    selected = set(view.state.graph.selected_ids)
    view.apply_edit('node_color', lambda: view.state.set_node_fields(selected, color='#b55148'))
    assert view.state.graph.nodes[view.state.graph.active_id].color == '#b55148'


def test_layer_blend_option_survives_projection_without_primary_grade_pollution(live):
    harness, stack_id = live
    view = harness.panel.view
    view.apply_edit('add_layer', lambda: view.state.add_branch(NodeKind.LAYER_MIXER))
    mixer = next(node for node in view.state.graph.nodes.values() if node.kind == NodeKind.LAYER_MIXER)
    view.state.select({mixer.id}, mixer.id)
    view.active_node_changed.emit(mixer.id)
    view._set_grade_value('blend_mode', 'Screen')
    assert view.state.graph.nodes[mixer.id].node_options['blend_mode'] == 'Screen'
    assert 'blend_mode' not in view.state.graph.nodes[mixer.id].grade


@pytest.mark.parametrize('kind', [NodeKind.PARALLEL_MIXER, NodeKind.LAYER_MIXER, NodeKind.KEY_MIXER])
def test_add_input_adds_a_visible_port_for_all_mixers(live, kind):
    harness, _ = live
    view = harness.panel.view
    if kind == NodeKind.KEY_MIXER:
        view.apply_edit('add_key_mixer', view.state.add_key_mixer)
    else:
        action = 'add_parallel' if kind == NodeKind.PARALLEL_MIXER else 'add_layer'
        view.apply_edit(action, lambda: view.state.add_branch(kind))
    mixer = next(node for node in view.state.graph.nodes.values() if node.kind == kind)
    view.state.select({mixer.id}, mixer.id)
    view.active_node_changed.emit(mixer.id)
    before = mixer.input_count
    view.apply_edit('add_input', lambda: view.state.set_node_fields({mixer.id}, input_count=before + 1))
    assert view.state.graph.nodes[mixer.id].input_count == before + 1


def test_clipboard_paste_preserves_complete_legacy_grade_payload(live):
    harness, stack_id = live
    view = harness.panel.view
    original_id = view.state.graph.active_id
    view.copy_grade()
    original = harness.holder['project'].color_stacks[stack_id].color_graph
    source = next(node for node in original.nodes if node.node_id == original_id)
    before = set(view.state.graph.nodes)
    view.apply_edit('paste_nodes', lambda: view.state.paste_nodes(view._copied_graph, view._copied_graph.selected_ids))
    added = set(view.state.graph.nodes) - before
    assert len(added) == 1
    pasted = next(node for node in harness.holder['project'].color_stacks[stack_id].color_graph.nodes if node.node_id in added)
    assert pasted.legacy_payload['grading_params'] == source.legacy_payload['grading_params']
    assert pasted.legacy_payload['id'] != source.legacy_payload['id']


def test_duplicate_compound_rewrites_child_identities(live):
    harness, stack_id = live
    view = harness.panel.view
    view.apply_edit('make_compound', lambda: view.state.make_compound(set(view.state.graph.selected_ids)))
    compound = next(node for node in view.state.graph.nodes.values() if node.kind == NodeKind.COMPOUND)
    graph = harness.holder['project'].color_stacks[stack_id].color_graph
    result = ColorGraphService().op_duplicate_subgraph(graph, [compound.id])
    assert result.ok, result.diagnostics
    nodes = [node for node in result.graph.nodes if node.node_type == 'compound']
    assert len(nodes) == 2
    first_ids = {node.node_id for node in nodes[0].compound_children.nodes}
    second_ids = {node.node_id for node in nodes[1].compound_children.nodes}
    assert not first_ids & second_ids
    assert not result.graph.validate()


def test_move_and_insert_ui_gesture_reaches_atomic_domain_command(live):
    harness, stack_id = live
    view = harness.panel.view
    view.apply_edit('add_node', lambda: view.state.add_node(NodeKind.CORRECTOR, 10, 30))
    detached = view.state.graph.active_id
    edge = next(item for item in view.state.graph.connections if item.src_node != detached and item.dst_node != detached)
    view.apply_edit('move_and_insert', lambda: view.state.move_nodes_and_insert({detached: (50, 60)}, detached, edge), {detached: (50, 60)})
    graph = harness.holder['project'].color_stacks[stack_id].color_graph
    assert any(item.src_node == edge.src_node and item.dst_node == detached for item in graph.edges)
    assert any(item.src_node == detached and item.dst_node == edge.dst_node for item in graph.edges)
    assert graph.ui_state.positions[detached] == (50, 60)


def test_locked_node_rejects_grade_and_reset_until_unlocked(live):
    harness, stack_id = live
    graph = harness.holder['project'].color_stacks[stack_id].color_graph
    target = harness.panel.view.state.graph.active_id
    service = ColorGraphService()
    locked = service.op_set_edit_lock(graph, [target], True)
    changed = service.op_set_node_option(locked.graph, target, 'grade_patch', ('exposure', 30.0))
    assert not changed.ok and changed.diagnostics[0].code == 'node_locked'
    assert not service.op_reset_corrector_parameters(locked.graph, [target]).ok
    assert service.op_set_node_metadata(locked.graph, target, 'Locked grade').ok
    unlocked = service.op_set_edit_lock(locked.graph, [target], False)
    assert service.op_set_node_option(unlocked.graph, target, 'grade_patch', ('exposure', 30.0)).ok


def test_node_transform_and_cache_menus_wired_with_real_commands(live):
    """N05/N06 接线后的菜单 parity 契约：Color Space/Gamma/Node Cache
    菜单启用且子项即真实命令值（GL/CPU 已消费；Metal/导出显式拒绝），
    LUT 菜单保持 R02+R05 契约。"""
    harness, _ = live
    menu = harness.panel.view.build_context_menu()

    def submenu(title: str):
        action = next(action for action in menu.actions()
                      if action.menu() is not None and action.text() == title)
        assert action.menu().isEnabled(), title
        return action.menu()

    color_space = submenu('Color Space')
    gamma = submenu('Gamma')
    cache = submenu('Node Cache')
    cs_texts = {action.text() for action in color_space.actions()}
    gamma_texts = {action.text() for action in gamma.actions()}
    cache_texts = {action.text() for action in cache.actions()}
    assert {'Use Timeline', 'Rec.709', 'Rec.2020', 'P3-D65', 'ACES AP0',
            'ACES AP1', 'DaVinci Wide Gamut'} <= cs_texts
    assert {'Use Timeline', 'Linear', 'Gamma 2.2', 'Gamma 2.4', 'sRGB',
            'ST2084', 'HLG', 'DaVinci Intermediate'} <= gamma_texts
    assert {'Auto', 'On', 'Off'} <= cache_texts
    lut_menu = submenu('LUT')
    texts = [action.text() for action in lut_menu.actions()]
    assert 'No LUT' in texts and 'Browse…' in texts
