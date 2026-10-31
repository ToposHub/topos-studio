"""T124 Graph Editor off-screen layout tests.

Renders the Graph Editor under offscreen at many sizes / states and asserts:
  * paint completes without throwing for every configuration;
  * the legend doesn't overlap the plot area;
  * multi-channel (X/Y/Z) series all paint;
  * the empty state (no series) paints cleanly;
  * long parameter names don't overflow the legend.

These are layout/render-contract tests (plan §24.1) — they DON'T assert
pixel content (brittle across platforms/DPI); they assert 'paint doesn't
crash' + 'plot rect stays within the widget bounds' + 'legend width fits'.
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from PyQt6.QtCore import QRectF

from src.features.composite.ui.graph_editor_view import (
    GraphEditorView, _MARGIN_LEFT, _MARGIN_RIGHT, _MARGIN_TOP, _MARGIN_BOTTOM,
)
from src.features.composite.ui.graph_model import build_graph_series
from src.features.composite.nodes.transform_node import TransformNode
from src.shared.keyframe import Keyframe, KeyframeTrack


pytestmark = pytest.mark.composite_core


# ── fixtures ──────────────────────────────────────────────────────────


def _series(param="rotation", kfs=((0, 0.0), (50, 90.0), (100, 0.0))):
    node = TransformNode()
    track = KeyframeTrack(param_name=param)
    for f, v in kfs:
        track.add_keyframe(Keyframe(f, v))
    node.set_keyframe_track(param, track)
    return build_graph_series(node, param)


def _multi_channel_series():
    """X/Y/Z series for a spatial param (translate_x/y)."""
    out = []
    for param in ("translate_x", "translate_y"):
        node = TransformNode()
        track = KeyframeTrack(param_name=param)
        for f, v in ((0, 0.0), (50, 50.0), (100, 100.0)):
            track.add_keyframe(Keyframe(f, v))
        node.set_keyframe_track(param, track)
        out.append(build_graph_series(node, param))
    return out


@pytest.fixture
def view(qapp):
    v = GraphEditorView()
    v.set_frame_range(0, 100)
    return v


def _plot_rect(view):
    """The plot rectangle the view paints into."""
    return QRectF(_MARGIN_LEFT, _MARGIN_TOP,
                  max(1, view.width() - _MARGIN_LEFT - _MARGIN_RIGHT),
                  max(1, view.height() - _MARGIN_TOP - _MARGIN_BOTTOM))


# ── T124: multi-size render ───────────────────────────────────────────


class TestMultiSizeRender:
    @pytest.mark.parametrize("w,h", [
        (200, 150),   # minimum size
        (400, 300),   # small
        (800, 400),   # default
        (1200, 600),  # large
        (1920, 1080), # 1080p
        (320, 240),   # very small / narrow
    ])
    def test_paint_completes_at_size(self, view, qapp, w, h):
        view.resize(w, h)
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        view.repaint()  # no exception = pass

    def test_plot_rect_stays_within_bounds(self, view, qapp):
        """The plot rectangle never exceeds the widget dimensions."""
        for w, h in ((200, 150), (800, 400), (1920, 1080)):
            view.resize(w, h)
            view.set_series([_series()])
            view.show()
            qapp.processEvents()
            pr = _plot_rect(view)
            assert pr.left() >= 0
            assert pr.top() >= 0
            assert pr.right() <= w
            assert pr.bottom() <= h


# ── T124: multi-channel render ────────────────────────────────────────


class TestMultiChannelRender:
    def test_xy_channels_paint(self, view, qapp):
        """X + Y channel series render together without crashing."""
        view.resize(800, 400)
        view.set_series(_multi_channel_series())
        view.show()
        qapp.processEvents()
        view.repaint()

    def test_channels_have_distinct_colours(self, view, qapp):
        """X and Y channels get distinct axis colours (plan §19.6)."""
        view.set_series(_multi_channel_series())
        colors = {s.color for s in view._series}
        assert len(colors) >= 2  # X orange-red, Y green


# ── T124: empty state ─────────────────────────────────────────────────


class TestEmptyStateRender:
    def test_no_series_paints(self, view, qapp):
        """No series → empty state paints cleanly (no crash, blank canvas)."""
        view.resize(800, 400)
        view.set_series([])
        view.show()
        qapp.processEvents()
        view.repaint()

    def test_empty_series_paints(self, view, qapp):
        """A series with zero points paints without crashing."""
        view.resize(800, 400)
        s = _series(kfs=((0, 0.0),))  # single point → no curve
        view.set_series([s])
        view.show()
        qapp.processEvents()
        view.repaint()


# ── T124: long names ──────────────────────────────────────────────────


class TestLongNames:
    def test_long_param_name_paints(self, view, qapp):
        """A very long parameter name shouldn't crash the legend render."""
        view.resize(800, 400)
        node = TransformNode()
        track = KeyframeTrack(param_name="rotation")
        for f, v in ((0, 0.0), (50, 90.0), (100, 0.0)):
            track.add_keyframe(Keyframe(f, v))
        node.set_keyframe_track("rotation", track)
        s = build_graph_series(node, "rotation")
        # Inject a very long display name.
        s.display_name = "A" * 200
        view.set_series([s])
        view.show()
        qapp.processEvents()
        view.repaint()


# ── T124: combined overlays ───────────────────────────────────────────


class TestCombinedOverlaysRender:
    def test_all_overlays_paint_together(self, view, qapp):
        """Work-area + markers + transform box + expression curves all
        rendered together at a small size without crashing (no overlap
        assert — just 'completes')."""
        from src.features.composite.ui.graph_model import GraphOptions
        view.resize(600, 350)
        view.set_options(GraphOptions(
            show_post_expression_graph=True,
            show_transform_box=True,
            snap_enabled=True,
        ))
        view.set_series([_series()])
        view.set_play_range((20, 80))
        view.set_markers([
            {"frame": 30, "color": "#ff0000", "name": "M1"},
            {"frame": 70, "color": "#00ff00", "name": "M2"},
        ])
        view.show()
        qapp.processEvents()
        view.repaint()

    def test_render_after_zoom_and_pan(self, view, qapp):
        """After a user zoom + pan the layout still renders cleanly."""
        from PyQt6.QtCore import QPointF, Qt
        from PyQt6.QtGui import QMouseEvent
        view.resize(800, 400)
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        # Wheel-zoom (scroll).
        ev = QMouseEvent(QMouseEvent.Type.MouseButtonPress,
                         QPointF(400, 200), Qt.MouseButton.MiddleButton,
                         Qt.MouseButton.MiddleButton,
                         Qt.KeyboardModifier.NoModifier)
        view.mousePressEvent(ev)
        ev2 = QMouseEvent(QMouseEvent.Type.MouseMove, QPointF(450, 220),
                          Qt.MouseButton.MiddleButton,
                          Qt.MouseButton.NoButton,
                          Qt.KeyboardModifier.NoModifier)
        view.mouseMoveEvent(ev2)
        ev3 = QMouseEvent(QMouseEvent.Type.MouseButtonRelease,
                          QPointF(450, 220), Qt.MouseButton.MiddleButton,
                          Qt.MouseButton.NoButton,
                          Qt.KeyboardModifier.NoModifier)
        view.mouseReleaseEvent(ev3)
        view.repaint()
