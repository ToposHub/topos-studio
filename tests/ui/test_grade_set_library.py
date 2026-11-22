"""Exercise both Library entry points against the shared Files catalog."""

from pathlib import Path

from PyQt6.QtCore import QItemSelectionModel, QMimeData, QPointF, Qt, QUrl
from PyQt6.QtGui import QColor, QDragEnterEvent, QDragMoveEvent, QDropEvent, QImage
from PyQt6.QtWidgets import QApplication, QPushButton, QStyle, QWidget
from pytest import MonkeyPatch
from pytestqt.qtbot import QtBot

from src.app.controllers.grade_sets import GradeSetModel
from src.app.ui.library_panel import LibraryPanel
from src.app.ui.native_files_locations import MountedVolume, library_icon
from src.app.ui.native_files_panel import FilesEntryModel, NativeFilesPanel
from src.features.files.repository import FilesDataLocator
from src.shared.assets.icon_manager import get_icon
from src.shared.i18n.i18n import I18N


def test_library_grade_set_sidebar_and_multi_selection(
    qtbot: QtBot, monkeypatch: MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("TOPOS_CONFIG_PATH", str(tmp_path / "config.json"))
    locator = FilesDataLocator.for_config_root(tmp_path / "catalog")
    monkeypatch.setattr(FilesDataLocator, "default", classmethod(lambda cls: locator))
    monkeypatch.setattr(NativeFilesPanel, "is_browsable_path", lambda self, path: True)
    monkeypatch.setattr("src.app.ui.library_panel.QInputDialog.getText",
                        lambda *args, **kwargs: ("Portraits", True))
    media = tmp_path / "Photos"
    media.mkdir()
    photos = [media / name for name in ("first.png", "second.png", "third.png")]
    pixels = QImage(16, 24, QImage.Format.Format_RGB32)
    pixels.fill(QColor("tomato"))
    for path in photos:
        assert pixels.save(str(path))
    unrelated = media / "notes.txt"
    unrelated.write_text("taken at dusk", encoding="utf-8")
    disk = MountedVolume(str(tmp_path / "Archive"), "Archive", b"disk")
    monkeypatch.setattr("src.app.ui.native_files_transport.mounted_data_volumes", lambda: (disk,))

    library = LibraryPanel(start_path=str(media))
    qtbot.addWidget(library)
    panel = library._native_panel
    transport = library._native_transport
    locations = panel.locations
    panel.show_libraries([{"path": str(media), "name": "Photos", "library_id": "photos"}])
    model = GradeSetModel(transport._runtime, parent=library)
    library.set_grade_sets(model)
    tree_model = locations.item_model
    libraries, grade_sets, drives = (tree_model.index(row, 0) for row in range(3))
    assert [index.data() for index in (libraries, grade_sets, drives)] == [
        I18N.tr("files.native.libraries_heading", "素材库"),
        I18N.tr("grade_sets.title", "调色集"),
        I18N.tr("files.native.volumes_heading", "磁盘"),
    ]
    registered_library = tree_model.index(0, 0, libraries)
    assert locations.library_root(registered_library)
    decoration = Qt.ItemDataRole.DecorationRole
    database = get_icon("fa5s.database").pixmap(16, 16).toImage()
    photo_video = library_icon().pixmap(16, 16).toImage()
    drive = locations.style().standardIcon(QStyle.StandardPixmap.SP_DriveHDIcon).pixmap(16, 16).toImage()
    assert libraries.data(decoration).pixmap(16, 16).toImage() == database
    assert registered_library.data(decoration).pixmap(16, 16).toImage() == database
    assert grade_sets.data(decoration).pixmap(16, 16).toImage() == photo_video
    assert drives.data(decoration).pixmap(16, 16).toImage() == drive
    assert database != photo_video
    assert len({index.data(Qt.ItemDataRole.FontRole).toString() for index in (libraries, grade_sets, drives)}) == 1
    library.resize(1200, 680)
    library.show()
    try:
        qtbot.waitUntil(lambda: panel.current_path == str(media) and panel.model.rowCount() == 4)
        model.refresh()
        qtbot.waitUntil(lambda: bool(model.collections))
        assert model.collections[0].is_default
        library.findChild(QPushButton, "newGradeSet").click()
        qtbot.waitUntil(lambda: len(model.collections) == 2)
        collection_id = model.collections[1].id
        assert model.collections[1].count == 0

        library.set_view_mode("list")
        view = panel.detail_view
        flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
        for row in range(panel.proxy.rowCount()):
            index = panel.proxy.index(row, 0)
            path = index.data(FilesEntryModel.ENTRY_ROLE).path
            if path in {str(item) for item in photos[:2]}:
                panel.selection.select(index, flags)
        selected = panel.selection.selectedRows(0)
        assert len(selected) == 2
        position = view.visualRect(selected[0]).center()
        menu = panel._build_context_menu(view, position)
        label = I18N.tr("grade_sets.add_to", "添加到调色集")
        submenu = next(action.menu() for action in menu.actions() if action.text() == label)
        next(action for action in submenu.actions() if action.text() == "Portraits").trigger()
        qtbot.waitUntil(lambda: set(model.paths) == {str(path) for path in photos[:2]})

        default, portraits = (tree_model.index(row, 0, grade_sets) for row in range(2))
        assert locations.isExpanded(grade_sets)
        assert default.data(locations.GRADE_SET_ROLE) == model.collections[0].id
        assert portraits.data(locations.GRADE_SET_ROLE) == collection_id
        assert portraits.data(decoration).pixmap(16, 16).toImage() == photo_video
        assert portraits.data(locations.PATH_ROLE) is None
        qtbot.waitUntil(lambda: tree_model.index(0, 0, drives).data() == "Archive")
        assert locations.isExpanded(drives)
        locations.collapse(grade_sets)
        assert not locations.visualRect(portraits).isValid()
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(photos[-1])), QUrl.fromLocalFile(str(unrelated)),
                      QUrl("https://example.test/remote.png")])
        actions = Qt.DropAction.MoveAction | Qt.DropAction.CopyAction
        button, mods = Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier
        group_position = locations.visualRect(grade_sets).center()
        enter = QDragEnterEvent(group_position, actions, mime, button, mods)
        QApplication.sendEvent(locations.viewport(), enter)
        assert enter.isAccepted()
        qtbot.waitUntil(lambda: locations.isExpanded(grade_sets))

        class InternalDrop(QDropEvent):
            def source(self) -> QWidget:
                return panel.icon_view

        file_transfers: list[tuple[object, ...]] = []
        panel.file_task_requested.connect(lambda *args: file_transfers.append(args))
        position = locations.visualRect(portraits).center()
        QApplication.sendEvent(locations.viewport(), QDragMoveEvent(position, actions, mime, button, mods))
        drop = InternalDrop(QPointF(position), actions, mime, button, mods)
        QApplication.sendEvent(locations.viewport(), drop)
        assert drop.isAccepted()
        assert drop.dropAction() == Qt.DropAction.CopyAction
        assert not file_transfers
        qtbot.waitUntil(lambda: set(model.paths) == {str(path) for path in photos})

        qtbot.mouseClick(locations.viewport(), button, pos=position)
        qtbot.waitUntil(lambda: panel._view_location.get("collection_id") == collection_id
                        and panel.model.rowCount() == 3)
        assert locations.currentIndex() == portraits
        locations.collapse(grade_sets)
        panel.selection.clear()
        panel.selection.select(panel.proxy.index(0, 0), flags)
        panel._remove_selection_from_collection()
        qtbot.waitUntil(lambda: model.collections[1].count == 2 and panel.model.rowCount() == 2)
        assert not locations.isExpanded(grade_sets)

        # A coalesced catalog refresh can replace members without changing count.
        removed = next(str(path) for path in photos if str(path) not in model.paths)
        replaced = model.paths[0]
        collections = transport._runtime().collections
        assert collections.remove_members(collection_id, [replaced]).ok
        assert collections.add_members(collection_id, [removed]).ok
        model.refresh()
        qtbot.waitUntil(lambda: removed in {entry.path for entry in panel.model.entries}
                        and replaced not in {entry.path for entry in panel.model.entries})
        assert locations.currentIndex() == grade_sets
        qtbot.keyClick(locations, Qt.Key.Key_Right)
        assert locations.isExpanded(grade_sets)
        locations.setCurrentIndex(default)
        qtbot.keyClick(locations, Qt.Key.Key_Return)
        qtbot.waitUntil(lambda: panel._view_location.get("collection_id") == model.collections[0].id)
        assert all(path.is_file() for path in photos)
        assert unrelated.is_file()
    finally:
        model.shutdown()
        library.shutdown()
        assert library.wait_for_shutdown(3)
