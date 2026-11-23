"""
WelcomeDialog - Startup welcome screen for Topos Color.

Displays version info, recent files, and quick action shortcuts.
Shown on startup when `show_welcome_on_startup` config is True.
"""

import logging
import os
from pathlib import Path

import qtawesome as qta
from PyQt6.QtWidgets import (
    QButtonGroup, QCheckBox, QDialog, QFrame, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QPushButton, QVBoxLayout,
)
from PyQt6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, pyqtSignal
from PyQt6.QtGui import QColor, QIcon
from src.shared.i18n.i18n import I18N
from src.app.update_checker import CURRENT_VERSION

logger = logging.getLogger(__name__)

_OPEN_ICON = (Path(__file__).resolve().parents[2] / "shared" / "assets" / "icons"
              / "buttons" / "folder-open.svg")


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
        from src.shared.feature_flags import get_product_mode, get_work_mode

        self.setWindowTitle(I18N.tr("welcome.title", "Welcome to Topos"))
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMinimumWidth(500)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        surface = QFrame()
        surface.setObjectName("welcomeSurface")
        outer.addWidget(surface)
        layout = QVBoxLayout(surface)
        layout.setContentsMargins(24, 20, 24, 18)
        layout.setSpacing(16)

        header = QHBoxLayout()
        header.setSpacing(10)
        title = QLabel(I18N.tr("app.brand", "Topos"))
        title.setObjectName("welcomeTitle")
        header.addWidget(title, alignment=Qt.AlignmentFlag.AlignBaseline)
        version = QLabel(f"v{CURRENT_VERSION}")
        version.setObjectName("welcomeVersion")
        header.addWidget(version, alignment=Qt.AlignmentFlag.AlignBaseline)
        header.addStretch()

        close_text = I18N.tr("welcome.close", "Close")
        self.close_btn = QPushButton()
        self.close_btn.setObjectName("welcomeClose")
        self.close_btn.setIcon(qta.icon("fa5s.times", color="#b8b8bf"))
        self.close_btn.setIconSize(QSize(14, 14))
        self.close_btn.setFixedSize(28, 28)
        self.close_btn.setAccessibleName(close_text)
        self.close_btn.setToolTip(close_text)
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.setAutoDefault(False)
        self.close_btn.clicked.connect(self.reject)
        header.addWidget(self.close_btn, alignment=Qt.AlignmentFlag.AlignTop)
        layout.addLayout(header)

        actions = QHBoxLayout()
        actions.setSpacing(12)

        studio = get_product_mode() == "studio"
        if studio:
            modes = QFrame()
            modes.setObjectName("welcomeModes")
            mode_row = QHBoxLayout(modes)
            mode_row.setContentsMargins(3, 3, 3, 3)
            mode_row.setSpacing(2)
            mode_group = QButtonGroup(modes)
            current_mode = get_work_mode(self._config)
            self._mode_btns = {}
            for mode_value, mode_text_default in (
                ("image", "Image Grading"),
                ("video", "Video Grading"),
            ):
                btn = QPushButton(
                    I18N.tr(f"preferences.general.work_mode.{mode_value}", mode_text_default)
                )
                btn.setObjectName("welcomeMode")
                btn.setCheckable(True)
                mode_group.addButton(btn)
                btn.setChecked(mode_value == current_mode)
                btn.setFixedHeight(30)
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.setAutoDefault(False)
                btn.clicked.connect(lambda checked, m=mode_value: self._on_mode_clicked(m))
                mode_row.addWidget(btn)
                self._mode_btns[mode_value] = btn
            actions.addWidget(modes)
            actions.addStretch()

        self.open_btn = QPushButton(I18N.tr("welcome.open_image", "Open Image…"))
        self.open_btn.setObjectName("welcomeOpen")
        self.open_btn.setProperty("withWorkModes", studio)
        icon = QIcon(str(_OPEN_ICON))
        icon.addFile(str(_OPEN_ICON), mode=QIcon.Mode.Active)
        self.open_btn.setIcon(icon)
        self.open_btn.setIconSize(QSize(18, 18))
        self.open_btn.setFixedHeight(36)
        self.open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_btn.setAutoDefault(False)
        self.open_btn.clicked.connect(self._on_open)
        actions.addWidget(self.open_btn)
        if not studio:
            actions.addStretch()
        layout.addLayout(actions)

        self.recent_list = self._add_recent_section(
            layout, I18N.tr("welcome.recent_files", "Recent Files")
        )
        self.recent_list.itemDoubleClicked.connect(self._on_recent_double_click)
        self._populate_recent()

        if studio:
            self.projects_list = self._add_recent_section(
                layout, I18N.tr("welcome.recent_projects", "Recent Projects")
            )
            self.projects_list.itemDoubleClicked.connect(self._on_project_double_click)
            self._populate_recent_projects()

        self.show_check = QCheckBox(I18N.tr("welcome.show_at_startup", "Show at startup"))
        self.show_check.setObjectName("welcomeStartup")
        self.show_check.setChecked(
            self._config_bool(self._config.get("show_welcome_on_startup"), default=True)
        )
        self.show_check.setCursor(Qt.CursorShape.PointingHandCursor)
        self.show_check.toggled.connect(self._on_toggle_startup)
        layout.addWidget(self.show_check, alignment=Qt.AlignmentFlag.AlignLeft)
        self.open_btn.setFocus()

    @staticmethod
    def _add_recent_section(layout: QVBoxLayout, title: str) -> QListWidget:
        section = QVBoxLayout()
        section.setSpacing(7)
        label = QLabel(title)
        label.setObjectName("welcomeSection")
        section.addWidget(label)
        recent = QListWidget()
        recent.setObjectName("welcomeRecent")
        recent.setAccessibleName(title)
        recent.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        recent.setTextElideMode(Qt.TextElideMode.ElideRight)
        recent.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        recent.setFixedHeight(98)
        section.addWidget(recent)
        layout.addLayout(section)
        return recent

    @staticmethod
    def _recent_item(path: str) -> QListWidgetItem:
        filename = os.path.basename(path)
        parent = os.path.basename(os.path.dirname(path))
        item = QListWidgetItem(f"{filename}  ·  {parent}" if parent else filename)
        item.setData(Qt.ItemDataRole.UserRole, path)
        item.setToolTip(path)
        return item

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
            item = self._recent_item(filepath)
            if not os.path.exists(filepath):
                item.setForeground(QColor("#a6a6ae"))
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
            self.projects_list.addItem(self._recent_item(path))

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
            QFrame#welcomeSurface {
                background: #232327;
                border: 1px solid #414147;
                border-radius: 12px;
            }
            QLabel#welcomeTitle, QLabel#welcomeVersion, QLabel#welcomeSection {
                background: transparent;
                border: none;
            }
            QLabel#welcomeTitle {
                color: #f1f0f2;
                font-size: 32px;
                font-weight: 700;
            }
            QLabel#welcomeVersion {
                color: #a3a3ad;
                font-size: 11px;
                font-weight: 400;
            }
            QLabel#welcomeSection {
                color: #d4d4db;
                font-size: 12px;
                font-weight: 600;
            }
            QPushButton#welcomeClose {
                background: transparent;
                border: 1px solid transparent;
                border-radius: 6px;
                padding: 0;
            }
            QPushButton#welcomeClose:hover { background: #36363d; }
            QPushButton#welcomeClose:pressed { background: #414149; }
            QPushButton#welcomeClose:focus { border-color: #a3a3ad; }
            QPushButton#welcomeOpen {
                background: #b64a00;
                color: #ffffff;
                border: 1px solid transparent;
                border-radius: 7px;
                padding: 0 14px;
                font-size: 13px;
                font-weight: 600;
            }
            QPushButton#welcomeOpen:hover { background: #bf5000; }
            QPushButton#welcomeOpen:pressed { background: #a14000; }
            QPushButton#welcomeOpen:focus { border-color: #ffbf75; }
            QPushButton#welcomeOpen[withWorkModes="true"] {
                background: #2e2e34;
                border-color: #43434a;
            }
            QPushButton#welcomeOpen[withWorkModes="true"]:hover { background: #393940; }
            QPushButton#welcomeOpen[withWorkModes="true"]:pressed { background: #26262b; }
            QPushButton#welcomeOpen[withWorkModes="true"]:focus { border-color: #a3a3ad; }
            QFrame#welcomeModes {
                background: #1c1c20;
                border: 1px solid #35353b;
                border-radius: 8px;
            }
            QPushButton#welcomeMode {
                background: #68371f;
                color: #ffe0c8;
                border: 1px solid transparent;
                border-radius: 5px;
                padding: 0 10px;
                font-size: 12px;
                font-weight: 500;
            }
            QPushButton#welcomeMode:hover { background: #814523; }
            QPushButton#welcomeMode:checked {
                background: #b64a00;
                color: #ffffff;
            }
            QPushButton#welcomeMode:checked:hover { background: #bf5000; }
            QPushButton#welcomeMode:focus { border-color: #ffbf75; }
            QListWidget#welcomeRecent {
                background: #1e1e22;
                border: 1px solid #34343a;
                border-radius: 7px;
                padding: 3px;
                color: #d1d1d8;
                font-size: 12px;
                font-weight: 400;
                outline: none;
            }
            QListWidget#welcomeRecent::item {
                padding: 5px 8px;
                min-height: 18px;
                border-radius: 4px;
            }
            QListWidget#welcomeRecent::item:disabled { color: #a2a2ab; }
            QListWidget#welcomeRecent::item:hover:enabled { background: #2b2b31; }
            QListWidget#welcomeRecent::item:selected {
                background: #3c3128;
                color: #ffc183;
            }
            QListWidget#welcomeRecent QScrollBar:vertical {
                width: 8px;
                border: none;
                background: transparent;
                margin: 2px 0;
            }
            QListWidget#welcomeRecent QScrollBar::handle:vertical {
                min-height: 20px;
                background: #424249;
                border: none;
                border-radius: 3px;
                margin: 0 1px;
            }
            QListWidget#welcomeRecent QScrollBar::handle:vertical:hover {
                background: #5a5a63;
            }
            QListWidget#welcomeRecent QScrollBar::add-line:vertical,
            QListWidget#welcomeRecent QScrollBar::sub-line:vertical {
                height: 0;
                border: none;
            }
            QListWidget#welcomeRecent QScrollBar::add-page:vertical,
            QListWidget#welcomeRecent QScrollBar::sub-page:vertical {
                background: transparent;
            }
            QCheckBox#welcomeStartup {
                color: #b6b6bf;
                font-size: 12px;
                font-weight: 400;
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
