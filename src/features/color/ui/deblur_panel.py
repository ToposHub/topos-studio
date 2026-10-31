from typing import Dict, Any, Optional
import logging
import os
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                             QComboBox, QPushButton, QGroupBox)
from src.shared.i18n.i18n import I18N
from src.shared.ui.widgets.chevron_combo import ChevronComboBox
from PyQt6.QtCore import Qt, pyqtSignal, QSize
from src.features.color.core.denoise_engine import RestorationEngine
from src.shared.ui.widgets.param_slider_row import ParamSliderRow
from src.shared.assets.icon_manager import icon_reset

logger = logging.getLogger(__name__)


class DeblurPanel(QWidget):
    """
    AI 图像去模糊面板
    
    Features:
        - AI 模型选择 (NAFNet / SwinIR)
        - 自动分块选项 (VRAM 安全)
        - 混合比例控制
        - 统一的 UI 风格与 denoise_panel.py 一致
    """
    
    deblur_requested = pyqtSignal(str, dict)  # mode, params
    reset_requested = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.engine = RestorationEngine(model_subdir="Deblur")
        self.init_ui()
        self.connect_signals()

    def init_ui(self) -> None:
        """初始化 UI 布局"""
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(4, 4, 4, 4)
        main_layout.setSpacing(4)
        self.setLayout(main_layout)

        # === Header: Model Selection + Reset Button ===
        header_layout = QHBoxLayout()
        
        model_label = QLabel(I18N.tr("deblur.model", "模型:"))
        model_label.setFixedWidth(60)
        model_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        model_label.setStyleSheet("color: #bbbbbb; font-size: 11px;")
        
        self.model_combo = ChevronComboBox()
        self.model_combo.setMinimumHeight(18)
        self.model_combo.setMinimumWidth(150)
        self.model_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        
        header_layout.addWidget(model_label)
        header_layout.addWidget(self.model_combo)
        header_layout.addStretch()
        
        # Reset Button
        self.reset_btn = QPushButton()
        self.reset_btn.setObjectName("reset_btn")
        self.reset_btn.setIcon(icon_reset())
        self.reset_btn.setIconSize(QSize(12, 12))
        self.reset_btn.setFixedSize(16, 16)
        self.reset_btn.setToolTip(I18N.tr(
            "deblur.reset_tooltip", "Reset All Deblur Parameters"))
        self.reset_btn.clicked.connect(self.reset_defaults)
        header_layout.addWidget(self.reset_btn)
        
        main_layout.addLayout(header_layout)

        # === AI Params Group ===
        self.ai_group = QGroupBox(I18N.tr("deblur.title", "去模糊"))
        ai_layout = QVBoxLayout()
        ai_layout.setContentsMargins(4, 0, 4, 4)
        ai_layout.setSpacing(2)
        self.ai_group.setLayout(ai_layout)
        
        # Tiling Options Sub-Group
        tiling_group = QGroupBox(I18N.tr("deblur.tiling", "分块选项"))
        tiling_layout = QVBoxLayout()
        tiling_layout.setContentsMargins(4, 0, 4, 4)
        tiling_layout.setSpacing(2)
        tiling_group.setLayout(tiling_layout)
        
        # Auto-Tiling Toggle
        self.auto_tiling_combo = self.create_labeled_combo(I18N.tr("deblur.auto_tile", "自动分块:"), ["On", "Off"])
        self.auto_tiling_combo['combo'].setCurrentIndex(0)  # Default: On
        tiling_layout.addLayout(self.auto_tiling_combo['layout'])
        
        # Tile Size
        self.tile_size_control = self.create_labeled_combo(I18N.tr("deblur.tile_size", "分块大小:"), ["256", "512", "1024", "2048"])
        self.tile_size_control['combo'].setCurrentText("512")
        tiling_layout.addLayout(self.tile_size_control['layout'])
        
        # Overlap
        self.overlap_control = self.create_labeled_combo(I18N.tr("deblur.overlap", "重叠:"), ["16", "32", "64", "128"])
        self.overlap_control['combo'].setCurrentText("64")
        tiling_layout.addLayout(self.overlap_control['layout'])
        
        ai_layout.addWidget(tiling_group)
        
        # Blend Control
        self.blend_control = ParamSliderRow(
            I18N.tr("deblur.blend", "混合:"), 0.0, 1.0, 1.0,
            step=0.01, decimals=2)
        ai_layout.addWidget(self.blend_control)
        
        ai_layout.addStretch()
        main_layout.addWidget(self.ai_group)

        # === Apply Button ===
        self.apply_btn = QPushButton(I18N.tr("deblur.apply", "应用去模糊"))
        self.apply_btn.setObjectName("gradingApplyBtn")  # 使用全局样式中的主按钮样式
        self.apply_btn.setMinimumHeight(28)
        self.apply_btn.clicked.connect(self.on_apply_clicked)
        main_layout.addWidget(self.apply_btn)
        
        main_layout.addStretch()
        
        # Initial state
        self.refresh_models()

    def create_labeled_combo(self, label: str, items: list) -> Dict[str, Any]:
        """
        创建带标签的下拉选择框。
        
        Args:
            label: 标签文字
            items: 下拉选项列表
            
        Returns:
            包含 layout, combo 的字典
        """
        layout = QHBoxLayout()
        layout.setSpacing(4)
        
        lbl = QLabel(label)
        lbl.setFixedWidth(65)
        lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        lbl.setStyleSheet("color: #bbbbbb; font-size: 11px;")
        
        combo = ChevronComboBox()
        combo.addItems(items)
        combo.setMinimumHeight(18)
        combo.view().setMinimumWidth(150)  # 确保下拉列表足够宽
        
        layout.addWidget(lbl)
        layout.addWidget(combo)
        
        return {'layout': layout, 'combo': combo}

    def connect_signals(self) -> None:
        """连接内部信号"""
        pass  # 按钮信号已在 init_ui 中直接连接

    def reset_defaults(self) -> None:
        """重置所有参数到默认值"""
        self.blend_control.setValue(1.0)
        self.auto_tiling_combo['combo'].setCurrentIndex(0)
        self.tile_size_control['combo'].setCurrentText("512")
        self.overlap_control['combo'].setCurrentText("64")
        self.reset_requested.emit()

    def refresh_models(self) -> None:
        """刷新可用模型列表"""
        current_text = self.model_combo.currentText()
        self.model_combo.clear()
        models = self.engine.get_available_models()
        if not models:
            self.model_combo.addItem("No models found")
        else:
            self.model_combo.addItems(models)
            index = self.model_combo.findText(current_text)
            if index >= 0:
                self.model_combo.setCurrentIndex(index)

    def on_apply_clicked(self) -> None:
        """Apply 按钮点击处理"""
        model_name = self.model_combo.currentText()
        if model_name.startswith("No models"):
            return
            
        params: Dict[str, Any] = {}
        params["model_path"] = os.path.join("models/Deblur", model_name)
        params["blend"] = self.blend_control.value()
        
        # AI Tiling params
        params["use_tiling"] = self.auto_tiling_combo['combo'].currentIndex() == 0
        params["tile_size"] = int(self.tile_size_control['combo'].currentText())
        params["overlap"] = int(self.overlap_control['combo'].currentText())
            
        self.deblur_requested.emit("AI", params)

    def set_params(self, params: Dict[str, Any]) -> None:
        """
        从外部设置参数（用于恢复状态）。

        AUD-25316: 非法持久化值不再被 setCurrentText 静默丢弃——findText
        校验，未命中回退默认值并 warning 留痕；blockSignals 挂在面板
        self 上挡不住子控件信号，恢复期间对子控件逐一上锁（当前子控件
        未接线，此保护为未来接线兜底，防信号回声）。

        Args:
            params: 参数字典
        """
        if not params:
            return

        combos = (
            self.auto_tiling_combo['combo'],
            self.tile_size_control['combo'],
            self.overlap_control['combo'],
        )
        for combo in combos:
            combo.blockSignals(True)
        try:
            if 'blend' in params:
                self.blend_control.setValue(float(params['blend']))

            if 'use_tiling' in params:
                self.auto_tiling_combo['combo'].setCurrentIndex(0 if params['use_tiling'] else 1)

            if 'tile_size' in params:
                self._set_combo_text_validated(
                    self.tile_size_control['combo'],
                    str(params['tile_size']), "512", 'tile_size')

            if 'overlap' in params:
                self._set_combo_text_validated(
                    self.overlap_control['combo'],
                    str(params['overlap']), "64", 'overlap')
        finally:
            for combo in combos:
                combo.blockSignals(False)

    @staticmethod
    def _set_combo_text_validated(combo: QComboBox, text: str,
                                  default: str, param_name: str) -> None:
        """按 findText 校验后设置 combo；非法值回退默认并留痕（AUD-25316）。

        非可编辑 QComboBox 上 ``setCurrentText("768")``（不在候选内）是
        无操作，坏数据被静默丢弃——这里显式回退到默认值并记录告警。
        """
        index = combo.findText(text)
        if index >= 0:
            combo.setCurrentIndex(index)
            return
        logger.warning(
            "DeblurPanel.set_params: 非法持久化值 %s=%r 不在候选 %s 内，"
            "回退默认值 %s", param_name, text,
            [combo.itemText(i) for i in range(combo.count())], default)
        fallback = combo.findText(default)
        combo.setCurrentIndex(fallback if fallback >= 0 else 0)

    def set_baked_state(self, is_baked: bool) -> None:
        """Update UI to reflect baked source state (deblur applied and saved to project)."""
        if is_baked:
            self.apply_btn.setText(I18N.tr("deblur.applied", "已应用去模糊 (Baked)"))
            self.apply_btn.setStyleSheet("background-color: #2a5a3a;")
        else:
            self.apply_btn.setText(I18N.tr("deblur.apply", "应用去模糊"))
            self.apply_btn.setStyleSheet("")  # Reset to default theme style

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        ParamSliderRow.apply_visibility_to_all(
            ParamSliderRow.collect_rows(self), self.width())

