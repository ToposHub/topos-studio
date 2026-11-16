"""Isolated real builtin launch smoke and native plugin UI captures.

Only four known repository plugins are explicitly loaded into a temporary Flask
host. No user config/database is initialized, no processing job is started.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from src.app.ui.files_plugins_controller import FilesPluginsController
from src.app.ui.native_files_panel import NativeFilesPanel
from src.app.ui.native_files_prefs import NativeFilesPreferences
from src.app.ui.native_files_transport import NativeFilesTransport
from src.app.integrations.file_explorer.plugins import FileExplorerPluginCatalog
from src.shared.ui.style_sheet import DARK_THEME
from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QApplication
from flask import Flask, jsonify, request
from werkzeug.serving import make_server


def main() -> None:
    out = Path(__file__).parent
    app = QApplication([])
    app.setFont(QFont("PingFang SC", 10))
    app.setStyleSheet(DARK_THEME)
    api = Flask("isolated-plugin-validation")
    @api.before_request
    def authenticate() -> object:
        if request.headers.get("X-Auth-Token") != "isolated-smoke-token":
            return jsonify({"error": "unauthorized"}), 401
        return None
    root = ROOT / "external/file_explorer/plugins"
    ids = ("format_factory", "image_factory", "shutdown_timer", "decode_benchmark")
    modules = {}
    for pid in ids:
        name = f"topos_validation_{pid}"
        spec = importlib.util.spec_from_file_location(name, root / pid / "__init__.py", submodule_search_locations=[str(root / pid)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        context = SimpleNamespace(socket=None, register_js=lambda *args: None, register_ui_component=lambda *args: None)
        module.setup(context)
        api.register_blueprint(module.bp, url_prefix=f"/api/plugin/{pid}")
        modules[pid] = module
    manager = SimpleNamespace(plugins=[{"name": pid} for pid in ids], failed_plugins=[])
    server = make_server("127.0.0.1", 0, api)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with tempfile.TemporaryDirectory(prefix="topos-plugin-ui-") as temporary:
        prefs = NativeFilesPreferences(QSettings(str(Path(temporary) / "prefs.ini"), QSettings.Format.IniFormat))
        panel = NativeFilesPanel(preferences=prefs)
        panel.resize(1180, 760)
        panel.show()
        transport = NativeFilesTransport(lambda: f"http://127.0.0.1:{server.server_port}", "isolated-smoke-token", panel)
        catalog = FileExplorerPluginCatalog(root, manager, lambda: {"disabled_plugins": []})
        host = FilesPluginsController(catalog, transport, prefs, panel, lambda: [], lambda: None, panel)
        def wait_until(predicate: object) -> None:
            deadline = time.monotonic() + 15
            while not predicate():
                app.processEvents()
                if time.monotonic() > deadline:
                    raise TimeoutError(host.center.notice.text())
                time.sleep(.005)
            app.processEvents()
        try:
            wait_until(lambda: len(host._infos) == 10)
            results = []
            for pid in ids:
                host.launch(pid)
                wait_until(lambda: modules[pid]._launcher.window is not None and modules[pid]._launcher.window.isVisible())
                wait_until(lambda: pid in prefs.plugin_recent)
                window = modules[pid]._launcher.window
                if pid == "shutdown_timer":
                    assert not window.is_running
                window.grab().save(str(out / f"launched_{pid}.png"))
                results.append({"id": pid, "window": type(window).__name__, "visible": window.isVisible(), "http_ack": pid in prefs.plugin_recent})
                window.hide()
            host._pin("image_factory")
            host._set_shortcut("image_factory", "Ctrl+Alt+I")
            host.show_center()
            wait_until(lambda: host.center.items.count() == 10)
            host.center.select_plugin("image_factory")
            app.processEvents()
            host.center.grab().save(str(out / "plugin_center.png"))
            host.center.resize(840, 560)
            app.processEvents()
            host.center.grab().save(str(out / "plugin_center_compact.png"))
            host.center.select_plugin("code_editor")
            app.processEvents()
            host.center.grab().save(str(out / "plugin_legacy_details.png"))
            host.center.hide()
            host.show_launcher()
            app.processEvents()
            host.launcher.grab().save(str(out / "plugin_launcher.png"))
            (out / "launch_results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
            print(json.dumps(results, ensure_ascii=False))
        finally:
            host.shutdown()
            host._pool.waitForDone(3000)
            transport.shutdown()
            for module in modules.values():
                if module._launcher.window is not None:
                    module._launcher.window.close()
            panel.shutdown()
            panel.close()
            app.processEvents()
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


if __name__ == "__main__":
    main()
