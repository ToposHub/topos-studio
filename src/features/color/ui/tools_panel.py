# SUPERSEDED(2026-08-19 审计): 已被新实现取代且无生产引用，仅存档。
# AUD-25708：LutPanel 的创建代码被注释停用后，信号连接与
# update_from_layer 中残留的 lut_panel 引用会让类一旦实例化即抛
# AttributeError；同步注释停用这些引用，保持"存档但可实例化"。
from PyQt6.QtWidgets import QWidget, QTabWidget, QVBoxLayout, QSplitter, QLabel, QFrame
from PyQt6.QtCore import pyqtSignal, Qt, QTimer
import numpy as np

from src.shared.i18n.i18n import I18N
from src.features.color.ui.color_wheel import GradingPanel
from src.features.color.ui.mixer_panel import MixerPanel
from src.features.color.ui.warper_panel import WarperPanel
from src.features.color.ui.blur_sharpen_panel import BlurSharpenPanel
from src.features.color.ui.mask_panel import MaskPanel
from src.shared.ui.widgets.curves_widget import CurvesWidget
from src.features.color.ui.scope_panel import ScopePanel
from src.shared.ui.style_sheet import C

class ToolsPanel(QWidget):
    # Proxy signals to MainWindow
    grading_changed = pyqtSignal(tuple, tuple, tuple, float, float, float, float,
                                float, float, float, float, float, float)
    mixer_changed = pyqtSignal(dict)
    warper_changed = pyqtSignal(object)
    effects_changed = pyqtSignal(object)
    curves_changed = pyqtSignal(np.ndarray)
    mask_shape_added = pyqtSignal(str)
    mask_shape_removed = pyqtSignal(str)
    mask_shape_updated = pyqtSignal(str, dict)
    interaction_started = pyqtSignal()
    interaction_ended = pyqtSignal()
    lut_selected = pyqtSignal(str)
    lut_intensity_changed = pyqtSignal(float)
    lut_reset = pyqtSignal()
    
    reset_clicked = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.init_ui()

    def init_ui(self) -> None:
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(0, 0, 0, 0)
        self.setLayout(main_layout)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setHandleWidth(2)
        self.splitter.setStyleSheet(
            f"QSplitter::handle {{ background: {C.PANEL_DIVIDER}; }}"
        )

        # Common tab style (Beautified with wider tabs)
        tab_style = """
            QTabWidget::pane { 
                border-top: 2px solid #333; 
                background: #181818; 
            }
            QTabBar::tab { 
                background: #222; 
                color: #999;
                padding: 10px 20px; 
                margin-right: 2px;
                min-width: 60px;
                font-size: 11px;
                font-weight: bold;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
            }
            QTabBar::tab:hover {
                background: #2a2a2a;
                color: #eee;
            }
            QTabBar::tab:selected { 
                background: #333; 
                color: #ff8800;
                border-bottom: 2px solid #ff8800;
            }
        """

        # --- 1. Left Panel (40%): Noise Reduction + Color Wheels (Tabbed) ---
        self.left_tabs = QTabWidget()
        self.left_tabs.setMinimumWidth(0) # Allow splitter to compress it to 40%
        self.left_tabs.setStyleSheet(tab_style)
        
        self.noise_reduction_placeholder = self.create_placeholder(I18N.tr("panel.denoise", "降噪"))
        self.left_tabs.addTab(self.noise_reduction_placeholder, I18N.tr("panel.denoise", "降噪"))
        
        self.grading_panel = GradingPanel()
        self.left_tabs.addTab(self.grading_panel, I18N.tr("panel.color_wheel", "色轮"))
        self.left_tabs.setCurrentIndex(1) # Default to Color Wheels
        
        # --- 2. Middle Panel (30%): Curves, Warper, Mixer, Effects, LUT ---
        self.mid_tabs = QTabWidget()
        self.mid_tabs.setMinimumWidth(0)
        self.mid_tabs.setStyleSheet(tab_style)
        
        self.curves_widget = CurvesWidget()
        self.mid_tabs.addTab(self.curves_widget, I18N.tr("panel.curves", "曲线"))
        
        self.warper_panel = WarperPanel()
        self.mid_tabs.addTab(self.warper_panel, I18N.tr("panel.warper", "变形"))
        
        self.mixer_panel = MixerPanel()
        self.mid_tabs.addTab(self.mixer_panel, I18N.tr("panel.mixer", "通道混合"))
        
        self.effects_panel = BlurSharpenPanel()
        self.mid_tabs.addTab(self.effects_panel, I18N.tr("panel.blur_sharpen", "模糊/锐化"))
        
        # self.lut_panel = LutPanel()
        # self.mid_tabs.addTab(self.lut_panel, I18N.tr("panel.lut", "3D LUT"))
        
        # --- 3. Right Panel (30%): Scopes & Mask ---
        self.right_tabs = QTabWidget()
        self.right_tabs.setMinimumWidth(0)
        self.right_tabs.setStyleSheet(tab_style)
        
        self.scope_panel = ScopePanel()
        self.right_tabs.addTab(self.scope_panel, I18N.tr("panel.analysis", "示波器"))

        self.mask_panel = MaskPanel()
        self.right_tabs.addTab(self.mask_panel, I18N.tr("panel.masks", "遮罩"))

        # Add to Main Splitter
        self.splitter.addWidget(self.left_tabs)
        self.splitter.addWidget(self.mid_tabs)
        self.splitter.addWidget(self.right_tabs)
        
        # Set Stretch Factors (45%, 30%, 25%)
        self.splitter.setStretchFactor(0, 45)
        self.splitter.setStretchFactor(1, 30)
        self.splitter.setStretchFactor(2, 25)
        
        main_layout.addWidget(self.splitter)

        # Force initial sizes after layout is established
        QTimer.singleShot(0, self.enforce_ratio)

        # Connect internal signals
        self.grading_panel.grading_changed.connect(self.grading_changed.emit)
        self.grading_panel.interaction_started.connect(self.interaction_started.emit)
        self.grading_panel.interaction_ended.connect(self.interaction_ended.emit)
        self.grading_panel.reset_clicked.connect(self.reset_clicked.emit)

        self.mixer_panel.mixer_changed.connect(self.mixer_changed.emit)
        self.mixer_panel.interaction_started.connect(self.interaction_started.emit)
        self.mixer_panel.interaction_ended.connect(self.interaction_ended.emit)
        self.mixer_panel.reset_clicked.connect(self.reset_clicked.emit)

        self.warper_panel.warper_changed.connect(self.warper_changed.emit)
        self.warper_panel.interaction_started.connect(self.interaction_started.emit)
        self.warper_panel.interaction_ended.connect(self.interaction_ended.emit)
        self.warper_panel.reset_clicked.connect(self.reset_clicked.emit)

        self.effects_panel.effects_changed.connect(self.effects_changed.emit)
        self.effects_panel.interaction_started.connect(self.interaction_started.emit)
        self.effects_panel.interaction_ended.connect(self.interaction_ended.emit)
        self.effects_panel.reset_clicked.connect(self.reset_clicked.emit)

        self.curves_widget.changed.connect(self.curves_changed.emit)
        self.curves_widget.interaction_started.connect(self.interaction_started.emit)
        self.curves_widget.interaction_ended.connect(self.interaction_ended.emit)
        
        # AUD-25708：lut_panel 已停用（创建代码见上方注释块），
        # 其信号透传一并注释，避免 AttributeError。
        # self.lut_panel.lut_selected.connect(self.lut_selected.emit)
        # self.lut_panel.interaction_started.connect(self.interaction_started.emit)
        # self.lut_panel.interaction_ended.connect(self.interaction_ended.emit)

        # self.lut_panel.lut_reset.connect(self.lut_reset.emit)

        self.mask_panel.shape_added.connect(self.mask_shape_added.emit)
        self.mask_panel.shape_removed.connect(self.mask_shape_removed.emit)
        self.mask_panel.shape_updated.connect(self.mask_shape_updated.emit)
        self.mask_panel.interaction_started.connect(self.interaction_started.emit)
        self.mask_panel.interaction_ended.connect(self.interaction_ended.emit)

    def enforce_ratio(self) -> None:
        """Forces the splitter to respect the 45/30/25 ratio."""
        total_w = self.splitter.width()
        if total_w > 0:
            s1 = int(total_w * 0.45)
            s2 = int(total_w * 0.30)
            s3 = total_w - s1 - s2
            self.splitter.setSizes([s1, s2, s3])

    def update_scope(self, image_rgb) -> None:
        """Updates the scope with new image data."""
        self.scope_panel.update_scope(image_rgb)

    def create_placeholder(self, text):
        widget = QFrame()
        widget.setStyleSheet("background: #181818; border: none;")
        layout = QVBoxLayout(widget)
        label = QLabel(f"{text}\n(Panel Placeholder)")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet("color: #444; font-size: 12px; font-weight: bold;")
        layout.addWidget(label)
        return widget

    def reset_all(self) -> None:
        self.grading_panel.reset_all()
        self.mixer_panel.reset_all()
        self.warper_panel.reset_all()
        self.effects_panel.reset_all()
        self.curves_widget.reset()

    def update_from_layer(self, layer) -> None:
        """Updates all UI panels to reflect the data in the given layer."""
        if not layer: return
        
        # Grading
        p = layer.grading_params
        self.grading_panel.set_params(
            p["lift"], p["gamma"], p["gain"], 
            p["exposure"], p["temp"], p["tint"], 
            p["contrast"], p["shadow"], p["saturation"],
            p.get("vibrance", 0), p.get("mid_detail", 0), p.get("hue_shift", 0),
            highlights=p.get("highlights", 0),
        )
        
        # Curves
        if layer.curves_lut is not None:
            self.curves_widget.set_lut(layer.curves_lut)
        else:
            self.curves_widget.reset()
            
        # Mixer
        self.mixer_panel.set_params(layer.mixer_params)
            
        # Effects
        self.effects_panel.set_params_dict(layer.effects_params)
        
        # LUT（AUD-25708：lut_panel 停用，恢复时连同上方信号连接一并启用）

        
        # Warper
        self.warper_panel.set_params(layer.warper_params)
        
        # Mask
        if layer.mask_shapes:
            s = layer.mask_shapes[-1] # Show last shape params for now
            self.mask_panel.set_params(s.feather, s.opacity)
        else:
            self.mask_panel.set_params(50, 1.0)
