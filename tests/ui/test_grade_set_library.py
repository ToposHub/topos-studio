"""Exercise both Library entry points against the shared Files catalog."""

from pathlib import Path

from PyQt6.QtCore import QEvent, QItemSelectionModel, QMimeData, QPoint, QPointF, QSize, Qt, QUrl
from PyQt6.QtGui import QColor, QDragEnterEvent, QDragMoveEvent, QDropEvent, QImage, QMouseEvent
from PyQt6.QtWidgets import QApplication, QPushButton, QWidget
from pytest import MonkeyPatch
from pytestqt.qtbot import QtBot

from src.app.controllers.grade_sets import GradeSetModel
from src.app.ui.library_panel import LibraryPanel
from src.app.ui.native_files_locations import MountedVolume, location_icon
from src.app.ui.native_files_panel import FilesEntryModel, NativeFilesPanel
from src.features.files.repository import FilesDataLocator
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
    database = location_icon("library").pixmap(16, 16).toImage()
    photo_video = location_icon("grade-set").pixmap(16, 16).toImage()
    drive = location_icon("drive").pixmap(16, 16).toImage()
    assert libraries.data(decoration).pixmap(16, 16).toImage() == database
    assert registered_library.data(decoration).pixmap(16, 16).toImage() == database
    assert grade_sets.data(decoration).pixmap(16, 16).toImage() == photo_video
    assert drives.data(decoration).pixmap(16, 16).toImage() == drive
    for button_name, icon_name in (("addMediaLibrary", "library-add"), ("newGradeSet", "grade-set-add")):
        button = library.findChild(QPushButton, button_name)
        size = QSize(18, 18)
        icon = button.icon().pixmap(size).toImage()
        assert button.iconSize() == size
        assert button.height() == 20
        assert icon == location_icon(icon_name).pixmap(size).toImage()
        assert icon.pixelColor(14, 3).name() == "#e0e0e0"
    assert panel._library_header.sizeHint().height() == 20
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
        assert panel.inspector_export_button.isHidden()
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
        index = panel.proxy.index(0, 0)
        photo_path = index.data(FilesEntryModel.ENTRY_ROLE).path
        panel.selection.select(index, flags)
        assert not panel.inspector_export_button.isHidden()
        with qtbot.waitSignal(library.photo_export_requested) as export:
            panel.inspector_export_button.click()
        assert export.args == [photo_path]
        panel.selection.select(panel.proxy.index(1, 0), flags)
        assert panel.inspector_export_button.isHidden()
        with qtbot.waitSignal(library.photo_grade_requested) as grade:
            view.doubleClicked.emit(index)
        assert grade.args == [photo_path]
        with qtbot.waitSignal(library.image_lightbox_active) as opened:
            assert panel.open_path_native(photo_path)
        assert opened.args == [True]
        point = QPoint(100, 4)
        QApplication.sendEvent(panel._preview, QMouseEvent(
            QEvent.Type.MouseMove, QPointF(point), QPointF(panel._preview.mapToGlobal(point)),
            Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
        assert panel._preview.grade_button.isVisible()
        with qtbot.waitSignal(library.photo_grade_requested) as grade:
            panel._preview.grade_button.click()
        assert grade.args == [photo_path] and not panel._preview.is_open
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


def test_home_doubles_clicks_open_lightbox_and_use_escape(qtbot, monkeypatch, tmp_path):
    from PyQt6.QtWidgets import QLineEdit
    from src.shared.playback.player_host import NativePlayerHost

    monkeypatch.setenv("TOPOS_CONFIG_PATH", str(tmp_path / "config.json"))
    locator = FilesDataLocator.for_config_root(tmp_path / "catalog")
    monkeypatch.setattr(FilesDataLocator, "default", classmethod(lambda cls: locator))
    played = []
    monkeypatch.setattr(NativePlayerHost, "play", lambda self, path: played.append(path))
    library = LibraryPanel(start_path=str(tmp_path))
    qtbot.addWidget(library)
    library.resize(1000, 620)
    library.show()
    panel = library._native_panel
    home = panel.home_path
    files = [
        {"name": "Photo.jpg", "path": f"{home}/Photo.jpg", "type": "image"},
        {"name": "Movie.mov", "path": f"{home}/Movie.mov", "type": "video"},
    ]
    panel.set_entries(home, files)
    view = panel.pages.currentWidget()
    try:
        for file in files:
            index = panel.proxy.mapFromSource(panel.model.index(panel._entry_rows[file["path"]], 0))
            panel.selection.setCurrentIndex(index, QItemSelectionModel.SelectionFlag.ClearAndSelect
                                            | QItemSelectionModel.SelectionFlag.Rows)
            position = view.itemDelegate().name_rect(view.visualRect(index)).center()
            qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=position)
            with qtbot.waitSignal(library.image_lightbox_active, check_params_cb=lambda active: active):
                qtbot.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton, pos=position)
            assert not view.findChild(QLineEdit, "filesNameEditor")
            assert panel._preview.current_path == file["path"]
            qtbot.keyClick(panel._preview, Qt.Key.Key_Space)
            assert panel._preview.is_open
            with qtbot.waitSignal(library.image_lightbox_active, check_params_cb=lambda active: not active):
                qtbot.keyClick(panel._preview, Qt.Key.Key_Escape)
            assert not panel._preview.is_open
        assert played == [files[1]["path"]]
    finally:
        library.shutdown()
