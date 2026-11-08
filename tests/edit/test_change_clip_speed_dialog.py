"""Behavioral checks for the standalone, model-free speed-edit draft."""
from __future__ import annotations

from typing import Any

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QDialog

from src.features.edit.ui.change_clip_speed_dialog import ChangeClipSpeedDialog

pytestmark = pytest.mark.edit_ui


def _dialog(qtbot: Any, **kwargs: Any) -> ChangeClipSpeedDialog:
    widget = ChangeClipSpeedDialog(
        speed=kwargs.pop("speed", 1.0), source_fps=kwargs.pop("source_fps", 25.0),
        timeline_fps=kwargs.pop("timeline_fps", 25.0), duration_frames=kwargs.pop("duration_frames", 100),
        **kwargs,
    )
    qtbot.addWidget(widget)
    widget.show()
    widget.activateWindow()
    QTest.qWait(5)
    return widget


def test_speed_fps_and_timecode_edit_the_same_content_span(qtbot: Any) -> None:
    dialog = _dialog(qtbot)
    dialog.speed_percent.setValue(200.0)
    assert dialog.frames_per_second.value() == 50.0
    assert dialog.duration.text() == "00:00:02:00"
    assert dialog.settings()["duration_frames"] == 50
    dialog.frames_per_second.setValue(12.5)
    assert dialog.speed_percent.value() == 50.0
    assert dialog.duration.text() == "00:00:08:00"
    QTest.mouseClick(dialog.duration, Qt.MouseButton.LeftButton)
    dialog.duration.selectAll()
    QTest.keyClicks(dialog.duration, "00:00:05:00")
    QTest.keyClick(dialog.duration, Qt.Key.Key_Tab)
    assert dialog.settings()["speed"] == pytest.approx(0.8)
    assert dialog.frames_per_second.value() == 20.0
    assert dialog.settings()["duration_frames"] == 125


def test_mixed_fps_duration_uses_timeline_timebase(qtbot: Any) -> None:
    dialog = _dialog(qtbot, source_fps=50.0, timeline_fps=24.0, duration_frames=96)
    dialog.duration.setText("00:00:02:00")
    dialog.duration.editingFinished.emit()
    assert dialog.settings()["speed"] == 2.0
    assert dialog.frames_per_second.value() == 100.0
    assert dialog.settings()["duration_frames"] == 48


@pytest.mark.parametrize("text", ["garbage", "00:00:00:00", "00:65:00:00", "00:00:04:25", "00:00:00:01"])
def test_invalid_duration_reverts_without_changing_the_draft(qtbot: Any, text: str) -> None:
    dialog = _dialog(qtbot)
    original = dialog.settings()
    dialog.duration.setText(text)
    dialog.duration.editingFinished.emit()
    assert dialog.settings() == original
    assert dialog.duration.text() == "00:00:04:00"
    assert not dialog.validation_message.isHidden()


def test_freeze_preserves_numeric_draft_and_restores_editability(qtbot: Any) -> None:
    dialog = _dialog(qtbot, speed=1.25, duration_frames=80)
    dialog.reverse.setChecked(True)
    before = dialog.settings()
    QTest.mouseClick(dialog.freeze, Qt.MouseButton.LeftButton)
    assert dialog.settings()["freeze"]
    assert all(not widget.isEnabled() for widget in (
        dialog.speed_percent, dialog.frames_per_second, dialog.duration, dialog.reverse, dialog.pitch_correction,
    ))
    assert dialog.settings()["speed"] == before["speed"]
    QTest.mouseClick(dialog.freeze, Qt.MouseButton.LeftButton)
    assert dialog.speed_percent.isEnabled() and dialog.reverse.isEnabled()
    assert dialog.reverse.isChecked()


def test_change_returns_all_explicit_options(qtbot: Any) -> None:
    dialog = _dialog(qtbot)
    dialog.reverse.setChecked(True)
    dialog.ripple.setChecked(True)
    dialog.pitch_correction.setChecked(True)
    dialog.stretch_to_fit.setChecked(True)
    QTest.mouseClick(dialog.change_button, Qt.MouseButton.LeftButton)
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.settings() == {
        "speed": 1.0, "reverse": True, "freeze": False, "preserve_pitch": True,
        "ripple": True, "keyframe_timing": "stretch", "duration_frames": 100,
    }


def test_cancel_rejects_without_any_accepted_result(qtbot: Any) -> None:
    dialog = _dialog(qtbot)
    accepted: list[bool] = []
    dialog.accepted.connect(lambda: accepted.append(True))
    dialog.speed_percent.setValue(150.0)
    QTest.mouseClick(dialog.cancel_button, Qt.MouseButton.LeftButton)
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert accepted == []


def test_escape_rejects_the_local_draft(qtbot: Any) -> None:
    dialog = _dialog(qtbot)
    dialog.speed_percent.setFocus()
    QTest.keyClick(dialog, Qt.Key.Key_Escape)
    assert dialog.result() == QDialog.DialogCode.Rejected


def test_existing_freeze_is_visible_and_can_be_unchecked(qtbot: Any) -> None:
    dialog = _dialog(qtbot, freeze=True)
    assert dialog.freeze.isChecked()
    assert not dialog.speed_percent.isEnabled()
    QTest.mouseClick(dialog.freeze, Qt.MouseButton.LeftButton)
    assert not dialog.settings()["freeze"]
    assert dialog.speed_percent.isEnabled()


def test_invalid_timecode_keeps_dialog_open_on_accept(qtbot: Any) -> None:
    dialog = _dialog(qtbot)
    accepted: list[bool] = []
    dialog.accepted.connect(lambda: accepted.append(True))
    dialog.duration.setText("bad timecode")
    dialog.accept()
    assert accepted == []
    assert dialog.isVisible()
    assert not dialog.validation_message.isHidden()
