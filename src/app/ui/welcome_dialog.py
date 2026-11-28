"""
WelcomeDialog - Startup welcome screen for Topos Color.

Displays version info, recent files, and quick action shortcuts.
Shown on startup when `show_welcome_on_startup` config is True.
"""

import logging
import os
from collections.abc import Callable
from pathlib import Path

import qtawesome as qta
from PyQt6.QtWidgets import (
    QCheckBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMenu, QMessageBox, QPushButton, QVBoxLayout,
)
from PyQt6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, pyqtSignal
from PyQt6.QtGui import QIcon, QKeySequence, QShortcut
from src.app.ui.startup_dialog import DraggableHeaderDialog, WorkModeSelector
from src.shared.i18n.i18n import I18N
from src.shared.project.library import ProjectMeta, project_library_key, scan_project_libraries
from src.app.update_checker import CURRENT_VERSION

logger = logging.getLogger(__name__)

_IMAGE_FOLDERS_KEY = "image_project_folders"

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


class _ProjectFolderWorker(QRunnable):
    class _Signals(QObject):
        done = pyqtSignal(int, object, bool)

    def __init__(self, request: int, folder: str, config: object) -> None:
        super().__init__()
        self._request = request
        self._folder = Path(folder)
        self._config = config
        self.signals = self._Signals()

    def run(self) -> None:
        try:
            projects, locations = scan_project_libraries(
                self._config, work_mode="image", directories=(self._folder,),
            )
        except (OSError, RuntimeError) as exc:
            logger.warning("Cannot scan image project folder %s: %s", self._folder, exc)
            self.signals.done.emit(self._request, [], False)
            return
        self.signals.done.emit(self._request, projects, locations[0].available)


class _RecentPathProbe(QRunnable):
    class _Signals(QObject):
        checked = pyqtSignal(str, bool)

    def __init__(self, path: str) -> None:
        super().__init__()
        self._path = path
        self.signals = self._Signals()

    def run(self) -> None:
        self.signals.checked.emit(self._path, os.path.isfile(self._path))


class WelcomeDialog(DraggableHeaderDialog):
    """Welcome dialog shown at application startup.

    Signals:
        open_file_requested: Emitted when user clicks "Open Image..."
        recent_file_selected(str): Emitted when user selects a recent file
        recent_project_selected(str): Emitted when user chooses a .topos project
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
        self._pending_recent: tuple[str, bool] | None = None
        self._recent_checks: set[str] = set()
        self._recent_project_paths: list[str] | None = None
        self._project_folder: str | None = None
        self._folder_request = 0
        self._setup_ui()
        self._apply_styles()

    def _setup_ui(self) -> None:
        from src.shared.feature_flags import get_product_mode, get_work_mode

        self.setWindowTitle(I18N.tr("welcome.title", "Welcome to Topos"))
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMinimumWidth(560)
        self.resize(620, 400)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        surface = QFrame()
        surface.setObjectName("welcomeSurface")
        outer.addWidget(surface)
        layout = QVBoxLayout(surface)
        layout.setContentsMargins(24, 20, 24, 18)
        layout.setSpacing(16)

        self._header.setParent(surface)
        self._header.setObjectName("welcomeHeader")
        header = QGridLayout(self._header)
        header.setContentsMargins(0, 0, 0, 0)
        header.setHorizontalSpacing(12)
        header.setColumnStretch(0, 1)
        header.setColumnStretch(2, 1)
        branding = QHBoxLayout()
        branding.setSpacing(8)
        title = QLabel(I18N.tr("app.brand", "Topos"))
        title.setObjectName("welcomeTitle")
        branding.addWidget(title)
        version = QLabel(f"v{CURRENT_VERSION}")
        version.setObjectName("welcomeVersion")
        branding.addWidget(version)
        branding.addStretch()
        header.addLayout(branding, 0, 0)

        product_mode = get_product_mode()
        studio = product_mode == "studio"
        current_mode = get_work_mode(self._config) if studio else "image"
        show_project_browser = product_mode == "color" or (studio and current_mode == "image")
        self._mode_btns: dict[str, QPushButton] = {}
        if studio:
            modes = WorkModeSelector(current_mode)
            self._mode_btns = modes.buttons
            modes.mode_requested.connect(self._on_mode_clicked)
            header.addWidget(modes, 0, 1)

        close_text = I18N.tr("welcome.close", "Close")
        self.close_btn = QPushButton()
        self.close_btn.setObjectName("welcomeClose")
        self.close_btn.setIcon(qta.icon("fa5s.times", color="#b8b8bf"))
        self.close_btn.setIconSize(QSize(14, 14))
        self.close_btn.setFixedSize(36, 36)
        self.close_btn.setAccessibleName(close_text)
        self.close_btn.setToolTip(close_text)
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.setAutoDefault(False)
        self.close_btn.clicked.connect(self.reject)
        header.addWidget(self.close_btn, 0, 2, Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self._header)

        self.recent_list, _ = self._add_recent_section(
            layout, I18N.tr("welcome.recent_files", "Recent Files"), self._on_recent_double_click
        )
        self._populate_recent()

        folder_button = None
        if show_project_browser:
            folder_button = self._secondary_button(
                I18N.tr("welcome.project_folders", "Project Folders"), height=28,
            )
            self.project_folder_menu = QMenu(folder_button)
            self.project_folder_menu.aboutToShow.connect(self._build_project_folder_menu)
            self.project_folder_menu.setToolTipsVisible(True)
            folder_button.setMenu(self.project_folder_menu)
        if studio or show_project_browser:
            self.projects_list, self.projects_label = self._add_recent_section(
                layout, I18N.tr("welcome.recent_projects", "Recent Projects"),
                self._on_project_double_click, folder_button,
            )
            self._populate_recent_projects()

        footer = QHBoxLayout()
        footer.setSpacing(12)
        self.show_check = QCheckBox(I18N.tr("welcome.show_at_startup", "Show at startup"))
        self.show_check.setObjectName("welcomeStartup")
        self.show_check.setChecked(
            self._config_bool(self._config.get("show_welcome_on_startup"), default=True)
        )
        self.show_check.setCursor(Qt.CursorShape.PointingHandCursor)
        self.show_check.toggled.connect(self._on_toggle_startup)
        footer.addWidget(self.show_check)
        footer.addStretch()

        if show_project_browser:
            self.open_project_btn = self._secondary_button(
                I18N.tr("welcome.open_project", "Open Project…"),
            )
            self.open_project_btn.clicked.connect(self._choose_project_file)
            footer.addWidget(self.open_project_btn)

        self.open_btn = QPushButton(I18N.tr("welcome.open_image", "Open Image…"))
        self.open_btn.setObjectName("welcomeOpen")
        icon = QIcon(str(_OPEN_ICON))
        icon.addFile(str(_OPEN_ICON), mode=QIcon.Mode.Active)
        self.open_btn.setIcon(icon)
        self.open_btn.setIconSize(QSize(18, 18))
        self.open_btn.setFixedHeight(36)
        self.open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_btn.setAutoDefault(False)
        self.open_btn.clicked.connect(self._on_open)
        footer.addWidget(self.open_btn)
        layout.addLayout(footer)
        self.open_btn.setFocus()

    @staticmethod
    def _secondary_button(text: str, *, height: int = 36) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("welcomeSecondary")
        button.setFixedHeight(height)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setAutoDefault(False)
        return button

    @staticmethod
    def _add_recent_section(
        layout: QVBoxLayout, title: str, open_item: Callable[[QListWidgetItem | None], None],
        control: QPushButton | None = None,
    ) -> tuple[QListWidget, QLabel]:
        section = QVBoxLayout()
        section.setSpacing(7)
        label = QLabel(title)
        label.setObjectName("welcomeSection")
        label.setTextFormat(Qt.TextFormat.PlainText)
        heading = QHBoxLayout()
        heading.addWidget(label, 1)
        if control is not None:
            heading.addWidget(control)
        section.addLayout(heading)
        recent = QListWidget()
        recent.setObjectName("welcomeRecent")
        recent.setAccessibleName(title)
        recent.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        recent.setTextElideMode(Qt.TextElideMode.ElideRight)
        recent.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        recent.setMinimumHeight(104)
        recent.itemDoubleClicked.connect(open_item)
        for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            shortcut = QShortcut(QKeySequence(key), recent)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(lambda: open_item(recent.currentItem()))
        section.addWidget(recent)
        layout.addLayout(section, 1)
        return recent, label

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
            from src.shared.feature_flags import get_product_mode, get_work_mode
            if get_product_mode() == "color" or get_work_mode(self._config) == "image":
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
            self.recent_list.addItem(self._recent_item(filepath))

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
            from src.shared.feature_flags import get_product_mode, get_work_mode
            current_mode = "image" if get_product_mode() == "color" else get_work_mode(self._config)
        except Exception as exc:
            # AUD-4112: 降级到 video 模式需留痕，掩盖真实失败会误导排障。
            logger.warning("Welcome project work-mode detection failed: %s", exc)
            current_mode = "video"

        if not projects:
            self._on_projects_classified([])
            return

        self._show_project_status(I18N.tr("welcome.loading_projects", "Loading projects…"))
        worker = _ProjectClassifyWorker(projects, current_mode)
        worker.signals.done.connect(self._on_projects_classified)
        QThreadPool.globalInstance().start(worker)

    def _on_projects_classified(self, matching: list[str]) -> None:
        self._recent_project_paths = matching
        if self._project_folder is None:
            self._show_recent_projects()

    def _show_project_status(self, text: str) -> None:
        self.projects_list.clear()
        item = QListWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
        self.projects_list.addItem(item)

    def _set_projects_title(self, title: str, *, hint: str = "") -> None:
        display_title = title
        if hint:
            display_title = self.projects_label.fontMetrics().elidedText(
                title, Qt.TextElideMode.ElideMiddle, self.projects_label.width(),
            )
        self.projects_label.setText(display_title)
        self.projects_label.setToolTip(hint)
        self.projects_list.setAccessibleName(title)

    def _show_recent_projects(self) -> None:
        self._project_folder = None
        self._set_projects_title(I18N.tr("welcome.recent_projects", "Recent Projects"))
        if self._recent_project_paths is None:
            self._show_project_status(I18N.tr("welcome.loading_projects", "Loading projects…"))
        elif not self._recent_project_paths:
            self._show_project_status(I18N.tr("welcome.no_recent_projects", "No recent projects"))
        else:
            self.projects_list.clear()
            for path in self._recent_project_paths:
                self.projects_list.addItem(self._recent_item(path))

    def _project_folders(self) -> list[str]:
        paths = self._str_list(self._config.get(_IMAGE_FOLDERS_KEY))
        return list(dict.fromkeys(
            project_library_key(path) for path in paths if "\x00" not in path and Path(path).is_absolute()
        ))

    def _build_project_folder_menu(self) -> None:
        menu = self.project_folder_menu
        menu.clear()
        recent = menu.addAction(I18N.tr("welcome.recent_projects", "Recent Projects"))
        recent.triggered.connect(self._show_recent_projects)
        recent.setCheckable(True)
        recent.setChecked(self._project_folder is None)
        folders = self._project_folders()
        if folders:
            menu.addSeparator()
            for folder in folders:
                label = menu.fontMetrics().elidedText(folder, Qt.TextElideMode.ElideMiddle, 480)
                item = menu.addAction(label.replace("&", "&&"))
                item.setToolTip(folder)
                item.setCheckable(True)
                item.setChecked(folder == self._project_folder)
                item.triggered.connect(lambda _checked, path=folder: self._show_project_folder(path))
        menu.addSeparator()
        menu.addAction(
            I18N.tr("welcome.add_project_folder", "Add Project Folder…"), self._choose_project_folder,
        )
        if self._project_folder is not None:
            menu.addAction(
                I18N.tr("welcome.remove_project_folder", "Remove This Folder from the List"),
                self._remove_project_folder,
            )

    def _project_picker_directory(self) -> str:
        if self._project_folder is not None:
            return self._project_folder
        path = self._config.get("last_project_path")
        return path if isinstance(path, str) else ""

    def _choose_project_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, I18N.tr("welcome.add_project_folder", "Add Project Folder…"),
            self._project_picker_directory(),
        )
        if folder:
            path = project_library_key(folder)
            folders = self._project_folders()
            if path not in folders:
                self._config.set(_IMAGE_FOLDERS_KEY, self._str_list([path, *folders]), auto_save=True)
            self._show_project_folder(path)

    def _remove_project_folder(self) -> None:
        folders = [folder for folder in self._project_folders() if folder != self._project_folder]
        self._config.set(_IMAGE_FOLDERS_KEY, folders, auto_save=True)
        self._show_recent_projects()

    def _show_project_folder(self, folder: str) -> None:
        self._project_folder = folder
        self._folder_request += 1
        label = I18N.tr("welcome.project_folder_title", "Projects in {folder}").format(
            folder=Path(folder).name or folder,
        )
        self._set_projects_title(label, hint=folder)
        self._show_project_status(I18N.tr("pm.scanning", "Scanning projects…"))
        worker = _ProjectFolderWorker(self._folder_request, folder, self._config)
        worker.signals.done.connect(self._on_project_folder_scanned)
        QThreadPool.globalInstance().start(worker)

    def _on_project_folder_scanned(
        self, request: int, projects: list[ProjectMeta], available: bool,
    ) -> None:
        if request != self._folder_request or self._project_folder is None:
            return
        if not available:
            self._show_project_status(
                I18N.tr("welcome.project_folder_unavailable", "Could not read this project folder.")
            )
        elif not projects:
            self._show_project_status(
                I18N.tr("welcome.no_image_projects_in_folder", "No image projects in this folder.")
            )
        else:
            self.projects_list.clear()
            for project in projects:
                self.projects_list.addItem(self._recent_item(project.path))

    def _choose_project_file(self) -> None:
        filepath, _ = QFileDialog.getOpenFileName(
            self, I18N.tr("welcome.open_project", "Open Project…"), self._project_picker_directory(),
            I18N.tr("project.file_filter", "Topos Projects (*.topos);;All Files (*)").split(";;", 1)[0],
        )
        if filepath:
            self.recent_project_selected.emit(filepath)

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
            QWidget#welcomeHeader {
                background: transparent;
                border: none;
            }
            QLabel#welcomeTitle, QLabel#welcomeVersion, QLabel#welcomeSection {
                background: transparent;
                border: none;
            }
            QLabel#welcomeTitle {
                color: #f1f0f2;
                font-size: 15px;
                font-weight: 700;
            }
            QLabel#welcomeVersion {
                color: #a3a3ad;
                font-size: 12px;
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
            QPushButton#welcomeSecondary {
                background: #303035;
                color: #f2f2f4;
                border: 1px solid #414148;
                border-radius: 6px;
                padding: 0 12px;
                font-size: 13px;
                font-weight: 500;
            }
            QPushButton#welcomeSecondary:hover { background: #393940; }
            QPushButton#welcomeSecondary:pressed { background: #323238; }
            QPushButton#welcomeSecondary:focus { border-color: #ff955c; }
            QListWidget#welcomeRecent {
                background: #1e1e22;
                border: 1px solid #34343a;
                border-radius: 7px;
                padding: 4px;
                color: #e4e4e9;
                font-size: 13px;
                font-weight: 400;
                outline: none;
            }
            QListWidget#welcomeRecent:focus { border-color: #aa754f; }
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
                width: 6px;
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

    def _on_recent_double_click(self, item: QListWidgetItem | None) -> None:
        self._check_recent_path(item, project=False)

    def _on_project_double_click(self, item: QListWidgetItem | None) -> None:
        self._check_recent_path(item, project=True)

    def _check_recent_path(self, item: QListWidgetItem | None, *, project: bool) -> None:
        path = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if not isinstance(path, str) or not path:
            return
        self._pending_recent = path, project
        if path in self._recent_checks:
            return
        self._recent_checks.add(path)
        worker = _RecentPathProbe(path)
        worker.signals.checked.connect(self._on_recent_path_checked, Qt.ConnectionType.QueuedConnection)
        QThreadPool.globalInstance().start(worker)

    def _on_recent_path_checked(self, path: str, available: bool) -> None:
        self._recent_checks.discard(path)
        if self._pending_recent is None or self._pending_recent[0] != path or not self.isVisible():
            return
        _, project = self._pending_recent
        self._pending_recent = None
        if available:
            if project:
                self.recent_project_selected.emit(path)
            else:
                self.recent_file_selected.emit(path)
                self.accept()
            return
        QMessageBox.warning(
            self,
            I18N.tr("error.title", "Error"),
            I18N.tr("error.file_not_found", "File not found: {0}").format(path),
        )

    def _on_mode_clicked(self, mode: str) -> None:
        """Switch the work mode and offer to restart immediately.

        The mode only takes effect on next launch (feature flags are resolved
        at startup), so offer to restart now via the shared restart helper
        (src.shared.app_restart, same mechanism as the Preferences dialog).
        """
        from src.shared.feature_flags import get_work_mode

        current = get_work_mode(self._config)
        if mode == current:
            # No change — keep the radio state accurate.
            btn = self._mode_btns.get(current)
            if btn is not None:
                btn.setChecked(True)
            return
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
        if reply != QMessageBox.StandardButton.Yes:
            btn = self._mode_btns.get(current)
            if btn is not None:
                btn.setChecked(True)
            return
        self._config.set("work_mode", mode, auto_save=True)
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
