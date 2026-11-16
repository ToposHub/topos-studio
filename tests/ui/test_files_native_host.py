"""Check native/legacy mode coordination without starting Chromium or Flask."""
from types import SimpleNamespace

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QWidget

from src.app.ui import files_panel
from src.app.ui.native_files_transport import NativeFilesTransport


def test_native_navigation_is_lazy_and_activation_stays_native(qtbot, monkeypatch):
    scripts = []

    class FakeHost(QObject):
        closed = pyqtSignal()
        prev_requested = pyqtSignal()
        next_requested = pyqtSignal()
        failed = pyqtSignal(str)
        notice = pyqtSignal(str)

        def __init__(self, container, parent=None):
            super().__init__(parent)
            self.container = container

        def play(self, path):
            self.path = path

        def stop(self):
            pass

        def shutdown(self):
            pass

    class Browser(QWidget):
        loadFinished = pyqtSignal(bool)
        renderProcessTerminated = pyqtSignal(int, int)

        def page(self):
            return SimpleNamespace(
                setBackgroundColor=lambda *_: None,
                setWebChannel=lambda *_: None,
                runJavaScript=lambda code, *_: scripts.append(code),
            )

        def setHtml(self, *_):
            pass

    adapter = SimpleNamespace(
        start_server=lambda: False, stop_server=lambda: None,
        get_server_url=lambda: "http://127.0.0.1:1",
    )
    monkeypatch.setattr(files_panel, "QWebEngineView", Browser)
    import src.app.ui.native_files_panel as native_panel_module
    monkeypatch.setattr(native_panel_module, "NativePlayerHost", FakeHost)
    monkeypatch.setattr(files_panel.LibraryAdapter, "instance", lambda: adapter)
    monkeypatch.setattr(files_panel, "fe_bridge", None)
    monkeypatch.setattr(files_panel.FilesPanel, "_wait_server_healthy", lambda self: None)
    path = "/media/a+b%20"
    entry = {"name": "a 'test'.mov", "path": path + "/a 'test'.mov", "type": "video"}

    def load(transport, directory):
        transport.loading.emit(directory)
        transport.loaded.emit(directory, [entry])

    monkeypatch.setattr(NativeFilesTransport, "load_directory", load)
    panel = files_panel.FilesPanel()
    qtbot.addWidget(panel)
    panel._page_loaded = True
    panel._is_error_page = False
    panel.mode_combo.setCurrentIndex(0)
    baseline = len(scripts)
    panel._files_root_path = path
    panel.mode_combo.setCurrentIndex(1)
    assert panel._native_panel.current_path == path
    # A hidden legacy grid must not render on each native navigation.
    assert len(scripts) == baseline
    assert panel.content_stack.currentWidget() is panel._native_panel
    panel._native_panel.file_activated.emit(entry["path"])
    assert panel.mode_combo.currentIndex() == 1
    # 原生模式激活视频必须留在原生面板，不再借道旧 WebView。
    assert panel._files_prefs.native_default is True
    assert len(scripts) == baseline
    assert panel._native_panel._preview.is_video
    assert panel._native_panel._preview_path == entry["path"]
    assert panel._pending_path == path
    panel.shutdown()
