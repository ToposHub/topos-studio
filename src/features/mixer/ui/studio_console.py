"""Studio presentation for the shared Edit Audio Mixer."""
from __future__ import annotations

import math
from collections.abc import Collection, Mapping
from typing import Any

from PyQt6.QtCore import QPoint, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QKeySequence, QShortcut
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QMenu, QSizePolicy, QToolButton, QVBoxLayout, QWidget

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
from .workspace_style import compact_combo, TOOL_QSS


STUDIO_STRIP_WIDTH = 96
STUDIO_COMPACT_STRIP_WIDTH = 84


class StudioChannelStrip(TrackMixerWidget):
    """Use the real insert rack and retain the Studio fader and peak meter."""

    output_bus_requested = pyqtSignal(str, object)
    recording_input_requested = pyqtSignal(str)
    solo_safe_requested = pyqtSignal(str, bool)

    def __init__(
        self, track_id: str, track_name: str, track_tag: str, parent: QWidget,
        *, is_master: bool,
    ) -> None:
        self._drag_start_db: float | None = None
        self._meter_gr = 0.0
        self._playback_levels = (VOLUME_DB_MIN, VOLUME_DB_MIN)
        self._showing_input_level = False
        super().__init__(track_id, track_name, track_tag, parent, is_master)
        self._solo_safe = False
        self.mixer_menu_requested.connect(self._append_solo_safe_action)
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
        self.studio_meter.peakReset.connect(self._sync_studio_peak)
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

    def _append_solo_safe_action(self, menu: QMenu) -> None:
        if self._is_master:
            return
        action = menu.addAction(I18N.tr("mixer.console.solo_safe", "Solo safe"))
        action.setObjectName("mixerSoloSafe")
        action.setCheckable(True)
        action.setChecked(self._solo_safe)
        action.triggered.connect(lambda checked: self.solo_safe_requested.emit(self._track_id, checked))

    def set_solo_safe(self, safe: bool) -> None:
        self._solo_safe = safe

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
    vca_groups_requested = pyqtSignal()
    health_requested = pyqtSignal()
    track_solo_safe_requested = pyqtSignal(str, bool)
    recover_recording_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        self._recording_inputs: dict[str, str] = {}
        self._record_armed_ids: frozenset[str] = frozenset()
        self._recording_busy = False
        self._recording_workflow_active = False
        self._channel_order: tuple[str, ...] = ()
        self._bus_channel_ids: set[str] = set()
        self._routing_view_tracks: list[dict[str, Any]] = []
        self._routing_overview = None
        super().__init__(parent)
        self._init_channel_navigation()

    def _init_channel_navigation(self) -> None:
        toolbar = QWidget(self)
        toolbar.setObjectName("mixerChannelNavigation")
        toolbar_layout = QVBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(4, 3, 4, 3)
        toolbar_layout.setSpacing(2)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        toolbar_layout.addLayout(row)
        view_row = QHBoxLayout()
        view_row.setSpacing(4)
        toolbar_layout.addLayout(view_row)
        self.channel_search = QLineEdit(toolbar)
        self.channel_search.setObjectName("mixerChannelSearch")
        search_label = I18N.tr("mixer.console.search", "Find channel")
        self.channel_search.setPlaceholderText(search_label)
        self.channel_search.setAccessibleName(search_label)
        self.channel_search.setToolTip(I18N.tr("mixer.console.search_tip", "Search names or channel tags · Enter to locate · Esc to clear"))
        self.channel_search.setClearButtonEnabled(True)
        self.channel_search.setMinimumWidth(45)
        self.channel_search.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.channel_search.textChanged.connect(self._apply_channel_filter)
        self.channel_search.returnPressed.connect(self._locate_first_channel)
        row.addWidget(self.channel_search, 1)
        routing_button = QToolButton(toolbar)
        routing_button.setText("⇄")
        routing_button.setAccessibleName(I18N.tr("mixer.routing.overview", "Routing overview"))
        routing_button.setToolTip(routing_button.accessibleName())
        routing_button.clicked.connect(self.open_routing_overview)
        self.routing_overview_button = routing_button
        row.addWidget(routing_button)
        self.channel_filter = compact_combo()
        self.channel_filter.setObjectName("mixerChannelFilter")
        self.channel_filter.setAccessibleName(I18N.tr("mixer.console.filter", "Channel view"))
        self.channel_filter.setToolTip(I18N.tr("mixer.console.filter_tip", "Filter the view; audio, routing and recording stay active"))
        for key, fallback in (("all", "All"), ("audio", "Audio"), ("bus", "Buses"),
                              ("solo", "Solo"), ("armed", "Armed")):
            self.channel_filter.addItem(I18N.tr(f"mixer.console.{key}", fallback), key)
        self.channel_filter.setFixedWidth(76)
        self.channel_filter.currentIndexChanged.connect(self._apply_channel_filter)
        view_row.addWidget(self.channel_filter)
        self.channel_count = QLabel(toolbar)
        self.channel_count.setAccessibleName(I18N.tr("mixer.console.count", "Visible channels / total"))
        view_row.addWidget(self.channel_count)
        view_row.addStretch()
        self.clear_solo_button = QToolButton(toolbar)
        self.clear_solo_button.setText("S×")
        self.clear_solo_button.setAccessibleName(I18N.tr("mixer.console.clear_solo", "Clear all Solo"))
        self.clear_solo_button.setToolTip(self.clear_solo_button.accessibleName())
        self.clear_solo_button.clicked.connect(self.clear_all_solo)
        view_row.addWidget(self.clear_solo_button)
        self.reset_peaks_button = QToolButton(toolbar)
        self.reset_peaks_button.setText("↺")
        self.reset_peaks_button.setAccessibleName(I18N.tr("mixer.console.reset_peaks", "Reset all peak holds and clip indicators"))
        self.reset_peaks_button.setToolTip(self.reset_peaks_button.accessibleName())
        self.reset_peaks_button.clicked.connect(self.reset_peak_holds)
        view_row.addWidget(self.reset_peaks_button)
        self.console_options_button = QToolButton(toolbar)
        self.console_options_button.setText("⋯")
        self.console_options_button.setAccessibleName(I18N.tr("audio.mixer.options", "Mixer options"))
        self.console_options_button.setToolTip(self.console_options_button.accessibleName())
        self.console_options_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.console_options_button)
        menu.aboutToShow.connect(lambda: self._refresh_navigation_menu(menu))
        self.console_options_button.setMenu(menu)
        row.addWidget(self.console_options_button)
        for button in (routing_button, self.clear_solo_button, self.reset_peaks_button, self.console_options_button):
            button.setFixedSize(24, 24)
            button.setStyleSheet(TOOL_QSS + f"QToolButton {{ color: {C.TEXT_PRIMARY}; }}")
        self.layout().insertWidget(0, toolbar)
        self.channel_empty_label = QLabel(I18N.tr("mixer.console.no_match", "No matching channels"))
        self.channel_empty_label.setWordWrap(True)
        self.channel_empty_label.setMaximumWidth(180)
        self._tracks_layout.addWidget(self.channel_empty_label)
        self._find_channel_shortcut = QShortcut(QKeySequence.StandardKey.Find, self)
        self._find_channel_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._find_channel_shortcut.activated.connect(self._focus_channel_search)
        self._clear_search_shortcut = QShortcut(QKeySequence("Escape"), self.channel_search)
        self._clear_search_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        self._clear_search_shortcut.activated.connect(self._clear_channel_search)
        self._apply_channel_filter()

    def _refresh_navigation_menu(self, menu: QMenu) -> None:
        menu.clear()
        self._populate_mixer_menu(menu)
        menu.addSeparator()
        menu.addAction(I18N.tr("mixer.vca.title", "VCA groups"), self.vca_groups_requested.emit)
        menu.addAction(I18N.tr("mixer.health.title", "Audio engine health"), self.health_requested.emit)
        menu.addAction(I18N.tr("mixer.recovery.title", "Recover interrupted recording…"), self.recover_recording_requested.emit)
        reset = menu.addAction(I18N.tr("mixer.console.reset_view", "Reset channel view"))
        reset.triggered.connect(self.reset_channel_view)

    def open_routing_overview(self) -> None:
        from .routing_overview import RoutingOverviewDialog

        if self._routing_overview is None:
            self._routing_overview = RoutingOverviewDialog(self)
            self._routing_overview.track_edit_requested.connect(self.track_show_routing)
        self._routing_overview.set_tracks(self._routing_view_tracks)
        self._routing_overview.show()
        self._routing_overview.raise_()
        self._routing_overview.activateWindow()

    def _focus_channel_search(self) -> None:
        self.channel_search.setFocus()
        self.channel_search.selectAll()

    def _clear_channel_search(self) -> None:
        self.channel_search.clear()
        self.channel_search.clearFocus()

    def reset_channel_view(self) -> None:
        """Restore all strips without touching their sound or project state."""
        self.channel_search.clear()
        self.channel_filter.setCurrentIndex(0)
        self.show_all_tracks()

    def reset_peak_holds(self) -> None:
        """Clear display holds, preserving live level, GR and engine generation."""
        for strip in (*self._track_mixers.values(), self._master_mixer):
            strip.studio_meter.reset_clip()

    def _update_minimum_width(self) -> None:
        visible = [strip for strip in self._track_mixers.values() if not strip.isHidden()]
        self._scroll_area.setMinimumWidth(visible[0].minimumWidth() + 2 if visible else 30)

    def _sync_master_separator(self) -> None:
        if self._master_mixer is None or self._master_separator is None:
            return
        top_left = self._master_mixer.mapTo(self, QPoint())
        self._master_separator.setGeometry(max(0, top_left.x() - 1), top_left.y(),
                                           1, max(0, self.height() - top_left.y()))
        self._master_separator.raise_()

    def _apply_channel_filter(self) -> None:
        if not hasattr(self, "channel_search"):
            return
        words = self.channel_search.text().casefold().split()
        kind = self.channel_filter.currentData()
        visible = 0
        for track_id, strip in self._track_mixers.items():
            text = f"{strip._track_name} {strip._track_tag}".casefold()
            matches_kind = (kind == "all"
                            or kind == "bus" and track_id in self._bus_channel_ids
                            or kind == "audio" and track_id not in self._bus_channel_ids
                            or kind == "solo" and strip._is_solo
                            or kind == "armed" and strip._is_armed)
            shown = (track_id not in self._hidden_track_ids and matches_kind
                     and all(word in text for word in words))
            strip.setVisible(shown)
            self._track_separators[track_id].setVisible(shown)
            visible += int(shown)
        self.channel_count.setText(f"{visible}/{len(self._track_mixers)}")
        self.channel_count.setToolTip(self.channel_count.accessibleName())
        self.clear_solo_button.setEnabled(bool(self._solo_tracks))
        self.channel_empty_label.setVisible(visible == 0)
        self._update_minimum_width()

    def _locate_first_channel(self) -> None:
        order = self._channel_order + tuple(tid for tid in self._track_mixers if tid not in self._channel_order)
        for track_id in order:
            strip = self._track_mixers.get(track_id)
            if strip is not None and not strip.isHidden():
                self._scroll_area.ensureWidgetVisible(strip, 8, 0)
                break

    def register_track(self, track_id: str, track_name: str = "", track_tag: str = "") -> None:
        super().register_track(track_id, track_name, track_tag)
        self._apply_channel_filter()

    def deregister_track(self, track_id: str) -> None:
        super().deregister_track(track_id)
        self._bus_channel_ids.discard(track_id)
        self._routing_view_tracks = [track for track in self._routing_view_tracks if track["track_id"] != track_id]
        if self._routing_overview is not None:
            self._routing_overview.set_tracks(self._routing_view_tracks)
        self._apply_channel_filter()

    def clear_all_tracks(self) -> None:
        super().clear_all_tracks()
        self._channel_order = ()
        self._routing_view_tracks = []
        if self._routing_overview is not None:
            self._routing_overview.set_tracks([])
        self._apply_channel_filter()

    def hide_track(self, track_id: str) -> None:
        super().hide_track(track_id)
        self._apply_channel_filter()

    def show_track(self, track_id: str) -> None:
        super().show_track(track_id)
        self._apply_channel_filter()

    def set_track_name(self, track_id: str, name: str) -> None:
        super().set_track_name(track_id, name)
        self._apply_channel_filter()

    def clear_all_solo(self) -> None:
        super().clear_all_solo()
        self._apply_channel_filter()

    def _on_solo_toggled(self, track_id: str, solo: bool) -> None:
        super()._on_solo_toggled(track_id, solo)
        self._apply_channel_filter()

    def set_track_solo(self, track_id: str, solo: bool, additive: bool = False) -> None:
        super().set_track_solo(track_id, solo, additive)
        self._apply_channel_filter()

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
        strip.solo_safe_requested.connect(self.track_solo_safe_requested)
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
            strip.set_solo_safe(bool(track.get("solo_safe", False)))
            strip.set_output_routing(track.get("output_bus_id"), buses)
            strip.set_recording_track_kind(bus=bool(track.get("is_bus")), locked=bool(track.get("is_locked")))
            tag = track.get("tag", "")
            if tag and tag != strip._track_tag:
                strip._track_tag = tag
                strip._track_tag_label.setText(tag)
                strip._update_track_label()
        order = tuple(track["track_id"] for track in tracks)
        if order != self._channel_order:
            self._channel_order = order
            for index, track_id in enumerate(order):
                self._tracks_layout.insertWidget(index * 2, self._track_mixers[track_id])
                self._tracks_layout.insertWidget(index * 2 + 1, self._track_separators[track_id])
        self._bus_channel_ids = {track["track_id"] for track in tracks if track.get("is_bus")}
        self._routing_view_tracks = tracks
        if self._routing_overview is not None:
            self._routing_overview.set_tracks(tracks)
        self._apply_channel_filter()

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
        self._apply_channel_filter()

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
        self._apply_channel_filter()

    def _on_record_arm_toggled(self, track_id: str, armed: bool) -> None:
        """The host owns the full set of armed tracks."""
        if self._recording_workflow_active:
            self.track_record_arm_toggled.emit(track_id, armed)
        else:
            super()._on_record_arm_toggled(track_id, armed)
        self._apply_channel_filter()

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
        if self._routing_overview is not None:
            self._routing_overview.close()
        for strip in (*self._track_mixers.values(), self._master_mixer):
            strip.close_all_dialogs()
