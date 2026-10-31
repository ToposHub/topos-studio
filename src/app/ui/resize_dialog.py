from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout,
                             QSpinBox, QCheckBox, QPushButton, QFormLayout)
from src.shared.i18n.i18n import I18N

class ResizeDialog(QDialog):
    def __init__(self, current_width, current_height, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(I18N.tr("resize_dialog.title", "调整图像大小"))
        self.setFixedWidth(300)

        self.aspect_ratio = current_width / current_height if current_height > 0 else 1.0

        layout = QVBoxLayout()
        self.setLayout(layout)

        form_layout = QFormLayout()
        layout.addLayout(form_layout)

        # Width
        self.width_spin = QSpinBox()
        self.width_spin.setRange(1, 10000)
        self.width_spin.setValue(current_width)
        form_layout.addRow(I18N.tr("resize_dialog.width", "宽度 (px):"), self.width_spin)

        # Height
        self.height_spin = QSpinBox()
        self.height_spin.setRange(1, 10000)
        self.height_spin.setValue(current_height)
        form_layout.addRow(I18N.tr("resize_dialog.height", "高度 (px):"), self.height_spin)

        # Aspect Ratio
        self.ratio_check = QCheckBox(I18N.tr("resize_dialog.maintain_aspect", "保持宽高比"))
        self.ratio_check.setChecked(True)
        layout.addWidget(self.ratio_check)

        # Buttons
        btn_layout = QHBoxLayout()
        self.btn_ok = QPushButton(I18N.tr("resize_dialog.ok", "确定"))
        self.btn_cancel = QPushButton(I18N.tr("resize_dialog.cancel", "取消"))

        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_ok)
        btn_layout.addWidget(self.btn_cancel)
        layout.addLayout(btn_layout)
        
        # Connections
        self.btn_ok.clicked.connect(self.accept)
        self.btn_cancel.clicked.connect(self.reject)
        
        self.width_spin.valueChanged.connect(self.on_width_changed)
        self.height_spin.valueChanged.connect(self.on_height_changed)
        
    def on_width_changed(self, new_width):
        if self.ratio_check.isChecked():
            new_height = int(new_width / self.aspect_ratio)
            self.height_spin.blockSignals(True)
            self.height_spin.setValue(new_height)
            self.height_spin.blockSignals(False)
            
    def on_height_changed(self, new_height):
        if self.ratio_check.isChecked():
            new_width = int(new_height * self.aspect_ratio)
            self.width_spin.blockSignals(True)
            self.width_spin.setValue(new_width)
            self.width_spin.blockSignals(False)

    def get_dimensions(self):
        return self.width_spin.value(), self.height_spin.value()
