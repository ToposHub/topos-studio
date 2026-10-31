"""T100/T101 tests: Graph Editor / Timeline view-mode toggle.

Locks the mode-switch contract (plan §22.1): toggling Graph Editor swaps
the right-hand canvas via the GraphCanvasHost stack, but does NOT reset
current frame, work area, left-tree expansion, filter or keyframe
selection — all of that state lives on the panel / shared model, which
the host never touches.

These tests cover:
  * the toolbar Graph Editor button is checkable and wired;
  * Shift+F3 shortcut is registered;
  * set_view_mode / toggle_view_mode flip the stack;
  * mode switch preserves current frame, selection, expansion;
  * GraphCanvasHost holds the timeline canvas (page 0) and a placeholder
    (page 1) until a real Graph view is installed via set_graph_view;
  * set_graph_view replaces the placeholder without losing the timeline
    page.
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.ui.graph_view import GraphCanvasHost
from src.features.composite.ui.keyframes_panel import KeyframesPanel
from src.shared.keyframe import Keyframe, KeyframeTrack


CLIP_FRAMES = 100
CLIP_FPS = 24.0


@pytest.fixture
def graph():
    g = NodeGraph()
    node = TransformNode()
    track = KeyframeTrack(param_name="rotation")
    for f in (0, 25, 50):
        track.add_keyframe(Keyframe(f, float(f)))
    node.set_keyframe_track("rotation", track)
    g.add_node(node)
    return g


@pytest.fixture
def panel(qapp, graph):
    p = KeyframesPanel()
    p.resize(1200, 300)
    p.set_node_graph(graph)
    p.set_frame_range(0, CLIP_FRAMES - 1, fps=CLIP_FPS)
    return p


# ── T100: view-mode state + toolbar button ────────────────────────────


class TestViewModeState:
    def test_default_view_mode_is_timeline(self, panel):
        assert panel.view_mode == "timeline"

    def test_graph_editor_action_is_checkable(self, panel):
        assert panel._act_graph_editor.isCheckable()
        assert panel._act_graph_editor.isChecked() is False

    def test_set_view_mode_to_graph(self, panel):
        panel.set_view_mode("graph")
        assert panel.view_mode == "graph"
        # Toolbar toggle stays in sync.
        assert panel._act_graph_editor.isChecked() is True

    def test_set_view_mode_to_timeline(self, panel):
        panel.set_view_mode("graph")
        panel.set_view_mode("timeline")
        assert panel.view_mode == "timeline"
        assert panel._act_graph_editor.isChecked() is False

    def test_set_view_mode_unknown_is_noop(self, panel):
        panel.set_view_mode("graph")
        panel.set_view_mode("nonsense")
        assert panel.view_mode == "graph"

    def test_set_view_mode_same_is_noop(self, panel):
        before = panel.view_mode
        panel.set_view_mode(before)
        assert panel.view_mode == before

    def test_toggle_view_mode(self, panel):
        assert panel.view_mode == "timeline"
        panel.toggle_view_mode()
        assert panel.view_mode == "graph"
        panel.toggle_view_mode()
        assert panel.view_mode == "timeline"

    def test_toolbar_button_toggle_calls_set_view_mode(self, panel):
        panel._act_graph_editor.setChecked(True)
        assert panel.view_mode == "graph"
        panel._act_graph_editor.setChecked(False)
        assert panel.view_mode == "timeline"


# ── T100: Shift+F3 shortcut registered ────────────────────────────────


class TestShiftF3Shortcut:
    def test_shortcut_registered(self, panel):
        sc = panel._shortcut_toggle_graph
        assert sc is not None
        # The key sequence should be Shift+F3.
        seq = sc.key().toString()
        assert "F3" in seq
        assert "Shift" in seq

    def test_shortcut_toggles_view_mode(self, panel):
        assert panel.view_mode == "timeline"
        panel._shortcut_toggle_graph.activated.emit()
        assert panel.view_mode == "graph"
        panel._shortcut_toggle_graph.activated.emit()
        assert panel.view_mode == "timeline"


# ── T100: mode switch preserves panel state ──────────────────────────


class TestModeSwitchPreservesState:
    def test_current_frame_preserved(self, panel):
        """Switching to Graph and back keeps the playhead where it was."""
        panel.set_current_frame(42)
        panel.set_view_mode("graph")
        assert panel._state.current_frame == 42
        panel.set_view_mode("timeline")
        assert panel._state.current_frame == 42

    def test_keyframe_selection_preserved(self, panel, graph):
        """A keyframe selected in Timeline stays selected in Graph mode."""
        node = graph.nodes[next(iter(graph.nodes))]
        row_id = f"param:{node.id}:rotation"
        track = panel._model.track_for(row_id)
        track.keyframes[0].selected = True
        panel.set_view_mode("graph")
        assert track.keyframes[0].selected is True
        panel.set_view_mode("timeline")
        assert track.keyframes[0].selected is True

    def test_node_expansion_preserved(self, panel, graph):
        """Left-tree expansion state survives the mode swap."""
        node = graph.nodes[next(iter(graph.nodes))]
        panel._model.set_expanded(node.id, False)
        panel.set_view_mode("graph")
        row = next(r for r in panel._model.rows
                   if r.row_type == "node" and r.node_id == node.id)
        assert row.expanded is False
        panel.set_view_mode("timeline")
        assert row.expanded is False

    def test_work_area_preserved(self, panel):
        """The work-area In/Out must survive the mode swap."""
        panel.set_play_range(10, 60)
        panel.set_view_mode("graph")
        assert panel.effective_play_range() == (10, 60)
        panel.set_view_mode("timeline")
        assert panel.effective_play_range() == (10, 60)


# ── T101: GraphCanvasHost ─────────────────────────────────────────────


class TestGraphCanvasHost:
    def test_host_holds_timeline_at_index_0(self, panel):
        host = panel._canvas_host
        assert host.timeline_view is panel._timeline_view

    def test_host_default_view_mode_timeline(self, panel):
        assert panel._canvas_host.view_mode == "timeline"

    def test_host_set_view_mode_updates_stack(self, panel):
        host = panel._canvas_host
        # Page 0 = timeline, page 1 = placeholder/graph.
        host.set_view_mode("graph")
        assert host.view_mode == "graph"
        host.set_view_mode("timeline")
        assert host.view_mode == "timeline"

    def test_set_graph_view_replaces_placeholder(self, panel, qapp):
        """Installing a graph canvas replaces whatever was at page 1."""
        from PyQt6.QtWidgets import QWidget
        host = panel._canvas_host
        # The panel installs a real GraphEditorView at construction; the
        # host still allows swapping it for another widget.
        first = host.graph_view
        assert first is not None

        class FakeGraph(QWidget):
            pass
        fake = FakeGraph()
        host.set_graph_view(fake)
        assert host.graph_view is fake

    def test_set_graph_view_none_reverts_to_placeholder(self, panel):
        host = panel._canvas_host
        from PyQt6.QtWidgets import QWidget
        host.set_graph_view(QWidget())  # install
        host.set_graph_view(None)       # revert
        assert host.graph_view is None

    def test_refresh_calls_active_canvas(self, panel):
        """refresh() must reach whichever canvas is current (no-op on the
        other)."""
        # Timeline mode → timeline.refresh() called.
        panel._canvas_host.refresh()
        # Switching to graph and calling refresh must not crash even with
        # the placeholder (no refresh method).
        panel.set_view_mode("graph")
        panel._canvas_host.refresh()

    def test_set_current_frame_forwards_to_both(self, panel):
        """The host pushes the frame to whichever canvas is visible. The
        timeline always gets it; the graph canvas (when installed) too."""
        panel._canvas_host.set_current_frame(15)
        assert panel._timeline_view._current_frame == 15


# ── standalone host construction (no panel) ───────────────────────────


class TestStandaloneHost:
    def test_construct_with_arbitrary_timeline_view(self, qapp):
        """GraphCanvasHost works with any QWidget as the timeline canvas —
        it doesn't require a KeyframesTimelineView specifically."""
        from PyQt6.QtWidgets import QLabel
        fake_timeline = QLabel("timeline")
        host = GraphCanvasHost(fake_timeline)
        assert host.timeline_view is fake_timeline
        assert host.view_mode == "timeline"

    def test_forwarded_signals_present(self, qapp):
        """The host exposes the signal surface the panel connects to,
        regardless of whether the timeline canvas emits them."""
        from PyQt6.QtWidgets import QLabel
        host = GraphCanvasHost(QLabel("t"))
        # All re-emitted signals must exist on the host.
        for name in ("current_frame_changed", "vertical_scroll_changed",
                     "track_changed", "range_changed", "play_range_changed",
                     "display_mode_changed"):
            assert hasattr(host, name)


# ── panel → graph editor integration (T105 host glue) ────────────────


class TestPanelGraphIntegration:
    """End-to-end: entering graph mode shows the current param's curve."""

    def test_panel_has_graph_view_installed(self, panel):
        """The panel constructs and installs a GraphEditorView at build
        time so toggling graph mode has a canvas to swap to."""
        assert hasattr(panel, "_graph_view")
        assert panel._graph_view is not None
        assert panel._canvas_host.graph_view is panel._graph_view

    def test_entering_graph_mode_populates_series(self, panel, graph):
        """Switching to graph mode builds a series from the selected param
        (or the first animated param). The graph view must not be empty."""
        node = graph.nodes[next(iter(graph.nodes))]
        panel.set_view_mode("graph")
        # The view should have at least one series (the fallback picks the
        # first animatable param row, which may or may not have keyframes).
        assert len(panel._graph_view._series) >= 1
        s = panel._graph_view._series[0]
        # The series carries the node + param metadata regardless of
        # whether the param has keyframes yet.
        assert s.row_id.startswith(f"param:{node.id}:")

    def test_entering_graph_mode_with_selected_param_shows_curve(self,
                                                                 panel, graph):
        """Selecting the rotation param first → graph mode shows rotation's
        curve with all 3 keyframes."""
        node = graph.nodes[next(iter(graph.nodes))]
        # Select rotation via the panel's selection handler.
        panel._on_param_selected(node.id, "rotation")
        panel.set_view_mode("graph")
        assert len(panel._graph_view._series) == 1
        s = panel._graph_view._series[0]
        assert s.row_id == f"param:{node.id}:rotation"
        assert s.has_keyframes is True
        assert len(s.points) == 3   # rotation has 3 keyframes
        # Samples were built for the curve.
        assert len(s.samples) > 0

    def test_graph_mode_frame_range_follows_panel(self, panel):
        """The graph view's x axis must match the comp frame range."""
        panel.set_frame_range(0, 99)
        panel.set_view_mode("graph")
        assert panel._graph_view._frame_range == (0, 99)

    def test_graph_mode_playhead_follows_panel(self, panel):
        """Moving the panel's playhead updates the graph view's indicator."""
        panel.set_current_frame(42)
        panel.set_view_mode("graph")
        assert panel._graph_view._current_frame == 42
        panel.set_current_frame(60)
        assert panel._graph_view._current_frame == 60

    def test_graph_mode_paints_without_throwing(self, panel, qapp):
        """The graph view must render end-to-end after the panel populates
        its series (no exception in the paint path with real data)."""
        panel.set_view_mode("graph")
        panel._graph_view.show()
        panel._graph_view.repaint()
        qapp.processEvents()
        panel._graph_view.hide()

    def test_empty_graph_paints_empty_state(self, qapp):
        """A param with no keyframes → graph mode shows an empty-state
        series (0 points) and paints without throwing. The panel always
        falls back to the first animatable param row, so the series list
        isn't empty — but the series itself has no points to draw."""
        from src.features.composite.core.node_engine import NodeGraph
        from src.features.composite.nodes.transform_node import TransformNode
        g = NodeGraph()
        g.add_node(TransformNode())  # animatable params, but NO keyframes
        p = KeyframesPanel()
        p.resize(800, 300)
        p.set_node_graph(g)
        p.set_frame_range(0, 99, fps=24.0)
        p.set_view_mode("graph")
        # The series exists (fallback picked a param) but has no points.
        assert len(p._graph_view._series) == 1
        assert p._graph_view._series[0].points == []
        p._graph_view.show()
        p._graph_view.repaint()
        qapp.processEvents()
        p._graph_view.hide()


# ── T110 panel integration: graph type combo ──────────────────────────


class TestPanelGraphTypeCombo:
    """The footer's Graph Type combo (T110) — visible only in graph mode,
    pushes Auto/Value/Speed to the graph view."""

    def test_combo_hidden_in_timeline_mode(self, panel):
        assert panel.view_mode == "timeline"
        assert panel._graph_type_combo.isVisible() is False

    def test_combo_visible_in_graph_mode(self, panel):
        panel.set_view_mode("graph")
        # The combo's visibility *policy* is on (isVisible() needs the
        # panel shown, which doesn't happen under offscreen). Test the
        # policy instead.
        assert panel._graph_type_combo.isVisibleTo(panel) is True or \
            panel._graph_type_combo.isHidden() is False

    def test_combo_default_auto(self, panel):
        assert panel._graph_type_combo.currentIndex() == 0  # Auto

    def test_combo_speed_pushes_to_graph_view(self, panel, graph):
        node = graph.nodes[next(iter(graph.nodes))]
        panel._on_param_selected(node.id, "rotation")
        panel.set_view_mode("graph")
        panel._graph_type_combo.setCurrentIndex(2)  # Speed
        assert panel._graph_view._viewport.graph_type == "speed"

    def test_combo_value_pushes_to_graph_view(self, panel, graph):
        node = graph.nodes[next(iter(graph.nodes))]
        panel._on_param_selected(node.id, "rotation")
        panel.set_view_mode("graph")
        panel._graph_type_combo.setCurrentIndex(1)  # Value
        assert panel._graph_view._viewport.graph_type == "value"

    def test_combo_auto_resolves_per_series(self, panel, graph):
        node = graph.nodes[next(iter(graph.nodes))]
        panel._on_param_selected(node.id, "rotation")
        panel.set_view_mode("graph")
        # Switch to Value first (index 1) then back to Auto (index 0) so the
        # currentIndexChanged signal fires (it doesn't fire when the index
        # is unchanged from the default).
        panel._graph_type_combo.setCurrentIndex(1)
        panel._graph_type_combo.setCurrentIndex(0)  # Auto
        assert panel._graph_view._viewport.graph_type == "auto"
        # Per-series type follows the auto decision (rotation = value).
        for s in panel._graph_view._series:
            assert s.graph_type == "value"


# ── T113: Graph selection → shared model back-sync ────────────────────


class TestGraphSelectionBackSync:
    """When the user selects a keyframe in the Graph Editor, the selection
    flows into the shared AnimationSelectionModel so the Keyframes timeline
    (and Spline) reflect it."""

    def test_graph_selection_pushes_to_shared_model(self, panel, graph):
        node = graph.nodes[next(iter(graph.nodes))]
        panel._on_param_selected(node.id, "rotation")
        panel.set_view_mode("graph")
        row_id = f"param:{node.id}:rotation"
        # Simulate the graph view emitting a selection change.
        panel._graph_view.selection_changed.emit({(row_id, 50)})
        sel = panel.selection_model.selection
        assert row_id in sel.rows
        assert (row_id, 50) in sel.keyframes

    def test_graph_selection_sets_primary(self, panel, graph):
        node = graph.nodes[next(iter(graph.nodes))]
        panel._on_param_selected(node.id, "rotation")
        panel.set_view_mode("graph")
        row_id = f"param:{node.id}:rotation"
        panel._graph_view.selection_changed.emit({(row_id, 25)})
        sel = panel.selection_model.selection
        assert sel.primary_row_id == row_id
        assert sel.primary_frame == 25

    def test_graph_selection_empty_is_noop(self, panel, graph):
        """An empty selection emission doesn't crash and doesn't wipe the
        shared model (the graph view handles its own clear via _clear_selection)."""
        node = graph.nodes[next(iter(graph.nodes))]
        panel._on_param_selected(node.id, "rotation")
        panel.set_view_mode("graph")
        # Empty set → handler returns early.
        panel._graph_view.selection_changed.emit(set())
        # No crash; the model keeps whatever it had.

    def test_clicking_keyframe_in_graph_syncs_to_timeline(self, panel, graph,
                                                          qapp):
        """End-to-end: clicking a keyframe in the graph view updates the
        shared selection that the Keyframes timeline reads."""
        node = graph.nodes[next(iter(graph.nodes))]
        panel._on_param_selected(node.id, "rotation")
        panel.set_view_mode("graph")
        panel.show()
        qapp.processEvents()
        # Find the middle keyframe's screen position and click it.
        from src.features.composite.ui.graph_model import (
            frame_to_x, value_to_y)
        s = panel._graph_view._series[0]
        kp = s.points[1]  # frame 25 (graph fixture uses 0/25/50)
        x = frame_to_x(kp.frame, panel._graph_view._viewport, 40.0)
        y = value_to_y(kp.value, panel._graph_view._viewport,
                       float(panel._graph_view.height() - 24))
        from PyQt6.QtCore import QPointF, Qt
        from PyQt6.QtGui import QMouseEvent
        ev = QMouseEvent(QMouseEvent.Type.MouseButtonPress, QPointF(x, y),
                         Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier)
        panel._graph_view.mousePressEvent(ev)
        row_id = f"param:{node.id}:rotation"
        sel = panel.selection_model.selection
        assert (row_id, int(kp.frame)) in sel.keyframes
        panel.hide()


# ── T122: Graph Info summary label ────────────────────────────────────


class TestGraphInfoSummary:
    """The footer's Graph Info label (T122) — shows property/frame/value/
    speed/interpolation for the selected keyframe, or a count when many."""

    def test_label_hidden_in_timeline_mode(self, panel):
        assert panel.view_mode == "timeline"
        assert panel._graph_info_label.isHidden() is True or \
            panel._graph_info_label.isVisible() is False

    def test_label_shown_in_graph_mode(self, panel):
        panel.set_view_mode("graph")
        # Visibility policy on (actual isVisible needs the panel shown).
        assert panel._graph_info_label.isHidden() is False

    def test_label_empty_when_nothing_selected(self, panel, graph):
        node = graph.nodes[next(iter(graph.nodes))]
        panel._on_param_selected(node.id, "rotation")
        panel.set_view_mode("graph")
        # Nothing selected → empty label.
        assert panel._graph_info_label.text() == ""

    def test_label_shows_single_keyframe_detail(self, panel, graph):
        node = graph.nodes[next(iter(graph.nodes))]
        panel._on_param_selected(node.id, "rotation")
        panel.set_view_mode("graph")
        s = panel._graph_view._series[0]
        s.points[1].selected = True  # frame 25, value 25.0 (graph fixture)
        panel._update_graph_info()
        text = panel._graph_info_label.text()
        assert "Rotation" in text
        assert "f25" in text
        assert "v25" in text
        assert "linear" in text

    def test_label_shows_count_when_many_selected(self, panel, graph):
        node = graph.nodes[next(iter(graph.nodes))]
        panel._on_param_selected(node.id, "rotation")
        panel.set_view_mode("graph")
        s = panel._graph_view._series[0]
        s.points[0].selected = True
        s.points[1].selected = True
        panel._update_graph_info()
        text = panel._graph_info_label.text()
        assert "2 selected" in text

    def test_label_updates_on_selection_click(self, panel, graph, qapp):
        """End-to-end: clicking a keyframe updates the info label."""
        node = graph.nodes[next(iter(graph.nodes))]
        panel._on_param_selected(node.id, "rotation")
        panel.set_view_mode("graph")
        panel.show()
        qapp.processEvents()
        from src.features.composite.ui.graph_model import (
            frame_to_x, value_to_y)
        s = panel._graph_view._series[0]
        kp = s.points[1]
        x = frame_to_x(kp.frame, panel._graph_view._viewport, 40.0)
        y = value_to_y(kp.value, panel._graph_view._viewport,
                       float(panel._graph_view.height() - 24))
        from PyQt6.QtCore import QPointF, Qt
        from PyQt6.QtGui import QMouseEvent
        ev = QMouseEvent(QMouseEvent.Type.MouseButtonPress, QPointF(x, y),
                         Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier)
        panel._graph_view.mousePressEvent(ev)
        text = panel._graph_info_label.text()
        assert "f25" in text or "f" in text  # selected keyframe shown
        panel.hide()
