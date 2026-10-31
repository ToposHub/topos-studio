"""T46 tests: timeline drag snapping.

The snap toggle (T27 toolbar) flips a flag on the timeline view; when on,
dragged keyframes and the playhead snap to:
  * integer frames (always — rounding already does this),
  * the nearest keyframe on any *other* track, within a pixel tolerance,
  * the work-area In/Out edges, within a pixel tolerance.

This file locks:
  * ``set_snap_enabled`` flips the flag and ``snap_enabled`` reads it back;
  * ``_snap_frame`` returns the input unchanged when snap is off;
  * ``_snap_frame`` snaps to a neighbour keyframe within tolerance and
    leaves it unchanged when no target is in range;
  * the dragged row's own keyframes are NOT snap targets (a keyframe
    doesn't snap to itself);
  * the work-area edges are snap targets;
  * the panel's toolbar toggle forwards through to the view.
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.ui.keyframes_panel import KeyframesPanel
from src.shared.keyframe import Keyframe, KeyframeTrack


CLIP_FRAMES = 100
CLIP_FPS = 24.0
_RULER_H = 30
_ROW_H = 24
PPF = 10.0   # 1 frame = 10 px → 8 px snap tol ≈ 0.8 frame → rounds to 1


def _graph_two_tracks():
    """A graph with two nodes, each with a keyframe track, so we can test
    snapping a dragged keyframe on one track to a keyframe on the other."""
    g = NodeGraph()
    t1 = TransformNode()
    tr1 = KeyframeTrack(param_name="translate_x")
    tr1.add_keyframe(Keyframe(20, 0.0))
    tr1.add_keyframe(Keyframe(50, 100.0))
    t1.set_keyframe_track("translate_x", tr1)
    t2 = TransformNode()
    tr2 = KeyframeTrack(param_name="translate_x")
    tr2.add_keyframe(Keyframe(30, 0.0))   # snap target
    tr2.add_keyframe(Keyframe(70, 200.0))
    t2.set_keyframe_track("translate_x", tr2)
    g.add_node(t1)
    g.add_node(t2)
    return g, t1, t2


@pytest.fixture
def panel(qapp):
    g, _t1, _t2 = _graph_two_tracks()
    p = KeyframesPanel()
    p.resize(1000, 300)
    p.set_node_graph(g)
    p.set_frame_range(0, CLIP_FRAMES - 1, fps=CLIP_FPS)
    p._frame_mapper.set_zoom(PPF)
    p._frame_mapper.set_scroll(0.0)
    return p


@pytest.fixture
def view(panel):
    return panel._timeline_view


# ── toggle plumbing ────────────────────────────────────────────────────


class TestToggle:
    def test_snap_disabled_by_default(self, view):
        assert view.snap_enabled is False
        assert view._snap_enabled is False

    def test_set_snap_enabled_flips_flag(self, view):
        view.set_snap_enabled(True)
        assert view.snap_enabled is True
        view.set_snap_enabled(False)
        assert view.snap_enabled is False

    def test_panel_toolbar_forwards_to_view(self, panel, view):
        """The panel's snap action (T27) must forward its state to the view
        so the drag handler reads the same flag the button shows."""
        panel._act_snap.setChecked(True)
        assert view.snap_enabled is True
        panel._act_snap.setChecked(False)
        assert view.snap_enabled is False


# ── _snap_frame math ──────────────────────────────────────────────────


class TestSnapFrameMath:
    def test_returns_input_when_disabled(self, view):
        view.set_snap_enabled(False)
        assert view._snap_frame(42) == 42
        assert view._snap_frame(0) == 0

    def test_returns_input_when_no_targets(self, view):
        """No other-track keyframes near the candidate → unchanged."""
        view.set_snap_enabled(True)
        # Frame 95 is far from any keyframe (20/30/50/70) and no work area.
        assert view._snap_frame(95) == 95

    def test_snaps_to_neighbour_keyframe_in_tolerance(self, view):
        """A candidate within tolerance of another track's keyframe snaps
        to it."""
        view.set_snap_enabled(True)
        # t2 has a keyframe at frame 30. At PPF=10, the 8 px tolerance is
        # ~1 frame, so frame 29 → 30 and frame 31 → 30.
        assert view._snap_frame(29) == 30
        assert view._snap_frame(31) == 30

    def test_does_not_snap_to_own_row_keyframes(self, view, panel):
        """The dragged row's own keyframes are excluded — a keyframe on
        t1 (frames 20, 50) doesn't snap to itself when dragged."""
        view.set_snap_enabled(True)
        t1 = panel._model.rows[0].node_id
        row_id = f"param:{t1}:translate_x"
        # Cursor at frame 21 (near t1's own frame-20 keyframe). With row_id
        # excluded, frame 20 is NOT a candidate, so no snap happens (30 is
        # too far at ~1-frame tolerance).
        assert view._snap_frame(21, row_id=row_id) == 21

    def test_snaps_to_work_area_edges(self, view, panel):
        """The play-range In/Out edges are snap targets."""
        view.set_snap_enabled(True)
        panel.set_play_range(40, 60)
        # Frame 41 is within tolerance of In=40.
        assert view._snap_frame(41) == 40
        # Frame 59 is within tolerance of Out=60.
        assert view._snap_frame(59) == 60

    def test_picks_closest_when_multiple_in_tolerance(self, view, panel):
        """When two targets are both within tolerance, the closest wins."""
        view.set_snap_enabled(True)
        panel.set_play_range(40, 60)
        # t2 has keyframe at 50 (also the work-area midpoint). Cursor at 49
        # is 1 away from 50 (keyframe) and 9 away from 40 (edge) → snaps
        # to 50.
        assert view._snap_frame(49) == 50

    def test_frame_tolerance_grows_when_zoomed_out(self, view):
        """At a small pixels-per-frame (zoomed out), the pixel tolerance
        covers more frames so snap reaches further.

        P1-UI-02 规格裁决（snap 到 99 的语义）：首次绑定组合时默认工作区
        = 完整组合 (0, 99)，Out=99 与任何关键帧一样是合法吸附目标；
        frame 100 距 Out 1 帧、距 70 关键帧 30 帧 → 就近者胜，吸附 99。
        """
        view.set_snap_enabled(True)
        # 默认缩放（PPF=10 → 容差 ~1 帧）：95 距 Out=99 有 4 帧，不吸附。
        assert view._snap_frame(95) == 95
        # Zoom way out: 0.1 px/frame → 8 px tol ≈ 80 frames。
        view._fm.set_zoom(0.1)
        # Out=99（1 帧外）比 70 关键帧（30 帧外）更近 → 吸附 99。
        assert view._snap_frame(100) == 99


# ── end-to-end: dragging with snap lands on the target ────────────────


def _press_drag_release(view, press_x, press_y, drag_x,
                        button=None):
    """Synthesize a full left-button drag on the timeline view."""
    from PyQt6.QtCore import QPointF, Qt, QEvent
    from PyQt6.QtGui import QMouseEvent
    if button is None:
        button = Qt.MouseButton.LeftButton
    QET = QEvent.Type
    view.mousePressEvent(QMouseEvent(QET.MouseButtonPress, QPointF(press_x, press_y),
                                     button, button, Qt.KeyboardModifier.NoModifier))
    view.mouseMoveEvent(QMouseEvent(QET.MouseMove, QPointF(drag_x, press_y),
                                    Qt.MouseButton.NoButton, button,
                                    Qt.KeyboardModifier.NoModifier))
    view.mouseReleaseEvent(QMouseEvent(QET.MouseButtonRelease, QPointF(drag_x, press_y),
                                       button, Qt.MouseButton.NoButton,
                                       Qt.KeyboardModifier.NoModifier))


class TestEndToEndDragSnap:
    def test_drag_without_snap_lands_where_dropped(self, panel, view):
        """Sanity: with snap off, a drag moves the keyframe to the cursor's
        rounded frame (existing behaviour preserved)."""
        from PyQt6.QtCore import Qt
        view.set_snap_enabled(False)
        # Select t1's keyframe at frame 20.
        t1_id = panel._model.rows[0].node_id
        row_id = f"param:{t1_id}:translate_x"
        track = panel._model.track_for(row_id)
        track.keyframes[0].selected = True
        y = _RULER_H + _ROW_H // 2  # node-row y (param row is one below)
        # Find the param row's y.
        for i, row in enumerate(panel._model.visible_rows()):
            if row.row_id == row_id:
                y = _RULER_H + i * _ROW_H + _ROW_H // 2
                break
        # Press at frame 20 (x=200), drag to frame 25 (x=250).
        _press_drag_release(view, 200, y, 250)
        # t1's first keyframe should now be at frame 25.
        assert track.keyframes[0].frame == 25

    def test_drag_with_snap_lands_on_neighbour(self, panel, view):
        """With snap on, dragging t1's frame-20 keyframe near t2's frame-30
        keyframe snaps to 30."""
        from PyQt6.QtCore import Qt
        view.set_snap_enabled(True)
        t1_id = panel._model.rows[0].node_id
        row_id = f"param:{t1_id}:translate_x"
        track = panel._model.track_for(row_id)
        track.keyframes[0].selected = True
        y = _RULER_H + _ROW_H + _ROW_H // 2  # param row (second row)
        for i, row in enumerate(panel._model.visible_rows()):
            if row.row_id == row_id:
                y = _RULER_H + i * _ROW_H + _ROW_H // 2
                break
        # Drag to frame 29 (x=290) — within tolerance of t2's frame 30.
        _press_drag_release(view, 200, y, 290)
        assert track.keyframes[0].frame == 30, (
            f"snap should land on 30, got {track.keyframes[0].frame}")
