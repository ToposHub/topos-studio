"""
HSL Curves Panel for Topos Color.
Provides 6 professional HSL curve editing modes:

1. Lum vs Sat (Luminance → Saturation mapping)
2. Hue vs Hue (Hue shift based on hue)
3. Hue vs Sat (Hue → Saturation mapping)
4. Hue vs Lum (Hue → Luminance mapping)
5. Sat vs Sat (Saturation → Saturation mapping)
6. Sat vs Lum (Saturation → Luminance mapping)
"""
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QScrollArea
from PyQt6.QtCore import Qt, pyqtSignal
from src.shared.ui.widgets.hsl_curves_widget import HSLCurvesWidget


class HSLCurvesPanel(QWidget):
    """
    Panel wrapper for HSL Curves widget.
    Provides a clean interface for integration into the main window.
    """

    # Forward HSL picker signals
    hsl_picker_toggled = pyqtSignal(bool)  # True = activate picker, False = deactivate

    def __init__(self, engine=None) -> None:
        super().__init__()
        self.engine = engine
        self.hsl_curves_widget = None
        self.init_ui()

    def init_ui(self) -> None:
        """Initialize the panel UI."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Create HSL curves widget
        self.hsl_curves_widget = HSLCurvesWidget(self)
        self.hsl_curves_widget.setStyleSheet("""
            HSLCurvesWidget {
                background-color: #1a1a1d;
            }
        """)

        # Wrap in scroll area for small screens
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.hsl_curves_widget)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("""
            QScrollArea {
                background-color: #1a1a1d;
                border: none;
            }
            QScrollBar:vertical {
                background-color: rgba(255, 255, 255, 10);
                width: 8px;
                border-radius: 4px;
            }
            QScrollBar::handle:vertical {
                background-color: rgba(255, 255, 255, 30);
                border-radius: 4px;
                min-height: 20px;
            }
            QScrollBar::handle:vertical:hover {
                background-color: rgba(255, 255, 255, 50);
            }
        """)

        layout.addWidget(scroll)

        # Connect signals
        # AUD-25418: 面板层不再连接空转发槽——实际链路是 main_window 直接
        # 连接 hsl_curves_widget.changed；面板级的转发信号并不存在，原空槽
        # 已随本修复删除（每曲线变更空跑一次槽调用属于死代码）。
        self.hsl_curves_widget.hsl_picker_toggled.connect(self.hsl_picker_toggled)

    def get_curves_data(self):
        """Get current curve data for saving."""
        if self.hsl_curves_widget:
            return self.hsl_curves_widget.get_curves_data()
        return {}

    def set_curves_data(self, data) -> None:
        """Restore curve data from saved state."""
        if self.hsl_curves_widget and data:
            self.hsl_curves_widget.set_curves_data(data)

    def reset(self, emit_changed: bool = False) -> None:
        """Reset all curves to default."""
        if self.hsl_curves_widget:
            self.hsl_curves_widget.reset_all_curves(emit_changed=emit_changed)

    def add_point_from_color(self, r: float, g: float, b: float) -> bool:
        """Add a control point on current curve based on RGB color."""
        if self.hsl_curves_widget:
            return self.hsl_curves_widget.add_point_from_color(r, g, b)
        return False

    def deactivate_picker(self) -> None:
        """Deactivate the HSL picker mode."""
        if self.hsl_curves_widget:
            self.hsl_curves_widget.deactivate_picker()

    def is_picker_active(self) -> bool:
        """Check if HSL picker is currently active."""
        if self.hsl_curves_widget:
            return self.hsl_curves_widget.is_picker_active()
        return False
