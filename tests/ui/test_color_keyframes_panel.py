"""Exercise the Color panel with real Qt events, without a project or renderer."""
from __future__ import annotations

from dataclasses import replace

from PyQt6.QtCore import QPoint, Qt, QTimer
from PyQt6.QtGui import QContextMenuEvent
from PyQt6.QtWidgets import QApplication, QDialog, QMenu, QSlider
from pytest import MonkeyPatch
from pytestqt.qtbot import QtBot

from src.features.color.ui.color_keyframes_panel import ColorKeyframesPanel
from src.features.color.ui.color_keyframes_view_state import EditRequest, demo_snapshot, tr

LEFT = Qt.MouseButton.LeftButton


def _open(qtbot: QtBot, *, demo: bool = True) -> ColorKeyframesPanel:
    panel = ColorKeyframesPanel(demo=demo)
    panel.resize(550, 380)
    qtbot.addWidget(panel)
    panel.show()
    qtbot.waitUntil(panel.isVisible)
    return panel


def _point(panel: ColorKeyframesPanel, row: str, frame: int) -> QPoint:
    return next(QPoint(panel.timeline.frame_x(frame), y + 12)
                for key, _, y in panel.timeline._visible_rows() if key == row)


def test_scope_shortcut_and_row_menu_have_distinct_write_targets(qtbot: QtBot, monkeypatch: MonkeyPatch) -> None:
    panel = _open(qtbot)
    panel.mode.setCurrentIndex(panel.mode.findData("sizing"))
    assert len(panel._rows) == 4

    qtbot.keyClick(panel.timeline, Qt.Key.Key_BracketLeft, Qt.KeyboardModifier.ControlModifier)
    assert ("sizing/input", 32) in panel._markers
    assert ("corrector-1/correction", 32) not in panel._markers

    def choose_add(menu: QMenu, _: QPoint) -> None:
        menu.actions()[0].trigger()

    monkeypatch.setattr(QMenu, "exec", choose_add)
    pos = _point(panel, "corrector-1", 95)
    panel.timeline.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Reason.Mouse, pos))
    assert ("corrector-1/correction", 32) in panel._markers
    assert ("corrector-1/correction", 95) not in panel._markers
    assert panel._markers[("corrector-1/correction", 32)].kind == "static"


def test_collapsed_group_drag_moves_only_its_children_and_can_cancel(qtbot: QtBot) -> None:
    panel = _open(qtbot)
    row = "corrector-1"
    start = _point(panel, row, 0)
    dest = _point(panel, row, 20)
    qtbot.mousePress(panel.timeline, LEFT, pos=start)
    qtbot.mouseMove(panel.timeline, dest)
    qtbot.keyClick(panel.timeline, Qt.Key.Key_Escape)
    qtbot.mouseRelease(panel.timeline, LEFT, pos=dest)
    assert ("corrector-1/correction", 0) in panel._markers

    qtbot.mousePress(panel.timeline, LEFT, pos=start)
    qtbot.mouseMove(panel.timeline, dest)
    qtbot.mouseRelease(panel.timeline, LEFT, pos=dest)
    moved_frame = panel.timeline.x_frame(dest.x())
    assert len(panel._aggregate[row][moved_frame]) > 1
    assert ("corrector-1/correction", 0) not in panel._markers
    assert ("corrector-2/correction", 0) in panel._markers


def test_locked_parent_blocks_child_deletion_and_auto_switches(qtbot: QtBot) -> None:
    panel = _open(qtbot)
    row = "corrector-1"
    y = _point(panel, row, 0).y()
    qtbot.mouseClick(panel.timeline, LEFT, pos=QPoint(45, y))
    assert panel._tracks["corrector-1/correction"].auto
    qtbot.mouseClick(panel.timeline, LEFT, pos=QPoint(28, y))
    qtbot.mouseClick(panel.timeline, LEFT, pos=QPoint(60, y))
    child = "corrector-1/linear_window"
    qtbot.mouseClick(panel.timeline, LEFT, pos=_point(panel, child, 0))
    qtbot.keyClick(panel.timeline, Qt.Key.Key_Delete)
    assert (child, 0) in panel._markers
    qtbot.mouseClick(panel.timeline, LEFT, pos=QPoint(28, y))
    qtbot.mouseClick(panel.timeline, LEFT, pos=_point(panel, child, 0))
    qtbot.keyClick(panel.timeline, Qt.Key.Key_Delete)
    assert (child, 0) not in panel._markers


def test_host_requests_do_not_mutate_snapshot_or_echo_programmatic_seek(qtbot: QtBot) -> None:
    panel = _open(qtbot, demo=False)
    source = replace(demo_snapshot(), context="test")
    edits: list[EditRequest] = []
    seeks: list[int] = []
    panel.edit_requested.connect(edits.append)
    panel.seek_requested.connect(seeks.append)
    panel.set_snapshot(source)
    panel.set_playhead(45)
    assert not seeks

    qtbot.mouseClick(panel.timeline, LEFT, pos=QPoint(panel.timeline.frame_x(75), 8))
    assert seeks == [75]
    qtbot.keyClick(panel.timeline, Qt.Key.Key_BracketLeft, Qt.KeyboardModifier.ControlModifier)
    assert len(edits) == 1
    assert edits[0].context == "test"
    assert edits[0].kind == "dynamic"
    assert all(frame == 75 for _, frame in edits[0].targets)
    assert panel.snapshot is source
    assert not any(frame == 75 for _, frame in panel._markers)
    panel.set_snapshot(replace(source, context="another"))
    assert panel.playhead == 32
    assert seeks == [75]


def test_color_scope_follows_selected_demo_corrector(qtbot: QtBot) -> None:
    panel = _open(qtbot)
    panel.mode.setCurrentIndex(panel.mode.findData("color"))
    y = _point(panel, "corrector-2", 0).y()
    qtbot.mouseClick(panel.timeline, LEFT, pos=QPoint(102, y))
    qtbot.keyClick(panel.timeline, Qt.Key.Key_BracketLeft, Qt.KeyboardModifier.ControlModifier)
    assert ("corrector-2/correction", 32) in panel._markers
    assert ("corrector-1/correction", 32) not in panel._markers


def test_cancel_marquee_and_ignore_blank_menu_for_edits(qtbot: QtBot, monkeypatch: MonkeyPatch) -> None:
    panel = _open(qtbot)
    existing = ("corrector-1/correction", 58)
    qtbot.mouseClick(panel.timeline, LEFT, pos=_point(panel, "corrector-1", 58))
    assert panel.selected == {existing}
    qtbot.mousePress(panel.timeline, LEFT, pos=_point(panel, "corrector-1", 20))
    qtbot.mouseMove(panel.timeline, _point(panel, "sizing", 110))
    assert len(panel.selected) > 1
    qtbot.keyClick(panel.timeline, Qt.Key.Key_Escape)
    qtbot.mouseRelease(panel.timeline, LEFT, pos=_point(panel, "sizing", 110))
    assert panel.selected == {existing}

    menu_labels: list[str] = []

    def inspect(menu: QMenu, _: QPoint) -> None:
        menu_labels.extend(action.text() for action in menu.actions())

    monkeypatch.setattr(QMenu, "exec", inspect)
    panel.timeline.contextMenuEvent(QContextMenuEvent(
        QContextMenuEvent.Reason.Mouse, QPoint(panel.timeline.frame_x(50), 200),
    ))
    assert len(menu_labels) == 2  # Zoom commands only.


def test_dissolve_dialog_commits_only_on_accept(qtbot: QtBot) -> None:
    panel = _open(qtbot)
    ref = ("corrector-1/correction", 58)

    def close_dialog(accept: bool) -> None:
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, QDialog)
        start, end = dialog.findChildren(QSlider)
        start.setValue(20)
        end.setValue(30)
        dialog.accept() if accept else dialog.reject()

    QTimer.singleShot(0, lambda: close_dialog(False))
    panel.edit_dynamics((ref,))
    assert panel._markers[ref].start == 1
    assert panel._markers[ref].end == 1

    QTimer.singleShot(0, lambda: close_dialog(True))
    panel.edit_dynamics((ref,))
    assert panel._markers[ref].start == 2
    assert panel._markers[ref].end == 3


def test_context_menu_nudges_selected_keys_and_master_can_clear_visible_keys(
        qtbot: QtBot, monkeypatch: MonkeyPatch) -> None:
    panel = _open(qtbot)
    timeline = panel.timeline
    label = tr("move_right", "Move Selected Keyframes Right")

    def choose(menu: QMenu, _: QPoint) -> None:
        action = next(action for action in menu.actions() if action.text() == label)
        assert action.isEnabled()
        action.trigger()

    monkeypatch.setattr(QMenu, "exec", choose)
    timeline.contextMenuEvent(QContextMenuEvent(
        QContextMenuEvent.Reason.Mouse, _point(panel, "corrector-1", 58)))
    assert ("corrector-1/correction", 59) in panel._markers
    assert ("corrector-1/correction", 58) not in panel._markers

    label = tr("delete_visible", "Delete All Visible Keyframes")
    timeline.contextMenuEvent(QContextMenuEvent(
        QContextMenuEvent.Reason.Mouse, _point(panel, "", 80)))
    assert not panel._markers


def test_timecode_uses_rational_drop_frame(qtbot: QtBot) -> None:
    """R1：29.97 家族按 DF 时间码显示（Timecode 的 Rational 接口）。"""
    from src.shared.media import FPS_29_97, Rational
    panel = _open(qtbot)
    snapshot = demo_snapshot()
    snapshot = replace(snapshot, timecode_fps=FPS_29_97)
    panel.set_snapshot(snapshot)
    assert panel.timeline._timecode(1800) == "00:01:00;02"  # DF：跳 0/1 帧
    panel.set_snapshot(replace(snapshot, timecode_fps=Rational(24, 1)))
    assert panel.timeline._timecode(1800) == "00:01:15:00"  # NDF 整帧率
    # 旧载荷的 int 帧率仍按 NDF divmod（回退路径）
    panel.set_snapshot(replace(snapshot, timecode_fps=25))
    assert panel.timeline._timecode(1800) == "00:01:12:00"
