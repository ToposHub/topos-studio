"""VCA editor: canonical sequence snapshots in, edit intentions out."""
from __future__ import annotations

import uuid

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QPushButton, QVBoxLayout, QWidget,
)

from src.features.edit.core.timeline_v2 import TimelineSequence, VcaGroup
from src.shared.i18n.i18n import I18N


class VcaGroupsDialog(QDialog):
    """Apply creates one history entry; dismissal leaves the model untouched."""

    group_requested = pyqtSignal(str, object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(I18N.tr("mixer.vca.title", "VCA groups"))
        self.resize(440, 480)
        self._groups: dict[str, VcaGroup] = {}
        self._tracks: tuple[tuple[str, str], ...] = ()
        self._sequence_id: str | None = None
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.groups = QComboBox(self)
        self.groups.setAccessibleName(self.windowTitle())
        self.groups.currentIndexChanged.connect(self._load_group)
        row.addWidget(self.groups, 1)
        self.new_button = QPushButton(I18N.tr("mixer.vca.new", "New group"), self)
        self.new_button.clicked.connect(self._new_group)
        row.addWidget(self.new_button)
        layout.addLayout(row)
        form = QFormLayout()
        self.name = QLineEdit(self)
        form.addRow(I18N.tr("mixer.vca.name", "Name"), self.name)
        self.gain = QDoubleSpinBox(self)
        self.gain.setRange(-120.0, 24.0)
        self.gain.setDecimals(2)
        self.gain.setSuffix(" dB")
        form.addRow(I18N.tr("mixer.vca.gain", "Group gain"), self.gain)
        self.muted = QCheckBox(I18N.tr("mixer.vca.muted", "Mute group"), self)
        form.addRow(self.muted)
        layout.addLayout(form)
        description = QLabel(I18N.tr(
            "mixer.vca.description", "Members keep their routing. Overlapping group gains multiply."), self)
        description.setWordWrap(True)
        layout.addWidget(description)
        self.members = QListWidget(self)
        self.members.setAccessibleName(I18N.tr("mixer.vca.members", "Member audio tracks and buses"))
        layout.addWidget(self.members, 1)
        self.status = QLabel(self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Close, self)
        self.apply_button = buttons.button(QDialogButtonBox.StandardButton.Apply)
        self.apply_button.setText(I18N.tr("mixer.vca.apply", "Apply"))
        buttons.button(QDialogButtonBox.StandardButton.Close).setText(I18N.tr("mixer.vca.close", "Close"))
        self.apply_button.clicked.connect(self._apply)
        buttons.rejected.connect(self.close)
        self.delete_button = buttons.addButton(I18N.tr("mixer.vca.delete", "Delete group"), QDialogButtonBox.ButtonRole.DestructiveRole)
        self.delete_button.clicked.connect(self._delete)
        layout.addWidget(buttons)
        self.set_sequence(None)

    def set_sequence(self, sequence: TimelineSequence | None) -> None:
        """Refresh after commit/undo/switch without maintaining a second authority."""
        selected = self.groups.currentData()
        next_identity = sequence.timeline_id if sequence is not None else None
        first_load = next_identity != self._sequence_id
        self._sequence_id = next_identity
        self._groups = {g.vca_id: g for g in sequence.vcas} if sequence is not None else {}
        self._tracks = tuple((t.track_id, t.name or t.track_id) for t in sequence.audio_tracks) if sequence is not None else ()
        self.groups.blockSignals(True)
        self.groups.clear()
        for group in self._groups.values():
            self.groups.addItem(group.name or group.vca_id, group.vca_id)
        self.groups.addItem(I18N.tr("mixer.vca.new", "New group"), None)
        index = 0 if first_load else self.groups.findData(selected)
        self.groups.setCurrentIndex(max(0, index))
        self.groups.blockSignals(False)
        self.apply_button.setEnabled(sequence is not None)
        self.new_button.setEnabled(sequence is not None)
        self._load_group()

    def _new_group(self) -> None:
        self.groups.setCurrentIndex(self.groups.findData(None))
        self._load_group()
        self.name.setFocus()
        self.name.selectAll()

    def _load_group(self) -> None:
        group = self._groups.get(self.groups.currentData())
        self.name.setText(group.name if group else "")
        self.gain.setValue(group.gain_db if group else 0.0)
        self._loaded_gain = group.gain_db if group else 0.0
        self._loaded_display_gain = self.gain.value()
        self.muted.setChecked(group.muted if group else False)
        members = set(group.member_track_ids) if group else set()
        self.members.clear()
        for track_id, name in self._tracks:
            item = QListWidgetItem(name, self.members)
            item.setData(Qt.ItemDataRole.UserRole, track_id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if track_id in members else Qt.CheckState.Unchecked)
        missing = members - {tid for tid, _name in self._tracks}
        self.status.setText(I18N.tr("mixer.vca.missing", "Some members are offline; applying removes missing members.") if missing else "")
        self.delete_button.setEnabled(group is not None)

    def _apply(self) -> None:
        name = self.name.text().strip()
        if not name:
            self.status.setText(I18N.tr("mixer.vca.name_required", "Enter a group name."))
            self.name.setFocus()
            return
        members = tuple(self.members.item(i).data(Qt.ItemDataRole.UserRole)
                        for i in range(self.members.count())
                        if self.members.item(i).checkState() == Qt.CheckState.Checked)
        identity = self.groups.currentData() or str(uuid.uuid4())
        # A name/member edit must not round an existing high-precision gain
        # merely because the spin box displays two decimal places.
        gain = self.gain.value()
        if gain == self._loaded_display_gain:
            gain = self._loaded_gain
        group = VcaGroup(identity, name, members, gain, self.muted.isChecked())
        self.group_requested.emit(identity, group)
        index = self.groups.findData(identity)
        if index >= 0:
            self.groups.setCurrentIndex(index)

    def _delete(self) -> None:
        identity = self.groups.currentData()
        if identity in self._groups:
            self.group_requested.emit(identity, None)
