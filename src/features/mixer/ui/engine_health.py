"""Low frequency, read-only engine telemetry; hidden panels stop polling."""
from __future__ import annotations

from typing import Callable

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QHideEvent, QShowEvent
from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget

from src.features.edit.core.audio_health import AudioHealthSnapshot
from src.shared.i18n.i18n import I18N


class EngineHealthDialog(QDialog):
    """Display measurements without probing hardware or touching project state."""

    def __init__(self, provider: Callable[[], AudioHealthSnapshot | None], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._provider = provider
        self.setWindowTitle(I18N.tr("mixer.health.title", "Audio engine health"))
        self.resize(490, 490)
        layout = QVBoxLayout(self)
        self.table = QTableWidget(self)
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels([
            I18N.tr("mixer.health.metric", "Metric"), I18N.tr("mixer.health.value", "Measured value")])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(27)
        layout.addWidget(self.table)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText(I18N.tr("mixer.vca.close", "Close"))
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh)

    def refresh(self) -> None:
        snapshot = self._provider()
        data = snapshot.to_dict() if snapshot is not None else {}
        unknown = I18N.tr("mixer.health.unknown", "Not measured")
        definitions = (
            ("sample_rate", "Sample rate", "Hz"), ("buffer_size", "Device block", "samples"),
            ("render_p50_ms", "Render p50", "ms"), ("render_p95_ms", "Render p95", "ms"),
            ("render_p99_ms", "Render p99", "ms"), ("render_deadline_ms", "Render block budget", "ms"),
            ("deadline_miss_count", "Deadline misses", ""),
            ("ring_fill_ratio", "Ring fill", "%"), ("underruns", "Underruns", ""),
            ("overflows", "Overflows", ""), ("pdc_total_samples", "PDC", "samples"),
            ("active_tracks", "Audio tracks", ""), ("active_buses", "Buses", ""),
            ("sound_key_active", "Active sound identity", ""),
        )
        self.table.setRowCount(len(definitions))
        for row, (key, label, unit) in enumerate(definitions):
            value = data.get(key)
            text = unknown
            if value is not None:
                if key == "ring_fill_ratio":
                    text = f"{float(value) * 100:.1f} %"
                elif isinstance(value, float):
                    text = f"{value:.2f} {unit}".strip()
                else:
                    text = f"{value} {unit}".strip()
            self.table.setItem(row, 0, QTableWidgetItem(I18N.tr(f"mixer.health.{key}", label)))
            self.table.setItem(row, 1, QTableWidgetItem(text))
        self.table.resizeColumnToContents(0)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self.refresh()
        self.timer.start()

    def hideEvent(self, event: QHideEvent) -> None:
        self.timer.stop()
        super().hideEvent(event)
