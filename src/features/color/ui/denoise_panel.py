from copy import deepcopy
import math
import os
from typing import Dict, Any, Optional, Tuple, Callable
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
                             QLabel, QComboBox, QPushButton, QCheckBox,
                             QSizePolicy, QGroupBox, QToolButton)
from PyQt6.QtCore import Qt, pyqtSignal, QSize, QTimer
from src.shared.i18n.i18n import I18N
from src.features.color.core.denoise_engine import DenoiseEngine, get_model_tile_config
from src.features.color.ui.panel_chrome import (
    ColorParameterRow, PARAMETER_LABEL_STYLE, PARAMETER_ROW_SPACING, SECTION_TITLE_STYLE,
    create_panel_layout, style_reset_button,
)
from src.shared.ui.style_sheet import C
from src.shared.ui.widgets.chevron_combo import ChevronComboBox
from src.shared.ui.widgets.param_slider_row import ParamSliderRow
from src.shared.ui.widgets.toggle_switch import ToggleSwitch
from src.shared.assets.icon_manager import get_icon, IconColors
from src.shared.video.denoise.params import (
    SpatialDenoiseParams as SpatialParams,
    TemporalDenoiseParams as TemporalParams,
    VideoDenoiseParams,
)

# 行标签统一宽度：需容纳 "Mo. Est. Type" / "Edge Protection"（~80px）
_LABEL_WIDTH = 88
# 达芬奇 Motion Effects 每栏约 310px；这里放宽到 470 适配数值框/中英文，
# 超宽停靠时左对齐不再无限拉伸滑杆
_COLUMN_MAX_WIDTH = 470

# 默认值唯一来源：frozen dataclass 的字段默认值（init / 重置 / 节重置共用，
# 结束了旧版 init 50/30 与 reset 40/40 的漂移）。
_TEMPORAL_DEFAULTS = TemporalParams()
_SPATIAL_DEFAULTS = SpatialParams()


class DenoisePanel(QWidget):
    """图片使用经典/AI 降噪，视频使用 Motion Effects 式时域/空域两栏。

    图片模式复用已有 DenoiseEngine 和宿主的 source-bake 提交入口，不
    创建视频控件。隐藏的视频参数仍可被无损恢复和读取，不参与图片
    UI 的重置或信号发射。视频布局及其 16ms 参数节流保持原有行为。

    Signals:
        denoise_requested: 图片经典/AI 降噪请求，交给宿主异步处理。
        reset_requested: 请求清除降噪烘焙状态（全部重置时发射）。
        video_denoise_changed: 携带 VideoDenoiseParams.to_dict()，实时下发。
        video_history_reset_requested: 请求重置时域累积历史。
        video_debug_overlay_toggled: 运动置信度调试叠加开关。
    """

    denoise_requested = pyqtSignal(str, dict)  # mode, params
    reset_requested = pyqtSignal()
    # M5: 视频降噪（非破坏、实时调整），携带 VideoDenoiseParams.to_dict()
    video_denoise_changed = pyqtSignal(dict)
    video_history_reset_requested = pyqtSignal()
    # M7: 运动置信度调试叠加（video_denoise_debug_overlay）
    video_debug_overlay_toggled = pyqtSignal(bool)

    def __init__(self, parent: Optional[QWidget] = None, *, image_mode: bool = False) -> None:
        super().__init__(parent)
        self.image_mode = image_mode
        self._stored_video_params = VideoDenoiseParams().to_dict()
        self.engine = DenoiseEngine()
        self._image_request_active = False
        self._image_busy_timer = QTimer(self)
        self._image_busy_timer.setInterval(25)
        self._image_busy_timer.timeout.connect(self._poll_image_processing)
        if image_mode:
            self.engine.processing_finished.connect(self._on_image_processing_finished)
            self.engine.error_occurred.connect(self._on_image_processing_finished)
        self._is_interacting = False
        self._video_updating = False
        # AUD-25403: 恢复窗口防递归状态锁（经典/AI UI 已移除，保留供
        # set_params 兼容路径与既有约定使用）
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
        # 初始灰联动：默认 Frames=0 + 空域关 → 两列均变灰（达芬奇默认态）
        self._update_enabled_states()

    # ==================================================================
    # UI 构建
    # ==================================================================

    def init_ui(self) -> None:
        """初始化 UI 布局（达芬奇 Motion Effects 两栏式）。"""
        if self.image_mode:
            self._init_image_ui()
            return
        header_layout, main_layout = create_panel_layout(self, (8, 8, 6), 4)
        main_layout.setContentsMargins(0, 0, 0, 6)
        main_layout.setSpacing(0)

        # === 面板标题行：标题 + 全部重置 ===
        title_label = QLabel(I18N.tr("denoise.title", "降噪"))
        title_label.setStyleSheet(SECTION_TITLE_STYLE)
        header_layout.addWidget(title_label)
        header_layout.addStretch()

        self.reset_btn = QPushButton()
        self.reset_btn.setObjectName("reset_btn")
        style_reset_button(self.reset_btn, global_reset=True)
        self.reset_btn.setToolTip(I18N.tr(
            "denoise.reset_all_tooltip", "重置全部降噪参数"))
        self.reset_btn.clicked.connect(self.reset_defaults)
        header_layout.addWidget(self.reset_btn)

        # === 两栏主体：时域 | 空域（栏间 1px 竖分隔，达芬奇式） ===
        # 栏宽封顶 + 行尾 stretch：超宽停靠时面板左对齐（达芬奇调色板
        # 形态），窄停靠时两栏仍按 1:1 收缩
        columns_layout = QHBoxLayout()
        columns_layout.setSpacing(0)
        temporal_col = self._build_temporal_column()
        temporal_col.setMaximumWidth(_COLUMN_MAX_WIDTH)
        spatial_col = self._build_spatial_column()
        spatial_col.setMaximumWidth(_COLUMN_MAX_WIDTH)
        divider = QWidget()
        divider.setFixedWidth(1)
        divider.setStyleSheet(f"background-color: {C.PANEL_DIVIDER}; border: none;")
        columns_layout.addWidget(temporal_col, 1)
        columns_layout.addWidget(divider)
        columns_layout.addWidget(spatial_col, 1)
        columns_layout.addStretch()
        main_layout.addLayout(columns_layout)
        main_layout.addWidget(self._h_separator())

        # === 底部工具行：调试叠加（运动置信度，开发/调参用） ===
        footer_layout = QHBoxLayout()
        footer_layout.setContentsMargins(8, 4, 8, 0)
        footer_layout.addStretch()
        self.video_debug_overlay_chk = QCheckBox(
            I18N.tr("denoise.debug_overlay", "调试叠加（运动置信度）"))
        self.video_debug_overlay_chk.setStyleSheet(
            "color: #888888; font-size: 10px;")
        self.video_debug_overlay_chk.toggled.connect(
            self.video_debug_overlay_toggled)
        footer_layout.addWidget(self.video_debug_overlay_chk)
        main_layout.addLayout(footer_layout)

        self.setMinimumHeight(285)
        self.setMinimumWidth(460)

    def _init_image_ui(self) -> None:
        """Expose only the still-image backends that DenoiseEngine implements."""
        self.setStyleSheet(f"QGroupBox {{ {SECTION_TITLE_STYLE} }}")
        header, layout = create_panel_layout(self, (8, 8, 6), 6)
        self.mode_combo = ChevronComboBox()
        self.mode_combo.addItem(I18N.tr("denoise.classic", "Classic"), "Basic")
        self.mode_combo.addItem(I18N.tr("denoise.ai", "AI Denoise"), "AI")
        header.addWidget(self.mode_combo)
        header.addStretch()
        self.reset_btn = self._make_section_reset_btn(
            I18N.tr("denoise.reset_tooltip", "Reset Denoise"), self.reset_defaults,
            global_reset=True)
        header.addWidget(self.reset_btn)

        self.classic_group = QGroupBox(I18N.tr("denoise.classic", "Classic"))
        classic = QVBoxLayout(self.classic_group)
        self.luma_control = self._create_row(
            I18N.tr("denoise.luminance", "Luminance"), 0.0, 1.0, 0.05, 0.01)
        self.color_control = self._create_row(
            I18N.tr("denoise.color", "Color"), 0.0, 1.0, 0.1, 0.01)
        classic.addWidget(self.luma_control)
        classic.addWidget(self.color_control)
        layout.addWidget(self.classic_group)

        self.ai_group = QGroupBox(I18N.tr("denoise.ai", "AI Denoise"))
        ai = QVBoxLayout(self.ai_group)
        self.model_control = self.create_labeled_combo(I18N.tr("denoise.model", "Model:"), [])
        ai.addLayout(self.model_control["layout"])
        self.model_status = QLabel()
        self.model_status.setWordWrap(True)
        ai.addWidget(self.model_status)
        self.blend_control = self._create_row(
            I18N.tr("denoise.blend", "Blend"), 0.0, 1.0, 1.0, 0.01)
        ai.addWidget(self.blend_control)
        self.advanced_toggle = QToolButton()
        self.advanced_toggle.setText(I18N.tr("denoise.tiling", "Tiling Options"))
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.advanced_toggle.setArrowType(Qt.ArrowType.RightArrow)
        ai.addWidget(self.advanced_toggle)
        self.tiling_group = QWidget()
        tiling = QVBoxLayout(self.tiling_group)
        self.auto_tiling_combo = self.create_labeled_combo(
            I18N.tr("denoise.auto_tile", "Auto-Tile:"),
            [I18N.tr("denoise.on", "On"), I18N.tr("denoise.off", "Off")])
        self.tile_size_control = self.create_labeled_combo(
            I18N.tr("denoise.tile_size", "Tile Size:"), ["256", "512", "1024", "2048"])
        self.overlap_control = self.create_labeled_combo(
            I18N.tr("denoise.overlap", "Overlap:"), ["16", "32", "64", "128", "256", "384"])
        self.tile_size_control["combo"].setCurrentText("512")
        self.overlap_control["combo"].setCurrentText("64")
        for control in (self.auto_tiling_combo, self.tile_size_control, self.overlap_control):
            tiling.addLayout(control["layout"])
        ai.addWidget(self.tiling_group)
        self.tiling_group.hide()
        self.advanced_toggle.toggled.connect(self._toggle_image_advanced)
        layout.addWidget(self.ai_group)
        self.apply_btn = QPushButton(I18N.tr("denoise.apply", "Apply Denoise"))
        self.apply_btn.setObjectName("gradingApplyBtn")
        self.apply_btn.clicked.connect(self.on_apply_clicked)
        layout.addWidget(self.apply_btn)
        layout.addStretch()
        self.mode_combo.currentIndexChanged.connect(self._update_image_controls)
        self.model_control["combo"].currentIndexChanged.connect(self._on_image_model_changed)
        self.auto_tiling_combo["combo"].currentIndexChanged.connect(self._on_image_model_changed)
        self.refresh_models()
        self._update_image_controls()
        self.setMinimumHeight(220)
        self.setMinimumWidth(280)

    def _toggle_image_advanced(self, checked: bool) -> None:
        self.tiling_group.setVisible(checked)
        self.advanced_toggle.setArrowType(
            Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)

    def _update_image_controls(self, *_args: Any) -> None:
        if self._restoring_params:
            return
        ai_mode = self.mode_combo.currentData() == "AI"
        self.classic_group.setVisible(not ai_mode)
        self.ai_group.setVisible(ai_mode)
        busy = self._image_request_active or self.engine.is_processing()
        for control in (self.reset_btn, self.mode_combo, self.classic_group, self.ai_group):
            control.setEnabled(not busy)
        model_ready = bool(self.model_control["combo"].currentData())
        self.apply_btn.setEnabled(not busy and (not ai_mode or model_ready))
        if busy and not self._image_busy_timer.isActive():
            self._image_busy_timer.start()
        elif not busy:
            self._image_busy_timer.stop()

    def _on_image_processing_finished(self, *_args: Any) -> None:
        """Completion can arrive just before QThread exits; keep that gap locked."""
        self._image_request_active = False
        self._update_image_controls()

    def _poll_image_processing(self) -> None:
        """Observe worker shutdown without blocking the UI thread."""
        if not self.image_mode or self.engine.is_processing():
            return
        self._image_request_active = False
        self._update_image_controls()

    def _on_image_model_changed(self, *_args: Any) -> None:
        if self._restoring_params:
            return
        name = self.model_control["combo"].currentData()
        if name and self.auto_tiling_combo["combo"].currentIndex() == 0:
            defaults = get_model_tile_config(str(name))
            self.tile_size_control["combo"].setCurrentText(str(defaults["tile_size"]))
            self.overlap_control["combo"].setCurrentText(str(defaults["overlap"]))
        self._update_image_controls()

    def refresh_models(self) -> None:
        """Discover installed models; a missing model never starts inference."""
        if not self.image_mode:
            return
        combo = self.model_control["combo"]
        previous = combo.currentData()
        blocked = combo.blockSignals(True)
        try:
            combo.clear()
            models = self.engine.get_available_models()
            for name in models:
                combo.addItem(name, name)
            if not models:
                combo.addItem(I18N.tr("denoise.no_models", "No denoise model installed"), None)
            elif previous in models:
                combo.setCurrentIndex(models.index(previous))
        finally:
            combo.blockSignals(blocked)
        self.model_status.setText("" if models else I18N.tr(
            "denoise.model_missing", "Install an ONNX denoise model in {directory} to use AI Denoise.").format(
                directory=self.engine.model_dir))
        self._on_image_model_changed()

    def on_apply_clicked(self) -> None:
        """Emit a still-image request; video parameters are never involved."""
        if not self.image_mode or self._restoring_params:
            return
        if self._image_request_active or self.engine.is_processing():
            self._update_image_controls()
            return
        mode = self.mode_combo.currentData()
        if mode == "Basic":
            params = {"luma_strength": self.luma_control.value(),
                      "color_strength": self.color_control.value()}
        else:
            model = self.model_control["combo"].currentData()
            if not model or not os.path.isfile(os.path.join(self.engine.model_dir, model)):
                self.refresh_models()
                return
            tile_size = int(self.tile_size_control["combo"].currentText())
            overlap = int(self.overlap_control["combo"].currentText())
            if overlap >= tile_size:
                self.overlap_control["combo"].setCurrentText("64")
                overlap = 64
            params = {"model_path": os.path.join(self.engine.model_dir, model),
                      "blend": self.blend_control.value(),
                      "use_tiling": self.auto_tiling_combo["combo"].currentIndex() == 0,
                      "tile_size": tile_size, "overlap": overlap}
        # Lock reset before dispatch: it must not restore the source while an
        # earlier worker is still allowed to commit its bake into that source.
        self._image_request_active = True
        self._update_image_controls()
        self.denoise_requested.emit(str(mode), params)
        self._image_request_active = self.engine.is_processing()
        self._update_image_controls()

    def _build_temporal_column(self) -> QWidget:
        """左栏：时域降噪（Frames 0=关闭，达芬奇行为）。"""
        col = QWidget()
        layout = QVBoxLayout(col)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        controls, threshold = QVBoxLayout(), QVBoxLayout()
        for group in (controls, threshold):
            group.setContentsMargins(8, 0, 10, 0)
            group.setSpacing(PARAMETER_ROW_SPACING)
        layout.addLayout(controls)

        # 节头：时域降噪 [⟲ 历史重置] [↺ 节重置]
        header = QHBoxLayout()
        title = QLabel(I18N.tr("denoise.temporal", "时域降噪"))
        title.setStyleSheet(SECTION_TITLE_STYLE)
        header.addWidget(title)
        header.addStretch()
        header.addWidget(self._make_history_reset_btn())
        header.addWidget(self._make_section_reset_btn(
            I18N.tr("denoise.reset_temporal_tooltip", "重置时域降噪参数"),
            self._reset_temporal_section))
        controls.addLayout(header)

        # Frames：0 = 关闭（此时整列变灰，与达芬奇一致）
        self.temporal_frames = self.create_labeled_combo(
            I18N.tr("denoise.frames", "帧数"), ["0", "1", "2", "3"])
        self.temporal_frames['combo'].setCurrentIndex(0)
        self.temporal_frames['combo'].setSizePolicy(
            QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.temporal_frames['combo'].setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents)
        controls.addLayout(self.temporal_frames['layout'])
        # 紧凑下拉不吸收富余宽度；无 stretch 时 QBoxLayout 会把空隙均分
        # （视觉上内容居中），行尾 stretch 锁定左对齐
        self.temporal_frames['layout'].addStretch()

        # 运动估计（Mo. Est. Type）
        self.temporal_motion = self.create_labeled_combo(
            I18N.tr("denoise.motion_est", "运动估计"),
            [I18N.tr("denoise.motion_fast", "快速"),
             I18N.tr("denoise.motion_balanced", "均衡"),
             I18N.tr("denoise.motion_accurate", "精确")])
        controls.addLayout(self.temporal_motion['layout'])

        layout.addWidget(self._h_separator())
        layout.addLayout(threshold)

        # 子节头：时域阈值 [↺]
        threshold.addLayout(self._make_sub_header(
            I18N.tr("denoise.temporal_threshold", "时域阈值"),
            I18N.tr("denoise.reset_temporal_threshold_tooltip", "重置时域阈值"),
            self._reset_temporal_threshold))

        self.temporal_luma = self._create_row(
            I18N.tr("denoise.luma", "亮度"),
            0.0, 100.0, _TEMPORAL_DEFAULTS.luma, 1.0, decimals=1)

        self.temporal_chroma = self._create_row(
            I18N.tr("denoise.chroma", "色度"),
            0.0, 100.0, _TEMPORAL_DEFAULTS.chroma, 1.0, decimals=1)

        self.temporal_threshold = self._create_row(
            I18N.tr("denoise.motion_threshold", "运动阈值"),
            0.0, 1.0, _TEMPORAL_DEFAULTS.threshold, 0.01)

        # 历史混合 / 切换灵敏度（时域节内、阈值子节外，与达芬奇 Blend 行同级）
        self.temporal_blend = self._create_row(
            I18N.tr("denoise.blend", "历史混合"),
            0.0, 1.0, _TEMPORAL_DEFAULTS.blend, 0.01)

        self.temporal_scene_cut = self._create_row(
            I18N.tr("denoise.scene_cut", "切换灵敏度"),
            0.0, 1.0, _TEMPORAL_DEFAULTS.scene_cut_sensitivity, 0.01)

        # 栅格布局：链接图标占第 0 列、纵跨亮度/色度两行（达芬奇式栏沟
        # 挂架）；其余行放第 1 列，标签右缘保持对齐
        self.temporal_link_btn = self._make_link_btn()
        temporal_grid = QGridLayout()
        temporal_grid.setContentsMargins(0, 0, 0, 0)
        temporal_grid.setHorizontalSpacing(2)
        temporal_grid.setVerticalSpacing(PARAMETER_ROW_SPACING)
        temporal_grid.addWidget(self.temporal_link_btn, 0, 0, 2, 1)
        temporal_grid.addWidget(self.temporal_luma, 0, 1)
        temporal_grid.addWidget(self.temporal_chroma, 1, 1)
        temporal_grid.addWidget(self.temporal_threshold, 2, 1)
        temporal_grid.addWidget(self._h_separator(), 3, 1)
        temporal_grid.addWidget(self.temporal_blend, 4, 1)
        temporal_grid.addWidget(self.temporal_scene_cut, 5, 1)
        temporal_grid.setColumnStretch(1, 1)
        threshold.addLayout(temporal_grid)

        layout.addStretch()
        return col

    def _build_spatial_column(self) -> QWidget:
        """右栏：空域降噪（节标题 ToggleSwitch 启用）。"""
        col = QWidget()
        layout = QVBoxLayout(col)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        controls, threshold = QVBoxLayout(), QVBoxLayout()
        for group in (controls, threshold):
            group.setContentsMargins(10, 0, 8, 0)
            group.setSpacing(PARAMETER_ROW_SPACING)
        layout.addLayout(controls)

        # 节头：[⬤ 开关] 空域降噪 [↺ 节重置]
        header = QHBoxLayout()
        self.spatial_toggle = ToggleSwitch(checked=False)
        header.addWidget(self.spatial_toggle)
        title = QLabel(I18N.tr("denoise.spatial", "空域降噪"))
        title.setStyleSheet(SECTION_TITLE_STYLE)
        header.addWidget(title)
        header.addStretch()
        header.addWidget(self._make_section_reset_btn(
            I18N.tr("denoise.reset_spatial_tooltip", "重置空域降噪参数"),
            self._reset_spatial_section))
        controls.addLayout(header)

        self.spatial_mode = self.create_labeled_combo(
            I18N.tr("denoise.mode_q", "模式"),
            [I18N.tr("denoise.q_fast", "快速"),
             I18N.tr("denoise.q_better", "较好"),
             I18N.tr("denoise.q_enhanced", "增强")])
        controls.addLayout(self.spatial_mode['layout'])

        self.spatial_radius = self._create_row(
            I18N.tr("denoise.radius", "半径"),
            1.0, 10.0, float(_SPATIAL_DEFAULTS.radius), 1.0, decimals=0)
        controls.addWidget(self.spatial_radius)

        layout.addWidget(self._h_separator())
        layout.addLayout(threshold)

        # 子节头：空域阈值 [↺]
        threshold.addLayout(self._make_sub_header(
            I18N.tr("denoise.spatial_threshold", "空域阈值"),
            I18N.tr("denoise.reset_spatial_threshold_tooltip", "重置空域阈值"),
            self._reset_spatial_threshold))

        self.spatial_luma = self._create_row(
            I18N.tr("denoise.luma", "亮度"),
            0.0, 100.0, _SPATIAL_DEFAULTS.luma, 1.0, decimals=1)

        self.spatial_chroma = self._create_row(
            I18N.tr("denoise.chroma", "色度"),
            0.0, 100.0, _SPATIAL_DEFAULTS.chroma, 1.0, decimals=1)

        self.spatial_threshold = self._create_row(
            I18N.tr("denoise.edge_threshold", "边缘保护"),
            0.0, 1.0, _SPATIAL_DEFAULTS.threshold, 0.01)

        self.spatial_detail = self._create_row(
            I18N.tr("denoise.detail_recovery", "细节恢复"),
            0.0, 1.0, _SPATIAL_DEFAULTS.detail_recovery, 0.01)

        # 与时域对称的达芬奇式亮度/色度链接栏沟
        self.spatial_link_btn = self._make_link_btn()
        spatial_grid = QGridLayout()
        spatial_grid.setContentsMargins(0, 0, 0, 0)
        spatial_grid.setHorizontalSpacing(2)
        spatial_grid.setVerticalSpacing(PARAMETER_ROW_SPACING)
        spatial_grid.addWidget(self.spatial_link_btn, 0, 0, 2, 1)
        spatial_grid.addWidget(self.spatial_luma, 0, 1)
        spatial_grid.addWidget(self.spatial_chroma, 1, 1)
        spatial_grid.addWidget(self.spatial_threshold, 2, 1)
        spatial_grid.addWidget(self.spatial_detail, 3, 1)
        spatial_grid.setColumnStretch(1, 1)
        threshold.addLayout(spatial_grid)

        layout.addStretch()
        return col

    # ------------------------------------------------------------------
    # 小构件工厂
    # ------------------------------------------------------------------

    def _make_section_reset_btn(self, tooltip: str, slot: Callable[[], None],
                                *, global_reset: bool = False) -> QPushButton:
        btn = QPushButton()
        btn.setObjectName("reset_btn")
        style_reset_button(btn, global_reset=global_reset)
        btn.setToolTip(tooltip)
        btn.clicked.connect(slot)
        return btn

    def _make_history_reset_btn(self) -> QPushButton:
        """时域节头的历史重置小按钮（⟲，与节重置 ↺ 视觉区分）。"""
        btn = QPushButton()
        btn.setObjectName("reset_btn")
        style_reset_button(btn, glyph=lambda color: get_icon("fa5s.history", color))
        btn.setToolTip(I18N.tr("denoise.reset_history_tooltip", "重置时域历史"))
        btn.clicked.connect(self.video_history_reset_requested.emit)
        return btn

    def _make_link_btn(self) -> QPushButton:
        """亮度/色度链接开关（达芬奇式）。

        默认绑定（橙色链环）；点击解绑后图标转灰，两行可独立调整。
        再点击重新绑定——不强制对齐现有值，只耦合后续调整（与达芬奇一致）。
        """
        btn = QPushButton()
        btn.setCheckable(True)
        btn.setChecked(True)
        btn.setFixedSize(16, 16)
        btn.setIconSize(QSize(10, 10))
        btn.setIcon(get_icon('fa5s.link', IconColors.ORANGE))
        btn.setToolTip(I18N.tr("denoise.link_luma_chroma", "链接亮度/色度"))
        btn.setStyleSheet(
            "QPushButton { background: transparent; border: none; padding: 0px; }"
            "QPushButton:hover { background: #333333; border-radius: 8px; }")

        def _on_toggled(checked: bool) -> None:
            btn.setIcon(get_icon(
                'fa5s.link',
                IconColors.ORANGE if checked else IconColors.DEFAULT))

        btn.toggled.connect(_on_toggled)
        return btn

    def _propagate_link(self, link_btn: QPushButton,
                        target_row: ParamSliderRow, value: float) -> None:
        """链接态下把源行数值同步到目标行。

        目标行经 ``setValue`` 回填是静默的（不发 valueChanged），因此
        天然无信号递归；回填后需手动并入本次下发，节流器会自动合并。
        """
        if link_btn.isChecked():
            target_row.setValue(value)
            self._emit_video_params()

    def _make_sub_header(self, title: str, reset_tooltip: str,
                         slot: Callable[[], None]) -> QHBoxLayout:
        """子节标题行（达芬奇 "Temporal Threshold" 式）：标题 + [↺]。"""
        header = QHBoxLayout()
        header.setContentsMargins(0, 2, 0, 0)
        label = QLabel(title)
        label.setStyleSheet(SECTION_TITLE_STYLE)
        header.addWidget(label)
        header.addStretch()
        header.addWidget(self._make_section_reset_btn(reset_tooltip, slot))
        return header

    def _h_separator(self) -> QWidget:
        """栏内水平分隔线（达芬奇子节之间的细分隔）。"""
        line = QWidget()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background: {C.PANEL_DIVIDER}; border: none;")
        return line

    def _create_row(self, label: str, min_val: float, max_val: float,
                    default_val: float, step: float,
                    decimals: int = 2) -> ParamSliderRow:
        """创建标准参数行（标签 + 滑杆 + 数值框，标签宽对齐两栏）。"""
        return ColorParameterRow(
            label, min_val, max_val, default_val,
            step=step, decimals=decimals, label_width=_LABEL_WIDTH)

    def create_labeled_combo(self, label: str, items: list) -> Dict[str, Any]:
        """创建带标签的下拉行（标签右对齐、组合框伸展填充栏宽）。

        Args:
            label: 标签文字
            items: 下拉选项列表

        Returns:
            包含 layout, combo 的字典
        """
        layout = QHBoxLayout()
        layout.setSpacing(4)

        lbl = QLabel(label)
        lbl.setFixedWidth(_LABEL_WIDTH)
        lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        lbl.setStyleSheet(PARAMETER_LABEL_STYLE)

        combo = ChevronComboBox()
        combo.addItems(items)
        combo.setMinimumHeight(18)
        combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        combo.view().setMinimumWidth(150)  # 确保下拉列表足够宽

        layout.addWidget(lbl)
        layout.addWidget(combo)

        return {'layout': layout, 'combo': combo}

    # ==================================================================
    # 信号连接与启用状态联动
    # ==================================================================

    def connect_signals(self) -> None:
        """连接内部信号（视频降噪参数实时直发渲染器）。"""
        if self.image_mode:
            return
        # 达芬奇式亮度/色度链接：先于通用发参连接，保证同一 tick 内
        # 目标行已同步、收集到的参数两侧一致
        self.temporal_luma.valueChanged.connect(
            lambda v: self._propagate_link(
                self.temporal_link_btn, self.temporal_chroma, v))
        self.temporal_chroma.valueChanged.connect(
            lambda v: self._propagate_link(
                self.temporal_link_btn, self.temporal_luma, v))
        self.spatial_luma.valueChanged.connect(
            lambda v: self._propagate_link(
                self.spatial_link_btn, self.spatial_chroma, v))
        self.spatial_chroma.valueChanged.connect(
            lambda v: self._propagate_link(
                self.spatial_link_btn, self.spatial_luma, v))
        # Frames 变化 → 灰联动 + 发参（0 = 关闭整列，达芬奇行为）
        self.temporal_frames['combo'].currentIndexChanged.connect(
            self._on_frames_changed)
        self.temporal_motion['combo'].currentIndexChanged.connect(
            self._emit_video_params)
        self.spatial_mode['combo'].currentIndexChanged.connect(
            self._emit_video_params)
        self.spatial_toggle.toggled.connect(self._on_spatial_toggled)
        for row in (self.temporal_luma, self.temporal_chroma,
                    self.temporal_threshold, self.temporal_blend,
                    self.temporal_scene_cut, self.spatial_radius,
                    self.spatial_luma, self.spatial_chroma,
                    self.spatial_threshold, self.spatial_detail):
            row.valueChanged.connect(self._on_video_slider_changed)
        # AUD-25401：交互结束（滑杆/数值框释放）立即 flush 待发参数，
        # 保证最终值不因节流延迟丢失
        for row in (self.temporal_luma, self.temporal_chroma,
                    self.temporal_threshold, self.temporal_blend,
                    self.temporal_scene_cut, self.spatial_radius,
                    self.spatial_luma, self.spatial_chroma,
                    self.spatial_threshold, self.spatial_detail):
            row.interactionEnded.connect(self._flush_pending_video_params)

    def _on_frames_changed(self, _index: int) -> None:
        """Frames 档位变化：刷新整列灰联动并下发参数。"""
        self._update_enabled_states()
        self._emit_video_params()

    def _on_spatial_toggled(self, _checked: bool) -> None:
        """空域开关变化：刷新灰联动并下发参数。"""
        self._update_enabled_states()
        self._emit_video_params()

    def _update_enabled_states(self) -> None:
        """灰联动：时域 Frames=0 整列变灰；空域开关关闭整列变灰。

        与达芬奇 Motion Effects 一致——禁用的行仍可见（保留布局），
        只是变灰且不可交互。ParamSliderRow.setEnabled 会同步滑杆与
        数值框并切换标签/数值格的暗淡样式。
        """
        if self.image_mode:
            return
        temporal_on = self.temporal_frames['combo'].currentIndex() > 0
        for widget in (self.temporal_motion['combo'], self.temporal_luma,
                       self.temporal_chroma, self.temporal_threshold,
                       self.temporal_blend, self.temporal_scene_cut):
            widget.setEnabled(temporal_on)
        spatial_on = self.spatial_toggle.isChecked()
        for widget in (self.spatial_mode['combo'], self.spatial_radius,
                       self.spatial_luma, self.spatial_chroma,
                       self.spatial_threshold, self.spatial_detail):
            widget.setEnabled(spatial_on)

    # ==================================================================
    # 视频降噪参数收集 / 节流下发（AUD-25401 机制保持不变）
    # ==================================================================

    def _on_video_slider_changed(self, _value: float) -> None:
        if self._video_updating:
            return
        self._emit_video_params()

    def _collect_video_params(self) -> VideoDenoiseParams:
        """从控件收集当前视频降噪参数（发射与项目保存共用单一来源）。"""
        if self.image_mode:
            return VideoDenoiseParams.from_dict(self._stored_video_params)
        motion_map = ("fast", "balanced", "accurate")
        mode_map = ("fast", "better", "enhanced")
        # Frames 下拉 0-3：0 = 关闭（enabled=False）；开启时帧深即档位值
        frames_idx = self.temporal_frames['combo'].currentIndex()
        return VideoDenoiseParams(
            temporal=TemporalParams(
                enabled=frames_idx > 0,
                frames=frames_idx if frames_idx > 0 else _TEMPORAL_DEFAULTS.frames,
                luma=self.temporal_luma.value(),
                chroma=self.temporal_chroma.value(),
                motion=motion_map[self.temporal_motion['combo'].currentIndex()],
                threshold=self.temporal_threshold.value(),
                blend=self.temporal_blend.value(),
                scene_cut_sensitivity=self.temporal_scene_cut.value(),
            ),
            spatial=SpatialParams(
                enabled=self.spatial_toggle.isChecked(),
                mode=mode_map[self.spatial_mode['combo'].currentIndex()],
                radius=int(round(self.spatial_radius.value())),
                luma=self.spatial_luma.value(),
                chroma=self.spatial_chroma.value(),
                threshold=self.spatial_threshold.value(),
                detail_recovery=self.spatial_detail.value(),
            ),
        )

    def _emit_video_params(self, *_args) -> None:
        """收集当前视频降噪参数并经 16ms 节流后广播。

        渲染器参数变化会自动重置时域历史，直发（每个拖动 tick 一次）
        会反复重置历史并造成渲染风暴（AUD-25401）。
        """
        if self._video_updating or self.image_mode:
            return
        self._pending_video_params = self._collect_video_params().to_dict()
        if not self._video_param_timer.isActive():
            self._video_param_timer.start(16)

    def _emit_pending_video_params(self) -> None:
        """发射节流窗口内最后一次收集的参数（定时器到期）。"""
        if self._video_updating or self.image_mode:
            return
        if self._pending_video_params is not None:
            pending = self._pending_video_params
            self._pending_video_params = None
            self.video_denoise_changed.emit(pending)

    def _flush_pending_video_params(self) -> None:
        """立即发射待发参数（交互结束时刻）。"""
        if self._video_param_timer.isActive():
            self._video_param_timer.stop()
        self._emit_pending_video_params()

    # ==================================================================
    # 重置（全部 / 时域节 / 时域阈值子节 / 空域节 / 空域阈值子节）
    # ==================================================================

    def _run_guarded(self, apply_fn: Callable[[], None]) -> None:
        """``_video_updating`` 窗口内批量回填控件，结束后节流发射一次。"""
        if self._video_updating:
            return
        self._video_updating = True
        try:
            apply_fn()
        finally:
            self._video_updating = False
        self._emit_video_params()

    def _apply_temporal_threshold_defaults(self) -> None:
        """时域阈值子节 → 默认（亮度/色度/运动阈值）。"""
        self.temporal_luma.setValue(_TEMPORAL_DEFAULTS.luma)
        self.temporal_chroma.setValue(_TEMPORAL_DEFAULTS.chroma)
        self.temporal_threshold.setValue(_TEMPORAL_DEFAULTS.threshold)

    def _apply_temporal_defaults(self) -> None:
        """时域节全部 → 默认（含 Frames=0 关闭）。"""
        self.temporal_frames['combo'].setCurrentIndex(0)
        self.temporal_motion['combo'].setCurrentIndex(0)
        self._apply_temporal_threshold_defaults()
        self.temporal_blend.setValue(_TEMPORAL_DEFAULTS.blend)
        self.temporal_scene_cut.setValue(_TEMPORAL_DEFAULTS.scene_cut_sensitivity)

    def _apply_spatial_threshold_defaults(self) -> None:
        """空域阈值子节 → 默认（亮度/色度/边缘保护/细节恢复）。"""
        self.spatial_luma.setValue(_SPATIAL_DEFAULTS.luma)
        self.spatial_chroma.setValue(_SPATIAL_DEFAULTS.chroma)
        self.spatial_threshold.setValue(_SPATIAL_DEFAULTS.threshold)
        self.spatial_detail.setValue(_SPATIAL_DEFAULTS.detail_recovery)

    def _apply_spatial_defaults(self) -> None:
        """空域节全部 → 默认（含开关关闭）。"""
        # ToggleSwitch.setChecked 不发信号（仅鼠标点击发），联动需显式刷新
        self.spatial_toggle.setChecked(False)
        self.spatial_mode['combo'].setCurrentIndex(0)
        self.spatial_radius.setValue(float(_SPATIAL_DEFAULTS.radius))
        self._apply_spatial_threshold_defaults()

    def _apply_all_defaults(self) -> None:
        """全部控件 → 默认。"""
        self._apply_temporal_defaults()
        self._apply_spatial_defaults()

    def _reset_temporal_section(self) -> None:
        """时域节重置（节头 ↺）。"""
        self._run_guarded(self._apply_temporal_defaults)
        self._update_enabled_states()

    def _reset_temporal_threshold(self) -> None:
        """时域阈值子节重置（子节头 ↺）。"""
        self._run_guarded(self._apply_temporal_threshold_defaults)

    def _reset_spatial_section(self) -> None:
        """空域节重置（节头 ↺）。"""
        self._run_guarded(self._apply_spatial_defaults)
        self._update_enabled_states()

    def _reset_spatial_threshold(self) -> None:
        """空域阈值子节重置（子节头 ↺）。"""
        self._run_guarded(self._apply_spatial_threshold_defaults)

    def reset_defaults(self) -> None:
        """重置全部视频降噪参数到默认值（时域/空域均关闭）。"""
        if self.image_mode:
            if self._image_request_active or self.engine.is_processing():
                self._update_image_controls()
                return
            self.set_params({"mode": "Basic", "luma_strength": 0.05,
                             "color_strength": 0.1, "blend": 1.0,
                             "use_tiling": True, "tile_size": 512, "overlap": 64})
            self.set_baked_state(False)
            self.reset_requested.emit()
            return
        self._run_guarded(self._apply_all_defaults)
        self._update_enabled_states()
        self.reset_requested.emit()

    # ==================================================================
    # 图片参数恢复 / 已应用状态；视频继续兼容忽略旧图片参数
    # ==================================================================

    def set_params(self, params: Dict[str, Any]) -> None:
        """Restore image settings silently without dispatching a processing job."""
        if not self.image_mode or not isinstance(params, dict):
            return
        combos = (self.mode_combo, self.model_control["combo"],
                  self.auto_tiling_combo["combo"], self.tile_size_control["combo"],
                  self.overlap_control["combo"])
        blocked = [combo.blockSignals(True) for combo in combos]
        self._restoring_params = True
        try:
            if params.get("mode") in ("Basic", "AI"):
                self.mode_combo.setCurrentIndex(0 if params["mode"] == "Basic" else 1)
            for key, row in (("luma_strength", self.luma_control),
                             ("color_strength", self.color_control),
                             ("blend", self.blend_control)):
                value = params.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                    row.setValue(float(value))
            if isinstance(params.get("use_tiling"), bool):
                self.auto_tiling_combo["combo"].setCurrentIndex(0 if params["use_tiling"] else 1)
            for key, control, fallback in (("tile_size", self.tile_size_control, "512"),
                                           ("overlap", self.overlap_control, "64")):
                if key in params:
                    text = str(params[key])
                    combo = control["combo"]
                    combo.setCurrentText(text if combo.findText(text) >= 0 else fallback)
            model_path = params.get("model_path")
            if isinstance(model_path, str):
                index = self.model_control["combo"].findData(os.path.basename(model_path))
                if index >= 0:
                    self.model_control["combo"].setCurrentIndex(index)
        finally:
            self._restoring_params = False
            for combo, previous in zip(combos, blocked):
                combo.blockSignals(previous)
        self._update_image_controls()

    def set_baked_state(self, is_baked: bool) -> None:
        """Reflect the existing source-bake state on the still-image Apply button."""
        if self.image_mode:
            self.apply_btn.setText(I18N.tr("denoise.applied", "Denoise Applied") if is_baked
                                   else I18N.tr("denoise.apply", "Apply Denoise"))
            self._update_image_controls()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # 窄面板时自动隐藏滑杆（ParamSliderRow 既有行为）
        ParamSliderRow.apply_visibility_to_all(
            ParamSliderRow.collect_rows(self), self.width())

    # ------------------------------------------------------------------
    # M5: 视频降噪参数读写（项目保存/恢复）
    # ------------------------------------------------------------------

    def get_video_params_dict(self) -> Dict[str, Any]:
        """返回当前面板的视频降噪参数（用于项目保存）。"""
        if self.image_mode:
            return deepcopy(self._stored_video_params)
        return self._collect_video_params().to_dict()

    def set_video_params_dict(self, data: Dict[str, Any]) -> None:
        """从项目数据恢复视频降噪参数（不触发信号风暴）。

        Frames 双向映射：enabled=False → 档位 0（关闭）；enabled=True →
        档位 = frames（1-3）。
        """
        if not isinstance(data, dict):
            return
        if self.image_mode:
            self._video_param_timer.stop()
            self._pending_video_params = None
            self._stored_video_params = deepcopy(data)
            return
        params = VideoDenoiseParams.from_dict(data)
        # 丢弃恢复前残留的节流待发参数，避免旧值在恢复完成后发射
        self._video_param_timer.stop()
        self._pending_video_params = None
        self._video_updating = True
        try:
            frames_idx = params.temporal.frames if params.temporal.enabled else 0
            self.temporal_frames['combo'].setCurrentIndex(
                max(0, min(3, frames_idx)))
            self.temporal_luma.setValue(params.temporal.luma)
            self.temporal_chroma.setValue(params.temporal.chroma)
            motion_map = ("fast", "balanced", "accurate")
            self.temporal_motion['combo'].setCurrentIndex(
                motion_map.index(params.temporal.motion)
                if params.temporal.motion in motion_map else 0)
            self.temporal_threshold.setValue(params.temporal.threshold)
            self.temporal_blend.setValue(params.temporal.blend)
            self.temporal_scene_cut.setValue(params.temporal.scene_cut_sensitivity)

            # ToggleSwitch.setChecked 不发信号，恢复期安全
            self.spatial_toggle.setChecked(params.spatial.enabled)
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
        self._update_enabled_states()
