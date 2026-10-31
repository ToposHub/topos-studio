from typing import Dict, Any, Optional, Tuple, Callable
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
                             QLabel, QComboBox, QPushButton, QCheckBox,
                             QSizePolicy)
from PyQt6.QtCore import Qt, pyqtSignal, QSize, QTimer
from src.shared.i18n.i18n import I18N
from src.features.color.core.denoise_engine import DenoiseEngine
from src.shared.ui.widgets.chevron_combo import ChevronComboBox
from src.shared.ui.widgets.param_slider_row import ParamSliderRow
from src.shared.ui.widgets.toggle_switch import ToggleSwitch
from src.shared.assets.icon_manager import icon_reset, get_icon, IconColors
from src.shared.video.denoise.params import (
    SpatialDenoiseParams as SpatialParams,
    TemporalDenoiseParams as TemporalParams,
    VideoDenoiseParams,
)

# --- 达芬奇 Motion Effects 式标题样式 ---
_PANEL_TITLE_STYLE = (
    "color: #e8e8e8; font-weight: bold; font-size: 12px; letter-spacing: 0.5px;")
_SECTION_TITLE_STYLE = "color: #dddddd; font-weight: bold; font-size: 11px;"
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
    """降噪面板 — 达芬奇 Motion Effects 式两栏视频降噪。

    2026-11 重设计：
    - 经典/AI（图片烘焙式）降噪从 UI 移除：``DenoiseEngine`` 与外部信号
      接线保留，``denoise_requested`` 不再发射，``set_params`` /
      ``set_baked_state`` 保留为兼容空操作（旧工程恢复路径安全跳过）。
    - 面板唯一模式为视频降噪（非破坏、实时、参数直发渲染器）：
      左栏时域（Frames 下拉 0-3，0 = 关闭且整列变灰，达芬奇行为），
      右栏空域（节标题 ToggleSwitch 启用）。
    - 连续数值参数沿用 ParamSliderRow（滑杆 + DragValue 数值框），
      枚举参数用 ChevronComboBox；参数变化经 16ms 节流直发渲染器。

    Signals:
        denoise_requested: 兼容保留，UI 移除烘焙模式后不再发射。
        reset_requested: 请求清除降噪烘焙状态（全部重置时发射）。
        video_denoise_changed: 携带 VideoDenoiseParams.to_dict()，实时下发。
        video_history_reset_requested: 请求重置时域累积历史。
        video_debug_overlay_toggled: 运动置信度调试叠加开关。
    """

    denoise_requested = pyqtSignal(str, dict)  # mode, params（兼容保留，不再发射）
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
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(8, 6, 8, 6)
        main_layout.setSpacing(4)
        self.setLayout(main_layout)

        # === 面板标题行：标题 + 全部重置 ===
        header_layout = QHBoxLayout()
        title_label = QLabel(I18N.tr("denoise.title", "降噪"))
        title_label.setStyleSheet(_PANEL_TITLE_STYLE)
        header_layout.addWidget(title_label)
        header_layout.addStretch()

        self.reset_btn = QPushButton()
        self.reset_btn.setObjectName("reset_btn")
        self.reset_btn.setIcon(icon_reset())
        self.reset_btn.setIconSize(QSize(12, 12))
        self.reset_btn.setFixedSize(16, 16)
        self.reset_btn.setToolTip(I18N.tr(
            "denoise.reset_all_tooltip", "重置全部降噪参数"))
        self.reset_btn.clicked.connect(self.reset_defaults)
        header_layout.addWidget(self.reset_btn)
        main_layout.addLayout(header_layout)

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
        divider.setStyleSheet("background-color: #333333;")
        columns_layout.addWidget(temporal_col, 1)
        columns_layout.addWidget(divider)
        columns_layout.addWidget(spatial_col, 1)
        columns_layout.addStretch()
        main_layout.addLayout(columns_layout)

        # === 底部工具行：调试叠加（运动置信度，开发/调参用） ===
        footer_layout = QHBoxLayout()
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

    def _build_temporal_column(self) -> QWidget:
        """左栏：时域降噪（Frames 0=关闭，达芬奇行为）。"""
        col = QWidget()
        layout = QVBoxLayout(col)
        layout.setContentsMargins(0, 0, 10, 0)
        layout.setSpacing(6)

        # 节头：时域降噪 [⟲ 历史重置] [↺ 节重置]
        header = QHBoxLayout()
        title = QLabel(I18N.tr("denoise.temporal", "时域降噪"))
        title.setStyleSheet(_SECTION_TITLE_STYLE)
        header.addWidget(title)
        header.addStretch()
        header.addWidget(self._make_history_reset_btn())
        header.addWidget(self._make_section_reset_btn(
            I18N.tr("denoise.reset_temporal_tooltip", "重置时域降噪参数"),
            self._reset_temporal_section))
        layout.addLayout(header)

        # Frames：0 = 关闭（此时整列变灰，与达芬奇一致）
        self.temporal_frames = self.create_labeled_combo(
            I18N.tr("denoise.frames", "帧数"), ["0", "1", "2", "3"])
        self.temporal_frames['combo'].setCurrentIndex(0)
        self.temporal_frames['combo'].setSizePolicy(
            QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.temporal_frames['combo'].setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents)
        layout.addLayout(self.temporal_frames['layout'])
        # 紧凑下拉不吸收富余宽度；无 stretch 时 QBoxLayout 会把空隙均分
        # （视觉上内容居中），行尾 stretch 锁定左对齐
        self.temporal_frames['layout'].addStretch()

        # 运动估计（Mo. Est. Type）
        self.temporal_motion = self.create_labeled_combo(
            I18N.tr("denoise.motion_est", "运动估计"),
            [I18N.tr("denoise.motion_fast", "快速"),
             I18N.tr("denoise.motion_balanced", "均衡"),
             I18N.tr("denoise.motion_accurate", "精确")])
        layout.addLayout(self.temporal_motion['layout'])

        layout.addWidget(self._h_separator())

        # 子节头：时域阈值 [↺]
        layout.addLayout(self._make_sub_header(
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
        temporal_grid.setVerticalSpacing(6)
        temporal_grid.addWidget(self.temporal_link_btn, 0, 0, 2, 1)
        temporal_grid.addWidget(self.temporal_luma, 0, 1)
        temporal_grid.addWidget(self.temporal_chroma, 1, 1)
        temporal_grid.addWidget(self.temporal_threshold, 2, 1)
        temporal_grid.addWidget(self._h_separator(), 3, 1)
        temporal_grid.addWidget(self.temporal_blend, 4, 1)
        temporal_grid.addWidget(self.temporal_scene_cut, 5, 1)
        temporal_grid.setColumnStretch(1, 1)
        layout.addLayout(temporal_grid)

        layout.addStretch()
        return col

    def _build_spatial_column(self) -> QWidget:
        """右栏：空域降噪（节标题 ToggleSwitch 启用）。"""
        col = QWidget()
        layout = QVBoxLayout(col)
        layout.setContentsMargins(10, 0, 0, 0)
        layout.setSpacing(6)

        # 节头：[⬤ 开关] 空域降噪 [↺ 节重置]
        header = QHBoxLayout()
        self.spatial_toggle = ToggleSwitch(checked=False)
        header.addWidget(self.spatial_toggle)
        title = QLabel(I18N.tr("denoise.spatial", "空域降噪"))
        title.setStyleSheet(_SECTION_TITLE_STYLE)
        header.addWidget(title)
        header.addStretch()
        header.addWidget(self._make_section_reset_btn(
            I18N.tr("denoise.reset_spatial_tooltip", "重置空域降噪参数"),
            self._reset_spatial_section))
        layout.addLayout(header)

        self.spatial_mode = self.create_labeled_combo(
            I18N.tr("denoise.mode_q", "模式"),
            [I18N.tr("denoise.q_fast", "快速"),
             I18N.tr("denoise.q_better", "较好"),
             I18N.tr("denoise.q_enhanced", "增强")])
        layout.addLayout(self.spatial_mode['layout'])

        self.spatial_radius = self._create_row(
            I18N.tr("denoise.radius", "半径"),
            1.0, 10.0, float(_SPATIAL_DEFAULTS.radius), 1.0, decimals=0)
        layout.addWidget(self.spatial_radius)

        layout.addWidget(self._h_separator())

        # 子节头：空域阈值 [↺]
        layout.addLayout(self._make_sub_header(
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
        spatial_grid.setVerticalSpacing(6)
        spatial_grid.addWidget(self.spatial_link_btn, 0, 0, 2, 1)
        spatial_grid.addWidget(self.spatial_luma, 0, 1)
        spatial_grid.addWidget(self.spatial_chroma, 1, 1)
        spatial_grid.addWidget(self.spatial_threshold, 2, 1)
        spatial_grid.addWidget(self.spatial_detail, 3, 1)
        spatial_grid.setColumnStretch(1, 1)
        layout.addLayout(spatial_grid)

        layout.addStretch()
        return col

    # ------------------------------------------------------------------
    # 小构件工厂
    # ------------------------------------------------------------------

    def _make_section_reset_btn(self, tooltip: str,
                                slot: Callable[[], None]) -> QPushButton:
        """节标题右侧的圆形重置小按钮（#reset_btn 全局样式）。"""
        btn = QPushButton()
        btn.setObjectName("reset_btn")
        btn.setIcon(icon_reset())
        btn.setIconSize(QSize(12, 12))
        btn.setFixedSize(16, 16)
        btn.setToolTip(tooltip)
        btn.clicked.connect(slot)
        return btn

    def _make_history_reset_btn(self) -> QPushButton:
        """时域节头的历史重置小按钮（⟲，与节重置 ↺ 视觉区分）。"""
        btn = QPushButton()
        btn.setObjectName("reset_btn")
        icon = get_icon('fa5s.history', IconColors.WHITE)
        btn.setIcon(icon if not icon.isNull() else icon_reset())
        btn.setIconSize(QSize(12, 12))
        btn.setFixedSize(16, 16)
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
        label.setStyleSheet(_SECTION_TITLE_STYLE)
        header.addWidget(label)
        header.addStretch()
        header.addWidget(self._make_section_reset_btn(reset_tooltip, slot))
        return header

    def _h_separator(self) -> QWidget:
        """栏内水平分隔线（达芬奇子节之间的细分隔）。"""
        line = QWidget()
        line.setFixedHeight(1)
        line.setStyleSheet("background-color: #333333;")
        return line

    def _create_row(self, label: str, min_val: float, max_val: float,
                    default_val: float, step: float,
                    decimals: int = 2) -> ParamSliderRow:
        """创建标准参数行（标签 + 滑杆 + 数值框，标签宽对齐两栏）。"""
        return ParamSliderRow(
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
        lbl.setStyleSheet("color: #bbbbbb; font-size: 11px;")

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
        if self._video_updating:
            return
        self._pending_video_params = self._collect_video_params().to_dict()
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
        self._run_guarded(self._apply_all_defaults)
        self._update_enabled_states()
        self.reset_requested.emit()

    # ==================================================================
    # 兼容保留 API（经典/AI 烘焙降噪 UI 已移除）
    # ==================================================================

    def set_params(self, params: Dict[str, Any]) -> None:
        """兼容空操作：经典/AI 烘焙降噪 UI 已随 2026-11 重设计移除。

        旧工程恢复路径仍可能携带 Basic/AI 降噪参数键；面板不再展示或
        消费这些键，安全忽略以保证恢复流程不中断。视频降噪参数走
        :meth:`set_video_params_dict`。
        """
        return

    def set_baked_state(self, is_baked: bool) -> None:
        """兼容空操作：烘焙降噪状态指示（Apply 按钮）已随 UI 移除删除。

        加载带历史烘焙（source_bake_type == "denoise"）的工程时由
        file_controller 调用；面板已无烘焙 UI，忽略即可。
        """
        return

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
        return self._collect_video_params().to_dict()

    def set_video_params_dict(self, data: Dict[str, Any]) -> None:
        """从项目数据恢复视频降噪参数（不触发信号风暴）。

        Frames 双向映射：enabled=False → 档位 0（关闭）；enabled=True →
        档位 = frames（1-3）。
        """
        if not isinstance(data, dict):
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
