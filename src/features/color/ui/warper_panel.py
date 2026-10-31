from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel
from src.shared.i18n.i18n import I18N
from src.shared.ui.widgets.chevron_combo import ChevronComboBox
from PyQt6.QtCore import pyqtSignal, QSize
from src.shared.ui.widgets.color_warper import ColorWarper
from src.shared.assets.icon_manager import icon_reset

class WarperPanel(QWidget):
    # Proxy signals
    warper_changed = pyqtSignal(object)
    interaction_started = pyqtSignal()
    interaction_ended = pyqtSignal()
    reset_clicked = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.init_ui()

    def init_ui(self) -> None:
        layout = QVBoxLayout()
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        self.setLayout(layout)

        # Top Controls
        ctrl_layout = QHBoxLayout()
        ctrl_layout.setSpacing(8)
        
        self.grid_combo = ChevronComboBox()
        self.grid_combo.addItems(["6 × 6", "8 × 8", "12 × 12"])
        self.grid_combo.setFixedHeight(24)
        self.grid_combo.setFixedWidth(80)
        self.grid_combo.currentIndexChanged.connect(self.on_grid_size_changed)
        
        self.reset_btn = QPushButton()
        self.reset_btn.setObjectName("reset_btn")
        self.reset_btn.setIcon(icon_reset())
        self.reset_btn.setIconSize(QSize(12, 12))
        self.reset_btn.setFixedSize(16, 16)
        self.reset_btn.setToolTip(I18N.tr("warper.reset", "Reset Grid"))
        self.reset_btn.setStyleSheet("""
            QPushButton#reset_btn {
                padding: 0px;
                border-radius: 8px;
                background-color: transparent;
                border: 1px solid #333;
            }
            QPushButton#reset_btn:hover {
                background-color: #333;
                border: 1px solid #ff8800;
            }
        """)
        self.reset_btn.clicked.connect(self.on_reset)
        
        density_label = QLabel(I18N.tr("warper.grid_density", "网格密度:"))
        density_label.setStyleSheet("color: #aaa; font-size: 11px;")
        
        ctrl_layout.addWidget(density_label)
        ctrl_layout.addWidget(self.grid_combo)
        ctrl_layout.addStretch()
        ctrl_layout.addWidget(self.reset_btn)
        
        layout.addLayout(ctrl_layout)

        # Warper Widget
        self.warper = ColorWarper()
        self.warper.changed.connect(self.warper_changed.emit)
        self.warper.interaction_started.connect(self.interaction_started.emit)
        self.warper.interaction_ended.connect(self.interaction_ended.emit)
        
        layout.addWidget(self.warper, stretch=1)

    def on_grid_size_changed(self, index) -> None:
        if index == 0:
            size = 6
        elif index == 1:
            size = 8
        else:
            size = 12
        self.warper.blockSignals(True)
        self.warper.set_grid_size(size)
        self.warper.reset()
        self.warper.blockSignals(False)
        self.warper_changed.emit(None)

    def on_reset(self) -> None:
        # Block signals to avoid sending the identity grid via self.warper.reset()
        self.warper.blockSignals(True)
        self.warper.reset()
        self.warper.blockSignals(False)
        # Emit None to tell the engine to bypass the warper entirely
        self.warper_changed.emit(None)
        self.reset_clicked.emit()

    def set_params(self, grid_data, emit=False) -> None:
        """Updates the UI from the given grid data (delta_hue, delta_sat format).

        AUD-25814：改用 ``set_grid_size`` 复位防护（内部会停掉
        ``_changed_timer`` 并清空 ``_pending_deltas``，见
        ColorWarper.reset_grid 的注释），替代此前手工复制的网格重建——
        手工版本漏掉这两步，拖动进行中（16ms 合流窗口内）被 undo/预设
        恢复调用时，陈旧拖动样本会在 blockSignals 解除后经
        ``warper.changed`` 发布并覆盖刚 hydrate 的新状态。
        """
        if grid_data is not None:
            self.warper.blockSignals(True)
            try:
                # set_grid_size = 换尺寸 + reset_grid（含定时器/待发样本清理）
                self.warper.set_grid_size(grid_data.shape[0])
                self.warper.apply_deltas(grid_data)
                self.grid_combo.blockSignals(True)
                if self.warper.grid_size == 6: self.grid_combo.setCurrentIndex(0)
                elif self.warper.grid_size == 8: self.grid_combo.setCurrentIndex(1)
                elif self.warper.grid_size == 12: self.grid_combo.setCurrentIndex(2)
                self.grid_combo.blockSignals(False)
            finally:
                self.warper.blockSignals(False)
            self.warper.update()
            if emit:
                self.warper_changed.emit(grid_data)

    def reset_all(self) -> None:
        self.on_reset()
