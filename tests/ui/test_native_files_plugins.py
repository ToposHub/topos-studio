"""Native plugin interactions against isolated settings/catalog and real HTTP I/O."""
from __future__ import annotations

import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtWidgets import QLabel, QLineEdit, QVBoxLayout, QWidget

from src.app.integrations.file_explorer.plugins import FileExplorerPluginCatalog
from src.app.ui.files_plugins_controller import FilesPluginsController
from src.app.ui.native_files_plugins import PluginCenterDialog, PluginLauncherDialog
from src.app.ui.native_files_prefs import NativeFilesPreferences
from src.app.ui.native_files_transport import NativeFilesTransport


@pytest.fixture
def catalog(tmp_path):
    root = tmp_path / "plugins"
    root.mkdir()
    ids = ("format_factory", "image_factory", "shutdown_timer", "decode_benchmark", "code_editor")
    for pid in ids:
        folder = root / pid
        folder.mkdir()
        (folder / "__init__.py").write_text(
            f"NAME = {pid!r}\nDESCRIPTION = '测试素材工具'\nAUTHOR = 'Topos'\n", encoding="utf-8")
    config = {"disabled_plugins": [], "unknown": {"keep": True}}
    manager = SimpleNamespace(plugins=[{"name": pid} for pid in ids], failed_plugins=[])
    def save(value):
        config.clear()
        config.update(value)
    return FileExplorerPluginCatalog(root, manager, lambda: dict(config), save)


@pytest.fixture
def endpoint():
    observed = []
    response = {"status": 200, "payload": {"status": "ok"}}
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            observed.append((urlsplit(self.path).path, self.headers.get("X-Auth-Token"), json.loads(self.rfile.read(length))))
            self.send_response(response["status"])
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response["payload"]).encode())
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", observed, response
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


@pytest.fixture
def controller(qtbot, catalog, tmp_path, endpoint):
    prefs = NativeFilesPreferences(QSettings(str(tmp_path / "plugins.ini"), QSettings.Format.IniFormat))
    prefs.plugin_shortcuts = {"image_factory": "Ctrl+Alt+I"}
    panel = QWidget()
    layout = QVBoxLayout(panel)
    panel.meta_status = QLabel()
    editor = QLineEdit()
    layout.addWidget(editor)
    layout.addWidget(panel.meta_status)
    qtbot.addWidget(panel)
    transport = NativeFilesTransport(lambda: endpoint[0], "private-test-token", panel)
    selected = ["/media/A #100% 中文.mov"]
    legacy = []
    host = FilesPluginsController(catalog, transport, prefs, panel, lambda: list(selected), lambda: legacy.append(True), panel)
    qtbot.waitUntil(lambda: len(host._infos) == 5)
    yield host, selected, legacy, editor
    host.shutdown()
    host._pool.waitForDone(3000)
    transport.shutdown()


def test_center_keeps_disabled_failed_plaintext_and_filters(qtbot, catalog):
    rows = list(catalog.snapshot())
    rows[0] = replace(rows[0], name="<b>未执行的文本</b>", error="broken", launch_unavailable_reason="broken")
    rows[1] = replace(rows[1], desired_enabled=False, needs_restart=False, runtime_loaded=False)
    center = PluginCenterDialog()
    qtbot.addWidget(center)
    center.set_snapshot(rows, [], [], {}, set())
    assert center.items.count() == 5
    assert center.title.text() == "<b>未执行的文本</b>"
    assert center.title.textFormat() == Qt.TextFormat.PlainText
    center.categories.setCurrentRow(6)  # failed
    assert center.items.count() == 1
    assert not center.launch.isEnabled()
    center.categories.setCurrentRow(5)  # disabled
    assert center.items.count() == 1
    center.search.setText("does-not-exist")
    assert center.items.count() == 0
    assert center.details.isHidden()
    assert center._selected == ""


def test_center_function_category_pin_and_legacy_intents(qtbot, catalog):
    center = PluginCenterDialog()
    qtbot.addWidget(center)
    center.set_snapshot(catalog.snapshot(), ["image_factory"], [], {}, set())
    center.categories.setCurrentRow(1)
    assert center.items.count() == 1
    assert center._selected == "image_factory"
    for row in range(center.categories.count()):
        if center.categories.item(row).data(Qt.ItemDataRole.UserRole) == "category:转档与压缩":
            center.categories.setCurrentRow(row)
            break
    assert center.items.count() == 1
    assert center._selected == "format_factory"
    center.select_plugin("code_editor")
    assert not center.launch.isEnabled()
    assert not center.legacy.isHidden()
    with qtbot.waitSignal(center.legacy_requested):
        center.legacy.click()


def test_launcher_only_available_tools_pin_order_search_keyboard(qtbot, catalog):
    launcher = PluginLauncherDialog()
    qtbot.addWidget(launcher)
    launcher.set_snapshot(catalog.snapshot(), ["shutdown_timer"], ["image_factory"], {}, set())
    assert launcher.items.count() == 4
    assert launcher.items.item(0).data(Qt.ItemDataRole.UserRole) == "shutdown_timer"
    assert launcher.items.item(1).data(Qt.ItemDataRole.UserRole) == "image_factory"
    launcher.show()
    qtbot.keyClick(launcher.search, Qt.Key.Key_Down)
    assert launcher.items.currentRow() == 1
    with qtbot.waitSignal(launcher.launch_requested) as signal:
        qtbot.keyClick(launcher.search, Qt.Key.Key_Return)
    assert signal.args == ["image_factory"]
    launcher.search.setText("格式工厂")
    assert launcher.items.count() == 1
    launcher.search.setText("absent")
    assert launcher.items.count() == 0
    assert not launcher.empty.isHidden()


def test_initial_shortcuts_restore_without_opening_center_and_ignore_editor(controller):
    host, _, _, editor = controller
    assert len(host._actions) == 1
    assert host._actions[0].shortcut().toString() == "Ctrl+Alt+I"
    host._focus_changed(None, editor)
    assert not host._actions[0].isEnabled()
    host._focus_changed(editor, host.panel)
    assert host._actions[0].isEnabled()


def test_shortcut_conflicts_persistence_and_clear(controller):
    host, _, _, _ = controller
    host._set_shortcut("format_factory", "Ctrl+Alt+I")
    assert "占用" in host.center.notice.text()
    assert "format_factory" not in host.preferences.plugin_shortcuts
    host._set_shortcut("format_factory", "Ctrl+C")
    assert "format_factory" not in host.preferences.plugin_shortcuts
    host._set_shortcut("format_factory", "P")
    assert "单组" in host.center.notice.text()
    host._set_shortcut("format_factory", "Ctrl+Alt+F")
    assert host.preferences.plugin_shortcuts["format_factory"] == "Ctrl+Alt+F"
    host._set_shortcut("format_factory", "")
    assert "format_factory" not in host.preferences.plugin_shortcuts


def test_disable_visible_persistent_and_no_runtime_reload(controller, qtbot):
    host, _, _, _ = controller
    host.center.select_plugin("image_factory")
    host.center.enabled.click()
    assert host._writing
    host._project()
    assert not host.center.enabled.isEnabled()
    qtbot.waitUntil(lambda: not host._writing)
    row = next(info for info in host._infos if info.plugin_id == "image_factory")
    assert not row.desired_enabled and row.runtime_loaded and row.needs_restart
    assert not row.can_launch
    assert host.center.items.count() == 5
    assert host.catalog._config()["unknown"] == {"keep": True}
    assert "重启" in host.center.notice.text()


def test_launch_auth_selection_snapshot_duplicate_suppression_and_recent(controller, endpoint, qtbot):
    host, selected, _, _ = controller
    host.launch("format_factory")
    host.launch("format_factory")
    selected[:] = ["/other.mov"]
    qtbot.waitUntil(lambda: bool(host.preferences.plugin_recent))
    assert endpoint[1] == [("/api/plugin/format_factory/launch", "private-test-token", {"path": "/media/A #100% 中文.mov"})]
    assert host.preferences.plugin_recent == ["format_factory"]
    assert "已提交" in host.center.notice.text()


def test_multiselection_is_rejected_without_losing_files(controller, endpoint, qtbot):
    host, selected, _, _ = controller
    selected.append("/second.mov")
    host.launch("format_factory")
    qtbot.waitUntil(lambda: not host._busy)
    assert "只能接收一个" in host.center.notice.text()
    assert endpoint[1] == []
    assert host.preferences.plugin_recent == []


@pytest.mark.parametrize("status,payload", [(500, {"status": "ok"}), (200, []), (200, {"status": "error"}), (302, {"status": "ok"})])
def test_server_failure_never_records_usage(controller, endpoint, qtbot, status, payload):
    host, _, _, _ = controller
    endpoint[2].update(status=status, payload=payload)
    host.launch("image_factory")
    qtbot.waitUntil(lambda: not host._busy)
    assert host.preferences.plugin_recent == []
    assert "未接受" in host.center.notice.text()


def test_pin_roundtrip_and_legacy_navigation(controller):
    host, _, legacy, _ = controller
    host._pin("image_factory")
    assert host.preferences.plugin_pins == ["image_factory"]
    host._pin("image_factory")
    assert host.preferences.plugin_pins == []
    host._open_legacy()
    assert legacy == [True]


def test_transport_rejects_foreign_host_and_non_launcher(qtbot):
    transport = NativeFilesTransport(lambda: "https://example.com", "secret")
    results = []
    transport.plugin_launch_finished.connect(lambda *args: results.append(args))
    transport.launch_plugin("image_factory", "/api/plugin/image_factory/launch", {})
    transport.launch_plugin("code_editor", "/api/plugin/code_editor/launch", {})
    assert len(results) == 2 and all(not row[1] for row in results)
    assert not transport._plugin_replies
    transport.shutdown()


def test_shutdown_discards_late_launch_completion(controller):
    host, _, _, _ = controller
    host.shutdown()
    host._launch_finished("image_factory", True, "late")
    assert host.preferences.plugin_recent == []


def test_rejected_dispatch_clears_busy_without_waiting_for_network_ack(controller, endpoint, qtbot):
    from src.shared.plugins.services import rejected
    host, _, _, _ = controller
    host._commands.dispatch = lambda prepared: rejected(prepared.command_id, error_code="INVOCATION_CANCELLED", message="调用已取消")
    host.launch("image_factory")
    qtbot.waitUntil(lambda: not host._busy)
    assert host.preferences.plugin_recent == []
    assert endpoint[1] == []
    assert host.center.notice.text() == "调用已取消"


def test_shutdown_aborts_inflight_reply_and_discards_its_result(qtbot):
    from tests.ui.test_files_session_regressions import _FakeNetwork
    transport = NativeFilesTransport(lambda: "http://127.0.0.1:8000", "token")
    transport._network = _FakeNetwork()
    results = []
    transport.plugin_launch_finished.connect(lambda *args: results.append(args))
    transport.launch_plugin("image_factory", "/api/plugin/image_factory/launch", {})
    reply = transport._network.pending[0]
    transport.shutdown()
    assert reply.aborted and not transport._plugin_replies
    reply._payload = {"status": "ok"}
    reply.finished.emit()
    assert results == []


def test_native_panel_plugin_toolbar_and_context_actions(qtbot):
    from src.app.ui.native_files_panel import NativeFilesPanel
    panel = NativeFilesPanel()
    qtbot.addWidget(panel)
    with qtbot.waitSignal(panel.plugins_requested):
        panel.plugins_button.click()
    menu = panel._build_context_menu(panel.icon_view, panel.icon_view.rect().center())
    assert any("使用插件" in action.text() for action in menu.actions())
    panel.shutdown()
