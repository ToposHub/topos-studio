"""Project library and startup dialog for video grading."""

from __future__ import annotations

import datetime
import logging
import os
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from PyQt6.QtCore import (
    QItemSelectionModel, QModelIndex, QObject, QPoint, QRect, QRectF, QRunnable, QSignalBlocker,
    QSize, QThreadPool, QTimer, Qt, pyqtSignal, pyqtSlot,
)
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QKeySequence,
    QPaintEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QResizeEvent,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QGridLayout,
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
    QSizePolicy,
    QStyle,
    QStyleOptionButton,
    QStyleOptionViewItem,
    QStylePainter,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from src.app.ui.startup_dialog import DraggableHeaderDialog, WorkModeSelector
from src.app.update_checker import CURRENT_VERSION
from src.shared.config.config_manager import ConfigManager
from src.shared.feature_flags import get_product_mode, get_work_mode
from src.shared.i18n.i18n import I18N
from src.shared.project.library import (
    ProjectBackupMeta,
    ProjectLibraryLocation,
    ProjectMeta,
    delete_project,
    duplicate_project,
    extract_cover_thumbnail,
    get_project_library_dir,
    inspect_project_media,
    list_project_backups,
    project_library_key,
    register_project_library_dir,
    remove_project_library_dir,
    rename_project,
    restore_project_backup,
    scan_project_libraries,
)
from src.shared.media.thumbnail_provider import ThumbnailProvider
from src.shared.assets.icon_manager import (
    get_icon,
    icon_close,
    icon_favorite_outline,
    icon_folder_open,
    icon_grid,
    icon_list,
    icon_new_folder,
    icon_operation_add,
    icon_refresh,
    icon_search,
)
from src.shared.ui.widgets.chevron_combo import ChevronComboBox

logger = logging.getLogger(__name__)


_PANEL = "#27272b"
_WELL = "#202024"
_CONTROL = "#303035"
_HOVER = "#393940"
_PRESSED = "#323238"
_BORDER = "#414148"
_TEXT = "#f2f2f4"
_MUTED = "#aaaab4"
_DISABLED = "#81818b"
_ACCENT = "#ff955c"
_PRIMARY = "#c84b08"
_PRIMARY_HOVER = "#d54404"
_PRIMARY_PRESSED = "#b94008"
_PRIMARY_TEXT = "#ffffff"
_SELECTED = "#39302e"
_SCROLL_THUMB = "#606067"
_SCROLL_THUMB_HOVER = "#76767e"
_BADGE_BACKGROUND = "#27272b"
_BROWSER_SETTINGS = "project_manager_browser"

_MIN_CARD_CELL_WIDTH = 150
_CARD_INSET = 6
_CARD_DETAILS_HEIGHT = 42


class _ResumeButton(QPushButton):
    """Keep the footer action readable when the project name is long."""

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(100, super().minimumSizeHint().height())

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        option = QStyleOptionButton()
        self.initStyleOption(option)
        contents = self.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, option, self)
        text_width = contents.width() - option.iconSize.width() - 4
        option.text = option.fontMetrics.elidedText(option.text, Qt.TextElideMode.ElideRight, text_width)
        painter = QStylePainter(self)
        painter.drawControl(QStyle.ControlElement.CE_PushButton, option)


class _ProjectGrid(QListWidget):
    """Keep cards evenly spaced across the available viewport."""

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self.update_card_grid()

    def update_card_grid(self) -> None:
        if self.viewMode() != QListView.ViewMode.IconMode:
            return
        # Reserve the scrollbar width so it cannot cause a change in column count.
        width = self.maximumViewportSize().width() - self.verticalScrollBar().sizeHint().width()
        columns = max(1, width // _MIN_CARD_CELL_WIDTH)
        cell_width = width // columns
        preview_width = cell_width - 2 * (_CARD_INSET + 1)
        cell_height = preview_width * 9 // 16 + 2 * _CARD_INSET + _CARD_DETAILS_HEIGHT
        size = QSize(cell_width, cell_height)
        if size != self.gridSize():
            self.setGridSize(size)


class _ProjectTask(QRunnable):
    """Run a project-library operation off the UI thread."""

    class _Signals(QObject):
        done = pyqtSignal(object, object)

    def __init__(self, operation: Callable[[], object]) -> None:
        super().__init__()
        self._operation = operation
        self.signals = self._Signals()

    def run(self) -> None:
        try:
            result = self._operation()
        except Exception as error:
            logger.exception("Project manager operation failed")
            self.signals.done.emit(None, error)
        else:
            self.signals.done.emit(result, None)


class _ProjectCardDelegate(QStyledItemDelegate):
    """Paint compact project cards with separate title and timestamp styles."""

    PROJECT_SEARCH_ROLE = Qt.ItemDataRole.UserRole + 1
    PROJECT_TIME_ROLE = Qt.ItemDataRole.UserRole + 2
    PROJECT_FAVORITE_ROLE = Qt.ItemDataRole.UserRole + 3
    PROJECT_LIBRARY_ROLE = Qt.ItemDataRole.UserRole + 4
    PROJECT_ACTIVE_ROLE = Qt.ItemDataRole.UserRole + 5
    PROJECT_LOCATION_LABEL_ROLE = Qt.ItemDataRole.UserRole + 6

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:  # noqa: N802
        if self.parent().viewMode() == QListView.ViewMode.ListMode:
            return QSize(120, 32)
        return self.parent().gridSize()

    def paint(  # noqa: N802 - Qt override
        self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex
    ) -> None:
        if self.parent().viewMode() == QListView.ViewMode.ListMode:
            self._paint_row(painter, option, index)
            return
        painter.save()
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
        )
        rect = option.rect.adjusted(_CARD_INSET, _CARD_INSET, -_CARD_INSET, -_CARD_INSET)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        focused = bool(option.state & QStyle.StateFlag.State_HasFocus)

        shape = QPainterPath()
        shape.addRoundedRect(QRectF(rect), 8, 8)
        painter.fillPath(shape, QColor(_SELECTED if selected else _CONTROL if hovered else _PANEL))

        preview_rect = QRect(
            rect.left() + 1, rect.top() + 1, rect.width() - 2,
            rect.height() - _CARD_DETAILS_HEIGHT,
        )
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        if isinstance(icon, QIcon):
            painter.save()
            painter.setClipPath(shape)
            painter.drawPixmap(preview_rect, icon.pixmap(QSize(250, 140), painter.device().devicePixelRatioF()))
            painter.restore()

        active = index.data(self.PROJECT_ACTIVE_ROLE)
        location = index.data(self.PROJECT_LOCATION_LABEL_ROLE)
        if active:
            self._paint_badge(
                painter, option.font, preview_rect.adjusted(6, 5, -6, 0), active, _ACCENT,
            )
        if location:
            badge_area = QRect(
                preview_rect.left() + 6, preview_rect.bottom() - 20, preview_rect.width() - 12, 16,
            )
            self._paint_badge(painter, option.font, badge_area, location, _TEXT)

        name = str(index.data(Qt.ItemDataRole.DisplayRole))
        modified = str(index.data(self.PROJECT_TIME_ROLE) or "—")
        favorite = bool(index.data(self.PROJECT_FAVORITE_ROLE))
        text_rect = QRect(rect.left() + 10, preview_rect.bottom() + 5, rect.width() - 20, 16)
        name_rect = text_rect.adjusted(0, 0, -18 if favorite else 0, 0)
        align = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        font = QFont(option.font)
        font.setPixelSize(12)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(QColor(_TEXT))
        painter.drawText(
            name_rect, align,
            QFontMetrics(font).elidedText(name, Qt.TextElideMode.ElideRight, name_rect.width()),
        )

        if favorite:
            painter.setPen(QColor(_ACCENT))
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, "★")

        font.setPixelSize(10)
        font.setWeight(QFont.Weight.Normal)
        painter.setFont(font)
        painter.setPen(QColor(_MUTED))
        painter.drawText(text_rect.translated(0, 15), align, modified)
        border = _ACCENT if selected or focused else _DISABLED if hovered else _BORDER
        painter.strokePath(shape, QPen(QColor(border), 1.5 if selected or focused else 1))
        painter.restore()


    @staticmethod
    def _badge_width(font: QFont, text: str) -> int:
        badge_font = QFont(font)
        badge_font.setPixelSize(10)
        badge_font.setWeight(QFont.Weight.DemiBold)
        return QFontMetrics(badge_font).horizontalAdvance(text) + 12

    @staticmethod
    def _paint_badge(
        painter: QPainter, font: QFont, area: QRect, text: str, color: str
    ) -> None:
        if area.width() <= 12:
            return
        painter.save()
        badge_font = QFont(font)
        badge_font.setPixelSize(10)
        badge_font.setWeight(QFont.Weight.DemiBold)
        metrics = QFontMetrics(badge_font)
        text = metrics.elidedText(text, Qt.TextElideMode.ElideMiddle, area.width() - 12)
        badge = QRect(area.left(), area.top(), metrics.horizontalAdvance(text) + 12, 16)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(_BADGE_BACKGROUND))
        painter.setPen(QColor(_BORDER))
        painter.drawRoundedRect(badge, 4, 4)
        painter.setFont(badge_font)
        painter.setPen(QColor(color))
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, text)
        painter.restore()

    def _paint_row(
        self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex
    ) -> None:
        painter.save()
        rect = option.rect.adjusted(2, 1, -2, -1)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        focused = bool(option.state & QStyle.StateFlag.State_HasFocus)
        if selected or hovered or focused:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setBrush(QColor(_SELECTED if selected else _CONTROL))
            painter.setPen(QPen(QColor(_ACCENT if selected or focused else _BORDER)))
            painter.drawRoundedRect(rect, 5, 5)
        library_path = index.data(self.PROJECT_LIBRARY_ROLE)
        location = index.data(self.PROJECT_LOCATION_LABEL_ROLE)
        library = location or Path(library_path).name or library_path
        active = index.data(self.PROJECT_ACTIVE_ROLE)
        favorite = bool(index.data(self.PROJECT_FAVORITE_ROLE))
        name = str(index.data(Qt.ItemDataRole.DisplayRole))
        if favorite:
            name = f"★  {name}"
        columns = (
            (name, int(rect.width() * 0.49), _TEXT, Qt.TextElideMode.ElideRight),
            (str(index.data(self.PROJECT_TIME_ROLE)), int(rect.width() * 0.22), _MUTED,
             Qt.TextElideMode.ElideRight),
            (library, int(rect.width() * 0.29), _MUTED, Qt.TextElideMode.ElideMiddle),
        )
        left = rect.left()
        for column, (text, width, color, elide_mode) in enumerate(columns):
            cell = QRect(left + 10, rect.top(), max(0, width - 20), rect.height())
            if column == 0 and active:
                badge_width = self._badge_width(option.font, active)
                if cell.width() > badge_width + 20:
                    badge = QRect(cell.right() - badge_width + 1, cell.center().y() - 8, badge_width, 16)
                    self._paint_badge(painter, option.font, badge, active, _ACCENT)
                    cell.adjust(0, 0, -badge_width - 8, 0)
            painter.setPen(QColor(color))
            painter.drawText(cell, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                             option.fontMetrics.elidedText(text, elide_mode, cell.width()))
            left += width
        painter.restore()


class _BackupHistoryDialog(QDialog):
    """Browse snapshots and restore the chosen revision on a worker thread."""

    def __init__(self, path: str, config: ConfigManager, active: bool, parent: QWidget) -> None:
        super().__init__(parent)
        self._path = path
        self._config = config
        self._can_replace = not active
        self._restoring = False
        self.restored_path: str | None = None
        self.setWindowTitle(I18N.tr("pm.backup_history", "Backup history"))
        self.resize(560, 360)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)
        title = QLabel(Path(path).stem)
        title.setObjectName("pmBackupTitle")
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        title.setToolTip(path)
        layout.addWidget(title)
        self._list = QListWidget()
        self._list.setObjectName("pmBackupList")
        self._list.currentItemChanged.connect(self._update_buttons)
        layout.addWidget(self._list, 1)
        self._status = QLabel(I18N.tr("pm.loading_backups", "Reading backups…"))
        self._status.setObjectName("pmMeta")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)
        actions = QHBoxLayout()
        self._replace = QPushButton(I18N.tr("pm.replace_original", "Replace original…"))
        self._replace.setObjectName("pmSecondary")
        self._replace.setFixedHeight(34)
        self._replace.clicked.connect(self._replace_original)
        self._replace.setEnabled(False)
        if active:
            self._replace.setToolTip(I18N.tr("pm.close_to_replace", "Close the project before replacing it."))
        actions.addWidget(self._replace)
        actions.addStretch()
        self._cancel = QPushButton(I18N.tr("pm.cancel", "Cancel"))
        self._cancel.setFixedHeight(34)
        self._cancel.clicked.connect(self.reject)
        actions.addWidget(self._cancel)
        self._copy = QPushButton(I18N.tr("pm.restore_copy", "Restore as copy"))
        self._copy.setObjectName("pmPrimary")
        self._copy.setFixedHeight(34)
        self._copy.setDefault(True)
        self._copy.setEnabled(False)
        self._copy.clicked.connect(self._restore_copy)
        actions.addWidget(self._copy)
        layout.addLayout(actions)
        self.setStyleSheet(f"""
            QLabel#pmBackupTitle {{ font-size: 17px; font-weight: 600; }}
            QListWidget#pmBackupList {{
                background: {_WELL}; border: 1px solid {_BORDER};
                border-radius: 8px; padding: 4px; outline: none;
            }}
            QListWidget#pmBackupList::item {{
                color: {_TEXT}; padding: 6px 10px; border: 1px solid transparent;
                border-radius: 4px;
            }}
            QListWidget#pmBackupList::item:hover {{ background: {_CONTROL}; }}
            QListWidget#pmBackupList::item:selected {{
                color: {_TEXT}; background: {_SELECTED}; border-color: {_ACCENT};
            }}
        """)
        worker = _ProjectTask(lambda: list_project_backups(path, config))
        worker.signals.done.connect(self._on_backups_loaded)
        QThreadPool.globalInstance().start(worker)

    @pyqtSlot(object, object)
    def _on_backups_loaded(
        self, backups: list[ProjectBackupMeta] | None, error: Exception | None
    ) -> None:
        if error:
            self._status.setText(I18N.tr("pm.backup_read_failed", "Could not read the backups."))
            return
        if not backups:
            self._status.setText(I18N.tr("pm.no_backups", "No backups available"))
            return
        for backup in backups:
            timestamp = ProjectManagerDialog._format_time(backup.created_at, seconds=True)
            item = QListWidgetItem(timestamp)
            item.setToolTip(backup.path)
            item.setData(Qt.ItemDataRole.UserRole, backup.path)
            self._list.addItem(item)
        hint = (
            I18N.tr("pm.choose_backup", "Choose a backup to restore.")
            if self._can_replace
            else I18N.tr("pm.close_to_replace", "Close the project before replacing it.")
        )
        self._status.setText(hint)
        self._list.setCurrentRow(0)

    def _update_buttons(self) -> None:
        enabled = self._list.currentItem() is not None and not self._restoring
        self._copy.setEnabled(enabled)
        self._replace.setEnabled(enabled and self._can_replace)

    def _restore_copy(self) -> None:
        item = self._list.currentItem()
        if item is None or self._restoring:
            return
        backup_path = item.data(Qt.ItemDataRole.UserRole)
        original = Path(self._path)
        name = I18N.tr("pm.restored_name", "{name} (Recovered)").format(name=original.stem)
        self._run_restore(
            lambda: duplicate_project(backup_path, name, destination_directory=original.parent)
        )

    def _replace_original(self) -> None:
        item = self._list.currentItem()
        if item is None or self._restoring or not self._can_replace:
            return
        backup_path = item.data(Qt.ItemDataRole.UserRole)
        reply = QMessageBox.question(
            self,
            I18N.tr("pm.restore_backup_title", "Restore Backup"),
            I18N.tr(
                "pm.restore_backup_confirm",
                "Restore this backup over the current project?\n\n{backup}",
            ).format(backup=Path(backup_path).name),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        original = self._path
        config = self._config

        def replace() -> str:
            if not restore_project_backup(original, backup_path, config):
                raise OSError("Project backup could not be restored")
            try:
                Path(f"{original}.autosave").unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not clear the superseded autosave for %s", original)
            return original

        self._run_restore(replace)

    def _run_restore(self, operation: Callable[[], str]) -> None:
        self._restoring = True
        self._list.setEnabled(False)
        self._cancel.setEnabled(False)
        self._update_buttons()
        self._status.setText(I18N.tr("pm.restoring_backup", "Restoring backup…"))
        worker = _ProjectTask(operation)
        worker.signals.done.connect(self._on_restored)
        QThreadPool.globalInstance().start(worker)

    @pyqtSlot(object, object)
    def _on_restored(self, path: str | None, error: Exception | None) -> None:
        self._restoring = False
        if error:
            self._status.setText(
                I18N.tr("pm.restore_backup_error", "Could not restore the selected backup.")
            )
            self._list.setEnabled(True)
            self._cancel.setEnabled(True)
            self._update_buttons()
            return
        self.restored_path = path
        self.accept()

    def reject(self) -> None:
        if not self._restoring:
            super().reject()


class ProjectManagerDialog(DraggableHeaderDialog):
    """Video-mode project manager shown at startup.

    Signals:
        project_selected(str): Emitted with the .topos path to open.
        new_project_requested(): Emitted when the user clicks "New Project".
    """

    project_selected = pyqtSignal(str)
    new_project_requested = pyqtSignal()
    project_renamed = pyqtSignal(str, str)

    def __init__(
        self,
        config_manager: ConfigManager,
        parent: QWidget | None = None,
        active_project_path: str | None = None,
    ) -> None:
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
        self._all_projects: list[ProjectMeta] = []
        self._locations: list[ProjectLibraryLocation] = []
        self._library_filter: str | None = None
        self._scan_error = False
        self._has_scan_snapshot = False
        self._media_path: str | None = None
        self._media_inflight = False
        self._media_generation = 0
        self._media_timer = QTimer(self)
        self._media_timer.setSingleShot(True)
        self._media_timer.setInterval(180)
        self._media_timer.timeout.connect(self._check_selected_media)
        self._scan_inflight = False
        self._scan_pending = False
        self._empty_preview = self._placeholder_preview()
        self._thumbnail_provider = ThumbnailProvider.instance()
        self._thumbnail_provider.thumbnail_ready.connect(self._on_thumbnail_ready)
        self._setup_ui()
        self._apply_styles()
        self._restore_browser_settings()
        self._refresh_project_list()
        self.search_box.setFocus()

    # ── UI ─────────────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        self.setWindowTitle(I18N.tr("pm.title", "Project Manager"))
        self.resize(1120, 660)
        self.setMinimumSize(840, 520)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.FramelessWindowHint)
        self.setSizeGripEnabled(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 16, 24, 20)
        layout.setSpacing(12)

        header = QGridLayout(self._header)
        header.setContentsMargins(0, 0, 0, 0)
        header.setHorizontalSpacing(12)
        header.setColumnStretch(0, 1)
        header.setColumnStretch(2, 1)
        branding = QHBoxLayout()
        branding.setSpacing(8)
        brand = QLabel(I18N.tr("app.brand", "Topos"))
        brand.setObjectName("pmBrand")
        branding.addWidget(brand)
        version = QLabel(f"v{CURRENT_VERSION}")
        version.setObjectName("pmMeta")
        branding.addWidget(version)
        branding.addStretch()
        header.addLayout(branding, 0, 0)

        self._mode_btns: dict[str, QPushButton] = {}

        if get_product_mode() == "studio":
            mode_bar = WorkModeSelector(get_work_mode(self._config))
            mode_bar.mode_requested.connect(self._on_mode_clicked)
            self._mode_btns = mode_bar.buttons
            header.addWidget(mode_bar, 0, 1)

        self.close_btn = self._button(
            "", "pmIcon", icon_close(_MUTED), hint=I18N.tr("pm.close", "Close")
        )
        self.close_btn.clicked.connect(self.reject)
        header.addWidget(self.close_btn, 0, 2, Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self._header)

        heading = QHBoxLayout()
        heading.setSpacing(12)
        title = QLabel(self.windowTitle())
        title.setObjectName("pmTitle")
        heading.addWidget(title)
        self.project_count = QLabel()
        self.project_count.setAccessibleName(I18N.tr("pm.projects", "Projects"))
        self.project_count.setObjectName("pmMeta")
        heading.addWidget(self.project_count)
        self.scan_status = QLabel()
        self.scan_status.setObjectName("pmMeta")
        self.scan_status.hide()
        heading.addWidget(self.scan_status)
        heading.addStretch()
        layout.addLayout(heading)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        self.search_box = QLineEdit()
        self.search_box.setObjectName("pmSearch")
        search_hint = I18N.tr("pm.search_placeholder", "Search projects...")
        self.search_box.setPlaceholderText(search_hint)
        self.search_box.setAccessibleName(search_hint)
        self.search_box.setFixedHeight(36)
        self.search_box.setMaximumWidth(360)
        self.search_box.addAction(icon_search(_MUTED), QLineEdit.ActionPosition.LeadingPosition)
        self.search_box.setClearButtonEnabled(True)
        self.search_box.textChanged.connect(self._apply_filter)
        self.search_box.returnPressed.connect(self._on_search_submit)
        toolbar.addWidget(self.search_box, 1)
        self.clear_filters_btn = self._button(
            I18N.tr("pm.clear_filters", "Clear filters"), "pmQuiet"
        )
        self.clear_filters_btn.clicked.connect(self._clear_filters)
        self.clear_filters_btn.hide()
        toolbar.addWidget(self.clear_filters_btn)
        toolbar.addStretch()

        self.sort_box = ChevronComboBox()
        self.sort_box.setStyleSheet("")  # The dialog styles the field while the shared view styles its popup.
        self.sort_box.setObjectName("pmSort")
        self.sort_box.setAccessibleName(I18N.tr("pm.sort_by", "Sort projects"))
        self.sort_box.addItem(I18N.tr("pm.sort_recent", "Recently modified"), "recent")
        self.sort_box.addItem(I18N.tr("pm.sort_name", "Name"), "name")
        self.sort_box.addItem(I18N.tr("pm.sort_favorites", "Favorites first"), "favorites")
        self.sort_box.setFixedHeight(36)
        self.sort_box.currentIndexChanged.connect(self._rebuild_projects)
        toolbar.addWidget(self.sort_box)

        self.favorites_only_btn = self._button(
            "", "pmIcon", icon_favorite_outline(_MUTED),
            hint=I18N.tr("pm.only_favorites", "Show favorites only"),
        )
        self.favorites_only_btn.setCheckable(True)
        self.favorites_only_btn.toggled.connect(self._on_filter_changed)
        toolbar.addWidget(self.favorites_only_btn)

        self.list_view_btn = self._button(
            "", "pmIcon", icon_list(_MUTED), hint=I18N.tr("pm.list_view", "List view"),
        )
        self.list_view_btn.setCheckable(True)
        self.list_view_btn.toggled.connect(self._on_view_changed)
        toolbar.addWidget(self.list_view_btn)

        self.browse_btn = self._button(
            I18N.tr("pm.browse", "Open File…"), "pmSecondary", icon_folder_open(_MUTED)
        )
        self.browse_btn.clicked.connect(self._on_browse_clicked)
        toolbar.addWidget(self.browse_btn)

        self.add_location_btn = self._button(
            "", "pmIcon", icon_new_folder(_MUTED),
            hint=I18N.tr("pm.add_library", "Add project library"),
        )
        self.add_location_btn.clicked.connect(self._on_add_library_location)
        toolbar.addWidget(self.add_location_btn)

        self.refresh_btn = self._button(
            "", "pmIcon", icon_refresh(_MUTED), hint=I18N.tr("pm.refresh", "Refresh projects"),
        )
        self.refresh_btn.clicked.connect(self._refresh_project_list)
        toolbar.addWidget(self.refresh_btn)
        layout.addLayout(toolbar)

        self.list_headers = QWidget()
        columns = QHBoxLayout(self.list_headers)
        columns.setContentsMargins(18, 0, 9, 0)
        columns.setSpacing(0)
        for key, name, width in (
            ("project_column", "Project", 49),
            ("modified_column", "Modified", 22),
            ("library_column", "Library", 29),
        ):
            label = QLabel(I18N.tr(f"pm.{key}", name))
            label.setObjectName("pmMeta")
            columns.addWidget(label, width)
        self.list_headers.hide()
        layout.addWidget(self.list_headers)

        self.list_widget = _ProjectGrid()
        self.list_widget.setObjectName("pmList")
        self.list_widget.setAccessibleName(I18N.tr("pm.projects", "Projects"))
        self.list_widget.setViewMode(QListView.ViewMode.IconMode)
        self.list_widget.setFlow(QListView.Flow.LeftToRight)
        self.list_widget.setWrapping(True)
        self.list_widget.setResizeMode(QListView.ResizeMode.Adjust)
        self.list_widget.setMovement(QListView.Movement.Static)
        self.list_widget.setGridSize(QSize(174, 144))
        self.list_widget.setUniformItemSizes(True)
        self.list_widget.setMouseTracking(True)
        self.list_widget.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_widget.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.list_widget.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.list_widget.setEditTriggers(QListWidget.EditTrigger.NoEditTriggers)
        self.list_widget.setItemDelegate(_ProjectCardDelegate(self.list_widget))
        self.list_widget.itemDoubleClicked.connect(self._on_item_double_click)
        self.list_widget.itemSelectionChanged.connect(self._on_selection_changed)
        self.list_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self._on_context_menu)
        layout.addWidget(self.list_widget, 1)

        empty_layout = QVBoxLayout(self.list_widget.viewport())
        empty_layout.setContentsMargins(24, 24, 24, 24)
        self.empty_label = QLabel()
        self.empty_label.setObjectName("pmEmpty")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.empty_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.empty_label.hide()
        empty_layout.addWidget(self.empty_label)

        footer = QHBoxLayout()
        footer.setSpacing(12)
        self.library_btn = self._button(
            I18N.tr("pm.libraries", "Project libraries"), "pmSecondary", icon_folder_open(_MUTED)
        )
        self.library_btn.clicked.connect(self._show_library_menu)
        footer.addWidget(self.library_btn)
        self.skip_startup_check = QCheckBox(
            I18N.tr("pm.skip_startup", "Don’t show on startup")
        )
        self.skip_startup_check.setObjectName("pmSkipStartup")
        self.skip_startup_check.setChecked(
            self._config.get("show_project_manager_on_startup") is False
        )
        self.skip_startup_check.toggled.connect(self._on_skip_startup_toggled)
        footer.addWidget(self.skip_startup_check)

        self.selection_label = QLabel()
        self.selection_label.setObjectName("pmMeta")
        self.selection_label.setTextFormat(Qt.TextFormat.PlainText)
        self.selection_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.selection_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        footer.addWidget(self.selection_label, 1)
        self.resume_btn = self._button(
            I18N.tr("pm.resume", "Continue last project"), "pmQuiet",
            get_icon("fa5s.history", color=_MUTED),
            button_type=_ResumeButton,
        )
        self.resume_btn.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.resume_btn.clicked.connect(self._on_resume_last_project)
        self._update_resume_button()
        footer.addWidget(self.resume_btn)

        self.new_btn = self._button(
            I18N.tr("pm.new_project", "New Project"), "pmPrimary",
            icon_operation_add(_PRIMARY_TEXT), height=36,
        )
        self.new_btn.clicked.connect(self._on_new_project)
        footer.addWidget(self.new_btn)
        self.open_btn = self._button(I18N.tr("pm.open", "Open"), "pmOpen", height=36)
        self.open_btn.setEnabled(False)
        self.open_btn.clicked.connect(self._on_open_clicked)
        footer.addWidget(self.open_btn)
        self.actions_btn = self._button(
            "", "pmIcon", get_icon("fa5s.ellipsis-h", color=_MUTED),
            hint=I18N.tr("pm.project_actions", "Project actions"),
        )
        self.actions_btn.setEnabled(False)
        self.actions_btn.clicked.connect(self._on_actions_clicked)
        footer.addWidget(self.actions_btn)
        layout.addLayout(footer)

        QShortcut(QKeySequence.StandardKey.Find, self, self._focus_search)
        QShortcut(QKeySequence.StandardKey.Refresh, self, self._refresh_project_list)
        for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            shortcut = QShortcut(QKeySequence(key), self.list_widget, self._on_open_clicked)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)

    def _restore_browser_settings(self) -> None:
        saved = self._config.get(_BROWSER_SETTINGS)
        if not isinstance(saved, dict):
            return
        size = saved.get("size")
        if (
            isinstance(size, (list, tuple)) and len(size) == 2
            and all(type(value) is int and value > 0 for value in size)
        ):
            screen = self.screen()
            available = screen.availableGeometry().size() if screen is not None else QSize(1120, 660)
            self.resize(
                max(self.minimumWidth(), min(size[0], available.width())),
                max(self.minimumHeight(), min(size[1], available.height())),
            )
        sort = saved.get("sort")
        if isinstance(sort, str):
            index = self.sort_box.findData(sort)
            if index >= 0:
                with QSignalBlocker(self.sort_box):
                    self.sort_box.setCurrentIndex(index)
        self.list_view_btn.setChecked(saved.get("view") == "list")

    def done(self, result: int) -> None:
        settings = {
            "view": "list" if self.list_view_btn.isChecked() else "cards",
            "sort": self.sort_box.currentData(),
            "size": [self.width(), self.height()],
        }
        if settings != self._config.get(_BROWSER_SETTINGS):
            self._config.set(_BROWSER_SETTINGS, settings, auto_save=True)
        super().done(result)

    def _clear_filters(self) -> None:
        with QSignalBlocker(self.search_box), QSignalBlocker(self.favorites_only_btn):
            self.search_box.clear()
            self.favorites_only_btn.setChecked(False)
        self._set_library_filter(None)

    @staticmethod
    def _button(
        text: str, role: str, icon: QIcon | None = None, *, height: int = 36, hint: str = "",
        button_type: type[QPushButton] = QPushButton,
    ) -> QPushButton:
        button = button_type(text)
        button.setObjectName(role)
        button.setAutoDefault(False)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFixedHeight(height)
        if icon is not None:
            button.setIcon(icon)
            button.setIconSize(QSize(14, 14))
        if hint:
            button.setToolTip(hint)
            button.setAccessibleName(hint)
        if not text:
            button.setFixedWidth(height)
        return button

    def _apply_styles(self) -> None:
        self.setStyleSheet(f"""
            ProjectManagerDialog {{
                background: {_PANEL}; border: 1px solid {_BORDER}; border-radius: 12px;
            }}
            QLabel {{ background: transparent; color: {_TEXT}; }}
            QLabel#pmBrand {{ font-size: 15px; font-weight: 700; }}
            QLabel#pmTitle {{ font-size: 26px; font-weight: 650; }}
            QLabel#pmMeta {{ color: {_MUTED}; font-size: 12px; }}
            QLabel#pmEmpty {{ color: {_MUTED}; font-size: 14px; }}
            QCheckBox#pmSkipStartup {{ color: {_MUTED}; font-size: 12px; }}
            QCheckBox#pmSkipStartup:hover, QCheckBox#pmSkipStartup:focus {{ color: {_TEXT}; }}
            QPushButton {{
                background: transparent; color: {_TEXT}; border: 1px solid transparent;
                border-radius: 6px; padding: 0 12px; font-size: 13px; font-weight: 500;
            }}
            QPushButton#pmIcon {{ padding: 0; }}
            QPushButton#pmIcon:checked {{ background: {_SELECTED}; border-color: {_ACCENT}; }}
            QPushButton#pmQuiet {{ color: {_MUTED}; }}
            QPushButton:hover {{ background: {_HOVER}; color: {_TEXT}; }}
            QPushButton:pressed {{ background: {_PRESSED}; }}
            QPushButton:focus {{ border-color: {_ACCENT}; }}
            QPushButton:disabled {{ color: {_DISABLED}; background: transparent; }}
            QPushButton#pmSecondary {{
                background: {_CONTROL}; border: 1px solid {_BORDER};
            }}
            QPushButton#pmSecondary:hover {{ background: {_HOVER}; }}
            QPushButton#pmSecondary:pressed {{ background: {_PRESSED}; }}
            QPushButton#pmSecondary:focus {{ border-color: {_ACCENT}; }}
            QPushButton#pmOpen {{ min-width: 60px; }}
            QPushButton#pmOpen:disabled {{
                background: transparent; color: {_DISABLED}; border-color: {_BORDER};
            }}
            QPushButton#pmPrimary, QPushButton#pmOpen:enabled {{
                background: {_PRIMARY}; color: {_PRIMARY_TEXT}; font-weight: 700; padding: 0 16px;
            }}
            QPushButton#pmPrimary:hover, QPushButton#pmOpen:enabled:hover {{
                background: {_PRIMARY_HOVER};
            }}
            QPushButton#pmPrimary:pressed, QPushButton#pmOpen:enabled:pressed {{
                background: {_PRIMARY_PRESSED};
            }}
            QPushButton#pmPrimary:focus, QPushButton#pmOpen:enabled:focus {{
                border-color: {_PRIMARY_TEXT};
            }}
            QLineEdit#pmSearch, QComboBox#pmSort {{
                background: {_WELL}; color: {_TEXT}; border: 1px solid {_BORDER};
                border-radius: 6px; font-size: 13px;
            }}
            QLineEdit#pmSearch {{ padding: 0 8px; placeholder-text-color: {_MUTED}; }}
            QLineEdit#pmSearch:hover, QComboBox#pmSort:hover {{ border-color: {_DISABLED}; }}
            QLineEdit#pmSearch:focus, QComboBox#pmSort:focus {{ border-color: {_ACCENT}; }}
            QComboBox#pmSort {{ padding: 0 26px 0 12px; }}
            QComboBox#pmSort::drop-down {{ border: none; width: 0; }}
            QComboBox#pmSort::down-arrow {{
                image: none; border: none; margin: 0; width: 0; height: 0;
            }}
            QListWidget#pmList {{
                background: {_WELL}; border: 1px solid {_BORDER}; border-radius: 10px;
                outline: none; padding: 6px; color: {_TEXT};
            }}
            QListWidget#pmList::item {{ background: transparent; border: none; padding: 0; }}
            QListWidget#pmList QScrollBar:vertical {{
                background: {_WELL}; border: none; width: 10px; margin: 0;
            }}
            QListWidget#pmList QScrollBar::handle:vertical {{
                background: {_SCROLL_THUMB}; border: none; border-radius: 5px;
                margin: 2px 0; min-height: 26px;
            }}
            QListWidget#pmList QScrollBar::handle:vertical:hover {{ background: {_SCROLL_THUMB_HOVER}; }}
            QListWidget#pmList QScrollBar::add-line:vertical,
            QListWidget#pmList QScrollBar::sub-line:vertical {{ height: 0; }}
            QListWidget#pmList QScrollBar::add-page:vertical,
            QListWidget#pmList QScrollBar::sub-page:vertical {{ background: transparent; }}
        """)

    def _on_skip_startup_toggled(self, checked: bool) -> None:
        self._config.set("show_project_manager_on_startup", not checked, auto_save=True)

    # ── Data ───────────────────────────────────────────────────────────────

    def _last_project_path(self) -> str | None:
        path = self._config.get("last_project")
        return path if isinstance(path, str) and os.path.isfile(path) else None

    def _update_resume_button(self) -> None:
        path = self._last_project_path()
        self.resume_btn.setVisible(path is not None)
        if path is None:
            return
        meta = next((item for item in self._all_projects if project_library_key(item.path) == project_library_key(path)), None)
        name = (self._safe_meta_name(meta) if meta else "") or Path(path).stem
        prefix = I18N.tr("pm.resume", "Continue last project")
        display_name = self.resume_btn.fontMetrics().elidedText(name, Qt.TextElideMode.ElideMiddle, 128)
        self.resume_btn.setText(f"{prefix} · {display_name}")
        self.resume_btn.setToolTip(f"{prefix} · {name}")
        self.resume_btn.setAccessibleName(f"{prefix} · {name}")

    def _on_resume_last_project(self) -> None:
        last_project = self._config.get("last_project")
        if isinstance(last_project, str) and last_project:
            self._open_project_file(last_project)

    def _refresh_project_list(self) -> None:
        """Scan in the background while the previous results stay usable."""
        if self._scan_inflight:
            self._scan_pending = True
            return
        self._scan_inflight = True
        self._scan_error = False
        self._set_scan_status(
            I18N.tr("pm.refreshing", "Refreshing…") if self._has_scan_snapshot else ""
        )
        self._apply_filter(self.search_box.text())
        config = self._config
        worker = _ProjectTask(lambda: scan_project_libraries(config))
        worker.signals.done.connect(self._on_scan_done)
        QThreadPool.globalInstance().start(worker)

    def _set_scan_status(self, text: str, *, hint: str = "") -> None:
        self.scan_status.setText(text)
        self.scan_status.setToolTip(hint)
        self.scan_status.setVisible(bool(text))

    @pyqtSlot(object, object)
    def _on_scan_done(
        self,
        result: tuple[list[ProjectMeta], list[ProjectLibraryLocation]] | None,
        error: Exception | None,
    ) -> None:
        self._scan_inflight = False
        if self._scan_pending:
            self._scan_pending = False
            self._refresh_project_list()
            return
        if error is not None or result is None:
            self._scan_error = True
            self._set_scan_status(
                I18N.tr("pm.refresh_failed", "Refresh failed") if self._has_scan_snapshot else "",
                hint=I18N.tr("pm.refresh_failed_cached", "Refresh failed · Showing previous results"),
            )
            self._apply_filter(self.search_box.text())
            return

        self._has_scan_snapshot = True
        self._set_scan_status("")
        self._all_projects, self._locations = result
        if self._library_filter and not any(item.path == self._library_filter for item in self._locations):
            self._library_filter = None
        self._update_library_button()
        self._update_resume_button()
        scroll_bar = self.list_widget.verticalScrollBar()
        scroll_position = scroll_bar.value()
        self._cover_pixmap_cache.clear()
        self._media_path = None
        self._rebuild_projects()
        self.list_widget.doItemsLayout()
        scroll_bar.setValue(scroll_position)

    @staticmethod
    def _library_name(path: str) -> str:
        return Path(path).name or path

    def _update_library_button(self) -> None:
        label = I18N.tr("pm.libraries", "Project libraries")
        if self._library_filter:
            name = self._library_name(self._library_filter)
            name = self.library_btn.fontMetrics().elidedText(name, Qt.TextElideMode.ElideMiddle, 104)
            label = f"{label} · {name}"
        unavailable = sum(not item.available for item in self._locations)
        if unavailable:
            label += " · " + I18N.tr("pm.libraries_offline", "{count} offline").format(count=unavailable)
        self.library_btn.setText(label)
        self.library_btn.setToolTip(self._library_filter or label)

    def _show_library_menu(self) -> None:
        primary = project_library_key(get_project_library_dir(self._config, create=False))
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        all_libraries = menu.addAction(I18N.tr("pm.all_libraries", "All libraries"))
        all_libraries.setCheckable(True)
        all_libraries.setChecked(self._library_filter is None)
        all_libraries.triggered.connect(lambda: self._set_library_filter(None))
        for location in self._locations:
            path = location.path
            label = self._library_name(path)
            if path == primary:
                label += " · " + I18N.tr("pm.default_library", "Default")
            if not location.available:
                label += " · " + I18N.tr("pm.library_offline", "Offline")
            submenu = menu.addMenu(label.replace("&", "&&"))
            submenu.menuAction().setToolTip(path)
            submenu.setToolTipsVisible(True)
            location_action = submenu.addAction(path.replace("&", "&&"))
            location_action.setEnabled(False)
            show = submenu.addAction(I18N.tr("pm.filter_library", "Show this library"))
            show.setCheckable(True)
            show.setChecked(self._library_filter == path)
            show.triggered.connect(lambda _checked, value=path: self._set_library_filter(value))
            reveal = submenu.addAction(I18N.tr("pm.reveal_in_finder", "Reveal in File Manager"))
            reveal.setEnabled(location.available)
            reveal.triggered.connect(lambda _checked, value=path: self._reveal_in_file_manager(value, directory=True))
            if path != primary:
                remove = submenu.addAction(I18N.tr("pm.remove_library", "Remove from project libraries"))
                remove.triggered.connect(lambda _checked, value=path: self._remove_library(value))
        menu.addSeparator()
        menu.addAction(I18N.tr("pm.add_library", "Add project library"), self._on_add_library_location)
        menu.addAction(I18N.tr("pm.refresh", "Refresh projects"), self._refresh_project_list)
        menu.exec(self.library_btn.mapToGlobal(QPoint(0, self.library_btn.height())))
        menu.deleteLater()

    def _set_library_filter(self, path: str | None) -> None:
        self._library_filter = path
        self._update_library_button()
        self._on_filter_changed()

    def _remove_library(self, path: str) -> None:
        remove_project_library_dir(self._config, path)
        if self._library_filter == path:
            self._set_library_filter(None)
        self._refresh_project_list()

    def _on_filter_changed(self) -> None:
        self._apply_filter(self.search_box.text())

    def _on_view_changed(self, list_view: bool) -> None:
        self.list_headers.setVisible(list_view)
        view = self.list_widget
        view.setViewMode(QListView.ViewMode.ListMode if list_view else QListView.ViewMode.IconMode)
        view.setFlow(QListView.Flow.TopToBottom if list_view else QListView.Flow.LeftToRight)
        view.setWrapping(not list_view)
        view.setMovement(QListView.Movement.Static)
        if list_view:
            view.setGridSize(QSize())
        else:
            view.update_card_grid()
        hint = I18N.tr("pm.card_view", "Card view") if list_view else I18N.tr("pm.list_view", "List view")
        self.list_view_btn.setToolTip(hint)
        self.list_view_btn.setAccessibleName(hint)
        self.list_view_btn.setIcon(icon_grid(_MUTED) if list_view else icon_list(_MUTED))
        if view.currentItem() is not None:
            view.scrollToItem(view.currentItem())

    def _favorite_paths(self) -> set[str]:
        try:
            values = self._config.get("favorite_projects") or []
        except Exception:
            values = []
        return {project_library_key(str(path)) for path in values if path}

    def _project_tags(self, path: str) -> list[str]:
        try:
            mapping = self._config.get("project_tags") or {}
            tags = mapping.get(path) or mapping.get(project_library_key(path))
            if tags is None and os.name == "nt":
                tags = next(
                    (tags for key, tags in mapping.items() if project_library_key(key) == project_library_key(path)),
                    None,
                )
            tags = tags or []
        except Exception:
            tags = []
        return [str(tag).strip() for tag in tags if str(tag).strip()]

    def _set_favorite(self, path: str, favorite: bool) -> None:
        values = list(self._favorite_paths())
        normalized = project_library_key(path)
        values = [value for value in values if value != normalized]
        if favorite:
            values.insert(0, normalized)
        self._config.set("favorite_projects", values, auto_save=True)

    def _set_project_tags(self, path: str, tags: list[str]) -> None:
        try:
            mapping = dict(self._config.get("project_tags") or {})
        except Exception:
            mapping = {}
        normalized = project_library_key(path)
        mapping = {key: value for key, value in mapping.items() if project_library_key(key) != normalized}
        if tags:
            mapping[normalized] = tags
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

    @pyqtSlot()
    def _rebuild_projects(self) -> None:
        selection = set(self._selected_paths())
        current = self.list_widget.currentItem()
        current_path = current.data(Qt.ItemDataRole.UserRole) if current is not None else None
        projects = list(self._all_projects)
        favorite_paths = self._favorite_paths()
        sort = self.sort_box.currentData()
        if sort == "name":
            projects.sort(key=lambda meta: self._safe_meta_name(meta).casefold())
        elif sort == "favorites":
            projects.sort(
                key=lambda meta: (project_library_key(meta.path) not in favorite_paths, -self._safe_meta_mtime(meta))
            )
        else:
            projects.sort(key=self._safe_meta_mtime, reverse=True)

        names = [self._display_meta_name(meta) for meta in projects]
        locations = self._duplicate_location_labels(projects, names)
        with QSignalBlocker(self.list_widget):
            self.list_widget.clear()
            self._preview_items.clear()
            self._preview_paths.clear()
            for meta, name in zip(projects, names):
                self._add_list_item(
                    meta, name, favorite=project_library_key(meta.path) in favorite_paths,
                    location_hint=locations.get(meta.path, ""),
                )
            self._apply_filter(self.search_box.text(), update_selection=False)
            item = self._preview_items.get(current_path)
            if item is not None and not item.isHidden():
                self.list_widget.setCurrentItem(item, QItemSelectionModel.SelectionFlag.NoUpdate)
            for path in selection:
                item = self._preview_items.get(path)
                if item is not None and not item.isHidden():
                    item.setSelected(True)
        self._on_selection_changed()

    def _apply_filter(self, text: str, *, update_selection: bool = True) -> None:
        needle = text.strip().casefold()
        favorites_only = self.favorites_only_btn.isChecked()
        has_filters = bool(needle or favorites_only or self._library_filter)
        self.clear_filters_btn.setVisible(has_filters)
        delegate = _ProjectCardDelegate
        visible_count = 0
        with QSignalBlocker(self.list_widget):
            for item in self._preview_items.values():
                matches = (
                    needle in item.data(delegate.PROJECT_SEARCH_ROLE)
                    and (not favorites_only or item.data(delegate.PROJECT_FAVORITE_ROLE))
                    and (self._library_filter is None or self._library_filter == item.data(delegate.PROJECT_LIBRARY_ROLE))
                )
                item.setHidden(not matches)
                if matches:
                    visible_count += 1
                elif item.isSelected():
                    item.setSelected(False)

        if self._scan_inflight and not self._has_scan_snapshot:
            message = I18N.tr("pm.scanning", "Scanning projects…")
        elif self._scan_error:
            message = I18N.tr("pm.scan_failed", "Could not read the project libraries. Refresh to retry.")
        elif any(not item.available and item.path == self._library_filter for item in self._locations):
            message = I18N.tr("pm.offline_empty", "This project library is offline. Reconnect it and refresh.")
        elif not self._all_projects and self._locations and all(not item.available for item in self._locations):
            message = I18N.tr("pm.libraries_unavailable", "Project libraries are offline. Reconnect them and refresh.")
        elif has_filters:
            message = I18N.tr("pm.no_filtered_projects", "No projects match the current filters.")
        else:
            message = I18N.tr("pm.no_projects", 'No projects yet. Click "New Project" to create one.')
        self.empty_label.setText(message if visible_count == 0 else "")
        self.empty_label.setVisible(visible_count == 0)
        if not self._has_scan_snapshot:
            count = ""
        elif visible_count == 1:
            count = I18N.tr("pm.project_count_one", "1 project")
        else:
            count = I18N.tr("pm.project_count", "{count} projects").format(count=visible_count)
        self.project_count.setText(count)
        if update_selection:
            self._on_selection_changed()

    def _display_meta_name(self, meta: ProjectMeta) -> str:
        return self._safe_meta_name(meta) or I18N.tr("pm.untitled", "Untitled Project")

    def _duplicate_location_labels(
        self, projects: list[ProjectMeta], names: list[str]
    ) -> dict[str, str]:
        groups: dict[str, list[Path]] = {}
        for meta, name in zip(projects, names):
            groups.setdefault(name.casefold(), []).append(Path(meta.path))
        labels: dict[str, str] = {}
        for paths in groups.values():
            if len(paths) < 2:
                continue
            libraries: dict[str, set[Path]] = {}
            for path in paths:
                libraries.setdefault(path.parent.name.casefold(), set()).add(path.parent)
            location_labels: dict[Path, str] = {}
            for same_named_libraries in libraries.values():
                try:
                    common = Path(os.path.commonpath(same_named_libraries))
                except ValueError:
                    common = None  # Windows libraries may live on different drives.
                for library in same_named_libraries:
                    suffix = library.relative_to(common) if common is not None else library
                    location_labels[library] = str(suffix) if suffix.parts else self._library_name(str(library))
            in_library = Counter(path.parent for path in paths)
            for path in paths:
                library = path.parent
                label = location_labels[library]
                if in_library[library] > 1:
                    label = f"{label} · {path.name}"
                labels[str(path)] = label
        return labels

    def _add_list_item(
        self, meta: ProjectMeta, name: str, *, favorite: bool, location_hint: str
    ) -> None:
        modified = self._format_time(self._safe_meta_mtime(meta))
        tags = self._project_tags(meta.path)
        item = QListWidgetItem(name)
        item.setData(Qt.ItemDataRole.UserRole, meta.path)
        library = project_library_key(Path(meta.path).parent)
        item.setData(_ProjectCardDelegate.PROJECT_TIME_ROLE, modified)
        item.setData(_ProjectCardDelegate.PROJECT_LIBRARY_ROLE, library)
        item.setData(_ProjectCardDelegate.PROJECT_FAVORITE_ROLE, favorite)
        active = I18N.tr("pm.active_project", "Editing") if self._is_active_project(meta.path) else ""
        item.setData(_ProjectCardDelegate.PROJECT_ACTIVE_ROLE, active)
        item.setData(_ProjectCardDelegate.PROJECT_LOCATION_LABEL_ROLE, location_hint)
        item.setData(
            _ProjectCardDelegate.PROJECT_SEARCH_ROLE,
            " ".join([name, Path(meta.path).name, location_hint, *tags]).casefold(),
        )
        description = [name, *(part for part in (active, location_hint) if part)]
        item.setData(Qt.ItemDataRole.AccessibleTextRole, " · ".join(description))
        item.setToolTip("\n".join([*description, modified, *tags, meta.path]))
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
        """Reuse decoded covers across sorting and use a shared empty preview."""
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
        return self._empty_preview

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

    def _fit_preview(self, pixmap: QPixmap) -> QPixmap:
        """Fit a source pixmap into the fixed 16:9 card preview area."""
        target = QSize(250, 140)
        if pixmap.isNull():
            return self._empty_preview
        scaled = pixmap.scaled(
            target,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        canvas = QPixmap(target)
        canvas.fill(QColor(_WELL))
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
        """Create the shared film placeholder for projects without a cover."""
        pixmap = QPixmap(250, 140)
        pixmap.fill(QColor(_CONTROL))
        painter = QPainter(pixmap)
        icon = get_icon("fa5s.film", color=_DISABLED)
        size = 32
        icon.paint(
            painter, (pixmap.width() - size) // 2, (pixmap.height() - size) // 2, size, size
        )
        painter.end()
        return pixmap

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
    def _format_time(ts: float, *, seconds: bool = False) -> str:
        if not ts:
            return "—"
        try:
            dt = datetime.datetime.fromtimestamp(ts)
            return dt.strftime("%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M")
        except (OSError, ValueError):
            return "—"

    # ── Interaction ────────────────────────────────────────────────────────

    def _selected_paths(self) -> list[str]:
        paths: list[str] = []
        for item in self.list_widget.selectedItems():
            path = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(path, str) and path:
                paths.append(path)
        return paths

    def _focus_search(self) -> None:
        self.search_box.setFocus()
        self.search_box.selectAll()

    def _on_search_submit(self) -> None:
        if not self.search_box.text().strip():
            return
        visible = [item for item in self._preview_items.values() if not item.isHidden()]
        if len(visible) == 1:
            self._on_item_double_click(visible[0])
        elif visible:
            self.list_widget.setFocus()
            if not self.list_widget.selectedItems():
                self.list_widget.setCurrentItem(visible[0])

    def _on_selection_changed(self) -> None:
        items = self.list_widget.selectedItems()
        self.open_btn.setEnabled(len(items) == 1)
        self.actions_btn.setEnabled(bool(items))
        path = items[0].data(Qt.ItemDataRole.UserRole) if len(items) == 1 else None
        if self._media_path == path and path is not None:
            return
        self._media_path = path
        self._media_generation += 1
        self._media_timer.stop()
        if path is not None:
            self._update_selected_details(I18N.tr("pm.checking_media", "Checking media…"))
            if not self._media_inflight:
                self._media_timer.start()
        else:
            label = ""
            if items:
                label = I18N.tr("pm.selected_count", "{count} projects selected").format(count=len(items))
            self.selection_label.setText(label)
            self.selection_label.setToolTip(label)

    def _update_selected_details(self, media: str, missing: tuple[str, ...] = ()) -> None:
        if self._media_path is None:
            return
        path = self._media_path
        library = str(Path(path).parent)
        name = self._preview_items[path].text()
        label = f"{name}  ·  {self._library_name(library)}  ·  {media}"
        self.selection_label.setText(label)
        self.selection_label.setToolTip("\n".join([name, library, media, *missing[:12]]))

    def _check_selected_media(self) -> None:
        path = self._media_path
        if path is None:
            return
        self._media_inflight = True
        worker = _ProjectTask(lambda: inspect_project_media(path))
        self._media_request = self._media_generation
        worker.signals.done.connect(self._on_media_checked)
        QThreadPool.globalInstance().start(worker)

    @pyqtSlot(object, object)
    def _on_media_checked(
        self, result: tuple[int, tuple[str, ...]] | None, error: Exception | None,
    ) -> None:
        self._media_inflight = False
        if self._media_request != self._media_generation:
            if self._media_path is not None:
                self._media_timer.start()
            return
        if result is None or error:
            self._update_selected_details(I18N.tr("pm.media_check_failed", "Media check unavailable"))
            return
        total, missing = result
        if missing:
            status = I18N.tr("pm.missing_media_count", "{count} missing").format(count=len(missing))
        elif total:
            status = I18N.tr("pm.media_available", "Media available")
        else:
            status = I18N.tr("pm.no_media", "No media")
        self._update_selected_details(status, missing)

    def _on_item_double_click(self, item: QListWidgetItem) -> None:
        path = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(path, str):
            self._open_project_file(path)

    def _open_project_file(self, path: str, *, register: bool = False) -> None:
        if not os.path.isfile(path):
            last_project = self._config.get("last_project")
            if isinstance(last_project, str) and last_project and (
                project_library_key(last_project) == project_library_key(path)
            ):
                self.resume_btn.hide()
            self._show_missing_project(path)
            return
        if register:
            register_project_library_dir(self._config, os.path.dirname(path))
        self.project_selected.emit(path)

    def _show_missing_project(self, path: str) -> None:
        message = QMessageBox(
            QMessageBox.Icon.Warning,
            I18N.tr("pm.missing_project_title", "Project file unavailable"),
            I18N.tr(
                "pm.missing_project_message",
                "This project may have been moved, or its drive may be disconnected.",
            ),
            QMessageBox.StandardButton.Cancel,
            self,
        )
        message.setTextFormat(Qt.TextFormat.PlainText)
        message.setInformativeText(path)
        rescan = message.addButton(
            I18N.tr("pm.rescan", "Rescan libraries"), QMessageBox.ButtonRole.ActionRole
        )
        locate = message.addButton(
            I18N.tr("pm.locate_project", "Locate project…"), QMessageBox.ButtonRole.ActionRole
        )
        message.setDefaultButton(rescan)
        message.exec()
        choice = message.clickedButton()
        message.deleteLater()
        if choice == rescan:
            self._refresh_project_list()
        elif choice == locate:
            directory = os.path.dirname(path)
            if not os.path.isdir(directory):
                directory = str(get_project_library_dir(self._config, create=False))
            self._choose_project_file(directory)

    def _on_open_clicked(self) -> None:
        items = self.list_widget.selectedItems()
        if len(items) == 1:
            self._on_item_double_click(items[0])

    def _on_browse_clicked(self) -> None:
        directory = str(get_project_library_dir(self._config, create=False))
        self._choose_project_file(directory)

    def _choose_project_file(self, directory: str) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            I18N.tr("pm.open_from_disk", "Open Project"),
            directory,
            I18N.tr("project.file_filter", "Topos Projects (*.topos);;All Files (*)"),
        )
        if path:
            self._open_project_file(path, register=True)

    def _on_add_library_location(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self,
            I18N.tr("pm.add_library", "Add project library"),
            str(get_project_library_dir(self._config, create=False)),
        )
        if path:
            register_project_library_dir(self._config, path, explicit=True)
            self._library_filter = project_library_key(path)
            self._refresh_project_list()

    def _on_new_project(self) -> None:
        self.new_project_requested.emit()

    def _on_context_menu(self, pos: QPoint) -> None:
        item = self.list_widget.itemAt(pos)
        if item is None:
            return
        if not item.isSelected():
            self.list_widget.clearSelection()
            self.list_widget.setCurrentItem(item)
        self._show_project_menu(item, self.list_widget.viewport().mapToGlobal(pos))

    def _on_actions_clicked(self) -> None:
        items = self.list_widget.selectedItems()
        if items:
            anchor = self.actions_btn.mapToGlobal(QPoint(0, self.actions_btn.height()))
            self._show_project_menu(items[0], anchor)

    def _show_project_menu(self, item: QListWidgetItem, anchor: QPoint) -> None:
        path = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(path, str):
            return
        selected_paths = self._selected_paths()
        menu = QMenu(self)
        act_open = menu.addAction(I18N.tr("pm.open", "Open"))
        act_reveal = menu.addAction(I18N.tr("pm.reveal_in_finder", "Reveal in File Manager"))
        act_open.setEnabled(len(selected_paths) == 1)
        act_reveal.setEnabled(len(selected_paths) == 1)
        act_rename = None
        act_duplicate = None
        act_favorite = None
        act_tags = None
        act_backups = None
        act_package = None
        if len(selected_paths) == 1:
            act_rename = menu.addAction(I18N.tr("pm.rename", "Rename"))
            act_duplicate = menu.addAction(I18N.tr("pm.duplicate", "Duplicate"))
            act_favorite = menu.addAction(
                I18N.tr("pm.unfavorite", "Remove from Favorites")
                if project_library_key(path) in self._favorite_paths()
                else I18N.tr("pm.favorite", "Add to Favorites")
            )
            act_tags = menu.addAction(I18N.tr("pm.edit_tags", "Edit Tags..."))
            act_backups = menu.addAction(I18N.tr("pm.backup_history", "Backup history"))
            act_package = menu.addAction(I18N.tr("pm.package_action", "Package project…"))
        menu.addSeparator()
        act_delete = menu.addAction(
            I18N.tr("pm.delete_selected", "Delete {count} Projects").format(count=len(selected_paths))
            if len(selected_paths) > 1 else I18N.tr("pm.delete", "Delete")
        )
        act_delete.setEnabled(not any(self._is_active_project(p) for p in selected_paths))
        chosen = menu.exec(anchor)
        if chosen == act_open:
            if len(selected_paths) == 1:
                self._open_project_file(path, register=True)
        elif chosen == act_reveal:
            self._reveal_in_file_manager(path)
        elif act_rename is not None and chosen == act_rename:
            self._rename_project(path)
        elif act_duplicate is not None and chosen == act_duplicate:
            self._duplicate_project(path)
        elif act_favorite is not None and chosen == act_favorite:
            self._set_favorite(path, project_library_key(path) not in self._favorite_paths())
            self._refresh_project_list()
        elif act_tags is not None and chosen == act_tags:
            self._edit_tags(path)
        elif act_backups is not None and chosen == act_backups:
            self._show_backup_history(path)
        elif act_package is not None and chosen == act_package:
            self._package_project(path)
        elif chosen == act_delete:
            self._delete_projects(selected_paths)

    def _package_project(self, path: str) -> None:
        from src.app.ui.project_package_dialog import ProjectPackageDialog
        from src.features.edit.commands_facade import get_project_package_api

        api = get_project_package_api()
        if api is None:
            QMessageBox.warning(
                self,
                I18N.tr("pm.package_title", "Package Project"),
                I18N.tr("pm.package_unavailable", "Project packaging is unavailable."),
            )
            return
        dialog = ProjectPackageDialog(path, api, active=self._is_active_project(path), parent=self)
        dialog.exec()
        result = dialog.result_package
        dialog.deleteLater()
        if result is None:
            return
        message = QMessageBox(
            QMessageBox.Icon.Warning if result.missing else QMessageBox.Icon.Information,
            I18N.tr("pm.package_title", "Package Project"),
            (
                I18N.tr("pm.package_result_incomplete", "Package exported with {count} missing items.")
                .format(count=len(result.missing))
                if result.missing else I18N.tr("pm.package_result_ok", "Package exported.")
            ),
            QMessageBox.StandardButton.Close,
            self,
        )
        message.setTextFormat(Qt.TextFormat.PlainText)
        message.setInformativeText(result.path)
        if result.missing:
            message.setDetailedText("\n".join(result.missing))
        reveal = message.addButton(
            I18N.tr("pm.reveal_in_finder", "Reveal in File Manager"),
            QMessageBox.ButtonRole.ActionRole,
        )
        message.exec()
        if message.clickedButton() == reveal:
            self._reveal_in_file_manager(result.path, directory=Path(result.path).is_dir())
        message.deleteLater()

    def _is_active_project(self, path: str) -> bool:
        active = self._active_project_path
        return active is not None and project_library_key(active) == project_library_key(path)

    def _show_backup_history(self, path: str) -> None:
        dialog = _BackupHistoryDialog(path, self._config, self._is_active_project(path), self)
        dialog.exec()
        restored = dialog.restored_path
        dialog.deleteLater()
        if restored:
            self._refresh_project_list()
            self.project_selected.emit(restored)

    @staticmethod
    def _reveal_in_file_manager(path: str, *, directory: bool = False) -> None:
        import sys
        import subprocess
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", path] if directory else ["open", "-R", path])
            elif sys.platform == "win32":
                subprocess.Popen(["explorer", path] if directory else ["explorer", "/select,", path])
            else:
                subprocess.Popen(["xdg-open", path if directory else os.path.dirname(path)])
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
            was_favorite = project_library_key(path) in self._favorite_paths()
            if was_favorite:
                self._set_favorite(path, False)
                self._set_favorite(new_path, True)
            old_tags = self._project_tags(path)
            if old_tags:
                self._set_project_tags(path, [])
                self._set_project_tags(new_path, old_tags)
            if self._is_active_project(path):
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

    def _delete_projects(self, paths: list[str]) -> None:
        paths = [path for path in paths if os.path.exists(path)]
        if not paths:
            return
        if any(self._is_active_project(path) for path in paths):
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
        current = get_work_mode(self._config)
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
