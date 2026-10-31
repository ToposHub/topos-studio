"""T121 Marker / In-Out / Work Area overlay tests.

Locks the shared overlay contract between the Graph Editor and the
Timeline (plan §22.9 'Graph 与 Timeline 使用同一参考点和吸附目标'):

  * GraphEditorView.set_play_range / set_markers accept the same payload
    the Timeline uses and expose read-only accessors.
  * The work-area out-of-range shade + top band + marker ticks paint
    without crashing for every state (set, cleared, partial, off-screen).
  * _snap_frame snaps to work-area edges + markers + other keyframes when
    the snap toggle is on, and to the plain rounded integer when off.
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from src.features.composite.ui.graph_editor_view import GraphEditorView
from src.features.composite.ui.graph_model import (
    GraphOptions, build_graph_series,
)
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


@pytest.fixture
def view(qapp):
    v = GraphEditorView()
    v.resize(800, 400)
    v.set_frame_range(0, 100)
    return v


# ── T121: set_play_range / play_range ─────────────────────────────────


class TestPlayRange:
    def test_set_play_range_stores_tuple(self, view, qapp):
        view.set_play_range((10, 90))
        assert view.play_range() == (10, 90)

    def test_set_play_range_none_clears(self, view, qapp):
        view.set_play_range((10, 90))
        view.set_play_range(None)
        assert view.play_range() is None

    def test_set_play_range_coerces_ints(self, view, qapp):
        view.set_play_range((10.7, 90.2))
        assert view.play_range() == (10, 90)

    def test_set_play_range_idempotent(self, view, qapp):
        view.set_play_range((10, 90))
        view.set_play_range((10, 90))  # no change → no crash
        assert view.play_range() == (10, 90)

    def test_paint_with_play_range_no_crash(self, view, qapp):
        view.set_series([_series()])
        view.set_play_range((20, 80))
        view.show()
        qapp.processEvents()
        view.repaint()

    def test_paint_without_play_range_no_crash(self, view, qapp):
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        view.repaint()


# ── T121: set_markers / markers ───────────────────────────────────────


class TestMarkers:
    def test_set_markers_from_dicts(self, view, qapp):
        view.set_markers([
            {"frame": 10, "color": "#ff0000", "name": "A"},
            {"frame": 50, "color": "#00ff00", "name": "B"},
        ])
        ms = view.markers()
        assert len(ms) == 2
        assert ms[0] == {"frame": 10, "color": "#ff0000", "name": "A"}

    def test_set_markers_from_objects(self, view, qapp):
        class _M:
            def __init__(self, frame, color, name):
                self.frame = frame
                self.color = color
                self.name = name
        view.set_markers([_M(10, "#ff0000", "A")])
        ms = view.markers()
        assert ms[0] == {"frame": 10, "color": "#ff0000", "name": "A"}

    def test_set_markers_empty_clears(self, view, qapp):
        view.set_markers([{"frame": 10, "color": "#ff0000", "name": "A"}])
        view.set_markers([])
        assert view.markers() == []

    def test_set_markers_none_clears(self, view, qapp):
        view.set_markers([{"frame": 10, "color": "#ff0000", "name": "A"}])
        view.set_markers(None)
        assert view.markers() == []

    def test_markers_default_color(self, view, qapp):
        view.set_markers([{"frame": 10}])  # no color/name
        ms = view.markers()
        assert ms[0]["color"] == "#ff3d3d"  # default marker red
        assert ms[0]["name"] == ""

    def test_paint_with_markers_no_crash(self, view, qapp):
        view.set_series([_series()])
        view.set_markers([
            {"frame": 10, "color": "#ff0000", "name": "A"},
            {"frame": 90, "color": "#00ff00", "name": "B"},
        ])
        view.show()
        qapp.processEvents()
        view.repaint()


# ── T121: shared snap ─────────────────────────────────────────────────


class TestSnap:
    def test_snap_off_rounds_integer(self, view, qapp):
        """Snap disabled → plain integer rounding."""
        view.set_options(GraphOptions(snap_enabled=False))
        # 12.4 → 12 (round).
        assert view._snap_frame(12.4) == 12

    def test_snap_to_work_area_edge(self, view, qapp):
        """With snap on, a frame near the in-point snaps to it."""
        view.set_options(GraphOptions(snap_enabled=True))
        view.set_play_range((20, 80))
        view.show()
        qapp.processEvents()
        # 20.4 is closer to 20 than to 21 → snaps to 20 when within tol.
        assert view._snap_frame(20.4) == 20

    def test_snap_to_marker(self, view, qapp):
        """With snap on, a frame near a marker snaps to it."""
        view.set_options(GraphOptions(snap_enabled=True))
        view.set_markers([{"frame": 50, "color": "#ff0000", "name": "M"}])
        view.show()
        qapp.processEvents()
        assert view._snap_frame(50.4) == 50

    def test_snap_to_other_keyframe(self, view, qapp):
        """With snap on, a frame near another keyframe snaps to it."""
        view.set_options(GraphOptions(snap_enabled=True))
        view.set_series([_series(kfs=((0, 0.0), (50, 90.0), (100, 0.0)))])
        view.show()
        qapp.processEvents()
        # 50.4 is closer to 50 than to 51 → snaps to 50.
        assert view._snap_frame(50.4) == 50

    def test_snap_outside_tolerance_rounds(self, view, qapp):
        """A frame far from every candidate just rounds to the nearest int."""
        view.set_options(GraphOptions(snap_enabled=True))
        view.set_play_range((20, 80))
        view.set_markers([{"frame": 50, "color": "#ff0000", "name": "M"}])
        # 35.5 is far from 20/50/80 → rounds to 36.
        assert view._snap_frame(35.5) == 36

    def test_snap_returns_int(self, view, qapp):
        view.set_options(GraphOptions(snap_enabled=True))
        result = view._snap_frame(12.4)
        assert isinstance(result, int)


# ── T121: paint all overlays together ─────────────────────────────────


class TestOverlayPaint:
    def test_paint_work_area_and_markers_together(self, view, qapp):
        """All overlays render together without crashing."""
        view.set_series([_series()])
        view.set_play_range((20, 80))
        view.set_markers([
            {"frame": 30, "color": "#ff0000", "name": "M1"},
            {"frame": 70, "color": "#00ff00", "name": "M2"},
        ])
        view.show()
        qapp.processEvents()
        view.repaint()

    def test_paint_off_screen_marker_skipped(self, view, qapp):
        """A marker outside the visible frame range is skipped (no crash)."""
        view.set_series([_series()])
        view.set_markers([{"frame": 9999, "color": "#ff0000", "name": "Far"}])
        view.show()
        qapp.processEvents()
        view.repaint()
