"""
QuickProjectDialog - 快速创建项目对话框。

从 Library 双击 MOV 文件时推荐创建 Quick Project：
- 创建 movie_name.topos 项目文件
- 导入 MOV 作为 asset
- 创建默认 timeline
- 自动添加素材到 timeline
- 打开 Edit 页面开始编辑

设计原则：
- 简洁的一步式创建流程
- 自动检测视频元数据（分辨率、帧率）
- 可选的自定义设置
- 信号驱动与 MainWindow 集成

参考：
- 统一工程包架构方案 13.3
"""

from __future__ import annotations

import os
import logging
import re
from typing import Callable, Optional

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QLineEdit, QComboBox, QSpinBox,
    QCheckBox, QGroupBox, QWidget, QMessageBox, QFileDialog,
    QSizePolicy,
)
from PyQt6.QtCore import QObject, QRunnable, Qt, QThreadPool, pyqtSignal

from src.shared.i18n.i18n import I18N
from src.shared.config.config_manager import ConfigManager

logger = logging.getLogger(__name__)


class _QuickProjectProbeSignals(QObject):
    """后台元数据探测的队列信号载体（AUD-20502）。"""

    #: (metadata dict,)
    probed = pyqtSignal(dict)


class _MetadataProbeWorker(QRunnable):
    """在 QThreadPool 线程执行媒体元数据探测（AUD-20502）。

    ``probe``（``QuickProjectDialog._probe_media_metadata``）内部走
    FFmpeg/PyAV 子进程与文件 IO，未命中缓存时可达数百毫秒以上 —— 不得在
    UI 线程同步执行。结果经 ``probed`` 信号队列化回 UI 线程回填。
    """

    def __init__(
        self,
        probe: Callable[[str], dict],
        path: str,
        signals: _QuickProjectProbeSignals,
    ) -> None:
        super().__init__()
        self._probe = probe
        self._path = path
        self._signals = signals

    def run(self) -> None:
        try:
            metadata = self._probe(self._path)
        except Exception as exc:  # noqa: BLE001 — 探测失败保持占位元数据
            logger.warning(f"Async media metadata probe failed for {self._path}: {exc}")
            return
        try:
            self._signals.probed.emit(metadata)
        except RuntimeError:
            # 对话框已销毁（用户秒关）：信号载体已删除，结果作废。
            logger.debug("Metadata probe result arrived after dialog destroy")


class QuickProjectDialog(QDialog):
    """
    快速创建项目对话框。

    从单个媒体文件快速创建剪辑项目，自动：
    - 创建项目文件（.topos）
    - 导入素材作为 asset
    - 创建默认 timeline
    - 自动添加素材到 timeline

    E22: quick_project_created 信号已删除——0 emit 0 connect 的死信号；
    调用方（file_controller）经 dialog 的 getter 取创建结果。
    """

    # AUD-20501: 预设字典曾以 I18N.tr(...) 的翻译结果作为键，类属性在模块
    # import 期求值，语言切换（无需重启）后 findText/字典查找全部失配，
    # 默认分辨率静默落到第 0 项 "4K DCI"。现改为语言无关的稳定 key，
    # 显示文本在每次实例化时按当前 locale 生成，定位一律走 itemData。
    AUTO_PRESET_KEY = "__auto__"
    CUSTOM_PRESET_KEY = "__custom__"

    # 分辨率预设（稳定 key → 尺寸；None 表示 Auto/Custom 无固定尺寸）
    RESOLUTION_PRESETS: dict = {
        "4K DCI (4096x2160)": (4096, 2160),
        "4K UHD (3840x2160)": (3840, 2160),
        "2K DCI (2048x1080)": (2048, 1080),
        "2K QHD (2560x1440)": (2560, 1440),
        "1080p (1920x1080)": (1920, 1080),
        "720p (1280x720)": (1280, 720),
        AUTO_PRESET_KEY: None,  # 自动从素材获取
        CUSTOM_PRESET_KEY: None,
    }

    # 帧率预设（稳定 key → fps 值；None 表示 Auto）
    FPS_PRESETS: list = [
        (AUTO_PRESET_KEY, None),
        ("23.976", 23.976),
        ("24", 24.0),
        ("25", 25.0),
        ("29.97", 29.97),
        ("30", 30.0),
        ("50", 50.0),
        ("59.94", 59.94),
        ("60", 60.0),
    ]

    @classmethod
    def _resolution_preset_display_text(cls, preset_key: str) -> str:
        """按当前 locale 生成分辨率预设显示文本（稳定 key 不随语言变化）。"""
        if preset_key == cls.AUTO_PRESET_KEY:
            return I18N.tr("project.auto_from_media", "Auto (from Media)")
        if preset_key == cls.CUSTOM_PRESET_KEY:
            return I18N.tr("project.custom_resolution", "Custom")
        return preset_key

    @classmethod
    def _fps_preset_display_text(cls, preset_key: str) -> str:
        """按当前 locale 生成帧率预设显示文本。"""
        if preset_key == cls.AUTO_PRESET_KEY:
            return I18N.tr("project.auto_from_media", "Auto (from Media)")
        return preset_key

    def __init__(
        self,
        media_path: str,
        config_manager: Optional[ConfigManager] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        """
        初始化快速项目对话框。

        Args:
            media_path: 媒体文件路径（视频或图片）
            config_manager: 配置管理器（可选）
            parent: Qt 父窗口
        """
        super().__init__(parent)

        self._media_path = media_path
        self._config = config_manager or ConfigManager()

        # AUD-20502: 元数据探测（FFmpeg/PyAV 子进程 + 文件 IO）移出构造路径
        # —— 构造期只同步生成占位元数据（media_type 由扩展名判定，纯字符串
        # 操作），真实探测在 QThreadPool 后台执行，完成后经队列信号回填 UI。
        self._media_metadata: dict = self._placeholder_media_metadata(media_path)
        self._metadata_probe_pending: bool = True
        self._probe_signals = _QuickProjectProbeSignals()
        self._probe_signals.probed.connect(self._on_media_metadata_probed)

        # 项目设置
        self._project_path: Optional[str] = None
        self._timeline_settings: Optional[dict] = None

        self._setup_ui()
        self._apply_styles()
        self._load_defaults()
        self._start_metadata_probe(media_path)

        logger.debug(f"QuickProjectDialog initialized for: {media_path}")

    @staticmethod
    def _placeholder_media_metadata(path: str) -> dict:
        """构造期占位元数据（无 IO）：默认值 + 扩展名判定的 media_type。"""
        from src.shared.media.format_registry import VIDEO_EXTENSIONS

        metadata: dict = {
            "width": 1920,
            "height": 1080,
            "fps": 24.0,
            "duration_seconds": None,
            "total_frames": None,
            "media_type": "video",
            "codec": "",
        }
        if not path.lower().endswith(tuple(VIDEO_EXTENSIONS)):
            metadata["media_type"] = "image"
        return metadata

    def _probe_media_metadata(self, path: str) -> dict:
        """
        探测媒体文件元数据（AUD-20502: 由 QThreadPool worker 线程调用，
        不在 UI 线程同步执行）。

        Args:
            path: 媒体文件路径

        Returns:
            元数据字典（width, height, fps, duration_seconds, media_type）
        """
        metadata: dict = {
            "width": 1920,
            "height": 1080,
            "fps": 24.0,
            "duration_seconds": None,
            "total_frames": None,
            "media_type": "video",
            "codec": "",
        }

        # 判断媒体类型
        from src.shared.media.format_registry import VIDEO_EXTENSIONS
        video_extensions = tuple(VIDEO_EXTENSIONS)
        if path.lower().endswith(video_extensions):
            metadata["media_type"] = "video"

            # 使用 MediaSourceFactory 探测视频元数据
            try:
                from src.shared.media.factory import MediaSourceFactory
                video_meta = MediaSourceFactory.probe_video_metadata(path)

                if video_meta:
                    metadata["width"] = video_meta.width
                    metadata["height"] = video_meta.height
                    metadata["fps"] = float(video_meta.fps)
                    metadata["duration_seconds"] = video_meta.duration_seconds
                    metadata["total_frames"] = video_meta.total_frames
                    metadata["codec"] = video_meta.codec_name or ""

                    logger.debug(
                        f"Probed video metadata: {metadata['width']}x{metadata['height']}, "
                        f"{metadata['fps']}fps, {metadata['total_frames']} frames"
                    )
            except Exception as e:
                logger.warning(f"Failed to probe video metadata: {e}")
        else:
            metadata["media_type"] = "image"

            # 使用 FileIO 获取图片元数据
            try:
                from src.shared.io.file_io import FileIO
                file_meta = FileIO.get_metadata(path)

                if file_meta:
                    # AUD-20500: FileIO.get_metadata 从不返回 "width"/
                    # "height" 键——像素尺寸在 "Resolution" 字符串里
                    # （exiftool 与 Pillow 两条路径均产出 "W x H" 格式）。
                    # 原先按不存在的键取值使任何图片都回退 1920x1080，
                    # Auto 预设与媒体信息区的分辨率全部错误。
                    resolution_text = str(file_meta.get("Resolution", "") or "")
                    match = re.match(
                        r"^\s*(\d+(?:\.\d+)?)\s*[xX×]\s*(\d+(?:\.\d+)?)\s*$",
                        resolution_text,
                    )
                    if match:
                        width_value = int(float(match.group(1)))
                        height_value = int(float(match.group(2)))
                        if width_value > 0 and height_value > 0:
                            metadata["width"] = width_value
                            metadata["height"] = height_value
                    else:
                        logger.warning(
                            "QuickProject: no image resolution reported for "
                            f"{path} (Resolution={resolution_text!r}); "
                            "falling back to 1920x1080"
                        )
                    metadata["fps"] = 24.0  # 图片默认帧率

                    logger.debug(
                        f"Probed image metadata: {metadata['width']}x{metadata['height']}"
                    )
            except Exception as e:
                logger.warning(f"Failed to probe image metadata: {e}")

        return metadata

    def _start_metadata_probe(self, path: str) -> None:
        """后台线程启动媒体元数据探测（AUD-20502）。"""
        worker = _MetadataProbeWorker(
            self._probe_media_metadata, path, self._probe_signals
        )
        QThreadPool.globalInstance().start(worker)

    def _on_media_metadata_probed(self, metadata: dict) -> None:
        """探测完成（UI 线程队列回调）→ 回填元数据与 UI 显示。"""
        self._metadata_probe_pending = False
        if not isinstance(metadata, dict) or not metadata:
            return
        self._media_metadata = metadata
        self._refresh_media_info_labels()
        # Auto 分辨率模式下用真实媒体分辨率回填（其它预设不动用户所见值）。
        if self.resolution_combo.currentData() == self.AUTO_PRESET_KEY:
            try:
                width = int(metadata.get("width", self.width_spin.value()))
                height = int(metadata.get("height", self.height_spin.value()))
            except (TypeError, ValueError):
                return
            if width > 0:
                self.width_spin.setValue(width)
            if height > 0:
                self.height_spin.setValue(height)

    def _refresh_media_info_labels(self) -> None:
        """用（后台探测完成的）元数据刷新媒体信息区标签。"""
        labels = getattr(self, "_media_info_labels", None)
        if not labels:
            return
        meta = self._media_metadata
        res_lbl = labels.get("resolution")
        if res_lbl is not None:
            res_lbl.setText(f"{meta['width']}x{meta['height']}")
        fps_lbl = labels.get("fps")
        if fps_lbl is not None and meta.get("fps"):
            fps_lbl.setText(f"{float(meta['fps']):.3f} fps")
        dur_lbl = labels.get("duration")
        if dur_lbl is not None:
            duration = meta.get("duration_seconds")
            dur_lbl.setText(
                self._format_duration(duration) if duration else "—"
            )

    def _setup_ui(self) -> None:
        """设置 UI 界面"""
        media_name = os.path.basename(self._media_path)
        media_type = self._media_metadata.get("media_type", "video")

        self.setWindowTitle(I18N.tr("quick_project.title", "Create Quick Project"))
        # 减小固定尺寸，消除底部空白
        self.setFixedSize(520, 620)

        # 主布局 - 统一间距
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(12, 8, 12, 12)
        main_layout.setSpacing(8)

        # ── 标题区域 ─────────────────────────────────────
        title_label = QLabel(I18N.tr("quick_project.header", "Quick Project from Media"))
        title_label.setStyleSheet("font-size: 18px; font-weight: bold; color: #ff8800;")
        main_layout.addWidget(title_label)

        # ── 媒体信息 ─────────────────────────────────────
        info_group = QGroupBox(I18N.tr("quick_project.media_info", "Media Information"))
        info_layout = QGridLayout()
        info_layout.setSpacing(10)
        info_layout.setContentsMargins(10, 10, 10, 10)
        info_layout.setColumnMinimumWidth(0, 90)
        info_layout.setColumnStretch(1, 0)

        row = 0
        # 媒体文件名
        media_label_text = QLabel(I18N.tr("quick_project.media_file", "Media File:"))
        media_label_text.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        media_label = QLabel(media_name)
        media_label.setWordWrap(True)
        media_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        info_layout.addWidget(media_label_text, row, 0)
        info_layout.addWidget(media_label, row, 1)
        row += 1

        # AUD-20502: 值标签存引用，后台探测完成后由 _refresh_media_info_labels
        # 回填（构造期显示占位值）。
        self._media_info_labels: dict = {}

        # 媒体类型
        type_label_text = QLabel(I18N.tr("quick_project.media_type", "Type:"))
        type_label_text.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        type_label = QLabel(
            I18N.tr(f"quick_project.media_type_{media_type}", media_type.capitalize())
        )
        info_layout.addWidget(type_label_text, row, 0)
        info_layout.addWidget(type_label, row, 1)
        row += 1

        # 分辨率
        res_label_text = QLabel(I18N.tr("quick_project.resolution", "Resolution:"))
        res_label_text.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        res_str = f"{self._media_metadata['width']}x{self._media_metadata['height']}"
        res_label = QLabel(res_str)
        info_layout.addWidget(res_label_text, row, 0)
        info_layout.addWidget(res_label, row, 1)
        row += 1
        self._media_info_labels["resolution"] = res_label

        # 帧率 / 时长（仅视频；探测完成前显示占位值，完成后回填）
        if media_type == "video":
            fps_label_text = QLabel(I18N.tr("quick_project.fps", "Frame Rate:"))
            fps_label_text.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            fps_value = self._media_metadata.get("fps")
            fps_label = QLabel(
                f"{float(fps_value):.3f} fps" if fps_value else "—"
            )
            info_layout.addWidget(fps_label_text, row, 0)
            info_layout.addWidget(fps_label, row, 1)
            row += 1
            self._media_info_labels["fps"] = fps_label

            duration_label_text = QLabel(I18N.tr("quick_project.duration", "Duration:"))
            duration_label_text.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            duration = self._media_metadata.get("duration_seconds")
            duration_label = QLabel(
                self._format_duration(duration) if duration else "—"
            )
            info_layout.addWidget(duration_label_text, row, 0)
            info_layout.addWidget(duration_label, row, 1)
            row += 1
            self._media_info_labels["duration"] = duration_label

        info_group.setLayout(info_layout)
        main_layout.addWidget(info_group)

        # ── 项目设置 ─────────────────────────────────────
        project_group = QGroupBox(I18N.tr("quick_project.project_settings", "Project Settings"))
        project_layout = QGridLayout()
        project_layout.setSpacing(10)
        project_layout.setContentsMargins(10, 10, 10, 10)
        project_layout.setColumnMinimumWidth(0, 90)
        project_layout.setColumnStretch(1, 1)

        # 项目名称（默认使用媒体文件名）
        self.name_edit = QLineEdit()
        base_name = os.path.splitext(media_name)[0]
        self.name_edit.setText(base_name)
        self.name_edit.setPlaceholderText(I18N.tr("project.name_placeholder", "My Project"))
        # 使用 Expanding 让输入框填充可用空间，显示完整路径
        self.name_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        name_label = QLabel(I18N.tr("project.name", "Name:"))
        name_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        project_layout.addWidget(name_label, 0, 0)
        project_layout.addWidget(self.name_edit, 0, 1)

        # 项目位置
        location_widget = QWidget()
        # 使用 Expanding 让 widget 填充可用空间，显示完整路径
        location_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        location_layout = QHBoxLayout(location_widget)
        location_layout.setContentsMargins(0, 0, 0, 0)
        location_layout.setSpacing(6)
        self.location_edit = QLineEdit()
        self.location_edit.setPlaceholderText(I18N.tr("project.location_placeholder", "Select location..."))
        self.location_btn = QPushButton(I18N.tr("project.browse", "Browse..."))
        self.location_btn.setFixedWidth(80)
        self.location_btn.clicked.connect(self._browse_location)
        location_layout.addWidget(self.location_edit)
        location_layout.addWidget(self.location_btn)

        location_label = QLabel(I18N.tr("project.location", "Location:"))
        location_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        project_layout.addWidget(location_label, 1, 0)
        project_layout.addWidget(location_widget, 1, 1)

        project_group.setLayout(project_layout)
        main_layout.addWidget(project_group)

        # ── Timeline 设置 ────────────────────────────────
        timeline_group = QGroupBox(I18N.tr("quick_project.timeline_settings", "Timeline Settings"))
        timeline_layout = QGridLayout()
        timeline_layout.setSpacing(10)
        timeline_layout.setContentsMargins(10, 10, 10, 10)
        timeline_layout.setColumnMinimumWidth(0, 90)
        timeline_layout.setColumnStretch(1, 1)

        row = 0

        # 分辨率预设
        res_label = QLabel(I18N.tr("project.resolution", "Resolution:"))
        res_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.resolution_combo = QComboBox()
        for preset_key in self.RESOLUTION_PRESETS.keys():
            self.resolution_combo.addItem(
                self._resolution_preset_display_text(preset_key), preset_key
            )
        self.resolution_combo.currentIndexChanged.connect(self._on_resolution_changed)
        timeline_layout.addWidget(res_label, row, 0)
        timeline_layout.addWidget(self.resolution_combo, row, 1)
        row += 1

        # 自定义分辨率 - 宽度
        width_label = QLabel(I18N.tr("project.width", "Width:"))
        width_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.width_spin = QSpinBox()
        self.width_spin.setRange(64, 16384)
        self.width_spin.setValue(self._media_metadata["width"])
        self.width_spin.setSuffix(I18N.tr("project.px_suffix", " px"))
        timeline_layout.addWidget(width_label, row, 0)
        timeline_layout.addWidget(self.width_spin, row, 1)
        row += 1

        # 自定义分辨率 - 高度
        height_label = QLabel(I18N.tr("project.height", "Height:"))
        height_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.height_spin = QSpinBox()
        self.height_spin.setRange(64, 16384)
        self.height_spin.setValue(self._media_metadata["height"])
        self.height_spin.setSuffix(I18N.tr("project.px_suffix", " px"))
        timeline_layout.addWidget(height_label, row, 0)
        timeline_layout.addWidget(self.height_spin, row, 1)
        row += 1

        # 帧率
        fps_label = QLabel(I18N.tr("project.fps", "Frame Rate:"))
        fps_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.fps_combo = QComboBox()
        for fps_key, value in self.FPS_PRESETS:
            self.fps_combo.addItem(self._fps_preset_display_text(fps_key), value)
        timeline_layout.addWidget(fps_label, row, 0)
        timeline_layout.addWidget(self.fps_combo, row, 1)
        row += 1

        # 自动添加素材到 Timeline - 占据两列
        self.auto_add_checkbox = QCheckBox(
            I18N.tr("quick_project.auto_add_media", "Auto add media to timeline")
        )
        self.auto_add_checkbox.setChecked(True)
        timeline_layout.addWidget(self.auto_add_checkbox, row, 0, 1, 2)  # 跨两列

        timeline_group.setLayout(timeline_layout)

        main_layout.addWidget(timeline_group)

        # ── 按钮 ─────────────────────────────────────────
        btn_layout = QHBoxLayout()
        btn_layout.setContentsMargins(0, 8, 0, 0)
        btn_layout.addStretch()

        self.cancel_btn = QPushButton(I18N.tr("project.cancel", "Cancel"))
        self.cancel_btn.setObjectName("cancel_btn")
        self.cancel_btn.setFixedWidth(90)
        self.cancel_btn.clicked.connect(self.reject)

        self.create_btn = QPushButton(I18N.tr("quick_project.create", "Create Project"))
        self.create_btn.setFixedWidth(120)
        self.create_btn.clicked.connect(self._on_create)

        btn_layout.addWidget(self.cancel_btn)
        btn_layout.addWidget(self.create_btn)

        main_layout.addLayout(btn_layout)

        # 默认选择 Auto (from Media)——按稳定 key 定位（AUD-20501：
        # findText 在运行时切换语言后失配，默认项静默落回 4K DCI）
        auto_res_index = self.resolution_combo.findData(self.AUTO_PRESET_KEY)
        if auto_res_index >= 0:
            self.resolution_combo.setCurrentIndex(auto_res_index)

        auto_fps_index = 0  # 第一项是 Auto
        self.fps_combo.setCurrentIndex(auto_fps_index)

        self._on_resolution_changed(self.resolution_combo.currentIndex())

    def _apply_styles(self) -> None:
        """应用样式 - 简洁紧凑风格，橙色按钮"""
        self.setStyleSheet("""
            QuickProjectDialog {
                background: #27272b;
            }
            QLabel {
                color: #aaaaaa;
                font-size: 12px;
                background: transparent;
            }
            QLineEdit, QSpinBox, QComboBox {
                background: #252529;
                border: 1px solid #3a3a3a;
                border-radius: 3px;
                padding: 4px 6px;
                color: #E0E0E0;
                min-height: 20px;
                font-size: 12px;
            }
            QLineEdit:focus, QSpinBox:focus, QComboBox:focus {
                border-color: #ff8800;
            }
            QComboBox::drop-down {
                border: none;
                width: 18px;
            }
            QComboBox::down-arrow {
                image: none;
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-top: 5px solid #888;
            }
            QGroupBox {
                color: #ff8800;
                font-weight: bold;
                font-size: 12px;
                border: 1px solid #3a3a3a;
                border-radius: 4px;
                margin-top: 10px;
                padding-top: 0px;
                background: transparent;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: 10px;
                padding: 0 6px;
                background: transparent;
            }
            QPushButton {
                background: #ff8800;
                color: #fff;
                border: none;
                border-radius: 3px;
                padding: 6px 12px;
                font-size: 12px;
                font-weight: bold;
                min-height: 24px;
            }
            QPushButton:hover { 
                background: #ff9922; 
            }
            QPushButton:pressed { 
                background: #cc6600; 
            }
            QPushButton#cancel_btn {
                background: #444;
                color: #ccc;
            }
            QPushButton#cancel_btn:hover {
                background: #555;
                color: #fff;
            }
            QCheckBox {
                color: #aaaaaa;
                spacing: 6px;
                font-size: 12px;
                background: transparent;
            }
            QCheckBox::indicator {
                width: 14px;
                height: 14px;
                border-radius: 2px;
                border: 1px solid #3a3a3a;
                background: #252525;
            }
            QCheckBox::indicator:checked {
                background: #ff8800;
                border-color: #ff8800;
            }
            QSpinBox::up-button, QSpinBox::down-button {
                background: #333;
                border: none;
                width: 14px;
                height: 10px;
            }
            QSpinBox::up-arrow {
                border-left: 3px solid transparent;
                border-right: 3px solid transparent;
                border-bottom: 4px solid #888;
            }
            QSpinBox::down-arrow {
                border-left: 3px solid transparent;
                border-right: 3px solid transparent;
                border-top: 4px solid #888;
            }
        """)

    def _load_defaults(self) -> None:
        """加载默认值"""
        # 默认项目位置（媒体文件所在目录）
        media_dir = os.path.dirname(self._media_path)
        self.location_edit.setText(media_dir)

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
        """分辨率预设变化（按稳定 key 判定，不依赖翻译文本）"""
        preset_key = self.resolution_combo.itemData(index)
        if preset_key is None:
            return
        resolution = self.RESOLUTION_PRESETS.get(preset_key)

        is_auto = preset_key == self.AUTO_PRESET_KEY
        is_custom = preset_key == self.CUSTOM_PRESET_KEY or resolution is None

        # Width 和 Height 输入框始终可见
        # 根据选择自动填充值
        if is_auto:
            # 使用媒体元数据
            self.width_spin.setValue(self._media_metadata["width"])
            self.height_spin.setValue(self._media_metadata["height"])
        elif is_custom:
            # 自定义模式，保持当前值
            pass
        elif resolution:
            self.width_spin.setValue(resolution[0])
            self.height_spin.setValue(resolution[1])

    def _format_duration(self, seconds: float) -> str:
        """
        格式化时长。

        Args:
            seconds: 秒数

        Returns:
            格式化的时长字符串（HH:MM:SS）
        """
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)

        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        else:
            return f"{minutes:02d}:{secs:02d}"

    def _on_create(self) -> None:
        """创建项目 — 保存参数并关闭对话框，由 MainWindow 执行实际创建"""
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

        # 获取参数
        fps_data = self.fps_combo.currentData()
        if fps_data is None:
            fps = self._media_metadata.get("fps", 24.0)
        else:
            fps = fps_data

        width = self.width_spin.value()
        height = self.height_spin.value()

        auto_add = self.auto_add_checkbox.isChecked()

        # 生成项目文件路径
        project_filename = f"{name}.topos"
        project_path = os.path.join(location, project_filename)

        # 检查是否已存在同名项目
        if os.path.exists(project_path):
            reply = QMessageBox.question(
                self,
                I18N.tr("project.exists_title", "Project Exists"),
                I18N.tr(
                    "project.exists_message",
                    "A project with this name already exists. Do you want to overwrite it?"
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        # Timeline 设置字典
        timeline_settings = {
            "fps": fps,
            "width": width,
            "height": height,
            "auto_add_media": auto_add,
        }

        # 保存参数供 MainWindow 读取（不发射信号，避免重入）
        self._project_path = project_path
        self._timeline_settings = timeline_settings

        logger.info(
            f"Quick project dialog accepted: {project_path} from {self._media_path}, "
            f"timeline: {width}x{height} @ {fps}fps"
        )

        # 关闭对话框 — MainWindow 在 exec() 返回后读取结果并创建项目
        self.accept()

    def _show_error(self, message: str) -> None:
        """显示错误消息"""
        QMessageBox.warning(
            self,
            I18N.tr("project.error_title", "Error"),
            message,
        )

    def get_project_path(self) -> Optional[str]:
        """获取创建的项目路径"""
        return self._project_path

    def get_timeline_settings(self) -> dict:
        """获取 Timeline 设置（返回 _on_create 保存的结果）"""
        if self._timeline_settings is not None:
            return self._timeline_settings

        fps_data = self.fps_combo.currentData()
        fps = fps_data if fps_data is not None else self._media_metadata.get("fps", 24.0)

        return {
            "fps": fps,
            "width": self.width_spin.value(),
            "height": self.height_spin.value(),
            "auto_add_media": self.auto_add_checkbox.isChecked(),
        }
