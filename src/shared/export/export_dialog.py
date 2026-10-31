# DORMANT(2026-08-19 审计): 已实现且有测试，但暂无生产入口（有意休眠）。
# 接线前请勿在生产依赖；若半年内无接线计划建议删除。
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                             QComboBox, QSlider, QPushButton, QFormLayout,
                             QLineEdit, QFileDialog, QFrame, QWidget, QCheckBox, QSpinBox)
from PyQt6.QtCore import Qt
import logging
import os
from typing import Any, Dict, Optional, Tuple

from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import apply_combo_dropdown_style

logger = logging.getLogger(__name__)


class ExportDialog(QDialog):
    def __init__(
        self,
        parent: Optional[QWidget] = None,
        default_dir: str = "",
        default_name: str = "untitled",
        original_size: Tuple[int, int] = (0, 0),
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(I18N.tr("export.dialog_title", "导出图像"))
        self.setMinimumWidth(650)
        self.default_dir = default_dir
        self.default_name = default_name
        self.orig_w, self.orig_h = original_size
        # AUD-33110：resize 结果尺寸以成员属性为数据源（标签文本仅展示，
        # 且写入前缀已本地化，反向解析必然失配）；None = 无有效缩放尺寸。
        self._last_output_size: Optional[Tuple[int, int]] = None

        self.setup_ui()
        self.apply_styles()
        self.on_format_changed(self.format_combo.currentText())
        self.update_resize_ui()

    def setup_ui(self) -> None:
        # Main layout with margins
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(24, 24, 24, 24)
        self.main_layout.setSpacing(20)

        # 1. File Name & Location
        file_box = QFrame()
        file_box.setObjectName("ContentBox")
        file_layout = QFormLayout(file_box)
        file_layout.setContentsMargins(15, 15, 15, 15)
        file_layout.setSpacing(15)

        self.name_input = QLineEdit(self.default_name)
        self.name_input.setPlaceholderText(I18N.tr("export.file_name", "文件名"))

        path_layout = QHBoxLayout()
        self.path_input = QLineEdit(self.default_dir)
        self.path_input.setPlaceholderText(I18N.tr("export.select_dir_placeholder", "选择导出目录..."))
        self.path_input.setMinimumWidth(450)

        self.path_btn = QPushButton(I18N.tr("export.choose_btn", "选择..."))
        self.path_btn.setFixedWidth(80)
        self.path_btn.clicked.connect(self.pick_directory)
        path_layout.addWidget(self.path_input, 1)
        path_layout.addWidget(self.path_btn)

        file_layout.addRow(I18N.tr("export.export", "导出") + ":", self.name_input)
        file_layout.addRow(I18N.tr("export.location", "位置:"), path_layout)
        self.main_layout.addWidget(file_box)

        # 2. Resize Section
        resize_box = QFrame()
        resize_box.setObjectName("ContentBox")
        resize_layout = QVBoxLayout(resize_box)
        resize_layout.setContentsMargins(15, 15, 15, 15)
        resize_layout.setSpacing(10)

        self.resize_check = QCheckBox(I18N.tr("export.resize_image", "调整图像大小"))
        self.resize_check.toggled.connect(self.update_resize_ui)
        resize_layout.addWidget(self.resize_check)

        self.resize_widget = QWidget()
        self.resize_controls = QFormLayout(self.resize_widget)
        self.resize_controls.setContentsMargins(0, 0, 0, 0)
        self.resize_controls.setSpacing(10)

        self.resize_mode = QComboBox()
        self.resize_mode.addItems([
            I18N.tr("export.long_edge", "长边"),
            I18N.tr("export.short_edge", "短边"),
            I18N.tr("export.resize_width", "宽度"),
            I18N.tr("export.resize_height", "高度"),
            I18N.tr("export.percentage", "百分比")
        ])
        self.resize_mode.currentTextChanged.connect(self.update_output_size)

        self.resize_value = QSpinBox()
        self.resize_value.setRange(1, 10000)
        self.resize_value.setValue(2048)
        self.resize_value.setSuffix(I18N.tr("export.px_suffix", " px"))
        self.resize_value.valueChanged.connect(self.update_output_size)

        self.output_size_label = QLabel(f"{I18N.tr('export.result', '结果')}: {self.orig_w} x {self.orig_h}")
        self.output_size_label.setStyleSheet("color: #007aff; font-weight: bold;")

        self.resize_controls.addRow(I18N.tr("export.resize_mode", "模式:"), self.resize_mode)
        self.resize_controls.addRow(I18N.tr("export.resize_value", "数值:"), self.resize_value)
        self.resize_controls.addRow("", self.output_size_label)

        resize_layout.addWidget(self.resize_widget)
        self.main_layout.addWidget(resize_box)

        # 3. Format & Options
        options_box = QFrame()
        options_box.setObjectName("ContentBox")
        self.options_layout = QFormLayout(options_box)
        self.options_layout.setContentsMargins(15, 15, 15, 15)
        self.options_layout.setSpacing(15)

        self.format_combo = QComboBox()
        self.format_combo.addItems([
            I18N.tr("export.fmt_jpeg", "JPEG"),
            I18N.tr("export.fmt_png", "PNG"),
            I18N.tr("export.fmt_exr", "OpenEXR"),
            I18N.tr("export.fmt_tiff", "TIFF"),
            I18N.tr("export.fmt_webp", "WebP")
        ])
        self.format_combo.currentTextChanged.connect(self.on_format_changed)
        apply_combo_dropdown_style(self.format_combo)
        self.options_layout.addRow(I18N.tr("export.format", "格式:"), self.format_combo)

        # JPEG/PNG/WebP Params
        self.quality_widget = QWidget()
        quality_layout = QHBoxLayout(self.quality_widget)
        quality_layout.setContentsMargins(0, 0, 0, 0)
        self.quality_slider = QSlider(Qt.Orientation.Horizontal)
        self.quality_slider.setRange(0, 100)
        self.quality_slider.setValue(85)
        self.quality_val_label = QLabel("85")
        self.quality_val_label.setFixedWidth(25)
        self.quality_slider.valueChanged.connect(lambda v: self.quality_val_label.setText(str(v)))
        quality_layout.addWidget(self.quality_slider)
        quality_layout.addWidget(self.quality_val_label)
        self.options_layout.addRow(I18N.tr("export.quality", "质量:"), self.quality_widget)

        # EXR Specific Params
        self.exr_widget = QWidget()
        exr_layout = QFormLayout(self.exr_widget)
        exr_layout.setContentsMargins(0, 0, 0, 0)
        exr_layout.setSpacing(10)
        self.exr_type_combo = QComboBox()
        self.exr_type_combo.addItems([
            I18N.tr("export.exr_half", "Half (16-bit)"),
            I18N.tr("export.exr_full", "Full (32-bit)")
        ])
        apply_combo_dropdown_style(self.exr_type_combo)
        self.exr_comp_combo = QComboBox()
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
        exr_layout.addRow(I18N.tr("export.precision", "精度:"), self.exr_type_combo)
        exr_layout.addRow(I18N.tr("export.compression", "压缩:"), self.exr_comp_combo)
        self.options_layout.addRow(self.exr_widget)

        self.main_layout.addWidget(options_box)

        # 4. Action Buttons
        button_layout = QHBoxLayout()
        button_layout.setSpacing(12)
        self.cancel_btn = QPushButton(I18N.tr("export.cancel", "取消"))
        self.cancel_btn.clicked.connect(self.reject)
        self.export_btn = QPushButton(I18N.tr("export.export", "导出"))
        self.export_btn.setObjectName("PrimaryButton")
        self.export_btn.setDefault(True)
        self.export_btn.clicked.connect(self.accept)
        button_layout.addStretch()
        button_layout.addWidget(self.cancel_btn)
        button_layout.addWidget(self.export_btn)
        self.main_layout.addLayout(button_layout)

    def apply_styles(self) -> None:
        self.setStyleSheet("""
            QDialog { background-color: #282828; border: 1px solid #444; border-radius: 12px; }
            QLabel { color: #bbb; font-size: 13px; }
            #ContentBox { background-color: transparent; border: 1px solid #404040; border-radius: 10px; }
            QLineEdit, QSpinBox { background-color: #1e1e1e; border: 1px solid #444; border-radius: 6px; padding: 6px 10px; color: #fff; font-size: 13px; }
            QSpinBox::up-button, QSpinBox::down-button { border: none; background: #333; width: 16px; }
            QComboBox { background-color: #252529; border: 1px solid #444; border-radius: 6px; padding: 5px 10px; color: #fff; min-width: 120px; }
            QComboBox::drop-down { border: none; background: transparent; subcontrol-origin: padding; subcontrol-position: right; }
            QPushButton { background-color: #444; border: none; border-radius: 6px; padding: 8px 20px; color: #fff; font-weight: 500; min-width: 80px; }
            QPushButton:hover { background-color: #555; }
            #PrimaryButton { background-color: #007aff; }
            #PrimaryButton:hover { background-color: #006ce6; }
            QCheckBox { color: #ddd; spacing: 8px; }
            QCheckBox::indicator { width: 18px; height: 18px; border-radius: 4px; border: 1px solid #555; background: transparent; }
            QCheckBox::indicator:checked { background: #007aff; border-color: #007aff; }
        """)

    def pick_directory(self) -> None:
        current_path = self.path_input.text()
        dir_path = QFileDialog.getExistingDirectory(self, I18N.tr("export.select_dir_placeholder", "选择导出目录"), current_path)
        if dir_path:
            self.path_input.setText(dir_path)

    def update_resize_ui(self) -> None:
        enabled = self.resize_check.isChecked()
        self.resize_widget.setEnabled(enabled)
        if not enabled:
            # AUD-33110：缩放未启用时清空缓存尺寸，get_settings 不再回读
            self._last_output_size = None
            self.output_size_label.setText(f"{I18N.tr('export.result', '结果')}: {self.orig_w} x {self.orig_h}")
        else:
            self.update_output_size()

    def update_output_size(self) -> None:
        # AUD-33110：原图尺寸无效（0/负）时不计算——此前 orig_w==0 早退后
        # 标签停留 "Result: 0 x 0"，get_settings 解析文本得 resize=(0,0)。
        if not self.resize_check.isChecked() or self.orig_w <= 0 or self.orig_h <= 0:
            return

        mode = self.resize_mode.currentText()
        new_w, new_h = self.orig_w, self.orig_h

        if mode == I18N.tr("export.percentage", "百分比"):
            # AUD-33111：setSuffix/setRange 程序化更新必须 blockSignals——
            # setRange 收紧会 clamp 并 emit valueChanged（QSpinBox 文档化
            # 行为），重入本方法后外层用过期 val 覆盖标签（约 4 倍偏差）；
            # clamp 后重读控件实际值再计算。
            self.resize_value.blockSignals(True)
            self.resize_value.setSuffix(" %")
            self.resize_value.setRange(1, 500)
            self.resize_value.blockSignals(False)
            val = self.resize_value.value()
            new_w = int(self.orig_w * val / 100)
            new_h = int(self.orig_h * val / 100)
        else:
            self.resize_value.blockSignals(True)
            self.resize_value.setSuffix(I18N.tr("export.px_suffix", " px"))
            self.resize_value.setRange(1, 10000)
            self.resize_value.blockSignals(False)
            val = self.resize_value.value()
            if mode == I18N.tr("export.resize_width", "宽度"):
                new_w = val
                new_h = int(self.orig_h * (val / self.orig_w))
            elif mode == I18N.tr("export.resize_height", "高度"):
                new_h = val
                new_w = int(self.orig_w * (val / self.orig_h))
            elif mode == I18N.tr("export.long_edge", "长边"):
                if self.orig_w >= self.orig_h:
                    new_w = val
                    new_h = int(self.orig_h * (val / self.orig_w))
                else:
                    new_h = val
                    new_w = int(self.orig_w * (val / self.orig_h))
            elif mode == I18N.tr("export.short_edge", "短边"):
                if self.orig_w <= self.orig_h:
                    new_w = val
                    new_h = int(self.orig_h * (val / self.orig_w))
                else:
                    new_h = val
                    new_w = int(self.orig_w * (val / self.orig_h))

        # AUD-33110：尺寸以属性为数据源写入，标签只承担展示职责
        self._last_output_size = (new_w, new_h)
        self.output_size_label.setText(f"{I18N.tr('export.result', '结果')}: {new_w} x {new_h}")

    def on_format_changed(self, fmt: str) -> None:
        is_exr = fmt == I18N.tr("export.fmt_exr", "OpenEXR")
        self.options_layout.setRowVisible(self.exr_widget, is_exr)
        can_have_quality = fmt in [I18N.tr("export.fmt_jpeg", "JPEG"), I18N.tr("export.fmt_png", "PNG"), I18N.tr("export.fmt_webp", "WebP")]
        self.options_layout.setRowVisible(self.quality_widget, can_have_quality)
        label = self.options_layout.labelForField(self.quality_widget)
        if label and isinstance(label, QLabel):
            label.setText(I18N.tr("export.compression", "压缩:") if fmt == I18N.tr("export.fmt_png", "PNG") else I18N.tr("export.quality", "质量:"))

    def get_settings(self) -> Dict[str, Any]:
        fmt_map = {"JPEG": ".jpg", "PNG": ".png", "OpenEXR": ".exr", "TIFF": ".tif", "WebP": ".webp"}
        ext = fmt_map.get(self.format_combo.currentText(), ".jpg")
        filename = self.name_input.text()
        if not filename.lower().endswith(ext): filename += ext

        export_dir = self.path_input.text()
        settings: Dict[str, Any] = {
            "path": os.path.join(export_dir, filename),
            "format": self.format_combo.currentText(),
            "options": {"quality": self.quality_slider.value()}
        }

        if self.resize_check.isChecked():
            # AUD-33110：resize 取缓存属性并校验，不再解析显示标签文本
            # （写入前缀是 I18N.tr("export.result")，非英文 locale 下
            # replace("Result: ", "") 必然失配 → int() ValueError）
            size = self._last_output_size
            if size is not None and size[0] > 0 and size[1] > 0:
                settings["options"]["resize"] = (int(size[0]), int(size[1]))
            else:
                # 原图尺寸无效（未设置 original_size）时不产出 resize=(0,0)
                logger.warning(
                    "ExportDialog: resize 已勾选但当前输出尺寸无效"
                    "（原图尺寸=%dx%d），本次导出不缩放", self.orig_w, self.orig_h)

        if settings["format"] == "OpenEXR":
            settings["options"]["exr_type"] = self.exr_type_combo.currentIndex() + 1
            settings["options"]["exr_compression"] = self.exr_comp_combo.currentIndex()
            
        return settings
