# SUPERSEDED(2026-08-19 审计): 已被新实现取代且无生产引用，仅存档。
from PyQt6.QtWidgets import (QWidget, QVBoxLayout,
                             QLabel, QPushButton, QGridLayout)
from PyQt6.QtCore import Qt, pyqtSignal
from src.shared.ui.widgets.param_slider_row import ParamSliderRow

class AdjustmentPanel(QWidget):
    # Signal emits (brightness, contrast, saturation, blur, sharpen)
    values_changed = pyqtSignal(int, int, int, int, int)
    reset_clicked = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.init_ui()

    def init_ui(self) -> None:
        layout = QVBoxLayout()
        layout.setContentsMargins(10, 10, 10, 10)
        self.setLayout(layout)

        group = QWidget()
        grid = QGridLayout()
        grid.setSpacing(10)
        group.setLayout(grid)
        
        # Labels and corresponding sliders
        params = [
            ("Brightness", "Brightness"),
            ("Contrast", "Contrast"),
            ("Saturation", "Saturation"),
            ("Blur", "Blur"),
            ("Sharpen", "Sharpen")
        ]
        
        self.rows = {}
        for i, (display_name, internal_name) in enumerate(params):
            lbl = QLabel(display_name)
            lbl.setFixedWidth(75)
            lbl.setStyleSheet("color: #ccc; font-size: 10px; font-weight: bold;")
            lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            
            row, reset_btn = self.create_param_row(internal_name, -100 if i < 3 else 0, 100, 0)
            self.rows[internal_name] = row
            
            r = i // 2
            col_offset = (i % 2) * 3
            grid.addWidget(lbl, r, col_offset)
            grid.addWidget(row, r, col_offset + 1)
            grid.addWidget(reset_btn, r, col_offset + 2)

        # Set specific references for existing code compatibility
        self.brightness_slider = self.rows["Brightness"]
        self.contrast_slider = self.rows["Contrast"]
        self.saturation_slider = self.rows["Saturation"]
        self.blur_slider = self.rows["Blur"]
        self.sharpen_slider = self.rows["Sharpen"]

        layout.addWidget(group)
        layout.addStretch()

    def create_param_row(self, label, min_val, max_val, default_val):
        row = ParamSliderRow(
            label, float(min_val), float(max_val), float(default_val),
            step=1.0, decimals=0, label_width=0, slider_auto_hide=False)
        row.valueChanged.connect(lambda _: self.on_value_change())
        
        reset_btn = QPushButton("↺")
        reset_btn.setFixedSize(16, 16)
        reset_btn.setStyleSheet("padding: 0; font-size: 10px;")
        reset_btn.clicked.connect(lambda: self.reset_single_param(row))
        
        return row, reset_btn

    def reset_single_param(self, row) -> None:
        row.reset()
        self.on_value_change()

    def on_value_change(self, val=None) -> None:
        b = int(self.brightness_slider.value())
        c = int(self.contrast_slider.value())
        s = int(self.saturation_slider.value())
        bl = int(self.blur_slider.value())
        sh = int(self.sharpen_slider.value())
        self.values_changed.emit(b, c, s, bl, sh)

    def reset_values(self) -> None:
        # Block signals to prevent multiple updates during reset
        self.blockSignals(True)
        for row in self.rows.values():
            row.blockSignals(True)
            row.reset()
        for row in self.rows.values():
            row.blockSignals(False)
        self.blockSignals(False)
        
        # Emit reset signal (parent should discard changes)
        self.reset_clicked.emit()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        ParamSliderRow.apply_visibility_to_all(
            ParamSliderRow.collect_rows(self), self.width())

