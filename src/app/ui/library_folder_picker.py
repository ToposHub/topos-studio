"""Topos directory chooser for registering or creating a media library."""
from __future__ import annotations

import os
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import qtawesome as qta
from PyQt6 import sip
from PyQt6.QtCore import (
    QObject, QRunnable, QSize, QStandardPaths, QStorageInfo, QThreadPool, QTimer, Qt, pyqtSignal, pyqtSlot,
)
from PyQt6.QtGui import QColor, QHideEvent, QIcon, QKeyEvent, QShowEvent
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPushButton, QStyle, QVBoxLayout, QWidget,
)

from src.app.ui.native_files_locations import MountedVolume, mounted_data_volumes
from src.features.files.contracts import is_valid_folder_name
from src.features.files.services.query import child_directories
from src.features.files.transactions import CancelToken
from src.shared.i18n import I18N
from src.shared.ui.style_sheet import (
    C, DARK_THEME, ERROR_LABEL_STYLE, GLOBAL_LINE_EDIT_STYLE, PRIMARY_DIALOG_BUTTON_STYLE,
)

if TYPE_CHECKING:
    from src.app.ui.native_files_transport import NativeFilesTransport


def _tr(key: str, default: str) -> str:
    return I18N.tr(f"files.folder_picker.{key}", default)


class _Signals(QObject):
    done = pyqtSignal(object, object, object)


class _PickerJob(QRunnable):
    def __init__(self, work: Callable[[], object], request: tuple[Path, bool] | None = None) -> None:
        super().__init__()
        self.work = work
        self.request = request
        self.signals = _Signals()

    def run(self) -> None:
        try:
            result = self.work()
        except OSError as error:
            self._deliver(None, error)
        else:
            self._deliver(result, None)

    def _deliver(self, result: object, error: OSError | None) -> None:
        try:
            self.signals.done.emit(self.request, result, error)
        except RuntimeError:
            # The application can destroy the signal object during a slow OS read.
            if not sip.isdeleted(self.signals):
                raise


def _drives() -> tuple[MountedVolume, ...]:
    mounted = mounted_data_volumes()
    system = QStorageInfo.root()
    root = system.rootPath()
    if root and system.isValid() and system.isReady():
        return (MountedVolume(root, system.displayName() or root, bytes(system.device())), *mounted)
    return mounted


def _check_directory(path: Path) -> None:
    with os.scandir(path):
        pass


def _read_picker_folder(path: Path, confirm: bool, boundary: Path | None) -> list[Path] | None:
    if boundary is not None:
        if not path.is_relative_to(boundary):
            raise PermissionError(path)
        try:
            resolved = path.resolve(strict=True)
        except RuntimeError as exc:
            raise PermissionError(path) from exc
        if resolved != path:
            raise PermissionError(path)
    if confirm:
        _check_directory(path)
        return None
    folders = child_directories(path)
    return folders if boundary is None else [folder for folder in folders if not folder.is_symlink()]


class _DirectoryList(QListWidget):
    open_requested = pyqtSignal(QListWidgetItem)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and (item := self.currentItem()) is not None:
            self.open_requested.emit(item)
            event.accept()
        else:
            super().keyPressEvent(event)


class LibraryFolderPicker(QDialog):
    """Browse folders without thumbnail work or opening a catalog."""

    def __init__(self, parent: QWidget, *, title: str, initial_path: Path, home: Path,
                 transport: NativeFilesTransport | None = None, boundary: Path | None = None) -> None:
        super().__init__(parent)
        self.directory: Path | None = None
        self._home = home
        self._initial_path = initial_path
        self._boundary = boundary
        self._current: Path | None = None
        self._transport = transport
        self._creation: tuple[str, Path, CancelToken] | None = None
        if transport is not None:
            transport.folder_created.connect(self._folder_created, Qt.ConnectionType.QueuedConnection)
        self._folder_job: _PickerJob | None = None
        self._queued: tuple[Path, bool] | None = None
        self._drive_job: _PickerJob | None = None
        self._volumes: tuple[MountedVolume, ...] = ()
        self._places: dict[Path, QListWidgetItem] = {}
        self._drive_poll = QTimer(self, interval=2500)
        self._drive_poll.timeout.connect(self._load_drives)

        self.setWindowTitle(title)
        self.resize(656, 436)
        self.setMinimumSize(490, 340)
        self.setStyleSheet(DARK_THEME + GLOBAL_LINE_EDIT_STYLE + f"""
            QListWidget {{
                background: {C.BG_DARK}; border: 1px solid {C.BORDER};
                border-radius: 5px; padding: 4px; outline: 0;
            }}
            QListWidget::item {{ height: 27px; padding: 0 6px; border-radius: 4px; }}
            QListWidget::item:hover {{ background: {C.BG_HOVER}; }}
            QListWidget::item:selected {{ background: {C.BG_ACTIVE}; color: {C.TEXT_PRIMARY}; }}
            QLabel#pickerStatus {{ color: {C.TEXT_SECONDARY}; font-size: 12px; }}
            QPushButton:disabled {{ color: {C.TEXT_HINT}; }}
        """)
        self.places = _DirectoryList(self)
        self.places.setAccessibleName(_tr("places", "常用位置"))
        self.places.setFixedWidth(168)
        self.places.setIconSize(QSize(17, 17))
        self.places.setUniformItemSizes(True)
        self.places.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.places.itemClicked.connect(self._open_item)
        self.places.open_requested.connect(self._open_item)

        self.up = QPushButton(self)
        up_label = _tr("up", "上一级")
        self.up.setToolTip(up_label)
        self.up.setAccessibleName(up_label)
        self.up.setIcon(qta.icon("fa5s.arrow-up", color=C.TEXT_SECONDARY, color_disabled=C.TEXT_HINT))
        self.up.setIconSize(QSize(14, 14))
        self.up.setFixedSize(32, 32)
        self.up.setAutoDefault(False)
        self.up.clicked.connect(self._go_up)
        self.location = QLineEdit(self)
        self.location.setAccessibleName(_tr("location", "当前位置"))
        self.location.setReadOnly(True)
        self.location.setFixedHeight(self.up.height())
        self.new_folder = QPushButton(_tr("new_folder", "新建文件夹"), self)
        self.new_folder.setIcon(qta.icon("fa5s.folder-plus", color=C.TEXT_SECONDARY, color_disabled=C.TEXT_HINT))
        self.new_folder.setFixedHeight(self.up.height())
        self.new_folder.setAutoDefault(False)
        self.new_folder.clicked.connect(self._create_folder)
        self.new_folder.setVisible(boundary is None)
        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(self.up)
        top.addWidget(self.location, 1)
        top.addWidget(self.new_folder)

        self.folders = _DirectoryList(self)
        self.folders.setAccessibleName(_tr("folders", "子文件夹"))
        self.folders.setUniformItemSizes(True)
        self.folders.setIconSize(QSize(16, 16))
        self.folders.itemSelectionChanged.connect(self._update_selection)
        self.folders.itemDoubleClicked.connect(self._open_item)
        self.folders.open_requested.connect(self._open_item)
        self._folder_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon)
        self.status = QLabel(self)
        self.status.setObjectName("pickerStatus")
        self.status.setWordWrap(True)
        self.status.hide()
        contents = QVBoxLayout()
        contents.addLayout(top)
        contents.addWidget(self.folders, 1)
        contents.addWidget(self.status)

        panes = QHBoxLayout()
        panes.setSpacing(12)
        panes.addWidget(self.places)
        self.places.setVisible(boundary is None)
        panes.addLayout(contents, 1)

        selected_label = QLabel(_tr("selected", "已选文件夹"), self)
        self.selected = QLineEdit(self)
        self.selected.setReadOnly(True)
        selected_label.setBuddy(self.selected)
        actions = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        self.choose = actions.button(QDialogButtonBox.StandardButton.Ok)
        self.choose.setStyleSheet(PRIMARY_DIALOG_BUTTON_STYLE)
        self.choose.setText(_tr("choose", "选择文件夹"))
        self.choose.setDefault(True)
        actions.button(QDialogButtonBox.StandardButton.Cancel).setText(_tr("cancel", "取消"))
        actions.accepted.connect(self.accept)
        actions.rejected.connect(self.reject)
        footer = QHBoxLayout()
        footer.addWidget(selected_label)
        footer.addWidget(self.selected, 1)
        footer.addWidget(actions)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(12)
        root.addLayout(panes, 1)
        root.addLayout(footer)
        if boundary is None:
            self._set_places(())
        self._update_selection()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if self._boundary is None:
            self._load_drives()
            self._drive_poll.start()
        self._request_folder(self._current or self._initial_path)

    def hideEvent(self, event: QHideEvent) -> None:
        self._drive_poll.stop()
        self._queued = None
        super().hideEvent(event)

    def _load_drives(self) -> None:
        if self._drive_job is None:
            job = _PickerJob(_drives)
            job.signals.done.connect(self._drives_loaded)
            self._drive_job = job
            QThreadPool.globalInstance().start(job)

    @pyqtSlot(object, object, object)
    def _drives_loaded(self, _request: None, volumes: tuple[MountedVolume, ...] | None,
                       error: OSError | None) -> None:
        self._drive_job = None
        if not self.isVisible() or error is not None or volumes is None or volumes == self._volumes:
            return
        devices = {volume.path: volume.device for volume in volumes}
        removed = [Path(volume.path) for volume in self._volumes if devices.get(volume.path) != volume.device]
        self._volumes = volumes
        requested = self._queued or (self._folder_job.request if self._folder_job else None)
        lost_current = self._current is not None and any(self._current.is_relative_to(root) for root in removed)
        if lost_current:
            if self._creation is not None:
                self._creation[2].request()
            self._current = None
            self.location.clear()
            self.location.setToolTip("")
            self.folders.clear()
        self._set_places(volumes)
        if (requested is None and lost_current) or (
            requested is not None and any(requested[0].is_relative_to(root) for root in removed)
        ):
            self._request_folder(self._home)

    def _set_places(self, volumes: tuple[MountedVolume, ...]) -> None:
        self.places.clear()
        self._places.clear()
        self._add_heading(_tr("places", "常用位置"))
        home_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_DirHomeIcon)
        self._add_place(_tr("home", "个人文件夹"), self._home, home_icon)
        for location, name, icon_name in (
            (QStandardPaths.StandardLocation.DesktopLocation, _tr("desktop", "桌面"), "fa5s.desktop"),
            (QStandardPaths.StandardLocation.DownloadLocation, _tr("downloads", "下载"), "fa5s.download"),
        ):
            path = QStandardPaths.writableLocation(location)
            if path and Path(path) not in self._places:
                self._add_place(name, Path(path), qta.icon(icon_name, color=C.TEXT_SECONDARY))
        if volumes:
            self._add_heading(I18N.tr("files.native.volumes_heading", "磁盘"))
            drive_icon = qta.icon("fa5s.hdd", color=C.TEXT_SECONDARY)
            for volume in volumes:
                path = Path(volume.path)
                if path not in self._places:
                    self._add_place(volume.name, path, drive_icon)
        self._highlight_place()

    def _add_heading(self, name: str) -> None:
        item = QListWidgetItem(name, self.places)
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        font = item.font()
        font.setBold(True)
        item.setFont(font)
        item.setForeground(QColor(C.TEXT_DIM))

    def _add_place(self, name: str, path: Path, icon: QIcon) -> None:
        item = QListWidgetItem(icon, name, self.places)
        item.setData(Qt.ItemDataRole.UserRole, path)
        item.setToolTip(str(path))
        self._places[path] = item

    def _highlight_place(self) -> None:
        if self._current is None:
            return
        place = max((path for path in self._places if self._current.is_relative_to(path)),
                    key=lambda path: len(path.parts), default=None)
        self.places.setCurrentItem(self._places.get(place))

    def _open_item(self, item: QListWidgetItem) -> None:
        if (path := item.data(Qt.ItemDataRole.UserRole)) is not None:
            self._request_folder(path)

    def _go_up(self) -> None:
        if self._current is not None and self._current != self._boundary:
            self._request_folder(self._current.parent)

    def _create_folder(self) -> None:
        transport, parent = self._transport, self._current
        if transport is None or parent is None or self._folder_job is not None or self._creation is not None:
            return
        name = self._ask_folder_name()
        if not name or parent != self._current or self._folder_job is not None:
            return
        request_id = uuid4().hex
        cancel = CancelToken()
        self._creation = (request_id, parent, cancel)
        self._show_status(_tr("creating_folder", "正在新建文件夹…"))
        self._update_selection()
        transport.create_folder(request_id, str(parent), name, cancel=cancel)

    def _ask_folder_name(self) -> str:
        dialog = QDialog(self)
        dialog.setWindowTitle(_tr("new_folder", "新建文件夹"))
        name = QLineEdit(dialog)
        label = QLabel(_tr("folder_name", "文件夹名称"), dialog)
        label.setBuddy(name)
        error = QLabel(I18N.tr("files.add_library.invalid_name", "请输入有效的单级文件夹名称。"), dialog)
        error.setStyleSheet(ERROR_LABEL_STYLE)
        error.hide()
        actions = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, dialog)
        create = actions.button(QDialogButtonBox.StandardButton.Ok)
        create.setText(_tr("create", "创建"))
        create.setStyleSheet(PRIMARY_DIALOG_BUTTON_STYLE)
        create.setDefault(True)
        create.setEnabled(False)
        actions.button(QDialogButtonBox.StandardButton.Cancel).setText(_tr("cancel", "取消"))
        actions.accepted.connect(dialog.accept)
        actions.rejected.connect(dialog.reject)

        def validate(text: str) -> None:
            value = text.strip()
            valid = is_valid_folder_name(value)
            create.setEnabled(valid)
            error.setVisible(bool(value) and not valid)

        name.textChanged.connect(validate)
        layout = QVBoxLayout(dialog)
        layout.addWidget(label)
        layout.addWidget(name)
        layout.addWidget(error)
        layout.addWidget(actions)
        dialog.setMinimumWidth(300)
        name.setFocus()
        try:
            return name.text().strip() if dialog.exec() == QDialog.DialogCode.Accepted else ""
        finally:
            dialog.deleteLater()

    @pyqtSlot(str, str, str)
    def _folder_created(self, request_id: str, destination: str, error: str) -> None:
        if self._creation is None or request_id != self._creation[0]:
            return
        _, parent, _ = self._creation
        self._creation = None
        if not self.isVisible():
            return
        if self._current == parent and self._folder_job is None:
            if destination:
                self._request_folder(Path(destination))
                return
            self._show_status(error or _tr("create_failed", "无法新建文件夹。"))
        self._update_selection()

    def _request_folder(self, path: Path, confirm: bool = False) -> None:
        request = (path, confirm)
        if self._folder_job is not None:
            self._queued = None if request == self._folder_job.request else request
            return
        work = partial(_read_picker_folder, path, confirm, self._boundary)
        job = _PickerJob(work, request)
        job.signals.done.connect(self._folder_loaded)
        self._folder_job = job
        self._show_status(_tr("loading", "正在读取文件夹…"))
        self._update_selection()
        QThreadPool.globalInstance().start(job)

    @pyqtSlot(object, object, object)
    def _folder_loaded(self, request: tuple[Path, bool], folders: list[Path] | None,
                       error: OSError | None) -> None:
        self._folder_job = None
        if not self.isVisible():
            return
        if self._queued is not None:
            pending, self._queued = self._queued, None
            self._request_folder(*pending)
            return
        path, confirm = request
        if error is not None:
            if self._current is None and path == self._initial_path and path != self._home:
                self._request_folder(self._home)
                return
            if confirm:
                self.folders.clearSelection()
            self._show_status(_tr("read_failed", "无法打开文件夹「{name}」。").format(name=path.name or str(path)))
            self.status.setToolTip(str(error))
        elif confirm:
            self.directory = path
            super().accept()
            return
        elif folders is not None:
            self._current = path
            self.location.setText(str(path))
            self.location.setToolTip(str(path))
            self.folders.clear()
            for folder in folders:
                item = QListWidgetItem(self._folder_icon, folder.name, self.folders)
                item.setData(Qt.ItemDataRole.UserRole, folder)
                item.setToolTip(str(folder))
            self._show_status("" if folders else _tr("empty", "这个位置没有子文件夹。"))
        self._highlight_place()
        self._update_selection()

    def _show_status(self, text: str) -> None:
        self.status.setText(text)
        self.status.setToolTip("")
        self.status.setVisible(bool(text))

    def _selection(self) -> Path | None:
        items = self.folders.selectedItems()
        return items[0].data(Qt.ItemDataRole.UserRole) if items else self._current

    def _update_selection(self) -> None:
        folder = self._selection()
        path = str(folder) if folder is not None else ""
        self.selected.setText(path)
        self.selected.setToolTip(path)
        idle = self._folder_job is None and self._creation is None
        self.choose.setEnabled(folder is not None and idle)
        self.new_folder.setEnabled(self._transport is not None and self._current is not None and idle)
        request = self._folder_job.request if self._folder_job else None
        locked = self._creation is not None or (request is not None and request[1])
        self.folders.setEnabled(not locked)
        self.places.setEnabled(not locked)
        self.up.setEnabled(
            not locked and self._current is not None
            and self._current != self._boundary and self._current.parent != self._current
        )

    def accept(self) -> None:
        if self.choose.isEnabled() and (folder := self._selection()) is not None:
            self._request_folder(folder, True)
