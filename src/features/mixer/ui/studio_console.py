"""Studio presentation for the shared Edit Audio Mixer."""
from __future__ import annotations

import math
from collections.abc import Collection, Mapping
from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QMenu, QSizePolicy, QToolButton, QWidget

from src.shared.i18n.i18n import I18N
from src.shared.ui.audio_mixer import AudioMixerPanel
from src.shared.ui.audio_mixer.audio_level_meter import MeterMode
from src.shared.ui.audio_mixer.track_mixer_widget import (
    VOLUME_DB_MAX,
    VOLUME_DB_MIN,
    TrackMixerWidget,
    linear_to_db,
)
from src.shared.ui.style_sheet import C

from .console_controls import ACCENT_CYAN, EditableValuePill, LevelMeter, StudioFader, format_db
from .workspace_style import compact_combo


STUDIO_STRIP_WIDTH = 96
STUDIO_COMPACT_STRIP_WIDTH = 84


class StudioChannelStrip(TrackMixerWidget):
    """Use the real insert rack and retain the Studio fader and peak meter."""

    output_bus_requested = pyqtSignal(str, object)
    recording_input_requested = pyqtSignal(str)

    def __init__(
        self, track_id: str, track_name: str, track_tag: str, parent: QWidget,
        *, is_master: bool,
    ) -> None:
        self._drag_start_db: float | None = None
        self._meter_gr = 0.0
        self._playback_levels = (VOLUME_DB_MIN, VOLUME_DB_MIN)
        self._showing_input_level = False
        super().__init__(track_id, track_name, track_tag, parent, is_master)
        for widget in (self._fx_badge, self._dy_badge, self._eq_badge, self._meter_section, self._gain_section):
            widget.hide()

        self._recording_input = ""
        self._recording_locked = False
        self._recording_bus = False
        self._recording_busy = False
        self._recording_workflow_active = False
        self._legacy_arm_enabled = not is_master and self._monitor_btn.isEnabledTo(self)
        self._legacy_arm_tooltip = "" if is_master else self._monitor_btn.toolTip()
        self._recording_input_row = QWidget(self)
        input_row = QHBoxLayout(self._recording_input_row)
        input_row.setContentsMargins(4, 2, 4, 2)
        self._recording_input_row.setFixedHeight(24)
        self.recording_input_button = QToolButton(self._recording_input_row)
        self.recording_input_button.setObjectName("mixerRecordingInput")
        self.recording_input_button.setAccessibleName(I18N.tr("mixer.record.input", "Recording input"))
        self.recording_input_button.setFixedHeight(20)
        self.recording_input_button.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.recording_input_button.setStyleSheet(f"""
            QToolButton {{ background: {C.BG_INPUT}; color: {C.TEXT_SECONDARY};
                border: 1px solid {C.BORDER}; border-radius: 3px;
                font-size: 10px; padding: 1px 3px; }}
            QToolButton:hover, QToolButton:focus {{ border-color: {C.BORDER_HOVER}; color: {C.TEXT_PRIMARY}; }}
            QToolButton:disabled {{ color: {C.TEXT_HINT}; border-color: {C.BORDER_DARK}; }}
        """)
        self.recording_input_button.clicked.connect(
            lambda: self.recording_input_requested.emit(self._track_id))
        input_row.addWidget(self.recording_input_button)
        self.layout().insertWidget(1, self._recording_input_row)

        self._studio_readout = QWidget(self)
        readout = QHBoxLayout(self._studio_readout)
        readout.setContentsMargins(4, 2, 4, 0)
        readout.setSpacing(3)
        self.studio_volume = EditableValuePill(height=19)
        self.studio_volume.setToolTip(I18N.tr("audio.mixer.gain_tip", "Channel gain (dB)"))
        self.studio_volume.valueEdited.connect(self._edit_studio_volume)
        readout.addWidget(self.studio_volume, 1)
        self.studio_peak = QLabel("— dBFS")
        self.studio_peak.setToolTip(I18N.tr("audio.mixer.peak", "Peak level (dBFS)"))
        self.studio_peak.setStyleSheet("font: 9px 'Menlo'; color: #9B9DA6;")
        readout.addWidget(self.studio_peak)

        self._studio_instruments = QWidget(self)
        instruments = QHBoxLayout(self._studio_instruments)
        instruments.setContentsMargins(7, 0, 7, 6)
        instruments.setSpacing(5)
        self.studio_fader = StudioFader(max_db=VOLUME_DB_MAX)
        self.studio_fader.setAccessibleName(I18N.tr("audio.mixer.gain", "Channel gain"))
        self.studio_fader.setToolTip(I18N.tr("audio.mixer.gain_tip", "Channel gain (dB) · double-click the fader to reset"))
        self.studio_fader.dragStarted.connect(self._begin_studio_drag)
        self.studio_fader.dragFinished.connect(self._finish_studio_drag)
        self.studio_fader.dbChanged.connect(self._set_studio_volume)
        self.studio_meter = LevelMeter(stereo=True, show_gr=True)
        self._output_meter_tooltip = self.studio_meter.toolTip()
        self.studio_meter.setAccessibleName(self.studio_peak.toolTip())
        instruments.addStretch()
        instruments.addWidget(self.studio_fader)
        instruments.addWidget(self.studio_meter)
        instruments.addStretch()
        self.layout().addWidget(self._studio_readout)
        self.layout().addWidget(self._studio_instruments, 1)

        footer = QWidget(self)
        footer.setFixedHeight(26)
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(4, 0, 4, 4)
        self.output_bus_combo = compact_combo()
        self.output_bus_combo.setFixedHeight(22)
        self.output_bus_combo.setAccessibleName(I18N.tr("mixer.routing.output", "Output routing"))
        self.output_bus_combo.activated.connect(self._request_output_bus)
        self.output_bus_combo.setVisible(not is_master)
        footer_layout.addWidget(self.output_bus_combo)
        self.layout().addWidget(footer)
        self._output_choices: tuple[tuple[str | None, str], ...] = ()
        self._output_bus_id: str | None = None
        if not is_master:
            self.set_output_routing(None, ())
        self._sync_recording_controls()

    def set_recording_input(self, label: str) -> None:
        """Apply the host's patched input label to the track."""
        if self._recording_input != label:
            self._recording_input = label
            self._sync_recording_controls()

    def set_recording_track_kind(self, *, bus: bool, locked: bool) -> None:
        if (bus, locked) != (self._recording_bus, self._recording_locked):
            self._recording_bus = bus
            self._recording_locked = locked
            self._sync_recording_controls()

    def set_recording_arm_state(self, armed: bool, *, busy: bool) -> None:
        if self._is_armed != armed:
            self.set_armed(armed)
        self._recording_busy = busy
        self._sync_recording_controls()

    def set_recording_workflow_active(self, active: bool) -> None:
        if self._recording_workflow_active != active:
            self._recording_workflow_active = active
            self._sync_recording_controls()

    def _sync_recording_controls(self) -> None:
        self._recording_input_row.setVisible(self._recording_workflow_active)
        self._sync_recording_meter()
        if not self._recording_workflow_active:
            if not self._is_master:
                self._monitor_btn.show()
                self._monitor_btn.setEnabled(self._legacy_arm_enabled)
                self._monitor_btn.setToolTip(self._legacy_arm_tooltip)
                self._monitor_btn.setAccessibleName(self._legacy_arm_tooltip)
            return

        button = self.recording_input_button
        input_label = self._recording_input or I18N.tr("mixer.record.no_input", "No input")
        available = max(0, self.width() - 16)
        button.setText(button.fontMetrics().elidedText(input_label, Qt.TextElideMode.ElideRight, available))
        button.setToolTip(f"{button.accessibleName()} · {input_label}")
        button.setAccessibleDescription(input_label)

        recordable = not (self._is_master or self._recording_bus)
        blocked = self._recording_busy or self._recording_locked
        button.setVisible(recordable)
        button.setEnabled(recordable and not blocked)
        if self._is_master:
            return
        self._monitor_btn.setVisible(recordable)
        self._monitor_btn.setEnabled(
            self._legacy_arm_enabled and recordable and not blocked and bool(self._recording_input))
        if self._recording_locked:
            tooltip = I18N.tr("mixer.record.locked", "Unlock this track to record")
        elif self._recording_busy:
            tooltip = I18N.tr("mixer.record.busy", "Finish the current recording first")
        elif not self._recording_input:
            tooltip = I18N.tr("mixer.record.patch_first", "Assign a recording input first")
        elif self._is_armed:
            tooltip = I18N.tr("mixer.record.disarm", "Disarm track")
        else:
            tooltip = I18N.tr("mixer.record.arm", "Arm track for recording")
        self._monitor_btn.setToolTip(tooltip)
        self._monitor_btn.setAccessibleName(tooltip)

    def set_output_routing(self, output_bus_id: str | None, buses: tuple[tuple[str, str], ...]) -> None:
        master = I18N.tr("audio.mixer.master", "Master")
        choices = ((None, master),) + tuple(bus for bus in buses if bus[0] != self._track_id)
        if output_bus_id is not None and not any(bus_id == output_bus_id for bus_id, _ in choices):
            choices += ((output_bus_id, I18N.tr("mixer.routing.missing_bus", "Missing bus")),)
        combo = self.output_bus_combo
        if choices != self._output_choices:
            self._output_choices = choices
            combo.clear()
            for bus_id, name in choices:
                combo.addItem(name, bus_id)
                combo.setItemData(combo.count() - 1, name, Qt.ItemDataRole.ToolTipRole)
        self._output_bus_id = output_bus_id
        combo.setCurrentIndex(next(i for i, (bus_id, _) in enumerate(choices) if bus_id == output_bus_id))
        combo.setToolTip(I18N.tr("mixer.routing.output", "Output routing") + " · " + combo.currentText())

    def _request_output_bus(self, index: int) -> None:
        bus_id = self.output_bus_combo.itemData(index)
        if bus_id != self._output_bus_id:
            self.output_bus_requested.emit(self._track_id, bus_id)

    def _update_size_constraints(self) -> None:
        self.setFixedWidth(STUDIO_STRIP_WIDTH)

    def set_narrow(self, narrow: bool) -> None:
        super().set_narrow(narrow)
        self.setFixedWidth(STUDIO_COMPACT_STRIP_WIDTH if narrow else STUDIO_STRIP_WIDTH)
        self._sync_studio_peak()
        self._sync_recording_controls()

    def _show_eq_insert_in_rack(self) -> bool:
        # Existing sessions may contain EQ instances created by the old badge.
        return True

    def set_track_color(self, color: str | None) -> None:
        super().set_track_color(color)
        accent = QColor(color or ACCENT_CYAN)
        if self.studio_fader.accent_color != accent:
            self.studio_fader.accent_color = accent
            self.studio_fader.update()
        self.studio_volume.set_accent_color(accent.name())

    def set_volume_linear(self, vol: float) -> None:
        if self._drag_start_db is not None and linear_to_db(vol) == self._drag_start_db:
            # A queued model refresh can arrive during a fader preview.
            return
        super().set_volume_linear(vol)
        self._sync_studio_volume()

    def _sync_studio_volume(self) -> None:
        self.studio_fader.setDb(self._volume_db, emit_signal=False)
        self.studio_volume.setText(format_db(self._volume_db))

    def _edit_studio_volume(self, text: str) -> None:
        try:
            db = float(text.strip().replace("∞", "inf"))
        except ValueError:
            self._sync_studio_volume()
            return
        if math.isnan(db):
            self._sync_studio_volume()
            return
        self.studio_fader.setDb(db)
        self._sync_studio_volume()

    def _begin_studio_drag(self) -> None:
        self._drag_start_db = self._volume_db

    def _finish_studio_drag(self) -> None:
        start_db, self._drag_start_db = self._drag_start_db, None
        if start_db is not None and start_db != self._volume_db:
            self.volume_changed.emit(self._track_id, self._volume_db)

    def _set_studio_volume(self, db: float) -> None:
        self._set_volume_db(db)
        self._sync_studio_volume()
        if self._drag_start_db is None:
            self.volume_changed.emit(self._track_id, db)
        else:
            self.volume_preview.emit(self._track_id, db)

    @property
    def display_meter_mode(self) -> MeterMode:
        return MeterMode.PEAK

    def update_audio_level(self, levels: list[float]) -> None:
        left = levels[0] if levels else VOLUME_DB_MIN
        right = levels[1] if len(levels) > 1 else left
        self._playback_levels = (left, right)
        if not self._showing_input_level:
            self.studio_meter.set_levels(left, right, self._meter_gr)
            self._sync_studio_peak()

    def set_recording_input_level(self, dbfs: float) -> None:
        if self._showing_input_level:
            level = max(VOLUME_DB_MIN, min(dbfs, 0.0)) if math.isfinite(dbfs) else VOLUME_DB_MIN
            self.studio_meter.set_levels(level)
            self._sync_studio_peak()

    def _sync_recording_meter(self) -> None:
        showing_input = self._recording_workflow_active and self._is_armed and bool(self._recording_input)
        if showing_input == self._showing_input_level:
            return
        self._showing_input_level = showing_input
        label = (I18N.tr("mixer.record.input_peak", "Input peak level (dBFS)") if showing_input
                 else I18N.tr("audio.mixer.peak", "Peak level (dBFS)"))
        self.studio_peak.setToolTip(label)
        self.studio_meter.setAccessibleName(label)
        self.studio_meter.setToolTip(label if showing_input else self._output_meter_tooltip)
        self.studio_meter.clear_levels()
        if not showing_input:
            self.studio_meter.set_levels(*self._playback_levels, self._meter_gr)
        self._sync_studio_peak()

    def _sync_studio_peak(self) -> None:
        peak = max(self.studio_meter.peak_l, self.studio_meter.peak_r)
        label = f"{peak:.1f}" if peak > VOLUME_DB_MIN else "—"
        if not self._is_narrow:
            label += " dBFS"
        if label != self.studio_peak.text():
            self.studio_peak.setText(label)

    def update_track_telemetry(self, payload: dict) -> None:
        super().update_track_telemetry(payload)
        self._meter_gr = -max((payload.get("gr") or {}).values(), default=0.0)

    def reset_meter(self) -> None:
        super().reset_meter()
        self._meter_gr = 0.0
        self._playback_levels = (VOLUME_DB_MIN, VOLUME_DB_MIN)
        if not self._showing_input_level:
            self.studio_meter.clear_levels()
            self._sync_studio_peak()


class StudioAudioMixerPanel(AudioMixerPanel):
    """The same console is hosted in both Studio workspaces."""

    track_output_requested = pyqtSignal(str, object)
    track_input_requested = pyqtSignal(str)
    add_bus_requested = pyqtSignal()
    recording_settings_requested = pyqtSignal()
    adr_requested = pyqtSignal()
    output_levels_changed = pyqtSignal(list)
    output_meter_reset = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        self._recording_inputs: dict[str, str] = {}
        self._record_armed_ids: frozenset[str] = frozenset()
        self._recording_busy = False
        self._recording_workflow_active = False
        super().__init__(parent)

    def update_master_level(self, levels: list[float]) -> None:
        super().update_master_level(levels)
        self.output_levels_changed.emit(levels)

    def on_telemetry_reset(self, generation: int) -> None:
        super().on_telemetry_reset(generation)
        self.output_meter_reset.emit()

    def _create_strip(
        self, track_id: str, track_name: str, track_tag: str, *, is_master: bool = False,
    ) -> StudioChannelStrip:
        strip = StudioChannelStrip(track_id, track_name, track_tag, self, is_master=is_master)
        strip.output_bus_requested.connect(self.track_output_requested)
        strip.recording_input_requested.connect(self.track_input_requested)
        strip.set_recording_input(self._recording_inputs.get(track_id, ""))
        strip.set_recording_workflow_active(self._recording_workflow_active)
        if self._recording_workflow_active:
            strip.set_recording_arm_state(track_id in self._record_armed_ids, busy=self._recording_busy)
        return strip

    def sync_tracks(self, tracks: list[dict[str, Any]]) -> None:
        super().sync_tracks(tracks)
        buses = tuple((track["track_id"], track["name"] or track["tag"]) for track in tracks if track.get("is_bus"))
        for track in tracks:
            strip = self._track_mixers[track["track_id"]]
            strip.set_output_routing(track.get("output_bus_id"), buses)
            strip.set_recording_track_kind(bus=bool(track.get("is_bus")), locked=bool(track.get("is_locked")))

    def set_recording_inputs(self, inputs: Mapping[str, str]) -> None:
        self._recording_inputs = dict(inputs)
        for track_id, strip in self._track_mixers.items():
            strip.set_recording_input(self._recording_inputs.get(track_id, ""))

    def set_recording_input_level(self, track_id: str, dbfs: float) -> None:
        strip = self._track_mixers.get(track_id)
        if strip is not None:
            strip.set_recording_input_level(dbfs)

    def set_record_arm_state(self, armed_ids: Collection[str], *, busy: bool = False) -> None:
        self._record_armed_ids = frozenset(armed_ids)
        self._recording_busy = busy
        if self._recording_workflow_active:
            for track_id, strip in self._track_mixers.items():
                strip.set_recording_arm_state(track_id in self._record_armed_ids, busy=busy)

    def set_recording_workflow_active(self, active: bool) -> None:
        """Use the Studio arm workflow when the panel moves to Mixer."""
        if self._recording_workflow_active == active:
            return
        self._recording_workflow_active = active
        self._master_mixer.set_recording_workflow_active(active)
        for track_id, strip in self._track_mixers.items():
            strip.set_recording_workflow_active(active)
            armed = track_id in self._record_armed_ids if active else track_id == self._armed_track
            strip.set_recording_arm_state(armed, busy=active and self._recording_busy)

    def _on_record_arm_toggled(self, track_id: str, armed: bool) -> None:
        """The host owns the full set of armed tracks."""
        if self._recording_workflow_active:
            self.track_record_arm_toggled.emit(track_id, armed)
        else:
            super()._on_record_arm_toggled(track_id, armed)

    def _populate_mixer_menu(self, menu: QMenu) -> None:
        super()._populate_mixer_menu(menu)
        menu.addSeparator()
        menu.addAction(I18N.tr("mixer.routing.add_bus", "Add Bus"), self.add_bus_requested.emit)
        settings = menu.addAction(I18N.tr("mixer.record.settings_menu", "Recording Settings…"))
        settings.setObjectName("mixer_recording_settings")
        settings.triggered.connect(self.recording_settings_requested.emit)
        if self._recording_workflow_active:
            adr = menu.addAction(I18N.tr("panel.adr_take_sheet", "ADR / Takes") + "…")
            adr.setObjectName("mixer_adr")
            adr.triggered.connect(self.adr_requested.emit)

    def close_effect_editors(self) -> None:
        for strip in (*self._track_mixers.values(), self._master_mixer):
            strip.close_all_dialogs()
