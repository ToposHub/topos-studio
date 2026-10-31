from PyQt6.QtWidgets import QWidget, QVBoxLayout
from src.shared.ui.widgets.lut_gallery import LutGallery

class LutPanel(QWidget):
    """LUT 选择面板 - 使用全局 QSS 样式"""
    
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.init_ui()

    def init_ui(self) -> None:
        # 使用全局 QSS，无需本地样式表
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        
        # Header (Optional, consistent with other panels if they have one)
        # title = QLabel("3D LUT")
        # title.setStyleSheet("font-weight: bold; color: #ccc; padding: 5px;")
        # layout.addWidget(title)
        
        self.lut_gallery = LutGallery()
        layout.addWidget(self.lut_gallery)

    # Proxy methods for convenience
    def set_selected_lut(self, path) -> None:
        self.lut_gallery.set_selected_lut(path)

    def set_intensity(self, val) -> None:
        self.lut_gallery.set_intensity(val)

    @property
    def lut_selected(self):
        return self.lut_gallery.lut_selected
    
    @property
    def lut_removed(self):
        return self.lut_gallery.lut_removed

    @property
    def intensity_changed(self):
        return self.lut_gallery.intensity_changed

    @property
    def lut_reset(self):
        return self.lut_gallery.lut_reset

    @property
    def decompile_requested(self):
        return self.lut_gallery.decompile_requested

    @property
    def interaction_started(self):
        return self.lut_gallery.interaction_started

    @property
    def interaction_ended(self):
        return self.lut_gallery.interaction_ended
