"""Composite render-output dialog.

A dedicated export dialog triggered from the Output node's "Render Output"
button. Adapts its contents to the detected composite mode:

  - IMAGE mode  → single-image format options (PNG/JPEG/TIFF/EXR)
  - VIDEO mode  → output-type radio (video file / image sequence) +
                  codec + frame range + fps

Reuses the same visual vocabulary as QuickExportDialog:
ExportSectionWidget sections, ChevronComboBox dropdowns, DragValue numeric
fields, ToggleSwitch toggles.

get_settings() returns a dict consumed by CompositeRenderOutput /
composite_controller.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from PyQt6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from src.shared.export.export_panel import (
    ExportSectionWidget,
    _apply_compact_button_style,
    _apply_text_input_style,
    _make_param_label,
)
from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import apply_combo_dropdown_style
from src.shared.ui.widgets import ChevronComboBox, DragValue

from src.features.composite.render.composite_render_output import CompositeMode


# ── Format presets ────────────────────────────────────────────────────

# Single-image formats: key → (display label, extension, has_quality)
_IMAGE_FORMATS = [
    ("png", "PNG"),
    ("jpeg", "JPEG"),
    ("tiff", "TIFF"),
    ("exr", "OpenEXR"),
    ("webp", "WebP"),
]

# Video codecs: key → (display label, default container ext, supports_crf).
# CP-005: 对齐 shared/export/encoder.py 的 VideoCodec 全集（此前 UI 只列
# h264/hevc/prores/vp9 四项，dnxhr/av1/ffv1/gif 引擎支持却不可达）。
_VIDEO_CODECS = [
    ("h264", "H.264 / AVC", "mp4", True),
    ("hevc", "H.265 / HEVC", "mp4", True),
    ("prores", "Apple ProRes 422", "mov", False),
    ("dnxhr", "DNxHR", "mxf", False),
    ("vp9", "VP9", "webm", True),
    ("av1", "AV1", "mkv", False),
    ("ffv1", "FFV1 (lossless)", "mkv", False),
    # 复查修复（2026-10-14）：gif 编码器在本机 PyAV 构建下 avcodec_open2
    # 即 EINVAL（yuv420p/rgb24 均拒），列入 UI 会在导出时硬失败 —— 移出
    # 对话框；引擎枚举保留（脚本层自定义 pix_fmt 仍可尝试）。
]

# CP-005: 编码器原生像素格式约定。ProRes/DNxHR 是 4:2:2 编码族，yuv420p
# 会让 ffmpeg 报错或劣化；其余编码沿用 yuv420p（导出路径的 HDR/广色阶
# 自动升 10bit 逻辑仅在 yuv420p 时介入，不受此表影响）。
_CODEC_DEFAULT_PIX_FMT = {
    "prores": "yuv422p10le",
    "dnxhr": "yuv422p",
}

# Quality presets for H.264/HEVC → (label, crf)
_CRF_PRESETS = [
    ("low", 28),
    ("medium", 23),
    ("high", 18),
    ("very_high", 14),
]

_RENDER_PASS_LABELS = {
    "beauty": "Beauty",
    "depth": "Depth",
    "normal": "Normal",
    "position": "Position",
    "uv": "UV",
    "object_id": "Object ID",
    "material_id": "Material ID",
    "motion_vector": "Motion Vector",
    "diffuse": "Diffuse",
    "specular": "Specular",
    "emission": "Emission",
    "ao": "Ambient Occlusion",
    "shadow": "Shadow",
}
_RENDER_PASS_ORDER = (
    "beauty", "depth", "normal", "position", "uv", "object_id",
    "material_id", "motion_vector", "diffuse", "specular", "emission",
    "ao", "shadow",
)


class CompositeRenderDialog(QDialog):
    """Export dialog for rendering the current composite to disk.

    Args:
        parent: Parent widget.
        mode: Detected CompositeMode (IMAGE / VIDEO).
        default_dir: Initial output directory.
        default_name: Initial file base name.
        resolution: (width, height) read from the Output node.
        frame_range: (start_frame, end_frame) default for video mode.
        fps: Default fps for video mode.
    """

    def __init__(
        self,
        parent=None,
        mode: CompositeMode = CompositeMode.IMAGE,
        default_dir: str = "",
        default_name: str = "composite",
        resolution: tuple = (1920, 1080),
        frame_range: tuple = (0, 0),
        fps: float = 24.0,
        available_render_passes: Optional[tuple[str, ...]] = None,
        parallel_frames_default: int = 2,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(
            I18N.tr("composite.render_output.title", "渲染输出 / Render Output")
        )
        self.setMinimumWidth(540)

        self._mode = mode
        self._default_dir = default_dir or os.path.expanduser("~")
        self._default_name = default_name
        self._width, self._height = resolution
        self._fps = float(fps)
        # AUD-9600: normalize a possibly-inverted incoming frame range
        # (start > end) so the spin defaults can never seed an invalid state.
        init_start, init_end = frame_range
        if init_end < init_start:
            init_start, init_end = init_end, init_start
        self._start_frame, self._end_frame = init_start, init_end
        # CP-004: 导出并行帧数（=1 串行）。默认取调用方解析的偏好值，
        # 实际生效值仍经 resolve_parallel_workers 按 CPU/上限裁剪。
        self._parallel_default = max(1, int(parallel_frames_default or 2))
        # AUD-9600: anti-recursion lock for the start/end cross-clamp below
        # (spin A clamps spin B, whose valueChanged re-enters spin A's slot).
        self._range_sync_lock: bool = False
        available = available_render_passes or ("beauty",)
        self._available_render_passes = tuple(
            name for name in _RENDER_PASS_ORDER if name in set(available)
        ) or ("beauty",)

        self._build_ui()
        self._apply_mode_visibility()

    # ── UI construction ──────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        # ── Mode badge ──
        root.addWidget(self._build_mode_badge())

        # ── File & Location ──
        file_section = ExportSectionWidget(
            I18N.tr("export.output_section", "File & Location"),
            header_mode="none",
        )
        file_layout = file_section.content_layout()

        # Name
        name_row = QHBoxLayout()
        name_row.setContentsMargins(0, 2, 0, 2)
        name_row.setSpacing(4)
        name_row.addWidget(_make_param_label(I18N.tr("export.file_name", "文件名")))
        self.name_input = QLineEdit(self._default_name)
        _apply_text_input_style(self.name_input)
        name_row.addWidget(self.name_input, 1)
        file_layout.addLayout(name_row)

        # Directory
        path_row = QHBoxLayout()
        path_row.setContentsMargins(0, 2, 0, 2)
        path_row.setSpacing(4)
        path_row.addWidget(_make_param_label(I18N.tr("export.location", "位置:")))
        self.path_input = QLineEdit(self._default_dir)
        self.path_input.setPlaceholderText(
            I18N.tr("export.select_dir_placeholder", "选择导出目录...")
        )
        _apply_text_input_style(self.path_input)
        path_row.addWidget(self.path_input, 1)
        self.path_btn = QPushButton(I18N.tr("export.choose_btn", "选择..."))
        self.path_btn.clicked.connect(self._pick_directory)
        _apply_compact_button_style(self.path_btn)
        path_row.addWidget(self.path_btn)
        file_layout.addLayout(path_row)

        root.addWidget(file_section)

        # ── Render pass ──
        pass_section = ExportSectionWidget(
            I18N.tr("composite.render_output.render_pass", "Render Pass"),
            header_mode="none",
        )
        pass_layout = pass_section.content_layout()
        pass_row = QHBoxLayout()
        pass_row.setContentsMargins(0, 2, 0, 2)
        pass_row.setSpacing(4)
        pass_row.addWidget(
            _make_param_label(
                I18N.tr("composite.render_output.pass", "通道:")
            )
        )
        self.render_pass_combo = ChevronComboBox()
        for pass_name in self._available_render_passes:
            self.render_pass_combo.addItem(
                _RENDER_PASS_LABELS.get(pass_name, pass_name),
                userData=pass_name,
            )
        apply_combo_dropdown_style(self.render_pass_combo)
        pass_row.addWidget(self.render_pass_combo, 1)
        pass_layout.addLayout(pass_row)
        root.addWidget(pass_section)

        # ── Image options ──
        self._image_section = ExportSectionWidget(
            I18N.tr("composite.render_output.image_options", "图像选项"),
            header_mode="none",
        )
        self._build_image_options(self._image_section.content_layout())
        root.addWidget(self._image_section)

        # ── Video options ──
        self._video_section = ExportSectionWidget(
            I18N.tr("composite.render_output.video_options", "视频选项"),
            header_mode="none",
        )
        self._build_video_options(self._video_section.content_layout())
        root.addWidget(self._video_section)

        # ── Frame range (video only) ──
        self._range_section = ExportSectionWidget(
            I18N.tr("composite.render_output.frame_range", "帧范围"),
            header_mode="none",
        )
        self._build_frame_range(self._range_section.content_layout())
        root.addWidget(self._range_section)

        root.addStretch()

        # ── Bottom buttons ──
        root.addLayout(self._build_button_row())

    def _build_mode_badge(self) -> QWidget:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        if self._mode == CompositeMode.VIDEO:
            text = I18N.tr(
                "composite.render_output.mode_video",
                "视频模式 / Video Mode",
            )
            color = "#2d8c3c"
        else:
            text = I18N.tr(
                "composite.render_output.mode_image",
                "图片模式 / Image Mode",
            )
            color = "#007aff"
        badge = QLabel(text)
        badge.setStyleSheet(
            f"color: white; background: {color}; padding: 3px 10px;"
            f"border-radius: 9px; font-size: 11px; font-weight: bold;"
        )
        row.addWidget(badge)
        row.addStretch()
        holder = QWidget()
        holder.setLayout(row)
        return holder

    def _build_image_options(self, layout: QVBoxLayout) -> None:
        # Format
        fmt_row = QHBoxLayout()
        fmt_row.setContentsMargins(0, 2, 0, 2)
        fmt_row.setSpacing(4)
        fmt_row.addWidget(
            _make_param_label(I18N.tr("export.format", "格式:"))
        )
        self.image_format_combo = ChevronComboBox()
        for key, label in _IMAGE_FORMATS:
            self.image_format_combo.addItem(label, userData=key)
        apply_combo_dropdown_style(self.image_format_combo)
        fmt_row.addWidget(self.image_format_combo, 1)
        layout.addLayout(fmt_row)

        # Quality（AUD-9602：质量控件当前对所有图像格式恒显示、恒写入导出
        # 选项——不止 JPEG/WebP，注释按实际行为修正；按格式显隐属行为变更，
        # 挂账暂缓）。
        q_row = QHBoxLayout()
        q_row.setContentsMargins(0, 2, 0, 2)
        q_row.setSpacing(4)
        q_row.addWidget(
            _make_param_label(I18N.tr("export.quality", "质量:"))
        )
        self.quality_spin = DragValue(
            "",
            min_v=1,
            max_v=100,
            default=95,
            decimals=0,
            suffix="%",
        )
        q_row.addWidget(self.quality_spin, 1)
        layout.addLayout(q_row)

    def _build_video_options(self, layout: QVBoxLayout) -> None:
        # Output type radio: video file / image sequence
        type_row = QHBoxLayout()
        type_row.setContentsMargins(0, 2, 0, 2)
        type_row.setSpacing(12)
        self.radio_video = QRadioButton(
            I18N.tr("composite.render_output.type_video", "视频文件")
        )
        self.radio_sequence = QRadioButton(
            I18N.tr("composite.render_output.type_sequence", "图片序列")
        )
        self.radio_video.setChecked(True)
        self._video_type_group = QButtonGroup(self)
        self._video_type_group.addButton(self.radio_video)
        self._video_type_group.addButton(self.radio_sequence)
        type_row.addWidget(self.radio_video)
        type_row.addWidget(self.radio_sequence)
        type_row.addStretch()
        layout.addLayout(type_row)

        # Codec (video file only) — wrap in a holder so we can show/hide it
        codec_row = QHBoxLayout()
        codec_row.setContentsMargins(0, 2, 0, 2)
        codec_row.setSpacing(4)
        codec_row.addWidget(
            _make_param_label(I18N.tr("composite.render_output.codec", "编码:"))
        )
        self.codec_combo = ChevronComboBox()
        for key, label, _ext, _crf in _VIDEO_CODECS:
            self.codec_combo.addItem(label, userData=key)
        apply_combo_dropdown_style(self.codec_combo)
        codec_row.addWidget(self.codec_combo, 1)
        self._codec_row_holder = self._wrap_row(codec_row, layout)
        # CP-005: CRF 质量档只对支持 CRF 的编码显示（ProRes/DNxHR 走
        # profile/码率语义，显示 CRF 档会误导）。
        self.codec_combo.currentIndexChanged.connect(
            self._on_codec_changed
        )

        # Quality preset (video file only)
        cq_row = QHBoxLayout()
        cq_row.setContentsMargins(0, 2, 0, 2)
        cq_row.setSpacing(4)
        cq_row.addWidget(
            _make_param_label(I18N.tr("export.quality", "质量:"))
        )
        self.video_quality_combo = ChevronComboBox()
        for key, crf in _CRF_PRESETS:
            label = I18N.tr(
                f"composite.render_output.q_{key}",
                {"low": "低", "medium": "中", "high": "高", "very_high": "极高"}.get(
                    key, key
                ),
            )
            self.video_quality_combo.addItem(label, userData=crf)
        apply_combo_dropdown_style(self.video_quality_combo)
        cq_row.addWidget(self.video_quality_combo, 1)
        self._vquality_row_holder = self._wrap_row(cq_row, layout)

        # Sequence format (image sequence only)
        seq_row = QHBoxLayout()
        seq_row.setContentsMargins(0, 2, 0, 2)
        seq_row.setSpacing(4)
        seq_row.addWidget(
            _make_param_label(I18N.tr("export.format", "格式:"))
        )
        self.sequence_format_combo = ChevronComboBox()
        for key, label in _IMAGE_FORMATS:
            self.sequence_format_combo.addItem(label, userData=key)
        apply_combo_dropdown_style(self.sequence_format_combo)
        seq_row.addWidget(self.sequence_format_combo, 1)
        self._seqfmt_row_holder = self._wrap_row(seq_row, layout)

        # Wire radio toggle
        self.radio_video.toggled.connect(self._on_video_type_changed)

    def _build_frame_range(self, layout: QVBoxLayout) -> None:
        row = QHBoxLayout()
        row.setContentsMargins(0, 2, 0, 2)
        row.setSpacing(6)
        row.addWidget(
            _make_param_label(I18N.tr("composite.render_output.start", "起始"))
        )
        self.start_frame_spin = DragValue(
            "", min_v=0, max_v=1000000, default=self._start_frame, decimals=0
        )
        row.addWidget(self.start_frame_spin)
        row.addWidget(
            _make_param_label(I18N.tr("composite.render_output.end", "结束"))
        )
        self.end_frame_spin = DragValue(
            "", min_v=0, max_v=1000000, default=self._end_frame, decimals=0
        )
        row.addWidget(self.end_frame_spin)
        row.addWidget(
            _make_param_label(I18N.tr("composite.render_output.fps", "FPS"))
        )
        self.fps_spin = DragValue(
            "",
            min_v=1.0,
            max_v=240.0,
            default=self._fps,
            decimals=3,
        )
        row.addWidget(self.fps_spin)
        # CP-004: 并行帧数（1=串行）。渲染线程各自持 GL 上下文，超配会
        # 被引擎按 CPU/上限裁剪，这里仅表达用户意图。
        from src.features.composite.render.parallel_frame_render import (
            MAX_PARALLEL_WORKERS,
        )
        row.addWidget(
            _make_param_label(
                I18N.tr("composite.render_output.parallel", "并行")
            )
        )
        self.parallel_spin = DragValue(
            "", min_v=1, max_v=MAX_PARALLEL_WORKERS,
            default=min(self._parallel_default, MAX_PARALLEL_WORKERS),
            decimals=0,
        )
        row.addWidget(self.parallel_spin)
        layout.addLayout(row)

        # AUD-9600: cross-clamp the frame range so start <= end always holds.
        # Raising start above end pushes end up; lowering end below start
        # pulls start down (the user's latest edit wins). Without this an
        # inverted range flowed silently downstream: the serial render path
        # clamped the total to 0 frames, the parallel sequence path produced
        # a negative frame count.
        self.start_frame_spin.valueChanged.connect(self._on_start_frame_changed)
        self.end_frame_spin.valueChanged.connect(self._on_end_frame_changed)

        # Resolution preview (read-only info)
        info = QLabel(
            I18N.tr(
                "composite.render_output.resolution_info",
                "分辨率: {w} × {h}",
            ).format(w=self._width, h=self._height)
        )
        info.setStyleSheet("color: #888; font-size: 10px;")
        layout.addWidget(info)

    def _build_button_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addStretch()
        cancel_btn = QPushButton(I18N.tr("dialog.cancel", "取消"))
        _apply_compact_button_style(cancel_btn)
        cancel_btn.clicked.connect(self.reject)
        row.addWidget(cancel_btn)

        render_btn = QPushButton(
            I18N.tr("composite.render_output.render_btn", "渲染 / Render")
        )
        render_btn.setStyleSheet(
            "QPushButton{background:#cc6600;color:white;border:none;"
            "border-radius:4px;padding:6px 18px;font-weight:bold;}"
            "QPushButton:hover{background:#e67300;}"
            "QPushButton:pressed{background:#b35900;}"
        )
        render_btn.clicked.connect(self._on_render_clicked)
        row.addWidget(render_btn)
        return row

    # ── Helpers ──────────────────────────────────────────────────────

    def _on_start_frame_changed(self, value: float) -> None:
        """Push the end frame up when start rises above it (AUD-9600)."""
        if self._range_sync_lock:
            return
        self._range_sync_lock = True
        try:
            if value > self.end_frame_spin.value():
                self.end_frame_spin.setValue(value)
        finally:
            self._range_sync_lock = False

    def _on_end_frame_changed(self, value: float) -> None:
        """Pull the start frame down when end drops below it (AUD-9600)."""
        if self._range_sync_lock:
            return
        self._range_sync_lock = True
        try:
            if value < self.start_frame_spin.value():
                self.start_frame_spin.setValue(value)
        finally:
            self._range_sync_lock = False

    def _on_render_clicked(self) -> None:
        """Accept only when the frame range is valid (AUD-9600 backstop).

        The cross-clamp already keeps start <= end through normal UI edits;
        this guards code paths that bypass it (e.g. programmatic setValue
        with blocked signals) and gives the user an explicit message
        instead of a silent zero-frame render.
        """
        if (
            self._mode == CompositeMode.VIDEO
            and self.start_frame_spin.value() > self.end_frame_spin.value()
        ):
            QMessageBox.warning(
                self,
                I18N.tr(
                    "composite.render_output.invalid_range_title",
                    "无效帧范围 / Invalid Frame Range",
                ),
                I18N.tr(
                    "composite.render_output.invalid_range_text",
                    "起始帧不能大于结束帧。/ Start frame must not exceed "
                    "end frame.",
                ),
            )
            return
        self.accept()

    @staticmethod
    def _wrap_row(row_layout: QHBoxLayout, parent_layout: QVBoxLayout) -> QWidget:
        """Wrap a freshly-built HBoxLayout in a QWidget and add it to the parent.

        The row_layout must NOT yet have been added to parent_layout (this
        method takes sole ownership by setting it as the holder's layout).
        The returned holder can be shown/hidden as a unit.
        """
        holder = QWidget()
        holder.setLayout(row_layout)
        parent_layout.addWidget(holder)
        return holder

    def _pick_directory(self) -> None:
        current = self.path_input.text() or self._default_dir
        dir_path = QFileDialog.getExistingDirectory(
            self,
            I18N.tr("export.select_dir_placeholder", "选择导出目录"),
            current,
        )
        if dir_path:
            self.path_input.setText(dir_path)

    def _apply_mode_visibility(self) -> None:
        if self._mode == CompositeMode.IMAGE:
            self._image_section.setVisible(True)
            self._video_section.setVisible(False)
            self._range_section.setVisible(False)
        else:
            self._image_section.setVisible(False)
            self._video_section.setVisible(True)
            self._range_section.setVisible(True)
            self._on_video_type_changed(self.radio_video.isChecked())

    def _on_video_type_changed(self, is_video: bool) -> None:
        # Video-file-only rows
        self._codec_row_holder.setVisible(is_video)
        self._update_video_quality_visibility()
        # Image-sequence-only rows
        self._seqfmt_row_holder.setVisible(not is_video)

    def _codec_supports_crf(self, codec_key: str) -> bool:
        for key, _label, _ext, supports_crf in _VIDEO_CODECS:
            if key == codec_key:
                return supports_crf
        return False

    def _on_codec_changed(self, _index: int) -> None:
        """CRF 档可见性跟随所选编码（CP-005）。"""
        self._update_video_quality_visibility()

    def _update_video_quality_visibility(self) -> None:
        is_video = self.radio_video.isChecked()
        codec = self.codec_combo.currentData() or "h264"
        self._vquality_row_holder.setVisible(
            is_video and self._codec_supports_crf(codec)
        )

    # ── Result ───────────────────────────────────────────────────────

    def get_settings(self) -> Dict[str, Any]:
        """Return the user's render settings as a dict.

        Shape (always present):
            mode, output_dir, base_name, width, height
        Image mode adds:
            kind="image", image_format, quality
        Video mode adds:
            kind="video_file" | "image_sequence", start_frame, end_frame, fps,
            and either (codec, crf) or (sequence_format)
        """
        out_dir = self.path_input.text().strip() or self._default_dir
        base_name = self.name_input.text().strip() or "composite"

        settings: Dict[str, Any] = {
            "mode": self._mode.value,
            "output_dir": out_dir,
            "base_name": base_name,
            "width": self._width,
            "height": self._height,
            "render_pass": self.render_pass_combo.currentData() or "beauty",
        }

        if self._mode == CompositeMode.IMAGE:
            settings["kind"] = "image"
            settings["image_format"] = self.image_format_combo.currentData()
            settings["quality"] = int(self.quality_spin.value())
            # Full file path (name + ext)
            ext = settings["image_format"]
            settings["output_path"] = os.path.join(
                out_dir, f"{base_name}.{_ext_for(ext)}"
            )
            settings["options"] = {"quality": settings["quality"]}
            return settings

        # Video mode
        # AUD-9600: enforce the start<=end contract on the values that leave
        # this dialog even if a caller bypassed the UI cross-clamp.
        start_frame = int(self.start_frame_spin.value())
        end_frame = max(start_frame, int(self.end_frame_spin.value()))
        settings["start_frame"] = start_frame
        settings["end_frame"] = end_frame
        settings["fps"] = float(self.fps_spin.value())
        # CP-004: 导出并行帧数（worker→render_video/render_image_sequence
        # 的 parallel_frames 参数；1=串行）。
        settings["parallel_frames"] = max(1, int(self.parallel_spin.value()))

        if self.radio_video.isChecked():
            settings["kind"] = "video_file"
            codec = self.codec_combo.currentData() or "h264"
            settings["codec"] = codec
            if self._codec_supports_crf(codec):
                settings["crf"] = self.video_quality_combo.currentData()
            settings["pix_fmt"] = _CODEC_DEFAULT_PIX_FMT.get(
                codec, "yuv420p")
            ext = _container_for_codec(codec)
            settings["output_path"] = os.path.join(
                out_dir, f"{base_name}.{ext}"
            )
        else:
            settings["kind"] = "image_sequence"
            settings["sequence_format"] = self.sequence_format_combo.currentData()
            settings["options"] = {}
            settings["output_path"] = out_dir  # sequence writes into a dir

        return settings


# ── Extension helpers ─────────────────────────────────────────────────


def _ext_for(fmt_key: str) -> str:
    """Canonical file extension for an image-format key."""
    return {"jpeg": "jpg"}.get(fmt_key, fmt_key)


def _container_for_codec(codec_key: str) -> str:
    """Default container extension for a video codec key."""
    for key, _label, ext, _crf in _VIDEO_CODECS:
        if key == codec_key:
            return ext
    return "mp4"
