"""T36 view tests: off-screen layout check at 800 / 1200 / 1920.

The data half of T36 (column_x_offsets no-overlap) lives in
``tests/composite/test_timeline_layout.py``. This file locks the *view*
half: at the three reference dock widths the full KeyframesPanel — toolbar
+ tree + timeline + footer — must render without throwing, and the tree's
column geometry must stay non-overlapping at every width the splitter can
produce.

Why a render test rather than a pixel diff:

* pixel diffs are brittle across platforms / font rendering and the plan
  (§16) explicitly says the text spec is the executable requirement, not
  the screenshot;
* the real regression we're guarding against is "paint throws at width X"
  or "two columns overlap at width Y" — both are caught by a render +
  geometry check without coupling to exact pixel colours.
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.nodes.constant_node import ConstantNode
from src.features.composite.ui.keyframes_panel import KeyframesPanel
from src.shared.keyframe import Keyframe, KeyframeTrack


pytestmark = pytest.mark.composite_core


CLIP_FRAMES = 100
CLIP_FPS = 24.0


@pytest.fixture
def graph():
    g = NodeGraph()
    t = TransformNode()
    track = KeyframeTrack(param_name="rotation")
    track.add_keyframe(Keyframe(10, 0.0))
    track.add_keyframe(Keyframe(50, 90.0))
    track.add_keyframe(Keyframe(90, 180.0))
    t.set_keyframe_track("rotation", track)
    g.add_node(t)
    g.add_node(ConstantNode())
    return g


@pytest.fixture
def panel(qapp, graph):
    p = KeyframesPanel()
    p.set_node_graph(graph)
    p.set_frame_range(0, CLIP_FRAMES - 1, fps=CLIP_FPS)
    return p


# ── full-panel render across widths ────────────────────────────────────


class TestFullPanelRender:
    """Render the whole KeyframesPanel (toolbar + tree + timeline + footer)
    at the three reference widths. Any paint exception surfaces as a test
    failure under offscreen (Qt re-throws paint exceptions)."""

    @pytest.mark.parametrize("w", [800, 1200, 1920])
    def test_renders_without_throwing(self, panel, qapp, w):
        panel.resize(w, 300)
        panel.show()
        panel.repaint()
        qapp.processEvents()
        panel.hide()

    @pytest.mark.parametrize("h", [180, 300, 600])
    def test_renders_across_heights(self, panel, qapp, h):
        """Minimum (180) / default (300) / tall (600) dock heights."""
        panel.resize(1200, h)
        panel.show()
        panel.repaint()
        qapp.processEvents()
        panel.hide()

    def test_render_produces_non_blank_output(self, panel, qapp):
        """A successful paint must produce visible pixels — guards against
        a regression where paint completes but draws nothing (e.g. a bad
        token resolution painting fully-transparent content)."""
        panel.resize(1200, 300)
        pm = panel.grab()
        img = pm.toImage()
        # Count non-transparent pixels in the whole panel.
        visible = sum(
            1 for y in range(0, img.height(), 8)
            for x in range(0, img.width(), 8)
            if img.pixelColor(x, y).alpha() > 0)
        assert visible > 100, (
            f"panel render too sparse ({visible} sampled pixels visible)")


# ── tree column geometry stays consistent across widths ───────────────


class TestTreeColumnConsistency:
    """The tree's column geometry must stay non-overlapping at every width
    the splitter can hand it — including the narrow minimum (240) and the
    full-panel widths (800/1200/1920) where the splitter gives the tree
    ~43 % of the dock."""

    @pytest.mark.parametrize("tree_w", [240, 310, 420, 537, 800])
    def test_no_overlap_at_tree_width(self, panel, tree_w):
        """Resize just the tree side and confirm columns tile cleanly."""
        panel._tree_view.resize(tree_w, 250)
        geom = panel._tree_view._column_geometry()
        spans = sorted(geom.values(), key=lambda s: s[0])
        for i in range(1, len(spans)):
            assert spans[i][0] >= spans[i - 1][1], (
                f"tree_w={tree_w}: overlap between {spans[i-1]} and {spans[i]}")
        assert spans[-1][1] <= tree_w

    @pytest.mark.parametrize("w", [800, 1200, 1920])
    def test_tree_columns_non_overlapping_at_full_panel(self, panel, qapp, w):
        """At the full-panel width the splitter gives the tree a share;
        whatever that share is, the columns must still tile cleanly."""
        panel.resize(w, 300)
        panel.show()
        qapp.processEvents()
        panel.hide()
        tree_w = panel._tree_view.width()
        geom = panel._tree_view._column_geometry()
        spans = sorted(geom.values(), key=lambda s: s[0])
        for i in range(1, len(spans)):
            assert spans[i][0] >= spans[i - 1][1]
        assert spans[-1][1] == tree_w


# ── splitter resize keeps columns valid ────────────────────────────────


class TestSplitterResize:
    def test_dragging_splitter_keeps_columns_valid(self, panel, qapp):
        """Dragging the left/right splitter must not produce overlapping
        columns on either side at any intermediate position."""
        panel.resize(1200, 300)
        panel.show()
        qapp.processEvents()
        # Walk the splitter from far-left to far-right in steps.
        for left_w in (240, 330, 537, 700, 900):
            panel._splitter.setSizes([left_w, 1200 - left_w - 1])
            qapp.processEvents()
            geom = panel._tree_view._column_geometry()
            spans = sorted(geom.values(), key=lambda s: s[0])
            for i in range(1, len(spans)):
                assert spans[i][0] >= spans[i - 1][1], (
                    f"splitter left={left_w}: overlap {spans[i-1]} ∩ {spans[i]}")
        panel.hide()


# ── toolbar + footer render at all widths ──────────────────────────────


class TestToolbarFooterRender:
    """The toolbar (T27) and footer (T34) paint their own sub-widgets; make
    sure they render too, not just the central splitter."""

    @pytest.mark.parametrize("w", [800, 1200, 1920])
    def test_toolbar_renders(self, panel, qapp, w):
        panel.resize(w, 300)
        toolbar = panel.findChild(type(panel).__bases__[0],  # noqa: F841
                                  "KeyframesToolbar")
        # The toolbar is a QToolBar; find it by object name.
        from PyQt6.QtWidgets import QToolBar
        tb = panel.findChild(QToolBar, "KeyframesToolbar")
        assert tb is not None
        tb.repaint()
        qapp.processEvents()

    @pytest.mark.parametrize("w", [800, 1200, 1920])
    def test_footer_renders_with_time_display(self, panel, qapp, w):
        """The footer must contain the T25 time display and render it."""
        panel.resize(w, 300)
        assert hasattr(panel, "_time_display")
        panel._time_display.repaint()
        qapp.processEvents()
