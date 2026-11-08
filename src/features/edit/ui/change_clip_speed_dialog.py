"""Resolve-style clip-speed controls; this dialog never changes timeline data."""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QSignalBlocker, Qt
from PyQt6.QtWidgets import (
    QButtonGroup, QCheckBox, QDialog, QDoubleSpinBox, QFormLayout, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QRadioButton, QVBoxLayout,
    QWidget,
)

from src.features.edit.core.timeline_v2 import MAX_SPEED, MIN_SPEED
from src.features.edit.ui.edit_style import EDIT_COLORS
from src.shared.i18n.i18n import I18N


class ChangeClipSpeedDialog(QDialog):
    """Edit speed and related options as a local, cancelable settings draft.

    The caller owns capability checks and the command transaction. In
    particular, enabling Ripple or Freeze here does not simulate an edit.
    """

    def __init__(
        self, *, speed: float, source_fps: float, timeline_fps: float,
        duration_frames: int, reverse: bool = False,
        preserve_pitch: bool = False, freeze: bool = False, parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if not all(math.isfinite(value) and value > 0.0
                   for value in (speed, source_fps, timeline_fps)):
            raise ValueError("Speed and frame rates must be positive finite numbers")
        if duration_frames < 1:
            raise ValueError("Clip duration must contain at least one frame")
        self._source_fps = float(source_fps)
        self._timeline_fps = float(timeline_fps)
        self._nominal_fps = max(1, round(timeline_fps))
        self._speed = max(MIN_SPEED, min(MAX_SPEED, float(speed)))
        self._duration_frames = int(duration_frames)
        self._content_span = self._duration_frames * self._speed
        self._updating = False
        self.setObjectName("changeClipSpeedDialog")
        self.setWindowTitle(I18N.tr("edit.speed_dialog.title", "Change Clip Speed"))
        self.setModal(True)
        self.setFixedWidth(470)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self._build_ui(reverse, preserve_pitch)
        self._sync_fields()
        self.freeze.setChecked(freeze)

    def _build_ui(self, reverse: bool, preserve_pitch: bool) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(12)
        content = QFrame(self)
        content.setObjectName("speedSettingsFrame")
        columns = QHBoxLayout(content)
        columns.setContentsMargins(18, 15, 18, 15)
        columns.setSpacing(22)
        numbers = QFormLayout()
        numbers.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        numbers.setHorizontalSpacing(10)
        numbers.setVerticalSpacing(10)
        self.speed_percent = self._spin("clipSpeedPercent", 2, MIN_SPEED * 100.0, MAX_SPEED * 100.0)
        self.speed_percent.setSuffix(" %")
        self.speed_percent.setSingleStep(1.0)
        self.speed_percent.setAccessibleName(I18N.tr("edit.speed_dialog.speed", "Speed"))
        self.frames_per_second = self._spin("clipSpeedFps", 3, self._source_fps * MIN_SPEED, self._source_fps * MAX_SPEED)
        self.frames_per_second.setSingleStep(1.0)
        self.frames_per_second.setAccessibleName(I18N.tr("edit.speed_dialog.fps", "Frames per Second"))
        self.duration = QLineEdit(self)
        self.duration.setObjectName("clipSpeedDuration")
        self.duration.setAccessibleName(I18N.tr("edit.speed_dialog.duration", "Duration"))
        self.duration.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.duration.setFixedWidth(112)
        self.duration.setMaxLength(20)
        numbers.addRow(I18N.tr("edit.speed_dialog.speed", "Speed"), self.speed_percent)
        numbers.addRow(I18N.tr("edit.speed_dialog.fps", "Frames per Second"), self.frames_per_second)
        numbers.addRow(I18N.tr("edit.speed_dialog.duration", "Duration"), self.duration)
        columns.addLayout(numbers)
        switches = QVBoxLayout()
        switches.setSpacing(10)
        self.ripple = self._check("clipSpeedRipple", "edit.speed_dialog.ripple", "Ripple Timeline")
        self.reverse = self._check("clipSpeedReverse", "edit.speed_dialog.reverse", "Reverse Speed")
        self.freeze = self._check("clipSpeedFreeze", "edit.speed_dialog.freeze", "Freeze Frame")
        self.pitch_correction = self._check("clipSpeedPitch", "edit.speed_dialog.pitch", "Pitch Correction")
        self.reverse.setChecked(reverse)
        self.pitch_correction.setChecked(preserve_pitch)
        for widget in (self.ripple, self.reverse, self.freeze, self.pitch_correction):
            switches.addWidget(widget)
        switches.addStretch(1)
        columns.addLayout(switches)
        root.addWidget(content)

        timing_frame = QFrame(self)
        timing_frame.setObjectName("speedTimingFrame")
        timing = QHBoxLayout(timing_frame)
        timing.setContentsMargins(18, 14, 18, 14)
        timing.addWidget(QLabel(I18N.tr("edit.speed_dialog.keyframes", "Keyframes")))
        self.maintain_timing = QRadioButton(I18N.tr("edit.speed_dialog.maintain", "Maintain Timing"), self)
        self.maintain_timing.setObjectName("clipSpeedMaintainTiming")
        self.stretch_to_fit = QRadioButton(I18N.tr("edit.speed_dialog.stretch", "Stretch to Fit"), self)
        self.stretch_to_fit.setObjectName("clipSpeedStretchToFit")
        self.maintain_timing.setAccessibleName(self.maintain_timing.text())
        self.stretch_to_fit.setAccessibleName(self.stretch_to_fit.text())
        self._timing_group = QButtonGroup(self)
        for widget in (self.maintain_timing, self.stretch_to_fit):
            self._timing_group.addButton(widget)
            timing.addWidget(widget)
        self.maintain_timing.setChecked(True)
        root.addWidget(timing_frame)
        self.validation_message = QLabel(self)
        self.validation_message.setObjectName("clipSpeedValidation")
        self.validation_message.setWordWrap(True)
        self.validation_message.hide()
        root.addWidget(self.validation_message)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton(I18N.tr("common.cancel", "Cancel"), self)
        self.cancel_button.setObjectName("clipSpeedCancel")
        self.change_button = QPushButton(I18N.tr("edit.speed_dialog.change", "Change"), self)
        self.change_button.setObjectName("clipSpeedChange")
        for widget in (self.cancel_button, self.change_button):
            widget.setAccessibleName(widget.text())
            widget.setFixedWidth(110)
            buttons.addWidget(widget)
        self.change_button.setDefault(True)
        self.change_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)
        root.addLayout(buttons)
        self.speed_percent.valueChanged.connect(self._speed_changed)
        self.frames_per_second.valueChanged.connect(self._fps_changed)
        self.duration.editingFinished.connect(self._duration_changed)
        self.freeze.toggled.connect(self._freeze_toggled)
        self.setTabOrder(self.speed_percent, self.frames_per_second)
        self.setTabOrder(self.frames_per_second, self.duration)
        self.setTabOrder(self.duration, self.ripple)
        checkmark = (Path(__file__).resolve().parents[3] / "shared/assets/icons/checkmark.svg").as_posix()
        self.setStyleSheet(f"""
            QDialog {{ background: {EDIT_COLORS['bg_panel']}; color: {EDIT_COLORS['text']}; }}
            QLabel, QCheckBox, QRadioButton {{ color: {EDIT_COLORS['text_muted']}; background: transparent; }}
            QCheckBox, QRadioButton {{ spacing: 6px; }}
            QCheckBox::indicator, QRadioButton::indicator {{ width: 12px; height: 12px;
                border: 1px solid {EDIT_COLORS['text_muted']}; background: {EDIT_COLORS['bg_input']}; }}
            QCheckBox::indicator {{ border-radius: 2px; }}
            QCheckBox::indicator:checked {{ background: {EDIT_COLORS['accent']};
                border-color: {EDIT_COLORS['accent']}; image: url("{checkmark}"); }}
            QRadioButton::indicator {{ border-radius: 7px; }}
            QRadioButton::indicator:checked {{ background: {EDIT_COLORS['text']};
                border: 3px solid {EDIT_COLORS['bg_input']}; }}
            QFrame#speedSettingsFrame, QFrame#speedTimingFrame {{ border: 1px solid {EDIT_COLORS['border']}; border-radius: 3px; }}
            QLineEdit, QDoubleSpinBox {{ background: {EDIT_COLORS['bg_input']}; color: {EDIT_COLORS['text']}; border: 1px solid {EDIT_COLORS['border']}; padding: 3px; }}
            QLineEdit:focus, QDoubleSpinBox:focus {{ border-color: {EDIT_COLORS['accent']}; }}
            QLineEdit:disabled, QDoubleSpinBox:disabled, QCheckBox:disabled {{ color: {EDIT_COLORS['text_muted']}; }}
            QPushButton {{ color: {EDIT_COLORS['text']}; background: transparent; border: 1px solid {EDIT_COLORS['border']}; border-radius: 11px; padding: 4px 14px; }}
            QPushButton#clipSpeedChange {{ border-color: {EDIT_COLORS['accent']}; }}
            QPushButton:hover {{ background: {EDIT_COLORS['bg_panel_alt']}; }}
            QLabel#clipSpeedValidation {{ color: {EDIT_COLORS['accent']}; }}
        """)

    def _spin(self, name: str, decimals: int, minimum: float, maximum: float) -> QDoubleSpinBox:
        widget = QDoubleSpinBox(self)
        widget.setObjectName(name)
        widget.setDecimals(decimals)
        widget.setRange(minimum, maximum)
        widget.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
        widget.setKeyboardTracking(False)
        widget.setFixedWidth(112)
        return widget

    def _check(self, name: str, key: str, fallback: str) -> QCheckBox:
        widget = QCheckBox(I18N.tr(key, fallback), self)
        widget.setObjectName(name)
        widget.setAccessibleName(widget.text())
        return widget

    def _format_duration(self, frames: int) -> str:
        seconds, frame = divmod(frames, self._nominal_fps)
        minutes, second = divmod(seconds, 60)
        hour, minute = divmod(minutes, 60)
        return f"{hour:02d}:{minute:02d}:{second:02d}:{frame:02d}"

    def _parse_duration(self, text: str) -> int | None:
        match = re.fullmatch(r"(\d{2,}):(\d{2}):(\d{2}):(\d{2,3})", text.strip())
        if match is None:
            return None
        hour, minute, second, frame = (int(part) for part in match.groups())
        if minute > 59 or second > 59 or frame >= self._nominal_fps:
            return None
        frames = ((hour * 60 + minute) * 60 + second) * self._nominal_fps + frame
        return frames if frames > 0 else None

    def _sync_fields(self) -> None:
        self._updating = True
        try:
            blockers = [QSignalBlocker(widget) for widget in (self.speed_percent, self.frames_per_second, self.duration)]
            self.speed_percent.setValue(self._speed * 100.0)
            self.frames_per_second.setValue(self._source_fps * self._speed)
            self.duration.setText(self._format_duration(self._duration_frames))
            del blockers
        finally:
            self._updating = False

    def _set_speed(self, speed: float) -> None:
        self._speed = max(MIN_SPEED, min(MAX_SPEED, speed))
        self._duration_frames = max(1, round(self._content_span / self._speed))
        self.validation_message.hide()
        self._sync_fields()

    def _speed_changed(self, percent: float) -> None:
        if not self._updating:
            self._set_speed(percent / 100.0)

    def _fps_changed(self, fps: float) -> None:
        if not self._updating:
            self._set_speed(fps / self._source_fps)

    def _duration_changed(self) -> bool:
        if self._updating or self.freeze.isChecked():
            return True
        frames = self._parse_duration(self.duration.text())
        speed = self._content_span / frames if frames is not None else None
        if speed is None or not MIN_SPEED <= speed <= MAX_SPEED:
            self.validation_message.setText(I18N.tr(
                "edit.speed_dialog.invalid_duration",
                "Enter a valid HH:MM:SS:FF duration within the supported speed range.",
            ))
            self.validation_message.show()
            self._sync_fields()
            return False
        self._speed, self._duration_frames = speed, frames
        self.validation_message.hide()
        self._sync_fields()
        return True

    def _freeze_toggled(self, frozen: bool) -> None:
        for widget in (self.speed_percent, self.frames_per_second, self.duration, self.reverse, self.pitch_correction):
            widget.setEnabled(not frozen)

    def accept(self) -> None:
        """Commit pending timecode text into the draft before returning it."""
        if not self._duration_changed():
            self.duration.setFocus()
            return
        super().accept()

    def settings(self) -> dict[str, Any]:
        """Return a fresh settings payload; the dialog never holds a clip/model."""
        return {
            "speed": self._speed,
            "reverse": self.reverse.isChecked(),
            "freeze": self.freeze.isChecked(),
            "preserve_pitch": self.pitch_correction.isChecked(),
            "ripple": self.ripple.isChecked(),
            "keyframe_timing": "stretch" if self.stretch_to_fit.isChecked() else "maintain",
            "duration_frames": self._duration_frames,
        }
