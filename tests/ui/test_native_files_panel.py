"""Native Files interaction contracts without disk scanning or WebEngine."""
from PyQt6.QtCore import QBuffer, QIODevice, QItemSelectionModel, Qt
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtWidgets import QFrame

from src.app.ui.native_files_panel import NativeFilesPanel
from src.shared.ui.panel_splitter import PanelSplitter
from src.shared.ui.style_sheet import C


def _entries():
    return [
        {"name": "Folder", "path": "/media/Folder", "type": "dir"},
        {"name": "A #100% 素材.mov", "path": "/media/A #100% 素材.mov", "type": "video", "size_bytes": 50, "tags": "interview"},
        {"name": "B.png", "path": "/media/B.png", "type": "image", "size_bytes": 20},
    ]


def _panel(qtbot):
    panel = NativeFilesPanel()
    qtbot.addWidget(panel)
    panel.set_entries("/media", _entries())
    return panel


def _select_all(panel):
    panel.selection.select(
        panel.proxy.index(0, 0), QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows)
    for row in range(1, panel.proxy.rowCount()):
        panel.selection.select(panel.proxy.index(row, 0), QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)


def test_views_share_selection_and_drag_all_files(qtbot):
    panel = _panel(qtbot)
    _select_all(panel)
    panel.view_combo.setCurrentIndex(1)
    assert panel.icon_view.selectionModel() is panel.detail_view.selectionModel()
    assert len(panel.selection.selectedRows()) == 3
    mime = panel.mime_data_for_selection()
    assert [url.toLocalFile() for url in mime.urls()] == [entry["path"] for entry in _entries()[1:]]
    assert b"%23" in bytes(mime.data("text/uri-list"))
    assert b"%25" in bytes(mime.data("text/uri-list"))
    assert panel.model.flags(panel.model.index(1, 0)) & Qt.ItemFlag.ItemIsDragEnabled
    assert not panel.model.flags(panel.model.index(0, 0)) & Qt.ItemFlag.ItemIsDragEnabled


def test_filter_searches_tags_and_combines_type(qtbot):
    panel = _panel(qtbot)
    panel.search_edit.setText("INTERVIEW")
    assert panel.proxy.rowCount() == 1
    panel.type_combo.setCurrentIndex(panel.type_combo.findData("image"))
    assert panel.proxy.rowCount() == 0
    panel.search_edit.clear()
    assert panel.proxy.rowCount() == 2  # Folder stays available for navigation.


def test_loading_and_error_clear_old_drag_targets(qtbot):
    panel = _panel(qtbot)
    _select_all(panel)
    panel.set_loading("/locked")
    assert panel.model.rowCount() == 0
    assert not panel.mime_data_for_selection().hasUrls()
    panel.show_error("/locked", "locked")
    assert panel.path_edit.text() == "/locked"
    assert not panel.pages.isEnabled()
    assert panel.message.text() == "locked"
    assert panel.current_path == "/media"


def test_navigation_history_commits_only_success(qtbot):
    panel = _panel(qtbot)
    requests = []
    panel.directory_requested.connect(requests.append)
    panel.navigate("/failed")
    panel.show_error("/failed", "no access")
    assert panel._history == ["/media"]
    panel.navigate("/other")
    panel.set_entries("/other", [])
    panel.back_button.click()
    assert requests[-1] == "/media"
    panel.set_entries("/media", _entries())
    assert panel.forward_button.isEnabled()
    panel.forward_button.click()
    assert requests[-1] == "/other"


def test_activation_and_input_shortcuts_do_not_delete_or_navigate(qtbot):
    panel = _panel(qtbot)
    requests, opened = [], []
    panel.directory_requested.connect(requests.append)
    panel.file_activated.connect(opened.append)
    panel._activate(panel.proxy.index(0, 0))
    assert requests == ["/media/Folder"]
    panel.selection.select(panel.proxy.index(1, 0), QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows)
    qtbot.keyClick(panel.icon_view, Qt.Key.Key_Return)
    assert opened == ["/media/A #100% 素材.mov"]
    panel.path_edit.setText("/media/x")
    qtbot.keyClick(panel.path_edit, Qt.Key.Key_Backspace)
    assert panel.path_edit.text() == "/media/"
    assert requests == ["/media/Folder"]


def test_filter_and_sort_keep_folder_first_and_no_snapshot_requests(qtbot):
    panel = _panel(qtbot)
    requests = []
    panel.directory_requested.connect(requests.append)
    panel.proxy.sort(2, Qt.SortOrder.DescendingOrder)
    assert panel.proxy.index(0, 0).data() == "Folder"
    panel.set_entries("/media", _entries())
    assert requests == []


def test_virtual_location_does_not_issue_disk_request(qtbot):
    panel = _panel(qtbot)
    requests = []
    panel.directory_requested.connect(requests.append)
    panel.navigate("collection://1")
    assert requests == []
    assert panel.current_path == "/media"


def test_malformed_optional_metadata_does_not_escape_qt_callbacks(qtbot):
    panel = _panel(qtbot)
    entries = _entries()
    entries[1].update(rating="bad", size_bytes={}, mtime_ts="nan")
    panel.set_entries("/media", entries)
    assert panel.model.index(1, 4).data() == ""
    for column in (2, 3, 4):
        panel.proxy.sort(column)
        assert panel.proxy.rowCount() == 3


def _png_bytes():
    image = QImage(8, 8, QImage.Format.Format_RGB32)
    image.fill(QColor("#123456"))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def test_thumbnail_requests_are_lazy_and_failures_wait_for_refresh(qtbot):
    panel = _panel(qtbot)
    requests = []
    panel.thumbnail_requested.connect(lambda *args: requests.append(args))
    assert requests == []
    assert panel._image_for_entry(_entries()[0]) is None  # No directory thumbs.
    panel._image_for_entry(_entries()[1])
    panel._image_for_entry(_entries()[1])
    assert requests == [(_entries()[1]["path"], 0)]
    panel.thumbnail_failed(_entries()[1]["path"])
    panel._image_for_entry(_entries()[1])
    assert len(requests) == 1
    panel.set_entries("/media", _entries())
    panel._image_for_entry(_entries()[1])
    assert len(requests) == 2


def test_real_thumbnail_decode_and_directory_change_clear_old_images(qtbot):
    panel = _panel(qtbot)
    path = _entries()[1]["path"]
    panel.set_thumbnail(path, _png_bytes())
    assert panel._images[path].pixelColor(0, 0).name() == "#123456"
    panel.set_thumbnail(_entries()[2]["path"], b"invalid image")
    assert _entries()[2]["path"] in panel._thumbnail_failed
    panel.set_loading("/new")
    panel.set_thumbnail(path, _png_bytes())
    assert not panel._images


def test_thumbnail_lru_releases_evicted_image_for_refetch(qtbot):
    panel = _panel(qtbot)
    entries = [{"name": str(i), "path": f"/media/{i}.png", "type": "image"} for i in range(129)]
    panel.set_entries("/media", entries)
    released = []
    panel.thumbnail_released.connect(released.append)
    for entry in entries:
        panel.set_thumbnail(entry["path"], _png_bytes())
    assert len(panel._images) == 128
    assert released == ["/media/0.png"]


def test_responsive_layout_and_inspector_control_preserve_selection(qtbot):
    panel = _panel(qtbot)
    panel.resize(1120, 650)
    panel.show()
    panel.selection.select(panel.proxy.index(1, 0), QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows)
    qtbot.waitUntil(lambda: panel.inspector.isVisible())
    panel.resize(480, 520)
    qtbot.waitUntil(lambda: not panel.sidebar.isVisible() and not panel.inspector.isVisible())
    assert panel.width() <= 480
    assert panel.selection.selectedRows()[0].data() == _entries()[1]["name"]
    panel.inspector_button.click()
    assert panel.inspector.isVisible()
    panel.inspector_button.click()
    assert not panel.inspector.isVisible()


def test_cold_thumbnail_becomes_visible_without_manual_refresh(qtbot):
    panel = _panel(qtbot)
    requests = []

    def provide(path, version):
        requests.append(path)
        if requests.count(path) == 1:
            panel.thumbnail_pending(path)
        else:
            panel.set_thumbnail(path, _png_bytes())

    panel.thumbnail_requested.connect(provide)
    panel.resize(1120, 650)
    panel.show()
    path = _entries()[1]["path"]
    qtbot.waitUntil(lambda: path in panel._images, timeout=3000)
    assert requests.count(path) == 2
    assert path not in panel._thumbnail_failed


def test_directory_change_cancels_generation_retry(qtbot):
    panel = _panel(qtbot)
    path = _entries()[1]["path"]
    panel.thumbnail_pending(path)
    timer = panel._thumbnail_timers[path]
    assert timer.isActive()
    panel.set_loading("/other")
    assert not timer.isActive()
    assert not panel._thumbnail_timers
    assert not panel._thumbnail_attempts


def test_large_visible_gallery_cache_converges_without_reload_loop(qtbot, monkeypatch):
    panel = _panel(qtbot)
    entries = [{"name": str(i), "path": f"/media/{i}.png", "type": "image"} for i in range(129)]
    panel.set_entries("/media", entries)
    # Model a viewport that paints more items than the ordinary 128-image LRU.
    monkeypatch.setattr(panel, "_entry_visible", lambda path: True)
    monkeypatch.setattr(panel, "_thumbnail_capacity", lambda: 160)
    requests = []
    panel.thumbnail_requested.connect(lambda path, version: requests.append(path))
    for _ in range(3):
        for entry in entries:
            if panel._image_for_entry(entry) is None:
                panel.set_thumbnail(entry["path"], _png_bytes())
    assert len(requests) == 129
    assert len(panel._images) == 129


def test_search_expands_in_path_row_and_escape_restores_files(qtbot):
    panel = _panel(qtbot)
    panel.resize(1120, 650)
    panel.show()
    assert panel.search_edit.isHidden()
    panel.search_button.click()
    assert not panel.search_edit.isHidden()
    assert abs(panel.search_button.geometry().center().y() - panel.path_stack.geometry().center().y()) <= 2
    assert panel.search_edit.width() <= 200
    qtbot.keyClicks(panel.search_edit, "INTERVIEW")
    assert panel.proxy.rowCount() == 1
    qtbot.keyClick(panel.search_edit, Qt.Key.Key_Escape)
    assert panel.search_edit.isHidden()
    assert panel.search_edit.text() == ""
    assert panel.proxy.rowCount() == 3


def test_compact_search_fits_and_restores_navigation(qtbot):
    panel = _panel(qtbot)
    panel.resize(480, 520)
    panel.show()
    panel.search_button.click()
    qtbot.waitUntil(lambda: not panel.path_stack.isVisible())
    assert panel.search_edit.isVisible()
    assert panel.more_button.geometry().right() < panel.width()
    qtbot.keyClick(panel.search_edit, Qt.Key.Key_Escape)
    assert panel.path_stack.isVisible()
    assert panel.sidebar_button.isVisible()


def test_functional_sections_use_shared_black_rules(qtbot):
    panel = _panel(qtbot)
    panel.resize(1120, 650)
    panel.show()
    assert isinstance(panel.splitter, PanelSplitter)
    rule = panel.findChild(QFrame, "filesRule")
    assert rule.height() == 1
    assert rule.grab().toImage().pixelColor(0, 0).name() == QColor(C.PANEL_DIVIDER).name()


def test_home_tree_hides_system_folders_without_hiding_named_media_folders(qtbot):
    panel = NativeFilesPanel(home_path="/Users/creator")
    qtbot.addWidget(panel)
    entries = [{"name": name, "path": "/Users/creator/" + name, "type": "dir"}
               for name in ("Documents", "Movies", "Library", "Applications", ".hidden", "Editor.app")]
    panel.set_entries(panel.home_path, entries)
    assert [entry["name"] for entry in panel.model.entries] == ["Documents", "Movies"]
    panel.locations.set_children(panel.home_path, entries)
    assert panel.locations.home_item.text() == "Home"
    assert panel.locations.home_item.rowCount() == 2
    assert panel.is_browsable_path("/Volumes/Media/Library")
    assert panel.is_browsable_path("/Volumes/Media/System")
    requests = []
    panel.directory_requested.connect(requests.append)
    for blocked in ("/", "/Users", "/Applications", "/System/Library", "/Users/creator/Library", "/Users/creator/Applications", "/Volumes/Macintosh HD"):
        panel.navigate(blocked)
    panel.go_up()
    assert requests == []
    assert not panel.up_button.isEnabled()


def test_home_tree_expands_lazily_and_reveals_without_navigation_loop(qtbot):
    panel = NativeFilesPanel(home_path="/Users/creator")
    qtbot.addWidget(panel)
    trees, directories = [], []
    panel.tree_requested.connect(trees.append)
    panel.directory_requested.connect(directories.append)
    panel.start_home_tree()
    panel.start_home_tree()
    assert trees == [panel.home_path]
    panel.locations.set_children(panel.home_path, [{"name": "Movies", "path": panel.home_path + "/Movies"}])
    panel.locations.reveal(panel.home_path + "/Movies/Footage")
    assert trees[-1] == panel.home_path + "/Movies"
    panel.locations.set_children(panel.home_path + "/Movies", [{"name": "Footage", "path": panel.home_path + "/Movies/Footage"}])
    assert panel.locations.currentIndex().data(panel.locations.PATH_ROLE) == panel.home_path + "/Movies/Footage"
    assert directories == []


def test_home_tree_error_retries_and_reload_discards_old_nodes(qtbot):
    panel = NativeFilesPanel(home_path="/Users/creator")
    qtbot.addWidget(panel)
    requests = []
    panel.tree_requested.connect(requests.append)
    panel.start_home_tree()
    panel.locations.set_error(panel.home_path, "offline")
    panel.start_home_tree()
    assert requests == [panel.home_path, panel.home_path]
    panel.locations.set_children(panel.home_path, [{"name": "Old", "path": panel.home_path + "/Old"}])
    panel.locations.set_children(panel.home_path, [{"name": "New", "path": panel.home_path + "/New"}])
    assert panel.home_path + "/Old" not in panel.locations._nodes
    assert panel.locations.home_item.child(0).text() == "New"


def test_refresh_reloads_current_home_tree_node_and_file_list(qtbot):
    panel = NativeFilesPanel(home_path="/Users/creator")
    qtbot.addWidget(panel)
    panel.set_entries(panel.home_path, [])
    panel.locations.set_children(panel.home_path, [{"name": "Old", "path": panel.home_path + "/Old"}])
    trees, directories = [], []
    panel.tree_requested.connect(trees.append)
    panel.directory_requested.connect(directories.append)
    panel.refresh()
    assert trees == [panel.home_path]
    assert directories == [panel.home_path]
    panel.locations.set_children(panel.home_path, [{"name": "New", "path": panel.home_path + "/New"}])
    assert panel.locations.home_item.child(0).text() == "New"
    assert panel.home_path + "/Old" not in panel.locations._nodes
