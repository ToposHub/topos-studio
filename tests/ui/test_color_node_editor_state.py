"""Interaction-state tests; no QApplication or rendering backend is required."""

import copy

import pytest

from src.features.color.ui.node_editor_state import (
    ConnectionDraft,
    GraphDraft,
    NodeDraft,
    NodeEditorState,
    NodeKind,
)


def image_edge(source: str, target: str) -> ConnectionDraft:
    return ConnectionDraft(source, "image_out", target, "image_in")


def test_serial_insert_before_and_undo_is_a_single_gesture() -> None:
    state = NodeEditorState()
    first = state.graph.active_id
    second = state.add_serial(before=True)
    assert image_edge("source", second) in state.graph.connections
    assert image_edge(second, first) in state.graph.connections
    assert state.undo()
    assert set(state.graph.nodes) == {"source", first, "output"}
    assert not state.can_undo
    assert state.redo()
    assert second in state.graph.nodes


def test_parallel_branches_share_input_and_reuse_mixer() -> None:
    state = NodeEditorState()
    first = state.graph.active_id
    second = state.add_branch(NodeKind.PARALLEL_MIXER)
    mixer = next(n for n in state.graph.nodes.values() if n.kind == NodeKind.PARALLEL_MIXER)
    assert image_edge("source", second) in state.graph.connections
    assert ConnectionDraft(first, "image_out", mixer.id, "image_in_0") in state.graph.connections
    assert ConnectionDraft(second, "image_out", mixer.id, "image_in_1") in state.graph.connections
    third = state.add_branch(NodeKind.PARALLEL_MIXER)
    assert len([n for n in state.graph.nodes.values() if n.kind == NodeKind.PARALLEL_MIXER]) == 1
    assert state.graph.nodes[mixer.id].input_count == 3
    assert image_edge("source", third) in state.graph.connections
    assert ConnectionDraft(third, "image_out", mixer.id, "image_in_2") in state.graph.connections
    assert image_edge(mixer.id, "output") in state.graph.connections


def test_outside_node_has_serial_image_and_inverted_external_key() -> None:
    state = NodeEditorState()
    first = state.graph.active_id
    outside = state.add_outside()
    assert image_edge(first, outside) in state.graph.connections
    assert ConnectionDraft(first, "key_out", outside, "key_in") in state.graph.connections
    assert state.graph.nodes[outside].grade["key_invert"] is True
    assert state.undo()
    assert outside not in state.graph.nodes


def test_rejected_cycle_keeps_previous_input_and_history() -> None:
    state = NodeEditorState()
    first = state.graph.active_id
    second = state.add_serial()
    state._undo.clear()
    edges = list(state.graph.connections)
    assert not state.connect(image_edge(second, first))
    assert state.graph.connections == edges
    assert image_edge("source", first) in state.graph.connections
    assert not state.can_undo


def test_signal_types_and_port_directions_cannot_be_crossed() -> None:
    state = NodeEditorState()
    first = state.graph.active_id
    second = state.add_serial()
    assert not state.connect(ConnectionDraft(first, "key_out", second, "image_in"))
    assert not state.connect(ConnectionDraft(first, "image_in", second, "image_out"))
    assert not state.connect(ConnectionDraft(first, "missing", second, "image_in"))
    alpha = state.add_alpha_output()
    assert state.connect(ConnectionDraft(first, "key_out", alpha, "key_in"))
    assert state.add_alpha_output() == alpha


def test_deleting_middle_chain_reconnects_source_and_keeps_other_fanout() -> None:
    state = NodeEditorState()
    first = state.graph.active_id
    second = state.add_serial()
    third = state.add_serial()
    other = state.add_node(NodeKind.CORRECTOR, 0.0, 150.0)
    assert state.connect(image_edge(first, other))
    assert state.delete_nodes([second, third])
    assert image_edge(first, "output") in state.graph.connections
    assert image_edge(first, other) in state.graph.connections
    assert state.undo()
    assert second in state.graph.nodes and third in state.graph.nodes


def test_deleting_mixer_does_not_choose_an_arbitrary_branch() -> None:
    state = NodeEditorState()
    state.add_branch(NodeKind.LAYER_MIXER)
    mixer = next(n.id for n in state.graph.nodes.values() if n.kind == NodeKind.LAYER_MIXER)
    assert state.delete_nodes([mixer])
    assert not any(c.dst_node == "output" for c in state.graph.connections)


def test_extract_and_drop_on_connection_is_reversible() -> None:
    state = NodeEditorState()
    first = state.graph.active_id
    second = state.add_serial()
    assert state.extract_nodes([second])
    edge = image_edge(first, "output")
    assert edge in state.graph.connections
    assert state.insert_on_connection(second, edge)
    assert image_edge(first, second) in state.graph.connections
    assert image_edge(second, "output") in state.graph.connections
    assert state.undo()
    assert edge in state.graph.connections
    assert not any(c.src_node == second or c.dst_node == second for c in state.graph.connections)


def test_compound_roundtrip_preserves_rgb_key_edges_and_members() -> None:
    state = NodeEditorState()
    first = state.graph.active_id
    second = state.add_outside()
    third = state.add_serial()
    assert state.connect(ConnectionDraft("source", "image_out", third, "image_in"))
    assert state.connect(ConnectionDraft(first, "key_out", third, "key_in"))
    before = state.snapshot()
    compound = state.make_compound([first, second])
    assert compound
    assert state.graph.nodes[compound].children is not None
    assert "key_out" in state.ports(compound)
    members = state.expand_compound(compound)
    assert set(members) == {first, second}
    assert set(state.graph.connections) == set(before.connections)
    assert set(state.graph.nodes) == set(before.nodes)


def test_nonconvex_compound_selection_is_rejected_atomically() -> None:
    state = NodeEditorState()
    first = state.graph.active_id
    state.add_serial()
    third = state.add_serial()
    edges = list(state.graph.connections)
    assert not state.make_compound([first, third])
    assert state.graph.connections == edges


def test_duplicate_remaps_internal_edges_and_copies_grade_data() -> None:
    state = NodeEditorState()
    first = state.graph.active_id
    second = state.add_serial()
    state.set_node_fields([first], grade={"curves": {"points": [[0.0, 0.0]]}})
    duplicates = state.duplicate_nodes([first, second])
    assert len(duplicates) == 2
    assert image_edge(*duplicates) in state.graph.connections
    assert not any(c.src_node == "source" and c.dst_node in duplicates for c in state.graph.connections)
    state.graph.nodes[first].grade["curves"]["points"][0][0] = 0.5
    assert state.graph.nodes[duplicates[0]].grade["curves"]["points"][0][0] == 0.0


def test_splitter_creates_three_channels_and_combiner_before_output() -> None:
    state = NodeEditorState()
    splitter, combiner = state.add_splitter_combiner()
    for channel in ("red", "green", "blue"):
        edge = next(c for c in state.graph.connections if c.src_node == splitter and c.src_port == channel)
        assert ConnectionDraft(edge.dst_node, "image_out", combiner, channel) in state.graph.connections
    assert image_edge(combiner, "output") in state.graph.connections
    assert state.undo()
    assert len(state.graph.nodes) == 3


def test_load_and_snapshots_do_not_alias_and_loading_clears_history() -> None:
    state = NodeEditorState()
    state.add_serial()
    snapshot = state.snapshot()
    state.load_graph(snapshot)
    snapshot.nodes[snapshot.active_id].label = "external mutation"
    assert state.graph.nodes[state.graph.active_id].label == ""
    assert not state.can_undo
    invalid = copy.deepcopy(state.graph)
    invalid.connections.append(ConnectionDraft("missing", "image_out", "output", "image_in"))
    with pytest.raises(ValueError):
        state.load_graph(invalid)
    assert len(state.graph.nodes) == 4


def test_move_commits_once_and_lock_prevents_node_changes() -> None:
    state = NodeEditorState()
    first = state.graph.active_id
    assert state.move_nodes({first: (100.0, 80.0)})
    assert state.undo()
    assert (state.graph.nodes[first].x, state.graph.nodes[first].y) == (0.0, 0.0)
    state.set_node_fields([first], locked=True)
    assert not state.move_nodes({first: (500.0, 500.0)})
    assert not state.delete_nodes([first])
    assert not state.set_node_fields([first], label="locked")
    assert state.set_node_fields([first], locked=False)


def test_ui_selection_is_not_an_edit_and_branch_failure_creates_no_history() -> None:
    state = NodeEditorState()
    state.select(["source"], "source")
    assert not state.can_undo
    assert not state.add_branch(NodeKind.PARALLEL_MIXER)
    assert not state.can_undo
    assert not state.delete_nodes(["source", "output"])


def test_shrinking_mixer_inputs_removes_only_unavailable_edges() -> None:
    state = NodeEditorState()
    state.add_branch(NodeKind.PARALLEL_MIXER)
    state.add_branch(NodeKind.PARALLEL_MIXER)
    mixer = next(n.id for n in state.graph.nodes.values() if n.kind == NodeKind.PARALLEL_MIXER)
    assert state.set_node_fields([mixer], input_count=2)
    assert all(c.dst_port != "image_in_2" for c in state.graph.connections if c.dst_node == mixer)
    assert image_edge(mixer, "output") in state.graph.connections


def test_copied_compounds_can_both_be_expanded_with_independent_ids() -> None:
    state = NodeEditorState()
    first = state.graph.active_id
    second = state.add_serial()
    compound = state.make_compound([first, second])
    duplicate = state.duplicate_nodes([compound])[0]
    original_members = state.expand_compound(compound)
    duplicate_members = state.expand_compound(duplicate)
    assert len(original_members) == len(duplicate_members) == 2
    assert not set(original_members) & set(duplicate_members)
    assert image_edge(*duplicate_members) in state.graph.connections


def test_moved_compound_members_expand_at_the_new_location() -> None:
    state = NodeEditorState()
    first = state.graph.active_id
    second = state.add_serial()
    compound = state.make_compound([first, second])
    x, y = state.graph.nodes[compound].x, state.graph.nodes[compound].y
    state.move_nodes({compound: (x + 200.0, y + 300.0)})
    state.expand_compound(compound)
    assert state.graph.nodes[first].x == 200.0
    assert state.graph.nodes[first].y == 300.0
    assert state.graph.nodes[second].x == 330.0


def test_detached_compound_new_connections_survive_expansion() -> None:
    state = NodeEditorState()
    node = state.add_node(NodeKind.CORRECTOR, 100.0, 100.0)
    compound = state.make_compound([node])
    assert state.connect(image_edge("source", compound))
    assert state.connect(image_edge(compound, "output"))
    assert state.expand_compound(compound) == [node]
    assert image_edge("source", node) in state.graph.connections
    assert image_edge(node, "output") in state.graph.connections


def test_mixer_morph_keeps_ports_identity_and_connections() -> None:
    state = NodeEditorState()
    state.add_branch(NodeKind.PARALLEL_MIXER)
    mixer = next(n.id for n in state.graph.nodes.values() if n.kind == NodeKind.PARALLEL_MIXER)
    edges, ports = list(state.graph.connections), state.ports(mixer)
    assert state.morph_mixer(mixer)
    assert state.graph.nodes[mixer].kind == NodeKind.LAYER_MIXER
    assert state.ports(mixer) == ports
    assert state.graph.connections == edges
    assert state.undo()
    assert state.graph.nodes[mixer].kind == NodeKind.PARALLEL_MIXER
    assert not state.morph_mixer(state.graph.active_id)


def test_add_key_mixer_only_connects_key_and_is_one_undo() -> None:
    state = NodeEditorState()
    corrector = state.graph.active_id
    image_edges = list(state.graph.connections)
    mixer = state.add_key_mixer()
    assert state.graph.nodes[mixer].kind == NodeKind.KEY_MIXER
    assert state.graph.nodes[mixer].x > state.graph.nodes[corrector].x
    assert state.graph.nodes[mixer].y > state.graph.nodes[corrector].y
    assert ConnectionDraft(corrector, "key_out", mixer, "key_in_0") in state.graph.connections
    assert [c for c in state.graph.connections if c.src_port == "image_out"] == image_edges
    assert state.undo()
    assert not state.can_undo
    assert mixer not in state.graph.nodes


def test_paste_across_scopes_clones_compounds_and_internal_edges_once() -> None:
    source = NodeEditorState()
    first = source.graph.active_id
    second = source.add_serial()
    compound = source.make_compound([first, second])
    next_node = source.add_serial()
    target = NodeEditorState()
    pasted = target.paste_nodes(source.snapshot(), [compound, next_node], (40.0, 90.0))
    assert len(pasted) == 2
    assert image_edge(*pasted) in target.graph.connections
    assert set(pasted).isdisjoint(source.graph.nodes)
    source_children = source.graph.nodes[compound].children
    pasted_children = target.graph.nodes[pasted[0]].children
    assert source_children is not None and pasted_children is not None
    assert set(source_children.nodes).isdisjoint(pasted_children.nodes)
    assert target.graph.nodes[pasted[0]].x == source.graph.nodes[compound].x + 40.0
    assert target.undo()
    assert not target.can_undo
    assert all(key not in target.graph.nodes for key in pasted)


def test_replace_compound_children_updates_grades_without_losing_boundary() -> None:
    state = NodeEditorState()
    first = state.graph.active_id
    second = state.add_serial()
    compound = state.make_compound([first, second])
    child_editor = NodeEditorState(state.graph.nodes[compound].children)
    child_editor.set_node_fields([second], label="Inside", grade={"exposure": 2.0})
    boundary_edges = list(state.graph.connections)
    assert state.replace_compound_children(compound, child_editor.snapshot())
    children = state.graph.nodes[compound].children
    assert children is not None
    assert children.nodes[second].grade == {"exposure": 2.0}
    assert state.graph.connections == boundary_edges
    assert state.undo()
    children = state.graph.nodes[compound].children
    assert children is not None
    assert children.nodes[second].grade == {}


def test_replacing_compound_with_missing_boundary_node_is_atomic() -> None:
    state = NodeEditorState()
    first = state.graph.active_id
    second = state.add_serial()
    compound = state.make_compound([first, second])
    child_editor = NodeEditorState(state.graph.nodes[compound].children)
    child_editor.delete_nodes([first])
    history_size = len(state._undo)
    before = state.snapshot()
    assert not state.replace_compound_children(compound, child_editor.snapshot())
    assert state.graph == before
    assert len(state._undo) == history_size


def test_drag_move_and_wire_insertion_share_one_undo_step() -> None:
    state = NodeEditorState()
    first = state.graph.active_id
    detached = state.add_node(NodeKind.CORRECTOR, 500.0, 500.0)
    state._undo.clear()
    target = image_edge(first, "output")
    assert state.move_nodes_and_insert({detached: (130.0, 0.0)}, detached, target)
    assert (state.graph.nodes[detached].x, state.graph.nodes[detached].y) == (130.0, 0.0)
    assert image_edge(first, detached) in state.graph.connections
    assert image_edge(detached, "output") in state.graph.connections
    assert state.undo()
    assert not state.can_undo
    assert target in state.graph.connections
    assert (state.graph.nodes[detached].x, state.graph.nodes[detached].y) == (500.0, 500.0)
    assert state.redo()
    assert image_edge(first, detached) in state.graph.connections


def test_refused_drag_insertion_still_moves_without_dropping_branches() -> None:
    state = NodeEditorState()
    first = state.graph.active_id
    second = state.add_serial()
    branch = state.add_node(NodeKind.CORRECTOR, 0.0, 150.0)
    state.connect(image_edge(first, branch))
    old_edges = list(state.graph.connections)
    state._undo.clear()
    assert not state.move_nodes_and_insert({first: (200.0, 200.0)}, first,
                                           image_edge(second, "output"))
    assert state.graph.connections == old_edges
    assert (state.graph.nodes[first].x, state.graph.nodes[first].y) == (200.0, 200.0)
    assert state.undo()
    assert not state.can_undo
    assert state.graph.connections == old_edges
    assert (state.graph.nodes[first].x, state.graph.nodes[first].y) == (0.0, 0.0)


def test_remove_alpha_output_preserves_image_terminals_and_undo_restores_key() -> None:
    state = NodeEditorState()
    first = state.graph.active_id
    alpha = state.add_alpha_output()
    key = ConnectionDraft(first, "key_out", alpha, "key_in")
    state.connect(key)
    state._undo.clear()
    assert not state.remove_alpha_output("source")
    assert not state.remove_alpha_output("output")
    assert not state.remove_alpha_output(first)
    assert state.remove_alpha_output(alpha)
    assert alpha not in state.graph.nodes
    assert key not in state.graph.connections
    assert "source" in state.graph.nodes and "output" in state.graph.nodes
    assert image_edge(first, "output") in state.graph.connections
    assert state.undo()
    assert not state.can_undo
    assert alpha in state.graph.nodes
    assert key in state.graph.connections


def test_make_local_unlocks_shared_only_and_can_restore_shared_lock() -> None:
    state = NodeEditorState()
    shared = state.graph.active_id
    ordinary = state.add_serial()
    state.set_node_fields([shared], shared_id="shared-grade", locked=True)
    state.set_node_fields([ordinary], locked=True)
    state._undo.clear()
    assert not state.set_node_fields([shared], shared_id="")
    assert state.make_local([shared, ordinary])
    assert state.graph.nodes[shared].shared_id == ""
    assert not state.graph.nodes[shared].locked
    assert state.graph.nodes[ordinary].locked
    assert state.undo()
    assert not state.can_undo
    assert state.graph.nodes[shared].shared_id == "shared-grade"
    assert state.graph.nodes[shared].locked
    assert not state.make_local([ordinary])


def test_disconnect_many_ignores_missing_edges_and_undo_restores_all_once() -> None:
    state = NodeEditorState()
    first = state.graph.active_id
    state.add_serial()
    edges = list(state.graph.connections)
    state._undo.clear()
    assert state.disconnect_many(edges[:2] + [image_edge("missing", first)])
    assert state.graph.connections == edges[2:]
    assert not state.disconnect_many(edges[:2])
    assert state.undo()
    assert not state.can_undo
    assert state.graph.connections == edges


def test_external_recursive_compound_is_rejected_before_copying() -> None:
    state = NodeEditorState()
    graph = GraphDraft()
    recursive = NodeDraft("recursive", NodeKind.COMPOUND, children=graph)
    graph.nodes[recursive.id] = recursive
    with pytest.raises(ValueError, match="recursive"):
        state.load_graph(graph)
    assert len(state.graph.nodes) == 3


def test_external_deep_compound_is_rejected_before_copying() -> None:
    state = NodeEditorState()
    graph = GraphDraft()
    for index in range(18):
        node = NodeDraft(f"compound-{index}", NodeKind.COMPOUND, children=graph)
        graph = GraphDraft(nodes={node.id: node})
    with pytest.raises(ValueError, match="16 levels"):
        state.load_graph(graph)


def test_external_dangling_compound_boundary_is_rejected_atomically() -> None:
    state = NodeEditorState()
    node = state.graph.active_id
    compound_id = state.make_compound([node])
    invalid = state.snapshot()
    compound = invalid.nodes[compound_id]
    compound.input_mappings["image_in"] = ("missing", "image_in")
    before = state.snapshot()
    with pytest.raises(ValueError, match="boundary"):
        state.load_graph(invalid)
    assert state.graph == before
    assert state.can_undo


def test_mixers_do_not_consume_corrector_display_numbers() -> None:
    state = NodeEditorState()
    state.add_serial()
    state.add_branch(NodeKind.PARALLEL_MIXER)
    state.add_serial()
    correctors = [n for n in state.graph.nodes.values() if n.kind == NodeKind.CORRECTOR]
    assert [node.number for node in correctors] == [1, 2, 3, 4]
    mixer = next(n for n in state.graph.nodes.values() if n.kind == NodeKind.PARALLEL_MIXER)
    assert mixer.number == 0
    assert state.graph.next_number == 5
    state.paste_nodes(state.snapshot(), [mixer.id])
    assert state.graph.next_number == 5
