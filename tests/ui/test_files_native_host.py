"""Real native host composition, with no Chromium or loopback server."""
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLineEdit

from src.app.ui.files_panel import FilesPanel
from src.app.ui.library_panel import LibraryPanel
from src.app.ui.native_files_panel import NativeFilesPanel
from src.features.files.repository import FilesDataLocator


def _isolate(monkeypatch, tmp_path):
    locator = FilesDataLocator.for_config_root(tmp_path / "config")
    monkeypatch.setattr(FilesDataLocator, "default", classmethod(lambda cls: locator))
    monkeypatch.setattr(NativeFilesPanel, "is_browsable_path", lambda self, path: True)
    return locator


def test_native_host_loads_real_service_without_browser_or_server(qtbot, monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    media = tmp_path / "media"
    media.mkdir()
    (media / "item.txt").write_text("synthetic", encoding="utf-8")
    panel = FilesPanel()
    qtbot.addWidget(panel)
    try:
        assert not hasattr(panel, "browser") and not hasattr(panel, "adapter")
        assert not hasattr(panel, "mode_combo")
        panel.set_root_path(str(media))
        qtbot.waitUntil(lambda: panel._native_panel.current_path == str(media)
                        and panel._native_panel.model.rowCount() == 1, timeout=8000)
        assert panel.content_stack.currentWidget() is panel._native_panel
        assert panel._native_transport._facade is not None
        assert panel._native_panel.model.entries[0].name == "item.txt"
        qtbot.waitUntil(lambda: {"apps.topos_word", "apps.quick_3d_viewer"} <= {
            info.plugin_id for info in panel._files_plugins._infos}, timeout=8000)
        apps = [info for info in panel._files_plugins._infos if info.source == "local_app"]
        assert len(apps) == 2 and all(info.can_launch and not info.runtime_loaded for info in apps)
        assert panel._files_plugins._app_runtime is None  # Discovery never starts an application.
    finally:
        panel.shutdown()
        panel.shutdown()


def test_files_library_share_persisted_metadata_and_safe_operations(qtbot, monkeypatch, tmp_path):
    locator = _isolate(monkeypatch, tmp_path)
    media = tmp_path / "media"
    media.mkdir()
    source = media / "original.txt"
    source.write_text("source", encoding="utf-8")
    library, files = LibraryPanel(start_path=str(media)), FilesPanel()
    qtbot.addWidget(library)
    qtbot.addWidget(files)
    files.set_root_path(str(media))
    files.directory_changed.connect(library.set_root_path)
    library.directory_changed.connect(files.set_root_path)
    try:
        qtbot.waitUntil(lambda: library._native_panel.model.rowCount() == 1
                        and files._native_panel.current_path == str(media), timeout=8000)
        facade = library._native_transport._runtime()
        record = facade.libraries.register(str(media))
        facade.scan.scan_library(record.library_id)
        snapshots = []
        library._native_transport.metadata_snapshot.connect(snapshots.append)
        library.set_file_rating(str(source), 4)
        qtbot.waitUntil(lambda: bool(snapshots), timeout=8000)
        files.refresh_file_rating(str(source), 4)
        qtbot.waitUntil(lambda: bool(files._native_panel.model.entries) and files._native_panel.model.entries[0].user.rating == 4,
                        timeout=8000)
        library.resize(1100, 700)
        library.show()
        jobs = []
        library._native_transport.task_finished.connect(jobs.append)
        library.rename_item(str(source))
        editor = library._native_panel.pages.currentWidget().findChild(QLineEdit, "filesNameEditor")
        assert editor is not None
        editor.setText("renamed.txt")
        qtbot.keyClick(editor, Qt.Key.Key_Return)
        qtbot.waitUntil(lambda: bool(jobs), timeout=8000)
        assert (media / "renamed.txt").read_text() == "source"
        assert not source.exists()
        assert locator.catalog_db.is_file()
        assert not hasattr(library, "db")  # no independent host.db metadata store
    finally:
        library.shutdown()
        files.shutdown()
