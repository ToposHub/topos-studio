"""Local monitor source, speaker patch and gain controls for Studio Mixer."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtMultimedia import QMediaDevices
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QPushButton, QSpinBox, QWidget,
)

from src.features.edit.core.monitor_service import ControlRoomPatch, MonitorSessionState
from src.features.edit.core.channel_layout import ChannelLayout
from src.shared.i18n.i18n import I18N

if TYPE_CHECKING:
    from src.features.edit.core.timeline_v2 import TimelineSequence


def _tr(key: str, fallback: str) -> str:
    return I18N.tr(f"mixer.monitor.{key}", fallback)


class ControlRoomDialog(QDialog):
    """Edit one local session. Opening or closing the dialog leaves audio running."""

    state_requested = pyqtSignal(object)
    patch_requested = pyqtSignal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(_tr("title", "Control room"))
        self.setMinimumWidth(415)
        self._syncing = False
        self._state = MonitorSessionState()
        form = QFormLayout(self)
        self.source = QComboBox()
        for key, label in (("program", "Programme"), ("pfl", "PFL · Before fader"),
                           ("afl", "AFL · After fader"), ("sip", "SIP · Programme solo"),
                           ("input", "Live input")):
            self.source.addItem(_tr(key, label), key)
        form.addRow(_tr("source", "Listen to"), self.source)
        self.tracks = QListWidget()
        self.tracks.setMaximumHeight(115)
        self.tracks.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        form.addRow(_tr("channels", "Listen channels"), self.tracks)
        self.input_track = QComboBox()
        self.input_label = QLabel(_tr("input_track", "Input patch"))
        form.addRow(self.input_label, self.input_track)
        self.gain = QDoubleSpinBox()
        self.gain.setRange(-90, 0)
        self.gain.setDecimals(1)
        self.gain.setSuffix(" dB")
        form.addRow(_tr("gain", "Monitor level"), self.gain)
        self.mono = QCheckBox(_tr("mono", "Mono"))
        self.dim = QCheckBox(_tr("dim", "DIM"))
        self.mute = QCheckBox(_tr("mute", "Mute speakers"))
        self.dim_level = QDoubleSpinBox()
        self.dim_level.setRange(-60, 0)
        self.dim_level.setDecimals(1)
        self.dim_level.setSuffix(" dB")
        form.addRow(self.mono, self.mute)
        form.addRow(self.dim, self.dim_level)

        self.device = QComboBox()
        form.addRow(_tr("device", "Output device"), self.device)
        self.width = QSpinBox()
        self.width.setRange(1, 32)
        form.addRow(_tr("width", "Device channels"), self.width)
        self.channel_map = QLineEdit()
        self.channel_map.setPlaceholderText(_tr("map_hint", "Automatic stereo, or e.g. 1,2,3,4,5,6"))
        form.addRow(_tr("map", "Speaker outputs"), self.channel_map)
        self.speaker_order = QLabel()
        self.speaker_order.setWordWrap(True)
        form.addRow(self.speaker_order)
        self.trims = QLineEdit()
        self.trims.setPlaceholderText(_tr("trims_hint", "Optional, e.g. 0,-1.5,0,0,0,0 dB"))
        form.addRow(_tr("trims", "Speaker trims (dB)"), self.trims)
        self.apply_patch = QPushButton(_tr("apply", "Apply output patch"))
        form.addRow(self.apply_patch)
        self.status = QLabel()
        self.status.setWordWrap(True)
        form.addRow(self.status)

        self.source.currentIndexChanged.connect(self._publish_state)
        self.tracks.itemSelectionChanged.connect(self._publish_state)
        self.input_track.currentIndexChanged.connect(self._publish_state)
        self.gain.valueChanged.connect(self._publish_state)
        self.mono.toggled.connect(self._publish_state)
        self.dim.toggled.connect(self._publish_state)
        self.dim_level.valueChanged.connect(self._publish_state)
        self.mute.toggled.connect(self._publish_state)
        self.device.currentIndexChanged.connect(self._device_changed)
        self.channel_map.textChanged.connect(self._show_speaker_order)
        self.apply_patch.clicked.connect(self._publish_patch)
        self._show_speaker_order("")

    def set_session(
        self, timeline: TimelineSequence | None, state: MonitorSessionState,
        patch: ControlRoomPatch, input_tracks: dict[str, str], *, refresh_devices: bool = False,
    ) -> None:
        self._syncing = True
        try:
            self._state = state
            if refresh_devices:
                self.device.clear()
                self.device.addItem(_tr("default_device", "System default"), b"")
                for device in QMediaDevices.audioOutputs():
                    self.device.addItem(device.description(), bytes(device.id()))
                if self.device.findData(patch.device_id) < 0:
                    self.device.addItem(_tr("unavailable_device", "Selected device unavailable"), patch.device_id)
                self.device.setCurrentIndex(self.device.findData(patch.device_id))
                self.width.setValue(patch.device_channels)
                self.channel_map.setText(",".join(str(channel + 1) for channel in patch.channels))
                self.trims.setText(",".join(f"{trim:g}" for trim in patch.trim_db))
            self.tracks.clear()
            self.input_track.clear()
            self.input_track.addItem(_tr("select_input", "Select an input patch"), "")
            for track in timeline.audio_tracks if timeline is not None else ():
                item = QListWidgetItem(track.name)
                item.setData(Qt.ItemDataRole.UserRole, track.track_id)
                self.tracks.addItem(item)
                item.setSelected(track.track_id in state.selected_listen_ids)
                if track.track_id in input_tracks:
                    self.input_track.addItem(f"{track.name} · {input_tracks[track.track_id]}", track.track_id)
            self.input_track.setCurrentIndex(max(0, self.input_track.findData(
                state.selected_listen_ids[0] if state.source_tap == "input" and state.selected_listen_ids else "")))
            self.source.setCurrentIndex(self.source.findData(state.source_tap))
            self.gain.setValue(state.gain_db)
            self.mono.setChecked(state.mono)
            self.dim.setChecked(state.dim)
            self.dim_level.setValue(state.dim_db)
            self.mute.setChecked(state.muted)
            self.tracks.setEnabled(timeline is not None)
            self.input_track.setEnabled(timeline is not None)
            self._update_source_controls(state.source_tap)
        finally:
            self._syncing = False

    def _update_source_controls(self, source: str) -> None:
        self.tracks.setVisible(source in ("pfl", "afl"))
        form = self.layout()
        form.labelForField(self.tracks).setVisible(source in ("pfl", "afl"))
        self.input_label.setVisible(source == "input")
        self.input_track.setVisible(source == "input")

    def _publish_state(self) -> None:
        if self._syncing:
            return
        source = self.source.currentData()
        self._update_source_controls(source)
        listening = tuple(item.data(Qt.ItemDataRole.UserRole) for item in self.tracks.selectedItems())
        if source == "input":
            track_id = self.input_track.currentData()
            listening = (track_id,) if track_id else ()
        self.state_requested.emit(replace(
            self._state, source_tap=source, selected_listen_ids=listening,
            gain_db=self.gain.value(), dim=self.dim.isChecked(), dim_db=self.dim_level.value(),
            mono=self.mono.isChecked(), muted=self.mute.isChecked(),
        ))

    def _device_changed(self) -> None:
        if not self._syncing:
            self.status.clear()

    def _show_speaker_order(self, patch_text: str) -> None:
        try:
            layout = ChannelLayout.from_channel_count(len(patch_text.split(",")) if patch_text.strip() else 2)
        except ValueError:
            self.speaker_order.clear()
        else:
            self.speaker_order.setText(_tr("speaker_order", "Speaker order {order}").format(
                order=" · ".join(layout.channel_labels)))

    def _publish_patch(self) -> None:
        try:
            slots = tuple(int(value.strip()) - 1 for value in self.channel_map.text().split(",")) \
                if self.channel_map.text().strip() else ()
            trim_db = tuple(float(value.strip()) for value in self.trims.text().split(",")) \
                if self.trims.text().strip() else ()
            patch = ControlRoomPatch(self.device.currentData() or b"", slots,
                                     self.width.value() if slots else 2, trim_db)
        except ValueError:
            self.status.setText(_tr("invalid_patch", "Enter unique speaker outputs within the device width and trims from −24 to 0 dB."))
            return
        self.patch_requested.emit(patch)
