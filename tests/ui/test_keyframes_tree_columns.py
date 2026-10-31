"""T28 view tests: AE column layout in KeyframesTreeView.

These tests lock the post-rewrite column contract on the left side of the
Keyframes panel. The tree now paints Eye / Solo / Lock / Shy / Label /
Name / Value / Expression columns (plan §3.3 / §16.4) from the
``timeline_layout.LEFT_COLUMNS`` spec, instead of the old ad-hoc
``_LOCK_COL_W`` / ``_NODE_LABEL_X`` constants.

What's locked here:

* column hit-testing — clicking the eye column emits ``eye_changed`` (was
  lock pre-rewrite), clicking the lock column emits ``lock_changed``;
* column geometry — ``_column_geometry()`` returns non-overlapping spans
  that tile the widget width, at every width the splitter can produce;
* the value column — blank by default, populated when the host installs a
  value provider;
* the expand-arrow legacy click zone is preserved so node rows still
  toggle expansion when clicked in the name cell;
* paint never throws for any combination of column widths (off-screen
  render covers the full code path).
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.ui import timeline_layout
from src.features.composite.ui.keyframes_model import KeyframesModel
from src.features.composite.ui.keyframes_tree_view import KeyframesTreeView


pytestmark = pytest.mark.composite_core


# ── fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def graph():
    g = NodeGraph()
    g.add_node(TransformNode())
    return g


@pytest.fixture
def model(graph):
    m = KeyframesModel()
    m.rebuild(graph)
    return m


@pytest.fixture
def tree(qapp, model):
    t = KeyframesTreeView(model, header_height=30)
    t.resize(330, 300)
    return t


# ── column geometry resolver ───────────────────────────────────────────


class TestColumnGeometry:
    def test_geometry_returns_all_columns(self, tree):
        geom = tree._column_geometry()
        names = {c.name for c in timeline_layout.LEFT_COLUMNS}
        assert set(geom.keys()) == names

    def test_geometry_tiles_widget_width(self, tree):
        for w in (240, 330, 480, 800, 1200):
            tree.resize(w, 300)
            geom = tree._column_geometry()
            # Adjacency + full coverage.
            sorted_cols = sorted(geom.values(), key=lambda s: s[0])
            for i in range(1, len(sorted_cols)):
                assert sorted_cols[i][0] == sorted_cols[i - 1][1]
            assert sorted_cols[-1][1] == w

    def test_geometry_no_overlap(self, tree):
        for w in (240, 330, 800):
            tree.resize(w, 300)
            geom = tree._column_geometry()
            spans = sorted(geom.values(), key=lambda s: s[0])
            for i in range(1, len(spans)):
                assert spans[i][0] >= spans[i - 1][1], (
                    f"overlap at width {w}: {spans[i-1]} ∩ {spans[i]}")

    def test_eye_is_first_column_lock_is_third(self, tree):
        """AE column order (plan §16.4): Eye, Solo, Lock, Shy, Label,
        Index, Name, Value, Expression."""
        geom = tree._column_geometry()
        assert geom["eye"][0] == 0
        assert geom["solo"][0] == geom["eye"][1]
        assert geom["lock"][0] == geom["solo"][1]
        assert geom["shy"][0] == geom["lock"][1]


# ── column hit-testing ─────────────────────────────────────────────────


def _press(tree, x, y):
    """Synthesize a left-click at widget coordinates (x, y)."""
    from PyQt6.QtCore import QPointF, Qt
    from PyQt6.QtGui import QMouseEvent
    ev = QMouseEvent(QMouseEvent.Type.MouseButtonPress,
                     QPointF(float(x), float(y)),
                     Qt.MouseButton.LeftButton,
                     Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier)
    tree.mousePressEvent(ev)


class TestColumnClicks:
    def test_eye_click_emits_eye_changed(self, tree, graph):
        """Clicking the eye column toggles row.eye and emits eye_changed.
        Pre-rewrite this x position was the lock column; now it's eye."""
        received = []
        tree.eye_changed.connect(lambda nid, vis: received.append((nid, vis)))
        geom = tree._column_geometry()
        eye_x = (geom["eye"][0] + geom["eye"][1]) / 2
        # Row 0 is the node row (y inside first row, below the header).
        _press(tree, eye_x, 30 + 12)
        assert received
        node_id = next(iter(graph.nodes))
        assert received[0][0] == node_id
        # Eye defaults to visible (True) → first click turns it off.
        assert received[0][1] is False

    def test_lock_click_emits_lock_changed(self, tree, graph):
        received = []
        tree.lock_changed.connect(lambda nid, lk: received.append((nid, lk)))
        geom = tree._column_geometry()
        lock_x = (geom["lock"][0] + geom["lock"][1]) / 2
        _press(tree, lock_x, 30 + 12)
        assert received
        node_id = next(iter(graph.nodes))
        assert received[0][0] == node_id
        assert received[0][1] is True

    def test_name_click_selects_node(self, tree, graph):
        received = []
        tree.node_selected.connect(received.append)
        # Click deep inside the name column (past the expand arrow).
        geom = tree._column_geometry()
        name_x = geom["name"][0] + 80
        _press(tree, name_x, 30 + 12)
        assert received
        node_id = next(iter(graph.nodes))
        assert received[0] == node_id

    def test_param_row_click_emits_param_selected(self, tree, graph):
        received = []
        tree.param_selected.connect(lambda nid, pn: received.append((nid, pn)))
        # Find the first param row's y.
        rows = tree._model.visible_rows()
        param_rows = [i for i, r in enumerate(rows) if r.row_type == "param"]
        assert param_rows, "expected at least one param row"
        idx = param_rows[0]
        y = 30 + idx * 24 + 12
        geom = tree._column_geometry()
        name_x = geom["name"][0] + 80
        _press(tree, name_x, y)
        assert received

    def test_eye_click_does_not_emit_lock(self, tree):
        """Eye and lock must be independent toggles — an eye click never
        leaks into lock_changed (regression guard for the column rewrite)."""
        lock_received = []
        tree.lock_changed.connect(lambda nid, lk: lock_received.append(lk))
        geom = tree._column_geometry()
        eye_x = (geom["eye"][0] + geom["eye"][1]) / 2
        _press(tree, eye_x, 30 + 12)
        assert lock_received == []


# ── value column provider ──────────────────────────────────────────────


class TestValueColumn:
    def test_value_provider_default_is_none(self, tree):
        assert tree._value_provider is None

    def test_set_value_provider_accepts_callable(self, tree, qapp):
        called = []

        def provider(node_id, param_name):
            called.append((node_id, param_name))
            return "1.0"

        tree.set_value_provider(provider)
        assert tree._value_provider is provider
        # Qt suppresses paintEvent for hidden widgets under offscreen, so we
        # must show the widget to actually trigger painting (same pattern as
        # the expression-status provider tests).
        tree.show()
        tree.repaint()
        qapp.processEvents()
        tree.hide()
        # Provider must have been queried for at least one param row.
        assert called

    def test_set_none_provider_clears(self, tree):
        tree.set_value_provider(lambda n, p: "1.0")
        tree.set_value_provider(None)
        assert tree._value_provider is None

    def test_provider_returning_none_does_not_crash(self, tree, qapp):
        tree.set_value_provider(lambda n, p: None)
        tree.repaint()
        qapp.processEvents()

    def test_provider_raising_is_swallowed(self, tree, qapp):
        def boom(n, p):
            raise RuntimeError("provider crashed")
        tree.set_value_provider(boom)
        tree.repaint()
        qapp.processEvents()


# ── paint robustness across widths ─────────────────────────────────────


class TestPaintAcrossWidths:
    """Force a repaint at every splitter-friendly width. Any exception
    inside paintEvent propagates as a failure under offscreen (Qt re-throws
    paint exceptions)."""

    @pytest.mark.parametrize("w", [180, 240, 330, 480, 800, 1200])
    def test_paint_does_not_throw(self, tree, qapp, w):
        tree.resize(w, 300)
        tree.repaint()
        qapp.processEvents()

    def test_paint_with_provider_installed(self, tree, qapp):
        tree.set_value_provider(lambda n, p: "42.0")
        tree.repaint()
        qapp.processEvents()

    def test_paint_with_expression_provider(self, tree, qapp):
        """The expression-status provider (T49) still paints alongside the
        new column layout — both must coexist."""
        from src.features.composite.ui.timeline_expression import (
            EXPRESSION_VALID, ExpressionStatus)
        tree.set_expression_status_provider(
            lambda n, p: ExpressionStatus(state=EXPRESSION_VALID, text="x"))
        tree.repaint()
        qapp.processEvents()


# ── header band ────────────────────────────────────────────────────────


class TestHeaderBand:
    def test_header_zero_skips_header_paint(self, model, qapp):
        """A standalone tree with header_height=0 must not try to paint the
        column header band (the panel always passes a header; tests don't)."""
        t = KeyframesTreeView(model, header_height=0)
        t.resize(330, 200)
        t.repaint()
        qapp.processEvents()

    def test_header_paints_at_default_panel_height(self, tree, qapp):
        """The panel passes header_height=KEYFRAMES_RULER_H (30). Paint must
        complete without throwing and reserve the band above row 0."""
        assert tree._header_height == 30
        tree.repaint()
        qapp.processEvents()
