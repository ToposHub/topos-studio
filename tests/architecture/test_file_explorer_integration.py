"""Integration tests for the file_explorer adapter and FilesPanel public API."""

from pathlib import Path
from unittest.mock import Mock

from src.app.ui import library_adapter
from src.app.ui.files_panel import FilesPanel


# ---------------------------------------------------------------------------
# 1. Paths that the adapter exposes must exist on disk
# ---------------------------------------------------------------------------

def test_file_explorer_adapter_paths_exist():
    assert Path(library_adapter.external_path).exists(), (
        f"external_path does not exist: {library_adapter.external_path}"
    )
    assert Path(library_adapter.file_explorer_path).exists(), (
        f"file_explorer_path does not exist: {library_adapter.file_explorer_path}"
    )
    assert Path(library_adapter.file_explorer_path, "main.py").exists(), (
        f"main.py missing in file_explorer_path: {library_adapter.file_explorer_path}"
    )


# ---------------------------------------------------------------------------
# 2. The adapter must successfully import the Flask app and helpers
# ---------------------------------------------------------------------------

def test_file_explorer_imports_flask_app():
    assert library_adapter.file_explorer_app is not None, (
        "file_explorer_app is None — Flask app failed to import"
    )
    assert library_adapter.find_available_port is not None, (
        "find_available_port is None — helper failed to import"
    )
    assert library_adapter.make_server is not None, (
        "make_server is None — werkzeug failed to import"
    )


# ---------------------------------------------------------------------------
# 3. FilesPanel must expose the public sync contract
# ---------------------------------------------------------------------------

def test_files_panel_public_sync_contract():
    assert hasattr(FilesPanel, "set_root_path"), (
        "FilesPanel is missing the 'set_root_path' method"
    )
    assert hasattr(FilesPanel, "refresh_file_rating"), (
        "FilesPanel is missing the 'refresh_file_rating' method"
    )


def test_files_panel_calls_the_namespaced_frontend_path_api():
    panel = Mock()
    FilesPanel._set_path_js(panel, '/tmp/素材 "A"')

    js_code = panel._run_js.call_args.args[0]
    assert "window.__FE" in js_code
    assert "api.loadPath(" in js_code
    assert "return true" in js_code
    assert panel._run_js.call_args.kwargs["purpose"] == "set_path"


def test_file_explorer_trailing_actions_use_iife_exports():
    actions = Path(library_adapter.file_explorer_path,
                   "static/assets/js/fe-actions.js").read_text(encoding="utf-8")
    assert "Object.assign(window.__FE, { loadPath, openItem, isVirtualView });" in actions
    assert (
        "const { $, $$, STATE, escapeHtml, loadPath, isVirtualView, openLightbox } "
        "= window.__FE;"
    ) in actions


# ---------------------------------------------------------------------------
# 4. R0-2: ensure_runtime must run on the caller (GUI) thread BEFORE the
#    FlaskWorker starts, or plugin GuiLauncher signals land on a thread
#    without a Qt event loop and the GUI plugins silently fail to launch.
# ---------------------------------------------------------------------------

def test_ensure_runtime_runs_on_gui_thread_before_worker(monkeypatch, qapp):
    import threading

    from src.app.integrations.file_explorer import adapter

    calls = []

    def fake_ensure_runtime(port=None):
        calls.append((threading.current_thread(), port))

    worker_starts = []

    class FakeWorker:
        def __init__(self, app, host, port):
            pass

        def start(self):
            worker_starts.append(True)

        def isRunning(self):
            return True

    monkeypatch.setattr(adapter, "file_explorer_ensure_runtime", fake_ensure_runtime)
    monkeypatch.setattr(adapter, "FlaskWorker", FakeWorker)

    # 重置单例后构造独立实例，避免依赖/污染全局单例状态（monkeypatch 自动还原）
    monkeypatch.setattr(adapter.FileExplorerAdapter, "_instance", None)
    instance = adapter.FileExplorerAdapter.instance()
    instance.worker = None
    instance.host, instance.port = "127.0.0.1", 8199

    assert instance.start_server() is True
    assert worker_starts, "FlaskWorker.start() should have been called"
    assert calls, "ensure_runtime was never called by start_server"
    assert calls[0][0] is threading.current_thread(), (
        "ensure_runtime must run on the caller (GUI) thread, not in the worker"
    )
    assert calls[0][1] == 8199, (
        "[R1-6] adapter 必须把选定端口传给 ensure_runtime，端口文件与实际绑定一致"
    )
