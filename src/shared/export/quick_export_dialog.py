"""
快速导出对话框 — UI 风格与 Batch Export 面板统一。

支持两种导出模式：
- 图像导出：快速导出（缓存）或高质量导出（完整渲染）
- LUT 导出：将调色参数导出为 3D LUT 文件

视觉与交互与 src/shared/export/export_panel.py 完全一致：
- 每个分组 = 一个可折叠 ExportSectionWidget（▼ + 标题 + 重置按钮）
- 数值参数用 DragValue（拖拽调值 + 双击输入 + 滚轮 + 中键重置）
- 下拉参数用 ChevronComboBox（自绘 chevron + Fusion 弹出）
- 百分比/范围参数用 ToposClickableSlider（stretch=1 自适应）
- 布尔开关用 ToggleSwitch（药丸开关）
- 文本输入框套 #232325 背景 + #151517 边框（与下拉框统一）
"""
import os

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QLineEdit, QFileDialog, QWidget,
    QStackedWidget, QTabBar,
    QSpinBox,
)
from PyQt6.QtCore import Qt

from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import (
    C,
    apply_combo_dropdown_style,
)
from src.shared.ui.widgets import ChevronComboBox, DragValue, ToggleSwitch
from src.shared.ui.widgets.topos_controls import ToposClickableSlider
# 复用 export_panel 的 section 外壳 + 文本框/按钮样式 helper + 点击失焦（同一套视觉）
from src.shared.export.export_panel import (
    ExportSectionWidget,
    _make_param_label,
    _apply_text_input_style,
    _apply_compact_button_style,
    install_click_defocus,
    _VALUE_FIXED_WIDTH,
)
# AUD-33301：容器-编码兼容矩阵以 deliver 注册表为单一真相源（模块仅
# 依赖 typing，无环；shared→features 延迟引用先例见 shared/ui/audio_mixer）
from src.features.deliver.core.video_export_formats import (
    allowed_codecs_for_container,
)


class QuickExportDialog(QDialog):
    """
    快速导出对话框

    支持两种导出模式：
    - 图像导出：快速导出（缓存）或高质量导出（完整渲染）
    - LUT导出：将调色参数导出为3D LUT文件
    """

    # 导出类型常量
    EXPORT_IMAGE = "image"
    EXPORT_LUT = "lut"
    EXPORT_VIDEO = "video"

    def __init__(self, parent=None, default_dir="", default_name="untitled",
                 original_size=(0, 0), cache_size=(0, 0), has_grading=False,
                 default_format="JPEG", default_quality=85,
                 has_composite_output=False, timeline_info=None,
                 *, initial_export_type: str = "image",
                 work_mode: str = "all") -> None:
        super().__init__(parent)
        self.setWindowTitle(I18N.tr("export.quick_title", "导出"))
        self.setMinimumWidth(520)
        self.default_dir = default_dir
        self.default_name = default_name
        self.orig_w, self.orig_h = original_size
        self.cache_w, self.cache_h = cache_size
        self.has_grading = has_grading  # 是否有调色效果（用于LUT导出提示）
        self._default_format = default_format
        self._default_quality = default_quality
        self._has_composite_output = has_composite_output
        self._initial_export_type: str = initial_export_type
        # Work mode ("image"/"video") gates which export tabs are available so
        # the dialog matches the active workflow. Default to video.
        self._work_mode = work_mode if work_mode in ("image", "video") else "video"

        # 时间线信息（视频导出页面用）
        # {
        #   has_timeline: bool, duration_frames: int,
        #   has_range: bool, range_start: int, range_end: int,
        #   width: int, height: int, fps: float,
        # }
        self._timeline_info = timeline_info or {}
        self._has_timeline = bool(self._timeline_info.get("has_timeline", False))

        # 快速导出最大分辨率（使用缓存尺寸）
        self.max_quick_w = max(self.cache_w, 1)
        self.max_quick_h = max(self.cache_h, 1)

        # 当前导出类型
        self.export_type = self.EXPORT_IMAGE

        self.setup_ui()
        self.apply_styles()
        self.on_format_changed(self.format_combo.currentText())
        self.update_export_mode()
        # 视频页面初始化：触发一次容器-编码联动（即使 Tab 被禁用，
        # 内部控件仍需初始化正确状态）
        self.on_video_format_changed(self.video_format_combo.currentText())
        self._apply_initial_export_type()
        # 点击对话框空白区域时让输入框失焦（退出选中态）
        install_click_defocus(self)

    def setup_ui(self):
        # Main layout with margins
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(16, 16, 16, 16)
        self.main_layout.setSpacing(10)

        # === 导出类型切换（TabBar 保留，但配色统一到主题橙） ===
        self.tab_bar = QTabBar()
        self.tab_bar.addTab(I18N.tr("export.tab_image", "图像导出"))
        self.tab_bar.addTab(I18N.tr("export.tab_lut", "LUT导出"))
        self.tab_bar.addTab(I18N.tr("export.tab_video", "视频导出"))
        self.tab_bar.setCurrentIndex(0)
        # Tab availability is driven by work mode and timeline availability:
        # - Image mode: video export is never available (no timeline workflow).
        # - Video mode: image export is hidden so users stay in the video flow;
        #   video export is available when there is a timeline to render.
        # - All mode: video export follows timeline availability (legacy).
        video_tab_enabled = self._has_timeline and self._work_mode != "image"
        if not video_tab_enabled:
            # QTabBar 没有 model()，用 setTabEnabled 直接禁用
            self.tab_bar.setTabEnabled(2, False)
        if self._work_mode == "video":
            # 纯视频模式下隐藏图像导出 Tab，保持工作流一致。
            # LUT 导出保留：视频调色项目导出 .cube/.3dl LUT 是标准交付物
            # (给摄影机/现场 DIT/套底流程)。
            self.tab_bar.setTabEnabled(0, False)
        self.tab_bar.currentChanged.connect(self.on_tab_changed)
        self.main_layout.addWidget(self.tab_bar)

        # === 内容区域（使用StackedWidget切换） ===
        self.content_stack = QStackedWidget()

        # --- 图像导出页面 ---
        self.image_page = QWidget()
        self.setup_image_page()
        self.content_stack.addWidget(self.image_page)

        # --- LUT导出页面 ---
        self.lut_page = QWidget()
        self.setup_lut_page()
        self.content_stack.addWidget(self.lut_page)

        # --- 视频导出页面 ---
        self.video_page = QWidget()
        self.setup_video_page()
        self.content_stack.addWidget(self.video_page)

        self.main_layout.addWidget(self.content_stack)

        # === Action Buttons ===
        button_layout = QHBoxLayout()
        button_layout.setSpacing(12)
        self.cancel_btn = QPushButton(I18N.tr("export.cancel", "取消"))
        self.cancel_btn.setMinimumHeight(34)
        self.cancel_btn.clicked.connect(self.reject)
        self.export_btn = QPushButton(I18N.tr("export.export", "导出"))
        self.export_btn.setObjectName("PrimaryButton")
        self.export_btn.setDefault(True)
        self.export_btn.setMinimumHeight(34)
        self.export_btn.clicked.connect(self.accept)
        button_layout.addStretch()
        button_layout.addWidget(self.cancel_btn)
        button_layout.addWidget(self.export_btn)
        self.main_layout.addLayout(button_layout)

    # ================================================================
    # 图像导出页面
    # ================================================================

    def setup_image_page(self):
        """设置图像导出页面（用 ExportSectionWidget 分组）。"""
        page_layout = QVBoxLayout(self.image_page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(6)

        # ── 1. File & Location section ──
        file_section = ExportSectionWidget(I18N.tr("export.output_section", "File & Location"), header_mode="none")
        file_layout = file_section.content_layout()

        # 文件名行
        name_row = QHBoxLayout()
        name_row.setContentsMargins(0, 2, 0, 2)
        name_row.setSpacing(4)
        name_label = _make_param_label(I18N.tr("export.file_name", "文件名"))
        name_row.addWidget(name_label)
        self.name_input = QLineEdit(self.default_name)
        self.name_input.setPlaceholderText(I18N.tr("export.file_name", "文件名"))
        _apply_text_input_style(self.name_input)
        name_row.addWidget(self.name_input, 1)
        file_layout.addLayout(name_row)

        # 路径行
        path_row = QHBoxLayout()
        path_row.setContentsMargins(0, 2, 0, 2)
        path_row.setSpacing(4)
        path_label = _make_param_label(I18N.tr("export.location", "位置:"))
        path_row.addWidget(path_label)
        self.path_input = QLineEdit(self.default_dir)
        self.path_input.setPlaceholderText(I18N.tr("export.select_dir_placeholder", "选择导出目录..."))
        _apply_text_input_style(self.path_input)
        path_row.addWidget(self.path_input, 1)
        self.path_btn = QPushButton(I18N.tr("export.choose_btn", "选择..."))
        self.path_btn.clicked.connect(self.pick_directory)
        _apply_compact_button_style(self.path_btn)
        path_row.addWidget(self.path_btn)
        file_layout.addLayout(path_row)

        page_layout.addWidget(file_section)

        # ── 2. Export Source section ──
        source_section = ExportSectionWidget(I18N.tr("export.source", "Export Source"), header_mode="none")
        source_layout = source_section.content_layout()

        source_row = QHBoxLayout()
        source_row.setContentsMargins(0, 2, 0, 2)
        source_row.setSpacing(4)
        source_param_label = _make_param_label(I18N.tr("export.source", "Export Source:"))
        source_row.addWidget(source_param_label)
        self.source_combo = ChevronComboBox()
        self.source_combo.addItem(I18N.tr("export.source_color", "Color Output"))
        self.source_combo.addItem(I18N.tr("export.source_composite", "Composite Output"))
        if self._has_composite_output:
            self.source_combo.setCurrentIndex(1)  # Default to Composite when available
        else:
            # 无合成输出时禁用 Composite 项
            model = self.source_combo.model()
            item = model.item(1)
            if item is not None:
                item.setEnabled(False)
        apply_combo_dropdown_style(self.source_combo)
        source_row.addWidget(self.source_combo, 1)
        source_layout.addLayout(source_row)

        page_layout.addWidget(source_section)

        # ── 3. Resolution section ──
        res_section = ExportSectionWidget(I18N.tr("export.resolution_section", "Resolution"), header_mode="none")
        res_layout = res_section.content_layout()

        # 分辨率信息显示
        info_row = QHBoxLayout()
        info_row.setContentsMargins(0, 2, 0, 2)
        info_row.setSpacing(4)
        self.orig_size_label = QLabel(
            f"{I18N.tr('export.original_size', '原始尺寸')}: {self.orig_w} × {self.orig_h}"
        )
        self.orig_size_label.setStyleSheet(f"color: {C.TEXT_HINT}; font-size: 10px;")
        self.cache_size_label = QLabel(
            f"{I18N.tr('export.quick_max', '快速导出最大')}: {self.max_quick_w} × {self.max_quick_h}"
        )
        self.cache_size_label.setStyleSheet(f"color: {C.CHANNEL_G}; font-size: 10px;")
        info_row.addWidget(self.orig_size_label)
        info_row.addStretch()
        info_row.addWidget(self.cache_size_label)
        res_layout.addLayout(info_row)

        # 自定义分辨率开关（ToggleSwitch）
        custom_row = QHBoxLayout()
        custom_row.setContentsMargins(0, 2, 0, 2)
        custom_row.setSpacing(4)
        custom_label = _make_param_label(I18N.tr("export.custom_resolution", "自定义分辨率"))
        custom_row.addWidget(custom_label)
        custom_row.addStretch()
        self.resize_check = ToggleSwitch(checked=False)
        self.resize_check.toggled.connect(self.update_export_mode)
        custom_row.addWidget(self.resize_check)
        res_layout.addLayout(custom_row)

        # 自定义分辨率控件区（resize_widget 包裹，开关关闭时禁用）
        self.resize_widget = QWidget()
        resize_controls = QVBoxLayout(self.resize_widget)
        resize_controls.setContentsMargins(0, 0, 0, 0)
        resize_controls.setSpacing(4)

        # 默认使用原图尺寸
        default_w = self.orig_w if self.orig_w > 0 else self.max_quick_w
        default_h = self.orig_h if self.orig_h > 0 else self.max_quick_h

        # 宽度行：[label] [DragValue 64px] [lock] [label] [DragValue 64px]
        dim_row = QHBoxLayout()
        dim_row.setContentsMargins(0, 2, 0, 2)
        dim_row.setSpacing(4)
        width_label = _make_param_label(I18N.tr("export.resize_width", "宽度") + ":")
        dim_row.addWidget(width_label)
        # width_spin 改名指向 DragValue（维持属性名契约）
        self.width_spin = DragValue("", min_v=1, max_v=65535, default=default_w, decimals=0, suffix=" W")
        self.width_spin.setFixedValueWidth(_VALUE_FIXED_WIDTH)
        self.width_spin.valueChanged.connect(self.on_resolution_changed)
        dim_row.addWidget(self.width_spin)

        # 锁定宽高比（保留 QCheckBox，全局主题样式；放在两个 DragValue 之间）
        self.lock_ratio = ToggleSwitch(checked=True)
        self.lock_ratio.setToolTip(I18N.tr("export.lock_aspect", "锁定宽高比"))
        dim_row.addWidget(self.lock_ratio)

        height_label = _make_param_label(I18N.tr("export.resize_height", "高度") + ":")
        dim_row.addWidget(height_label)
        self.height_spin = DragValue("", min_v=1, max_v=65535, default=default_h, decimals=0, suffix=" H")
        self.height_spin.setFixedValueWidth(_VALUE_FIXED_WIDTH)
        self.height_spin.valueChanged.connect(self.on_resolution_changed)
        dim_row.addWidget(self.height_spin)
        dim_row.addStretch()
        resize_controls.addLayout(dim_row)

        res_layout.addWidget(self.resize_widget)

        # 导出模式提示
        self.mode_label = QLabel()
        self.mode_label.setStyleSheet("font-size: 10px; padding: 4px 6px; border-radius: 4px;")
        res_layout.addWidget(self.mode_label)

        page_layout.addWidget(res_section)

        # ── 4. Format & Options section ──
        format_section = ExportSectionWidget(I18N.tr("export.format", "格式"), header_mode="none")
        # options_layout 直接复用 section 的 content_layout
        self.options_layout = format_section.content_layout()

        # 格式行
        format_row = QHBoxLayout()
        format_row.setContentsMargins(0, 2, 0, 2)
        format_row.setSpacing(4)
        format_param_label = _make_param_label(I18N.tr("export.format", "格式:"))
        format_row.addWidget(format_param_label)
        self.format_combo = ChevronComboBox()
        self.format_combo.addItems([
            I18N.tr("export.fmt_jpeg", "JPEG"),
            I18N.tr("export.fmt_png", "PNG"),
            I18N.tr("export.fmt_exr", "OpenEXR"),
            I18N.tr("export.fmt_tiff", "TIFF"),
            I18N.tr("export.fmt_webp", "WebP"),
            I18N.tr("export.fmt_toos", "TOOS (.toos)"),
        ])
        # Apply default format from preferences（internal name → display text）
        name_to_display = {
            "JPEG": I18N.tr("export.fmt_jpeg", "JPEG"),
            "PNG": I18N.tr("export.fmt_png", "PNG"),
            "OpenEXR": I18N.tr("export.fmt_exr", "OpenEXR"),
            "TIFF": I18N.tr("export.fmt_tiff", "TIFF"),
            "WebP": I18N.tr("export.fmt_webp", "WebP"),
        }
        display = name_to_display.get(self._default_format, self._default_format)
        idx = self.format_combo.findText(display)
        if idx >= 0:
            self.format_combo.setCurrentIndex(idx)
        self.format_combo.currentTextChanged.connect(self.on_format_changed)
        apply_combo_dropdown_style(self.format_combo)
        format_row.addWidget(self.format_combo, 1)
        self.options_layout.addLayout(format_row)

        # 质量行（ToposClickableSlider + DragValue，仿 Batch Export quality 行）
        # quality_widget 包裹整行，按格式显隐
        self.quality_widget = QWidget()
        quality_row = QHBoxLayout(self.quality_widget)
        quality_row.setContentsMargins(0, 2, 0, 2)
        quality_row.setSpacing(4)
        # quality_label 由 on_format_changed 动态改文字（质量↔压缩）
        self.quality_label = _make_param_label(I18N.tr("export.quality", "质量:"))
        quality_row.addWidget(self.quality_label)
        self.quality_slider = ToposClickableSlider(Qt.Orientation.Horizontal)
        self.quality_slider.setRange(0, 100)
        self.quality_slider.setValue(self._default_quality)
        quality_row.addWidget(self.quality_slider, 1)
        self.quality_val_label = DragValue("", min_v=0, max_v=100, default=self._default_quality, decimals=0, suffix=" %")
        self.quality_val_label.setFixedValueWidth(_VALUE_FIXED_WIDTH)
        # slider ↔ drag 双向同步（blockSignals 防递归）
        self.quality_slider.valueChanged.connect(
            lambda v: (self.quality_val_label.blockSignals(True),
                       self.quality_val_label.setValue(float(v)),
                       self.quality_val_label.blockSignals(False))
        )
        self.quality_val_label.valueChanged.connect(
            lambda v: (self.quality_slider.blockSignals(True),
                       self.quality_slider.setValue(int(round(v))),
                       self.quality_slider.blockSignals(False))
        )
        quality_row.addWidget(self.quality_val_label)
        self.options_layout.addWidget(self.quality_widget)

        # EXR 参数（exr_widget 包裹，按格式显隐）
        self.exr_widget = QWidget()
        exr_layout = QVBoxLayout(self.exr_widget)
        exr_layout.setContentsMargins(0, 0, 0, 0)
        exr_layout.setSpacing(4)

        # EXR 精度行
        exr_type_row = QHBoxLayout()
        exr_type_row.setContentsMargins(0, 2, 0, 2)
        exr_type_row.setSpacing(4)
        exr_type_label = _make_param_label(I18N.tr("export.precision", "精度:"))
        exr_type_row.addWidget(exr_type_label)
        self.exr_type_combo = ChevronComboBox()
        self.exr_type_combo.addItems([
            I18N.tr("export.exr_half", "Half (16-bit)"),
            I18N.tr("export.exr_full", "Full (32-bit)")
        ])
        apply_combo_dropdown_style(self.exr_type_combo)
        exr_type_row.addWidget(self.exr_type_combo, 1)
        exr_layout.addLayout(exr_type_row)

        # EXR 压缩行
        exr_comp_row = QHBoxLayout()
        exr_comp_row.setContentsMargins(0, 2, 0, 2)
        exr_comp_row.setSpacing(4)
        exr_comp_label = _make_param_label(I18N.tr("export.compression", "压缩:"))
        exr_comp_row.addWidget(exr_comp_label)
        self.exr_comp_combo = ChevronComboBox()
        self.exr_comp_combo.addItems([
            I18N.tr("export.exr_comp_none", "无"),
            I18N.tr("export.exr_comp_rle", "RLE"),
            I18N.tr("export.exr_comp_zips", "ZIPS"),
            I18N.tr("export.exr_comp_zip", "ZIP (默认)"),
            I18N.tr("export.exr_comp_piz", "PIZ"),
            I18N.tr("export.exr_comp_pxr24", "PXR24"),
            I18N.tr("export.exr_comp_b44", "B44"),
            I18N.tr("export.exr_comp_b44a", "B44A"),
            I18N.tr("export.exr_comp_dwaa", "DWAA"),
            I18N.tr("export.exr_comp_dwab", "DWAB")
        ])
        self.exr_comp_combo.setCurrentIndex(3)
        apply_combo_dropdown_style(self.exr_comp_combo)
        exr_comp_row.addWidget(self.exr_comp_combo, 1)
        exr_layout.addLayout(exr_comp_row)

        self.options_layout.addWidget(self.exr_widget)

        # M2-F9：TOOS 档位行（单一注册表取名；默认 float-ultra——
        # 'toos Ultra 16bit float' 一键静帧导出）
        self.toos_widget = QWidget()
        toos_row = QHBoxLayout(self.toos_widget)
        toos_row.setContentsMargins(0, 2, 0, 2)
        toos_row.setSpacing(4)
        toos_tier_label = _make_param_label(
            I18N.tr("export.toos_tier", "档位:"))
        toos_row.addWidget(toos_tier_label)
        self.toos_tier_combo = ChevronComboBox()
        from src.shared.codec.topos_profiles import available_topos_image_tiers
        for _t in available_topos_image_tiers():
            if _t.family in ("int422", "int444", "float"):
                self.toos_tier_combo.addItem(_t.label, _t.tier_id)
        _fdef = self.toos_tier_combo.findData("float-ultra")
        if _fdef >= 0:
            self.toos_tier_combo.setCurrentIndex(_fdef)
        apply_combo_dropdown_style(self.toos_tier_combo)
        toos_row.addWidget(self.toos_tier_combo, 1)
        self.options_layout.addWidget(self.toos_widget)
        self.toos_widget.setVisible(False)

        page_layout.addWidget(format_section)

    # ================================================================
    # LUT 导出页面
    # ================================================================

    def setup_lut_page(self):
        """设置LUT导出页面（用 ExportSectionWidget 分组）。"""
        page_layout = QVBoxLayout(self.lut_page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(6)

        # ── 1. File & Location section ──
        file_section = ExportSectionWidget(I18N.tr("export.output_section", "File & Location"), header_mode="none")
        file_layout = file_section.content_layout()

        # LUT 文件名（默认使用项目名）
        lut_default_name = (
            self.default_name.replace("_exported", "")
            if "_exported" in self.default_name else self.default_name
        )
        lut_name_row = QHBoxLayout()
        lut_name_row.setContentsMargins(0, 2, 0, 2)
        lut_name_row.setSpacing(4)
        lut_name_label = _make_param_label(I18N.tr("export.lut_name_label", "LUT名称:"))
        lut_name_row.addWidget(lut_name_label)
        self.lut_name_input = QLineEdit(lut_default_name)
        self.lut_name_input.setPlaceholderText(I18N.tr("export.lut_name", "LUT名称"))
        _apply_text_input_style(self.lut_name_input)
        lut_name_row.addWidget(self.lut_name_input, 1)
        file_layout.addLayout(lut_name_row)

        # LUT 路径（默认保存到 luts 目录）
        # AUD-33302：luts 目录锚定仓库根（由本模块文件位置上溯），不再
        # 依赖 os.getcwd()——工作目录随启动方式（终端 cd / Finder / 打包
        # .app）漂移且运行中可被其他组件改动，"luts 目录是否存在"因此
        # 非确定，甚至可能把 LUT 写进无关位置的同名目录。
        lut_default_dir = self.default_dir
        _project_root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))))
        luts_dir = os.path.join(_project_root, "luts")
        if os.path.isdir(luts_dir):
            lut_default_dir = luts_dir
        lut_path_row = QHBoxLayout()
        lut_path_row.setContentsMargins(0, 2, 0, 2)
        lut_path_row.setSpacing(4)
        lut_path_label = _make_param_label(I18N.tr("export.location", "位置:"))
        lut_path_row.addWidget(lut_path_label)
        self.lut_path_input = QLineEdit(lut_default_dir)
        self.lut_path_input.setPlaceholderText(I18N.tr("export.select_dir_placeholder", "选择导出目录..."))
        _apply_text_input_style(self.lut_path_input)
        lut_path_row.addWidget(self.lut_path_input, 1)
        self.lut_path_btn = QPushButton(I18N.tr("export.choose_btn", "选择..."))
        self.lut_path_btn.clicked.connect(self.pick_lut_directory)
        _apply_compact_button_style(self.lut_path_btn)
        lut_path_row.addWidget(self.lut_path_btn)
        file_layout.addLayout(lut_path_row)

        page_layout.addWidget(file_section)

        # ── 2. LUT Settings section ──
        lut_section = ExportSectionWidget(I18N.tr("export.lut_settings", "LUT Settings"), header_mode="none")
        lut_settings_layout = lut_section.content_layout()

        # LUT 格式
        lut_format_row = QHBoxLayout()
        lut_format_row.setContentsMargins(0, 2, 0, 2)
        lut_format_row.setSpacing(4)
        lut_format_label = _make_param_label(I18N.tr("export.lut_format", "文件格式:"))
        lut_format_row.addWidget(lut_format_label)
        self.lut_format_combo = ChevronComboBox()
        self.lut_format_combo.addItems([
            I18N.tr("export.lut_cube", "CUBE (.cube)"),
            I18N.tr("export.lut_3dl", "3DL (.3dl)")
        ])
        self.lut_format_combo.setCurrentIndex(0)  # 默认 CUBE 格式
        apply_combo_dropdown_style(self.lut_format_combo)
        lut_format_row.addWidget(self.lut_format_combo, 1)
        lut_settings_layout.addLayout(lut_format_row)

        # 采样点
        lut_size_row = QHBoxLayout()
        lut_size_row.setContentsMargins(0, 2, 0, 2)
        lut_size_row.setSpacing(4)
        lut_size_label = _make_param_label(I18N.tr("export.lut_size", "采样点:"))
        lut_size_row.addWidget(lut_size_label)
        self.lut_size_combo = ChevronComboBox()
        self.lut_size_combo.addItems([
            I18N.tr("export.lut_size_17", "17 × 17 × 17 (快速)"),
            I18N.tr("export.lut_size_33", "33 × 33 × 33 (推荐)"),
            I18N.tr("export.lut_size_65", "65 × 65 × 65 (高质量)")
        ])
        self.lut_size_combo.setCurrentIndex(1)  # 默认 33x33x33
        apply_combo_dropdown_style(self.lut_size_combo)
        lut_size_row.addWidget(self.lut_size_combo, 1)
        lut_settings_layout.addLayout(lut_size_row)

        page_layout.addWidget(lut_section)

        # ── 3. LUT 说明 section（信息提示） ──
        info_section = ExportSectionWidget(I18N.tr("export.lut_info_title", "LUT导出说明"), header_mode="none")
        info_layout = info_section.content_layout()

        info_text = QLabel(I18N.tr("export.lut_info_text",
            "可导出: 色轮调整、RGB曲线、混色器、软剪裁\n"
            "不可导出: 蒙版、限定器、模糊/锐化、胶片颗粒、插件效果"))
        info_text.setStyleSheet(f"color: {C.TEXT_HINT}; font-size: 10px;")
        info_text.setWordWrap(True)
        info_layout.addWidget(info_text)

        # 调色效果提示
        self.lut_grading_label = QLabel()
        if self.has_grading:
            self.lut_grading_label.setText(
                I18N.tr("export.lut_has_grading", "✅ 当前图层有调色效果，可以导出LUT"))
            self.lut_grading_label.setStyleSheet(f"color: {C.CHANNEL_G}; font-size: 10px;")
        else:
            self.lut_grading_label.setText(
                I18N.tr("export.lut_no_grading", "⚠️ 当前图层无调色效果，导出的LUT将是线性（无调整）"))
            self.lut_grading_label.setStyleSheet("color: #ff9800; font-size: 10px;")
        info_layout.addWidget(self.lut_grading_label)

        page_layout.addWidget(info_section)

    # ================================================================
    # 视频导出页面
    # ================================================================

    # 容器 → 可用编码列表（key 为 i18n key 的后缀，避免运行时切换语言导致映射失败）
    # 复验 P1-08 / AUD-33301：单一真相源 = deliver 的
    # CODEC_CONTAINER_TABLE（经 allowed_codecs_for_container 派生）。
    # 此前本文件维护硬编码副本，已与注册表漂移（deliver 允许 dnxhr 系入
    # mov/mkv、prores 系入 mkv/mxf，快速导出均未提供）；注册表新增档位时
    # 此处也必然再次漏更。快速导出仅做展示，不再维护第二份列表。
    @staticmethod
    def _container_codec_ids(container: str) -> list:
        """容器 id → 可用编码 id 列表（deliver 编解码注册表派生）。

        序列档位（png/tiff/topos image sequence，容器域为空串）与
        gif 容器之外的 gif 编码天然不会命中真实容器，无需额外过滤。
        """
        return allowed_codecs_for_container(container)

    _CONTAINER_EXT = {
        "mp4": ".mp4", "mov": ".mov", "mkv": ".mkv", "mxf": ".mxf", "gif": ".gif",
    }

    def _codec_display_key(self, codec_id: str) -> str:
        """codec_id → i18n key（export.* 命名空间），如 'prores_422hq' → 'export.codec_prores_422hq'。"""
        return f"export.codec_{codec_id}"

    def _codec_display_text(self, codec_id: str) -> str:
        """codec_id → 本地化显示文本。"""
        return I18N.tr(self._codec_display_key(codec_id), codec_id.upper())

    def setup_video_page(self):
        """设置视频导出页面（用 ExportSectionWidget 分组）。"""
        page_layout = QVBoxLayout(self.video_page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(6)

        # ── 1. File & Location section ──
        file_section = ExportSectionWidget(I18N.tr("export.output_section", "File & Location"), header_mode="none")
        file_layout = file_section.content_layout()

        # 文件名行
        name_row = QHBoxLayout()
        name_row.setContentsMargins(0, 2, 0, 2)
        name_row.setSpacing(4)
        name_label = _make_param_label(I18N.tr("export.video_name_label", "文件名:"))
        name_row.addWidget(name_label)
        # 默认文件名优先使用时间线名字；其次用 default_name 去掉 "_exported" 后缀
        timeline_name = self._timeline_info.get("name", "") or ""
        # 清理非法文件名字符
        invalid_chars = '<>:"/\\|?*'
        for ch in invalid_chars:
            timeline_name = timeline_name.replace(ch, "_")
        timeline_name = timeline_name.strip() or ""
        if timeline_name:
            video_default_name = timeline_name
        elif "_exported" in self.default_name:
            video_default_name = self.default_name.replace("_exported", "")
        else:
            video_default_name = self.default_name
        self.video_name_input = QLineEdit(video_default_name)
        self.video_name_input.setPlaceholderText(I18N.tr("export.video_name_label", "文件名:"))
        _apply_text_input_style(self.video_name_input)
        name_row.addWidget(self.video_name_input, 1)
        file_layout.addLayout(name_row)

        # 路径行
        path_row = QHBoxLayout()
        path_row.setContentsMargins(0, 2, 0, 2)
        path_row.setSpacing(4)
        path_label = _make_param_label(I18N.tr("export.location", "位置:"))
        path_row.addWidget(path_label)
        self.video_path_input = QLineEdit(self.default_dir)
        self.video_path_input.setPlaceholderText(I18N.tr("export.select_dir_placeholder", "选择导出目录..."))
        _apply_text_input_style(self.video_path_input)
        path_row.addWidget(self.video_path_input, 1)
        self.video_path_btn = QPushButton(I18N.tr("export.choose_btn", "选择..."))
        self.video_path_btn.clicked.connect(self.pick_video_directory)
        _apply_compact_button_style(self.video_path_btn)
        path_row.addWidget(self.video_path_btn)
        file_layout.addLayout(path_row)

        page_layout.addWidget(file_section)

        # ── 2. Export Range section（信息提示） ──
        range_section = ExportSectionWidget(I18N.tr("export.video_range_section", "导出范围"), header_mode="none")
        range_layout = range_section.content_layout()

        self.video_range_label = QLabel()
        self.video_range_label.setWordWrap(True)
        self.video_range_label.setStyleSheet("font-size: 10px; padding: 4px 6px; border-radius: 4px;")
        range_layout.addWidget(self.video_range_label)

        # 更新范围提示文本
        self._update_video_range_label()

        page_layout.addWidget(range_section)

        # ── 3. Video Settings section ──
        video_section = ExportSectionWidget(I18N.tr("export.video_settings_section", "视频设置"), header_mode="none")
        video_layout = video_section.content_layout()

        # 容器行
        container_row = QHBoxLayout()
        container_row.setContentsMargins(0, 2, 0, 2)
        container_row.setSpacing(4)
        container_label = _make_param_label(I18N.tr("export.video_container", "容器:"))
        container_row.addWidget(container_label)
        self.video_format_combo = ChevronComboBox()
        self.video_format_combo.addItems([
            I18N.tr("export.fmt_mp4", "MP4 (.mp4)"),
            I18N.tr("export.fmt_mov", "MOV (.mov)"),
            I18N.tr("export.fmt_mkv", "MKV (.mkv)"),
            I18N.tr("export.fmt_mxf", "MXF (.mxf)"),
            I18N.tr("export.fmt_gif", "GIF (.gif)"),
        ])
        self.video_format_combo.setCurrentIndex(0)
        self.video_format_combo.currentTextChanged.connect(self.on_video_format_changed)
        apply_combo_dropdown_style(self.video_format_combo)
        container_row.addWidget(self.video_format_combo, 1)
        video_layout.addLayout(container_row)

        # 编码行
        codec_row = QHBoxLayout()
        codec_row.setContentsMargins(0, 2, 0, 2)
        codec_row.setSpacing(4)
        codec_label = _make_param_label(I18N.tr("export.video_codec", "编码:"))
        codec_row.addWidget(codec_label)
        self.video_codec_combo = ChevronComboBox()
        apply_combo_dropdown_style(self.video_codec_combo)
        codec_row.addWidget(self.video_codec_combo, 1)
        video_layout.addLayout(codec_row)

        # 质量行（仅 H.264/HEVC 显示，CRF）
        self.video_quality_widget = QWidget()
        quality_row = QHBoxLayout(self.video_quality_widget)
        quality_row.setContentsMargins(0, 2, 0, 2)
        quality_row.setSpacing(4)
        quality_label = _make_param_label(I18N.tr("export.video_quality", "质量:"))
        quality_row.addWidget(quality_label)
        self.video_quality_combo = ChevronComboBox()
        self.video_quality_combo.addItems([
            I18N.tr("export.video_quality_high", "高 (CRF 18)"),
            I18N.tr("export.video_quality_recommended", "推荐 (CRF 20)"),
            I18N.tr("export.video_quality_balanced", "平衡 (CRF 23)"),
            I18N.tr("export.video_quality_small", "小体积 (CRF 28)"),
        ])
        self.video_quality_combo.setCurrentIndex(1)
        apply_combo_dropdown_style(self.video_quality_combo)
        quality_row.addWidget(self.video_quality_combo, 1)
        video_layout.addWidget(self.video_quality_widget)

        # GIF-only settings.  They are kept in the video page so the same
        # Quick Export dialog can switch between normal video and animated
        # preview output without changing the rest of the workflow.
        self.gif_settings_widget = QWidget()
        gif_layout = QVBoxLayout(self.gif_settings_widget)
        gif_layout.setContentsMargins(0, 2, 0, 2)
        gif_layout.setSpacing(4)

        gif_size_row = QHBoxLayout()
        gif_size_row.setContentsMargins(0, 0, 0, 0)
        gif_size_row.setSpacing(4)
        gif_size_row.addWidget(_make_param_label(
            I18N.tr("export.gif_size", "GIF 尺寸:")
        ))
        timeline_width = int(self._timeline_info.get("width", 0) or 1920)
        timeline_height = int(self._timeline_info.get("height", 0) or 1080)
        self.gif_width_spin = DragValue("", 16, 16384, timeline_width, decimals=0)
        self.gif_width_spin.setFixedValueWidth(_VALUE_FIXED_WIDTH)
        gif_size_row.addWidget(self.gif_width_spin)
        gif_size_row.addWidget(QLabel("×"))
        self.gif_height_spin = DragValue("", 16, 16384, timeline_height, decimals=0)
        self.gif_height_spin.setFixedValueWidth(_VALUE_FIXED_WIDTH)
        gif_size_row.addWidget(self.gif_height_spin)
        gif_size_row.addStretch()
        gif_layout.addLayout(gif_size_row)

        gif_fps_row = QHBoxLayout()
        gif_fps_row.setContentsMargins(0, 0, 0, 0)
        gif_fps_row.setSpacing(4)
        gif_fps_row.addWidget(_make_param_label(
            I18N.tr("export.gif_fps", "GIF 帧率:")
        ))
        timeline_fps = float(self._timeline_info.get("fps", 0.0) or 24.0)
        self.gif_fps_spin = DragValue("", 1, 100, timeline_fps, decimals=2, suffix=" fps")
        self.gif_fps_spin.setFixedValueWidth(_VALUE_FIXED_WIDTH)
        gif_fps_row.addWidget(self.gif_fps_spin)
        gif_fps_row.addStretch()
        gif_layout.addLayout(gif_fps_row)

        gif_colors_row = QHBoxLayout()
        gif_colors_row.setContentsMargins(0, 0, 0, 0)
        gif_colors_row.setSpacing(4)
        gif_colors_row.addWidget(_make_param_label(
            I18N.tr("export.gif_colors", "GIF 色彩数量:")
        ))
        self.gif_color_count_spin = QSpinBox()
        self.gif_color_count_spin.setRange(2, 256)
        self.gif_color_count_spin.setValue(256)
        self.gif_color_count_spin.setSuffix(I18N.tr("export.colors_suffix", " 色"))
        gif_colors_row.addWidget(self.gif_color_count_spin)
        gif_colors_row.addStretch()
        gif_layout.addLayout(gif_colors_row)

        gif_hint = QLabel(I18N.tr(
            "export.gif_hint",
            "GIF 不支持音频；颜色越少，文件越小，但渐变细节会减少。",
        ))
        gif_hint.setWordWrap(True)
        gif_hint.setStyleSheet(f"color: {C.TEXT_HINT}; font-size: 10px;")
        gif_layout.addWidget(gif_hint)
        self.gif_settings_widget.setVisible(False)
        video_layout.addWidget(self.gif_settings_widget)

        page_layout.addWidget(video_section)

        # ── 4. Audio Settings section ──
        audio_section = ExportSectionWidget(I18N.tr("export.video_audio_section", "音频设置"), header_mode="none")
        audio_layout = audio_section.content_layout()

        # 包含音频开关
        audio_switch_row = QHBoxLayout()
        audio_switch_row.setContentsMargins(0, 2, 0, 2)
        audio_switch_row.setSpacing(4)
        audio_switch_label = _make_param_label(I18N.tr("export.video_include_audio", "包含音频"))
        audio_switch_row.addWidget(audio_switch_label)
        audio_switch_row.addStretch()
        self.video_audio_check = ToggleSwitch(checked=True)
        self.video_audio_check.toggled.connect(self.on_video_audio_toggled)
        audio_switch_row.addWidget(self.video_audio_check)
        audio_layout.addLayout(audio_switch_row)

        # 音频编码行
        self.video_audio_codec_widget = QWidget()
        audio_codec_row = QHBoxLayout(self.video_audio_codec_widget)
        audio_codec_row.setContentsMargins(0, 2, 0, 2)
        audio_codec_row.setSpacing(4)
        audio_codec_label = _make_param_label(I18N.tr("export.video_audio_codec", "音频编码:"))
        audio_codec_row.addWidget(audio_codec_label)
        self.video_audio_codec_combo = ChevronComboBox()
        self.video_audio_codec_combo.addItems([
            I18N.tr("export.audio_aac", "AAC"),
            I18N.tr("export.audio_pcm16", "PCM 16-bit"),
            I18N.tr("export.audio_pcm24", "PCM 24-bit"),
        ])
        self.video_audio_codec_combo.setCurrentIndex(0)
        apply_combo_dropdown_style(self.video_audio_codec_combo)
        audio_codec_row.addWidget(self.video_audio_codec_combo, 1)
        audio_layout.addWidget(self.video_audio_codec_widget)

        # 音频码率行（仅 AAC 显示）
        self.video_audio_bitrate_widget = QWidget()
        audio_bitrate_row = QHBoxLayout(self.video_audio_bitrate_widget)
        audio_bitrate_row.setContentsMargins(0, 2, 0, 2)
        audio_bitrate_row.setSpacing(4)
        audio_bitrate_label = _make_param_label(I18N.tr("export.video_audio_bitrate", "音频码率:"))
        audio_bitrate_row.addWidget(audio_bitrate_label)
        self.video_audio_bitrate_combo = ChevronComboBox()
        self.video_audio_bitrate_combo.addItems(["128 kbps", "192 kbps", "256 kbps", "320 kbps"])
        self.video_audio_bitrate_combo.setCurrentIndex(1)
        apply_combo_dropdown_style(self.video_audio_bitrate_combo)
        audio_bitrate_row.addWidget(self.video_audio_bitrate_combo, 1)
        audio_layout.addWidget(self.video_audio_bitrate_widget)

        self.video_audio_section = audio_section
        page_layout.addWidget(audio_section)

        # ── 5. Resolution & Frame Rate section（只读信息） ──
        res_info_section = ExportSectionWidget(I18N.tr("export.video_resolution_section", "分辨率与帧率"), header_mode="none")
        res_info_layout = res_info_section.content_layout()

        width = self._timeline_info.get("width", 0)
        height = self._timeline_info.get("height", 0)
        fps = self._timeline_info.get("fps", 0.0)
        fps_str = f"{fps:.2f}" if isinstance(fps, (int, float)) and fps > 0 else "—"
        res_text = I18N.tr("export.video_resolution_info", "{w} × {h} @ {fps} fps",
                           w=width, h=height, fps=fps_str)
        res_label = QLabel(res_text)
        res_label.setStyleSheet(f"color: {C.TEXT_HINT}; font-size: 10px;")
        res_info_layout.addWidget(res_label)

        self.video_res_info_section = res_info_section
        page_layout.addWidget(res_info_section)

        # 初始显隐：质量行 + 音频码率行
        self._update_video_quality_visibility()
        self._update_audio_bitrate_visibility()
        # 音频编码变化时联动码率行显隐
        self.video_audio_codec_combo.currentTextChanged.connect(
            lambda _t: self._update_audio_bitrate_visibility()
        )

    def _update_video_range_label(self):
        """根据时间线状态更新导出范围提示。"""
        if not self._has_timeline:
            self.video_range_label.setText(
                I18N.tr("export.video_no_timeline", "⚠️ 无可用时间线，请先在剪辑页创建时间线"))
            self.video_range_label.setStyleSheet(
                "color: #ff9800; background-color: rgba(255, 152, 0, 0.1);"
                " font-size: 10px; padding: 4px 6px; border-radius: 4px;")
            return

        duration = self._timeline_info.get("duration_frames", 0)
        has_range = self._timeline_info.get("has_range", False)

        if has_range:
            start = self._timeline_info.get("range_start", 0)
            end = self._timeline_info.get("range_end", 0)
            frame_count = max(0, end - start + 1)
            text = I18N.tr("export.video_range_in_out",
                           "✅ 将导出 In/Out 范围: {start} → {end} (共 {frames} 帧)",
                           start=start, end=end, frames=frame_count)
            self.video_range_label.setText(text)
            self.video_range_label.setStyleSheet(
                f"color: {C.CHANNEL_G}; background-color: rgba(76, 175, 80, 0.1);"
                f" font-size: 10px; padding: 4px 6px; border-radius: 4px;")
        else:
            text = I18N.tr("export.video_range_entire",
                           "ℹ️ 未设置 In/Out 点，将导出整个时间线 (共 {frames} 帧)",
                           frames=duration)
            self.video_range_label.setText(text)
            self.video_range_label.setStyleSheet(
                "color: #ff9800; background-color: rgba(255, 152, 0, 0.1);"
                " font-size: 10px; padding: 4px 6px; border-radius: 4px;")

    def pick_video_directory(self):
        current_path = self.video_path_input.text()
        dir_path = QFileDialog.getExistingDirectory(
            self, I18N.tr("export.select_dir_placeholder", "选择导出目录"), current_path)
        if dir_path:
            self.video_path_input.setText(dir_path)

    def on_video_format_changed(self, fmt_display):
        """容器变化时刷新可选编码列表。"""
        # 显示文本 → 容器 id
        display_to_container = {
            I18N.tr("export.fmt_mp4", "MP4 (.mp4)"): "mp4",
            I18N.tr("export.fmt_mov", "MOV (.mov)"): "mov",
            I18N.tr("export.fmt_mkv", "MKV (.mkv)"): "mkv",
            I18N.tr("export.fmt_mxf", "MXF (.mxf)"): "mxf",
            I18N.tr("export.fmt_gif", "GIF (.gif)"): "gif",
        }
        container = display_to_container.get(fmt_display, "mp4")
        # AUD-33301：从 deliver 编解码注册表派生（单一真相源）
        codecs = self._container_codec_ids(container) or ["h264"]

        # 重建编码下拉
        self.video_codec_combo.blockSignals(True)
        self.video_codec_combo.clear()
        for codec_id in codecs:
            self.video_codec_combo.addItem(self._codec_display_text(codec_id), userData=codec_id)
        # ProRes 422 HQ / DNxHR HQ 作为默认优选（若该容器支持）
        preferred = "prores_422hq" if container == "mov" else (
            "dnxhr_hq" if container == "mxf" else ("gif" if container == "gif" else "h264")
        )
        for i in range(self.video_codec_combo.count()):
            if self.video_codec_combo.itemData(i) == preferred:
                self.video_codec_combo.setCurrentIndex(i)
                break
        self.video_codec_combo.blockSignals(False)

        # 根据编码类型显隐质量行
        self._update_video_quality_visibility()
        self._update_gif_visibility(container == "gif")

    def _update_video_quality_visibility(self):
        """质量行仅在 H.264/HEVC 时显示。"""
        codec_id = self.video_codec_combo.currentData() or "h264"
        is_lossy = codec_id in ("h264", "hevc")
        self.video_quality_widget.setVisible(is_lossy and codec_id != "gif")

    def _update_gif_visibility(self, is_gif: bool) -> None:
        """Toggle GIF controls and make the no-audio limitation explicit."""
        was_gif = getattr(self, "_is_gif_export", False)
        if is_gif and not was_gif:
            self._audio_before_gif = self.video_audio_check.isChecked()
        if not is_gif and was_gif:
            self.video_audio_check.setChecked(getattr(self, "_audio_before_gif", True))

        self._is_gif_export = is_gif
        self.gif_settings_widget.setVisible(is_gif)
        self.video_audio_section.setVisible(not is_gif)
        self.video_res_info_section.setVisible(not is_gif)
        self.video_audio_check.setEnabled(not is_gif)
        if is_gif:
            self.video_audio_check.setChecked(False)

    def on_video_audio_toggled(self, checked):
        """音频开关切换时显隐音频参数。"""
        self.video_audio_codec_widget.setEnabled(checked)
        # AAC 才显示码率行
        if checked:
            self._update_audio_bitrate_visibility()
        else:
            self.video_audio_bitrate_widget.setVisible(False)

    def _update_audio_bitrate_visibility(self):
        """音频码率行仅在 AAC 时显示。"""
        audio_codec_text = self.video_audio_codec_combo.currentText()
        is_aac = audio_codec_text == I18N.tr("export.audio_aac", "AAC")
        self.video_audio_bitrate_widget.setVisible(is_aac)

    def apply_styles(self):
        """对话框整体配色（与 Batch Export 面板统一）。"""
        self.setStyleSheet(f"""
            QuickExportDialog {{
                background-color: {C.BG_PANEL};
                border: 1px solid {C.BORDER};
                border-radius: 8px;
            }}
            QLabel {{ color: {C.TEXT_SECONDARY}; font-size: 11px; }}
            #PrimaryButton {{
                background-color: {C.ORANGE_PRESS};
                color: white;
                font-size: 13px;
                font-weight: bold;
                border: none;
                border-radius: 3px;
            }}
            #PrimaryButton:hover {{ background-color: {C.ORANGE_HOVER}; }}
            #PrimaryButton:pressed {{ background-color: {C.ORANGE}; }}
            QPushButton {{
                background-color: {C.BG_BUTTON};
                border: 1px solid {C.BORDER_MID};
                border-radius: 3px;
                padding: 6px 16px;
                color: {C.TEXT_BRIGHT};
            }}
            QPushButton:hover {{ background-color: {C.BG_BUTTON_HOVER}; }}
            QTabBar::tab {{
                background-color: {C.BG_DARK};
                border: 1px solid {C.BORDER};
                border-radius: 4px;
                padding: 6px 14px;
                margin-right: 4px;
                color: {C.TEXT_LABEL};
                font-size: 11px;
            }}
            QTabBar::tab:selected {{
                background-color: {C.ORANGE_PRESS};
                color: #fff;
                border-color: {C.ORANGE_PRESS};
            }}
            QTabBar::tab:hover:!selected {{ background-color: {C.BG_HOVER}; }}
        """)

    # ================================================================
    # Slots
    # ================================================================

    def _apply_initial_export_type(self) -> None:
        """Select the requested export tab when it is available."""
        tab_indices: dict[str, int] = {
            self.EXPORT_IMAGE: 0,
            self.EXPORT_LUT: 1,
            self.EXPORT_VIDEO: 2,
        }
        tab_index = tab_indices.get(self._initial_export_type, 0)
        # Fall back to the first enabled tab if the requested one is disabled
        # (e.g. video mode disabled the image tab, or no timeline for video).
        if not self.tab_bar.isTabEnabled(tab_index):
            for i in range(self.tab_bar.count()):
                if self.tab_bar.isTabEnabled(i):
                    tab_index = i
                    break
        self.tab_bar.setCurrentIndex(tab_index)

    def on_tab_changed(self, index):
        """切换导出类型"""
        if index == 0:
            self.export_type = self.EXPORT_IMAGE
        elif index == 1:
            self.export_type = self.EXPORT_LUT
        else:
            self.export_type = self.EXPORT_VIDEO
        self.content_stack.setCurrentIndex(index)

        # 更新导出按钮文本
        if self.export_type == self.EXPORT_LUT:
            self.export_btn.setText(I18N.tr("export.export_lut", "导出LUT"))
        elif self.export_type == self.EXPORT_VIDEO:
            self.export_btn.setText(I18N.tr("export.export_video", "导出视频"))
        else:
            self.export_btn.setText(I18N.tr("export.export", "导出"))

    def pick_directory(self):
        current_path = self.path_input.text()
        dir_path = QFileDialog.getExistingDirectory(
            self, I18N.tr("export.select_dir_placeholder", "选择导出目录"), current_path)
        if dir_path:
            self.path_input.setText(dir_path)

    def pick_lut_directory(self):
        current_path = self.lut_path_input.text()
        dir_path = QFileDialog.getExistingDirectory(
            self, I18N.tr("export.select_dir_placeholder", "选择导出目录"), current_path)
        if dir_path:
            self.lut_path_input.setText(dir_path)

    def update_export_mode(self):
        """更新导出模式提示"""
        enabled = self.resize_check.isChecked()
        self.resize_widget.setEnabled(enabled)

        if not enabled:
            # 默认导出：使用原图尺寸
            # 如果原图 <= 缓存尺寸，快速导出
            if self.orig_w <= self.max_quick_w and self.orig_h <= self.max_quick_h:
                self.mode_label.setText(I18N.tr("export.quick_export", "⚡ 快速导出（缓存，快速）"))
                self.mode_label.setStyleSheet(
                    f"color: {C.CHANNEL_G}; background-color: rgba(76, 175, 80, 0.1);"
                    f" font-size: 10px; padding: 4px 6px; border-radius: 4px;")
            else:
                self.mode_label.setText(I18N.tr("export.high_quality", "🎨 高质量导出（完整渲染）"))
                self.mode_label.setStyleSheet(
                    "color: #ff9800; background-color: rgba(255, 152, 0, 0.1);"
                    " font-size: 10px; padding: 4px 6px; border-radius: 4px;")
        else:
            # 自定义分辨率：检查是否超过缓存尺寸
            w = int(round(self.width_spin.value()))
            h = int(round(self.height_spin.value()))

            if w <= self.max_quick_w and h <= self.max_quick_h:
                self.mode_label.setText(I18N.tr("export.quick_export", "⚡ 快速导出（缓存，快速）"))
                self.mode_label.setStyleSheet(
                    f"color: {C.CHANNEL_G}; background-color: rgba(76, 175, 80, 0.1);"
                    f" font-size: 10px; padding: 4px 6px; border-radius: 4px;")
            else:
                self.mode_label.setText(I18N.tr("export.high_quality_slow", "🎨 高质量导出（完整渲染，较慢）"))
                self.mode_label.setStyleSheet(
                    "color: #ff9800; background-color: rgba(255, 152, 0, 0.1);"
                    " font-size: 10px; padding: 4px 6px; border-radius: 4px;")

    def on_resolution_changed(self):
        """分辨率变化时更新（DragValue.value() 返回 float，强转 int）。"""
        if self.lock_ratio.isChecked():
            sender = self.sender()
            if sender == self.width_spin:
                # 根据宽度调整高度
                ratio = self.orig_h / max(1, self.orig_w)
                new_h = int(self.width_spin.value() * ratio)
                self.height_spin.blockSignals(True)
                self.height_spin.setValue(float(new_h))
                self.height_spin.blockSignals(False)
            elif sender == self.height_spin:
                # 根据高度调整宽度
                ratio = self.orig_w / max(1, self.orig_h)
                new_w = int(self.height_spin.value() * ratio)
                self.width_spin.blockSignals(True)
                self.width_spin.setValue(float(new_w))
                self.width_spin.blockSignals(False)

        self.update_export_mode()

    def on_format_changed(self, fmt):
        """格式变化时显隐 EXR / 质量行，并切换质量↔压缩标签。"""
        is_exr = fmt == I18N.tr("export.fmt_exr", "OpenEXR")
        self.exr_widget.setVisible(is_exr)
        # M2-F9：TOOS 档位选择（整数/浮点家族；RAW 随图片 RAW 导出点亮）
        self.toos_widget.setVisible(
            fmt == I18N.tr("export.fmt_toos", "TOOS (.toos)"))
        can_have_quality = fmt in [
            I18N.tr("export.fmt_jpeg", "JPEG"),
            I18N.tr("export.fmt_png", "PNG"),
            I18N.tr("export.fmt_webp", "WebP"),
        ]
        self.quality_widget.setVisible(can_have_quality)
        # PNG 显示"压缩"，JPEG/WebP 显示"质量"
        if fmt == I18N.tr("export.fmt_png", "PNG"):
            self.quality_label.setText(I18N.tr("export.compression", "压缩:"))
        else:
            self.quality_label.setText(I18N.tr("export.quality", "质量:"))

    # ================================================================
    # 对外契约：get_settings
    # ================================================================

    def get_settings(self):
        """
        获取导出设置。

        Returns:
            dict: 包含导出类型和相应设置的字典。
                  图像导出键: export_type/path/format/options/quick_export/export_source
                  LUT 导出键: export_type/path/lut_format/lut_size/lut_title
                  视频导出键: export_type/path/container/codec/quality/include_audio/
                              audio_codec/audio_bitrate/resolution/fps/has_range/
                              range_start/range_end
        """
        settings = {
            "export_type": self.export_type
        }

        if self.export_type == self.EXPORT_IMAGE:
            # 图像导出设置
            # format_combo 显示 i18n 文本，映射回内部格式名
            fmt_display = self.format_combo.currentText()
            display_to_name = {
                I18N.tr("export.fmt_jpeg", "JPEG"): "JPEG",
                I18N.tr("export.fmt_png", "PNG"): "PNG",
                I18N.tr("export.fmt_exr", "OpenEXR"): "OpenEXR",
                I18N.tr("export.fmt_tiff", "TIFF"): "TIFF",
                I18N.tr("export.fmt_webp", "WebP"): "WebP",
                I18N.tr("export.fmt_toos", "TOOS (.toos)"): "TOOS",
            }
            fmt_name = display_to_name.get(fmt_display, "JPEG")
            ext_map = {"JPEG": ".jpg", "PNG": ".png", "OpenEXR": ".exr",
                       "TIFF": ".tif", "WebP": ".webp", "TOOS": ".toos"}
            ext = ext_map.get(fmt_name, ".jpg")

            filename = self.name_input.text()
            if not filename.lower().endswith(ext):
                filename += ext

            export_dir = self.path_input.text()

            settings["path"] = os.path.join(export_dir, filename)
            settings["format"] = fmt_name
            # quality_slider 是 ToposClickableSlider（int 域）
            settings["options"] = {"quality": self.quality_slider.value()}

            # 分辨率设置（width_spin/height_spin 是 DragValue，value() 返回 float，强转 int）
            if self.resize_check.isChecked():
                settings["options"]["resize"] = (
                    int(round(self.width_spin.value())),
                    int(round(self.height_spin.value())),
                )
            else:
                # 使用原图尺寸
                settings["options"]["resize"] = (self.orig_w, self.orig_h)

            # 判断导出模式：请求分辨率 <= 缓存尺寸时快速导出
            w, h = settings["options"]["resize"]
            settings["quick_export"] = (w <= self.max_quick_w and h <= self.max_quick_h)

            # 导出来源
            settings["export_source"] = "composite" if self.source_combo.currentIndex() == 1 else "color"

            if settings["format"] == "OpenEXR":
                settings["options"]["exr_type"] = self.exr_type_combo.currentIndex() + 1
                settings["options"]["exr_compression"] = self.exr_comp_combo.currentIndex()
            if settings["format"] == "TOOS":
                # M2-F9：图片档位 id（ToposImageSequenceEncoder 按 CLI 同锚编码）
                settings["options"]["toos_tier"] = str(
                    self.toos_tier_combo.currentData() or "float-ultra")

        elif self.export_type == self.EXPORT_LUT:
            # LUT导出设置
            # 格式映射
            lut_format_map = {
                I18N.tr("export.lut_cube", "CUBE (.cube)"): "cube",
                I18N.tr("export.lut_3dl", "3DL (.3dl)"): "3dl"
            }
            lut_format = lut_format_map.get(self.lut_format_combo.currentText(), "cube")

            # 采样点映射
            lut_size_map = {
                I18N.tr("export.lut_size_17", "17 × 17 × 17 (快速)"): 17,
                I18N.tr("export.lut_size_33", "33 × 33 × 33 (推荐)"): 33,
                I18N.tr("export.lut_size_65", "65 × 65 × 65 (高质量)"): 65
            }
            lut_size = lut_size_map.get(self.lut_size_combo.currentText(), 33)

            # 文件名和路径
            filename = self.lut_name_input.text()
            if not filename:
                filename = "untitled_lut"
            if not filename.lower().endswith(f".{lut_format}"):
                filename += f".{lut_format}"

            export_dir = self.lut_path_input.text()

            settings["path"] = os.path.join(export_dir, filename)
            settings["lut_format"] = lut_format
            settings["lut_size"] = lut_size
            settings["lut_title"] = self.lut_name_input.text() or "Topos LUT"

        elif self.export_type == self.EXPORT_VIDEO:
            # 视频导出设置
            # 容器（显示文本 → 容器 id）
            container_display = self.video_format_combo.currentText()
            display_to_container = {
                I18N.tr("export.fmt_mp4", "MP4 (.mp4)"): "mp4",
                I18N.tr("export.fmt_mov", "MOV (.mov)"): "mov",
                I18N.tr("export.fmt_mkv", "MKV (.mkv)"): "mkv",
                I18N.tr("export.fmt_mxf", "MXF (.mxf)"): "mxf",
                I18N.tr("export.fmt_gif", "GIF (.gif)"): "gif",
            }
            container = display_to_container.get(container_display, "mp4")
            ext = self._CONTAINER_EXT.get(container, ".mp4")

            # 编码（itemData 直接存 codec_id）
            codec_id = self.video_codec_combo.currentData() or "h264"

            # 质量（仅 H.264/HEVC 使用 CRF；ProRes/DNxHR 忽略）
            quality_index = self.video_quality_combo.currentIndex()
            crf_map = {0: 18, 1: 20, 2: 23, 3: 28}
            crf = crf_map.get(quality_index, 20)

            # 文件名 + 路径
            filename = self.video_name_input.text()
            if not filename:
                filename = "untitled_video"
            if not filename.lower().endswith(ext):
                filename += ext
            export_dir = self.video_path_input.text()
            settings["path"] = os.path.join(export_dir, filename)

            settings["container"] = container
            settings["codec"] = codec_id
            settings["quality"] = crf  # CRF for h264/hevc; ProRes/DNxHR 忽略

            # 音频设置
            include_audio = self.video_audio_check.isChecked()
            settings["include_audio"] = False if container == "gif" else include_audio
            audio_codec_text = self.video_audio_codec_combo.currentText()
            audio_codec_map = {
                I18N.tr("export.audio_aac", "AAC"): "aac",
                I18N.tr("export.audio_pcm16", "PCM 16-bit"): "pcm_s16le",
                I18N.tr("export.audio_pcm24", "PCM 24-bit"): "pcm_s24le",
            }
            settings["audio_codec"] = audio_codec_map.get(audio_codec_text, "aac")
            # 码率（仅 AAC）
            bitrate_text = self.video_audio_bitrate_combo.currentText()
            bitrate_map = {"128 kbps": 128, "192 kbps": 192, "256 kbps": 256, "320 kbps": 320}
            settings["audio_bitrate"] = bitrate_map.get(bitrate_text, 192)

            # GIF 的尺寸、帧率、调色板由 GIF 专属控件提供；普通视频继续
            # 沿用时间线的既有只读信息。
            if container == "gif":
                settings["resolution"] = (
                    int(round(self.gif_width_spin.value())),
                    int(round(self.gif_height_spin.value())),
                )
                settings["fps"] = float(self.gif_fps_spin.value())
                settings["gif_color_count"] = int(self.gif_color_count_spin.value())
            else:
                settings["resolution"] = (
                    self._timeline_info.get("width", 1920),
                    self._timeline_info.get("height", 1080),
                )
                settings["fps"] = self._timeline_info.get("fps", 24.0)
            # 导出范围（透传给 controller 判断）
            settings["has_range"] = self._timeline_info.get("has_range", False)
            settings["range_start"] = self._timeline_info.get("range_start")
            settings["range_end"] = self._timeline_info.get("range_end")

        return settings
