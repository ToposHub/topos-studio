"""Signal-only Color Stabilizer palette, sharing the Tracker time ruler."""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QResizeEvent
from PyQt6.QtWidgets import (
    QCheckBox, QGridLayout, QHBoxLayout, QLabel, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget,
)

from src.features.color.ui.power_window_panel import WindowValue
from src.features.color.ui.tracker_graph import TrackerGraph
from src.shared.i18n.i18n import tr
from src.shared.tracking.stabilization_state import DEFAULT_PARAMS
from src.shared.ui.widgets import ChevronComboBox


STABILIZER_DEFAULTS: dict[str, Any] = dict(DEFAULT_PARAMS)

STYLE = """
StabilizerPanel { background: #202024; }
StabilizerPanel QWidget#stabilizer_bar { background: #2c2d32; }
StabilizerPanel QLabel { color: #999b9d; font-size: 11px; background: transparent; }
StabilizerPanel QLabel:disabled { color: #56585a; }
StabilizerPanel QLabel#stabilizer_status { font-size: 10px; padding: 0 8px; }
StabilizerPanel QCheckBox { color: #a5a6a8; font-size: 11px; spacing: 6px; background: transparent; }
StabilizerPanel QCheckBox:disabled { color: #56585a; }
StabilizerPanel QCheckBox::indicator { width: 12px; height: 12px; }
StabilizerPanel QPushButton#stabilizer_analyze {
    color: #a5a6a8; font-size: 11px; background: transparent;
    border: 1px solid #45464c; border-radius: 12px; padding: 0 14px;
}
StabilizerPanel QPushButton#stabilizer_analyze:hover { background: #34363a; border-color: #74767b; }
StabilizerPanel QPushButton#stabilizer_analyze:disabled { color: #55575b; border-color: #393b3f; }
"""


class StabilizerPanel(QWidget):
    """Video stabilization controls; the controller owns analysis and rendering."""

    analyze_requested = pyqtSignal()
    cancel_requested = pyqtSignal()
    params_changed = pyqtSignal(dict)
    reset_requested = pyqtSignal()
    interaction_started = pyqtSignal()
    interaction_ended = pyqtSignal()
    state_changed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("StabilizerPanel")
        self.setMinimumWidth(300)
        self.setStyleSheet(STYLE)
        self._updating = False
        self._running = False
        self._video_available = False
        self._footer_rows = 0
        self.controls: dict[str, WindowValue] = {}
        self._parameter_groups: list[QWidget] = []
        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(0, 0, 0, 0)
        self._root.setSpacing(0)
        self._root.addWidget(self._build_header())
        self.status_label = QLabel(self)
        self.status_label.setObjectName("stabilizer_status")
        self.status_label.setFixedHeight(20)
        self.status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.status_label.hide()
        self._root.addWidget(self.status_label)
        self.graph = TrackerGraph(self, show_controls=False)
        self._root.addWidget(self.graph, 1)
        self._root.addWidget(self._build_footer())
        self.set_params(STABILIZER_DEFAULTS)
        self._apply_state()

    def _build_header(self) -> QWidget:
        bar = QWidget(self)
        bar.setObjectName("stabilizer_bar")
        bar.setFixedHeight(40)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(22, 5, 32, 5)
        self.bypass_box = QCheckBox(tr("tracker.stabilizer.bypass", "Bypass Stabilization"), bar)
        self.bypass_box.setAccessibleName("Bypass Stabilization")
        self.bypass_box.toggled.connect(self._on_parameter_edited)
        layout.addWidget(self.bypass_box)
        layout.addStretch(1)
        self.btn_stabilize = QPushButton(tr("tracker.stabilizer.analyze", "Stabilize"), bar)
        self.btn_stabilize.setObjectName("stabilizer_analyze")
        self.btn_stabilize.setAccessibleName("Stabilize")
        self.btn_stabilize.setFixedSize(110, 26)
        self.btn_stabilize.clicked.connect(self._on_analyze_clicked)
        layout.addWidget(self.btn_stabilize)
        return bar

    def _build_footer(self) -> QWidget:
        self.footer = QWidget(self)
        self.footer.setObjectName("stabilizer_bar")
        self._footer_grid = QGridLayout(self.footer)
        self._footer_grid.setContentsMargins(20, 5, 32, 5)
        self._footer_grid.setHorizontalSpacing(9)
        self._footer_grid.setVerticalSpacing(4)
        for key, title in (("cropping_ratio", "Cropping Ratio"), ("smooth", "Smooth"),
                           ("strength", "Strength")):
            group = QWidget(self.footer)
            layout = QHBoxLayout(group)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(5)
            label = QLabel(tr(f"tracker.stabilizer.{key}", title), group)
            layout.addWidget(label)
            value = WindowValue("", 0.0, 1.0, STABILIZER_DEFAULTS[key],
                                decimals=2, compact=True, parent=group)
            value.setFixedSize(44, 20)
            value._value_label.setMinimumWidth(40)
            value._value_label.setFixedHeight(20)
            value._edit.setFixedHeight(20)
            value._apply_palette_style()
            value.setAccessibleName(title)
            value.valueChanged.connect(self._on_parameter_edited)
            value.interaction_started.connect(self.interaction_started.emit)
            value.interaction_ended.connect(self.interaction_ended.emit)
            value.setProperty("parameterLabel", label)
            self.controls[key] = value
            layout.addWidget(value)
            self._parameter_groups.append(group)
        self.camera_lock_box = QCheckBox(tr("tracker.stabilizer.camera_lock", "Camera Lock"), self.footer)
        self.camera_lock_box.setAccessibleName("Camera Lock")
        self.camera_lock_box.toggled.connect(self._on_parameter_edited)
        self.zoom_box = QCheckBox(tr("tracker.stabilizer.zoom", "Zoom"), self.footer)
        self.zoom_box.setAccessibleName("Zoom")
        self.zoom_box.toggled.connect(self._on_parameter_edited)
        self._parameter_groups.extend((self.camera_lock_box, self.zoom_box))
        self.mode_combo = ChevronComboBox(self.footer)
        self.mode_combo.setAccessibleName("Stabilization Mode")
        self.mode_combo.setMinimumWidth(120)
        self.mode_combo.setMaximumWidth(170)
        self.mode_combo.setFixedHeight(22)
        self.mode_combo.setStyleSheet("""
            QComboBox { background:#202024; border:1px solid #111215; border-radius:2px;
                color:#c0c1c2; font-size:11px; padding:0 22px 0 8px; min-height:18px; }
            QComboBox:disabled { color:#55575b; }
            QComboBox::drop-down { border:0; width:0; }
        """)
        for mode, title in (("perspective", "Perspective"), ("similarity", "Similarity"),
                            ("translation", "Translation")):
            self.mode_combo.addItem(tr(f"tracker.stabilizer.mode_{mode}", title), mode)
        self.mode_combo.currentIndexChanged.connect(self._on_parameter_edited)
        self._parameter_groups.append(self.mode_combo)
        self._reflow_footer()
        return self.footer

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        if hasattr(self, "_footer_grid"):
            self._reflow_footer()

    def _reflow_footer(self) -> None:
        """Wrap complete label/value groups instead of overlapping narrow docks."""
        grid = self._footer_grid
        for column in range(grid.columnCount()):
            grid.setColumnStretch(column, 0)
        while grid.count():
            grid.takeAt(0)
        wide = self.width() >= 740
        margin = 20 if wide else 8
        grid.setContentsMargins(margin, 5, 32 if wide else 8, 5)
        available = max(1, self.width() - margin - (32 if wide else 8))
        row = col = used = 0
        for group in self._parameter_groups:
            required = group.sizeHint().width()
            if group is self.mode_combo:
                required = max(120, required)
            if col and used + 9 + required > available:
                row += 1
                col = used = 0
            alignment = (Qt.AlignmentFlag.AlignRight if group is self.mode_combo
                         else Qt.AlignmentFlag.AlignLeft)
            grid.addWidget(group, row, col, alignment=alignment)
            used += required + (9 if col else 0)
            col += 1
        self._footer_rows = row + 1
        self.footer.setMinimumHeight(10 + self._footer_rows * 24 + max(0, row) * 4)
        grid.setColumnStretch(max(0, col - 1), 1)

    def _on_analyze_clicked(self) -> None:
        if self._running:
            self.cancel_requested.emit()
        elif self._video_available:
            self.analyze_requested.emit()

    def _on_parameter_edited(self, _value: object = None) -> None:
        if self._updating:
            return
        self._apply_state()
        self.params_changed.emit(self.get_params())

    def get_params(self) -> dict[str, Any]:
        return {
            "mode": str(self.mode_combo.currentData()),
            "camera_lock": self.camera_lock_box.isChecked(),
            "zoom": self.zoom_box.isChecked(),
            "bypass": self.bypass_box.isChecked(),
            **{key: value.value() for key, value in self.controls.items()},
        }

    def set_params(self, params: Mapping[str, Any]) -> None:
        """Restore values without recursively creating edits or analysis requests."""
        if not isinstance(params, Mapping):
            return
        self._updating = True
        try:
            mode = params.get("mode")
            if isinstance(mode, str):
                index = self.mode_combo.findData(mode)
                if index >= 0:
                    self.mode_combo.setCurrentIndex(index)
            for key, checkbox in (("camera_lock", self.camera_lock_box),
                                  ("zoom", self.zoom_box), ("bypass", self.bypass_box)):
                value = params.get(key)
                if isinstance(value, bool):
                    checkbox.setChecked(value)
            for key, control in self.controls.items():
                value = params.get(key)
                if isinstance(value, (int, float)) and math.isfinite(value):
                    control.setValue(float(value))
        finally:
            self._updating = False
        self._apply_state()

    def set_video_available(self, available: bool) -> None:
        self._video_available = bool(available)
        self._apply_state()

    def set_running(self, running: bool) -> None:
        self._running = bool(running)
        if not self._running:
            self.status_label.hide()
            self.btn_stabilize.setToolTip("")
        self._apply_state()

    def _apply_state(self) -> None:
        editable = self._video_available and not self._running
        self.bypass_box.setEnabled(editable)
        self.mode_combo.setEnabled(editable)
        self.camera_lock_box.setEnabled(editable)
        self.zoom_box.setEnabled(editable)
        for key, control in self.controls.items():
            enabled = editable and not self.camera_lock_box.isChecked()
            control.setEnabled(enabled)
            label = control.property("parameterLabel")
            if isinstance(label, QLabel):
                label.setEnabled(enabled)
        self.btn_stabilize.setEnabled(self._running or self._video_available)
        self.btn_stabilize.setText(tr("tracker.stabilizer.stop", "Stop") if self._running
                                  else tr("tracker.stabilizer.analyze", "Stabilize"))
        self.btn_stabilize.setAccessibleName("Stop Stabilization" if self._running else "Stabilize")
        self.state_changed.emit()

    def can_reset(self) -> bool:
        return self._video_available or self._running

    def set_progress(self, done: int, total: int) -> None:
        denominator = max(0, int(total))
        numerator = max(0, min(int(done), denominator))
        percent = round(numerator / denominator * 100) if denominator else 0
        text = tr("tracker.stabilizer.progress", "Analyzing {done} / {total} · {percent}%",
                  done=numerator, total=denominator, percent=percent)
        self.status_label.setText(text)
        self.status_label.setVisible(self._running)
        self.btn_stabilize.setToolTip(text)

    def set_motion_series(self, series: dict[str, list[tuple[int, float]]]) -> None:
        self.graph.set_series(series)

    def set_frame_range(self, start: int, end: int) -> None:
        self.graph.set_frame_range(start, end)

    def set_timebase(self, fps: float, start: int = 0) -> None:
        self.graph.set_timebase(fps, start)

    def set_current_frame(self, frame: int) -> None:
        self.graph.set_current_frame(frame)
