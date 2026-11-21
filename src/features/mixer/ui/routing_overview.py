"""Live routing overview; edits use the existing per-track command entry."""
from __future__ import annotations

import math
from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from src.shared.i18n.i18n import I18N


class RoutingOverviewDialog(QDialog):
    track_edit_requested = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._tracks: tuple[dict[str, Any], ...] = ()
        self.setWindowTitle(I18N.tr("mixer.routing.overview", "Routing overview"))
        self.resize(760, 420)
        layout = QVBoxLayout(self)
        controls = QHBoxLayout()
        self.search = QLineEdit(self)
        self.search.setPlaceholderText(I18N.tr("mixer.console.search", "Find channel"))
        self.search.setAccessibleName(self.search.placeholderText())
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._refresh_rows)
        controls.addWidget(self.search, 1)
        self.target = QComboBox(self)
        self.target.setAccessibleName(I18N.tr("mixer.routing.upstream", "Show upstream of"))
        self.target.setToolTip(self.target.accessibleName())
        self.target.currentIndexChanged.connect(self._refresh_rows)
        controls.addWidget(self.target)
        layout.addLayout(controls)
        hint = QLabel(I18N.tr("mixer.routing.overview_tip", "Output and enabled, unmuted Sends define upstream membership. Double-click a channel to edit its routing."))
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.table = QTableWidget(0, 4, self)
        self.table.setHorizontalHeaderLabels([
            I18N.tr("mixer.routing.channel", "Channel"),
            I18N.tr("mixer.routing.kind", "Type"),
            I18N.tr("mixer.routing.output", "Output routing"),
            I18N.tr("mixer.routing.sends", "Sends"),
        ])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setMinimumSectionSize(64)
        self.table.setColumnWidth(0, 160)
        self.table.setColumnWidth(1, 64)
        self.table.setColumnWidth(2, 140)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.cellDoubleClicked.connect(lambda _row, _column: self._edit_selected())
        self.table.itemSelectionChanged.connect(self._sync_edit_button)
        layout.addWidget(self.table, 1)
        self.status = QLabel(self)
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText(I18N.tr("mixer.routing.close", "Close"))
        self.edit_button = QPushButton(I18N.tr("mixer.routing.edit", "Edit routing…"), self)
        self.edit_button.clicked.connect(self._edit_selected)
        buttons.addButton(self.edit_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)
        self.set_tracks([])

    def set_tracks(self, tracks: list[dict[str, Any]]) -> None:
        self._tracks = tuple(tracks)
        target = self.target.currentData()
        self.target.blockSignals(True)
        self.target.clear()
        self.target.addItem(I18N.tr("mixer.routing.all_routes", "All routes"), "all")
        self.target.addItem(I18N.tr("audio.mixer.master", "Master"), "master")
        for track in tracks:
            if track.get("is_bus"):
                self.target.addItem(self._name(track), track["track_id"])
        self.target.setCurrentIndex(max(0, self.target.findData(target)))
        self.target.blockSignals(False)
        self._refresh_rows()

    @staticmethod
    def _name(track: dict[str, Any]) -> str:
        return str(track.get("name") or track.get("tag") or track["track_id"])

    def _upstream(self, target: str) -> set[str]:
        """Walk view metadata only; never change the engine's routing graph."""
        visited = {target}
        upstream: set[str] = set()
        pending = [target]
        while pending:
            node = pending.pop()
            for track in self._tracks:
                track_id = track["track_id"]
                destinations = {track.get("output_bus_id") or "master"}
                destinations.update(send["target_bus_id"] for send in track.get("sends", ())
                                    if send.get("enabled", True) and not send.get("muted", False)
                                    and send.get("level", 1.0) > 0.0)
                if node in destinations:
                    upstream.add(track_id)
                    if track_id not in visited:
                        visited.add(track_id)
                        pending.append(track_id)
        return upstream

    def _refresh_rows(self) -> None:
        selected = self._selected_track_id()
        names = {track["track_id"]: self._name(track) for track in self._tracks}
        names["master"] = I18N.tr("audio.mixer.master", "Master")
        words = self.search.text().casefold().split()
        target = self.target.currentData()
        upstream = self._upstream(target) if target not in (None, "all") else set(names)
        rows = [track for track in self._tracks if track["track_id"] in upstream
                and all(word in f"{self._name(track)} {track.get('tag', '')}".casefold() for word in words)]
        self.table.setRowCount(0)
        self.table.setRowCount(len(rows))
        for row, track in enumerate(rows):
            sends = []
            for send in track.get("sends", ()):
                level = float(send.get("level", 1.0))
                db = f"{20.0 * math.log10(level):+.1f} dB" if level > 0.0 else "−∞ dB"
                state = (I18N.tr("mixer.routing.disabled", "Off") if not send.get("enabled", True)
                         else I18N.tr("mixer.routing.muted", "Muted") if send.get("muted", False) else "")
                tap = I18N.tr("mixer.routing.pre", "Pre") if send.get("tap") == "pre_fader" else I18N.tr("mixer.routing.post", "Post")
                name = names.get(send["target_bus_id"], I18N.tr("mixer.routing.missing_bus", "Missing bus"))
                sends.append(" · ".join(value for value in (name, db, tap, state) if value))
            output = names.get(track.get("output_bus_id") or "master", I18N.tr("mixer.routing.missing_bus", "Missing bus"))
            values = (self._name(track), I18N.tr("mixer.console.bus", "Buses") if track.get("is_bus") else I18N.tr("mixer.console.audio", "Audio"), output, "\n".join(sends) or "—")
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                item.setData(Qt.ItemDataRole.UserRole, track["track_id"])
                self.table.setItem(row, column, item)
            if track["track_id"] == selected:
                self.table.selectRow(row)
        self.table.resizeRowsToContents()
        self.status.setText(f"{len(rows)}/{len(self._tracks)}")
        self._sync_edit_button()

    def _selected_track_id(self) -> str | None:
        item = self.table.item(self.table.currentRow(), 0)
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _sync_edit_button(self) -> None:
        self.edit_button.setEnabled(self._selected_track_id() is not None)

    def _edit_selected(self) -> None:
        track_id = self._selected_track_id()
        if track_id is not None:
            self.track_edit_requested.emit(track_id)
