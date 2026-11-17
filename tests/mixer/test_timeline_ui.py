"""Focused interaction checks for the local-only Mixer timeline."""
from __future__ import annotations

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QSignalSpy, QTest

from src.features.mixer.ui.session import MixerUiSession
from src.features.mixer.ui.timeline_panel import TimelinePanel


def _panel(qtbot):
    session = MixerUiSession()
    panel = TimelinePanel(session)
    panel.resize(1100, 460)
    qtbot.addWidget(panel)
    panel.show()
    qtbot.wait(10)
    return session, panel


def test_mouse_move_is_one_undoable_local_edit(qtbot):
    session, panel = _panel(qtbot)
    original = session.clips[0]
    rect = panel.canvas.clip_rect(original)
    start = QPoint(int(rect.left() + 90), int(rect.top() + 48))
    QTest.mousePress(panel.canvas, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(panel.canvas, start + QPoint(40, 0), delay=5)
    QTest.mouseRelease(panel.canvas, Qt.MouseButton.LeftButton, pos=start + QPoint(40, 0))
    assert session.selected_clip_id == original.id
    assert session.clip_by_id(original.id).start == 2.0
    assert session.undo_stack.count() == 1
    session.undo_stack.undo()
    assert session.clip_by_id(original.id).start == 0.0
    session.undo_stack.redo()
    assert session.clip_by_id(original.id).start == 2.0


def test_context_editor_requests_selected_clip_and_flags_are_undoable(qtbot):
    session, panel = _panel(qtbot)
    clip_id = session.clips[1].id
    requested = QSignalSpy(panel.clipEditorRequested)
    menu = panel.canvas.context_menu_for_clip(clip_id)
    assert session.selected_clip_id == clip_id
    menu.actions()[0].trigger()
    assert list(requested[0]) == [clip_id]
    mute = next(action for action in menu.actions() if action.text() == "片段静音")
    mute.trigger()
    assert session.clip_by_id(clip_id).muted
    session.undo_stack.undo()
    assert not session.clip_by_id(clip_id).muted
    menu.deleteLater()


def test_razor_split_and_undo(qtbot):
    session, panel = _panel(qtbot)
    clip = session.clips[0]
    panel._set_tool("razor")
    point = QPoint(int(panel.canvas.time_to_x(10)), int(panel.canvas.clip_rect(clip).top() + 45))
    QTest.mouseClick(panel.canvas, Qt.MouseButton.LeftButton, pos=point)
    assert len(session.clips) == 4
    assert session.clips[0].duration == 10.0
    session.undo_stack.undo()
    assert len(session.clips) == 3
    assert session.clips[0].duration == 86.913


def test_trim_and_fade_gestures_keep_single_undo_step(qtbot):
    session, panel = _panel(qtbot)
    panel.zoom.setValue(5)
    clip = session.clips[0]
    rect = panel.canvas.clip_rect(clip)
    start = QPoint(int(rect.right()), int(rect.top() + 45))
    QTest.mousePress(panel.canvas, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(panel.canvas, start - QPoint(20, 0), delay=5)
    QTest.mouseRelease(panel.canvas, Qt.MouseButton.LeftButton, pos=start - QPoint(20, 0))
    assert session.clip_by_id(clip.id).duration < 84
    assert session.undo_stack.count() == 1
    session.undo_stack.undo()
    assert session.clip_by_id(clip.id).duration == 86.913
    clip = session.clip_by_id(clip.id)
    rect = panel.canvas.clip_rect(clip)
    start = QPoint(int(rect.left() + 1), int(rect.top() + 25))
    QTest.mousePress(panel.canvas, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(panel.canvas, start + QPoint(25, 0), delay=5)
    QTest.mouseRelease(panel.canvas, Qt.MouseButton.LeftButton, pos=start + QPoint(25, 0))
    assert session.clip_by_id(clip.id).fade_in == 5.0
    session.undo_stack.undo()
    assert session.clip_by_id(clip.id).fade_in == 0.0


def test_copy_paste_has_distinct_instance_and_range_sets_loop(qtbot):
    session, panel = _panel(qtbot)
    clip = session.clips[0]
    session.select_clip(clip.id)
    panel.canvas.copy_selected()
    session.seek(4)
    panel.canvas.paste_clip()
    pasted = session.clip_by_id(session.selected_clip_id)
    assert pasted.id != clip.id
    assert pasted.start == 4
    session.undo_stack.undo()
    assert len(session.clips) == 3
    panel._set_tool("range")
    left = QPoint(int(panel.canvas.time_to_x(2)), 100)
    right = QPoint(int(panel.canvas.time_to_x(6)), 100)
    QTest.mousePress(panel.canvas, Qt.MouseButton.LeftButton, pos=left)
    QTest.mouseMove(panel.canvas, right, delay=5)
    QTest.mouseRelease(panel.canvas, Qt.MouseButton.LeftButton, pos=right)
    assert session.loop_start == 2.0
    assert session.loop_end == 6.0


def test_timecode_entry_is_independent_from_display_format(qtbot):
    session, panel = _panel(qtbot)
    session.time_format = "bars"
    session.timeline_format_changed.emit()
    panel.start_edit.setText("00:00:02:12")
    panel._set_start()
    assert session.clip_by_id(session.selected_clip_id).start == 2.5


def test_one_second_snap_remains_one_second_at_fractional_frame_rate(qtbot):
    session, panel = _panel(qtbot)
    session.set_rate(3)
    panel.snap_combo.setCurrentIndex(3)
    assert panel.canvas.snap_time(1.1) == 1.0
    panel.snap_combo.setCurrentIndex(0)
    assert abs(panel.canvas.snap_time(1.1) * session.fps - 33) < 1e-9
