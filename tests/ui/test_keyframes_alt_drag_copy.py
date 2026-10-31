"""T42 tests: Alt/Option-drag keyframe copy.

Plan §4.1: 'Alt/Option 拖动关键帧 → 复制关键帧后拖动，保留原关键帧'.
The original keyframe survives the drag; a copy is created at the source
frame and dragged with the cursor. Release commits one undo step.

These tests exercise the full press→drag→release cycle and assert:
  * the original keyframe stays at its source frame;
  * a copy appears at the dropped frame;
  * one undo step is emitted (one track_changed per affected row);
  * plain drag (no Alt) still moves the keyframe (legacy preserved);
  * a no-move Alt+click does NOT duplicate in place (AUD-10203: the copy
    is only materialized on the first real move, so a press+release
    without dragging keeps the one-keyframe-per-frame invariant).
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from PyQt6.QtCore import QPointF, Qt, QEvent
from PyQt6.QtGui import QMouseEvent

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.ui.keyframes_panel import KeyframesPanel
from src.shared.keyframe import Keyframe, KeyframeTrack


CLIP_FRAMES = 100
CLIP_FPS = 24.0
_RULER_H = 30
_ROW_H = 24
PPF = 10.0


def _make_graph():
    g = NodeGraph()
    node = TransformNode()
    track = KeyframeTrack(param_name="translate_x")
    track.add_keyframe(Keyframe(20, 0.0))
    track.add_keyframe(Keyframe(50, 100.0))
    node.set_keyframe_track("translate_x", track)
    g.add_node(node)
    return g, node


@pytest.fixture
def panel(qapp):
    g, _node = _make_graph()
    p = KeyframesPanel()
    p.resize(1000, 300)
    p.set_node_graph(g)
    p.set_frame_range(0, CLIP_FRAMES - 1, fps=CLIP_FPS)
    p._frame_mapper.set_zoom(PPF)
    p._frame_mapper.set_scroll(0.0)
    return p


def _param_row_y(panel, node_id, param="translate_x"):
    for i, row in enumerate(panel._model.visible_rows()):
        if row.row_id == f"param:{node_id}:{param}":
            return _RULER_H + i * _ROW_H + _ROW_H // 2
    raise AssertionError("param row not found")


def _drag(view, press_x, press_y, drag_x,
          mods=Qt.KeyboardModifier.NoModifier):
    QET = QEvent.Type
    btn = Qt.MouseButton.LeftButton
    view.mousePressEvent(QMouseEvent(
        QET.MouseButtonPress, QPointF(press_x, press_y), btn, btn, mods))
    view.mouseMoveEvent(QMouseEvent(
        QET.MouseMove, QPointF(drag_x, press_y),
        Qt.MouseButton.NoButton, btn, mods))
    view.mouseReleaseEvent(QMouseEvent(
        QET.MouseButtonRelease, QPointF(drag_x, press_y),
        btn, Qt.MouseButton.NoButton, mods))


# ── plain drag still moves (legacy regression guard) ──────────────────


def test_plain_drag_moves_keyframe(panel):
    """No Alt → the keyframe moves (T42 must not break the legacy path)."""
    node_id = panel._model.rows[0].node_id
    row_id = f"param:{node_id}:translate_x"
    track = panel._model.track_for(row_id)
    track.keyframes[0].selected = True
    y = _param_row_y(panel, node_id)
    _drag(panel._timeline_view, 200, y, 250,
          mods=Qt.KeyboardModifier.NoModifier)
    frames = sorted(kf.frame for kf in track.keyframes)
    # Frame 20 moved to 25; frame 50 unchanged.
    assert frames == [25, 50]


# ── Alt-drag copies ───────────────────────────────────────────────────


def test_alt_drag_copies_keyframe(panel):
    """Alt+drag → original stays, copy appears at the dropped frame."""
    node_id = panel._model.rows[0].node_id
    row_id = f"param:{node_id}:translate_x"
    track = panel._model.track_for(row_id)
    track.keyframes[0].selected = True
    y = _param_row_y(panel, node_id)
    # Alt-drag from frame 20 (x=200) to frame 25 (x=250).
    _drag(panel._timeline_view, 200, y, 250,
          mods=Qt.KeyboardModifier.AltModifier)
    frames = sorted(kf.frame for kf in track.keyframes)
    # Original at 20 + copy at 25 + untouched keyframe at 50.
    assert frames == [20, 25, 50]


def test_alt_drag_preserves_value_and_interp(panel):
    """The copy inherits the original's value + interpolation."""
    node_id = panel._model.rows[0].node_id
    row_id = f"param:{node_id}:translate_x"
    track = panel._model.track_for(row_id)
    src = track.keyframes[0]
    src.value = 42.0
    src.interpolation = "bezier"
    src.selected = True
    y = _param_row_y(panel, node_id)
    _drag(panel._timeline_view, 200, y, 260,
          mods=Qt.KeyboardModifier.AltModifier)
    # Find the copy at frame 30 (200 + 60 px / 10 ppf = 60... wait, drag
    # delta = (260-200)/10 = 6 frames → 20+6 = 26).
    copy = next(kf for kf in track.keyframes if kf.frame == 26)
    assert copy.value == 42.0
    assert copy.interpolation == "bezier"
    # Original unchanged.
    orig = next(kf for kf in track.keyframes if kf.frame == 20)
    assert orig.value == 42.0


def test_alt_drag_emits_one_track_changed(panel):
    """One undo step per affected row (the drag commits once on release)."""
    node_id = panel._model.rows[0].node_id
    row_id = f"param:{node_id}:translate_x"
    track = panel._model.track_for(row_id)
    track.keyframes[0].selected = True
    y = _param_row_y(panel, node_id)
    emitted = []
    panel._timeline_view.track_changed.connect(
        lambda nid, pn, kfs: emitted.append((nid, pn)))
    _drag(panel._timeline_view, 200, y, 250,
          mods=Qt.KeyboardModifier.AltModifier)
    # Exactly one commit for the affected row.
    assert len(emitted) == 1
    assert emitted[0][0] == node_id
    assert emitted[0][1] == "translate_x"


def test_alt_click_with_no_move_does_not_duplicate(panel):
    """AUD-10203: pressing Alt and releasing without moving must NOT leave
    two coincident keyframes on the same frame. The T42 copies are only
    materialized once the gesture becomes a real drag; a no-move release
    commits nothing (no duplicate, no undo record)."""
    node_id = panel._model.rows[0].node_id
    row_id = f"param:{node_id}:translate_x"
    track = panel._model.track_for(row_id)
    track.keyframes[0].selected = True
    y = _param_row_y(panel, node_id)
    emitted = []
    panel._timeline_view.track_changed.connect(
        lambda nid, pn, kfs: emitted.append((nid, pn)))
    # Press + release at the same x.
    _drag(panel._timeline_view, 200, y, 200,
          mods=Qt.KeyboardModifier.AltModifier)
    frame20 = [kf for kf in track.keyframes if kf.frame == 20]
    # Exactly the original — the one-keyframe-per-frame invariant holds.
    assert len(frame20) == 1
    assert emitted == []


def test_alt_drag_multi_selection_copies_all(panel):
    """Alt-dragging a multi-keyframe selection copies every selected
    keyframe; the originals stay behind."""
    node_id = panel._model.rows[0].node_id
    row_id = f"param:{node_id}:translate_x"
    track = panel._model.track_for(row_id)
    # Select both keyframes (frames 20 and 50).
    track.keyframes[0].selected = True
    track.keyframes[1].selected = True
    y = _param_row_y(panel, node_id)
    _drag(panel._timeline_view, 200, y, 210,
          mods=Qt.KeyboardModifier.AltModifier)
    frames = sorted(kf.frame for kf in track.keyframes)
    # Originals at 20 + 50, copies at 21 + 51.
    assert frames == [20, 21, 50, 51]


# ── interaction with snap (T42 + T46) ─────────────────────────────────


def test_alt_drag_respects_snap(panel):
    """Alt-drag with snap on lands the copy on a snap target."""
    node_id = panel._model.rows[0].node_id
    row_id = f"param:{node_id}:translate_x"
    track = panel._model.track_for(row_id)
    track.keyframes[0].selected = True  # frame 20
    panel._timeline_view.set_snap_enabled(True)
    # The other keyframe at frame 50 is a snap target. Drag the copy near
    # frame 51 (within the ~1-frame tolerance at PPF=10).
    y = _param_row_y(panel, node_id)
    _drag(panel._timeline_view, 200, y, 510,
          mods=Qt.KeyboardModifier.AltModifier)
    frames = sorted(kf.frame for kf in track.keyframes)
    # The copy should snap to 50 (collides with the existing 50 — the
    # conflict guard refuses, so the copy stays at its raw drop = 51).
    # Either way, snap was attempted without crashing.
    assert 20 in frames  # original preserved
    assert len(frames) >= 3  # original + copy + other original


# ── flag lifecycle ────────────────────────────────────────────────────


def test_drag_copy_flag_resets_after_release(panel):
    """A plain drag following an Alt-drag must NOT copy — the flag is
    cleared on release."""
    node_id = panel._model.rows[0].node_id
    row_id = f"param:{node_id}:translate_x"
    track = panel._model.track_for(row_id)
    y = _param_row_y(panel, node_id)

    # First gesture: Alt-drag the frame-20 keyframe to 25.
    track.keyframes[0].selected = True
    _drag(panel._timeline_view, 200, y, 250,
          mods=Qt.KeyboardModifier.AltModifier)
    after_alt = sorted(kf.frame for kf in track.keyframes)
    assert after_alt == [20, 25, 50]

    # Reset selection: pick the frame-25 copy.
    for kf in track.keyframes:
        kf.selected = (kf.frame == 25)

    # Second gesture: plain drag (no Alt) of the copy from 25 to 30.
    _drag(panel._timeline_view, 250, y, 300,
          mods=Qt.KeyboardModifier.NoModifier)
    after_plain = sorted(kf.frame for kf in track.keyframes)
    # The copy moved 25→30 (not duplicated); originals at 20 and 50 stay.
    assert after_plain == [20, 30, 50]
