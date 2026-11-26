"""Registered sources stay outside the project until a timeline or pool action."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
import wave

import pytest
from PyQt6.QtCore import QEvent, QPoint, QPointF, QSettings, Qt
from PyQt6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent, QImage, QMouseEvent
from PyQt6.QtWidgets import QApplication, QListView, QListWidgetItem
from pytestqt.qtbot import QtBot

from src.app.services.library_source_preview import prepare_library_source
from src.app.ui.media_pool_library_browser import LibraryMediaList, MediaPoolLibraryBrowser
from src.app.ui.native_files_transport import NativeFilesTransport
from src.features.edit.core.project import EditProject
from src.features.edit.core.timeline_v2 import EditTrack, Rational, TimelineSequence
from src.features.edit.ui.media_pool_panel import AssetListModel, GridItem, MediaPoolPanel, ViewMode
from src.features.edit.ui.source_viewer import SourceViewerWidget
from src.features.edit.ui.timeline_view import TimelineView
from src.features.files.repository.catalog import FilesCatalog, LibraryRecord
from src.features.files.repository.locator import FilesDataLocator
from src.features.files.services.ai_library import MediaEvidence
from src.features.files.services.facade import FilesFacade
from src.features.files.transactions import CancelToken


@pytest.fixture
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[FilesFacade, LibraryRecord, Path]]:
    monkeypatch.setattr("src.app.services.ai_library_search.active_library_embedding_route", lambda: None)
    root = tmp_path / "library"
    folder = root / "recordings"
    folder.mkdir(parents=True)
    source = folder / "bell.wav"
    with wave.open(str(source), "wb") as audio:
        audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\x00\x00" * 32000)
    catalog = FilesCatalog(FilesDataLocator.for_config_root(tmp_path))
    facade = FilesFacade(catalog)
    try:
        record = facade.libraries.register(root, "Test library")
        facade.build_ai_library(
            lambda path, kind: [MediaEvidence("Coffee shop bell", 1.0, 2.0, ("bell",))],
            revision="fixture", cancel=CancelToken(), progress=lambda done, total: None,
        )
        yield facade, record, source
    finally:
        facade.shutdown(wait=True)
        catalog.close()


def test_library_defaults_expanded_and_collapsed_sections_reclaim_space(
    library: tuple[FilesFacade, LibraryRecord, Path], qtbot: QtBot,
) -> None:
    facade, record, _ = library
    pool = MediaPoolPanel()
    qtbot.addWidget(pool)
    project = EditProject.create("edit")
    pool.set_project(project)
    transport = NativeFilesTransport(lambda: "", None, facade=facade)
    browser = MediaPoolLibraryBrowser(pool, transport=transport)
    pool.install_library_view(browser.tree, browser, browser.set_presentation)
    pool.resize(850, 500)
    pool.show()
    with qtbot.waitSignal(transport.libraries_loaded):
        browser.refresh_registry()

    smart = pool.smart_bin_tree.topLevelItem(1)
    smart_height = pool.smart_bin_tree.visualItemRect(smart).height()
    library_height = browser.tree.visualItemRect(browser.heading).height()
    assert tuple(pool.sidebar.widget(index) for index in range(pool.sidebar.count())) == (
        pool.bin_tree, browser.tree, pool.smart_bin_tree,
    )
    assert not smart.isExpanded()
    assert browser.heading.isExpanded()
    assert not browser.heading.child(0).isExpanded()
    assert pool.sidebar.sizes()[1] > library_height
    browser.heading.setExpanded(False)
    collapsed_sizes = pool.sidebar.sizes()
    assert not browser.heading.isExpanded()
    assert collapsed_sizes[1:] == [library_height, smart_height]
    assert pool.smart_bin_tree.height() == smart_height
    assert browser.tree.height() == library_height

    qtbot.mouseClick(pool.smart_bin_tree.viewport(), Qt.MouseButton.LeftButton,
                     pos=QPoint(pool.smart_bin_tree.indentation() // 2, smart_height // 2))
    qtbot.mouseClick(browser.tree.viewport(), Qt.MouseButton.LeftButton,
                     pos=QPoint(browser.tree.indentation() // 2, library_height // 2))
    assert pool.sidebar.sizes()[1] > library_height
    assert pool.sidebar.sizes()[2] > smart_height

    pool._refresh_bin_tree()
    smart = pool.smart_bin_tree.topLevelItem(1)
    assert smart.isExpanded()
    smart_expanded_height = pool.smart_bin_tree.height()
    pool.action_show_smart_bins.setChecked(False)
    QApplication.processEvents()
    browser.heading.setExpanded(False)
    pool.action_show_smart_bins.setChecked(True)
    QApplication.processEvents()
    assert pool.smart_bin_tree.height() == smart_expanded_height

    browser.heading.setExpanded(True)
    smart.setExpanded(False)
    assert pool.sidebar.sizes()[1] > library_height
    assert pool.sidebar.sizes()[2] == smart_height

    browser.heading.setExpanded(False)
    assert pool.sidebar.sizes() == collapsed_sizes
    assert pool.smart_bin_tree.height() == smart_height
    assert browser.tree.height() == library_height

    pool._refresh_bin_tree()
    browser._set_libraries([{
        "library_id": record.library_id, "name": "Renamed library", "path": record.root,
    }])
    assert not pool.smart_bin_tree.topLevelItem(1).isExpanded()
    assert not browser.heading.isExpanded()
    assert pool.sidebar.sizes() == collapsed_sizes


@pytest.mark.parametrize(("poster_width", "viewport_width", "columns"), [(96, 394, 3), (122, 605, 4)])
def test_master_and_library_fit_the_same_columns_with_or_without_scrollbar(
    library: tuple[FilesFacade, LibraryRecord, Path], qtbot: QtBot,
    poster_width: int, viewport_width: int, columns: int,
) -> None:
    facade, _, source = library
    pool = MediaPoolPanel()
    qtbot.addWidget(pool)
    pool._thumbnail_settings = QSettings(str(source.parent / ".grid-prefs.ini"), QSettings.Format.IniFormat)
    pool.set_thumb_size(poster_width)
    project = EditProject.create("edit")
    for row in range(20):
        project = project.create_bin(f"Folder {row}", project.root_bin_id)
    pool.set_project(project)
    browser = MediaPoolLibraryBrowser(pool, transport=NativeFilesTransport(lambda: "", None, facade=facade))
    pool.install_library_view(browser.tree, browser, browser.set_presentation)
    pool.resize(630, 400)
    pool.show()

    grid = pool.grid_view
    pool.resize(pool.width() + viewport_width - grid.maximumViewportSize().width(), pool.height())
    qtbot.waitUntil(lambda: grid.maximumViewportSize().width() == viewport_width)
    bins = list(project.bins.values())
    pool.asset_model.set_items([GridItem("bin", folder.bin_id, folder) for folder in bins
                               if folder.bin_id != project.root_bin_id])
    qtbot.waitUntil(grid.verticalScrollBar().isVisible)
    master_cell = grid.gridSize()
    assert columns * master_cell.width() <= grid.viewport().width()
    assert grid.iconSize().width() == poster_width
    assert grid.visualRect(pool.asset_model.index(columns - 1)).top() == grid.visualRect(pool.asset_model.index(0)).top()
    assert grid.visualRect(pool.asset_model.index(columns)).top() > grid.visualRect(pool.asset_model.index(0)).top()

    for row in range(4):
        browser._append({"path": str(source.parent / f"Folder {row}"), "name": f"Folder {row}",
                         "kind": "dir", "online": True})
    pool.show_library_media()
    qtbot.waitUntil(lambda: browser.list.visualItemRect(browser.list.item(3)).isValid())
    assert not browser.list.verticalScrollBar().isVisible()
    assert browser.list.gridSize() == master_cell
    assert browser.list.visualItemRect(browser.list.item(columns - 1)).top() == browser.list.visualItemRect(browser.list.item(0)).top()


def test_browse_source_preview_and_drag_unindexed_material(
    library: tuple[FilesFacade, LibraryRecord, Path], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch,
) -> None:
    facade, record, source = library
    photo = source.with_name("blue.png")
    image = QImage(32, 24, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.blue)
    assert image.save(str(photo))
    catalog = facade._catalog
    catalog.upsert_asset(record.library_id, photo, width=32, height=24)
    tag_id = catalog.create_tag(record.library_id, "blue-label")
    catalog.tag_asset(record.library_id, photo, tag_id)
    pool = MediaPoolPanel()
    qtbot.addWidget(pool)
    pool._thumbnail_settings = QSettings(str(source.parent / ".qa-prefs.ini"), QSettings.Format.IniFormat)
    pool.set_thumb_size(120)
    project = EditProject.create("edit")
    pool.set_project(project)
    transport = NativeFilesTransport(lambda: "", None, facade=facade)
    browser = MediaPoolLibraryBrowser(pool, transport=transport)
    pool.install_library_view(browser.tree, browser, browser.set_presentation)
    browser.browse_requested.connect(pool.show_library_media)
    browser.project_requested.connect(pool.show_project_media)
    pool.library_search_visibility_changed.connect(browser.set_search_visible)
    pool.resize(850, 400)
    pool.show()
    try:
        with qtbot.waitSignal(transport.libraries_loaded):
            browser.refresh_registry()
        root = browser.heading.child(0)
        with qtbot.waitSignal(transport.loaded):
            browser.tree.setCurrentItem(root)
        assert browser.list.item(0).data(AssetListModel.ITEM_TYPE_ROLE) == "bin"
        pool.btn_metadata_view.click()
        assert browser.shelf.isVisible()
        assert browser.shelf.gridSize() == pool.metadata_shelf.gridSize()
        assert browser.list.item(0).isHidden()
        pool.btn_grid_view.click()
        assert not browser.shelf.isVisible()
        assert not browser.list.item(0).isHidden()
        with qtbot.waitSignal(transport.tree_loaded):
            root.setExpanded(True)
        with qtbot.waitSignal(transport.loaded):
            browser.tree.setCurrentItem(root.child(0))
        assert browser.isVisible()
        assert browser.list.count() == 2
        assert pool._project is project and not project.assets
        assert browser.search_bar.isHidden()
        assert isinstance(browser.list.itemDelegate(), type(pool.grid_delegate))
        assert browser.list.viewMode() == QListView.ViewMode.IconMode
        assert browser.list.dragEnabled() and browser.status.isHidden()
        assert browser.list.gridSize() == pool.grid_view.gridSize()
        qtbot.waitUntil(lambda: browser.list.visualItemRect(browser.list.item(1)).isValid())
        assert browser.list.visualItemRect(browser.list.item(0)).top() == browser.list.visualItemRect(browser.list.item(1)).top()
        photo_item = next(browser.list.item(row) for row in range(browser.list.count())
                          if browser.list.item(row).text() == photo.name)
        qtbot.waitUntil(lambda: photo_item.data(Qt.ItemDataRole.UserRole + 1) is not None)
        assert photo_item.data(AssetListModel.ITEM_TYPE_ROLE) == "asset"
        browser.list.setCurrentItem(photo_item)
        original_width = pool.get_thumb_size()
        try:
            pool.thumb_slider.setSliderDown(True)
            pool.set_thumb_size(208 if original_width != 208 else 180)
            qtbot.waitUntil(lambda: browser.list.iconSize().width() == pool.get_thumb_size())
            assert browser.list.gridSize() == pool.grid_view.gridSize()
        finally:
            pool.thumb_slider.setSliderDown(False)
            pool.set_thumb_size(original_width)
        pool.btn_list_view.click()
        assert pool.get_view_mode() == ViewMode.LIST
        assert browser.rows.isVisible()
        assert browser.rows.currentItem().text(0) == photo.name
        assert browser.rows.headerItem().text(5) == pool.tree.headerItem().text(5)
        pool.btn_metadata_view.click()
        assert browser.list.isVisible()
        assert browser.list.itemDelegate().metadata_mode
        assert browser.list.dragEnabled()
        assert browser.list.currentItem() is photo_item
        pool.btn_grid_view.click()
        assert browser.list.gridSize() == pool.grid_view.gridSize()
        with qtbot.waitSignal(browser.source_preview_requested) as preview:
            qtbot.mouseClick(browser.list.viewport(), Qt.MouseButton.LeftButton,
                             pos=browser.list.visualItemRect(photo_item).center())
        preview_asset, poster, focus = preview.args
        assert preview_asset.path == str(photo) and preview_asset.is_image
        assert not poster.isNull()
        assert not focus
        assert not pool._project.assets
        audio_item = next(browser.list.item(row) for row in range(browser.list.count())
                          if browser.list.item(row).text() == source.name)
        browser.list.setCurrentItem(audio_item)
        with qtbot.waitSignal(browser.source_preview_requested) as preview:
            qtbot.keyClick(browser.list, Qt.Key.Key_Return)
        assert preview.args[0].is_audio and preview.args[0].total_frames == 96
        assert preview.args[1].isNull()
        assert preview.args[2]
        pool.btn_search.click()
        assert browser.search_bar.isVisible() and browser.isVisible()
        browser.search.setText("blue-label")
        with qtbot.waitSignal(transport.ai_library_results):
            browser.search.returnPressed.emit()
        assert browser.list.count() == 1
        assert browser.status.isHidden()
        hit = browser.list.item(0).data(Qt.ItemDataRole.UserRole)
        assert hit["tags"] == ("blue-label",)
        assert browser.rows.topLevelItem(0).text(5) == "32x24"
        view = browser.list.viewport()
        drag_point = browser.list.visualItemRect(browser.list.item(0)).center()
        native_drag = MagicMock()
        monkeypatch.setattr("src.app.ui.media_pool_library_browser.QDrag", lambda _owner: native_drag)
        qtbot.mousePress(view, Qt.MouseButton.LeftButton, pos=drag_point)
        moved = drag_point + QPoint(QApplication.startDragDistance() + 8, 0)
        QApplication.sendEvent(view, QMouseEvent(
            QEvent.Type.MouseMove, QPointF(moved), QPointF(view.mapToGlobal(moved)),
            Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
        qtbot.mouseRelease(view, Qt.MouseButton.LeftButton, pos=moved)
        native_drag.exec.assert_called_once_with(Qt.DropAction.CopyAction)
        mime = native_drag.setMimeData.call_args.args[0]
        args = Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
        point = pool.bin_tree.visualItemRect(pool.bin_tree.topLevelItem(0)).center()
        enter = QDragEnterEvent(point, *args)
        move = QDragMoveEvent(point, *args)
        QApplication.sendEvent(pool.bin_tree.viewport(), enter)
        QApplication.sendEvent(pool.bin_tree.viewport(), move)
        assert enter.isAccepted() and move.isAccepted()
        assert pool._project.bins[project.root_bin_id].name == project.bins[project.root_bin_id].name
        pool._refresh_bin_tree()
        drop = QDropEvent(QPointF(point), *args)
        QApplication.sendEvent(pool.bin_tree.viewport(), drop)
        assert drop.isAccepted()
        qtbot.waitUntil(lambda: pool._project.get_asset_by_path(str(photo)) is not None)
        asset = pool._project.get_asset_by_path(str(photo))
        assert asset.path == str(photo)
        assert pool._project.bins[project.root_bin_id].asset_ids == (asset.asset_id,)
        assert len(pool._project.assets) == 1
        pool.main_splitter.setSizes([570, 280])
        pool.set_thumb_size(320)
        qtbot.waitUntil(lambda: browser.list.iconSize().width() < 320)
        library_size = browser.list.gridSize()
        browser.project_requested.emit()
        assert pool.view_container.isVisible()
        assert pool.grid_view.gridSize() == library_size
    finally:
        browser.shutdown()


def test_files_directory_warms_shared_cache_without_a_visible_request(
    library: tuple[FilesFacade, LibraryRecord, Path], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.features.files.services import media

    facade, _, source = library
    photo = source.with_name("cache-me.png")
    image = QImage(32, 24, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.blue)
    assert image.save(str(photo))
    decode = media.thumbnail_bytes
    decoded: list[str] = []

    def record_decode(path: str, **options: Any) -> bytes:
        decoded.append(path)
        return decode(path, **options)

    monkeypatch.setattr(media, "thumbnail_bytes", record_decode)
    files = NativeFilesTransport(lambda: "", None, facade=facade)
    browser = NativeFilesTransport(lambda: "", None, facade=facade)
    warmed: list[str] = []
    files.thumbnail_loaded.connect(lambda path, _raw: warmed.append(path))
    try:
        with qtbot.waitSignal(files.page_meta):
            files.load_directory(str(source.parent))
        qtbot.waitUntil(lambda: bool(decoded) and not files._thumbnail_replies)
        assert decoded == [str(photo)]
        assert not warmed
        with qtbot.waitSignal(browser.thumbnail_loaded) as cached:
            browser.request_thumbnail(str(photo))
        assert cached.args[0] == str(photo)
        assert decoded == [str(photo)]
    finally:
        files.shutdown()
        browser.shutdown()


def test_library_result_drop_keeps_excerpt_and_undoes_reference(
    library: tuple[FilesFacade, LibraryRecord, Path], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch,
) -> None:
    facade, record, source = library
    hit, = facade.search_ai_library("bell", library_id=record.library_id)
    timeline = TimelineView()
    qtbot.addWidget(timeline)
    timeline.resize(1050, 580)
    timeline.set_project(EditProject.create("edit"))
    timeline.load_timeline(TimelineSequence(
        name="Cuts", width=320, height=240, fps=Rational(25, 1),
        audio_tracks=(EditTrack("A1", "A1", "audio", 0),),
    ))
    timeline.show()
    QApplication.processEvents()
    lane = timeline._layout_engine.track_rect("A1")
    point = timeline.mapFromScene(QPointF(timeline._layout_engine.frame_to_x(50), lane.center().y()))
    action, button, modifiers = Qt.DropAction.CopyAction, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
    native_drag = MagicMock()
    monkeypatch.setattr("src.app.ui.media_pool_library_browser.QDrag", lambda _owner: native_drag)
    cards = LibraryMediaList()
    qtbot.addWidget(cards)
    item = QListWidgetItem(hit["name"], cards)
    item.setData(Qt.ItemDataRole.UserRole, hit)
    cards.setCurrentItem(item)
    cards.startDrag(action)
    native_drag.exec.assert_called_once_with(action)
    mime = native_drag.setMimeData.call_args.args[0]
    enter = QDragEnterEvent(point, action, mime, button, modifiers)
    QApplication.sendEvent(timeline.viewport(), enter)
    assert enter.isAccepted()
    drop = QDropEvent(QPointF(point), action, mime, button, modifiers)
    QApplication.sendEvent(timeline.viewport(), drop)
    assert drop.isAccepted()
    qtbot.waitUntil(lambda: len(timeline.get_timeline().audio_tracks[0].clips) == 1, timeout=5000)
    clip, = timeline.get_timeline().audio_tracks[0].clips
    asset = timeline._project.get_asset_by_path(str(source))
    assert clip.asset_id == asset.asset_id
    assert (clip.timeline_start, clip.source_start, clip.source_duration) == (50, 25, 25)
    assert asset.metadata["global_library_asset_id"] == hit["asset_id"]
    timeline.undo()
    assert not timeline.get_timeline().audio_tracks[0].clips
    assert timeline._project.get_asset_by_path(str(source)) is None
    timeline.redo()
    assert timeline._project.get_asset_by_path(str(source)).asset_id == asset.asset_id

    timeline.undo()
    with source.open("ab") as changed:
        changed.write(b"changed")
    QApplication.sendEvent(timeline.viewport(), QDragEnterEvent(point, action, mime, button, modifiers))
    stale = QDropEvent(QPointF(point), action, mime, button, modifiers)
    QApplication.sendEvent(timeline.viewport(), stale)
    assert not stale.isAccepted()
    assert not timeline._project.assets


def test_first_library_drop_keeps_the_visible_provisional_track(
    library: tuple[FilesFacade, LibraryRecord, Path], qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, source = library
    timeline = TimelineView()
    qtbot.addWidget(timeline)
    timeline.resize(1050, 580)
    timeline.set_project(EditProject.create("edit"))
    provisional = timeline.show_provisional_timeline()
    timeline.show()
    QApplication.processEvents()

    native_drag = MagicMock()
    monkeypatch.setattr("src.app.ui.media_pool_library_browser.QDrag", lambda _owner: native_drag)
    cards = LibraryMediaList()
    qtbot.addWidget(cards)
    item = QListWidgetItem(source.name, cards)
    item.setData(Qt.ItemDataRole.UserRole, {
        "path": str(source), "name": source.name, "kind": "audio", "online": True,
    })
    cards.setCurrentItem(item)
    cards.startDrag(Qt.DropAction.CopyAction)
    mime = native_drag.setMimeData.call_args.args[0]
    track_id = provisional.audio_tracks[0].track_id
    lane = timeline._layout_engine.track_rect(track_id)
    point = timeline.mapFromScene(QPointF(timeline._layout_engine.frame_to_x(25), lane.center().y()))
    args = Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
    QApplication.sendEvent(timeline.viewport(), QDragEnterEvent(point, *args))
    drop = QDropEvent(QPointF(point), *args)
    QApplication.sendEvent(timeline.viewport(), drop)
    assert drop.isAccepted()
    qtbot.waitUntil(lambda: bool(timeline.get_timeline().audio_tracks[0].clips))
    assert not timeline.has_provisional_timeline()
    assert timeline.get_timeline().audio_tracks[0].track_id == track_id


def test_library_source_replaces_cached_poster_with_full_image(tmp_path: Path, qtbot: QtBot) -> None:
    path = tmp_path / "source.png"
    source = QImage(640, 360, QImage.Format.Format_RGB32)
    source.fill(Qt.GlobalColor.blue)
    assert source.save(str(path))
    poster = source.scaled(128, 72)
    widget = SourceViewerWidget()
    qtbot.addWidget(widget)
    widget.viewer.submit_qimage = MagicMock()
    try:
        widget.open_asset(prepare_library_source(str(path)), preview_only=True, initial_preview=poster)
        assert widget.viewer.submit_qimage.call_args.args[0].size() == poster.size()
        assert widget.source_drag_payload_for_intent("video")[0] is None
        qtbot.waitUntil(lambda: widget.viewer.submit_qimage.call_args.args[0].size() == source.size())
    finally:
        widget.prepare_for_shutdown()


def test_library_browser_keeps_indexed_media_visible_when_drive_disconnects(
    library: tuple[FilesFacade, LibraryRecord, Path], qtbot: QtBot,
) -> None:
    facade, record, source = library
    transport = NativeFilesTransport(lambda: "", None, facade=facade)
    browser = MediaPoolLibraryBrowser(transport=transport)
    qtbot.addWidget(browser)
    try:
        with qtbot.waitSignal(transport.libraries_loaded):
            browser.refresh_registry()
        root_item = browser.heading.child(0)
        browser._expand(root_item)
        qtbot.waitUntil(lambda: root_item.childCount() == 1)
        folder = source.parent
        with qtbot.waitSignal(transport.loaded):
            browser.tree.setCurrentItem(root_item.child(0))
        root = Path(record.root)
        unplugged = root.with_name("unplugged")
        root.rename(unplugged)
        with qtbot.waitSignal(transport.ai_library_results):
            browser._refresh()
        assert root_item.childCount() == 1
        assert browser.list.count() == 1
        browser.list.setCurrentRow(0)
        assert browser.list.item(0).data(Qt.ItemDataRole.UserRole)["online"] is False
        assert browser._current_media() is None
        unplugged.rename(root)
        assert folder.exists()
        with qtbot.waitSignal(transport.loaded):
            browser._refresh()
        browser.list.setCurrentRow(0)
        assert browser._current_media() is not None
    finally:
        browser.shutdown()
