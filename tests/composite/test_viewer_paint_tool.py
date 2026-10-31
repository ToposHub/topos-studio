"""C2-3: viewer PAINT tool — B-key mode, stroke geometry commits,
clone-source offset, brush resize signals, and the page-side stroke
assembly with one undo per stroke.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.composite_core


def _make_viewer(qapp):
    from src.features.composite.ui.composite_viewport import CompositeViewer
    return CompositeViewer()


def _stub_paint_selection(viewer, node_id="paint-1", tex=(1920, 1080)):
    """Make the viewer behave as if a Paint node is selected at a fixed
    frame mapping: view px == image px (1:1, no zoom)."""
    from PyQt6.QtCore import QPointF

    viewer._selected_node_type = "paint"
    viewer.get_selected_node = lambda: node_id
    viewer._tex_width, viewer._tex_height = tex
    viewer.get_image_pos_at_view_pos = \
        lambda vp, check_bounds=True: QPointF(vp.x(), vp.y())
    viewer.get_view_pos_at_image_pos = lambda ip: QPointF(ip.x(), ip.y())
    return viewer


class TestPaintToolMode:
    def test_paint_mode_registered_with_b_shortcut(self):
        from src.features.composite.ui.viewer_tool_system import (
            ViewerToolMode, TOOL_MODE_SHORTCUTS, TOOL_MODE_LABELS,
        )
        assert ViewerToolMode.PAINT in TOOL_MODE_SHORTCUTS
        assert TOOL_MODE_SHORTCUTS[ViewerToolMode.PAINT] == "B"
        assert ViewerToolMode.PAINT in TOOL_MODE_LABELS

    def test_press_requires_selected_paint_node(self, qapp):
        viewer = _make_viewer(qapp)
        viewer._selected_node_type = "transform"
        viewer.get_selected_node = lambda: "xf-1"
        from PyQt6.QtCore import QPointF
        handled = viewer._handle_paint_press(QPointF(10, 10), 0)
        assert handled is False
        assert viewer._paint_drag_points is None


class TestStrokeGeometry:
    def test_drag_commits_normalized_geometry(self, qapp):
        from PyQt6.QtCore import QPointF
        viewer = _stub_paint_selection(_make_viewer(qapp))
        committed = []
        viewer.paint_stroke_committed.connect(lambda nid, g: committed.append((nid, g)))

        assert viewer._handle_paint_press(QPointF(192, 108), 0) is True
        viewer._handle_paint_move(QPointF(384, 108), 0)
        viewer._handle_paint_move(QPointF(384, 216), 0)
        viewer._handle_paint_release(QPointF(384, 216))

        assert len(committed) == 1
        node_id, geo = committed[0]
        assert node_id == "paint-1"
        assert geo["points"] == [[0.1, 0.1], [0.2, 0.1], [0.2, 0.2]]
        assert geo["size"] == pytest.approx(40.0 / 1920.0)
        assert geo["src_offset"] == [0.0, 0.0]
        assert viewer._paint_drag_points is None

    def test_alt_click_sets_clone_source_offset(self, qapp):
        from PyQt6.QtCore import QPointF
        viewer = _stub_paint_selection(_make_viewer(qapp))
        committed = []
        viewer.paint_stroke_committed.connect(lambda nid, g: committed.append(g))

        from PyQt6.QtCore import Qt
        alt = Qt.KeyboardModifier.AltModifier

        # Click where to copy FROM, then paint somewhere else.
        viewer._handle_paint_press(QPointF(960, 540), alt)
        assert viewer._paint_clone_src == [0.5, 0.5]
        assert viewer._paint_drag_points is None  # no stroke started
        viewer._handle_paint_press(QPointF(192, 108), 0)
        viewer._handle_paint_release(QPointF(192, 108))

        assert len(committed) == 1
        # src_offset = first point − source point (sample = dst − offset
        # lands back on the source).
        assert committed[0]["src_offset"] == pytest.approx(
            [0.1 - 0.5, 0.1 - 0.5])

    def test_mid_drag_selection_change_discards_stroke(self, qapp):
        from PyQt6.QtCore import QPointF
        viewer = _stub_paint_selection(_make_viewer(qapp))
        committed = []
        viewer.paint_stroke_committed.connect(lambda nid, g: committed.append(g))

        viewer._handle_paint_press(QPointF(10, 10), 0)
        # Selection switches to another node mid-drag.
        viewer.get_selected_node = lambda: "other-1"
        viewer._handle_paint_release(QPointF(20, 20))
        assert committed == []
        assert viewer._paint_drag_points is None

    def test_brush_keys_emit_size_signal(self, qapp):
        viewer = _stub_paint_selection(_make_viewer(qapp))
        from PyQt6.QtCore import QPointF, Qt, QEvent
        from PyQt6.QtGui import QKeyEvent
        sizes = []
        viewer.paint_brush_size_changed.connect(lambda nid, s: sizes.append(s))

        def press_key(key, text):
            ev = QKeyEvent(QEvent.Type.KeyPress, key,
                           Qt.KeyboardModifier.NoModifier, text)
            viewer.keyPressEvent(ev)

        from src.features.composite.ui.viewer_tool_system import ViewerToolMode
        viewer.set_tool_mode(ViewerToolMode.PAINT)
        before = viewer._paint_brush_size_px
        press_key(Qt.Key.Key_BracketRight, "]")
        assert viewer._paint_brush_size_px == pytest.approx(before * 1.15)
        assert sizes[-1] == pytest.approx(before * 1.15)
        press_key(Qt.Key.Key_BracketLeft, "[")
        assert viewer._paint_brush_size_px == pytest.approx(before, rel=0.01)
        press_key(Qt.Key.Key_BracketLeft, "[")
        assert viewer._paint_brush_size_px == pytest.approx(
            before / 1.15, rel=0.01)


class TestPageStrokeAssembly:
    """Page slot: assemble stroke from brush defaults, one undo per stroke."""

    def _make_page_stub(self, node):
        from src.features.composite.core.undo_stack import UndoStack

        class _Graph:
            def __init__(self, node):
                self._node = node

            def get_node(self, node_id):
                return self._node if self._node.id == node_id else None

            def set_node_parameter(self, node_id, name, value):
                node = self.get_node(node_id)
                if node is not None:
                    node.set_parameter(name, value)

        class _Page:
            def __init__(self, node):
                self._node_graph = _Graph(node)
                self._undo_stack = UndoStack()
                self.evals = 0

            def _evaluate_graph_now(self):
                self.evals += 1

        return _Page(node)

    def test_stroke_appended_with_brush_defaults(self, qapp):
        from src.features.composite.nodes.paint_node import PaintNode
        from src.features.composite.ui.composite_page import CompositePage

        node = PaintNode(node_id="p1")
        node.set_parameter("brush_type", "erase")
        node.set_parameter("brush_feather", 0.2)
        node.set_parameter("brush_opacity", 0.7)
        page = self._make_page_stub(node)
        geometry = {"points": [[0.1, 0.1], [0.2, 0.1]], "size": 0.02,
                    "src_offset": [0.05, 0.0]}

        CompositePage._on_paint_stroke_committed(page, "p1", geometry)

        strokes = node.get_strokes()
        assert len(strokes) == 1
        assert strokes[0]["type"] == "erase"
        assert strokes[0]["feather"] == 0.2
        assert strokes[0]["opacity"] == 0.7
        assert strokes[0]["points"] == geometry["points"]
        assert strokes[0]["src_offset"] == [0.05, 0.0]
        assert page.evals == 1

    def test_one_undo_per_stroke_restores_old_json(self, qapp):
        from src.features.composite.nodes.paint_node import PaintNode
        from src.features.composite.ui.composite_page import CompositePage

        node = PaintNode(node_id="p1")
        first = PaintNode.make_stroke("solid", [[0.5, 0.5]])
        node.set_strokes([first])
        old_json = node.parameters["strokes"]
        page = self._make_page_stub(node)

        geometry = {"points": [[0.1, 0.1]], "size": 0.02,
                    "src_offset": [0.0, 0.0]}
        CompositePage._on_paint_stroke_committed(page, "p1", geometry)
        assert len(node.get_strokes()) == 2
        assert page.evals == 1

        page._undo_stack.undo()
        assert node.parameters["strokes"] == old_json
        assert page.evals == 2
        page._undo_stack.redo()
        assert len(node.get_strokes()) == 2
        assert page.evals == 3

    def test_brush_size_signal_writes_parameter(self, qapp):
        from src.features.composite.nodes.paint_node import PaintNode
        from src.features.composite.ui.composite_page import CompositePage

        node = PaintNode(node_id="p1")
        page = self._make_page_stub(node)
        CompositePage._on_paint_brush_size_changed(page, "p1", 64.0)
        assert float(node.parameters["brush_size"]) == 64.0
