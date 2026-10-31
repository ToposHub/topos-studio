"""Batch D (wd10-g42) regression + coverage tests.

Covers:
  * AUD-9920 — group operations (group/ungroup/add/remove) are registered
               on the page UndoStack. The editor applies the group command
               and emits ``group_commands_applied``; CompositePage wraps the
               applied command objects into one UndoAction so Ctrl+Z can
               revert grouping, ungrouping and membership changes.
               ``build_group_command_from_selection`` returns the command
               unapplied instead of applying + discarding it.
  * GAP-003 — composite_page.py coverage additions (page-level group undo
               integration, graph_changed re-sync after group undo/redo)
  * GAP-013 — node_editor_widget.py coverage additions (snap state, zoom /
               fit, shortcuts toggle, select-all/upstream/downstream
               delegates, locked-selection guard, align/distribute/layout /
               overlap-cleanup, position sync, hit testing, group view with
               metadata round trip through clear_all/build_from_graph)
  * GAP-029 — spline_view.py coverage additions (fit_selected_view,
               resizeEvent plot-rect realign, set_fps validation, ruler
               playhead drag, zero-delta wheel, clipboard edge cases,
               pure helpers)
  * GAP-030 — node_editor_actions.py coverage additions (move ops,
               disconnect_by_dst / reconnect, bypass ops, set_parameter,
               selection ops, zoom, hit testing, preset CRUD,
               _safe_preset_position)

GAP hard constraints honoured: no src behaviour change beyond the AUD-9920
root-cause fix, no edits to existing tests, external IO (preset dir) via
tmp_path.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytestmark = pytest.mark.composite_core

from PyQt6.QtCore import QEvent, QPointF, QRectF, Qt
from PyQt6.QtGui import QMouseEvent, QWheelEvent
from PyQt6.QtCore import QPoint

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.blur_node import BlurNode
from src.features.composite.nodes.constant_node import ConstantNode
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.ui.group_commands import (
    build_group_command_from_selection,
    create_group_from_selection,
)
from src.shared.keyframe import Keyframe, KeyframeTrack


# ── fixtures ────────────────────────────────────────────────────────────


def _make_graph() -> NodeGraph:
    graph = NodeGraph()
    graph.add_node(ConstantNode(node_id="src"))
    graph.add_node(BlurNode(node_id="blur"))
    graph.add_node(OutputNode(node_id="out"))
    graph.connect("src", "result", "blur", "image")
    graph.connect("blur", "result", "out", "image")
    return graph


def _make_editor(qapp):
    """A NodeEditorWidget built from a 3-node chain graph."""
    from src.features.composite.ui.node_editor_widget import NodeEditorWidget

    editor = NodeEditorWidget()
    graph = _make_graph()
    editor.build_from_graph(graph)
    return editor, graph


def _wire_graph_signals(editor, graph) -> None:
    """Mimic CompositePage's node_connected/disconnected contract so the
    signal-driven action methods actually mutate the model + visuals."""
    editor.node_connected.connect(
        lambda sn, sp, dn, dp: (
            graph.connect(sn, sp, dn, dp),
            editor.add_connection_visual(sn, sp, dn, dp)))
    editor.node_disconnected.connect(
        lambda sn, sp, dn, dp: (
            graph.disconnect(sn, sp, dn, dp),
            editor.remove_connection_visual_by_dst(dn, dp)))


def _group_ids(graph: NodeGraph) -> set:
    return set(getattr(graph, "_node_groups", {}))


# ── AUD-9920: command layer ─────────────────────────────────────────────


class TestAud9920CommandLayer:
    def test_builder_returns_unapplied_command(self):
        graph = _make_graph()
        cmd = build_group_command_from_selection(
            graph, ["src", "blur"], title="MyGroup")
        # Nothing applied yet — the caller owns when redo() happens.
        assert _group_ids(graph) == set()
        assert cmd.group_id not in graph._node_groups

        assert cmd.redo() == cmd.group_id
        group = graph.get_group(cmd.group_id)
        assert group is not None
        assert group["title"] == "MyGroup"
        assert sorted(group["node_ids"]) == ["blur", "src"]

        cmd.undo()
        assert graph.get_group(cmd.group_id) is None

    def test_legacy_helper_still_applies_and_returns_id(self):
        """Locked old behaviour: create_group_from_selection applies."""
        graph = _make_graph()
        gid = create_group_from_selection(graph, ["src", "blur"])
        assert graph.get_group(gid) is not None
        assert sorted(graph.get_group(gid)["node_ids"]) == ["blur", "src"]

    def test_builder_position_computed_from_node_bounds(self):
        graph = _make_graph()
        graph.get_node("src").position = (100.0, 200.0)
        graph.get_node("blur").position = (150.0, 120.0)
        cmd = build_group_command_from_selection(graph, ["src", "blur"])
        cmd.redo()
        pos = graph.get_group(cmd.group_id)["position"]
        # bbox min (100, 120) with the documented padding (-20, -40).
        assert pos == (80.0, 80.0)

    def test_builder_with_unknown_nodes_uses_zero_position(self):
        graph = _make_graph()
        cmd = build_group_command_from_selection(graph, ["ghost"])
        cmd.redo()
        assert graph.get_group(cmd.group_id)["position"] == (0.0, 0.0)


# ── AUD-9920 + GAP-030: actions layer ───────────────────────────────────


class TestAud9920ActionsLayer:
    def test_group_emits_applied_commands(self, qapp):
        editor, graph = _make_editor(qapp)
        applied = []
        editor.group_commands_applied.connect(
            lambda desc, cmds: applied.append((desc, list(cmds))))

        editor._node_items["src"].set_selected(True)
        editor._node_items["blur"].set_selected(True)

        gid = editor._actions.group_selected_nodes()
        try:
            assert gid is not None
            assert graph.get_group(gid) is not None
            assert len(applied) == 1
            desc, cmds = applied[0]
            assert "group" in desc.lower()
            assert len(cmds) == 1
            # The emitted command must carry the undo for the operation.
            cmds[0].undo()
            assert graph.get_group(gid) is None
            cmds[0].redo()
            assert graph.get_group(gid) is not None
        finally:
            editor.deleteLater()

    def test_group_with_fewer_than_two_selected_is_noop(self, qapp):
        editor, graph = _make_editor(qapp)
        applied = []
        editor.group_commands_applied.connect(
            lambda desc, cmds: applied.append((desc, cmds)))

        editor._node_items["src"].set_selected(True)
        assert editor._actions.group_selected_nodes() is None
        assert applied == []
        assert _group_ids(graph) == set()
        editor.deleteLater()

    def test_ungroup_emits_single_batch(self, qapp):
        editor, graph = _make_editor(qapp)
        gid = create_group_from_selection(graph, ["src", "blur"])
        applied = []
        editor.group_commands_applied.connect(
            lambda desc, cmds: applied.append((desc, list(cmds))))

        editor._node_items["src"].set_selected(True)
        editor._node_items["blur"].set_selected(True)
        dissolved = editor._actions.ungroup_selected()
        try:
            assert dissolved == [gid]
            assert graph.get_group(gid) is None
            # ONE signal for the whole batch → one undo step.
            assert len(applied) == 1
            desc, cmds = applied[0]
            assert "ungroup" in desc.lower()
            assert len(cmds) == 1
            cmds[0].undo()
            assert graph.get_group(gid) is not None
        finally:
            editor.deleteLater()

    def test_ungroup_with_no_selection_is_noop(self, qapp):
        editor, graph = _make_editor(qapp)
        create_group_from_selection(graph, ["src", "blur"])
        applied = []
        editor.group_commands_applied.connect(
            lambda desc, cmds: applied.append((desc, cmds)))

        assert editor._actions.ungroup_selected() == []
        assert applied == []
        assert _group_ids(graph)  # group untouched
        editor.deleteLater()

    def test_add_and_remove_membership_emit_commands(self, qapp):
        editor, graph = _make_editor(qapp)
        gid = create_group_from_selection(graph, ["src", "blur"])
        applied = []
        editor.group_commands_applied.connect(
            lambda desc, cmds: applied.append((desc, list(cmds))))

        assert editor._actions.add_to_group(gid, ["out"]) is True
        assert sorted(graph.get_group(gid)["node_ids"]) == [
            "blur", "out", "src"]
        assert editor._actions.remove_from_group(gid, ["out"]) is True
        assert sorted(graph.get_group(gid)["node_ids"]) == ["blur", "src"]

        try:
            assert len(applied) == 2
            # Undoing in reverse: re-add "out", then remove the group ops.
            applied[-1][1][0].undo()
            assert sorted(graph.get_group(gid)["node_ids"]) == [
                "blur", "out", "src"]
            applied[-1][1][0].redo()
            assert sorted(graph.get_group(gid)["node_ids"]) == [
                "blur", "src"]
        finally:
            editor.deleteLater()

    def test_add_to_unknown_group_does_not_raise(self, qapp):
        """Command guards missing groups; the action still reports True
        (locked behaviour) and emits the command for the undo record."""
        editor, graph = _make_editor(qapp)
        applied = []
        editor.group_commands_applied.connect(
            lambda desc, cmds: applied.append((desc, cmds)))

        assert editor._actions.add_to_group("nope", ["out"]) is True
        assert len(applied) == 1
        # undo() on an unknown group must be safe inside the slot.
        applied[0][1][0].undo()
        editor.deleteLater()


# ── AUD-9920 + GAP-003: page-level undo integration ─────────────────────


class TestAud9920PageUndo:
    def _make_page(self, qapp):
        from src.features.composite.ui.composite_page import CompositePage

        page = CompositePage(parent=None)
        graph = _make_graph()
        page._node_graph = graph
        page._node_editor.build_from_graph(graph)
        page._evaluate_graph = MagicMock()
        return page, graph

    def test_group_undo_redo_through_page_stack(self, qapp):
        page, graph = self._make_page(qapp)
        try:
            editor = page._node_editor
            editor._node_items["src"].set_selected(True)
            editor._node_items["blur"].set_selected(True)

            gid = editor._actions.group_selected_nodes()
            assert gid is not None
            assert graph.get_group(gid) is not None
            assert page._undo_stack.can_undo()

            page.undo()
            assert graph.get_group(gid) is None, \
                "Ctrl+Z must revert grouping (AUD-9920)"

            page.redo()
            assert graph.get_group(gid) is not None
            assert sorted(graph.get_group(gid)["node_ids"]) == [
                "blur", "src"]
        finally:
            page.cleanup()
            page.deleteLater()

    def test_ungroup_undo_restores_group(self, qapp):
        page, graph = self._make_page(qapp)
        try:
            editor = page._node_editor
            gid = create_group_from_selection(graph, ["src", "blur"])
            editor._node_items["src"].set_selected(True)

            assert editor._actions.ungroup_selected() == [gid]
            assert "ungroup" in page._undo_stack.undo_description().lower()

            page.undo()
            restored = graph.get_group(gid)
            assert restored is not None
            assert sorted(restored["node_ids"]) == ["blur", "src"]

            page.redo()
            assert graph.get_group(gid) is None
        finally:
            page.cleanup()
            page.deleteLater()

    def test_membership_change_undo(self, qapp):
        page, graph = self._make_page(qapp)
        try:
            editor = page._node_editor
            gid = create_group_from_selection(graph, ["src", "blur"])
            editor._actions.add_to_group(gid, ["out"])
            assert "out" in graph.get_group(gid)["node_ids"]

            page.undo()
            assert "out" not in graph.get_group(gid)["node_ids"]
            page.redo()
            assert "out" in graph.get_group(gid)["node_ids"]
        finally:
            page.cleanup()
            page.deleteLater()

    def test_group_undo_resyncs_editor_graph_changed(self, qapp):
        page, graph = self._make_page(qapp)
        try:
            editor = page._node_editor
            emitted = []
            editor.graph_changed.connect(lambda: emitted.append(1))

            editor._node_items["src"].set_selected(True)
            editor._node_items["blur"].set_selected(True)
            editor._actions.group_selected_nodes()
            before = len(emitted)
            page.undo()
            assert len(emitted) > before, \
                "group undo must re-sync the editor (graph_changed)"
        finally:
            page.cleanup()
            page.deleteLater()

    def test_handler_ignores_empty_command_list(self, qapp):
        page, _graph = self._make_page(qapp)
        try:
            page._on_group_commands_applied("empty", [])
            page._on_group_commands_applied("none", [None])
            assert page._undo_stack.can_undo() is False
        finally:
            page.cleanup()
            page.deleteLater()


# ── GAP-030: NodeEditorActions coverage ─────────────────────────────────


class TestGap030MoveOps:
    def test_move_node_updates_item_and_model(self, qapp):
        editor, graph = _make_editor(qapp)
        moved = []
        editor.nodes_moved.connect(lambda ids, old: moved.append((ids, old)))
        try:
            assert editor._actions.move_node("ghost", 10, 20) is False
            assert editor._actions.move_node("blur", 111.0, 222.0) is True
            item = editor._node_items["blur"]
            assert (item.pos().x(), item.pos().y()) == (111.0, 222.0)
            assert graph.get_node("blur").position == (111.0, 222.0)
            assert moved and moved[0][0] == ["blur"]
        finally:
            editor.deleteLater()

    def test_move_nodes_length_mismatch_rejected(self, qapp):
        editor, _g = _make_editor(qapp)
        try:
            assert editor._actions.move_nodes(["src"], []) is False
        finally:
            editor.deleteLater()

    def test_move_nodes_moves_each_and_reports(self, qapp):
        editor, graph = _make_editor(qapp)
        try:
            ok = editor._actions.move_nodes(
                ["src", "ghost", "out"], [(1.0, 2.0), (3.0, 4.0), (5.0, 6.0)])
            assert ok is True
            assert graph.get_node("src").position == (1.0, 2.0)
            assert graph.get_node("out").position == (5.0, 6.0)
            # The ghost id is skipped, not an error.
            assert "ghost" not in editor._node_items
        finally:
            editor.deleteLater()

    def test_move_nodes_by_delta(self, qapp):
        editor, graph = _make_editor(qapp)
        try:
            old_x = editor._node_items["src"].pos().x()
            assert editor._actions.move_nodes_by_delta(["src"], 25.0, -5.0)
            assert editor._node_items["src"].pos().x() == old_x + 25.0
            assert graph.get_node("src").position == (
                old_x + 25.0, editor._node_items["src"].pos().y())
            assert editor._actions.move_nodes_by_delta(["ghost"], 1, 1) is False
        finally:
            editor.deleteLater()


class TestGap030ConnectionOps:
    def test_disconnect_by_dst_returns_source(self, qapp):
        editor, graph = _make_editor(qapp)
        _wire_graph_signals(editor, graph)
        try:
            assert editor._actions.disconnect_by_dst("ghost", "image") is None
            src = editor._actions.disconnect_by_dst("out", "image")
            assert src == ("blur", "result")
            assert not any(
                c.dst_node_id == "out" for c in graph.connections)
        finally:
            editor.deleteLater()

    def test_reconnect_swaps_source(self, qapp):
        editor, graph = _make_editor(qapp)
        _wire_graph_signals(editor, graph)
        try:
            ok = editor._actions.reconnect(
                "blur", "result", "src", "result", "out", "image")
            assert ok is True
            quads = {(c.src_node_id, c.dst_node_id) for c in graph.connections}
            assert ("blur", "out") not in quads
            assert ("src", "out") in quads
        finally:
            editor.deleteLater()


class TestGap030BypassAndParams:
    def test_toggle_bypass_roundtrip(self, qapp):
        editor, graph = _make_editor(qapp)
        toggled = []
        editor.node_bypass_toggled.connect(toggled.append)
        try:
            assert editor._actions.toggle_bypass("ghost") is False
            assert editor._actions.toggle_bypass("blur") is True
            assert graph.get_node("blur").disabled is True
            assert editor._actions.toggle_bypass("blur") is False
            assert graph.get_node("blur").disabled is False
            assert toggled == ["blur", "blur"]
        finally:
            editor.deleteLater()

    def test_set_bypass_no_change_returns_false(self, qapp):
        editor, _g = _make_editor(qapp)
        try:
            assert editor._actions.set_bypass("blur", False) is False
            assert editor._actions.set_bypass("blur", True) is True
            assert editor._actions.set_bypass("blur", True) is False
        finally:
            editor.deleteLater()

    def test_toggle_bypass_selected_only_selected(self, qapp):
        editor, graph = _make_editor(qapp)
        try:
            editor._node_items["src"].set_selected(True)
            assert editor._actions.toggle_bypass_selected() == ["src"]
            assert graph.get_node("src").disabled is True
            assert graph.get_node("blur").disabled is False
        finally:
            editor.deleteLater()

    def test_set_parameter(self, qapp):
        editor, graph = _make_editor(qapp)
        try:
            assert editor._actions.set_parameter("ghost", "radius", 5) is False
            assert editor._actions.set_parameter("blur", "radius", 7.5) is True
            assert float(graph.get_node("blur").parameters["radius"]) == 7.5
        finally:
            editor.deleteLater()


class TestGap030SelectionOps:
    def test_select_node_and_additive(self, qapp):
        editor, _g = _make_editor(qapp)
        selected = []
        editor.node_selected.connect(selected.append)
        try:
            assert editor._actions.select_node("ghost") is False
            assert editor._actions.select_node("src") is True
            assert selected == ["src"]
            assert editor._actions.get_selected_node_ids() == ["src"]
            editor._actions.select_node("blur", add_to_selection=True)
            assert sorted(editor._actions.get_selected_node_ids()) == [
                "blur", "src"]
        finally:
            editor.deleteLater()

    def test_select_nodes_empty_and_multi(self, qapp):
        editor, _g = _make_editor(qapp)
        selected = []
        editor.node_selected.connect(selected.append)
        try:
            assert editor._actions.select_nodes([]) is False
            assert editor._actions.select_nodes(["src", "blur"]) is True
            assert selected == ["src"]
            assert sorted(editor._actions.get_selected_node_ids()) == [
                "blur", "src"]
        finally:
            editor.deleteLater()

    def test_select_all_and_clear(self, qapp):
        editor, _g = _make_editor(qapp)
        cleared = []
        editor.selection_cleared.connect(lambda: cleared.append(1))
        try:
            assert editor._actions.select_all_nodes() == 3
            editor._actions.clear_selection()
            assert editor._actions.get_selected_node_ids() == []
            assert cleared == [1]
        finally:
            editor.deleteLater()

    def test_select_upstream_downstream(self, qapp):
        editor, _g = _make_editor(qapp)
        try:
            # No selection → no-op.
            assert editor._actions.select_upstream() == []
            assert editor._actions.select_downstream() == []
            # U-10（2026-10-14 审计）：旧的预标记递归只遍历一跳；修复后
            # 返回完整传递闭包（src→blur→out 链）。契约更新为全闭包。
            editor._node_items["out"].set_selected(True)
            assert set(editor._actions.select_upstream()) == {"blur", "src"}
            editor._actions.clear_selection()
            editor._node_items["src"].set_selected(True)
            assert set(editor._actions.select_downstream()) == {"blur", "out"}
        finally:
            editor.deleteLater()


class TestGap030ViewAndHit:
    def test_set_zoom_clamped_and_reported(self, qapp):
        editor, _g = _make_editor(qapp)
        zooms = []
        editor.zoom_changed.connect(zooms.append)
        try:
            editor._actions.set_zoom(99.0)
            assert editor._actions.get_zoom() == 5.0
            editor._actions.set_zoom(0.001)
            assert editor._actions.get_zoom() == 0.1
            editor._actions.set_zoom(1.0)
            assert editor._actions.get_zoom() == 1.0
            assert zooms == [5.0, 0.1, 1.0]
        finally:
            editor.deleteLater()

    def test_fit_delegates_run_clean(self, qapp):
        editor, _g = _make_editor(qapp)
        try:
            editor._actions.fit_all_nodes()
            editor._actions.fit_selected_nodes()
            assert editor._actions.get_zoom() > 0
        finally:
            editor.deleteLater()

    def test_get_node_at_position(self, qapp):
        editor, _g = _make_editor(qapp)
        try:
            # Default positions can stack nodes; isolate the target first.
            editor._node_items["blur"].setPos(-3000.0, -3000.0)
            assert editor._actions.get_node_at_position(
                -2990.0, -2990.0) == "blur"
            assert editor._actions.get_node_at_position(-4999, -4999) is None
        finally:
            editor.deleteLater()

    def test_get_port_at_position_none_when_empty(self, qapp):
        editor, _g = _make_editor(qapp)
        try:
            assert editor._actions.get_port_at_position(-4999, -4999) is None
        finally:
            editor.deleteLater()


class TestGap030Presets:
    def _armed(self, qapp, tmp_path):
        from src.features.composite.utils.preset_manager import PresetManager

        editor, graph = _make_editor(qapp)
        editor._preset_manager = PresetManager(presets_dir=str(tmp_path))
        editor._node_items["blur"].set_selected(True)
        return editor, graph

    def test_save_list_load_delete_roundtrip(self, qapp, tmp_path):
        editor, graph = self._armed(qapp, tmp_path)
        try:
            assert editor._actions.save_selected_as_preset("p1") is True
            assert editor._actions.list_presets() == ["p1"]

            new_ids = editor._actions.load_preset("p1", position=(500.0, 500.0))
            assert len(new_ids) == 1
            assert graph.get_node(new_ids[0]) is not None
            assert graph.get_node(new_ids[0]).node_type == "blur"
            pos = graph.get_node(new_ids[0]).position
            assert pos[0] == 500.0 and pos[1] == 500.0

            assert editor._actions.load_preset("missing") == []
            assert editor._actions.delete_preset("p1") is True
            assert editor._actions.list_presets() == []
        finally:
            editor.deleteLater()

    def test_save_without_selection_fails(self, qapp, tmp_path):
        editor, _g = self._armed(qapp, tmp_path)
        try:
            editor._actions.clear_selection()
            assert editor._actions.save_selected_as_preset("p2") is False
        finally:
            editor.deleteLater()

    def test_export_import_roundtrip(self, qapp, tmp_path):
        editor, _g = self._armed(qapp, tmp_path)
        # Export OUTSIDE the presets dir — files inside it are managed by
        # PresetManager and delete_preset() would remove the export too.
        out = tmp_path / "exports" / "p1.json"
        out.parent.mkdir()
        try:
            assert editor._actions.save_selected_as_preset("p1") is True
            assert editor._actions.export_preset("p1", str(out)) is True
            assert out.exists()
            assert editor._actions.delete_preset("p1") is True
            assert editor._actions.import_preset(str(out)) == "p1"
            assert editor._actions.list_presets() == ["p1"]
        finally:
            editor.deleteLater()

    def test_get_preset_manager_reuses_instance(self, qapp, tmp_path):
        editor, _g = self._armed(qapp, tmp_path)
        try:
            assert editor._actions.get_preset_manager() is editor._preset_manager
        finally:
            editor.deleteLater()

    def test_safe_preset_position_shapes(self):
        from src.features.composite.ui.node_editor_actions import (
            NodeEditorActions)

        ok = NodeEditorActions._safe_preset_position([10, 20])
        assert ok == [10.0, 20.0]
        for bad in ([], [1], (1,), "xy", None, 100, [True, 3], [1, "a"]):
            assert NodeEditorActions._safe_preset_position(bad) == [0.0, 0.0]


# ── GAP-013: NodeEditorWidget coverage ──────────────────────────────────


class TestGap013SnapAndShortcuts:
    def test_snap_state_roundtrip_and_toggle(self, qapp):
        editor, _g = _make_editor(qapp)
        try:
            assert editor.get_snap_state() is False
            editor.set_snap_state(True)
            assert editor.get_snap_state() is True
            editor._toggle_grid_snap()
            assert editor.get_snap_state() is False
        finally:
            editor.deleteLater()

    def test_shortcuts_enabled_toggle(self, qapp):
        editor, _g = _make_editor(qapp)
        try:
            assert editor.shortcuts_enabled() is True
            editor.set_shortcuts_enabled(False)
            assert editor.shortcuts_enabled() is False
            editor.set_shortcuts_enabled(True)
            assert editor.shortcuts_enabled() is True
        finally:
            editor.deleteLater()


class TestGap013SelectionDelegates:
    def test_select_all_nodes(self, qapp):
        editor, _g = _make_editor(qapp)
        try:
            editor._select_all_nodes()
            assert sorted(
                nid for nid, it in editor._node_items.items()
                if it.isSelected()) == ["blur", "out", "src"]
        finally:
            editor.deleteLater()

    def test_upstream_downstream_delegates(self, qapp):
        editor, _g = _make_editor(qapp)
        try:
            # U-10：闭包遍历（src→blur→out 链）；widget delegate 用完整
            # 上/下游闭包替换选择。
            editor._node_items["out"].set_selected(True)
            editor._select_upstream_nodes()
            assert sorted(
                nid for nid, it in editor._node_items.items()
                if it.isSelected()) == ["blur", "src"]
            editor._actions.clear_selection()
            editor._node_items["src"].set_selected(True)
            editor._select_downstream_nodes()
            assert sorted(
                nid for nid, it in editor._node_items.items()
                if it.isSelected()) == ["blur", "out"]
        finally:
            editor.deleteLater()

    def test_group_ungroup_delegates(self, qapp):
        editor, graph = _make_editor(qapp)
        applied = []
        editor.group_commands_applied.connect(
            lambda desc, cmds: applied.append((desc, cmds)))
        try:
            editor._node_items["src"].set_selected(True)
            editor._node_items["blur"].set_selected(True)
            editor._group_selected_nodes()
            assert len(applied) == 1
            assert _group_ids(graph)

            editor._node_items["src"].set_selected(True)
            editor._ungroup_selected_nodes()
            assert len(applied) == 2
            assert _group_ids(graph) == set()
        finally:
            editor.deleteLater()

    def test_group_blocked_when_selection_locked(self, qapp):
        editor, graph = _make_editor(qapp)
        applied = []
        editor.group_commands_applied.connect(
            lambda desc, cmds: applied.append((desc, cmds)))
        try:
            editor._node_items["src"].set_locked(True)
            editor._node_items["src"].set_selected(True)
            editor._node_items["blur"].set_selected(True)
            assert editor._selection_contains_locked_node() is True
            editor._group_selected_nodes()
            editor._ungroup_selected_nodes()
            assert applied == []
        finally:
            editor.deleteLater()


class TestGap013ArrangeOps:
    def _select_all(self, editor):
        for item in editor._node_items.values():
            item.set_selected(True)

    @staticmethod
    def _set_pos(editor, graph, nid, x, y):
        """Set BOTH the item and the model position (arrange ops compute
        from the model, apply to the items)."""
        editor._node_items[nid].setPos(x, y)
        graph.get_node(nid).position = (x, y)

    def test_align_left_aligns_x(self, qapp):
        editor, graph = _make_editor(qapp)
        try:
            self._set_pos(editor, graph, "src", 0.0, 0.0)
            self._set_pos(editor, graph, "blur", 300.0, 50.0)
            self._set_pos(editor, graph, "out", 600.0, 100.0)
            self._select_all(editor)
            editor._align_selected_nodes("left")
            xs = {editor._node_items[n].pos().x()
                  for n in ("src", "blur", "out")}
            assert len(xs) == 1
            assert graph.get_node("out").position[0] == xs.pop()
        finally:
            editor.deleteLater()

    def test_align_unknown_direction_is_noop(self, qapp):
        editor, _g = _make_editor(qapp)
        try:
            self._select_all(editor)
            editor._align_selected_nodes("diagonal")  # must not raise
        finally:
            editor.deleteLater()

    def test_distribute_horizontal_spreads(self, qapp):
        editor, graph = _make_editor(qapp)
        try:
            # Uneven spacing (0, 5, 40): the middle node must move to the
            # even slot (20); endpoints stay pinned.
            self._set_pos(editor, graph, "src", 0.0, 0.0)
            self._set_pos(editor, graph, "blur", 5.0, 0.0)
            self._set_pos(editor, graph, "out", 40.0, 0.0)
            self._select_all(editor)
            editor._distribute_selected_nodes("horizontal")
            xs = {n: editor._node_items[n].pos().x()
                  for n in ("src", "blur", "out")}
            assert xs["src"] == 0.0
            assert xs["out"] == 40.0
            assert xs["blur"] == pytest.approx(20.0)
        finally:
            editor.deleteLater()

    def test_auto_layout_orders_chain(self, qapp):
        editor, graph = _make_editor(qapp)
        moved = []
        editor.nodes_moved.connect(lambda ids, old: moved.append(ids))
        try:
            # Deliberately reversed positions: out left, src right.
            self._set_pos(editor, graph, "out", 0.0, 0.0)
            self._set_pos(editor, graph, "blur", 200.0, 0.0)
            self._set_pos(editor, graph, "src", 400.0, 0.0)
            self._select_all(editor)
            editor._auto_layout_selected_nodes()
            assert moved, "auto layout must emit nodes_moved for undo"
            xs = {n: editor._node_items[n].pos().x()
                  for n in ("src", "blur", "out")}
            assert xs["src"] < xs["blur"] < xs["out"], \
                "topological order: input left, output right"
        finally:
            editor.deleteLater()

    def test_cleanup_overlaps_separates_stacked_nodes(self, qapp):
        editor, graph = _make_editor(qapp)
        try:
            for nid in ("src", "blur", "out"):
                self._set_pos(editor, graph, nid, 50.0, 50.0)
            self._select_all(editor)
            editor._cleanup_overlaps_selected_nodes()
            positions = {(editor._node_items[n].pos().x(),
                          editor._node_items[n].pos().y())
                         for n in ("src", "blur", "out")}
            assert len(positions) == 3, "stacked nodes must be separated"
        finally:
            editor.deleteLater()

    def test_arrange_ops_need_two_nodes(self, qapp):
        editor, _g = _make_editor(qapp)
        moved = []
        editor.nodes_moved.connect(lambda ids, old: moved.append(ids))
        try:
            editor._node_items["src"].set_selected(True)
            editor._align_selected_nodes("left")
            editor._distribute_selected_nodes("horizontal")
            editor._auto_layout_selected_nodes()
            editor._cleanup_overlaps_selected_nodes()
            assert moved == []
        finally:
            editor.deleteLater()


class TestGap013Misc:
    def test_update_graph_positions_syncs_model(self, qapp):
        editor, graph = _make_editor(qapp)
        try:
            editor._node_items["blur"].setPos(321.0, 123.0)
            editor._update_graph_positions()
            assert graph.get_node("blur").position == (321.0, 123.0)
        finally:
            editor.deleteLater()

    def test_find_node_at(self, qapp):
        editor, _g = _make_editor(qapp)
        try:
            editor._node_items["src"].setPos(-3200.0, -3200.0)
            found = editor._find_node_at(QPointF(-3190.0, -3190.0))
            assert found is not None and found.node_id == "src"
            assert editor._find_node_at(QPointF(-4999, -4999)) is None
        finally:
            editor.deleteLater()

    def test_zoom_level_accessor(self, qapp):
        editor, _g = _make_editor(qapp)
        try:
            assert editor.get_zoom_level() == 1.0
            editor.set_snap_state(True)  # unrelated state change must not break
            assert editor.get_zoom_level() == 1.0
        finally:
            editor.deleteLater()

    def test_clear_and_rebuild_preserves_groups(self, qapp):
        editor, graph = _make_editor(qapp)
        gid = create_group_from_selection(graph, ["src", "blur"])
        try:
            editor.clear_all()
            assert editor._node_items == {}
            editor.build_from_graph(graph)
            assert set(editor._node_items) == {"src", "blur", "out"}
            # Group metadata survives the visual rebuild (enter works).
            assert editor.enter_group_view(gid) is True
            visible = {nid for nid, it in editor._node_items.items()
                       if it.isVisible()}
            assert visible == {"src", "blur"}
            assert editor.exit_group_view() is True
        finally:
            editor.deleteLater()

    def test_group_view_stack_property(self, qapp):
        editor, graph = _make_editor(qapp)
        gid = create_group_from_selection(graph, ["src", "blur"])
        try:
            assert editor.group_view_stack == []
            assert editor.is_in_group_view() is False
            editor.enter_group_view(gid)
            assert editor.group_view_stack == [gid]
            assert editor.is_in_group_view() is True
        finally:
            editor.deleteLater()


# ── GAP-029: SplineView coverage ────────────────────────────────────────


def _spline_model_two_curves():
    from src.features.composite.ui.spline_model import SplineModel

    g = NodeGraph()
    t1 = ConstantNode(node_id="n1")
    track1 = KeyframeTrack(param_name="value", min_val=0.0, max_val=200.0)
    for f, v in [(0, 0.0), (10, 30.0), (20, 60.0), (30, 90.0)]:
        track1.add_keyframe(Keyframe(f, v))
    t1.set_keyframe_track("value", track1)
    g.add_node(t1)
    t2 = ConstantNode(node_id="n2")
    track2 = KeyframeTrack(param_name="value", min_val=0.0, max_val=200.0)
    for f, v in [(0, 0.0), (50, 45.0), (100, 90.0)]:
        track2.add_keyframe(Keyframe(f, v))
    t2.set_keyframe_track("value", track2)
    g.add_node(t2)
    m = SplineModel()
    m.rebuild(g)
    return m


@pytest.fixture
def sview(qapp):
    from src.features.composite.ui.spline_view import SplineView

    m = _spline_model_two_curves()
    view = SplineView(m)
    view.resize(960, 400)
    view.fit_view()
    return view, m


def _mev(typ, x, y, button=Qt.MouseButton.LeftButton,
         mods=Qt.KeyboardModifier.NoModifier):
    return QMouseEvent(typ, QPointF(x, y), button, button, mods)


class TestGap029ViewportAndState:
    def test_fit_selected_view_fits_only_selected(self, sview):
        view, m = sview
        curves = sorted(m.curves, key=lambda c: c.keys[-1].frame)
        short, long = curves[0], curves[1]
        short.selected = True
        long.selected = False

        view.fit_view()
        view._ensure_viewport()  # fit_view only invalidates; force compute
        assert view._viewport.frame_max > 90.0  # full model bounds
        view.fit_selected_view()
        assert view._viewport is not None
        assert view._viewport.frame_max < 60.0  # selected curve bounds only

    def test_fit_selected_view_no_selection_resets(self, sview):
        view, m = sview
        for c in m.curves:
            c.selected = False
        view.fit_selected_view()
        assert view._viewport is None  # falls back to the default fit

    def test_resize_event_realigns_plot_rect(self, sview):
        from PyQt6.QtCore import QSize
        from PyQt6.QtGui import QResizeEvent

        view, _m = sview
        view._ensure_viewport()
        view.resize(1200, 600)
        view.resizeEvent(QResizeEvent(QSize(1200, 600), QSize(960, 400)))
        rect = view._viewport.plot_rect
        assert rect.width() == pytest.approx(
            1200 - view._margin_left - view._margin_right)
        assert rect.height() == pytest.approx(
            600 - view._margin_top - view._margin_bottom)

    def test_refresh_clears_viewport(self, sview):
        view, _m = sview
        view._ensure_viewport()
        assert view._viewport is not None
        view.refresh()
        assert view._viewport is None

    def test_set_fps_rejects_non_positive(self, sview):
        view, _m = sview
        view.set_fps(0.0)
        view.set_fps(-5.0)
        assert view._fps == 24.0
        view.set_fps(48.0)
        assert view._fps == 48.0

    def test_seconds_label_uses_fps(self, sview):
        view, _m = sview
        view.set_fps(25.0)
        view.set_time_mode("seconds")
        assert view._fmt_frame_label(50.0) == "2.00s"

    def test_wheel_zero_delta_does_not_zoom(self, sview):
        view, _m = sview
        view._ensure_viewport()
        before = (view._viewport.frame_min, view._viewport.frame_max)
        ev = QWheelEvent(QPointF(400, 200), QPointF(400, 200),
                         QPoint(0, 0), QPoint(0, 0),
                         Qt.MouseButton.NoButton,
                         Qt.KeyboardModifier.NoModifier,
                         Qt.ScrollPhase.NoScrollPhase, False)
        view.wheelEvent(ev)
        assert (view._viewport.frame_min,
                view._viewport.frame_max) == before

    def test_ruler_press_drags_playhead(self, sview):
        view, _m = sview
        frames = []
        view.current_frame_changed.connect(frames.append)
        target = view.screen_to_data(QPointF(500.0, 10.0)).x()
        view.mousePressEvent(_mev(QEvent.Type.MouseButtonPress, 500.0, 5.0))
        try:
            assert view._drag_mode == "playhead"
            assert frames and frames[-1] == int(round(target))
            assert view._current_frame == int(round(target))
        finally:
            view.mouseReleaseEvent(_mev(QEvent.Type.MouseButtonRelease,
                                        500.0, 5.0))
        assert view._drag_mode == "none"

    def test_right_button_press_starts_pan(self, sview):
        view, _m = sview
        view.mousePressEvent(_mev(QEvent.Type.MouseButtonPress, 400.0, 200.0,
                                  button=Qt.MouseButton.RightButton))
        assert view._drag_mode == "pan"
        view.mouseReleaseEvent(_mev(QEvent.Type.MouseButtonRelease, 400.0,
                                    200.0,
                                    button=Qt.MouseButton.RightButton))
        assert view._drag_mode == "none"
        assert view._pan_anchor_data is None

    def test_render_to_painter_resets_viewport(self, sview):
        from PyQt6.QtGui import QImage, QPainter

        view, _m = sview
        img = QImage(400, 260, QImage.Format.Format_ARGB32)
        painter = QPainter(img)
        view.render_to_painter(painter, QRectF(0, 0, 400, 260))
        painter.end()
        assert view._viewport is None
        assert not img.isNull()

    def test_double_click_non_left_ignored(self, sview):
        view, m = sview
        before = sum(len(c.keys) for c in m.curves)
        view.mouseDoubleClickEvent(_mev(
            QEvent.Type.MouseButtonDblClick, 300.0, 200.0,
            button=Qt.MouseButton.RightButton))
        assert sum(len(c.keys) for c in m.curves) == before

    def test_unknown_key_falls_through(self, sview):
        from PyQt6.QtGui import QKeyEvent

        view, _m = sview
        # A key the view does not handle must reach the base class safely.
        view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress,
                                     Qt.Key.Key_F35,
                                     Qt.KeyboardModifier.NoModifier))


class TestGap029ClipboardEdges:
    def _select_first_curve_keys(self, m):
        for c in m.curves:
            c.selected = False
            for k in c.keys:
                k.selected = False
        first = sorted(m.curves, key=lambda c: c.keys[0].frame)[0]
        first.selected = True
        for k in first.keys:
            k.selected = True
        return first

    def test_copy_with_no_selection_returns_zero(self, sview):
        view, m = sview
        for c in m.curves:
            c.selected = False
            for k in c.keys:
                k.selected = False
        assert view.copy_selected_keys() == 0
        assert view.has_key_clipboard() is False

    def test_paste_skips_unknown_curve(self, sview):
        view, m = sview
        curve = self._select_first_curve_keys(m)
        n = view.copy_selected_keys()
        assert n == len(curve.keys)
        assert view.has_key_clipboard() is True
        # Corrupt one clipboard entry to point at a removed curve.
        view._key_clipboard.append({
            "curve_id": "ghost", "node_id": "nope", "param_name": "value",
            "rel_frame": 3, "data": {"frame": 3, "value": 1.0}})
        assert view.paste_keys_at_frame(200) is True
        # Only the valid curve got the paste; no crash from the ghost entry.
        pasted = [c for c in m.curves if any(k.frame >= 200 for k in c.keys)]
        assert len(pasted) == 1

    def test_paste_with_malformed_payload_skips_entry(self, sview):
        view, m = sview
        self._select_first_curve_keys(m)
        view.copy_selected_keys()
        view._key_clipboard = [{
            "curve_id": m.curves[0].curve_id,
            "node_id": m.curves[0].node_id,
            "param_name": m.curves[0].param_name,
            "rel_frame": 1,
            "data": {"value": object()},  # float() → TypeError
        }]
        assert view.paste_keys_at_frame(50) is False

    def test_paste_without_clipboard_is_noop(self, sview):
        view, _m = sview
        assert view.paste_keys_at_frame(10) is False

    def test_cut_copies_and_deletes(self, sview):
        view, m = sview
        curve = self._select_first_curve_keys(m)
        count = len(curve.keys)
        edits = []
        view.keyframe_changed.connect(
            lambda nid, param, kfs: edits.append((nid, param, kfs)))
        assert view.cut_selected_keys() == count
        assert len(curve.keys) == 0
        assert view.has_key_clipboard() is True
        assert edits and len(edits[0][2]) == 0

    def test_multi_selection_emits_none_readout(self, sview):
        view, m = sview
        for c in m.curves:
            for k in c.keys:
                k.selected = True
        payloads = []
        view.selected_key_changed.connect(payloads.append)
        view._emit_selected_key_changed()
        assert payloads == [None]

    def test_key_selection_pairs_use_row_ids(self, sview):
        view, m = sview
        self._select_first_curve_keys(m)
        payloads = []
        view.key_selection_changed.connect(payloads.append)
        view._emit_key_selection_changed()
        assert payloads and len(payloads[0]) == len(
            [k for c in m.curves for k in c.keys if k.selected])
        row_id = payloads[0][0][0]
        assert row_id.startswith("param:")


class TestGap029PureHelpers:
    def test_nice_step_table(self):
        from src.features.composite.ui.spline_view import SplineView

        nice = SplineView._nice_step
        assert nice(0) == 1.0
        assert nice(-5) == 1.0
        # span/6 = 10 → 1×10^1
        assert nice(60) == 10.0
        # span/6 = 20 → 2×10^1
        assert nice(120) == 20.0
        # span/6 = 50 → 5×10^1
        assert nice(300) == 50.0
        # span/6 = 100 → 1×10^2
        assert nice(600) == 100.0

    def test_fmt_num(self):
        from src.features.composite.ui.spline_view import _fmt_num

        assert _fmt_num(3.0) == "3"
        assert _fmt_num(2.567) == "2.57"

    def test_round_step(self):
        from src.features.composite.ui.spline_view import _round_step

        assert _round_step(7.0, 5.0) == 5.0
        assert _round_step(-1.0, 5.0) == -5.0
        assert _round_step(10.0, 5.0) == 10.0

    def test_frame_to_timecode(self):
        from src.features.composite.ui.spline_view import _frame_to_timecode

        assert _frame_to_timecode(0, 24.0) == "00:00:00:00"
        assert _frame_to_timecode(23, 24.0) == "00:00:00:23"
        assert _frame_to_timecode(24, 24.0) == "00:00:01:00"
        # Non-positive fps falls back to 1.0 without raising.
        assert _frame_to_timecode(90, 0.0) == "00:01:30:00"


# ── AUD-9921: group_id 碰撞不得覆盖现有组 ────────────────────────────────


class TestAud9921GroupIdCollision:
    def test_collision_falls_back_to_full_uuid_without_overwrite(
            self, monkeypatch):
        import uuid as uuid_module

        import src.features.composite.ui.group_commands as gcu

        graph = _make_graph()
        monkeypatch.setattr(
            gcu.uuid, "uuid4", lambda: uuid_module.UUID(int=0))

        existing_id = create_group_from_selection(graph, ["src"], title="keep")
        assert existing_id == "00000000"

        # 第二次创建：8 位片段与现有组必撞 → 重生成 8 次仍撞 → 完整 uuid 兜底
        gid = create_group_from_selection(graph, ["blur"], title="new")

        assert gid == "00000000-0000-0000-0000-000000000000"
        assert sorted(graph._node_groups["00000000"]["node_ids"]) == ["src"]
        assert graph._node_groups["00000000"]["title"] == "keep"
        assert sorted(graph.get_group(gid)["node_ids"]) == ["blur"]

    def test_collision_regenerates_short_id_when_free_roll_available(
            self, monkeypatch):
        import uuid as uuid_module

        import src.features.composite.ui.group_commands as gcu

        graph = _make_graph()
        monkeypatch.setattr(
            gcu.uuid, "uuid4",
            lambda: uuid_module.UUID("aaaaaaaa-0000-0000-0000-000000000000"))
        existing_id = create_group_from_selection(graph, ["src"], title="keep")
        assert existing_id == "aaaaaaaa"

        # 下一轮换新值：重生成一次即得空闲 8 位片段，无需完整 uuid
        rolls = iter([
            uuid_module.UUID("aaaaaaaa-0000-0000-0000-000000000000"),
            uuid_module.UUID("bbbbbbbb-0000-0000-0000-000000000000"),
        ])
        monkeypatch.setattr(gcu.uuid, "uuid4", lambda: next(rolls))

        gid = create_group_from_selection(graph, ["blur"], title="new")

        assert gid == "bbbbbbbb"
        assert sorted(graph._node_groups["aaaaaaaa"]["node_ids"]) == ["src"]
        assert sorted(graph.get_group(gid)["node_ids"]) == ["blur"]
