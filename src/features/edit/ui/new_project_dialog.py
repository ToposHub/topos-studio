# SUPERSEDED(2026-08-19 审计): 已被新实现取代且无生产引用，仅存档。
"""
NewProjectDialog - 新建项目对话框。

用户设置：
- 项目名称
- 项目位置
- 默认分辨率
- 默认帧率
- 音频采样率
"""

import os
import dataclasses
from typing import Optional
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QPushButton, QLineEdit, QComboBox, QSpinBox,
    QFileDialog, QGroupBox, QWidget,
)
from PyQt6.QtCore import pyqtSignal

from src.shared.i18n.i18n import I18N
from src.features.edit.core.project import EditProject
from src.features.edit.core.edit_project_io import EditProjectIO, EDIT_PROJECT_EXTENSION


class NewProjectDialog(QDialog):
    """
    新建项目对话框。

    Signals:
        project_created(str): 项目文件路径（创建成功时发出）
    """

    project_created = pyqtSignal(str)

    # 分辨率预设
    RESOLUTION_PRESETS = {
        "4K DCI (4096x2160)": (4096, 2160),
        "4K UHD (3840x2160)": (3840, 2160),
        "2K DCI (2048x1080)": (2048, 1080),
        "2K QHD (2560x1440)": (2560, 1440),
        "1080p (1920x1080)": (1920, 1080),
        "720p (1280x720)": (1280, 720),
        "480p (854x480)": (854, 480),
        I18N.tr("project.custom_resolution", "Custom"): None,
    }

    # 帧率预设
    FPS_PRESETS = [
        ("23.976", 23.976),
        ("24", 24.0),
        ("25", 25.0),
        ("29.97", 29.97),
        ("30", 30.0),
        ("50", 50.0),
        ("59.94", 59.94),
        ("60", 60.0),
    ]

    # 音频采样率预设
    SAMPLE_RATE_PRESETS = [
        ("44.1 kHz", 44100),
        ("48 kHz", 48000),
        ("96 kHz", 96000),
    ]

    def __init__(self, config_manager, default_location: Optional[str] = None, parent=None) -> None:
        super().__init__(parent)
        self._config = config_manager
        self._default_location = default_location  # explicit override (e.g. project library dir)
        self._project_path: Optional[str] = None
        self._setup_ui()
        self._apply_styles()
        self._load_defaults()

    def _setup_ui(self) -> None:
        self.setWindowTitle(I18N.tr("project.new_title", "New Project"))
        self.setMinimumSize(500, 400)
        self.setFixedSize(500, 400)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)

        # ── 项目基本信息 ─────────────────────────────────────
        info_group = QGroupBox(I18N.tr("project.project_info", "Project Information"))
        info_layout = QFormLayout(info_group)
        info_layout.setSpacing(12)

        # 项目名称
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText(I18N.tr("project.name_placeholder", "My Project"))
        info_layout.addRow(I18N.tr("project.name", "Name:"), self.name_edit)

        # 项目位置
        location_layout = QHBoxLayout()
        self.location_edit = QLineEdit()
        self.location_edit.setPlaceholderText(I18N.tr("project.location_placeholder", "Select location..."))
        self.location_btn = QPushButton(I18N.tr("project.browse", "Browse..."))
        self.location_btn.setFixedWidth(100)
        self.location_btn.clicked.connect(self._browse_location)
        location_layout.addWidget(self.location_edit)
        location_layout.addWidget(self.location_btn)
        info_layout.addRow(I18N.tr("project.location", "Location:"), location_layout)

        layout.addWidget(info_group)

        # ── 视频设置 ─────────────────────────────────────────
        video_group = QGroupBox(I18N.tr("project.video_settings", "Video Settings"))
        video_layout = QFormLayout(video_group)
        video_layout.setSpacing(12)

        # 分辨率预设
        self.resolution_combo = QComboBox()
        for preset_name in self.RESOLUTION_PRESETS.keys():
            # 动态翻译预设名称
            if preset_name.startswith("4K") or preset_name.startswith("2K") or preset_name.startswith("1080") or preset_name.startswith("720") or preset_name.startswith("480"):
                self.resolution_combo.addItem(preset_name)
            else:
                self.resolution_combo.addItem(I18N.tr("project.custom_resolution", "Custom"))
        self.resolution_combo.currentIndexChanged.connect(self._on_resolution_changed)
        video_layout.addRow(I18N.tr("project.resolution", "Resolution:"), self.resolution_combo)

        # 自定义分辨率（宽度、高度）
        custom_res_widget = QWidget()
        custom_res_layout = QHBoxLayout(custom_res_widget)
        custom_res_layout.setContentsMargins(0, 0, 0, 0)
        custom_res_layout.setSpacing(8)

        self.width_spin = QSpinBox()
        self.width_spin.setRange(64, 16384)
        self.width_spin.setValue(1920)
        self.width_spin.setSuffix(I18N.tr("project.px_suffix", " px"))
        custom_res_layout.addWidget(self.width_spin)

        self.height_spin = QSpinBox()
        self.height_spin.setRange(64, 16384)
        self.height_spin.setValue(1080)
        self.height_spin.setSuffix(I18N.tr("project.px_suffix", " px"))
        custom_res_layout.addWidget(self.height_spin)

        custom_res_layout.addStretch()
        video_layout.addRow("", custom_res_widget)
        self.custom_res_widget = custom_res_widget

        # 帧率
        self.fps_combo = QComboBox()
        for label, value in self.FPS_PRESETS:
            self.fps_combo.addItem(label, value)
        video_layout.addRow(I18N.tr("project.fps", "Frame Rate:"), self.fps_combo)

        layout.addWidget(video_group)

        # ── 音频设置 ─────────────────────────────────────────
        audio_group = QGroupBox(I18N.tr("project.audio_settings", "Audio Settings"))
        audio_layout = QFormLayout(audio_group)
        audio_layout.setSpacing(12)

        self.sample_rate_combo = QComboBox()
        for label, value in self.SAMPLE_RATE_PRESETS:
            self.sample_rate_combo.addItem(label, value)
        audio_layout.addRow(I18N.tr("project.sample_rate", "Sample Rate:"), self.sample_rate_combo)

        layout.addWidget(audio_group)

        layout.addStretch()

        # ── 按钮 ─────────────────────────────────────────────
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        self.create_btn = QPushButton(I18N.tr("project.create", "Create"))
        self.create_btn.setFixedWidth(120)
        self.create_btn.clicked.connect(self._on_create)

        self.cancel_btn = QPushButton(I18N.tr("project.cancel", "Cancel"))
        self.cancel_btn.setFixedWidth(100)
        self.cancel_btn.clicked.connect(self.reject)

        btn_layout.addWidget(self.cancel_btn)
        btn_layout.addWidget(self.create_btn)

        layout.addLayout(btn_layout)

        # 初始状态
        self._on_resolution_changed(0)

    def _apply_styles(self) -> None:
        self.setStyleSheet("""
            NewProjectDialog {
                background: #1e1e1e;
                border: 1px solid #444;
            }
            QLabel { color: #ddd; }
            QLineEdit, QSpinBox, QComboBox {
                background: #252525;
                border: 1px solid #444;
                border-radius: 4px;
                padding: 6px;
                color: #ddd;
                min-height: 28px;
            }
            QLineEdit:focus, QSpinBox:focus, QComboBox:focus {
                border-color: #ff8800;
            }
            QGroupBox {
                color: #ccc;
                font-weight: bold;
                border: 1px solid #444;
                border-radius: 6px;
                margin-top: 12px;
                padding-top: 8px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 8px;
                padding: 0 4px;
            }
            QPushButton {
                background: #333;
                color: #ddd;
                border: 1px solid #555;
                border-radius: 4px;
                padding: 8px 16px;
                font-size: 13px;
            }
            QPushButton:hover { background: #444; border-color: #ff8800; }
            QPushButton:pressed { background: #555; }
        """)

    def _load_defaults(self) -> None:
        """加载默认值"""
        # 默认项目位置：显式传入的 default_location 优先（如项目库目录），
        # 否则用配置里的 default_save_directory，最后回退到用户主目录。
        default_dir = (
            self._default_location
            or self._config.get("default_save_directory")
            or os.path.expanduser("~")
        )
        self.location_edit.setText(default_dir)

        # 默认项目名称
        self.name_edit.setText(I18N.tr("project.default_name", "Untitled Project"))

        # 默认帧率（24fps）
        fps_index = self.fps_combo.findData(24.0)
        if fps_index >= 0:
            self.fps_combo.setCurrentIndex(fps_index)

        # 默认音频采样率（48kHz）
        sr_index = self.sample_rate_combo.findData(48000)
        if sr_index >= 0:
            self.sample_rate_combo.setCurrentIndex(sr_index)

    def _browse_location(self) -> None:
        """选择项目位置"""
        current_dir = self.location_edit.text() or os.path.expanduser("~")
        dir_path = QFileDialog.getExistingDirectory(
            self,
            I18N.tr("project.select_location", "Select Project Location"),
            current_dir,
        )
        if dir_path:
            self.location_edit.setText(dir_path)

    def _on_resolution_changed(self, index: int) -> None:
        """分辨率预设变化"""
        preset_name = self.resolution_combo.itemText(index)
        resolution = self.RESOLUTION_PRESETS.get(preset_name)

        # Custom 需要动态翻译匹配
        custom_text = I18N.tr("project.custom_resolution", "Custom")
        is_custom = preset_name == custom_text or resolution is None

        self.custom_res_widget.setVisible(is_custom)

        if resolution:
            self.width_spin.setValue(resolution[0])
            self.height_spin.setValue(resolution[1])

    def _on_create(self) -> None:
        """创建项目"""
        # 验证输入
        name = self.name_edit.text().strip()
        if not name:
            self._show_error(I18N.tr("project.error_name_empty", "Please enter a project name"))
            return

        location = self.location_edit.text().strip()
        if not location:
            self._show_error(I18N.tr("project.error_location_empty", "Please select a project location"))
            return

        if not os.path.isdir(location):
            self._show_error(I18N.tr("project.error_location_invalid", "Selected location does not exist"))
            return

        # 获取视频参数
        fps = self.fps_combo.currentData()
        width = self.width_spin.value()
        height = self.height_spin.value()
        sample_rate = self.sample_rate_combo.currentData()

        # 创建项目 - 使用 EditProject 直接创建
        project = EditProject.create(name)

        # 创建默认时间线（使用指定参数）
        project = project.create_timeline(
            name=f"{name} Timeline",
            fps=fps,
            width=width,
            height=height,
        )

        # AUD-20306: create_timeline 固定 48000Hz，不接收采样率参数；
        # 这里把用户在对话框中选择的采样率替换进新建时间线。工程与
        # 时间线引用均为 frozen dataclass，用 dataclasses.replace 重建
        # （重跑 __post_init__ 校验），不绕过不变量。
        sample_rate_value = int(sample_rate) if sample_rate else 48000
        timeline_id = project.active_timeline_id
        timeline = project.timelines.get(timeline_id) if timeline_id else None
        if (
            timeline is not None
            and timeline_id is not None
            and timeline.sample_rate != sample_rate_value
        ):
            project = dataclasses.replace(
                project,
                timelines={
                    **project.timelines,
                    timeline_id: dataclasses.replace(
                        timeline, sample_rate=sample_rate_value
                    ),
                },
            )

        # 生成项目文件路径 - 使用 EditProjectIO 的扩展名
        project_path = os.path.join(location, f"{name}{EDIT_PROJECT_EXTENSION}")

        # 检查是否已存在同名项目
        if os.path.exists(project_path):
            from PyQt6.QtWidgets import QMessageBox
            reply = QMessageBox.question(
                self,
                I18N.tr("project.exists_title", "Project Exists"),
                I18N.tr("project.exists_message", "A project with this name already exists. Do you want to overwrite it?"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        # 保存项目 - 使用 EditProjectIO 统一保存入口
        edit_project_io = EditProjectIO()
        if edit_project_io.save_project(project_path, project):
            self._project_path = project_path
            self.project_created.emit(project_path)
            self.accept()
        else:
            self._show_error(I18N.tr("project.error_save_failed", "Failed to save project"))

    def _show_error(self, message: str) -> None:
        """显示错误消息"""
        from PyQt6.QtWidgets import QMessageBox
        QMessageBox.warning(
            self,
            I18N.tr("project.error_title", "Error"),
            message,
        )

    def get_project_path(self) -> Optional[str]:
        """获取创建的项目路径"""
        return self._project_path
