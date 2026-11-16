"""Native Files interaction contracts without disk scanning or WebEngine."""
from PyQt6.QtCore import QBuffer, QIODevice, QItemSelectionModel, Qt
from PyQt6.QtGui import QColor, QImage

from src.app.ui.native_files_panel import NativeFilesPanel


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
