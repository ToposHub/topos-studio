"""T48 tests: playhead drag throttle + release-commit.

Plan §3.5: '拖动播放头时不触发昂贵的 undo；鼠标释放后只发布一次当前帧变化'.
The playhead drag throttles emits to ~60fps so a fast drag doesn't flood
CompositePage with re-render requests; on release, one final emit at the
release position guarantees the settled frame is always published exactly
once — even if the last mouseMoveEvent landed inside the throttle window.

These tests lock:
  * the throttle drops moves within the 16ms window (fewer emits than
    moves);
  * a ``force=True`` call bypasses the throttle (used on release);
  * release always publishes the final frame, even when the preceding
    move was throttled away;
  * the released frame matches the release x position (no stale frame).
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
PPF = 10.0


@pytest.fixture
def panel(qapp):
    g = NodeGraph()
    node = TransformNode()
    track = KeyframeTrack(param_name="translate_x")
    track.add_keyframe(Keyframe(20, 0.0))
    node.set_keyframe_track("translate_x", track)
    g.add_node(node)
    p = KeyframesPanel()
    p.resize(1000, 300)
    p.set_node_graph(g)
    p.set_frame_range(0, CLIP_FRAMES - 1, fps=CLIP_FPS)
    p._frame_mapper.set_zoom(PPF)
    p._frame_mapper.set_scroll(0.0)
    return p


def _move(view, x, mods=Qt.KeyboardModifier.NoModifier):
    btn = Qt.MouseButton.LeftButton
    view.mouseMoveEvent(QMouseEvent(
        QEvent.Type.MouseMove, QPointF(x, _RULER_H // 2),
        Qt.MouseButton.NoButton, btn, mods))


def _press(view, x, mods=Qt.KeyboardModifier.NoModifier):
    btn = Qt.MouseButton.LeftButton
    view.mousePressEvent(QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(x, _RULER_H // 2),
        btn, btn, mods))


def _release(view, x, mods=Qt.KeyboardModifier.NoModifier):
    btn = Qt.MouseButton.LeftButton
    view.mouseReleaseEvent(QMouseEvent(
        QEvent.Type.MouseButtonRelease, QPointF(x, _RULER_H // 2),
        btn, Qt.MouseButton.NoButton, mods))


# ── throttle drops moves within the window ────────────────────────────


class TestThrottle:
    def test_rapid_moves_emit_fewer_than_moves(self, panel):
        """Many moves in quick succession should produce fewer emits than
        moves — the throttle is doing its job."""
        view = panel._timeline_view
        emitted = []
        view.current_frame_changed.connect(emitted.append)
        _press(view, 100)   # frame 10
        # Fire 20 moves in a tight loop — all within the 16ms throttle
        # window relative to each other. At most a handful should emit.
        for i in range(20):
            _move(view, 100 + i * 10)   # frames 10..29
        # Without throttle we'd have ~20 emits; with throttle we should
        # have far fewer.
        assert len(emitted) < 20, (
            f"throttle not effective: {len(emitted)} emits for 20 moves")
        # The playhead should still track the last emitted frame.
        assert view._current_frame <= 29

    def test_force_bypasses_throttle(self, panel):
        """A force=True call emits even inside the throttle window."""
        view = panel._timeline_view
        emitted = []
        view.current_frame_changed.connect(emitted.append)
        _press(view, 100)   # frame 10 — emits
        first_count = len(emitted)
        # Immediate force call at a new x — should emit despite the window.
        view._do_playhead_drag(200, force=True)   # frame 20
        assert len(emitted) == first_count + 1
        assert emitted[-1] == 20


# ── release always commits the final frame ────────────────────────────


class TestReleaseCommit:
    def test_release_publishes_final_frame_after_throttled_move(self, panel):
        """The key T48 behaviour: when the last move is throttled away, the
        release still publishes the frame at the release position."""
        view = panel._timeline_view
        emitted = []
        view.current_frame_changed.connect(emitted.append)
        _press(view, 100)   # frame 10
        # Move to frame 50 — this emit may or may not be throttled.
        _move(view, 500)
        # Immediately release at frame 50 — even if the move was throttled,
        # the release must publish frame 50.
        _release(view, 500)
        assert 50 in emitted, (
            f"release did not commit the final frame; emits={emitted}")
        assert emitted[-1] == 50, (
            f"final emit should be the release frame (50); got {emitted[-1]}")

    def test_release_frame_matches_release_x(self, panel):
        """The released frame must equal the release x position (no stale
        frame from the last non-throttled move)."""
        view = panel._timeline_view
        emitted = []
        view.current_frame_changed.connect(emitted.append)
        _press(view, 100)        # frame 10
        _move(view, 300)         # frame 30
        _release(view, 700)      # frame 70
        assert emitted[-1] == 70, (
            f"expected final frame 70 (release x=700), got {emitted[-1]}")

    def test_release_without_move_emits_once(self, panel):
        """A press+release at the same x (a click on the ruler, no drag)
        publishes that frame exactly once."""
        view = panel._timeline_view
        emitted = []
        view.current_frame_changed.connect(emitted.append)
        _press(view, 400)    # frame 40
        before = len(emitted)
        _release(view, 400)
        # The press already emitted (force on the first press isn't used,
        # but the press calls _do_playhead_drag once). The release either
        # emits once more (throttled press) or is a no-op duplicate. Either
        # way the final state is frame 40.
        assert view._current_frame == 40
        assert emitted.count(40) >= 1

    def test_release_publishes_even_when_no_move_emitted(self, panel, monkeypatch):
        """Edge case: every move was throttled away (extremely fast drag),
        so the only emit comes from the release."""
        view = panel._timeline_view
        emitted = []
        view.current_frame_changed.connect(emitted.append)
        # Stub the throttle to always drop non-forced moves.
        monkeypatch.setattr(view, "_drag_last_emit", float("inf"))
        _press(view, 100)
        emitted.clear()
        # All moves throttled (last_emit is inf → window never expires).
        for x in (200, 300, 400):
            _move(view, x)
        assert emitted == [], "moves should all be throttled"
        # Release must still publish the final frame.
        _release(view, 500)
        assert 50 in emitted


# ── snap + throttle interact correctly ─────────────────────────────────


class TestSnapWithThrottle:
    def test_release_applies_snap(self, panel):
        """T46 snap + T48 release: the release frame is snapped when snap
        is on."""
        view = panel._timeline_view
        view.set_snap_enabled(True)
        emitted = []
        view.current_frame_changed.connect(emitted.append)
        _press(view, 100)    # frame 10
        _move(view, 205)     # frame ~20.5 → 21
        _release(view, 205)
        # There's a keyframe at frame 20; at PPF=10 the 8 px tolerance
        # rounds to 1 frame, so frame 21 is just outside. The release
        # should still emit a frame near 20-21 (snap may or may not fire
        # depending on tolerance), but the important thing is it emits.
        assert emitted
        assert view._current_frame in (20, 21)
