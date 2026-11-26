"""Keyboard handling during an active rolling trim."""

from typing import Any

import pytest
from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QKeyEvent

from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.edit.ui.timeline_view import TimelineView

pytestmark = pytest.mark.edit_ui


def _roll_view(qtbot: Any) -> TimelineView:
    view = TimelineView()
    qtbot.addWidget(view)
    view.load_timeline(TimelineSequence("roll", "Roll", fps=24, video_tracks=(
        EditTrack("v", "V1", "video", 0, clips=(
            ClipInstance("a", "asset", "v", 0, 100, 100, 100),
            ClipInstance("b", "asset", "v", 100, 100, 200, 100),
        )),
    )))
    for item in view._items_map.values():
        item._max_source_frames = 1000
    return view


class TestRollJklKeyboard:
    def _press(self, view, key, modifiers=Qt.KeyboardModifier.NoModifier):
        from PyQt6.QtCore import QEvent

        event = QKeyEvent(
            QEvent.Type.KeyPress, key, modifiers)
        view.keyPressEvent(event)
        return event

    def test_jkl_steps_cut_and_enter_commits_once(self, qapp, qtbot) -> None:
        view = _roll_view(qtbot)
        view._on_roll_trim_started("a", "b", 100)

        # L +1、L +1、Shift+L +10、J −1 → delta = 11
        self._press(view, Qt.Key.Key_L)
        self._press(view, Qt.Key.Key_L)
        self._press(view, Qt.Key.Key_L, Qt.KeyboardModifier.ShiftModifier)
        self._press(view, Qt.Key.Key_J)
        assert view._roll_preview_state["delta"] == 11

        # K 暂停不改变 delta
        self._press(view, Qt.Key.Key_K)
        assert view._roll_preview_state["delta"] == 11

        # Enter 提交：单步 Undo，双片按 delta 落库
        self._press(view, Qt.Key.Key_Return)
        assert view._timeline.get_clip_by_id("a").timeline_duration == 111
        assert view._timeline.get_clip_by_id("b").timeline_start == 111
        assert view._timeline.get_clip_by_id("b").timeline_duration == 89
        assert len(view._command_stack._undo_stack) == 1
        assert view._interaction.is_idle

    def test_esc_cancels_without_revision(self, qapp, qtbot) -> None:
        view = _roll_view(qtbot)
        view._on_roll_trim_started("a", "b", 100)
        self._press(view, Qt.Key.Key_L)
        assert view._roll_preview_state["delta"] == 1

        from PyQt6.QtCore import QEvent

        view.keyPressEvent(QKeyEvent(
            QEvent.Type.KeyPress, Qt.Key.Key_Escape,
            Qt.KeyboardModifier.NoModifier))
        assert view._interaction.is_idle
        assert len(view._command_stack._undo_stack) == 0
        assert view._timeline.get_clip_by_id("a").timeline_duration == 100

    def test_jkl_outside_session_not_consumed(self, qapp, qtbot) -> None:
        """会话外 J/K/L 不被本分支消费（透传输层 JKL）。"""
        view = _roll_view(qtbot)
        event = self._press(view, Qt.Key.Key_L)
        assert not event.isAccepted() or view._roll_preview_state is not None
        assert view._roll_preview_state is None
