"""
WelcomeDialog - Startup welcome screen for Topos Color.

Displays version info, recent files, and quick action shortcuts.
Shown on startup when `show_welcome_on_startup` config is True.
"""

import logging
import os
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QListWidget, QListWidgetItem,
)
from PyQt6.QtCore import Qt, pyqtSignal, QObject, QRunnable, QThreadPool
from PyQt6.QtGui import QFont
from src.shared.i18n.i18n import I18N
from src.app.update_checker import CURRENT_VERSION

logger = logging.getLogger(__name__)


class _ProjectClassifyWorker(QRunnable):
    """[R0-7] 在线程池对最近项目逐个开 zip 读 manifest 并按工作模式过滤。"""

    class _Signals(QObject):
        done = pyqtSignal(object)  # list[str] matching paths

    def __init__(self, projects: list, current_mode: str) -> None:
        super().__init__()
        self._projects = projects
        self._current_mode = current_mode
        self.signals = self._Signals()

    def run(self) -> None:
        matching = []
        for path in self._projects:
            if not os.path.exists(path):
                continue
            kind = WelcomeDialog._classify_project(path)
            expected = "image" if kind == "image" else "video"
            if expected == self._current_mode:
                matching.append(path)
        self.signals.done.emit(matching)


class WelcomeDialog(QDialog):
    """Welcome dialog shown at application startup.

    Signals:
        open_file_requested: Emitted when user clicks "Open Image..."
        recent_file_selected(str): Emitted when user selects a recent file
        recent_project_selected(str): Emitted when user selects a recent .topos project
    """

    open_file_requested = pyqtSignal()
    recent_file_selected = pyqtSignal(str)
    recent_project_selected = pyqtSignal(str)

    @staticmethod
    def _config_bool(value, default: bool = False) -> bool:
        return default if value is None else bool(value)

    @staticmethod
    def _str_list(value: object, limit: int = 50) -> list[str]:
        """[AUD-4110] recent_files/recent_projects 是外部可编辑数据。

        config.json 被手工改写/残留时列表值可能是 str（被逐字符迭代成
        单字符路径）或含非 str 条目（os.path.basename/os.path.exists 抛
        TypeError——前者在 _populate_recent 使 WelcomeDialog 构造即崩，
        后者在 ThreadPool 工作线程内中止分类）。只保留非空字符串条目。
        """
        if not isinstance(value, list):
            return []
        return [entry for entry in value if isinstance(entry, str) and entry][:limit]

    def __init__(self, config_manager, parent=None) -> None:
        super().__init__(parent)
        # [R2-2] 关闭即销毁：此前每次新建、parent=self、无 WA_DeleteOnClose，
        # C++ 对象随打开次数永久累积
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._config = config_manager
        self._setup_ui()
        self._apply_styles()

    def _setup_ui(self) -> None:
        self.setWindowTitle(I18N.tr("welcome.title", "Welcome to Topos"))
        self.setMinimumSize(480, 400)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.FramelessWindowHint)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(48, 40, 48, 32)
        layout.setSpacing(0)

        # ── Header ─────────────────────────────────────────────
        title = QLabel(I18N.tr("app.brand", "Topos"))
        title.setObjectName("title")
        title.setFont(QFont("Segoe UI", 36, QFont.Weight.Bold))
        layout.addWidget(title)

        version = QLabel(f"v{CURRENT_VERSION}")
        version.setObjectName("version")
        version.setFont(QFont("Segoe UI", 11))
        layout.addWidget(version)

        layout.addSpacing(4)

        subtitle = QLabel(
            I18N.tr("welcome.subtitle", "Professional color grading & image editing")
        )
        subtitle.setObjectName("subtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        layout.addSpacing(32)

        # ── Work Mode Switch ───────────────────────────────────
        # One-click mode toggle right on the welcome screen (no need to dig
        # into Preferences). Studio only; other launchers ignore work_mode.
        from src.shared.feature_flags import get_product_mode, get_work_mode
        if get_product_mode() == "studio":
            mode_row = QHBoxLayout()
            mode_row.setSpacing(8)
            mode_label = QLabel(I18N.tr("welcome.work_mode", "Work Mode:"))
            mode_label.setObjectName("sectionLabel")
            mode_row.addWidget(mode_label)

            current_mode = get_work_mode(self._config)
            self._mode_btns = {}
            for mode_value, mode_text_key, mode_text_default in (
                ("image", "preferences.general.work_mode.image", "Image Grading"),
                ("video", "preferences.general.work_mode.video", "Video Grading"),
            ):
                btn = QPushButton(I18N.tr(mode_text_key, mode_text_default))
                btn.setObjectName("modeBtn")
                btn.setCheckable(True)
                btn.setChecked(mode_value == current_mode)
                btn.setFixedHeight(28)
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.clicked.connect(lambda checked, m=mode_value: self._on_mode_clicked(m))
                mode_row.addWidget(btn)
                self._mode_btns[mode_value] = btn
            mode_row.addStretch()
            layout.addLayout(mode_row)
            layout.addSpacing(20)

        # ── Quick Actions ──────────────────────────────────────
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(16)

        self.open_btn = QPushButton(I18N.tr("welcome.open_image", "Open Image..."))
        self.open_btn.setObjectName("primaryBtn")
        self.open_btn.setFixedHeight(34)
        self.open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_btn.clicked.connect(self._on_open)
        btn_layout.addWidget(self.open_btn)

        self.close_btn = QPushButton(I18N.tr("welcome.close", "Close"))
        self.close_btn.setObjectName("secondaryBtn")
        self.close_btn.setFixedHeight(34)
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self.close_btn)
        btn_layout.addStretch()

        layout.addLayout(btn_layout)
        layout.addSpacing(28)

        # ── Recent Files ───────────────────────────────────────
        recent_label = QLabel(I18N.tr("welcome.recent_files", "Recent Files"))
        recent_label.setObjectName("sectionLabel")
        layout.addWidget(recent_label)

        self.recent_list = QListWidget()
        self.recent_list.setObjectName("recentList")
        self.recent_list.setMaximumHeight(110)
        self.recent_list.itemDoubleClicked.connect(self._on_recent_double_click)
        self._populate_recent()
        layout.addWidget(self.recent_list, 1)

        # ── Recent Projects ────────────────────────────────────
        from src.shared.feature_flags import get_product_mode
        if get_product_mode() == "studio":
            layout.addSpacing(20)
            projects_label = QLabel(I18N.tr("welcome.recent_projects", "Recent Projects"))
            projects_label.setObjectName("sectionLabel")
            layout.addWidget(projects_label)

            self.projects_list = QListWidget()
            self.projects_list.setObjectName("recentList")
            self.projects_list.setMaximumHeight(90)
            self.projects_list.itemDoubleClicked.connect(self._on_project_double_click)
            self._populate_recent_projects()
            layout.addWidget(self.projects_list, 1)

        # ── Footer ─────────────────────────────────────────────
        layout.addSpacing(16)
        self.show_check = QPushButton(I18N.tr("welcome.show_at_startup", "Show at startup"))
        self.show_check.setObjectName("toggleBtn")
        self.show_check.setCheckable(True)
        self.show_check.setChecked(
            self._config_bool(self._config.get("show_welcome_on_startup"), default=True)
        )
        self.show_check.setCursor(Qt.CursorShape.PointingHandCursor)
        self.show_check.clicked.connect(self._on_toggle_startup)
        layout.addWidget(self.show_check)

    def _populate_recent(self) -> None:
        # [AUD-4110] 列表元素类型收敛，非 str 条目不进入 basename/exists
        recent = self._str_list(self._config.get("recent_files"))
        # Image Grading mode cannot open videos, so filter them out of the
        # Welcome recent list (same behaviour as the Recent Files menu).
        try:
            from src.shared.feature_flags import get_work_mode
            if get_work_mode(self._config) == "image":
                from src.shared.media.format_registry import is_video as _is_video_file
                recent = [p for p in recent if not _is_video_file(p)]
        except Exception as exc:
            # AUD-4112: 过滤失败只是降级为"不过滤"，但必须留痕而非静默吞掉。
            logger.warning("Welcome recent-files work-mode filter skipped: %s", exc)
        self.recent_list.clear()
        if not recent:
            item = QListWidgetItem(I18N.tr("welcome.no_recent", "No recent files"))
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            self.recent_list.addItem(item)
            return

        for filepath in recent:
            basename = os.path.basename(filepath)
            dirname = os.path.dirname(filepath)
            item = QListWidgetItem(f"{basename}  —  {dirname}")
            item.setData(Qt.ItemDataRole.UserRole, filepath)
            if not os.path.exists(filepath):
                item.setForeground(item.foreground())
                font = item.font()
                font.setItalic(True)
                item.setFont(font)
            self.recent_list.addItem(item)

    def _populate_recent_projects(self) -> None:
        """Populate the Recent Projects list, filtered by the active work mode.

        A .topos project is shown only when its project_mode matches the active
        work mode (standalone_image → image mode; everything else → video mode).
        This is read from the archive's manifest.json without a full load.

        [R0-7] 逐个打开 zip 读 manifest 的分类在工作线程执行（项目多时明显
        卡顿），先显示占位行，完成后回 UI 线程填充。
        """
        if not hasattr(self, "projects_list"):
            return
        # [AUD-4110] 非 str 条目会在线程池工作线程里让 os.path.exists 抛
        # TypeError（done 信号永不发射，"Loading projects…" 永久停留）。
        projects = self._str_list(self._config.get("recent_projects"))
        # Filter by work mode: read each project's manifest to classify it.
        try:
            from src.shared.feature_flags import get_work_mode
            current_mode = get_work_mode(self._config)
        except Exception as exc:
            # AUD-4112: 降级到 video 模式需留痕，掩盖真实失败会误导排障。
            logger.warning("Welcome project work-mode detection failed: %s", exc)
            current_mode = "video"

        self.projects_list.clear()
        placeholder = QListWidgetItem(
            I18N.tr("welcome.loading_projects", "Loading projects…")
        )
        placeholder.setFlags(placeholder.flags() & ~Qt.ItemFlag.ItemIsEnabled)
        self.projects_list.addItem(placeholder)

        worker = _ProjectClassifyWorker(projects, current_mode)
        worker.signals.done.connect(self._on_projects_classified)
        QThreadPool.globalInstance().start(worker)

    def _on_projects_classified(self, matching: list) -> None:
        """[R0-7] 分类完成，回 UI 线程填充 Recent Projects 列表。"""
        if not hasattr(self, "projects_list"):
            return
        self.projects_list.clear()
        if not matching:
            item = QListWidgetItem(I18N.tr("welcome.no_recent_projects", "No recent projects"))
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            self.projects_list.addItem(item)
            return

        for path in matching:
            basename = os.path.basename(path)
            dirname = os.path.dirname(path)
            item = QListWidgetItem(f"{basename}  —  {dirname}")
            item.setData(Qt.ItemDataRole.UserRole, path)
            self.projects_list.addItem(item)

    @staticmethod
    def _classify_project(filepath: str) -> str:
        """Lightweight classify a .topos project as 'image' or 'video'.

        Reads only manifest.json from the zip. Returns 'video' on any error
        (conservative — video mode can still open it and will surface a proper
        load error if the archive is broken).
        """
        try:
            import zipfile
            import json as _json
            with zipfile.ZipFile(filepath, "r") as zf:
                with zf.open("manifest.json") as fh:
                    data = _json.load(fh)
            mode = data.get("project_mode", "standalone_image")
            if mode == "single_asset":
                mode = "standalone_image"
            return "image" if mode == "standalone_image" else "video"
        except Exception:
            return "video"

    def _apply_styles(self) -> None:
        self.setStyleSheet("""
            WelcomeDialog {
                background: #27272b;
                border: 1px solid #3a3a3a;
                border-radius: 12px;
            }
            QLabel#title {
                color: #ff8800;
            }
            QLabel#version {
                color: #666;
            }
            QLabel#subtitle {
                color: #888;
            }
            QLabel#sectionLabel {
                color: #aaa;
                font-size: 11px;
                font-weight: bold;
                letter-spacing: 0.5px;
            }
            QPushButton#primaryBtn {
                background: transparent;
                color: #ff8800;
                border: 1px solid #ff8800;
                border-radius: 4px;
                padding: 0 20px;
                font-size: 12px;
                font-weight: bold;
            }
            QPushButton#primaryBtn:hover {
                background: rgba(255, 136, 0, 0.12);
            }
            QPushButton#primaryBtn:pressed {
                background: rgba(255, 136, 0, 0.2);
            }
            QPushButton#secondaryBtn {
                background: transparent;
                color: #aaa;
                border: 1px solid #555;
                border-radius: 4px;
                padding: 0 20px;
                font-size: 12px;
            }
            QPushButton#secondaryBtn:hover {
                color: #ddd;
                border-color: #777;
            }
            QPushButton#secondaryBtn:pressed {
                background: rgba(255, 255, 255, 0.05);
            }
            QPushButton#modeBtn {
                background: transparent;
                color: #aaa;
                border: 1px solid #555;
                border-radius: 4px;
                padding: 0 16px;
                font-size: 12px;
            }
            QPushButton#modeBtn:hover {
                color: #ddd;
                border-color: #777;
            }
            QPushButton#modeBtn:checked {
                color: #ff8800;
                border-color: #ff8800;
                font-weight: bold;
            }
            QPushButton#toggleBtn {
                background: transparent;
                border: none;
                color: #555;
                font-size: 11px;
                text-align: left;
                padding: 4px 0;
            }
            QPushButton#toggleBtn:hover {
                color: #888;
            }
            QPushButton#toggleBtn:checked {
                color: #888;
            }
            QListWidget#recentList {
                background: transparent;
                border: none;
                color: #bbb;
                font-size: 12px;
                outline: none;
            }
            QListWidget#recentList::item {
                padding: 6px 4px;
                border-bottom: 1px solid #333;
            }
            QListWidget#recentList::item:hover {
                background: rgba(255, 255, 255, 0.03);
            }
            QListWidget#recentList::item:selected {
                background: rgba(255, 136, 0, 0.1);
                color: #ff8800;
            }
        """)

    def _on_open(self) -> None:
        self.open_file_requested.emit()
        self.accept()

    def _on_recent_double_click(self, item: QListWidgetItem) -> None:
        filepath = item.data(Qt.ItemDataRole.UserRole)
        if filepath and os.path.exists(filepath):
            self.recent_file_selected.emit(filepath)
            self.accept()

    def _on_project_double_click(self, item: QListWidgetItem) -> None:
        filepath = item.data(Qt.ItemDataRole.UserRole)
        if filepath and os.path.exists(filepath):
            self.recent_project_selected.emit(filepath)
            self.accept()

    def _on_mode_clicked(self, mode: str) -> None:
        """Switch the work mode and offer to restart immediately.

        The mode only takes effect on next launch (feature flags are resolved
        at startup), so offer to restart now via the shared restart helper
        (src.shared.app_restart, same mechanism as the Preferences dialog).
        """
        from PyQt6.QtWidgets import QMessageBox
        current = self._config.get("work_mode") or "video"
        if mode == current:
            # No change — keep the radio state accurate.
            btn = self._mode_btns.get(current)
            if btn is not None:
                btn.setChecked(True)
            return
        self._config.set("work_mode", mode, auto_save=True)
        target_label = I18N.tr(
            "preferences.general.work_mode.image" if mode == "image"
            else "preferences.general.work_mode.video",
            "Image Grading" if mode == "image" else "Video Grading",
        )
        reply = QMessageBox.question(
            self,
            I18N.tr("welcome.mode_changed.title", "Work mode changed"),
            I18N.tr(
                "welcome.mode_changed.message",
                "Work mode will switch to {mode} after restart.\n\n"
                "Would you like to restart now?",
            ).format(mode=target_label),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply == QMessageBox.StandardButton.Yes:
            # [R2-6] Save any pending config before restarting.
            try:
                self._config.save_config()
            except Exception as exc:
                # AUD-4112: 保存失败不能静默重启 —— 用户已被告知模式将切换，
                # 若落盘失败重启后模式未变，是极难排查的状态不一致。（此前的
                # set(work_mode, auto_save=True) 已落盘一次，这里是冗余二次
                # 保存，失败仍值得提示。）
                logger.error("Config save before restart failed: %s", exc)
                QMessageBox.warning(
                    self,
                    I18N.tr("welcome.mode_changed.title", "Work mode changed"),
                    I18N.tr(
                        "welcome.mode_changed.save_failed",
                        "Failed to save settings ({error}).\n"
                        "The new work mode may not take effect.",
                    ).format(error=exc),
                )
            from src.shared.app_restart import restart_application
            restart_application()

    def _on_toggle_startup(self, checked: bool) -> None:
        # [R2-7] auto_save=True：此前依赖退出时才落盘，崩溃即回滚
        self._config.set("show_welcome_on_startup", checked, auto_save=True)
