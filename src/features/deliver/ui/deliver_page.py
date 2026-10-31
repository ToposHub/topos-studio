"""
Deliver Page - 导出页面UI

提供时间线导出的完整配置界面，支持：
- 多种来源选择：Color Image / Composite Output / Timeline
- Timeline 导出使用 render plan，不走 UI viewer
- Range、Resolution、FPS、Codec、Audio 配置
- 进度显示和取消操作

## 设计原则

- 参考 export_panel.py 的结构但扩展为完整页面
- 支持 TimelineExportConfig 的所有配置项
- 进度条实时更新
- 取消导出功能
- 国际化支持

## 布局

┌──────────────────────────────────────┐
│ Source: [Timeline▼] / Color / Composite │
├──────────────────────────────────────┤
│ Timeline: [当前时间线名称]             │
│ Range: [Entire▼] / In-Out / Selected  │
│ Resolution: [1920x1080▼] / 原始 / 自定义 │
│ FPS: [24▼] / 原始                     │
│ Codec: [H.264▼] / ProRes / PNG seq    │
│ Audio: [Include▼] / Exclude           │
├──────────────────────────────────────┤
│ Output Path: [浏览...]                │
│ [开始导出]                            │
├──────────────────────────────────────┤
│ Progress: ████░░░░░░ 40%              │
└──────────────────────────────────────┘
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional, Tuple

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
    QPushButton, QLineEdit, QProgressBar, QFormLayout,
    QFileDialog, QSpinBox, QCheckBox, QMessageBox, QGroupBox,
    QApplication,
)
from PyQt6.QtCore import pyqtSignal, QThread
from PyQt6.QtGui import QCloseEvent

from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import apply_combo_dropdown_style

# Guarded import: edit feature may be disabled or physically absent
try:
    from src.features.edit.core.timeline_export import (
        TimelineExporter,
        TimelineExportConfig,
        ExportSource,
        ExportRangeType,
        ExportCodec,
        create_timeline_exporter,
        estimate_export_duration,
        estimate_export_size,
    )
    EDIT_EXPORT_AVAILABLE = True
except ImportError:
    TimelineExporter = None  # type: ignore[assignment,misc]
    TimelineExportConfig = None  # type: ignore[assignment,misc]
    ExportSource = None  # type: ignore[assignment,misc]
    ExportRangeType = None  # type: ignore[assignment,misc]
    ExportCodec = None  # type: ignore[assignment,misc]
    create_timeline_exporter = None  # type: ignore[assignment]
    estimate_export_duration = None  # type: ignore[assignment]
    estimate_export_size = None  # type: ignore[assignment]
    EDIT_EXPORT_AVAILABLE = False


logger = logging.getLogger(__name__)


# =============================================================================
# Export Worker Thread
# =============================================================================


class ExportWorker(QThread):
    """
    导出工作线程。

    在后台执行导出任务，不阻塞 UI 线程。

    信号:
        progress_changed: 进度更新 (current_frame, total_frames, percent)
        export_complete: 导出完成 (success: bool)
        export_error: 导出错误 (error_message: str)
    """

    progress_changed = pyqtSignal(int, int, float)
    export_complete = pyqtSignal(bool)
    export_error = pyqtSignal(str)

    def __init__(
        self,
        exporter: TimelineExporter,
        config: TimelineExportConfig,
    ) -> None:
        super().__init__()
        self._exporter = exporter
        self._config = config

    def run(self) -> None:
        """执行导出"""
        try:
            # 设置进度回调
            def progress_callback(current: int, total: int, percent: float) -> None:
                self.progress_changed.emit(current, total, percent)

            self._exporter._progress_callback = progress_callback

            # 执行导出
            # P0-05：export_timeline 返回 ExportOutcome（__bool__=success）；
            # 该信号契约仍是 bool，取消/失败细分由 VideoExportWorker 呈现
            success = bool(self._exporter.export_timeline(self._config))

            # 发送完成信号
            self.export_complete.emit(success)

        except Exception as e:
            logger.exception(f"Export worker error: {e}")
            self.export_error.emit(str(e))


# =============================================================================
# DeliverPage
# =============================================================================


class DeliverPage(QWidget):
    """
    Deliver 导出页面。

    提供完整的导出配置界面和进度显示。

    信号:
        export_started: 导出开始
        export_completed: 导出完成 (success: bool)
        export_cancelled: 导出取消

    Attributes:
        project: EditProject 引用
        sequence: TimelineSequence 引用
        exporter: TimelineExporter 实例
        worker: ExportWorker 实例（导出时）
    """

    export_started = pyqtSignal()
    export_completed = pyqtSignal(bool)
    export_cancelled = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("DeliverPage")

        # 引用（由 MainWindow 设置）
        self._project: Optional[Any] = None
        self._sequence: Optional[Any] = None
        # P1-02: 全量时间线（嵌套序列导出递归解析）
        self._timeline_sequences: dict = {}
        self._exporter: Optional[TimelineExporter] = None
        self._worker: Optional[ExportWorker] = None

        # 状态
        self._is_exporting: bool = False
        self._current_timeline_name: str = ""
        self._export_started_at: Optional[float] = None
        self._export_paused_at: Optional[float] = None
        self._export_paused_total: float = 0.0
        self._export_paused: bool = False

        # 创建 UI
        self._setup_ui()
        self._setup_style()
        self._setup_connections()

        logger.debug("DeliverPage initialized")

    # =========================================================================
    # UI Setup
    # =========================================================================

    def _setup_ui(self) -> None:
        """创建 UI 结构"""
        # 主布局
        self._main_layout = QVBoxLayout(self)
        self._main_layout.setContentsMargins(16, 16, 16, 16)
        self._main_layout.setSpacing(12)

        # 1. Source 选择
        source_group = QGroupBox(I18N.tr("deliver.source_group", "Source"))
        source_layout = QFormLayout(source_group)
        source_layout.setSpacing(8)

        self._source_combo = QComboBox()
        if EDIT_EXPORT_AVAILABLE:
            self._source_combo.addItem(I18N.tr("deliver.source_timeline", "Timeline"), ExportSource.TIMELINE)
            self._source_combo.addItem(I18N.tr("deliver.source_color", "Color Image"), ExportSource.COLOR_IMAGE)
            self._source_combo.addItem(I18N.tr("deliver.source_composite", "Composite Output"), ExportSource.COMPOSITE_OUTPUT)
        else:
            self._source_combo.addItem(I18N.tr("deliver.source_color", "Color Image"), "color_image")
            self._source_combo.addItem(I18N.tr("deliver.source_composite", "Composite Output"), "composite")
        apply_combo_dropdown_style(self._source_combo)
        source_layout.addRow(I18N.tr("deliver.source", "Source"), self._source_combo)

        # Timeline 名称显示
        self._timeline_name_label = QLabel(I18N.tr("deliver.no_timeline", "No Timeline"))
        self._timeline_name_label.setStyleSheet("color: #888; font-style: italic;")
        source_layout.addRow(I18N.tr("deliver.timeline", "Timeline"), self._timeline_name_label)

        if not EDIT_EXPORT_AVAILABLE:
            self._timeline_name_label.setText(
                I18N.tr("deliver.edit_unavailable", "Timeline export unavailable (edit feature disabled)")
            )

        self._main_layout.addWidget(source_group)

        # 2. Range 选择
        range_group = QGroupBox(I18N.tr("deliver.range_group", "Range"))
        range_layout = QFormLayout(range_group)
        range_layout.setSpacing(8)

        self._range_combo = QComboBox()
        if EDIT_EXPORT_AVAILABLE:
            self._range_combo.addItem(I18N.tr("deliver.range_entire", "Entire Timeline"), ExportRangeType.ENTIRE)
            self._range_combo.addItem(I18N.tr("deliver.range_inout", "In/Out Points"), ExportRangeType.IN_OUT)
            self._range_combo.addItem(I18N.tr("deliver.range_selected", "Selected Clips"), ExportRangeType.SELECTED)
            self._range_combo.addItem(I18N.tr("deliver.range_custom", "Custom Range"), ExportRangeType.CUSTOM)
        else:
            self._range_combo.addItem(I18N.tr("deliver.range_entire", "Entire Timeline"), "entire")
            self._range_combo.addItem(I18N.tr("deliver.range_custom", "Custom Range"), "custom")
        apply_combo_dropdown_style(self._range_combo)
        range_layout.addRow(I18N.tr("deliver.range", "Range"), self._range_combo)

        # 自定义范围输入（默认隐藏）
        custom_range_widget = QWidget()
        custom_range_layout = QHBoxLayout(custom_range_widget)
        custom_range_layout.setContentsMargins(0, 0, 0, 0)
        custom_range_layout.setSpacing(8)

        self._start_frame_spin = QSpinBox()
        self._start_frame_spin.setRange(0, 1000000)
        self._start_frame_spin.setValue(0)
        self._start_frame_spin.setSuffix(I18N.tr("deliver.frames_suffix", " frames"))

        self._end_frame_spin = QSpinBox()
        # 哨兵模式：minimum=-1 使 setValue(-1) 不被 QSpinBox 钳位为 0，
        # 值为 -1（即 minimum）时显示 specialValueText "To End"；
        # get_export_config 原样传 -1，TimelineExportConfig 将
        # end_frame < 0 解析为"导出到时间线末尾"（timeline_export.py）。
        self._end_frame_spin.setRange(-1, 1000000)
        self._end_frame_spin.setValue(-1)  # -1 表示到末尾
        self._end_frame_spin.setSpecialValueText(I18N.tr("deliver.to_end", "To End"))

        custom_range_layout.addWidget(QLabel(I18N.tr("deliver.start_frame", "Start")))
        custom_range_layout.addWidget(self._start_frame_spin)
        custom_range_layout.addWidget(QLabel(I18N.tr("deliver.end_frame", "End")))
        custom_range_layout.addWidget(self._end_frame_spin)

        self._custom_range_widget = custom_range_widget
        self._custom_range_widget.setVisible(False)
        range_layout.addRow(I18N.tr("deliver.custom_range", "Custom"), self._custom_range_widget)

        self._main_layout.addWidget(range_group)

        # 3. Output Settings
        output_group = QGroupBox(I18N.tr("deliver.output_group", "Output Settings"))
        output_layout = QFormLayout(output_group)
        output_layout.setSpacing(8)

        # Resolution
        resolution_widget = QWidget()
        resolution_layout = QHBoxLayout(resolution_widget)
        resolution_layout.setContentsMargins(0, 0, 0, 0)
        resolution_layout.setSpacing(8)

        self._resolution_combo = QComboBox()
        self._resolution_combo.addItem(I18N.tr("deliver.resolution_original", "Original"))
        self._resolution_combo.addItem("1920x1080 (HD)")
        self._resolution_combo.addItem("3840x2160 (4K)")
        self._resolution_combo.addItem("1280x720 (720p")
        self._resolution_combo.addItem(I18N.tr("deliver.resolution_custom", "Custom"))
        apply_combo_dropdown_style(self._resolution_combo)
        resolution_layout.addWidget(self._resolution_combo)

        # 自定义分辨率输入
        self._custom_width_spin = QSpinBox()
        self._custom_width_spin.setRange(64, 16384)
        self._custom_width_spin.setValue(1920)
        self._custom_width_spin.setSuffix(I18N.tr("deliver.px_suffix", " px"))

        self._custom_height_spin = QSpinBox()
        self._custom_height_spin.setRange(64, 16384)
        self._custom_height_spin.setValue(1080)
        self._custom_height_spin.setSuffix(I18N.tr("deliver.px_suffix", " px"))

        self._custom_resolution_widget = QWidget()
        custom_res_layout = QHBoxLayout(self._custom_resolution_widget)
        custom_res_layout.setContentsMargins(0, 0, 0, 0)
        custom_res_layout.addWidget(self._custom_width_spin)
        custom_res_layout.addWidget(QLabel("x"))
        custom_res_layout.addWidget(self._custom_height_spin)
        self._custom_resolution_widget.setVisible(False)

        resolution_layout.addWidget(self._custom_resolution_widget)
        output_layout.addRow(I18N.tr("deliver.resolution", "Resolution"), resolution_widget)

        # FPS
        fps_widget = QWidget()
        fps_layout = QHBoxLayout(fps_widget)
        fps_layout.setContentsMargins(0, 0, 0, 0)
        fps_layout.setSpacing(8)

        self._fps_combo = QComboBox()
        self._fps_combo.addItem(I18N.tr("deliver.fps_original", "Original"))
        self._fps_combo.addItem("24 fps")
        self._fps_combo.addItem("25 fps")
        self._fps_combo.addItem("30 fps")
        self._fps_combo.addItem("60 fps")
        self._fps_combo.addItem(I18N.tr("deliver.fps_custom", "Custom"))
        apply_combo_dropdown_style(self._fps_combo)
        fps_layout.addWidget(self._fps_combo)

        self._custom_fps_spin = QSpinBox()
        self._custom_fps_spin.setRange(1, 120)
        self._custom_fps_spin.setValue(24)
        self._custom_fps_spin.setSuffix(I18N.tr("deliver.fps_suffix", " fps"))
        self._custom_fps_spin.setVisible(False)
        fps_layout.addWidget(self._custom_fps_spin)

        output_layout.addRow(I18N.tr("deliver.fps", "FPS"), fps_widget)

        self._main_layout.addWidget(output_group)

        # 4. Codec Settings
        codec_group = QGroupBox(I18N.tr("deliver.codec_group", "Codec Settings"))
        codec_layout = QFormLayout(codec_group)
        codec_layout.setSpacing(8)

        # Codec
        self._codec_combo = QComboBox()
        if EDIT_EXPORT_AVAILABLE:
            self._codec_combo.addItem(I18N.tr("deliver.codec_h264", "H.264 (MP4)"), ExportCodec.H264)
            self._codec_combo.addItem(I18N.tr("deliver.codec_hevc", "HEVC (H.265)"), ExportCodec.HEVC)
            self._codec_combo.addItem(I18N.tr("deliver.codec_gif", "Animated GIF"), ExportCodec.GIF)
            self._codec_combo.addItem(I18N.tr("deliver.codec_prores422", "ProRes 422"), ExportCodec.PRORES_422)
            self._codec_combo.addItem(I18N.tr("deliver.codec_prores422hq", "ProRes 422 HQ"), ExportCodec.PRORES_422HQ)
            self._codec_combo.addItem(I18N.tr("deliver.codec_prores4444", "ProRes 4444"), ExportCodec.PRORES_4444)
            self._codec_combo.addItem(I18N.tr("deliver.codec_dnxhr_sq", "DNxHR SQ"), ExportCodec.DNXHR_SQ)
            self._codec_combo.addItem(I18N.tr("deliver.codec_dnxhr_hq", "DNxHR HQ"), ExportCodec.DNXHR_HQ)
            # E20：PNG/TIFF 序列暂无编码器实现，标签就地披露（唯一序列
            # 格式是 Topos Image Sequence）。
            self._codec_combo.addItem(I18N.tr(
                "deliver.codec_png_seq", "PNG Sequence"),
                ExportCodec.PNG_SEQUENCE)
            self._codec_combo.addItem(I18N.tr(
                "deliver.codec_tiff_seq", "TIFF Sequence"),
                ExportCodec.TIFF_SEQUENCE)
            self._codec_combo.addItem(I18N.tr("deliver.codec_topos_seq", "Topos Image Sequence"), ExportCodec.TOPOS_IMAGE_SEQUENCE)
        else:
            self._codec_combo.addItem("H.264 (MP4)", "h264")
            self._codec_combo.addItem("Animated GIF", "gif")
            self._codec_combo.addItem("ProRes 422", "prores_422")
            self._codec_combo.addItem("ProRes 422 HQ", "prores_422hq")
            self._codec_combo.addItem("PNG Sequence", "png_sequence")
            self._codec_combo.addItem("Topos Image Sequence", "topos_image_sequence")
        apply_combo_dropdown_style(self._codec_combo)
        codec_layout.addRow(I18N.tr("deliver.codec", "Codec"), self._codec_combo)

        # Quality (CRF/Bitrate)
        quality_widget = QWidget()
        quality_layout = QHBoxLayout(quality_widget)
        quality_layout.setContentsMargins(0, 0, 0, 0)
        quality_layout.setSpacing(8)

        self._quality_combo = QComboBox()
        self._quality_combo.addItem(I18N.tr("deliver.quality_default", "Default"))
        self._quality_combo.addItem(I18N.tr("deliver.quality_high", "High"))
        self._quality_combo.addItem(I18N.tr("deliver.quality_medium", "Medium"))
        self._quality_combo.addItem(I18N.tr("deliver.quality_low", "Low"))
        apply_combo_dropdown_style(self._quality_combo)
        quality_layout.addWidget(self._quality_combo)

        codec_layout.addRow(I18N.tr("deliver.quality", "Quality"), quality_widget)

        self._gif_color_count_spin = QSpinBox()
        self._gif_color_count_spin.setRange(2, 256)
        self._gif_color_count_spin.setValue(256)
        self._gif_color_count_spin.setSuffix(I18N.tr("deliver.colors_suffix", " colors"))
        self._gif_color_count_label = QLabel(I18N.tr("deliver.gif_colors_label", "GIF colors"))
        self._gif_color_count_label.setVisible(False)
        self._gif_color_count_spin.setVisible(False)
        codec_layout.addRow(self._gif_color_count_label, self._gif_color_count_spin)

        # Audio
        self._audio_check = QCheckBox(I18N.tr("deliver.include_audio", "Include Audio"))
        self._audio_check.setChecked(True)
        codec_layout.addRow(self._audio_check)

        self._main_layout.addWidget(codec_group)

        # 5. Output Path
        path_group = QGroupBox(I18N.tr("deliver.path_group", "Output"))
        path_layout = QVBoxLayout(path_group)
        path_layout.setSpacing(8)

        path_row = QHBoxLayout()
        path_row.setSpacing(8)

        self._path_input = QLineEdit()
        self._path_input.setPlaceholderText(I18N.tr("deliver.select_path_placeholder", "Select output path..."))
        path_row.addWidget(self._path_input, 1)

        self._browse_btn = QPushButton(I18N.tr("deliver.browse", "Browse"))
        self._browse_btn.setMinimumWidth(100)
        self._browse_btn.clicked.connect(self._browse_output_path)
        path_row.addWidget(self._browse_btn)

        path_layout.addLayout(path_row)

        # 估算信息
        estimate_row = QHBoxLayout()
        estimate_row.setSpacing(16)

        self._duration_label = QLabel(I18N.tr("deliver.duration", "Duration: --"))
        self._duration_label.setStyleSheet("color: #888;")
        estimate_row.addWidget(self._duration_label)

        self._size_label = QLabel(I18N.tr("deliver.size_estimate", "Size: --"))
        self._size_label.setStyleSheet("color: #888;")
        estimate_row.addWidget(self._size_label)

        estimate_row.addStretch()
        path_layout.addLayout(estimate_row)

        self._main_layout.addWidget(path_group)

        # 6. 导出按钮
        button_layout = QHBoxLayout()
        button_layout.setSpacing(16)

        self._export_btn = QPushButton(I18N.tr("deliver.start_export", "Start Export"))
        self._export_btn.setObjectName("PrimaryButton")
        self._export_btn.setMinimumHeight(40)
        self._export_btn.clicked.connect(self._start_export)
        button_layout.addWidget(self._export_btn)

        # B2: Stem 分轨导出（独立对话框，QThread worker，与主导出互不影响）
        self._stems_btn = QPushButton(I18N.tr("deliver.export_stems", "Export Stems…"))
        self._stems_btn.setMinimumHeight(40)
        self._stems_btn.setToolTip(I18N.tr(
            "deliver.export_stems_tooltip",
            "将各音频轨 / Bus / D-M-E 分组导出为独立 WAV 分轨",
        ))
        self._stems_btn.clicked.connect(self._export_stems)
        button_layout.addWidget(self._stems_btn)

        # C5: 章节标记导出（YouTube 章节文本 / FFMetadata）
        self._chapters_btn = QPushButton(I18N.tr("deliver.export_chapters", "Export Chapters…"))
        self._chapters_btn.setMinimumHeight(40)
        self._chapters_btn.setToolTip(I18N.tr(
            "deliver.export_chapters_tooltip",
            "把时间线上的章节标记导出为 YouTube 章节文本或 FFMetadata",
        ))
        self._chapters_btn.clicked.connect(self._export_chapters)
        button_layout.addWidget(self._chapters_btn)

        self._cancel_btn = QPushButton(I18N.tr("deliver.cancel", "Cancel"))
        self._cancel_btn.setMinimumHeight(40)
        self._cancel_btn.clicked.connect(self._cancel_export)
        self._cancel_btn.setVisible(False)
        button_layout.addWidget(self._cancel_btn)

        self._pause_btn = QPushButton(I18N.tr("export.pause", "暂停"))
        self._pause_btn.setMinimumHeight(40)
        self._pause_btn.clicked.connect(self._toggle_pause_export)
        self._pause_btn.setVisible(False)
        button_layout.addWidget(self._pause_btn)

        button_layout.addStretch()
        self._main_layout.addLayout(button_layout)

        # 7. Progress Bar
        progress_group = QGroupBox(I18N.tr("deliver.progress_group", "Progress"))
        progress_layout = QVBoxLayout(progress_group)
        progress_layout.setSpacing(8)

        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(True)
        self._progress_bar.setFormat(I18N.tr("deliver.progress_format", "%p% (%v/%m frames)"))
        progress_layout.addWidget(self._progress_bar)

        # 状态文本
        status_row = QHBoxLayout()
        self._status_label = QLabel(I18N.tr("deliver.status_ready", "Ready"))
        self._status_label.setStyleSheet("color: #888;")
        status_row.addWidget(self._status_label)
        self._fps_label = QLabel()
        self._fps_label.setStyleSheet("color: #888;")
        status_row.addWidget(self._fps_label)
        status_row.addStretch()
        progress_layout.addLayout(status_row)

        self._main_layout.addWidget(progress_group)

        # 弹性空间
        self._main_layout.addStretch()

    def _setup_style(self) -> None:
        """设置样式"""
        # 使用全局样式，仅保留必要的本地样式
        input_style = """
            QLineEdit, QComboBox, QSpinBox {
                background-color: #1e1e1e;
                border: 1px solid #3a3a3a;
                border-radius: 4px;
                padding: 6px 8px;
                color: #ccc;
                min-height: 18px;
            }
            QLineEdit:focus, QComboBox:focus, QSpinBox:focus {
                border-color: #ff8800;
            }
            QComboBox::drop-down {
                border: none;
                width: 20px;
            }
            QComboBox::down-arrow {
                image: none;
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-top: 6px solid #888;
                margin-right: 6px;
            }
            QSpinBox::up-button, QSpinBox::down-button {
                background-color: #2a2a2a;
                border: none;
                width: 16px;
            }
            QSpinBox::up-button:hover, QSpinBox::down-button:hover {
                background-color: #3a3a3a;
            }
        """

        self.setStyleSheet(f"""
            QGroupBox {{
                font-weight: bold;
                color: #eee;
                border: 1px solid #3a3a3a;
                border-radius: 6px;
                margin-top: 12px;
                padding-top: 8px;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 0 8px;
                background-color: #252525;
                border-radius: 4px;
            }}
            #PrimaryButton {{
                background-color: #ff8800;
                color: white;
                font-size: 14px;
                font-weight: bold;
                border-radius: 6px;
            }}
            #PrimaryButton:hover {{
                background-color: #ffaa44;
            }}
            #PrimaryButton:disabled {{
                background-color: #555;
                color: #888;
            }}
            QPushButton {{
                background-color: #2a2a2a;
                color: #ccc;
                border: 1px solid #3a3a3a;
                border-radius: 4px;
                padding: 8px 16px;
            }}
            QPushButton:hover {{
                background-color: #3a3a3a;
                border-color: #ff8800;
            }}
            QPushButton:pressed {{
                background-color: #1e1e1e;
            }}
            {input_style}
        """)

    def _setup_connections(self) -> None:
        """设置信号连接"""
        # Range 类型变化
        self._range_combo.currentIndexChanged.connect(self._on_range_changed)

        # Resolution 选择变化
        self._resolution_combo.currentIndexChanged.connect(self._on_resolution_changed)

        # FPS 选择变化
        self._fps_combo.currentIndexChanged.connect(self._on_fps_changed)

        # Codec 选择变化
        self._codec_combo.currentIndexChanged.connect(self._on_codec_changed)
        self._gif_color_count_spin.valueChanged.connect(self._update_estimates)

        # Source 选择变化
        self._source_combo.currentIndexChanged.connect(self._on_source_changed)

        # 自定义范围变化时更新估算
        self._start_frame_spin.valueChanged.connect(self._update_estimates)
        self._end_frame_spin.valueChanged.connect(self._update_estimates)

        # 自定义分辨率变化时更新估算
        self._custom_width_spin.valueChanged.connect(self._update_estimates)
        self._custom_height_spin.valueChanged.connect(self._update_estimates)

        # AUD-12204：浏览/手输输出路径后估算也要刷新
        self._path_input.textChanged.connect(self._update_estimates)

    # =========================================================================
    # 公共接口
    # =========================================================================

    def set_project(self, project: Any) -> None:
        """
        设置 EditProject 引用。

        Args:
            project: EditProject 实例
        """
        self._project = project
        logger.debug("DeliverPage: project set")

    def set_sequence(self, sequence: Any) -> None:
        """
        设置 TimelineSequence 引用。

        Args:
            sequence: TimelineSequence 实例
        """
        self._sequence = sequence

        if sequence:
            self._current_timeline_name = sequence.name
            self._timeline_name_label.setText(sequence.name)
            self._timeline_name_label.setStyleSheet("color: #00aaff;")
            self._update_estimates()
        else:
            self._current_timeline_name = ""
            self._timeline_name_label.setText(I18N.tr("deliver.no_timeline", "No Timeline"))
            self._timeline_name_label.setStyleSheet("color: #888; font-style: italic;")

        logger.debug(f"DeliverPage: sequence set to '{self._current_timeline_name}'")

    def set_timeline_sequences(self, sequences: Any) -> None:
        """P1-02: 设置全量时间线（嵌套序列导出递归解析用）。

        Args:
            sequences: ``{timeline_id: TimelineSequence}`` 字典或 None
        """
        self._timeline_sequences = dict(sequences) if sequences else {}
        # AUD-12202: 此前这里向从未创建的 ``_settings_panel`` 转发（该面板
        # 属于 DeliverWorkspace），每次调用必然抛 AttributeError 再被宽泛
        # except 吞掉——死代码且掩盖真实异常，直接删除。DeliverPage 自身的
        # 导出路径在 _start_export 里直接消费 _timeline_sequences。
        logger.debug("DeliverPage: %d timeline sequences set", len(self._timeline_sequences))

    def _build_export_config(self) -> Any:
        """从 UI 构建 TimelineExportConfig（不做 validate 校验）。

        AUD-12204：validate() 无条件要求 output_path 非空，而时长/大小
        估算根本不依赖 output_path——估算路径用本方法绕开该校验，
        用户尚未选路径时估算也能工作。
        """
        # 获取 Source
        source = self._source_combo.currentData()

        # 获取 Range
        range_type = self._range_combo.currentData()
        start_frame = self._start_frame_spin.value() if range_type == ExportRangeType.CUSTOM else 0
        end_frame = self._end_frame_spin.value() if range_type == ExportRangeType.CUSTOM else -1
        if range_type == ExportRangeType.CUSTOM and end_frame < 0:
            # "To End" 哨兵（AUD-12200）：spin 值为负（minimum=-1，显示
            # "To End"）时统一传 -1，TimelineExportConfig 将其解析为导出
            # 到时间线末尾，避免哨兵被钳位成 0 导致导出 0 帧。
            end_frame = -1

        # 获取 Resolution
        resolution = self._get_resolution()

        # 获取 FPS
        fps = self._get_fps()

        # 获取 Codec
        codec = self._codec_combo.currentData()

        # 获取 Quality
        quality = self._get_quality()

        # 获取 Audio
        include_audio = self._audio_check.isChecked()
        if codec == ExportCodec.GIF:
            include_audio = False

        # 获取 Output Path
        output_path = self._path_input.text()

        return TimelineExportConfig(
            source=source,
            timeline_id=self._sequence.timeline_id if self._sequence else None,
            range_type=range_type,
            start_frame=start_frame,
            end_frame=end_frame,
            resolution=resolution,
            fps=fps,
            codec=codec,
            quality=quality,
            gif_color_count=(self._gif_color_count_spin.value()
                             if codec == ExportCodec.GIF else 256),
            include_audio=include_audio,
            output_path=output_path,
        )

    def get_export_config(self) -> Optional[Any]:
        """
        从 UI 获取导出配置。

        Returns:
            TimelineExportConfig 实例（edit available），或 None（edit unavailable 或配置无效）
        """
        if not EDIT_EXPORT_AVAILABLE:
            logger.warning("Edit feature not available, cannot create export config")
            return None

        if not self._sequence:
            logger.warning("No sequence set")
            return None

        config = self._build_export_config()

        # 验证
        errors = config.validate()
        if errors:
            logger.warning(f"Config validation errors: {errors}")
            return None

        return config

    def is_exporting(self) -> bool:
        """是否正在导出"""
        return self._is_exporting

    # =========================================================================
    # 内部方法
    # =========================================================================

    def _get_resolution(self) -> Optional[Tuple[int, int]]:
        """获取分辨率设置"""
        index = self._resolution_combo.currentIndex()

        if index == 0:  # Original
            return None
        elif index == 1:  # 1920x1080
            return (1920, 1080)
        elif index == 2:  # 3840x2160
            return (3840, 2160)
        elif index == 3:  # 1280x720
            return (1280, 720)
        elif index == 4:  # Custom
            return (self._custom_width_spin.value(), self._custom_height_spin.value())

        return None

    def _get_fps(self) -> Optional[float]:
        """获取帧率设置"""
        index = self._fps_combo.currentIndex()

        if index == 0:  # Original
            return None
        elif index == 1:  # 24
            return 24.0
        elif index == 2:  # 25
            return 25.0
        elif index == 3:  # 30
            return 30.0
        elif index == 4:  # 60
            return 60.0
        elif index == 5:  # Custom
            return float(self._custom_fps_spin.value())

        return None

    def _get_quality(self) -> Optional[int]:
        """获取质量设置"""
        index = self._quality_combo.currentIndex()

        if index == 0:  # Default
            return None
        elif index == 1:  # High
            return 18  # CRF 18 for H264/HEVC
        elif index == 2:  # Medium
            return 23
        elif index == 3:  # Low
            return 28

        return None

    def _on_range_changed(self, index: int) -> None:
        """Range 类型变化"""
        range_type = self._range_combo.currentData()
        is_custom = (range_type == ExportRangeType.CUSTOM) if EDIT_EXPORT_AVAILABLE else (range_type == "custom")
        self._custom_range_widget.setVisible(is_custom)
        self._update_estimates()

    def _on_resolution_changed(self, index: int) -> None:
        """Resolution 选择变化"""
        self._custom_resolution_widget.setVisible(index == 4)  # Custom
        self._update_estimates()

    def _on_fps_changed(self, index: int) -> None:
        """FPS 选择变化"""
        self._custom_fps_spin.setVisible(index == 5)  # Custom
        self._update_estimates()

    def _on_codec_changed(self, index: int) -> None:
        """Codec 选择变化"""
        codec = self._codec_combo.currentData()
        is_gif = codec == ExportCodec.GIF or codec == "gif"
        self._gif_color_count_label.setVisible(is_gif)
        self._gif_color_count_spin.setVisible(is_gif)
        self._audio_check.setEnabled(not is_gif)
        if is_gif:
            self._audio_check.setChecked(False)
        self._update_estimates()

    def _on_source_changed(self, index: int) -> None:
        """Source 选择变化"""
        source = self._source_combo.currentData()

        # Timeline 相关选项只在 Timeline source 时启用
        is_timeline = (source == ExportSource.TIMELINE) if EDIT_EXPORT_AVAILABLE else (source == "timeline")

        self._timeline_name_label.setEnabled(is_timeline)
        self._range_combo.setEnabled(is_timeline)

        # 非 Timeline 时隐藏自定义范围；切回 Timeline 时按当前 Range 类型
        # 恢复（AUD-12203：此前只有 _on_range_changed 会恢复，而切回
        # Timeline 不改变 range 下标、不触发该槽，Custom 输入框一去不返）。
        if is_timeline:
            range_type = self._range_combo.currentData()
            is_custom = (
                (range_type == ExportRangeType.CUSTOM)
                if EDIT_EXPORT_AVAILABLE
                else (range_type == "custom")
            )
            self._custom_range_widget.setVisible(is_custom)
        else:
            self._custom_range_widget.setVisible(False)

    def _browse_output_path(self) -> None:
        """浏览输出路径"""
        codec = self._codec_combo.currentData()

        # 根据编码器选择文件类型
        if EDIT_EXPORT_AVAILABLE:
            is_sequence = codec in (ExportCodec.PNG_SEQUENCE, ExportCodec.TIFF_SEQUENCE,
                                    ExportCodec.TOPOS_IMAGE_SEQUENCE)
        else:
            is_sequence = codec in ("png_sequence", "tiff_sequence", "topos_image_sequence")

        if is_sequence:
            # 图像序列输出到目录
            path = QFileDialog.getExistingDirectory(
                self,
                I18N.tr("deliver.select_output_dir", "Select Output Directory"),
                self._path_input.text(),
            )
            if path:
                self._path_input.setText(path)
        else:
            # 视频文件
            # 根据编码器确定默认扩展名
            if EDIT_EXPORT_AVAILABLE:
                ext_map = {
                    ExportCodec.H264: ".mp4",
                    ExportCodec.HEVC: ".mp4",
                    ExportCodec.GIF: ".gif",
                    ExportCodec.PRORES_422: ".mov",
                    ExportCodec.PRORES_422HQ: ".mov",
                    ExportCodec.PRORES_4444: ".mov",
                    ExportCodec.DNXHR_SQ: ".mov",
                    ExportCodec.DNXHR_HQ: ".mov",
                }
            else:
                ext_map = {
                    "h264": ".mp4",
                    "gif": ".gif",
                    "prores_422": ".mov",
                    "prores_422hq": ".mov",
                }
            default_ext = ext_map.get(codec, ".mov")

            # 默认文件名
            default_name = f"{self._current_timeline_name or 'export'}{default_ext}"

            path, _ = QFileDialog.getSaveFileName(
                self,
                I18N.tr("deliver.select_output_file", "Select Output File"),
                os.path.join(self._path_input.text() or os.path.expanduser("~"), default_name),
                f"Video Files (*{default_ext});;All Files (*)",
            )
            if path:
                self._path_input.setText(path)

    def _update_estimates(self) -> None:
        """更新时长和大小估算"""
        if not EDIT_EXPORT_AVAILABLE:
            self._duration_label.setText(I18N.tr("deliver.duration", "Duration: --"))
            self._size_label.setText(I18N.tr("deliver.size_estimate", "Size: --"))
            return

        if not self._sequence:
            self._duration_label.setText(I18N.tr("deliver.duration", "Duration: --"))
            self._size_label.setText(I18N.tr("deliver.size_estimate", "Size: --"))
            return

        # 获取当前配置（AUD-12204：估算走未校验的构建路径——validate() 的
        # output_path 必填校验与估算无关，不能把估算挡死在"未选路径"上）。
        config = self._build_export_config()

        try:
            # 估算时长
            duration = estimate_export_duration(config, self._sequence)
            minutes = int(duration // 60)
            seconds = int(duration % 60)
            self._duration_label.setText(f"{I18N.tr('deliver.duration', 'Duration')}: {minutes}:{seconds:02d}")

            # 估算大小
            size_bytes = estimate_export_size(config, self._sequence)
            if size_bytes < 1024 * 1024:
                size_str = f"{size_bytes / 1024:.1f} KB"
            elif size_bytes < 1024 * 1024 * 1024:
                size_str = f"{size_bytes / (1024 * 1024):.1f} MB"
            else:
                size_str = f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"
            self._size_label.setText(f"{I18N.tr('deliver.size_estimate', 'Size')}: ~{size_str}")

        except Exception as e:
            logger.warning(f"Failed to estimate: {e}")

    def _export_stems(self) -> None:
        """B2: 打开 Stem 分轨导出对话框（当前时间线 + 工程取源）。"""
        if self._sequence is None:
            QMessageBox.warning(
                self,
                I18N.tr("deliver.no_timeline_title", "No Timeline"),
                I18N.tr("deliver.no_timeline_message", "Please select a timeline to export."),
            )
            return
        from src.features.edit.ui.stem_export_dialog import StemExportDialog

        # 对话框自带 QThread worker 与进度/取消；模态运行避免重复打开。
        StemExportDialog(
            self._sequence, self._project, parent=self,
            sequence_resolver=dict(self._timeline_sequences).get,
        ).exec()

    def _export_chapters(self) -> None:
        """C5: 打开章节标记导出对话框（YouTube 文本 / FFMetadata）。"""
        if self._sequence is None:
            QMessageBox.warning(
                self,
                I18N.tr("deliver.no_timeline_title", "No Timeline"),
                I18N.tr("deliver.no_timeline_message", "Please select a timeline to export."),
            )
            return
        from src.features.edit.ui.chapter_export_dialog import ChapterExportDialog

        ChapterExportDialog(self._sequence, parent=self).exec()

    def _start_export(self) -> None:
        """开始导出"""
        # 检查 edit feature 是否可用
        if not EDIT_EXPORT_AVAILABLE:
            QMessageBox.warning(
                self,
                I18N.tr("deliver.export_failed_title", "Export Failed"),
                I18N.tr("deliver.edit_unavailable", "Timeline export unavailable (edit feature disabled)"),
            )
            return

        # 检查是否有序列
        if not self._sequence:
            QMessageBox.warning(
                self,
                I18N.tr("deliver.no_timeline_title", "No Timeline"),
                I18N.tr("deliver.no_timeline_message", "Please select a timeline to export."),
            )
            return

        # 获取配置
        config = self.get_export_config()
        if config is None:
            QMessageBox.warning(
                self,
                I18N.tr("deliver.invalid_config_title", "Invalid Configuration"),
                I18N.tr("deliver.invalid_config_message", "Please check export settings."),
            )
            return

        # 检查输出路径
        if not config.output_path:
            QMessageBox.warning(
                self,
                I18N.tr("deliver.no_path_title", "No Output Path"),
                I18N.tr("deliver.no_path_message", "Please select an output path."),
            )
            return

        # 检查是否正在导出
        if self._is_exporting:
            logger.warning("Already exporting")
            return

        logger.info(f"Starting export: {config.output_path}")

        # 更新 UI 状态
        self._is_exporting = True
        self._export_started_at = time.perf_counter()
        self._export_paused_at = None
        self._export_paused_total = 0.0
        self._export_paused = False
        self._export_btn.setEnabled(False)
        self._export_btn.setVisible(False)
        self._cancel_btn.setVisible(True)
        self._pause_btn.setVisible(True)
        self._pause_btn.setText(I18N.tr("export.pause", "暂停"))
        self._status_label.setText(I18N.tr("deliver.status_exporting", "Exporting..."))
        self._fps_label.clear()
        self._progress_bar.setValue(0)

        # 发送开始信号
        self.export_started.emit()

        # 创建导出器
        try:
            self._exporter = create_timeline_exporter(
                project=self._project,
                sequence=self._sequence,
                progress_callback=None,  # Worker 会设置
                timeline_sequences=getattr(self, "_timeline_sequences", None),
            )

            # 创建并启动工作线程
            self._worker = ExportWorker(self._exporter, config)
            self._worker.progress_changed.connect(self._on_progress_changed)
            self._worker.export_complete.connect(self._on_export_complete)
            self._worker.export_error.connect(self._on_export_error)
            self._worker.start()

            # AUD-12205：导出期间应用退出（页面不会收到 closeEvent）也要
            # 先停线程再进入析构，避免 "QThread: Destroyed while thread is
            # still running"。连接仅在导出期间存在，_release_worker 会断开。
            app = QApplication.instance()
            if app is not None:
                app.aboutToQuit.connect(self._teardown_worker)

        except Exception as e:
            logger.exception(f"Failed to start export: {e}")
            self._reset_ui()
            QMessageBox.critical(
                self,
                I18N.tr("deliver.export_failed_title", "Export Failed"),
                str(e),
            )

    def _cancel_export(self) -> None:
        """取消导出"""
        if self._exporter and self._is_exporting:
            self._exporter.cancel_export()
            self._status_label.setText(I18N.tr("deliver.status_canceling", "Canceling..."))
            self._cancel_btn.setEnabled(False)

            # 发送取消信号
            self.export_cancelled.emit()

    def _toggle_pause_export(self) -> None:
        """Pause/resume the active timeline export at a frame boundary."""
        if self._exporter is None or not self._is_exporting:
            return
        if self._export_paused:
            if self._export_paused_at is not None:
                self._export_paused_total += max(
                    0.0, time.perf_counter() - self._export_paused_at
                )
            self._export_paused_at = None
            self._export_paused = False
            self._exporter.resume_export()
            self._pause_btn.setText(I18N.tr("export.pause", "暂停"))
            self._status_label.setText(I18N.tr("deliver.status_exporting", "Exporting..."))
        else:
            self._exporter.pause_export()
            self._export_paused_at = time.perf_counter()
            self._export_paused = True
            self._pause_btn.setText(I18N.tr("export.resume", "继续"))
            self._status_label.setText(I18N.tr("export.status_paused", "导出已暂停"))

    def _on_progress_changed(self, current: int, total: int, percent: float) -> None:
        """进度更新"""
        self._progress_bar.setMaximum(total)
        self._progress_bar.setValue(current)

        started = self._export_started_at
        if started is not None:
            paused_total = self._export_paused_total
            if self._export_paused_at is not None:
                paused_total += max(0.0, time.perf_counter() - self._export_paused_at)
            elapsed = max(1e-6, time.perf_counter() - started - paused_total)
            fps = current / elapsed if current > 0 else 0.0
            self._fps_label.setText(f"{fps:.1f} fps" if fps > 0 else "")

        # 更新状态文本
        if percent < 1.0:
            remaining_frames = total - current
            fps = self._get_fps() or (self._sequence.fps if self._sequence else 24.0)
            remaining_seconds = remaining_frames / fps if fps > 0 else 0
            minutes = int(remaining_seconds // 60)
            seconds = int(remaining_seconds % 60)
            self._status_label.setText(
                f"{I18N.tr('deliver.status_exporting', 'Exporting...')} "
                f"({minutes}:{seconds:02d} remaining)"
            )

    def _on_export_complete(self, success: bool) -> None:
        """导出完成"""
        self._is_exporting = False
        self._reset_ui()

        if success:
            self._progress_bar.setValue(self._progress_bar.maximum())
            self._status_label.setText(I18N.tr("deliver.status_complete", "Export Complete"))
            self._status_label.setStyleSheet("color: #00aaff;")

            # 发送完成信号
            self.export_completed.emit(True)

            # 显示成功消息
            QMessageBox.information(
                self,
                I18N.tr("deliver.export_complete_title", "Export Complete"),
                I18N.tr("deliver.export_complete_message", f"Successfully exported to:\n{self._path_input.text()}"),
            )
        else:
            self._status_label.setText(I18N.tr("deliver.status_cancelled", "Export Cancelled"))
            self._status_label.setStyleSheet("color: #ff8800;")

            # 发送完成信号
            self.export_completed.emit(False)

        # 清理 worker
        self._release_worker(cancel=False)

    def _on_export_error(self, error_message: str) -> None:
        """导出错误"""
        self._is_exporting = False
        self._reset_ui()

        self._status_label.setText(I18N.tr("deliver.status_error", "Export Error"))
        self._status_label.setStyleSheet("color: #ff4444;")

        # 发送完成信号
        self.export_completed.emit(False)

        # 显示错误消息
        QMessageBox.critical(
            self,
            I18N.tr("deliver.export_failed_title", "Export Failed"),
            error_message,
        )

        # 清理 worker
        self._release_worker(cancel=False)

    def _release_worker(self, *, cancel: bool) -> None:
        """分离并等待导出工作线程（AUD-12205）。

        Args:
            cancel: True 时先请求协作取消再等待（页面销毁/应用退出路径，
                worker 的 run() 不跑事件循环，quit() 无效，必须先 cancel
                让 export_timeline 返回）；False 用于导出已自然结束的路径。
        """
        app = QApplication.instance()
        if app is not None:
            try:
                app.aboutToQuit.disconnect(self._teardown_worker)
            except (TypeError, RuntimeError):
                pass  # 未连接（本次导出未注册或已断开）
        worker = self._worker
        self._worker = None
        if worker is None:
            return
        if cancel:
            exporter = self._exporter
            if exporter is not None:
                try:
                    exporter.cancel_export()
                except Exception:  # noqa: BLE001 — 清理路径必须尽力而为
                    logger.exception("cancel_export() raised during teardown")
        worker.quit()
        if not worker.wait(5000):
            logger.warning("Export worker thread did not finish within 5s")

    def _teardown_worker(self) -> None:
        """页面销毁/应用退出前停止导出线程（AUD-12205）。"""
        self._is_exporting = False
        self._release_worker(cancel=True)

    def closeEvent(self, event: QCloseEvent) -> None:
        """关闭页面前等待导出线程退出，避免 QThread 析构崩溃。"""
        self._teardown_worker()
        super().closeEvent(event)

    def _reset_ui(self) -> None:
        """重置 UI 状态"""
        # AUD-12201: 统一在此复位导出状态。此前 _is_exporting 只在
        # complete/error 槽里复位，_start_export 的启动失败路径不经过它们，
        # 一次异常后 "Start Export" 永远被 if self._is_exporting: return 挡死。
        self._is_exporting = False
        self._export_btn.setEnabled(True)
        self._export_btn.setVisible(True)
        self._cancel_btn.setVisible(False)
        self._cancel_btn.setEnabled(True)
        self._pause_btn.setVisible(False)
        self._pause_btn.setText(I18N.tr("export.pause", "暂停"))
        self._export_paused = False
        self._export_paused_at = None

        # 清理导出器
        if self._exporter:
            self._exporter = None
