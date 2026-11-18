"""Studio presentation for the shared Edit Audio Mixer."""
from __future__ import annotations

import math
from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QMenu, QWidget

from src.shared.i18n.i18n import I18N
from src.shared.ui.audio_mixer import AudioMixerPanel
from src.shared.ui.audio_mixer.audio_level_meter import MeterMode
from src.shared.ui.audio_mixer.track_mixer_widget import (
    VOLUME_DB_MAX,
    VOLUME_DB_MIN,
    TrackMixerWidget,
    linear_to_db,
)

from .console_controls import ACCENT_CYAN, EditableValuePill, LevelMeter, StudioFader, format_db
from .workspace_style import compact_combo


STUDIO_STRIP_WIDTH = 96
STUDIO_COMPACT_STRIP_WIDTH = 84


class StudioChannelStrip(TrackMixerWidget):
    """Use the real insert rack and retain the Studio fader and peak meter."""

    output_bus_requested = pyqtSignal(str, object)

    def __init__(
        self, track_id: str, track_name: str, track_tag: str, parent: QWidget,
        *, is_master: bool,
    ) -> None:
        self._drag_start_db: float | None = None
        self._meter_gr = 0.0
        super().__init__(track_id, track_name, track_tag, parent, is_master)
        for widget in (self._fx_badge, self._dy_badge, self._eq_badge, self._meter_section, self._gain_section):
            widget.hide()

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
        self.output_bus_combo.setAccessibleName(I18N.tr("mixer.routing.output", "Output routing"))
        self.output_bus_combo.activated.connect(self._request_output_bus)
        self.output_bus_combo.setVisible(not is_master)
        footer_layout.addWidget(self.output_bus_combo)
        self.layout().addWidget(footer)
        self._output_choices: tuple[tuple[str | None, str], ...] = ()
        self._output_bus_id: str | None = None
        if not is_master:
            self.set_output_routing(None, ())

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
        self.studio_meter.set_levels(left, right, self._meter_gr)
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
        self.studio_meter.clear_levels()
        self._sync_studio_peak()


class StudioAudioMixerPanel(AudioMixerPanel):
    """The same console is hosted in both Studio workspaces."""

    track_output_requested = pyqtSignal(str, object)
    add_bus_requested = pyqtSignal()

    def _create_strip(
        self, track_id: str, track_name: str, track_tag: str, *, is_master: bool = False,
    ) -> StudioChannelStrip:
        strip = StudioChannelStrip(track_id, track_name, track_tag, self, is_master=is_master)
        strip.output_bus_requested.connect(self.track_output_requested)
        return strip

    def sync_tracks(self, tracks: list[dict[str, Any]]) -> None:
        super().sync_tracks(tracks)
        buses = tuple((track["track_id"], track["name"] or track["tag"]) for track in tracks if track.get("is_bus"))
        for track in tracks:
            strip = self._track_mixers[track["track_id"]]
            strip.set_output_routing(track.get("output_bus_id"), buses)

    def _populate_mixer_menu(self, menu: QMenu) -> None:
        super()._populate_mixer_menu(menu)
        menu.addSeparator()
        menu.addAction(I18N.tr("mixer.routing.add_bus", "Add Bus"), self.add_bus_requested.emit)

    def close_effect_editors(self) -> None:
        for strip in (*self._track_mixers.values(), self._master_mixer):
            strip.close_all_dialogs()
