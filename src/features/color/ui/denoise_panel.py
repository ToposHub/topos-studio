from typing import Dict, Any, Optional
import os
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                             QComboBox, QPushButton, QGroupBox, QCheckBox)
from src.shared.i18n.i18n import I18N
from src.shared.ui.widgets.chevron_combo import ChevronComboBox
from PyQt6.QtCore import Qt, pyqtSignal, QSize, QTimer
from src.features.color.core.denoise_engine import DenoiseEngine, get_model_tile_config
from src.shared.ui.widgets.param_slider_row import ParamSliderRow
from src.shared.assets.icon_manager import icon_reset
from src.shared.video.denoise.params import (
    SpatialDenoiseParams as SpatialParams,
    TemporalDenoiseParams as TemporalParams,
    VideoDenoiseParams,
)


class DenoisePanel(QWidget):
    """
    降噪面板 - Classic / AI / Video 降噪模式

    Features:
        - Classic 模式：亮度/色彩降噪强度滑块（图片，烘焙式）
        - AI 模式：模型选择、自动分块、混合控制（图片，烘焙式）
        - Video 模式：时域 + 空域视频降噪（非破坏、实时、参数直发渲染器）
        - 统一的 UI 风格与 raw_panel.py 一致
    """

    denoise_requested = pyqtSignal(str, dict)  # mode, params
    reset_requested = pyqtSignal()
    # M5: 视频降噪（非破坏、实时调整），携带 VideoDenoiseParams.to_dict()
    video_denoise_changed = pyqtSignal(dict)
    video_history_reset_requested = pyqtSignal()
    # M7: 运动置信度调试叠加（video_denoise_debug_overlay）
    video_debug_overlay_toggled = pyqtSignal(bool)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.engine = DenoiseEngine()
        self._is_interacting = False
        self._video_updating = False
        # AUD-25403: set_params 恢复窗口的防递归状态锁（挂面板 self 的
        # blockSignals 挡不住子控件信号）
        self._restoring_params = False
        # AUD-25401：视频降噪参数发射节流（16ms ≈ 60 FPS），拖动滑杆
        # 时合并渲染请求，避免每个 tick 都触发渲染管线并重置时域历史
        self._pending_video_params: Optional[dict] = None
        self._video_param_timer = QTimer(self)
        self._video_param_timer.setSingleShot(True)
        self._video_param_timer.setInterval(16)
        self._video_param_timer.timeout.connect(self._emit_pending_video_params)
        self.init_ui()
        self.connect_signals()

    def init_ui(self) -> None:
        """初始化 UI 布局"""
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(4, 4, 4, 4)
        main_layout.setSpacing(4)
        self.setLayout(main_layout)

        # === Header: Mode Selection + Reset Button ===
        header_layout = QHBoxLayout()
        
        mode_label = QLabel(I18N.tr("denoise.mode", "模式:"))
        mode_label.setFixedWidth(60)
        mode_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        mode_label.setStyleSheet("color: #bbbbbb; font-size: 11px;")
        
        self.mode_combo = ChevronComboBox()
        self.mode_combo.addItems([
            I18N.tr("denoise.classic", "经典降噪"),
            I18N.tr("denoise.ai", "AI 降噪"),
            I18N.tr("denoise.video", "视频降噪"),
        ])
        self.mode_combo.setMinimumHeight(18)
        self.mode_combo.setMinimumWidth(120)
        self.mode_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.mode_combo.currentIndexChanged.connect(self.on_mode_changed)
        
        header_layout.addWidget(mode_label)
        header_layout.addWidget(self.mode_combo)
        header_layout.addStretch()
        
        # Reset Button
        self.reset_btn = QPushButton()
        self.reset_btn.setObjectName("reset_btn")
        self.reset_btn.setIcon(icon_reset())
        self.reset_btn.setIconSize(QSize(12, 12))
        self.reset_btn.setFixedSize(16, 16)
        self.reset_btn.setToolTip(I18N.tr(
            "denoise.reset_tooltip", "Reset All Denoise Parameters"))
        self.reset_btn.clicked.connect(self.reset_defaults)
        header_layout.addWidget(self.reset_btn)
        
        main_layout.addLayout(header_layout)

        # === Container for dynamic params ===
        self.params_container = QWidget()
        self.params_layout = QVBoxLayout(self.params_container)
        self.params_layout.setContentsMargins(0, 0, 0, 0)
        self.params_layout.setSpacing(4)
        main_layout.addWidget(self.params_container)

        # --- Classic Params Group ---
        self.classic_group = QGroupBox(I18N.tr("denoise.classic", "经典降噪"))
        classic_layout = QVBoxLayout()
        classic_layout.setContentsMargins(4, 0, 4, 4)
        classic_layout.setSpacing(2)
        self.classic_group.setLayout(classic_layout)
        
        from src.shared.config.config_manager import ConfigManager
        _cfg_denoise = ConfigManager().get("default_denoise_strength") or 50
        _luma_default = _cfg_denoise / 1000.0  # config 0-100 maps to 0.0-0.1 luma range
        # AUD-25400：保存为实例属性，reset_defaults 复用同一默认值，
        # 避免重置后偏离用户配置（此前硬编码 0.05 忽略 ConfigManager）
        self._luma_default = _luma_default
        self.luma_control = self._create_row(I18N.tr("denoise.luminance", "亮度"), 0.0, 1.0, _luma_default, 0.01)
        classic_layout.addWidget(self.luma_control)
        
        self.color_control = self._create_row(I18N.tr("denoise.color", "色彩"), 0.0, 1.0, 0.10, 0.01)
        classic_layout.addWidget(self.color_control)
        
        self.params_layout.addWidget(self.classic_group)

        # --- AI Params Group ---
        self.ai_group = QGroupBox(I18N.tr("denoise.ai", "AI 降噪"))
        ai_layout = QVBoxLayout()
        ai_layout.setContentsMargins(4, 0, 4, 4)
        ai_layout.setSpacing(2)
        self.ai_group.setLayout(ai_layout)
        
        # Model Selection
        self.model_control = self.create_labeled_combo(I18N.tr("denoise.model", "模型:"), ["No models found"])
        ai_layout.addLayout(self.model_control['layout'])
        
        # Tiling Options Sub-Group
        tiling_group = QGroupBox(I18N.tr("denoise.tiling", "分块选项"))
        tiling_layout = QVBoxLayout()
        tiling_layout.setContentsMargins(4, 0, 4, 4)
        tiling_layout.setSpacing(2)
        tiling_group.setLayout(tiling_layout)
        
        # Auto-Tiling Toggle (using checkable button style via checkbox)
        self.auto_tiling_combo = self.create_labeled_combo(I18N.tr("denoise.auto_tile", "自动分块:"), ["On", "Off"])
        self.auto_tiling_combo['combo'].setCurrentIndex(0)  # Default: On
        tiling_layout.addLayout(self.auto_tiling_combo['layout'])
        
        # Tile Size + Overlap on same row
        tile_overlap_layout = QHBoxLayout()
        tile_overlap_layout.setSpacing(4)
        
        self.tile_size_control = self.create_labeled_combo(I18N.tr("denoise.tile_size", "分块大小:"), ["256", "512", "1024", "2048"])
        self.tile_size_control['combo'].setCurrentText("512")
        tile_overlap_layout.addLayout(self.tile_size_control['layout'])
        
        self.overlap_control = self.create_labeled_combo(I18N.tr("denoise.overlap", "重叠:"), ["16", "32", "64", "128", "256", "384"])
        self.overlap_control['combo'].setCurrentText("64")
        tile_overlap_layout.addLayout(self.overlap_control['layout'])
        
        tiling_layout.addLayout(tile_overlap_layout)
        
        ai_layout.addWidget(tiling_group)
        
        # Blend Control
        self.blend_control = self._create_row(I18N.tr("denoise.blend", "混合:"), 0.0, 1.0, 1.0, 0.01)
        ai_layout.addWidget(self.blend_control)
        
        self.params_layout.addWidget(self.ai_group)
        self.ai_group.hide()

        # --- Video Params Group (M5: 时域 + 空域，非破坏实时) ---
        self.video_group = QGroupBox(I18N.tr("denoise.video", "视频降噪"))
        video_layout = QVBoxLayout()
        video_layout.setContentsMargins(4, 0, 4, 4)
        video_layout.setSpacing(2)
        self.video_group.setLayout(video_layout)

        # Temporal 子组
        temporal_group = QGroupBox(I18N.tr("denoise.temporal", "时域 (Temporal)"))
        temporal_layout = QVBoxLayout()
        temporal_layout.setContentsMargins(4, 0, 4, 4)
        temporal_layout.setSpacing(2)
        temporal_group.setLayout(temporal_layout)

        self.temporal_enable = self.create_labeled_combo(
            I18N.tr("denoise.enable", "启用:"), ["Off", "On"])
        self.temporal_enable['combo'].setCurrentIndex(0)
        temporal_layout.addLayout(self.temporal_enable['layout'])

        self.temporal_frames = self.create_labeled_combo(
            I18N.tr("denoise.frames", "帧数:"), ["1", "2", "3"])
        self.temporal_frames['combo'].setCurrentIndex(1)
        temporal_layout.addLayout(self.temporal_frames['layout'])

        self.temporal_luma = self._create_row(I18N.tr("denoise.luma", "亮度"), 0.0, 100.0, 50.0, 1.0)
        temporal_layout.addWidget(self.temporal_luma)

        self.temporal_chroma = self._create_row(I18N.tr("denoise.chroma", "色度"), 0.0, 100.0, 30.0, 1.0)
        temporal_layout.addWidget(self.temporal_chroma)

        self.temporal_motion = self.create_labeled_combo(
            I18N.tr("denoise.motion_quality", "运动估计:"),
            [I18N.tr("denoise.motion_fast", "快速"),
             I18N.tr("denoise.motion_balanced", "均衡"),
             I18N.tr("denoise.motion_accurate", "精确")])
        temporal_layout.addLayout(self.temporal_motion['layout'])

        self.temporal_threshold = self._create_row(I18N.tr("denoise.threshold", "阈值"), 0.0, 1.0, 0.5, 0.01)
        temporal_layout.addWidget(self.temporal_threshold)

        self.temporal_blend = self._create_row(
            I18N.tr("denoise.temporal_blend", "历史混合"), 0.0, 1.0, 0.8, 0.01)
        temporal_layout.addWidget(self.temporal_blend)

        self.temporal_scene_cut = self._create_row(
            I18N.tr("denoise.scene_cut", "切换灵敏度"), 0.0, 1.0, 0.5, 0.01)
        temporal_layout.addWidget(self.temporal_scene_cut)

        video_layout.addWidget(temporal_group)

        # Spatial 子组
        spatial_group = QGroupBox(I18N.tr("denoise.spatial", "空域 (Spatial)"))
        spatial_layout = QVBoxLayout()
        spatial_layout.setContentsMargins(4, 0, 4, 4)
        spatial_layout.setSpacing(2)
        spatial_group.setLayout(spatial_layout)

        self.spatial_enable = self.create_labeled_combo(
            I18N.tr("denoise.enable", "启用:"), ["Off", "On"])
        self.spatial_enable['combo'].setCurrentIndex(0)
        spatial_layout.addLayout(self.spatial_enable['layout'])

        self.spatial_mode = self.create_labeled_combo(
            I18N.tr("denoise.mode_q", "模式:"),
            [I18N.tr("denoise.q_fast", "快速"),
             I18N.tr("denoise.q_better", "较好"),
             I18N.tr("denoise.q_enhanced", "增强")])
        spatial_layout.addLayout(self.spatial_mode['layout'])

        self.spatial_radius = self._create_row(I18N.tr("denoise.radius", "半径"), 1.0, 10.0, 3.0, 1.0)
        spatial_layout.addWidget(self.spatial_radius)

        self.spatial_luma = self._create_row(I18N.tr("denoise.luma", "亮度"), 0.0, 100.0, 40.0, 1.0)
        spatial_layout.addWidget(self.spatial_luma)

        self.spatial_chroma = self._create_row(I18N.tr("denoise.chroma", "色度"), 0.0, 100.0, 40.0, 1.0)
        spatial_layout.addWidget(self.spatial_chroma)

        self.spatial_threshold = self._create_row(
            I18N.tr("denoise.edge_threshold", "边缘保护"), 0.0, 1.0, 0.5, 0.01)
        spatial_layout.addWidget(self.spatial_threshold)

        self.spatial_detail = self._create_row(I18N.tr("denoise.detail_recovery", "细节恢复"), 0.0, 1.0, 0.5, 0.01)
        spatial_layout.addWidget(self.spatial_detail)

        video_layout.addWidget(spatial_group)

        # Reset temporal history button
        self.video_reset_history_btn = QPushButton(
            I18N.tr("denoise.reset_history", "重置时域历史"))
        self.video_reset_history_btn.setObjectName("gradingApplyBtn")
        self.video_reset_history_btn.setMinimumHeight(22)
        self.video_reset_history_btn.clicked.connect(
            self.video_history_reset_requested.emit)
        video_layout.addWidget(self.video_reset_history_btn)

        # Debug overlay（运动置信度可视化，开发/调参用）
        self.video_debug_overlay_chk = QCheckBox(
            I18N.tr("denoise.debug_overlay", "调试叠加（运动置信度）"))
        self.video_debug_overlay_chk.toggled.connect(
            self.video_debug_overlay_toggled)
        video_layout.addWidget(self.video_debug_overlay_chk)

        self.params_layout.addWidget(self.video_group)
        self.video_group.hide()

        # === Apply Button ===
        self.apply_btn = QPushButton(I18N.tr("denoise.apply", "应用降噪"))
        self.apply_btn.setObjectName("gradingApplyBtn")  # 使用全局样式中的主按钮样式
        self.apply_btn.setMinimumHeight(28)
        self.apply_btn.clicked.connect(self.on_apply_clicked)
        main_layout.addWidget(self.apply_btn)
        
        # Initial state
        self.refresh_models()
        
        # Set minimum height for compact layout
        self.setMinimumHeight(220)

    def _create_row(self, label, min_val, max_val, default_val, step):
        """Create a ParamSliderRow with standard wiring."""
        row = ParamSliderRow(
            label, min_val, max_val, default_val,
            step=step, decimals=2)
        return row

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
        self.model_control['combo'].currentIndexChanged.connect(self._on_model_changed)
        # M5: 视频降噪实时参数（值变化直发渲染器，无需 Apply）
        self.temporal_enable['combo'].currentIndexChanged.connect(self._emit_video_params)
        self.temporal_frames['combo'].currentIndexChanged.connect(self._emit_video_params)
        self.temporal_luma.valueChanged.connect(self._on_video_slider_changed)
        self.temporal_chroma.valueChanged.connect(self._on_video_slider_changed)
        self.temporal_threshold.valueChanged.connect(self._on_video_slider_changed)
        self.temporal_blend.valueChanged.connect(self._on_video_slider_changed)
        self.temporal_scene_cut.valueChanged.connect(self._on_video_slider_changed)
        self.temporal_motion['combo'].currentIndexChanged.connect(self._emit_video_params)
        self.spatial_enable['combo'].currentIndexChanged.connect(self._emit_video_params)
        self.spatial_mode['combo'].currentIndexChanged.connect(self._emit_video_params)
        self.spatial_radius.valueChanged.connect(self._on_video_slider_changed)
        self.spatial_luma.valueChanged.connect(self._on_video_slider_changed)
        self.spatial_chroma.valueChanged.connect(self._on_video_slider_changed)
        self.spatial_threshold.valueChanged.connect(self._on_video_slider_changed)
        self.spatial_detail.valueChanged.connect(self._on_video_slider_changed)
        # AUD-25401：滑杆释放（交互结束）时立即 flush 待发参数，保证
        # 最终值不因节流延迟丢失
        for row in (self.temporal_luma, self.temporal_chroma,
                    self.temporal_threshold, self.temporal_blend,
                    self.temporal_scene_cut, self.spatial_radius,
                    self.spatial_luma, self.spatial_chroma,
                    self.spatial_threshold, self.spatial_detail):
            row.interactionEnded.connect(self._flush_pending_video_params)

    def _on_video_slider_changed(self, _value: float) -> None:
        if self._video_updating:
            return
        self._emit_video_params()

    def _emit_video_params(self, *_args) -> None:
        """收集当前视频降噪参数并经 16ms 节流后广播。

        渲染器参数变化会自动重置时域历史，直发（每个拖动 tick 一次）
        会反复重置历史并造成渲染风暴（AUD-25401）。
        """
        if self._video_updating:
            return
        motion_map = ("fast", "balanced", "accurate")
        mode_map = ("fast", "better", "enhanced")
        params = VideoDenoiseParams(
            temporal=TemporalParams(
                enabled=self.temporal_enable['combo'].currentIndex() == 1,
                frames=self.temporal_frames['combo'].currentIndex() + 1,
                luma=self.temporal_luma.value(),
                chroma=self.temporal_chroma.value(),
                motion=motion_map[self.temporal_motion['combo'].currentIndex()],
                threshold=self.temporal_threshold.value(),
                blend=self.temporal_blend.value(),
                scene_cut_sensitivity=self.temporal_scene_cut.value(),
            ),
            spatial=SpatialParams(
                enabled=self.spatial_enable['combo'].currentIndex() == 1,
                mode=mode_map[self.spatial_mode['combo'].currentIndex()],
                radius=int(round(self.spatial_radius.value())),
                luma=self.spatial_luma.value(),
                chroma=self.spatial_chroma.value(),
                threshold=self.spatial_threshold.value(),
                detail_recovery=self.spatial_detail.value(),
            ),
        )
        self._pending_video_params = params.to_dict()
        if not self._video_param_timer.isActive():
            self._video_param_timer.start(16)

    def _emit_pending_video_params(self) -> None:
        """发射节流窗口内最后一次收集的参数（定时器到期）。"""
        if self._video_updating:
            return
        if self._pending_video_params is not None:
            pending = self._pending_video_params
            self._pending_video_params = None
            self.video_denoise_changed.emit(pending)

    def _flush_pending_video_params(self) -> None:
        """立即发射待发参数（滑杆释放等交互结束时刻）。"""
        if self._video_param_timer.isActive():
            self._video_param_timer.stop()
        self._emit_pending_video_params()

    def _on_model_changed(self) -> None:
        """Auto-adjust tile_size and overlap when model changes (only when auto-tiling is On)."""
        if self.auto_tiling_combo['combo'].currentIndex() != 0:
            return  # Manual mode — don't override user settings

        model_name = self.model_control['combo'].currentText()
        if model_name.startswith("No models"):
            return

        config = get_model_tile_config(model_name)

        ts_combo = self.tile_size_control['combo']
        ol_combo = self.overlap_control['combo']

        ts_text = str(config["tile_size"])
        ol_text = str(config["overlap"])

        # Set if the value exists in the dropdown
        idx = ts_combo.findText(ts_text)
        if idx >= 0:
            ts_combo.setCurrentIndex(idx)

        idx = ol_combo.findText(ol_text)
        if idx >= 0:
            ol_combo.setCurrentIndex(idx)

    def reset_defaults(self) -> None:
        """重置所有参数到默认值"""
        # Video 模式下重置必须同时复位视频降噪控件并下发全默认参数：
        # 否则面板跳回 Classic，但已下发到 engine/renderer 的时域+空域
        # 参数原样生效，UI 上却再无任何可见开关。
        was_video = self.mode_combo.currentIndex() == 2
        if was_video:
            self._video_updating = True
            try:
                self.temporal_enable['combo'].setCurrentIndex(0)
                self.temporal_frames['combo'].setCurrentIndex(4)  # 5 帧
                self.temporal_luma.setValue(40.0)
                self.temporal_chroma.setValue(40.0)
                self.temporal_motion['combo'].setCurrentIndex(1)
                self.temporal_threshold.setValue(0.5)
                self.temporal_blend.setValue(0.8)
                self.temporal_scene_cut.setValue(0.5)
                self.spatial_enable['combo'].setCurrentIndex(0)
                self.spatial_mode['combo'].setCurrentIndex(0)
                self.spatial_radius.setValue(3.0)
                self.spatial_luma.setValue(40.0)
                self.spatial_chroma.setValue(40.0)
                self.spatial_threshold.setValue(0.5)
                self.spatial_detail.setValue(0.5)
            finally:
                self._video_updating = False
            self.video_denoise_changed.emit(
                VideoDenoiseParams().to_dict())
        self.mode_combo.setCurrentIndex(0)
        # AUD-25400：使用 ConfigManager 推导的默认值（与初始构建一致），
        # 而非硬编码 0.05
        self.luma_control.setValue(getattr(self, '_luma_default', 0.05))
        self.color_control.setValue(0.10)
        self.blend_control.setValue(1.0)
        self.auto_tiling_combo['combo'].setCurrentIndex(0)
        self.tile_size_control['combo'].setCurrentText("512")
        self.overlap_control['combo'].setCurrentText("64")
        self.on_mode_changed(0)
        self.reset_requested.emit()

    def on_mode_changed(self, index: int) -> None:
        """切换 Classic / AI / Video 模式"""
        # AUD-25403: set_params 恢复窗口内忽略子控件触发的模式切换
        # （恢复完成后由 set_params 显式调用一次，避免冗余副作用）。
        if self._restoring_params:
            return
        if index == 2:
            # 视频降噪：非破坏实时，不需要 Apply 烘焙按钮
            self.classic_group.hide()
            self.ai_group.hide()
            self.video_group.show()
            self.apply_btn.hide()
            return
        self.video_group.hide()
        self.apply_btn.show()
        if index == 0:
            self.classic_group.show()
            self.ai_group.hide()
        else:
            self.classic_group.hide()
            self.ai_group.show()
            self.refresh_models()

    def refresh_models(self) -> None:
        """刷新可用模型列表"""
        current_text = self.model_control['combo'].currentText()
        self.model_control['combo'].clear()
        models = self.engine.get_available_models()
        if not models:
            self.model_control['combo'].addItem("No models found")
        else:
            self.model_control['combo'].addItems(models)
            index = self.model_control['combo'].findText(current_text)
            if index >= 0:
                self.model_control['combo'].setCurrentIndex(index)

    def on_apply_clicked(self) -> None:
        """Apply 按钮点击处理"""
        mode = "Basic" if self.mode_combo.currentIndex() == 0 else "AI"
        params: Dict[str, Any] = {}
        
        if mode == "Basic":
            params["luma_strength"] = self.luma_control.value()
            params["color_strength"] = self.color_control.value()
        else:
            model_name = self.model_control['combo'].currentText()
            if model_name.startswith("No models"):
                return
            params["model_path"] = os.path.join("models/denoise", model_name)
            params["blend"] = self.blend_control.value()
            
            # AI Tiling params
            params["use_tiling"] = self.auto_tiling_combo['combo'].currentIndex() == 0
            params["tile_size"] = int(self.tile_size_control['combo'].currentText())
            params["overlap"] = int(self.overlap_control['combo'].currentText())
            
        self.denoise_requested.emit(mode, params)

    def set_params(self, params: Dict[str, Any]) -> None:
        """
        从外部设置参数（用于恢复状态）。

        AUD-25403: 防递归锁从面板 self 改为实例状态锁 ``_restoring_params``
        （``blockSignals(True)`` 挂在 self 上挡不住子控件
        ``mode_combo.currentIndexChanged`` 等信号），恢复期间子控件触发的
        ``on_mode_changed`` / ``_emit_video_params`` 直接忽略；恢复完成后
        仍显式调用一次 ``on_mode_changed``（唯一预期的副作用）。恢复窗口
        内同时对子控件逐一 blockSignals，双保险防信号回声。

        Args:
            params: 参数字典
        """
        if not params:
            return

        child_combos = (
            self.mode_combo,
            self.auto_tiling_combo['combo'],
            self.tile_size_control['combo'],
            self.overlap_control['combo'],
        )
        for combo in child_combos:
            combo.blockSignals(True)
        self._restoring_params = True
        try:
            if 'mode' in params:
                mode_idx = 0 if params['mode'] == 'Basic' else 1
                self.mode_combo.setCurrentIndex(mode_idx)

            if 'luma_strength' in params:
                self.luma_control.setValue(float(params['luma_strength']))

            if 'color_strength' in params:
                self.color_control.setValue(float(params['color_strength']))

            if 'blend' in params:
                self.blend_control.setValue(float(params['blend']))

            if 'use_tiling' in params:
                self.auto_tiling_combo['combo'].setCurrentIndex(0 if params['use_tiling'] else 1)

            if 'tile_size' in params:
                self.tile_size_control['combo'].setCurrentText(str(params['tile_size']))

            if 'overlap' in params:
                self.overlap_control['combo'].setCurrentText(str(params['overlap']))
        finally:
            self._restoring_params = False
            for combo in child_combos:
                combo.blockSignals(False)
        self.on_mode_changed(self.mode_combo.currentIndex())

    def set_baked_state(self, is_baked: bool) -> None:
        """Update UI to reflect baked source state (denoise applied and saved to project)."""
        if is_baked:
            self.apply_btn.setText(I18N.tr("denoise.applied", "已应用降噪 (Baked)"))
            self.apply_btn.setStyleSheet("background-color: #2a5a3a;")
        else:
            self.apply_btn.setText(I18N.tr("denoise.apply", "应用降噪"))
            self.apply_btn.setStyleSheet("")  # Reset to default theme style

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        ParamSliderRow.apply_visibility_to_all(
            ParamSliderRow.collect_rows(self), self.width())

    # ------------------------------------------------------------------
    # M5: 视频降噪参数读写（项目保存/恢复）
    # ------------------------------------------------------------------

    def get_video_params_dict(self) -> Dict[str, Any]:
        """返回当前面板的视频降噪参数（用于项目保存）。"""
        motion_map = ("fast", "balanced", "accurate")
        mode_map = ("fast", "better", "enhanced")
        params = VideoDenoiseParams(
            temporal=TemporalParams(
                enabled=self.temporal_enable['combo'].currentIndex() == 1,
                frames=self.temporal_frames['combo'].currentIndex() + 1,
                luma=self.temporal_luma.value(),
                chroma=self.temporal_chroma.value(),
                motion=motion_map[self.temporal_motion['combo'].currentIndex()],
                threshold=self.temporal_threshold.value(),
                blend=self.temporal_blend.value(),
                scene_cut_sensitivity=self.temporal_scene_cut.value(),
            ),
            spatial=SpatialParams(
                enabled=self.spatial_enable['combo'].currentIndex() == 1,
                mode=mode_map[self.spatial_mode['combo'].currentIndex()],
                radius=int(round(self.spatial_radius.value())),
                luma=self.spatial_luma.value(),
                chroma=self.spatial_chroma.value(),
                threshold=self.spatial_threshold.value(),
                detail_recovery=self.spatial_detail.value(),
            ),
        )
        return params.to_dict()

    def set_video_params_dict(self, data: Dict[str, Any]) -> None:
        """从项目数据恢复视频降噪参数（不触发信号风暴）。"""
        if not isinstance(data, dict):
            return
        params = VideoDenoiseParams.from_dict(data)
        # 丢弃恢复前残留的节流待发参数，避免旧值在恢复完成后发射
        self._video_param_timer.stop()
        self._pending_video_params = None
        self._video_updating = True
        try:
            self.temporal_enable['combo'].setCurrentIndex(
                1 if params.temporal.enabled else 0)
            self.temporal_frames['combo'].setCurrentIndex(
                max(0, min(2, params.temporal.frames - 1)))
            self.temporal_luma.setValue(params.temporal.luma)
            self.temporal_chroma.setValue(params.temporal.chroma)
            motion_map = ("fast", "balanced", "accurate")
            self.temporal_motion['combo'].setCurrentIndex(
                motion_map.index(params.temporal.motion)
                if params.temporal.motion in motion_map else 0)
            self.temporal_threshold.setValue(params.temporal.threshold)
            self.temporal_blend.setValue(params.temporal.blend)
            self.temporal_scene_cut.setValue(params.temporal.scene_cut_sensitivity)

            self.spatial_enable['combo'].setCurrentIndex(
                1 if params.spatial.enabled else 0)
            mode_map = ("fast", "better", "enhanced")
            self.spatial_mode['combo'].setCurrentIndex(
                mode_map.index(params.spatial.mode)
                if params.spatial.mode in mode_map else 0)
            self.spatial_radius.setValue(float(params.spatial.radius))
            self.spatial_luma.setValue(params.spatial.luma)
            self.spatial_chroma.setValue(params.spatial.chroma)
            self.spatial_threshold.setValue(params.spatial.threshold)
            self.spatial_detail.setValue(params.spatial.detail_recovery)
        finally:
            self._video_updating = False
