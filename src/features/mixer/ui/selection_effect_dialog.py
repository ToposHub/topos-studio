"""Controls for the Mixer effects that render into a clip's processing stack."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from PyQt6.QtCore import QEvent, Qt, pyqtSignal
from PyQt6.QtGui import QHideEvent
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QTabWidget, QVBoxLayout, QWidget,
)

from src.features.mixer.effects.catalog import get_effect
from src.features.mixer.effects.panel_style import style_panel_inputs
from src.shared.i18n.i18n import I18N
from src.shared.ui.audio_mixer.pro_widgets import apply_float_panel
from src.shared.ui.widgets.chevron_combo import ChevronComboBox


SELECTION_EFFECTS: dict[str, tuple[str, ...]] = {
    "declick_dehum": ("de_click", "de_hum"),
    "pitch_correction": ("pitch_correction",),
    "doubler": ("doubler",),
}
_NOTES = ("C", "C♯", "D", "D♯", "E", "F", "F♯", "G", "G♯", "A", "A♯", "B")
_SCALES = (
    ("major", "Major", (0, 2, 4, 5, 7, 9, 11)),
    ("minor", "Natural minor", (0, 2, 3, 5, 7, 8, 10)),
    ("harmonic_minor", "Harmonic minor", (0, 2, 3, 5, 7, 8, 11)),
    ("chromatic", "Chromatic", tuple(range(12))),
)


def _tr(key: str, default: str) -> str:
    return I18N.tr(f"mixer.selection_effect.{key}", default)


def selection_effect_name(effect_id: str) -> str:
    descriptor = get_effect(effect_id)
    return I18N.tr(f"mixer.effect_panel.name.{effect_id}", descriptor.name_en)


@dataclass(frozen=True)
class VoiceControls:
    enabled: QCheckBox
    interval: QDoubleSpinBox
    detune: QDoubleSpinBox
    delay: QDoubleSpinBox
    pan: QDoubleSpinBox
    gain: QDoubleSpinBox

    def params(self, index: int) -> dict[str, Any]:
        return {
            "voice_id": f"voice-{index + 1}",
            "enabled": self.enabled.isChecked(),
            "interval_st": self.interval.value(),
            "detune_cents": self.detune.value(),
            "delay_ms": self.delay.value(),
            "pan_pct": self.pan.value(),
            "gain_db": self.gain.value(),
        }


class SelectionEffectDialog(QDialog):
    """Display production parameters only and request explicit audition or Apply."""

    requested = pyqtSignal(str, object)
    draft_changed = pyqtSignal()
    dismissed = pyqtSignal()
    stop_requested = pyqtSignal()

    def __init__(self, effect_id: str, parent: QWidget) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        apply_float_panel(self)
        self.effect_id = effect_id
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle(selection_effect_name(effect_id))
        self._available = False
        self._busy = False
        self._audition_active = False
        self.setMinimumWidth(380)
        layout = QVBoxLayout(self)
        self.target = QLabel(self)
        self.target.setTextFormat(Qt.TextFormat.PlainText)
        self.target.setWordWrap(True)
        layout.addWidget(self.target)
        self.status = QLabel(self)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.hide()
        if effect_id == "declick_dehum":
            self._build_cleanup(layout)
        elif effect_id == "pitch_correction":
            self._build_pitch(layout)
        elif effect_id == "doubler":
            self._build_doubler(layout)
        else:
            raise ValueError(f"Unknown selection effect {effect_id!r}")

        layout.addWidget(self.status)
        actions = QHBoxLayout()
        self.preview = self._button("preview", "Audition", actions)
        self.delta = self._button("delta", "Difference", actions)
        self.stop = self._button("stop", "Stop", actions)
        actions.addStretch()
        self.apply = self._button("apply", "Apply", actions)
        close = self._button("close", "Close", actions)
        layout.addLayout(actions)
        for button, mode in ((self.preview, "preview"), (self.delta, "delta"), (self.apply, "apply")):
            button.clicked.connect(lambda _checked=False, action=mode: self._request(action))
        self.stop.clicked.connect(self.stop_requested.emit)
        close.clicked.connect(self.close)
        self._update_buttons()
        style_panel_inputs(self)

    def _button(self, key: str, default: str, row: QHBoxLayout) -> QPushButton:
        button = QPushButton(_tr(key, default), self)
        button.setObjectName(f"SelectionEffect_{key}")
        button.setAutoDefault(False)
        row.addWidget(button)
        return button

    def _check(self, key: str, default: str, checked: bool = False) -> QCheckBox:
        check = QCheckBox(_tr(key, default), self)
        check.setObjectName(f"SelectionEffect_{key}")
        check.setChecked(checked)
        check.toggled.connect(self._changed)
        return check

    def _number(
        self, form: QFormLayout, key: str, label: str, value: float, low: float,
        high: float, suffix: str = "", *, decimals: int = 1,
    ) -> QDoubleSpinBox:
        control = QDoubleSpinBox(self)
        control.setObjectName(f"SelectionEffect_{key}")
        control.setDecimals(decimals)
        control.setRange(low, high)
        control.setSuffix(suffix)
        control.setValue(value)
        control.setKeyboardTracking(False)
        control.valueChanged.connect(self._changed)
        control.lineEdit().textEdited.connect(self._changed)
        form.addRow(_tr(key, label), control)
        return control

    def _build_cleanup(self, layout: QVBoxLayout) -> None:
        self.click_enabled = self._check("de_click", "De-click", True)
        self.hum_enabled = self._check("de_hum", "De-hum")
        for check in (self.click_enabled, self.hum_enabled):
            layout.addWidget(check)
            box = QGroupBox(self)
            form = QFormLayout(box)
            box.setEnabled(check.isChecked())
            check.toggled.connect(box.setEnabled)
            layout.addWidget(box)
            if check is self.click_enabled:
                self.threshold = self._number(form, "threshold", "Click threshold (σ)", 8, 2, 30)
                self.click_width = self._number(form, "click_width", "Max click width", 2.5, .1, 25, " ms")
            else:
                self.hum_base = ChevronComboBox(self)
                self.hum_base.addItem(_tr("automatic", "Automatic"), "auto")
                self.hum_base.addItem("50 Hz", "50")
                self.hum_base.addItem("60 Hz", "60")
                self.hum_base.currentIndexChanged.connect(self._changed)
                form.addRow(_tr("hum_base", "Hum frequency"), self.hum_base)
                self.harmonics = QLineEdit("1, 2, 3, 4, 5, 6, 7, 8", self)
                self.harmonics.setObjectName("SelectionEffect_harmonics")
                self.harmonics.setToolTip(_tr("harmonics_help", "Enter harmonic numbers from 1 to 16, separated by commas."))
                self.harmonics.textChanged.connect(self._changed)
                form.addRow(_tr("harmonics", "Harmonics"), self.harmonics)
                self.hum_q = self._number(form, "hum_q", "Filter Q", 30, 1, 1000)
                self.hum_drift = self._number(form, "hum_drift", "Frequency drift", .6, 0, 5, " Hz")
                self.reduction = self._number(form, "reduction", "Hum reduction", 48, 0, 48, " dB")

    def _build_pitch(self, layout: QVBoxLayout) -> None:
        form = QFormLayout()
        self.key = ChevronComboBox(self)
        self.key.addItems(_NOTES)
        self.scale = ChevronComboBox(self)
        for key, label, notes in _SCALES:
            self.scale.addItem(_tr(key, label), notes)
        self.scale.addItem(_tr("custom", "Custom"), None)
        self.key.currentIndexChanged.connect(self._load_scale)
        self.scale.currentIndexChanged.connect(self._load_scale)
        form.addRow(_tr("key", "Key"), self.key)
        form.addRow(_tr("scale", "Scale"), self.scale)
        self.a4 = self._number(form, "a4", "Reference A4", 440, 400, 480, " Hz")
        self.strength = self._number(form, "strength", "Correction strength", 100, 0, 100, " %")
        self.retune = self._number(form, "retune", "Retune time", 45, 0, 400, " ms")
        self.note_checks = self._note_checks(form, "target_notes", "Target notes", self._make_custom)
        self.bypass_checks = self._note_checks(form, "bypass_notes", "Bypass notes", self._changed)
        self._load_scale()
        layout.addLayout(form)

    def _note_checks(
        self, form: QFormLayout, key: str, label: str, changed: Callable[[bool], None],
    ) -> tuple[QCheckBox, ...]:
        note_row = QHBoxLayout()
        note_row.setSpacing(5)
        checks = []
        for note in _NOTES:
            check = QCheckBox(note, self)
            check.setAccessibleName(f"{_tr(key, label)} {note}")
            check.toggled.connect(changed)
            note_row.addWidget(check)
            checks.append(check)
        form.addRow(_tr(key, label), note_row)
        return tuple(checks)

    def _load_scale(self, _index: int = 0) -> None:
        offsets = self.scale.currentData()
        self.key.setEnabled(offsets is not None)
        if offsets is not None:
            notes = {(offset + self.key.currentIndex()) % 12 for offset in offsets}
            for index, check in enumerate(self.note_checks):
                blocked = check.blockSignals(True)
                check.setChecked(index in notes)
                check.blockSignals(blocked)
        self._changed()

    def _make_custom(self, _checked: bool) -> None:
        if self.scale.currentData() is not None:
            self.scale.setCurrentIndex(self.scale.count() - 1)
        else:
            self._changed()

    def _build_doubler(self, layout: QVBoxLayout) -> None:
        form = QFormLayout()
        self.dry = self._number(form, "dry_gain", "Dry gain", 0, -120, 6, " dB")
        self.wet = self._number(form, "wet_level", "Voice mix", 100, 0, 100, " %")
        self.output = self._number(form, "output_gain", "Output gain", 0, -24, 24, " dB")
        layout.addLayout(form)
        tabs = QTabWidget(self)
        self.voices: list[VoiceControls] = []
        for index, (detune, delay, pan) in enumerate(((-6, 12, -100), (6, 19, 100), (0, 24, 0), (0, 32, 0))):
            voice = QWidget(tabs)
            rows = QFormLayout(voice)
            enabled = self._check("voice_enabled", "Enable voice", index < 2)
            rows.addRow(enabled)
            self.voices.append(VoiceControls(
                enabled,
                self._number(rows, "interval", "Interval", 0, -24, 24, " st", decimals=0),
                self._number(rows, "detune", "Detune", detune, -100, 100, " ct"),
                self._number(rows, "delay", "Delay", delay, 0, 100, " ms"),
                self._number(rows, "pan", "Pan", pan, -100, 100, " %"),
                self._number(rows, "voice_gain", "Voice gain", 0, -60, 6, " dB"),
            ))
            tabs.addTab(voice, _tr("voice", "Voice {number}").format(number=index + 1))
        layout.addWidget(tabs)

    def operations(self) -> tuple[tuple[str, dict[str, Any]], ...]:
        if self.effect_id == "declick_dehum":
            operations = []
            if self.click_enabled.isChecked():
                operations.append(("de_click", {
                    "click_threshold_sigma": self.threshold.value(),
                    "click_max_width_ms": self.click_width.value(),
                }))
            if self.hum_enabled.isChecked():
                fields = re.split(r"[,，\s]+", self.harmonics.text().strip())
                if any(re.fullmatch(r"(?:[1-9]|1[0-6])", field) is None for field in fields):
                    raise ValueError(self.harmonics.toolTip())
                operations.append(("de_hum", {
                    "hum_base_mode": self.hum_base.currentData(),
                    "hum_harmonics": sorted({int(field) for field in fields}),
                    "hum_q": self.hum_q.value(),
                    "hum_drift_hz": self.hum_drift.value(),
                    "hum_reduction_db": self.reduction.value(),
                }))
            if not operations:
                raise ValueError(_tr("choose_module", "Enable at least one repair module."))
            return tuple(operations)
        if self.effect_id == "pitch_correction":
            notes = [i for i, check in enumerate(self.note_checks) if check.isChecked()]
            if not notes:
                raise ValueError(_tr("choose_notes", "Choose at least one target note."))
            return (("pitch_correction", {
                "scale_notes": notes,
                "bypass_notes": [note.replace("♯", "#") for note, check in zip(_NOTES, self.bypass_checks)
                                 if check.isChecked()],
                "ref_a4_hz": self.a4.value(), "strength": self.strength.value(),
                "retune_ms": self.retune.value(),
            }),)
        voices = [voice.params(index) for index, voice in enumerate(self.voices)]
        if any(voice["enabled"] and abs(voice["interval_st"] + voice["detune_cents"] / 100) > 24
               for voice in voices):
            raise ValueError(_tr("voice_range", "Each enabled voice must stay within ±24 semitones, including detune."))
        # Keep active voices in their original order; disabled voices have no DSP contribution.
        active = [voice for voice in voices if voice["enabled"]]
        if not active:
            active = [{**voices[0], "interval_st": 0.0, "detune_cents": 0.0}]
        return (("doubler", {
            "dry_gain_db": self.dry.value(), "wet_mix_pct": self.wet.value(),
            "output_gain_db": self.output.value(), "voices": active,
        }),)

    def set_target(self, label: str, available: bool, *, stereo: bool = False) -> None:
        self.target.setText(label)
        self._available = available
        if self.effect_id == "doubler":
            for voice in self.voices:
                voice.pan.setEnabled(stereo)
                voice.pan.setToolTip("" if stereo else _tr("pan_stereo", "Pan requires both stereo channels."))
        self._update_buttons()

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._update_buttons()

    def set_audition_active(self, active: bool) -> None:
        self._audition_active = active
        self._update_buttons()

    def set_status(self, text: str) -> None:
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def _update_buttons(self) -> None:
        for button in (self.preview, self.delta, self.apply):
            button.setEnabled(self._available and not self._busy)
        self.stop.setEnabled(self._busy or self._audition_active)

    def _changed(self, _value: object = None) -> None:
        self.set_status("")
        self.draft_changed.emit()

    def _request(self, mode: str) -> None:
        try:
            operations = self.operations()
        except ValueError as exc:
            self.set_status(str(exc))
            return
        self.requested.emit(mode, operations)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange and self.isMinimized():
            self.dismissed.emit()

    def hideEvent(self, event: QHideEvent) -> None:
        self.dismissed.emit()
        super().hideEvent(event)
