"""
NewTimelineDialog - 新建时间线对话框。

参考 Premiere Pro 的 New Sequence 对话框，提供完整的时间线参数配置：
- 时间线名称
- 时基（帧率）
- 视频设置：分辨率、像素宽高比、场序、显示格式
- 音频设置：采样率、轨道类型、显示格式
- 起始时间码
"""

from __future__ import annotations

from fractions import Fraction
from typing import Optional

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit,
    QGroupBox, QDialogButtonBox,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QRegularExpressionValidator
from PyQt6.QtCore import QRegularExpression

from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import C
from src.shared.ui.widgets.drag_value import DragValue
from src.shared.ui.widgets.chevron_combo import ChevronComboBox
from src.shared.assets.icon_manager import make_link_button


# =============================================================================
# Presets
# =============================================================================

# FPS presets: (display_label, fps_float, numerator, denominator)
FPS_PRESETS = [
    ("23.976", 23.976, 24000, 1001),
    ("24", 24.0, 24, 1),
    ("25", 25.0, 25, 1),
    ("29.97", 29.97, 30000, 1001),
    ("30", 30.0, 30, 1),
    ("50", 50.0, 50, 1),
    ("59.94", 59.94, 60000, 1001),
    ("60", 60.0, 60, 1),
]

# Resolution presets: name -> (width, height)
RESOLUTION_PRESETS = {
    "4K DCI (4096x2160)": (4096, 2160),
    "4K UHD (3840x2160)": (3840, 2160),
    "2K DCI (2048x1080)": (2048, 1080),
    "2K QHD (2560x1440)": (2560, 1440),
    "1080p (1920x1080)": (1920, 1080),
    "720p (1280x720)": (1280, 720),
    "480p (854x480)": (854, 480),
}

# Pixel aspect ratio presets: (display_name, ratio_float)
PIXEL_ASPECT_RATIOS = [
    ("Square Pixels (1.0)", 1.0),
    ("DV NTSC (0.9091)", 0.9091),
    ("DV PAL (1.0926)", 1.0926),
    ("HD Anamorphic (1.3333)", 1.3333),
]

# Field order presets
FIELD_ORDERS = [
    ("progressive", "No Fields (Progressive)"),
    ("upper", "Upper Field First"),
    ("lower", "Lower Field First"),
]

# Sample rate presets
SAMPLE_RATE_PRESETS = [
    ("44.1 kHz", 44100),
    ("48 kHz", 48000),
    ("96 kHz", 96000),
]

# Audio track type presets
AUDIO_TRACK_TYPES = [
    ("mono", "Mono"),
    ("stereo", "Stereo"),
    ("51", "5.1 Surround"),
]


def _gcd(a: int, b: int) -> int:
    while b:
        a, b = b, a % b
    return a


def _aspect_label(w: int, h: int) -> str:
    g = _gcd(w, h)
    return f"{w // g}:{h // g}"


# =============================================================================
# NewTimelineDialog
# =============================================================================

class NewTimelineDialog(QDialog):
    """
    新建时间线配置对话框。

    参照 Premiere Pro New Sequence 对话框设计，提供完整的时间线参数配置。
    从 ConfigManager 读取默认值，用户修改后创建时间线。

    Usage:
        dialog = NewTimelineDialog(config_manager, parent=main_window)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            settings = dialog.get_settings()
    """

    def __init__(
        self,
        config_manager=None,
        parent=None,
        existing_names=None,
        initial_settings: Optional[dict] = None,
        edit_mode: bool = False,
    ) -> None:
        super().__init__(parent)
        self._config = config_manager
        self._existing_names: set[str] = set(existing_names or [])
        self._initial_settings = dict(initial_settings or {})
        self._edit_mode = edit_mode
        self._original_name = self._initial_settings.get("name", "")
        # 分辨率宽高比锁定状态（与 Project Settings 同款 make_link_button）。
        self._aspect_locked = False
        self._aspect_ratio = 16.0 / 9.0
        # AUD-20310: 非预设 fps 序列的 Custom 标记。(num, den, value)，
        # None 表示当前 combo 选中项是常规预设。
        self._custom_fps: Optional[tuple[Optional[int], Optional[int], float]] = None
        self._setup_ui()
        self._apply_styles()
        self._load_defaults()
        if self._initial_settings:
            self._apply_initial_settings(self._initial_settings)

    # =========================================================================
    # UI Setup
    # =========================================================================

    def _setup_ui(self) -> None:
        self.setWindowTitle(
            I18N.tr("timeline.settings_title", "Timeline Settings")
            if self._edit_mode
            else I18N.tr("timeline.new_title", "New Timeline")
        )
        self.setMinimumSize(480, 620)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)

        # ── Common form layout style ──────────────────────────
        def _make_form_layout() -> QFormLayout:
            fl = QFormLayout()
            fl.setSpacing(8)
            fl.setContentsMargins(12, 16, 12, 12)
            fl.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            fl.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            fl.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
            return fl

        # ── Timeline Name ─────────────────────────────────────
        name_group = QGroupBox(I18N.tr("timeline.name_group", "Timeline"))
        name_layout = _make_form_layout()

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText(
            I18N.tr("timeline.name_placeholder", "Timeline 1")
        )
        self.name_edit.setMinimumWidth(260)
        name_layout.addRow(
            I18N.tr("timeline.name", "Name:"), self.name_edit
        )

        # Start Timecode
        self.start_tc_edit = QLineEdit("00:00:00:00")
        self.start_tc_edit.setMaxLength(11)
        tc_regex = QRegularExpression(r"\d{2}:\d{2}:\d{2}:\d{2}")
        self.start_tc_edit.setValidator(
            QRegularExpressionValidator(tc_regex, self.start_tc_edit)
        )
        name_layout.addRow(
            I18N.tr("timeline.start_timecode", "Start Timecode:"), self.start_tc_edit
        )
        name_group.setLayout(name_layout)
        layout.addWidget(name_group)

        # ── Video Settings ─────────────────────────────────────
        video_group = QGroupBox(I18N.tr("timeline.video", "Video"))
        video_layout = _make_form_layout()

        # Timebase (FPS)
        self.fps_combo = ChevronComboBox()
        self.fps_combo.setMinimumWidth(260)
        for label, fps_val, _num, _den in FPS_PRESETS:
            self.fps_combo.addItem(
                f"{label} {I18N.tr('timeline.fps_unit', 'fps')}",
                fps_val,
            )
        self.fps_combo.currentIndexChanged.connect(self._on_fps_changed)
        video_layout.addRow(
            I18N.tr("timeline.timebase", "Timebase:"), self.fps_combo
        )

        # Frame Size — 预设下拉（ChevronComboBox 自带全局风格）。
        self.resolution_combo = ChevronComboBox()
        self.resolution_combo.setMinimumWidth(260)
        self.resolution_combo.addItem(
            I18N.tr("timeline.custom_resolution", "Custom")
        )
        for preset_name in RESOLUTION_PRESETS:
            self.resolution_combo.addItem(preset_name)
        self.resolution_combo.currentIndexChanged.connect(self._on_resolution_changed)
        video_layout.addRow(
            I18N.tr("timeline.frame_size", "Frame Size:"), self.resolution_combo
        )

        # 宽高 + 锁 —— Project Settings 风格：W [width] [🔒] H [height]。
        # 用 DragValue 取代 QSpinBox（自带拖拽 + 全局风格），锁按钮用
        # make_link_button（锁定=橙 / 解锁=灰），与 Project Settings 完全一致。
        # 原来的 × 分隔符和只读 aspect_label 由锁定按钮取代（锁住即固定比例）。
        res_layout = QHBoxLayout()
        res_layout.setSpacing(4)

        w_lbl = QLabel(I18N.tr("composite.project_settings.w_label", "W"))
        w_lbl.setStyleSheet("color: #888; font-size: 10px;")
        res_layout.addWidget(w_lbl)
        self.width_spin = DragValue("", 64, 16384, 1920.0, decimals=0)
        self.width_spin.setFixedValueWidth(64)
        res_layout.addWidget(self.width_spin)

        self.aspect_lock_btn = make_link_button(
            initial_linked=False,
            tooltip=I18N.tr("timeline.aspect_lock", "Lock aspect ratio"),
        )
        self.aspect_lock_btn.toggled.connect(self._on_aspect_lock_toggled)
        res_layout.addWidget(self.aspect_lock_btn)

        h_lbl = QLabel(I18N.tr("composite.project_settings.h_label", "H"))
        h_lbl.setStyleSheet("color: #888; font-size: 10px;")
        res_layout.addWidget(h_lbl)
        self.height_spin = DragValue("", 64, 16384, 1080.0, decimals=0)
        self.height_spin.setFixedValueWidth(64)
        res_layout.addWidget(self.height_spin)

        # 保留只读宽高比标签（锁定按钮旁的辅助显示）。
        self.aspect_label = QLabel("16:9")
        self.aspect_label.setStyleSheet("color: #888; font-size: 10px;")
        res_layout.addWidget(self.aspect_label)
        res_layout.addStretch(1)

        video_layout.addRow("", res_layout)

        self.width_spin.valueChanged.connect(self._on_width_changed)
        self.height_spin.valueChanged.connect(self._on_height_changed)

        # Pixel Aspect Ratio
        self.par_combo = ChevronComboBox()
        self.par_combo.setMinimumWidth(260)
        for label, ratio in PIXEL_ASPECT_RATIOS:
            self.par_combo.addItem(label, ratio)
        video_layout.addRow(
            I18N.tr("timeline.pixel_aspect_ratio", "Pixel Aspect Ratio:"),
            self.par_combo,
        )

        # Field Order
        self.field_combo = ChevronComboBox()
        self.field_combo.setMinimumWidth(260)
        for key, label in FIELD_ORDERS:
            self.field_combo.addItem(label, key)
        video_layout.addRow(
            I18N.tr("timeline.field_order", "Fields:"), self.field_combo
        )

        # Display Format
        self.display_fmt_combo = ChevronComboBox()
        self.display_fmt_combo.setMinimumWidth(260)
        self._rebuild_display_formats()
        video_layout.addRow(
            I18N.tr("timeline.display_format", "Display Format:"),
            self.display_fmt_combo,
        )
        video_group.setLayout(video_layout)
        layout.addWidget(video_group)

        # ── Audio Settings ─────────────────────────────────────
        audio_group = QGroupBox(I18N.tr("timeline.audio", "Audio"))
        audio_layout = _make_form_layout()

        # Sample Rate
        self.sample_rate_combo = ChevronComboBox()
        self.sample_rate_combo.setMinimumWidth(260)
        for label, value in SAMPLE_RATE_PRESETS:
            self.sample_rate_combo.addItem(label, value)
        audio_layout.addRow(
            I18N.tr("timeline.sample_rate", "Sample Rate:"),
            self.sample_rate_combo,
        )

        # Audio Track Type
        self.audio_track_combo = ChevronComboBox()
        self.audio_track_combo.setMinimumWidth(260)
        for key, label in AUDIO_TRACK_TYPES:
            self.audio_track_combo.addItem(label, key)
        audio_layout.addRow(
            I18N.tr("timeline.audio_track_type", "Track Type:"),
            self.audio_track_combo,
        )

        # Audio Display Format
        self.audio_fmt_combo = ChevronComboBox()
        self.audio_fmt_combo.setMinimumWidth(260)
        self.audio_fmt_combo.addItem(
            I18N.tr("timeline.audio_samples", "Audio Samples"), "samples"
        )
        self.audio_fmt_combo.addItem(
            I18N.tr("timeline.milliseconds", "Milliseconds"), "ms"
        )
        audio_layout.addRow(
            I18N.tr("timeline.audio_display_format", "Display Format:"),
            self.audio_fmt_combo,
        )
        audio_group.setLayout(audio_layout)
        layout.addWidget(audio_group)

        layout.addStretch()

        # ── Buttons ────────────────────────────────────────────
        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        btn_box.accepted.connect(self._on_accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)
        self._btn_box = btn_box
        # OK 按钮用项目标志性橙色（与工具栏 Export 按钮一致），
        # 通过 objectName 让 _apply_styles 的 #ok_button 选择器命中。
        ok_button = btn_box.button(QDialogButtonBox.StandardButton.Ok)
        if ok_button is not None:
            ok_button.setObjectName("ok_button")
            if self._edit_mode:
                ok_button.setText(I18N.tr("common.save", "Save"))

    def _apply_styles(self) -> None:
        # 注意：QComboBox 已全部换成 ChevronComboBox（自带全局 chevron 风格），
        # 分辨率宽高换成 DragValue（自带全局拖拽风格），它们各自管理自己的样式，
        # 这里不再覆盖它们的 QSS，避免与全局风格冲突。仅保留对话框背景、
        # QLineEdit（名称/时间码）、QGroupBox 标题和按钮样式。
        # 背景色用全局 C.BG_PANEL（#27272b），与 properties 面板等全局面板一致。
        self.setStyleSheet(f"""
            NewTimelineDialog {{
                background-color: {C.BG_PANEL};
                border: 1px solid {C.BORDER_DARK};
            }}
            QLabel {{ color: {C.TEXT_SECONDARY}; }}
            QLineEdit {{
                background-color: {C.BG_INPUT};
                border: 1px solid {C.BORDER_DARK};
                border-radius: 4px;
                padding: 5px 8px;
                color: {C.TEXT_PRIMARY};
                min-height: 26px;
            }}
            QLineEdit:focus {{
                border-color: {C.ORANGE};
            }}
            QGroupBox {{
                color: {C.TEXT_SECONDARY};
                font-weight: bold;
                border: 1px solid {C.BORDER_DARK};
                border-radius: 6px;
                margin-top: 14px;
                padding-top: 6px;
                background: transparent;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 6px;
            }}
            QPushButton {{
                background: {C.BG_BUTTON};
                color: {C.TEXT_BRIGHT};
                border: 1px solid {C.BORDER_MID};
                border-radius: 4px;
                padding: 8px 20px;
                font-size: 13px;
                min-width: 80px;
            }}
            QPushButton:hover {{ background: {C.BG_BUTTON_HOVER}; border-color: {C.ORANGE}; }}
            QPushButton:pressed {{ background: {C.BG_BUTTON_PRESS}; }}
            /* OK 按钮用项目标志性橙色（与工具栏 Export 按钮同款）。 */
            QPushButton#ok_button {{
                background-color: {C.ORANGE_PRESS};
                border: none;
                color: white;
                font-weight: bold;
            }}
            QPushButton#ok_button:hover {{ background-color: {C.ORANGE_HOVER}; }}
            QPushButton#ok_button:pressed {{ background-color: #b35900; }}
        """)

    # =========================================================================
    # Defaults
    # =========================================================================

    def _generate_unique_name(self) -> str:
        """Generate 'Timeline N' with auto-increment to avoid duplicates."""
        base = I18N.tr("timeline.default_name", "Timeline")
        n = 1
        while f"{base} {n}" in self._existing_names:
            n += 1
        return f"{base} {n}"

    def _load_defaults(self) -> None:
        """Load default values from ConfigManager."""
        cfg = self._config

        # Name — auto-increment to avoid duplicates
        default_name = self._generate_unique_name()
        self.name_edit.setText(default_name)

        # FPS
        if cfg:
            default_fps = cfg.get("default_timeline_fps") or "25"
        else:
            default_fps = "25"
        for i, (label, fps_val, _n, _d) in enumerate(FPS_PRESETS):
            if label == default_fps:
                self.fps_combo.setCurrentIndex(i)
                break

        # Resolution
        if cfg:
            w = cfg.get("default_timeline_width") or 1920
            h = cfg.get("default_timeline_height") or 1080
        else:
            w, h = 1920, 1080
        self.width_spin.setValue(int(w))
        self.height_spin.setValue(int(h))
        # Try to match a preset
        matched = False
        for i in range(1, self.resolution_combo.count()):
            preset_name = self.resolution_combo.itemText(i)
            pw, ph = RESOLUTION_PRESETS.get(preset_name, (0, 0))
            if pw == w and ph == h:
                self.resolution_combo.setCurrentIndex(i)
                matched = True
                break
        if not matched:
            self.resolution_combo.setCurrentIndex(0)  # Custom

        # Sample Rate
        if cfg:
            sr = cfg.get("default_timeline_sample_rate") or 48000
        else:
            sr = 48000
        idx = self.sample_rate_combo.findData(int(sr))
        if idx >= 0:
            self.sample_rate_combo.setCurrentIndex(idx)

        # Pixel Aspect Ratio
        if cfg:
            par = cfg.get("default_timeline_pixel_aspect_ratio") or "1.0"
        else:
            par = "1.0"
        idx = self.par_combo.findData(float(par))
        if idx >= 0:
            self.par_combo.setCurrentIndex(idx)

        # Field Order
        if cfg:
            field = cfg.get("default_timeline_field_order") or "progressive"
        else:
            field = "progressive"
        idx = self.field_combo.findData(field)
        if idx >= 0:
            self.field_combo.setCurrentIndex(idx)

        # Audio Track Type
        if cfg:
            atype = cfg.get("default_timeline_audio_track_type") or "stereo"
        else:
            atype = "stereo"
        idx = self.audio_track_combo.findData(atype)
        if idx >= 0:
            self.audio_track_combo.setCurrentIndex(idx)

        # Start Timecode
        if cfg:
            tc = cfg.get("default_timeline_start_timecode") or "00:00:00:00"
        else:
            tc = "00:00:00:00"
        self.start_tc_edit.setText(tc)

    def _apply_initial_settings(self, settings: dict) -> None:
        """Populate the dialog from an existing sequence without changing presets."""
        name = settings.get("name")
        if isinstance(name, str) and name:
            self.name_edit.setText(name)

        fps_num = settings.get("fps_num")
        fps_den = settings.get("fps_den")
        fps_value = settings.get("fps")
        for index, (_label, preset_fps, preset_num, preset_den) in enumerate(FPS_PRESETS):
            if fps_num is not None and fps_den is not None:
                matches = int(fps_num) == preset_num and int(fps_den) == preset_den
            else:
                matches = fps_value is not None and abs(float(fps_value) - preset_fps) < 0.001
            if matches:
                self.fps_combo.setCurrentIndex(index)
                break
        else:
            # AUD-20310: 序列 fps 不在预设内——以 Custom 条目承载原值。
            # 原先 combo 停留在默认预设上，用户仅改名点保存就会把整条
            # 序列按错误 fps 静默重缩放。
            self._set_custom_fps(fps_num, fps_den, fps_value)

        width = settings.get("width")
        height = settings.get("height")
        if width is not None:
            self.width_spin.setValue(float(int(width)))
        if height is not None:
            self.height_spin.setValue(float(int(height)))
        for index in range(1, self.resolution_combo.count()):
            preset = RESOLUTION_PRESETS.get(self.resolution_combo.itemText(index))
            if preset == (int(self.width_spin.value()), int(self.height_spin.value())):
                self.resolution_combo.setCurrentIndex(index)
                break
        else:
            self.resolution_combo.setCurrentIndex(0)

        combo_values = (
            (self.par_combo, "pixel_aspect_ratio"),
            (self.field_combo, "field_order"),
            (self.display_fmt_combo, "display_format"),
            (self.sample_rate_combo, "sample_rate"),
            (self.audio_track_combo, "audio_track_type"),
            (self.audio_fmt_combo, "audio_display_format"),
        )
        for combo, key in combo_values:
            value = settings.get(key)
            if value is None:
                continue
            index = combo.findData(value)
            if index >= 0:
                combo.setCurrentIndex(index)

        timecode = settings.get("start_timecode")
        if isinstance(timecode, str) and timecode:
            self.start_tc_edit.setText(timecode)

    # =========================================================================
    # Event Handlers
    # =========================================================================

    def _on_fps_changed(self, _index: int) -> None:
        # AUD-20310: 用户主动选回常规预设时丢弃 Custom fps 标记。
        if 0 <= _index < len(FPS_PRESETS):
            self._custom_fps = None
        self._rebuild_display_formats()

    def _set_custom_fps(
        self,
        fps_num: Optional[int],
        fps_den: Optional[int],
        fps_value: Optional[float],
    ) -> None:
        """AUD-20310: 用 Custom combo 条目承载非预设的序列 fps。

        保持原值（含分子/分母）待保存时原样返回，绝不静默吸附到预设。
        """
        num = int(fps_num) if fps_num is not None else None
        den = int(fps_den) if fps_den is not None else None
        if fps_value is not None:
            value = float(fps_value)
        elif num is not None and den:
            value = num / den
        else:
            return  # 无任何 fps 信息可展示，保持 combo 现状
        if value <= 0:
            return
        label = (
            f"{I18N.tr('timeline.custom_fps', 'Custom')} "
            f"({value:g} {I18N.tr('timeline.fps_unit', 'fps')})"
        )
        index = self.fps_combo.findText(label)
        if index < 0:
            self.fps_combo.addItem(label, value)
            index = self.fps_combo.count() - 1
        self._custom_fps = (num, den, value)
        self.fps_combo.setCurrentIndex(index)

    def _on_resolution_changed(self, index: int) -> None:
        if index == 0:
            # Custom — keep spin boxes editable
            return
        preset_name = self.resolution_combo.itemText(index)
        res = RESOLUTION_PRESETS.get(preset_name)
        if res:
            # AUD-20309: 选预设时按预设自身的宽高比重锚定锁比例，并在
            # blockSignals 下写入 W/H。原先逐个 setValue 会先后触发
            # _on_width_changed/_on_height_changed 的锁定重算，最终值按
            # 旧锁比例漂移（如 16:9 锁下选 4K DCI 得到 3840x2160）。
            if self._aspect_locked and res[1] > 0:
                self._aspect_ratio = res[0] / res[1]
            self.width_spin.blockSignals(True)
            self.height_spin.blockSignals(True)
            self.width_spin.setValue(float(res[0]))
            self.height_spin.setValue(float(res[1]))
            self.width_spin.blockSignals(False)
            self.height_spin.blockSignals(False)
            self._update_aspect_label()

    def _update_aspect_label(self) -> None:
        w = int(self.width_spin.value())
        h = int(self.height_spin.value())
        self.aspect_label.setText(_aspect_label(w, h))

    # ── 分辨率宽高比锁定（与 Project Settings 同款逻辑） ──────────

    def _on_aspect_lock_toggled(self, checked: bool) -> None:
        self._aspect_locked = checked
        if checked and self.height_spin.value() > 0:
            self._aspect_ratio = (
                self.width_spin.value() / self.height_spin.value())

    def _on_width_changed(self, value: float) -> None:
        if self._aspect_locked and value > 0:
            new_h = max(64, min(16384, int(round(value / self._aspect_ratio))))
            self.height_spin.blockSignals(True)
            self.height_spin.setValue(float(new_h))
            self.height_spin.blockSignals(False)
        self._update_aspect_label()

    def _on_height_changed(self, value: float) -> None:
        if self._aspect_locked and value > 0:
            new_w = max(64, min(16384, int(round(value * self._aspect_ratio))))
            self.width_spin.blockSignals(True)
            self.width_spin.setValue(float(new_w))
            self.width_spin.blockSignals(False)
        self._update_aspect_label()

    def _rebuild_display_formats(self) -> None:
        """Rebuild video display format combo based on current FPS."""
        fps = self.fps_combo.currentData() or 24.0
        self.display_fmt_combo.clear()
        self.display_fmt_combo.addItem(
            f"{fps} {I18N.tr('timeline.fps_timecode', 'fps Timecode')}",
            "timecode",
        )
        self.display_fmt_combo.addItem(
            I18N.tr("timeline.frames", "Frames"),
            "frames",
        )
        self.display_fmt_combo.addItem(
            I18N.tr("timeline.seconds", "Seconds + Frames"),
            "seconds_frames",
        )

    def _on_accept(self) -> None:
        """Validate and accept."""
        name = self.name_edit.text().strip()
        if not name:
            self.name_edit.setFocus()
            return
        if name in self._existing_names and name != self._original_name:
            self.name_edit.setFocus()
            return
        tc = self.start_tc_edit.text().strip()
        # Basic timecode validation
        parts = tc.split(":")
        if len(parts) != 4 or not all(p.isdigit() for p in parts):
            self.start_tc_edit.setFocus()
            return
        self.accept()

    # =========================================================================
    # Public API
    # =========================================================================

    def get_settings(self) -> dict:
        """
        Return timeline creation settings as a dict.

        Keys: name, fps, fps_num, fps_den, width, height,
              pixel_aspect_ratio, field_order, display_format,
              sample_rate, audio_track_type, audio_display_format,
              start_timecode
        """
        fps_idx = self.fps_combo.currentIndex()
        if 0 <= fps_idx < len(FPS_PRESETS):
            _label, fps_value, fps_num, fps_den = FPS_PRESETS[fps_idx]
        else:
            # AUD-20310: Custom fps 条目——按承载的原值返回，绝不吸附预设。
            custom = self._custom_fps
            fps_value = float(self.fps_combo.currentData() or 0.0)
            if fps_value <= 0:
                fps_value = 24.0
            fps_num = custom[0] if custom is not None else None
            fps_den = custom[1] if custom is not None else None
            if not fps_num or not fps_den:
                # 罕见路径（Custom 条目被重新手选而无携带值）：用有理数
                # 近似还原分子/分母，保 value 不变。
                fps_num, fps_den = Fraction(fps_value).limit_denominator(
                    1001
                ).as_integer_ratio()

        return {
            "name": self.name_edit.text().strip(),
            "fps": fps_value,
            "fps_num": fps_num,
            "fps_den": fps_den,
            "width": int(self.width_spin.value()),
            "height": int(self.height_spin.value()),
            "pixel_aspect_ratio": self.par_combo.currentData(),
            "field_order": self.field_combo.currentData(),
            "display_format": self.display_fmt_combo.currentData(),
            "sample_rate": self.sample_rate_combo.currentData(),
            "audio_track_type": self.audio_track_combo.currentData(),
            "audio_display_format": self.audio_fmt_combo.currentData(),
            "start_timecode": self.start_tc_edit.text().strip(),
        }
