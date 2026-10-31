"""Project Manager dialog (video mode only).

A DaVinci Resolve-style startup screen shown when the app launches in video
mode. Users create, open, rename and delete video projects from a fixed
library directory. Image mode never shows this dialog — image projects stay
sidecar .topos files managed by the existing flow.

The video-mode manager uses a local project browser with card previews,
search, sorting, and quick actions. Image mode keeps the separate lightweight
WelcomeDialog flow.
"""

from __future__ import annotations

import datetime
import logging
import os
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal, QSize, QRect, QObject, QRunnable, QThreadPool
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QLinearGradient,
    QPainter,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QDialog,
    QInputDialog,
    QHBoxLayout,
    QFileDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QListView,
    QMenu,
    QMessageBox,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
)

from src.app.update_checker import CURRENT_VERSION
from src.shared.i18n.i18n import I18N
from src.shared.project.library import (
    ProjectMeta,
    delete_project,
    duplicate_project,
    extract_cover_thumbnail,
    get_project_library_dir,
    get_project_library_dirs,
    list_project_backups,
    register_project_library_dir,
    rename_project,
    restore_project_backup,
    scan_video_projects,
)
from src.shared.media.thumbnail_provider import ThumbnailProvider
from src.shared.assets.icon_manager import icon_folder_open, icon_new_folder
from src.shared.ui.widgets.chevron_combo import ChevronComboBox

logger = logging.getLogger(__name__)


class _ProjectScanWorker(QRunnable):
    """[R0-7] 在线程池执行 scan_video_projects。

    逐个 .topos 读 manifest 的库扫描项目多时秒级，此前在 UI 线程同步执行，
    拖慢 Project Manager 启动/刷新。结果经队列信号回 UI 线程渲染。"""

    class _Signals(QObject):
        done = pyqtSignal(object)  # list[ProjectMeta]

    def __init__(self, config) -> None:
        super().__init__()
        self._config = config
        self.signals = self._Signals()

    def run(self) -> None:
        try:
            projects = scan_video_projects(self._config)
        except Exception:
            logger.exception("project library scan failed")
            projects = []
        self.signals.done.emit(projects)


class _ProjectCardDelegate(QStyledItemDelegate):
    """Paint compact project cards with separate title and timestamp styles."""

    PROJECT_NAME_ROLE = Qt.ItemDataRole.UserRole + 1
    PROJECT_TIME_ROLE = Qt.ItemDataRole.UserRole + 2
    PROJECT_FAVORITE_ROLE = Qt.ItemDataRole.UserRole + 3
    PROJECT_TAGS_ROLE = Qt.ItemDataRole.UserRole + 4

    def sizeHint(self, option, index):  # noqa: N802 - Qt override
        return QSize(238, 178)

    def paint(self, painter: QPainter, option, index) -> None:  # noqa: N802 - Qt override
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect.adjusted(3, 3, -3, -3)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)

        if selected:
            fill = QColor("#39302b")
            border = QColor("#e56b2f")
        elif hovered:
            fill = QColor("#30333b")
            border = QColor("#646975")
        else:
            fill = QColor("#27292f")
            border = QColor("#3c3f48")

        painter.setBrush(QBrush(fill))
        painter.setPen(QPen(border, 1))
        painter.drawRoundedRect(rect, 6, 6)

        preview_rect = QRect(rect.left() + 1, rect.top() + 1, rect.width() - 2, 124)
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        if isinstance(icon, QIcon):
            preview = icon.pixmap(preview_rect.size())
            painter.drawPixmap(preview_rect, preview)

        name = str(index.data(self.PROJECT_NAME_ROLE) or "Untitled Project")
        modified = str(index.data(self.PROJECT_TIME_ROLE) or "—")
        text_rect = QRect(rect.left() + 8, preview_rect.bottom() + 7, rect.width() - 16, 20)
        painter.setPen(QColor("#f0f1f3"))
        painter.setFont(QFont("Segoe UI", 11, QFont.Weight.DemiBold))
        painter.drawText(
            text_rect,
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            QFontMetrics(painter.font()).elidedText(
                name, Qt.TextElideMode.ElideRight, text_rect.width()
            ),
        )

        if bool(index.data(self.PROJECT_FAVORITE_ROLE)):
            painter.setPen(QColor("#f0a03a"))
            painter.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
            painter.drawText(
                QRect(rect.right() - 28, rect.top() + 6, 20, 20),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
                "★",
            )

        time_rect = QRect(rect.left() + 8, text_rect.bottom() + 1, rect.width() - 16, 15)
        painter.setPen(QColor("#858892"))
        painter.setFont(QFont("Segoe UI", 8))
        painter.drawText(
            time_rect,
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            modified,
        )
        painter.restore()


class ProjectManagerDialog(QDialog):
    """Video-mode project manager shown at startup.

    Signals:
        project_selected(str): Emitted with the .topos path to open.
        new_project_requested(): Emitted when the user clicks "New Project".
    """

    project_selected = pyqtSignal(str)
    new_project_requested = pyqtSignal()
    project_renamed = pyqtSignal(str, str)

    def __init__(self, config_manager, parent=None, active_project_path: str | None = None) -> None:
        super().__init__(parent)
        # [R2-2] 关闭即销毁：此前每次新建、parent=self、无 WA_DeleteOnClose，
        # C++ 对象随启动/刷新次数永久累积；且每次创建都向单例
        # ThumbnailProvider 重复 connect，历史对话框全被缩略图信号刷新。
        # WA_DeleteOnClose 销毁时 Qt 自动断开本对象的全部连接，一并解决。
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._config = config_manager
        self._active_project_path = (
            os.path.abspath(active_project_path) if active_project_path else None
        )
        self._preview_items: dict[str, QListWidgetItem] = {}
        self._preview_paths: dict[str, str] = {}
        # [AUD-40001] 已解码 .topos 封面按 path 缓存：搜索每个按键/排序
        # 每次变更都会整表重建并逐项重开 zip 解压封面（extract_cover_
        # thumbnail），抵消 [R0-7] 异步扫描；缓存随刷新清空、容量有上限。
        self._cover_pixmap_cache: dict[str, QPixmap] = {}
        self._thumbnail_provider = ThumbnailProvider.instance()
        self._thumbnail_provider.thumbnail_ready.connect(self._on_thumbnail_ready)
        self._setup_ui()
        self._apply_styles()
        self._refresh_project_list()

    # ── UI ─────────────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        self.setWindowTitle(I18N.tr("pm.title", "Project Manager"))
        self.resize(1120, 760)
        self.setMinimumSize(900, 620)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.FramelessWindowHint)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 24, 30, 22)
        layout.setSpacing(14)

        # ── Header ─────────────────────────────────────────────────────────
        header = QHBoxLayout()
        header.setSpacing(10)
        title = QLabel(I18N.tr("app.brand", "Topos"))
        title.setObjectName("pmTitle")
        title.setFont(QFont("Segoe UI", 25, QFont.Weight.Bold))
        header.addWidget(title)
        version = QLabel(f"v{CURRENT_VERSION}")
        version.setObjectName("pmVersion")
        version.setFont(QFont("Segoe UI", 10))
        header.addWidget(version)
        header.addStretch()

        self.refresh_btn = QPushButton("↻")
        self.refresh_btn.setObjectName("pmIconBtn")
        self.refresh_btn.setToolTip(I18N.tr("pm.refresh", "Refresh projects"))
        self.refresh_btn.setFixedSize(32, 30)
        self.refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_btn.clicked.connect(self._refresh_project_list)
        header.addWidget(self.refresh_btn)

        # Work mode switch (so users can flip to image mode from here too).
        self._mode_btns = {}
        from src.shared.feature_flags import get_product_mode, get_work_mode
        if get_product_mode() == "studio":
            current_mode = get_work_mode(self._config)
            for mode_value, text_key, text_default in (
                ("image", "preferences.general.work_mode.image", "Image Grading"),
                ("video", "preferences.general.work_mode.video", "Video Grading"),
            ):
                btn = QPushButton(I18N.tr(text_key, text_default))
                btn.setObjectName("pmModeBtn")
                btn.setCheckable(True)
                btn.setChecked(mode_value == current_mode)
                btn.setFixedHeight(26)
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.clicked.connect(lambda checked, m=mode_value: self._on_mode_clicked(m))
                header.addWidget(btn)
                self._mode_btns[mode_value] = btn
        layout.addLayout(header)

        subtitle = QLabel(
            I18N.tr("pm.subtitle", "Choose a project to continue, or start something new")
        )
        subtitle.setObjectName("pmSubtitle")
        layout.addWidget(subtitle)

        # ── Toolbar ────────────────────────────────────────────────────────
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        section = QLabel(I18N.tr("pm.projects", "PROJECTS"))
        section.setObjectName("pmSection")
        toolbar.addWidget(section)
        toolbar.addStretch()

        self.search_box = QLineEdit()
        self.search_box.setObjectName("pmSearch")
        self.search_box.setPlaceholderText(I18N.tr("pm.search_placeholder", "Search projects..."))
        self.search_box.setFixedHeight(32)
        self.search_box.textChanged.connect(self._on_search_changed)
        toolbar.addWidget(self.search_box, 0)

        # Use the shared ChevronComboBox so the expand/collapse arrow is
        # painted consistently on macOS and other platforms.
        self.sort_box = ChevronComboBox()
        self.sort_box.setObjectName("pmSort")
        self.sort_box.addItem(I18N.tr("pm.sort_recent", "Recently modified"), "recent")
        self.sort_box.addItem(I18N.tr("pm.sort_name", "Name"), "name")
        self.sort_box.addItem(I18N.tr("pm.sort_favorites", "Favorites first"), "favorites")
        self.sort_box.setFixedHeight(32)
        self.sort_box.currentIndexChanged.connect(self._on_sort_changed)
        toolbar.addWidget(self.sort_box)

        self.browse_btn = QPushButton(I18N.tr("pm.open_from_disk", "Open..."))
        self.browse_btn.setObjectName("pmSecondaryBtn")
        self.browse_btn.setIcon(icon_folder_open())
        self.browse_btn.setIconSize(QSize(15, 15))
        self.browse_btn.setFixedHeight(32)
        self.browse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.browse_btn.clicked.connect(self._on_browse_clicked)
        toolbar.addWidget(self.browse_btn)

        self.add_location_btn = QPushButton("+")
        self.add_location_btn.setObjectName("pmSecondaryBtn")
        self.add_location_btn.setText("")
        self.add_location_btn.setIcon(icon_new_folder())
        self.add_location_btn.setIconSize(QSize(16, 16))
        self.add_location_btn.setAccessibleName(
            I18N.tr("pm.add_library", "Add project library")
        )
        self.add_location_btn.setFixedSize(32, 32)
        self.add_location_btn.setToolTip(I18N.tr("pm.add_library", "Add project library"))
        self.add_location_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_location_btn.clicked.connect(self._on_add_library_location)
        toolbar.addWidget(self.add_location_btn)
        layout.addLayout(toolbar)

        # [R2-10] "继续上次项目"一键入口：上次会话打开过项目且文件仍在时显示
        last_project = ""
        try:
            last_project = self._config.get("last_project") or ""
        except Exception:
            pass
        if last_project and os.path.exists(last_project):
            self.resume_btn = QPushButton(
                I18N.tr("pm.resume_last", "Resume last project · {name}").format(
                    name=os.path.splitext(os.path.basename(last_project))[0]
                ))
            self.resume_btn.setObjectName("pmSecondaryBtn")
            self.resume_btn.setIcon(icon_folder_open())
            self.resume_btn.setIconSize(QSize(15, 15))
            self.resume_btn.setFixedHeight(32)
            self.resume_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.resume_btn.clicked.connect(self._on_resume_last_project)
            layout.addWidget(self.resume_btn)

        # ── Project list ───────────────────────────────────────────────────
        self.list_widget = QListWidget()
        self.list_widget.setObjectName("pmList")
        self.list_widget.setViewMode(QListView.ViewMode.IconMode)
        self.list_widget.setFlow(QListView.Flow.LeftToRight)
        self.list_widget.setWrapping(True)
        self.list_widget.setResizeMode(QListView.ResizeMode.Adjust)
        self.list_widget.setMovement(QListView.Movement.Static)
        self.list_widget.setSpacing(8)
        self.list_widget.setIconSize(QSize(236, 124))
        # The viewport is roughly 1,000px wide at the default window size;
        # 246px cells therefore keep four cards on one row with small gaps.
        self.list_widget.setGridSize(QSize(246, 188))
        self.list_widget.setUniformItemSizes(True)
        self.list_widget.setWordWrap(True)
        self.list_widget.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.list_widget.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.list_widget.setItemDelegate(_ProjectCardDelegate(self.list_widget))
        self.list_widget.itemDoubleClicked.connect(self._on_item_double_click)
        self.list_widget.itemSelectionChanged.connect(self._on_selection_changed)
        self.list_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self._on_context_menu)
        layout.addWidget(self.list_widget, 1)

        self.empty_label = QLabel(self.list_widget.viewport())
        self.empty_label.setObjectName("pmEmptyState")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.empty_label.hide()

        # ── Footer ─────────────────────────────────────────────────────────
        footer = QHBoxLayout()
        footer.setSpacing(12)
        self.location_label = QLabel()
        self.location_label.setObjectName("pmFooter")
        self._update_location_label()
        footer.addWidget(self.location_label, 1)

        self.selection_label = QLabel(
            I18N.tr("pm.no_selection", "Select a project to open")
        )
        self.selection_label.setObjectName("pmSelection")
        footer.addWidget(self.selection_label)
        layout.addLayout(footer)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self.new_btn = QPushButton(I18N.tr("pm.new_project", "New Project"))
        self.new_btn.setObjectName("pmPrimaryBtn")
        self.new_btn.setFixedHeight(36)
        self.new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.new_btn.clicked.connect(self._on_new_project)
        btn_row.addWidget(self.new_btn)
        btn_row.addStretch()
        self.open_btn = QPushButton(I18N.tr("pm.open", "Open"))
        self.open_btn.setObjectName("pmPrimaryBtn")
        self.open_btn.setFixedHeight(36)
        self.open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_btn.clicked.connect(self._on_open_clicked)
        btn_row.addWidget(self.open_btn)

        self.delete_btn = QPushButton(I18N.tr("pm.delete", "Delete"))
        self.delete_btn.setObjectName("pmDangerBtn")
        self.delete_btn.setFixedHeight(36)
        self.delete_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.delete_btn.clicked.connect(self._on_delete_selected)
        btn_row.addWidget(self.delete_btn)

        self.close_btn = QPushButton(I18N.tr("pm.close", "Close"))
        self.close_btn.setObjectName("pmSecondaryBtn")
        self.close_btn.setFixedHeight(36)
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.clicked.connect(self.reject)
        btn_row.addWidget(self.close_btn)
        layout.addLayout(btn_row)

    def _apply_styles(self) -> None:
        self.setStyleSheet("""
            ProjectManagerDialog {
                background: #202126;
                border: 1px solid #3b3d45;
                border-radius: 10px;
            }
            QLabel { background: transparent; }
            QLabel#pmTitle { color: #f4f4f5; }
            QLabel#pmVersion { color: #7d8089; }
            QLabel#pmSubtitle { color: #92959f; font-size: 12px; }
            QLabel#pmSection {
                color: #d6d7db; font-size: 11px; font-weight: bold;
                letter-spacing: 1px;
            }
            QLabel#pmFooter { color: #737681; font-size: 11px; }
            QLabel#pmSelection { color: #a7a9b0; font-size: 11px; }
            QLabel#pmEmptyState { color: #858892; font-size: 13px; padding: 30px; }
            QPushButton#pmPrimaryBtn {
                background: #e56b2f; color: #fff;
                border: 1px solid #f07b3b; border-radius: 5px;
                padding: 0 20px; font-size: 12px; font-weight: bold;
            }
            QPushButton#pmPrimaryBtn:hover { background: #f07b3b; }
            QPushButton#pmPrimaryBtn:pressed { background: #c95a27; }
            QPushButton#pmPrimaryBtn:disabled { color: #666; background: #36383f; border-color: #444; }
            QPushButton#pmSecondaryBtn {
                background: #2b2d33; color: #c1c3c9;
                border: 1px solid #484b55; border-radius: 5px;
                padding: 0 16px; font-size: 12px;
            }
            QPushButton#pmSecondaryBtn:hover { color: #fff; border-color: #777b87; background: #33363e; }
            QPushButton#pmDangerBtn {
                background: #2b2d33; color: #d98c82;
                border: 1px solid #75453f; border-radius: 5px;
                padding: 0 16px; font-size: 12px;
            }
            QPushButton#pmDangerBtn:hover { color: #fff; background: #653731; border-color: #c15d4e; }
            QPushButton#pmDangerBtn:disabled { color: #666; border-color: #444; background: #36383f; }
            QPushButton#pmIconBtn {
                background: transparent; color: #aeb1ba;
                border: 1px solid #484b55; border-radius: 5px;
                font-size: 18px; padding: 0;
            }
            QPushButton#pmIconBtn:hover { color: #fff; border-color: #777b87; }
            QPushButton#pmModeBtn {
                background: #2a2c32; color: #aeb1ba;
                border: 1px solid #484b55; border-radius: 5px;
                padding: 0 14px; font-size: 11px;
            }
            QPushButton#pmModeBtn:checked {
                color: #fff; background: #e56b2f; border-color: #f07b3b; font-weight: bold;
            }
            QPushButton#pmModeBtn:checked:hover { background: #f07b3b; }
            QLineEdit#pmSearch {
                background: #292b31; color: #e6e7ea;
                border: 1px solid #454852; border-radius: 5px;
                padding: 4px 12px; font-size: 12px; min-width: 210px;
            }
            QLineEdit#pmSearch:focus { border-color: #8b8f99; }
            QComboBox#pmSort {
                background: #292b31; color: #c7c9cf;
                border: 1px solid #454852; border-radius: 5px;
                padding: 0 10px; min-width: 138px;
            }
            QComboBox#pmSort QAbstractItemView {
                background: #292b31; color: #eee; selection-background-color: #454952;
            }
            QListWidget#pmList {
                background: #18191d; border: 1px solid #363941;
                border-radius: 7px; color: #d7d8dc; font-size: 12px;
                outline: none; padding: 8px;
            }
            QListWidget#pmList::item { background: transparent; border: none; padding: 0; }
        """)

    # ── Data ───────────────────────────────────────────────────────────────

    def _on_resume_last_project(self) -> None:
        """[R2-10] 一键恢复上次会话的项目。"""
        last_project = ""
        try:
            last_project = self._config.get("last_project") or ""
        except Exception:
            pass
        if last_project and os.path.exists(last_project):
            self.project_selected.emit(last_project)

    def _refresh_project_list(self) -> None:
        """Re-scan the library and rebuild the list.

        [R0-7] 扫描挪到线程池：先清空列表并显示"扫描中"骨架，完成后回
        UI 线程过滤渲染。进行中的重复刷新合并为 pending，避免并发扫描。"""
        if getattr(self, "_scan_inflight", False):
            self._scan_pending = True
            return
        self._scan_inflight = True
        self._all_projects = []
        self.list_widget.clear()
        self._preview_items.clear()
        self._preview_paths.clear()
        # [AUD-40001] 封面缓存随刷新失效（磁盘上的封面可能已变）。
        self._cover_pixmap_cache.clear()
        self.empty_label.setText(I18N.tr("pm.scanning", "Scanning projects…"))
        self.empty_label.setVisible(True)
        self.empty_label.setGeometry(self.list_widget.viewport().rect())
        worker = _ProjectScanWorker(self._config)
        worker.signals.done.connect(self._on_scan_done)
        QThreadPool.globalInstance().start(worker)

    def _on_scan_done(self, projects) -> None:
        self._scan_inflight = False
        self._all_projects = projects
        self._apply_filter(self.search_box.text())
        if getattr(self, "_scan_pending", False):
            self._scan_pending = False
            self._refresh_project_list()

    def _update_location_label(self) -> None:
        locations = get_project_library_dirs(self._config)
        location_text = [str(path) for path in locations]
        if len(location_text) == 1:
            label = I18N.tr("pm.library_at", "Project library · {path}").format(
                path=location_text[0]
            )
        else:
            label = I18N.tr(
                "pm.library_more", "Project libraries · {count} · {path}…"
            ).format(count=len(location_text), path=location_text[0])
        self.location_label.setText(label)
        self.location_label.setToolTip("\n".join(location_text))

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self.empty_label.setGeometry(self.list_widget.viewport().rect())

    def _favorite_paths(self) -> set[str]:
        try:
            values = self._config.get("favorite_projects") or []
        except Exception:
            values = []
        return {os.path.abspath(str(path)) for path in values if path}

    def _project_tags(self, path: str) -> list[str]:
        try:
            mapping = self._config.get("project_tags") or {}
            tags = mapping.get(path) or mapping.get(os.path.abspath(path)) or []
        except Exception:
            tags = []
        return [str(tag).strip() for tag in tags if str(tag).strip()]

    def _set_favorite(self, path: str, favorite: bool) -> None:
        values = list(self._favorite_paths())
        normalized = os.path.abspath(path)
        values = [value for value in values if value != normalized]
        if favorite:
            values.insert(0, normalized)
        self._config.set("favorite_projects", values, auto_save=True)

    def _set_project_tags(self, path: str, tags: list[str]) -> None:
        try:
            mapping = dict(self._config.get("project_tags") or {})
        except Exception:
            mapping = {}
        normalized = os.path.abspath(path)
        if tags:
            mapping[normalized] = tags
        else:
            mapping.pop(normalized, None)
        self._config.set("project_tags", mapping, auto_save=True)

    @staticmethod
    def _safe_meta_name(meta: ProjectMeta) -> str:
        """[AUD-40002] 归档内 project.json 的 name 可为任意 JSON 类型。

        read_project_meta（library.py）取 name 时只做 ``or`` 兜底、不验类
        型；数字/列表等真值非 str 直接进 ``casefold()``/``join()`` 会让排
        序或任意一次搜索按键在槽内抛 AttributeError/TypeError，列表停在
        清空状态。消费点统一走本收敛。
        """
        name = getattr(meta, "name", None)
        if isinstance(name, str):
            return name
        if name is None:
            return ""
        return str(name)

    @staticmethod
    def _safe_meta_mtime(meta: ProjectMeta) -> float:
        """[AUD-40002] modified_at/file_modified_at 同为外部数据，排序键
        统一收敛为 float，防止 str 与 float 混排时 sort 比较抛 TypeError。"""
        for value in (meta.modified_at, meta.file_modified_at):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
        return 0.0

    def _apply_filter(self, text: str) -> None:
        self.list_widget.clear()
        self._preview_items.clear()
        self._preview_paths.clear()
        needle = (text or "").strip().lower()
        projects = list(self._all_projects)
        favorite_paths = self._favorite_paths()
        if self.sort_box.currentData() == "name":
            projects.sort(
                key=lambda meta: self._safe_meta_name(meta).casefold())
        elif self.sort_box.currentData() == "favorites":
            projects.sort(
                key=lambda meta: (
                    meta.path not in favorite_paths,
                    -self._safe_meta_mtime(meta),
                )
            )
        else:
            projects.sort(
                key=lambda meta: self._safe_meta_mtime(meta),
                reverse=True,
            )
        for meta in projects:
            tags = self._project_tags(meta.path)
            searchable = " ".join([self._safe_meta_name(meta), *tags]).lower()
            if needle and needle not in searchable:
                continue
            self._add_list_item(meta, tags=tags, favorite=meta.path in favorite_paths)
        self.empty_label.setText(
            I18N.tr("pm.no_projects", "No projects yet. Click \"New Project\" to create one.")
        )
        self.empty_label.setVisible(self.list_widget.count() == 0)
        self.empty_label.setGeometry(self.list_widget.viewport().rect())
        # Update Open button state based on selection.
        self._update_open_button()

    def _add_list_item(
        self,
        meta: ProjectMeta,
        *,
        tags: list[str] | None = None,
        favorite: bool = False,
    ) -> None:
        modified = self._format_time(self._safe_meta_mtime(meta))
        item = QListWidgetItem("")
        item.setData(Qt.ItemDataRole.UserRole, meta.path)
        item.setData(_ProjectCardDelegate.PROJECT_NAME_ROLE, self._safe_meta_name(meta))
        item.setData(_ProjectCardDelegate.PROJECT_TIME_ROLE, modified)
        item.setData(_ProjectCardDelegate.PROJECT_FAVORITE_ROLE, favorite)
        item.setData(_ProjectCardDelegate.PROJECT_TAGS_ROLE, tags or [])
        item.setSizeHint(QSize(238, 178))
        tag_hint = f"\nTags: {', '.join(tags)}" if tags else ""
        item.setToolTip(
            f"{self._safe_meta_name(meta)}{tag_hint}\n"
            f"Project file: {os.path.basename(meta.path)}\n"
            f"Location: {os.path.dirname(meta.path)}"
        )
        item.setIcon(QIcon(self._project_preview(meta)))
        self._preview_items[meta.path] = item
        if meta.preview_path:
            self._preview_paths[meta.path] = meta.preview_path
            cached = self._thumbnail_provider.get_cached_thumbnail(meta.preview_path, 640)
            if cached is None:
                self._thumbnail_provider.get_thumbnail(meta.preview_path, 640)
            else:
                item.setIcon(QIcon(self._fit_preview(cached)))
        self.list_widget.addItem(item)

    def _project_preview(self, meta: ProjectMeta) -> QPixmap:
        """Load a stored cover or return a branded placeholder.

        [AUD-40001] 解码结果按 meta.path 缓存：_apply_filter 在每个搜索
        按键、每次排序变更时整表重建并对每个匹配项目调用本方法，此前
        每次都重新开 zip、解码 JPEG、SmoothTransformation 缩放（全在
        UI 线程）。QPixmap 隐式共享，多个 QIcon 引用同一缓存无额外拷贝。
        """
        cached = self._cover_pixmap_cache.get(meta.path)
        if cached is not None:
            return cached
        cover = extract_cover_thumbnail(meta.path)
        if cover:
            pixmap = QPixmap()
            if pixmap.loadFromData(cover):
                fitted = self._fit_preview(pixmap)
                self._remember_cover(meta.path, fitted)
                return fitted
        return self._placeholder_preview()

    # Upper bound for the decoded-cover cache (~250x140 ARGB ≈ 140 KB/张，
    # 256 张 ≈ 36 MB，覆盖典型项目库规模而不失控)。
    _COVER_CACHE_MAX = 256

    def _remember_cover(self, path: str, pixmap: QPixmap) -> None:
        """[AUD-40001] 记住解码后的封面，FIFO 超限逐出最旧条目。"""
        cache = self._cover_pixmap_cache
        cache.pop(path, None)
        cache[path] = pixmap
        while len(cache) > self._COVER_CACHE_MAX:
            oldest = next(iter(cache), None)
            if oldest is None:
                break
            cache.pop(oldest, None)

    @staticmethod
    def _fit_preview(pixmap: QPixmap) -> QPixmap:
        """Fit a source pixmap into the fixed 16:9 card preview area."""
        target = QSize(250, 140)
        if pixmap.isNull():
            return ProjectManagerDialog._placeholder_preview()
        scaled = pixmap.scaled(
            target,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        canvas = QPixmap(target)
        canvas.fill(QColor("#15161a"))
        painter = QPainter(canvas)
        painter.drawPixmap(
            (target.width() - scaled.width()) // 2,
            (target.height() - scaled.height()) // 2,
            scaled,
        )
        painter.end()
        return canvas

    @staticmethod
    def _placeholder_preview() -> QPixmap:
        """Create a lightweight neutral preview without external image assets."""
        size = QSize(250, 140)
        pixmap = QPixmap(size)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        gradient = QLinearGradient(0, 0, size.width(), size.height())
        gradient.setColorAt(0.0, QColor("#343740"))
        gradient.setColorAt(1.0, QColor("#17181d"))
        painter.fillRect(pixmap.rect(), QBrush(gradient))
        painter.setPen(QPen(QColor("#e56b2f"), 2))
        painter.drawRoundedRect(94, 43, 62, 42, 6, 6)
        painter.drawLine(105, 43, 113, 34)
        painter.drawLine(113, 34, 137, 34)
        painter.drawLine(137, 34, 145, 43)
        painter.setBrush(QColor("#e56b2f"))
        painter.drawEllipse(121, 54, 10, 10)
        painter.setPen(QColor("#aeb1ba"))
        painter.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom, "TOPOS PROJECT")
        painter.end()
        return pixmap

    @staticmethod
    def _elide_name(name: str, max_length: int) -> str:
        if len(name) <= max_length:
            return name
        return name[: max_length - 1].rstrip() + "…"

    def _on_thumbnail_ready(self, source_path: str, pixmap: QPixmap) -> None:
        """Apply an asynchronously decoded media poster to matching cards."""
        if pixmap is None or pixmap.isNull():
            return
        preview = self._fit_preview(pixmap)
        for project_path, item_source in self._preview_paths.items():
            if os.path.abspath(item_source) == os.path.abspath(source_path):
                item = self._preview_items.get(project_path)
                if item is not None:
                    item.setIcon(QIcon(preview))

    @staticmethod
    def _format_time(ts: float) -> str:
        if not ts:
            return "—"
        try:
            dt = datetime.datetime.fromtimestamp(ts)
            return dt.strftime("%Y-%m-%d %H:%M")
        except (OSError, ValueError):
            return "—"

    @staticmethod
    def _mode_tag(project_mode: str) -> str:
        return I18N.tr("pm.tag_video", "Video")

    # ── Interaction ────────────────────────────────────────────────────────

    def _selected_paths(self) -> list[str]:
        paths: list[str] = []
        for item in self.list_widget.selectedItems():
            path = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(path, str) and path:
                paths.append(path)
        return paths

    def _selected_path(self) -> Optional[str]:
        item = self.list_widget.currentItem()
        if item is None:
            return None
        path = item.data(Qt.ItemDataRole.UserRole)
        return path if isinstance(path, str) else None

    def _update_open_button(self) -> None:
        paths = self._selected_paths()
        self.open_btn.setEnabled(len(paths) == 1)
        self.delete_btn.setEnabled(bool(paths))

    def _on_search_changed(self, text: str) -> None:
        self._apply_filter(text)

    def _on_sort_changed(self, _index: int) -> None:
        self._apply_filter(self.search_box.text())

    def _on_selection_changed(self) -> None:
        paths = self._selected_paths()
        if not paths:
            self.selection_label.setText(
                I18N.tr("pm.no_selection", "Select a project to open")
            )
            self._update_open_button()
            return
        if len(paths) > 1:
            self.selection_label.setText(
                I18N.tr("pm.selected_count", "{count} projects selected").format(
                    count=len(paths)
                )
            )
            self._update_open_button()
            return
        path = paths[0]
        meta = next((item for item in self._all_projects if item.path == path), None)
        if meta is None:
            return
        self.selection_label.setText(
            I18N.tr("pm.selected_project", "{name}  ·  {modified}").format(
                name=self._safe_meta_name(meta),
                modified=self._format_time(self._safe_meta_mtime(meta)),
            )
        )
        self._update_open_button()

    def _on_item_double_click(self, item: QListWidgetItem) -> None:
        path = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(path, str) and os.path.exists(path):
            self.project_selected.emit(path)

    def _on_open_clicked(self) -> None:
        path = self._selected_path()
        if path and os.path.exists(path):
            self.project_selected.emit(path)

    def _on_browse_clicked(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            I18N.tr("pm.open_from_disk", "Open Project"),
            str(get_project_library_dir(self._config)),
            I18N.tr("project.file_filter", "Topos Projects (*.topos);;All Files (*)"),
        )
        if path:
            register_project_library_dir(self._config, os.path.dirname(path))
            self.project_selected.emit(path)

    def _on_add_library_location(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self,
            I18N.tr("pm.add_library", "Add project library"),
            str(get_project_library_dir(self._config)),
        )
        if path:
            register_project_library_dir(self._config, path)
            self._update_location_label()
            self._refresh_project_list()

    def _on_new_project(self) -> None:
        self.new_project_requested.emit()

    def _on_context_menu(self, pos) -> None:
        item = self.list_widget.itemAt(pos)
        if item is None:
            return
        path = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(path, str):
            return
        if item not in self.list_widget.selectedItems():
            self.list_widget.clearSelection()
            item.setSelected(True)
        selected_paths = self._selected_paths()
        menu = QMenu(self)
        act_open = menu.addAction(I18N.tr("pm.open", "Open"))
        act_reveal = menu.addAction(I18N.tr("pm.reveal_in_finder", "Reveal in File Manager"))
        act_rename = None
        act_duplicate = None
        act_favorite = None
        act_tags = None
        backup_actions: dict[object, str] = {}
        if len(selected_paths) == 1:
            act_rename = menu.addAction(I18N.tr("pm.rename", "Rename"))
            act_duplicate = menu.addAction(I18N.tr("pm.duplicate", "Duplicate"))
            act_favorite = menu.addAction(
                I18N.tr("pm.unfavorite", "Remove from Favorites")
                if os.path.abspath(path) in self._favorite_paths()
                else I18N.tr("pm.favorite", "Add to Favorites")
            )
            act_tags = menu.addAction(I18N.tr("pm.edit_tags", "Edit Tags..."))
            backup_menu = menu.addMenu(
                I18N.tr("pm.show_backups", "Show Backup Projects")
            )
            backups = list_project_backups(path, self._config)
            if backups:
                for backup in backups:
                    action = backup_menu.addAction(
                        self._format_time(backup.created_at)
                    )
                    action.setToolTip(backup.path)
                    action.setData(backup.path)
                    backup_actions[action] = backup.path
            else:
                empty_backup = backup_menu.addAction(
                    I18N.tr("pm.no_backups", "No backups available")
                )
                empty_backup.setEnabled(False)
        menu.addSeparator()
        act_delete = menu.addAction(
            I18N.tr("pm.delete_selected", "Delete {count} Projects").format(count=len(selected_paths))
            if len(selected_paths) > 1 else I18N.tr("pm.delete", "Delete")
        )
        chosen = menu.exec(self.list_widget.mapToGlobal(pos))
        if chosen == act_open:
            if len(selected_paths) == 1 and os.path.exists(path):
                register_project_library_dir(self._config, os.path.dirname(path))
                self.project_selected.emit(path)
        elif chosen == act_reveal:
            self._reveal_in_file_manager(path)
        elif act_rename is not None and chosen == act_rename:
            self._rename_project(path)
        elif act_duplicate is not None and chosen == act_duplicate:
            self._duplicate_project(path)
        elif act_favorite is not None and chosen == act_favorite:
            self._set_favorite(path, os.path.abspath(path) not in self._favorite_paths())
            self._refresh_project_list()
        elif act_tags is not None and chosen == act_tags:
            self._edit_tags(path)
        elif chosen in backup_actions:
            self._restore_backup(path, backup_actions[chosen])
        elif chosen == act_delete:
            self._delete_projects(selected_paths)

    def _restore_backup(self, project_path: str, backup_path: str) -> None:
        """Restore a selected timestamped backup and reopen the project."""
        reply = QMessageBox.question(
            self,
            I18N.tr("pm.restore_backup_title", "Restore Backup"),
            I18N.tr(
                "pm.restore_backup_confirm",
                "Restore this backup over the current project?\n\n{backup}",
            ).format(backup=os.path.basename(backup_path)),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        if not restore_project_backup(project_path, backup_path, self._config):
            QMessageBox.warning(
                self,
                I18N.tr("pm.restore_backup_failed", "Restore Failed"),
                I18N.tr("pm.restore_backup_error", "Could not restore the selected backup."),
            )
            return
        # A recovered autosave represents the pre-restore state and must not
        # immediately prompt the user to restore itself again.
        autosave_path = f"{project_path}.autosave"
        try:
            if os.path.exists(autosave_path):
                os.remove(autosave_path)
        except OSError:
            logger.debug("Could not remove stale autosave %s", autosave_path)
        self._refresh_project_list()
        self.project_selected.emit(project_path)

    @staticmethod
    def _reveal_in_file_manager(path: str) -> None:
        import sys
        import subprocess
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", "-R", path])
            elif sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", path])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(path)])
        except OSError as exc:
            logger.warning("Reveal in file manager failed: %s", exc)

    def _rename_project(self, path: str) -> None:
        current_name = os.path.splitext(os.path.basename(path))[0]
        name, accepted = QInputDialog.getText(
            self,
            I18N.tr("pm.rename", "Rename Project"),
            I18N.tr("pm.project_name", "Project name:"),
            text=current_name,
        )
        if not accepted or not name.strip() or name.strip() == current_name:
            return
        try:
            new_path = rename_project(path, name.strip(), self._config)
            register_project_library_dir(self._config, os.path.dirname(new_path))
            was_favorite = os.path.abspath(path) in self._favorite_paths()
            if was_favorite:
                self._set_favorite(path, False)
                self._set_favorite(new_path, True)
            old_tags = self._project_tags(path)
            if old_tags:
                self._set_project_tags(path, [])
                self._set_project_tags(new_path, old_tags)
            if self._active_project_path == os.path.abspath(path):
                self._active_project_path = os.path.abspath(new_path)
            self.project_renamed.emit(path, new_path)
            self._refresh_project_list()
        except Exception as exc:
            QMessageBox.warning(
                self,
                I18N.tr("pm.rename_failed", "Rename Failed"),
                str(exc),
            )

    def _duplicate_project(self, path: str) -> None:
        try:
            new_path = duplicate_project(path)
            register_project_library_dir(self._config, os.path.dirname(new_path))
            self._refresh_project_list()
            self.selection_label.setText(
                I18N.tr("pm.duplicated", "Duplicated: {name}").format(
                    name=os.path.basename(new_path)
                )
            )
        except Exception as exc:
            QMessageBox.warning(
                self,
                I18N.tr("pm.duplicate_failed", "Duplicate Failed"),
                str(exc),
            )

    def _edit_tags(self, path: str) -> None:
        current = self._project_tags(path)
        value, accepted = QInputDialog.getText(
            self,
            I18N.tr("pm.edit_tags", "Edit Tags..."),
            I18N.tr("pm.tags_hint", "Tags (comma separated):"),
            text=", ".join(current),
        )
        if accepted:
            tags = []
            for tag in value.split(","):
                clean = tag.strip()
                if clean and clean not in tags:
                    tags.append(clean)
            self._set_project_tags(path, tags)
            self._refresh_project_list()

    def _on_delete_selected(self) -> None:
        self._delete_projects(self._selected_paths())

    def _delete_projects(self, paths: list[str]) -> None:
        paths = [path for path in paths if os.path.exists(path)]
        if not paths:
            return
        active = self._active_project_path
        if active and any(os.path.abspath(path) == active for path in paths):
            QMessageBox.warning(
                self,
                I18N.tr("pm.delete_title", "Delete Project"),
                I18N.tr("pm.delete_active", "The current project cannot be deleted while it is open."),
            )
            return
        count = len(paths)
        name = os.path.basename(paths[0]) if count == 1 else f"{count} projects"
        backup_count = sum(
            len(list_project_backups(path, self._config)) for path in paths
        )
        message = I18N.tr(
            "pm.delete_confirm",
            "Delete \"{name}\"? This cannot be undone.",
        ).format(name=name)
        if backup_count:
            message += "\n\n" + I18N.tr(
                "pm.delete_backups_notice",
                "Its {count} archived backup(s) will be deleted as well.",
            ).format(count=backup_count)
        reply = QMessageBox.question(
            self,
            I18N.tr("pm.delete_title", "Delete Project"),
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        failures: list[str] = []
        for path in paths:
            try:
                delete_project(path, self._config)
            except Exception as exc:
                failures.append(f"{os.path.basename(path)}: {exc}")
        if failures:
            QMessageBox.warning(
                self,
                I18N.tr("pm.delete_failed", "Delete Failed"),
                "\n".join(failures),
            )
        try:
            recent = list(self._config.get("recent_projects") or [])
            # A partial trash failure must not make a still-existing project
            # disappear from Recent/Favorites/Tags.
            removed = {
                os.path.abspath(path) for path in paths if not os.path.exists(path)
            }
            recent = [p for p in recent if os.path.abspath(p) not in removed]
            self._config.set("recent_projects", recent, auto_save=True)
            self._config.set(
                "favorite_projects",
                [p for p in self._favorite_paths() if p not in removed],
                auto_save=True,
            )
            tags = dict(self._config.get("project_tags") or {})
            for project_path in removed:
                tags.pop(project_path, None)
            self._config.set("project_tags", tags, auto_save=True)
        except Exception as exc:
            logger.warning("Failed to prune deleted projects from config: %s", exc)
        self._refresh_project_list()

    def _on_mode_clicked(self, mode: str) -> None:
        """Switch work mode and offer to restart (same as Welcome)."""
        from PyQt6.QtWidgets import QMessageBox
        current = self._config.get("work_mode") or "video"
        if mode == current:
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
                "Work mode will switch to {mode} after restart.\n\nWould you like to restart now?",
            ).format(mode=target_label),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply == QMessageBox.StandardButton.Yes:
            # [R2-6] Save any pending config before restarting.
            try:
                self._config.save_config()
            except Exception as exc:
                logger.warning("Config save before restart failed: %s", exc)
            from src.shared.app_restart import restart_application
            restart_application()
        else:
            # Keep the running application and persisted preference aligned
            # when the user postpones the restart.
            self._config.set("work_mode", current, auto_save=True)
            for mode_value, btn in self._mode_btns.items():
                btn.setChecked(mode_value == current)
