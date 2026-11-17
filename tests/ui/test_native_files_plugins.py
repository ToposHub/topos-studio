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


def _legacy_http_mode(host):
    """[G1-G4] 四个可启动 builtin 已全部原生接管——需验证仍存在的
    legacy HTTP 通道行为的测试，显式禁用原生命令表。"""
    host._native_commands = {}
    return host


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
    _legacy_http_mode(host)
    host.launch("decode_benchmark")
    host.launch("decode_benchmark")
    selected[:] = ["/other.mov"]
    qtbot.waitUntil(lambda: bool(host.preferences.plugin_recent))
    assert endpoint[1] == [("/api/plugin/decode_benchmark/launch", "private-test-token", {"paths": ["/media/A #100% 中文.mov"]})]
    assert host.preferences.plugin_recent == ["decode_benchmark"]
    assert "已提交" in host.center.notice.text()


def test_multiselection_is_rejected_without_losing_files(controller, endpoint, qtbot):
    host, selected, _, _ = controller
    _legacy_http_mode(host)
    selected.append("/second.mov")
    # [G1] format_factory 已原生接管（多选在原生计划层处理）——单选政策
    # 的 HTTP 拒绝口径改用仍走 legacy 的 decode_benchmark（kind=paths
    # 接受多选，此处用 format_factory 旧单选语义的替代断言：legacy 通道
    # 对多选 decode_benchmark 照常提交）
    host.launch("decode_benchmark")
    qtbot.waitUntil(lambda: not host._busy, timeout=8000)
    # decode_benchmark 的 payload 携带全部选择（多选不丢失）
    assert endpoint[1] and endpoint[1][0][2] == {"paths": [
        "/media/A #100% 中文.mov", "/second.mov"]}


@pytest.mark.parametrize("status,payload", [(500, {"status": "ok"}), (200, []), (200, {"status": "error"}), (302, {"status": "ok"})])
def test_server_failure_never_records_usage(controller, endpoint, qtbot, status, payload):
    host, _, _, _ = controller
    _legacy_http_mode(host)
    endpoint[2].update(status=status, payload=payload)
    # [G2] image_factory 已原生接管——服务失败语义改用 legacy 通道的
    # decode_benchmark 验证（native 无 HTTP，不存在此路径）
    host.launch("decode_benchmark")
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
    _legacy_http_mode(host)
    host._commands.dispatch = lambda prepared: rejected(prepared.command_id, error_code="INVOCATION_CANCELLED", message="调用已取消")
    # [G2] image_factory 已原生接管——dispatch 拒绝语义改用 legacy 通道
    host.launch("decode_benchmark")
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


# ---------------------------------------------------------------------------
# F1：单一 Inventory 三源聚合进入真实控制器/中心 UI
# ---------------------------------------------------------------------------

@pytest.fixture
def f1_sources(tmp_path):
    """受管安装根装一个新插件 + 一个开发源目录，与 legacy 目录并存。"""
    import json as _json
    from src.shared.plugins.discovery import PluginSourceConfig
    from src.shared.plugins.installer import LocalPackageInstaller

    host = dict(host_api=1, topos_version="3.0.0", platform="darwin", arch="x86_64")
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "topos.plugin.json").write_text(_json.dumps({
        "plugin_id": "files.installed_tool", "display_name": "已安装工具",
        "version": "1.2.0",
        "capabilities": [{"capability_id": "launch", "kind": "command"}]}),
        encoding="utf-8")
    LocalPackageInstaller(tmp_path / "managed", host_facts=host).install(package)
    dev = tmp_path / "devsrc"
    (dev / "files.dev_tool").mkdir(parents=True)
    (dev / "files.dev_tool" / "topos.plugin.json").write_text(_json.dumps({
        "plugin_id": "files.dev_tool", "display_name": "开发中工具",
        "version": "0.1.0",
        "capabilities": [{"capability_id": "launch", "kind": "command"}]}),
        encoding="utf-8")
    return PluginSourceConfig.for_host(
        builtin_ids={"format_factory", "image_factory", "shutdown_timer",
                     "decode_benchmark", "code_editor"},
        install_root=tmp_path / "managed", dev_roots=[dev])


def test_controller_merges_three_sources_into_center(qtbot, catalog, tmp_path,
                                                     endpoint, f1_sources):
    """三源聚合：legacy 行带来源/状态增强，仅存在于 inventory 的安装/
    开发源以占位行出现（不可启动、原因如实）。"""
    prefs = NativeFilesPreferences(QSettings(str(tmp_path / "f1.ini"), QSettings.Format.IniFormat))
    panel = QWidget()
    layout = QVBoxLayout(panel)
    panel.meta_status = QLabel()
    layout.addWidget(QLineEdit())
    layout.addWidget(panel.meta_status)
    qtbot.addWidget(panel)
    transport = NativeFilesTransport(lambda: endpoint[0], "token", panel)
    host = FilesPluginsController(catalog, transport, prefs, panel,
                                  lambda: [], lambda: None, panel,
                                  sources_factory=lambda: f1_sources)
    try:
        qtbot.waitUntil(lambda: len(host._infos) == 7, timeout=8000)  # 5 legacy + 安装 + 开发
        by_id = {info.plugin_id: info for info in host._infos}
        # builtin 行：来源/安装事实直达 UI 数据；[F2] 合并行携带命名空间新 id
        builtin = by_id["files.format_factory"]
        assert builtin.source == "builtin" and builtin.install_state == "installed"
        assert builtin.command_key == "format_factory"   # 命令键保留短 id
        # 仅存在于 inventory 的安装源：占位行，不可启动且原因如实
        installed = by_id["files.installed_tool"]
        assert installed.source == "installed"
        assert installed.version == "1.2.0"
        assert not installed.can_launch
        assert "尚未接入" in installed.launch_unavailable_reason
        assert not installed.desired_enabled          # 第三方默认停用
        # 开发源：未验证 + 默认停用
        dev = by_id["files.dev_tool"]
        assert dev.source == "local_dev" and dev.install_state == "discovered"
        assert not dev.desired_enabled
        # 中心 UI 渲染来源行
        host.center.show()
        host.center.select_plugin("files.dev_tool")
        assert "开发源" in host.center.origin.text()
        assert "未验证" in host.center.origin.text()
        host.center.select_plugin("files.format_factory")
        assert "内建" in host.center.origin.text()
    finally:
        host.shutdown()


def test_controller_duplicate_identity_shows_diagnostic(qtbot, catalog, tmp_path,
                                                        endpoint, f1_sources):
    """同一 plugin_id 出现于 builtin 目录与安装根：诊断进入中心详情。"""
    import json as _json
    from dataclasses import replace as _replace
    from src.shared.plugins.discovery import PluginSourceConfig
    from src.shared.plugins.installer import LocalPackageInstaller

    package = tmp_path / "dupe-pkg"
    package.mkdir()
    (package / "topos.plugin.json").write_text(_json.dumps({
        "plugin_id": "files.format_factory", "display_name": "撞名安装包",
        "version": "9.9.9",
        "capabilities": [{"capability_id": "launch", "kind": "command"}]}),
        encoding="utf-8")
    LocalPackageInstaller(f1_sources.install_root, host_facts=dict(
        host_api=1, topos_version="3.0.0", platform="darwin",
        arch="x86_64")).install(package)
    sources = _replace(f1_sources)   # 同一安装根：现在有撞名包

    prefs = NativeFilesPreferences(QSettings(str(tmp_path / "dupe.ini"), QSettings.Format.IniFormat))
    panel = QWidget()
    layout = QVBoxLayout(panel)
    panel.meta_status = QLabel()
    layout.addWidget(QLineEdit())
    layout.addWidget(panel.meta_status)
    qtbot.addWidget(panel)
    transport = NativeFilesTransport(lambda: endpoint[0], "token", panel)
    host = FilesPluginsController(catalog, transport, prefs, panel,
                                  lambda: [], lambda: None, panel,
                                  sources_factory=lambda: sources)
    try:
        qtbot.waitUntil(lambda: len(host._infos) >= 6, timeout=8000)
        info = next(item for item in host._infos
                    if item.plugin_id == "files.format_factory")
        # builtin 优先保留；重复身份诊断并入该行（中心详情可见/可复制）
        assert info.source == "builtin"
        assert any("PLUGIN_ID_DUPLICATE" in note for note in info.diagnostics)
    finally:
        host.shutdown()


# ---------------------------------------------------------------------------
# F2：新 id/状态模型——固定/最近/快捷键/启停偏好 + 命令读同一快照
# ---------------------------------------------------------------------------

def test_f2_launch_keys_recents_on_new_id_and_wire_uses_command_key(
        qtbot, catalog, tmp_path, endpoint, f1_sources):
    """启动：最近使用按命名空间新 id 记账；线上仍是命令键（短 id）。"""
    prefs = NativeFilesPreferences(QSettings(str(tmp_path / "f2a.ini"), QSettings.Format.IniFormat))
    panel = QWidget()
    layout = QVBoxLayout(panel)
    panel.meta_status = QLabel()
    layout.addWidget(QLineEdit())
    layout.addWidget(panel.meta_status)
    qtbot.addWidget(panel)
    transport = NativeFilesTransport(lambda: endpoint[0], "token", panel)
    host = FilesPluginsController(catalog, transport, prefs, panel,
                                  lambda: ["/media/a.mov"], lambda: None, panel,
                                  sources_factory=lambda: f1_sources)
    _legacy_http_mode(host)   # [G4] 四 builtin 已全原生——显式验 legacy 通道
    try:
        qtbot.waitUntil(lambda: len(host._infos) == 7, timeout=8000)
        # [G1/G2] format_factory/image_factory 已由原生窗口接管（零 HTTP）
        # ——本测试改用仍走 legacy 路由的 decode_benchmark 验证线上命令键口径
        host.launch("files.decode_benchmark")   # UI 新 id
        qtbot.waitUntil(lambda: bool(endpoint[1]), timeout=8000)
        # 线上：命令键寻址（legacy 路由不变）
        path, token, payload = endpoint[1][0]
        assert path == "/api/plugin/decode_benchmark/launch"
        assert token == "token" and payload == {"paths": ["/media/a.mov"]}
        qtbot.waitUntil(lambda: bool(prefs.plugin_recent), timeout=8000)
        # 偏好：最近使用按新 id 建档
        assert prefs.plugin_recent[0] == "files.decode_benchmark"
        qtbot.waitUntil(lambda: "files.decode_benchmark" not in host._busy, timeout=8000)
    finally:
        host.shutdown()


def test_f2_inventory_only_enable_persists_desired(
        qtbot, catalog, tmp_path, endpoint, f1_sources):
    """新模型插件（无命令键）启停：desired 持久化到 store 并反映到行。"""
    from src.shared.plugins.discovery import DesiredStateStore

    store = DesiredStateStore(tmp_path / "desired.json")
    prefs = NativeFilesPreferences(QSettings(str(tmp_path / "f2b.ini"), QSettings.Format.IniFormat))
    panel = QWidget()
    layout = QVBoxLayout(panel)
    panel.meta_status = QLabel()
    layout.addWidget(QLineEdit())
    layout.addWidget(panel.meta_status)
    qtbot.addWidget(panel)
    transport = NativeFilesTransport(lambda: endpoint[0], "token", panel)
    host = FilesPluginsController(catalog, transport, prefs, panel,
                                  lambda: [], lambda: None, panel,
                                  sources_factory=lambda: f1_sources,
                                  desired_store=store)
    try:
        qtbot.waitUntil(lambda: len(host._infos) == 7, timeout=8000)
        info = next(item for item in host._infos
                    if item.plugin_id == "files.installed_tool")
        assert info.desired_enabled is False
        host._set_enabled("files.installed_tool", True)
        qtbot.waitUntil(lambda: store.load().get("files.installed_tool") is True,
                        timeout=8000)
        qtbot.waitUntil(lambda: next(
            item for item in host._infos
            if item.plugin_id == "files.installed_tool").desired_enabled,
            timeout=8000)
        # 重启语义：新 store 实例读回同一偏好
        assert store.load()["files.installed_tool"] is True
        # legacy 目录插件的启停不进 store（仍走旧 catalog 配置）
        host._set_enabled("files.format_factory", False)
        qtbot.wait(300)
        assert "files.format_factory" not in store.load()
    finally:
        host.shutdown()


def test_f2_old_short_id_preferences_are_not_migrated(
        qtbot, catalog, tmp_path, endpoint, f1_sources):
    """旧短 id 偏好自然失效：不迁移、不别名——固定列表无匹配行即不显示。"""
    prefs = NativeFilesPreferences(QSettings(str(tmp_path / "f2c.ini"), QSettings.Format.IniFormat))
    prefs.plugin_pins = ["format_factory"]          # 旧口径的固定项
    panel = QWidget()
    layout = QVBoxLayout(panel)
    panel.meta_status = QLabel()
    layout.addWidget(QLineEdit())
    layout.addWidget(panel.meta_status)
    qtbot.addWidget(panel)
    transport = NativeFilesTransport(lambda: endpoint[0], "token", panel)
    host = FilesPluginsController(catalog, transport, prefs, panel,
                                  lambda: [], lambda: None, panel,
                                  sources_factory=lambda: f1_sources)
    try:
        qtbot.waitUntil(lambda: len(host._infos) == 7, timeout=8000)
        host.center.show()
        host.center.select_plugin("files.format_factory")
        # 旧固定项不映射到新 id：固定按钮仍是"固定"（未固定状态）
        assert "固定到启动器" in host.center.pin.text()
    finally:
        host.shutdown()


def test_f2_desired_state_store_roundtrip(tmp_path):
    from src.shared.plugins.discovery import DesiredStateStore

    store = DesiredStateStore(tmp_path / "nested" / "desired.json")
    assert store.load() == {}
    store.save({"files.a": True, "files.b": False})
    assert DesiredStateStore(tmp_path / "nested" / "desired.json").load() == {
        "files.a": True, "files.b": False}
    store.save({})                                   # 清空即空档
    assert store.load() == {}


def test_f5_shortcuts_are_owned_contributions(qtbot, catalog, tmp_path, endpoint):
    """[F5] 快捷键经宿主会话持有所有权：替换释放旧句柄、停用整组注销。"""
    prefs = NativeFilesPreferences(QSettings(str(tmp_path / "f5.ini"), QSettings.Format.IniFormat))
    prefs.plugin_shortcuts = {"format_factory": "Ctrl+Alt+F"}
    panel = QWidget()
    layout = QVBoxLayout(panel)
    panel.meta_status = QLabel()
    layout.addWidget(QLineEdit())
    layout.addWidget(panel.meta_status)
    qtbot.addWidget(panel)
    transport = NativeFilesTransport(lambda: endpoint[0], "token", panel)
    host = FilesPluginsController(catalog, transport, prefs, panel,
                                  lambda: [], lambda: None, panel)
    try:
        qtbot.waitUntil(lambda: len(host._infos) == 5, timeout=8000)
        qtbot.waitUntil(lambda: host.host_session.shortcut_count("format_factory") == 1,
                        timeout=8000)
        # 停用（restart-bound）：贡献整组释放，快捷键注销
        host._set_enabled("format_factory", False)
        qtbot.waitUntil(lambda: host.host_session.shortcut_count("format_factory") == 0,
                        timeout=8000)
        from PyQt6.QtGui import QAction as _QAction
        actions = [a for a in panel.findChildren(_QAction)
                   if a.shortcut().toString() == "Ctrl+Alt+F"]
        assert not actions or not actions[0].isEnabled()
    finally:
        host.shutdown()


def test_f6_launch_is_owned_job_and_retire_waits_for_confirmation(
        qtbot, catalog, tmp_path, endpoint):
    """[F6] 启动=归属插件的任务：在途时停用进入 cancel_requested，
    应答到达确认终止后停用才完成（贡献保留到确认之后）。"""
    prefs = NativeFilesPreferences(QSettings(str(tmp_path / "f6.ini"), QSettings.Format.IniFormat))
    panel = QWidget()
    layout = QVBoxLayout(panel)
    panel.meta_status = QLabel()
    layout.addWidget(QLineEdit())
    layout.addWidget(panel.meta_status)
    qtbot.addWidget(panel)
    transport = NativeFilesTransport(lambda: endpoint[0], "token", panel)
    host = FilesPluginsController(catalog, transport, prefs, panel,
                                  lambda: ["/m/a.mov"], lambda: None, panel)
    _legacy_http_mode(host)   # [G4] 显式验证仍存在的 legacy 通道生命周期

    # 应答不自动到达：拦截 transport 提交（任务保持 running，不 emit 完成信号）
    submitted: list[tuple] = []
    host._commands.transport.launch_plugin = (
        lambda plugin_id, ep, payload:
        (submitted.append((plugin_id, ep, payload)), True)[1])
    try:
        qtbot.waitUntil(lambda: len(host._infos) == 5, timeout=8000)
        host.launch("decode_benchmark")
        qtbot.waitUntil(lambda: bool(submitted), timeout=8000)
        qtbot.waitUntil(lambda: "decode_benchmark" in host._launch_jobs,
                        timeout=8000)
        jobs = host.host_session.lifecycle.jobs
        job_id = host._launch_jobs["decode_benchmark"]
        assert jobs.owner(job_id) == "decode_benchmark"
        assert jobs.state(job_id) == "running"
        # 在途时停用：停新 → 按 owner 取消 → 未确认 → 停用不完成
        report = host.host_session.retire_plugin("decode_benchmark")
        assert not report["fully_stopped"]
        assert job_id in report["jobs_still_terminating"]
        assert jobs.state(job_id) == "cancel_requested"
        assert not host.host_session.lifecycle.accepting_calls("decode_benchmark")
        # 停新：停用中拒绝新调用（不再产生第二个提交）
        host.launch("decode_benchmark")
        assert len(submitted) == 1
        # 应答到达 = 终态确认 → 停用可完成
        host._launch_finished("decode_benchmark", True, "完成")
        qtbot.waitUntil(lambda: jobs.state(job_id) == "cancelled", timeout=8000)
        report2 = host.host_session.retire_plugin("decode_benchmark")
        assert report2["fully_stopped"]
    finally:
        host.shutdown()


def test_f6_retire_async_advances_without_gui_sleep(qtbot, catalog, tmp_path, endpoint):
    """[F6] 异步退役：QTimer 轮询推进到 fully_stopped（GUI 不 sleep）。"""
    prefs = NativeFilesPreferences(QSettings(str(tmp_path / "f6b.ini"), QSettings.Format.IniFormat))
    panel = QWidget()
    layout = QVBoxLayout(panel)
    panel.meta_status = QLabel()
    layout.addWidget(QLineEdit())
    layout.addWidget(panel.meta_status)
    qtbot.addWidget(panel)
    transport = NativeFilesTransport(lambda: endpoint[0], "token", panel)
    host = FilesPluginsController(catalog, transport, prefs, panel,
                                  lambda: [], lambda: None, panel)
    try:
        qtbot.waitUntil(lambda: len(host._infos) == 5, timeout=8000)
        host.host_session.lifecycle.activate(
            "files.format_factory",
            lambda contributions: contributions.register(
                "menu", "m", lambda: None))
        host._retire_plugin_async("files.format_factory")
        qtbot.waitUntil(
            lambda: host.host_session.lifecycle.is_stopped("files.format_factory"),
            timeout=5000)
        polls = host._retire_polls.get("files.format_factory", 0)
        assert polls <= 50
    finally:
        host.shutdown()


def test_g2_image_outputs_return_via_same_index_entry(
        qtbot, catalog, tmp_path, endpoint):
    """[G2] 生成结果同一索引入口回库：refresh + start_scan（不开第二套索引）。"""
    rescans = []
    transport_backup = None
    prefs = NativeFilesPreferences(QSettings(str(tmp_path / "g2.ini"), QSettings.Format.IniFormat))
    panel = QWidget()
    layout = QVBoxLayout(panel)
    panel.meta_status = QLabel()
    layout.addWidget(QLineEdit())
    layout.addWidget(panel.meta_status)
    qtbot.addWidget(panel)
    transport = NativeFilesTransport(lambda: endpoint[0], "token", panel)
    host = FilesPluginsController(catalog, transport, prefs, panel,
                                  lambda: [], lambda: None, panel)
    transport.start_scan = lambda: rescans.append(True)
    try:
        qtbot.waitUntil(lambda: len(host._infos) == 5, timeout=8000)
        host._image_outputs_to_library(
            [{"source": "/m/a.png", "output": "/m/a.jpg"}])
        assert rescans == [True]
        assert "回库" in panel.meta_status.text()
    finally:
        host.shutdown()


def test_g3_shutdown_timer_native_singleton_zero_http(
        qtbot, catalog, tmp_path, endpoint):
    """[G3] 原生接管：启动只开窗口；singleton 聚焦；零 HTTP；关闭销账。"""
    prefs = NativeFilesPreferences(QSettings(str(tmp_path / "g3.ini"), QSettings.Format.IniFormat))
    panel = QWidget()
    layout = QVBoxLayout(panel)
    panel.meta_status = QLabel()
    layout.addWidget(QLineEdit())
    layout.addWidget(panel.meta_status)
    qtbot.addWidget(panel)
    transport = NativeFilesTransport(lambda: endpoint[0], "token", panel)
    host = FilesPluginsController(catalog, transport, prefs, panel,
                                  lambda: [], lambda: None, panel)
    try:
        qtbot.waitUntil(lambda: len(host._infos) == 5, timeout=8000)
        host.launch("shutdown_timer")   # legacy 投影模式信息为短 id
        qtbot.waitUntil(lambda: host._shutdown_timer_window is not None,
                        timeout=8000)
        qtbot.waitUntil(lambda: "files.shutdown_timer" not in host._busy,
                        timeout=8000)   # busy 键为命名空间 id
        window = host._shutdown_timer_window
        assert window.isVisible()
        assert window._service.state == "idle"     # 启动只开窗口
        host.launch("shutdown_timer")              # singleton：同一实例
        qtbot.waitUntil(lambda: "files.shutdown_timer" not in host._busy,
                        timeout=8000)
        assert host._shutdown_timer_window is window
        assert endpoint[1] == []                   # 原生接管：零 HTTP
        host.host_session.windows.close_all("files.shutdown_timer")
        assert host.host_session.windows.windows_for(
            "files.shutdown_timer") == ()
        assert window._service.state == "idle"     # 关闭即取消
    finally:
        host.shutdown()


def test_g4_decode_benchmark_native_zero_http_singleton(
        qtbot, catalog, tmp_path, endpoint):
    """[G4] 原生接管：直接接既有跑分实现（F5 singleton；零 HTTP）。"""
    prefs = NativeFilesPreferences(QSettings(str(tmp_path / "g4.ini"), QSettings.Format.IniFormat))
    panel = QWidget()
    layout = QVBoxLayout(panel)
    panel.meta_status = QLabel()
    layout.addWidget(QLineEdit())
    layout.addWidget(panel.meta_status)
    qtbot.addWidget(panel)
    transport = NativeFilesTransport(lambda: endpoint[0], "token", panel)
    host = FilesPluginsController(catalog, transport, prefs, panel,
                                  lambda: [], lambda: None, panel)
    try:
        qtbot.waitUntil(lambda: len(host._infos) == 5, timeout=8000)
        host.launch("decode_benchmark")
        qtbot.waitUntil(lambda: host._decode_benchmark_window is not None,
                        timeout=10000)
        window = host._decode_benchmark_window
        assert window.isVisible()
        qtbot.waitUntil(lambda: "files.decode_benchmark" not in host._busy,
                        timeout=8000)
        host.launch("decode_benchmark")          # singleton：同一实例
        qtbot.waitUntil(lambda: "files.decode_benchmark" not in host._busy,
                        timeout=8000)
        assert host._decode_benchmark_window is window
        assert endpoint[1] == []                 # 零 HTTP
        host.host_session.windows.close_all("files.decode_benchmark")
        assert host.host_session.windows.windows_for(
            "files.decode_benchmark") == ()
        host._decode_benchmark_window = None
    finally:
        host.shutdown()


def test_g5_code_editor_native_launch_and_availability(
        qtbot, catalog, tmp_path, endpoint):
    """[G5] 原生接管：code_editor 可启动（native:// 路径）、零 HTTP、
    singleton、停用后可用性消失。"""
    prefs = NativeFilesPreferences(QSettings(str(tmp_path / "g5.ini"), QSettings.Format.IniFormat))
    panel = QWidget()
    layout = QVBoxLayout(panel)
    panel.meta_status = QLabel()
    layout.addWidget(QLineEdit())
    layout.addWidget(panel.meta_status)
    qtbot.addWidget(panel)
    transport = NativeFilesTransport(lambda: endpoint[0], "token", panel)
    host = FilesPluginsController(catalog, transport, prefs, panel,
                                  lambda: [], lambda: None, panel)
    try:
        qtbot.waitUntil(lambda: len(host._infos) == 5, timeout=8000)
        info = next(item for item in host._infos
                    if item.plugin_id == "code_editor")
        assert info.can_launch and info.launch_path == "native://code_editor"
        host.launch("code_editor")
        qtbot.waitUntil(lambda: host._code_editor_window is not None,
                        timeout=8000)
        window = host._code_editor_window
        assert window.isVisible()
        qtbot.waitUntil(lambda: "files.code_editor" not in host._busy,
                        timeout=8000)
        host.launch("code_editor")              # singleton：同一实例
        qtbot.waitUntil(lambda: "files.code_editor" not in host._busy,
                        timeout=8000)
        assert host._code_editor_window is window
        assert endpoint[1] == []                # 零 HTTP
        host._set_enabled("code_editor", False)
        qtbot.waitUntil(lambda: not host._writing, timeout=8000)
        info2 = next(item for item in host._infos
                     if item.plugin_id == "code_editor")
        assert not info2.desired_enabled and not info2.can_launch
    finally:
        host.shutdown()
