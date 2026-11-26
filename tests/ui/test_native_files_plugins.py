"""Native Files plugin UI and command tests using isolated catalog storage."""
from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QLabel, QLineEdit, QVBoxLayout, QWidget
from pytestqt.qtbot import QtBot

from src.app.ui.files_plugins_controller import FilesPluginsController
from src.app.ui.native_files_plugins import PluginCenterDialog, PluginLauncherDialog
from src.app.ui.native_files_prefs import NativeFilesPreferences
from src.app.ui.native_files_transport import NativeFilesTransport
from src.features.files.builtin_plugins import BUILTIN_IDS, NativePluginCatalog
from src.shared.plugins.discovery import DesiredStateStore, PluginSourceConfig, current_host_facts
from src.shared.plugins.installer import LocalPackageInstaller


@pytest.fixture
def desired_store(tmp_path: Path) -> DesiredStateStore:
    return DesiredStateStore(tmp_path / "desired.json")


@pytest.fixture
def catalog(desired_store: DesiredStateStore) -> NativePluginCatalog:
    return NativePluginCatalog(desired_store=desired_store)


@pytest.fixture
def make_host(
    qtbot: QtBot, catalog: NativePluginCatalog, desired_store: DesiredStateStore, tmp_path: Path,
) -> Iterator[Callable[..., tuple[FilesPluginsController, QLineEdit]]]:
    hosts: list[FilesPluginsController] = []

    def create(
        *,
        selected_paths: Callable[[], list[str]] | None = None,
        shortcuts: dict[str, str] | None = None,
        pins: list[str] | None = None,
        sources: PluginSourceConfig | None = None,
        extra_count: int = 0,
    ) -> tuple[FilesPluginsController, QLineEdit]:
        prefs = NativeFilesPreferences(QSettings(
            str(tmp_path / "plugins.ini"), QSettings.Format.IniFormat))
        prefs.plugin_shortcuts = shortcuts or {}
        prefs.plugin_pins = pins or []
        panel = QWidget()
        layout = QVBoxLayout(panel)
        editor = QLineEdit(panel)
        panel.meta_status = QLabel(panel)
        layout.addWidget(editor)
        layout.addWidget(panel.meta_status)
        qtbot.addWidget(panel)
        transport = NativeFilesTransport(lambda: "", None, panel)
        sources = sources or PluginSourceConfig.for_host(
            builtin_ids=BUILTIN_IDS, install_root=tmp_path / "managed")
        host = FilesPluginsController(
            catalog, transport, prefs, panel, selected_paths or list, lambda: None, panel,
            sources_factory=lambda: sources, desired_store=desired_store,
        )
        hosts.append(host)
        qtbot.waitUntil(lambda: len(host._infos) == len(BUILTIN_IDS) + extra_count, timeout=8000)
        return host, editor

    yield create
    for host in hosts:
        host.shutdown()
        host._pool.waitForDone(3000)
        host.transport.shutdown()


def _native_test_openers(host: FilesPluginsController) -> list[tuple[str, tuple[str, ...]]]:
    """Exercise native command dispatch without starting a plugin window."""
    opened: list[tuple[str, tuple[str, ...]]] = []
    host._native_opener = lambda key: lambda paths: opened.append((key, tuple(paths)))
    return opened


@pytest.fixture
def controller(make_host):
    selected = ["/media/A #100% 中文.mov"]
    host, editor = make_host(
        selected_paths=lambda: list(selected),
        shortcuts={"files.image_factory": "Ctrl+Alt+I"},
    )
    return host, selected, editor


def test_center_keeps_disabled_failed_plaintext_and_filters(qtbot, catalog):
    rows = list(catalog.snapshot())
    rows[0] = replace(rows[0], name="<b>未执行的文本</b>", error="broken", launch_unavailable_reason="broken")
    rows[1] = replace(rows[1], desired_enabled=False, needs_restart=False, runtime_loaded=False)
    center = PluginCenterDialog()
    qtbot.addWidget(center)
    center.set_snapshot(rows, [], [], {}, set())
    assert center.items.count() == len(BUILTIN_IDS)
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


def test_center_function_category_pin_and_unavailable_tools(qtbot, catalog):
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
    center.select_plugin("topos_image")
    assert center._selected == "topos_image"
    assert not center.launch.isEnabled()
    assert center.reason.text()


def test_launcher_only_available_tools_pin_order_search_keyboard(qtbot, catalog):
    launcher = PluginLauncherDialog()
    qtbot.addWidget(launcher)
    rows = catalog.snapshot()
    launcher.set_snapshot(rows, ["shutdown_timer"], ["image_factory"], {}, set())
    assert launcher.items.count() == sum(info.can_launch for info in rows) == len(BUILTIN_IDS) - 2
    assert launcher.items.item(0).data(Qt.ItemDataRole.UserRole) == "shutdown_timer"
    assert launcher.items.item(1).data(Qt.ItemDataRole.UserRole) == "image_factory"
    launcher.show()
    qtbot.keyClick(launcher.search, Qt.Key.Key_Down)
    assert launcher.items.currentRow() == 1
    with qtbot.waitSignal(launcher.launch_requested) as signal:
        qtbot.keyClick(launcher.search, Qt.Key.Key_Return)
    assert signal.args == ["image_factory"]
    launcher.search.setText("format_factory")
    assert launcher.items.count() == 1
    launcher.search.setText("absent")
    assert launcher.items.count() == 0
    assert not launcher.empty.isHidden()


def test_initial_shortcuts_restore_without_opening_center_and_ignore_editor(controller):
    host, _, editor = controller
    assert len(host._actions) == 1
    assert host._actions[0].shortcut().toString() == "Ctrl+Alt+I"
    host._focus_changed(None, editor)
    assert not host._actions[0].isEnabled()
    host._focus_changed(editor, host.panel)
    assert host._actions[0].isEnabled()


def test_shortcut_conflicts_persistence_and_clear(controller):
    host, _, _ = controller
    plugin_id = "files.format_factory"
    host._set_shortcut(plugin_id, "Ctrl+Alt+I")
    assert "占用" in host.center.notice.text()
    assert plugin_id not in host.preferences.plugin_shortcuts
    host._set_shortcut(plugin_id, "Ctrl+C")
    assert plugin_id not in host.preferences.plugin_shortcuts
    host._set_shortcut(plugin_id, "P")
    assert "单组" in host.center.notice.text()
    host._set_shortcut(plugin_id, "Ctrl+Alt+F")
    assert host.preferences.plugin_shortcuts[plugin_id] == "Ctrl+Alt+F"
    host._set_shortcut(plugin_id, "")
    assert plugin_id not in host.preferences.plugin_shortcuts


def test_disable_builtin_is_persistent_and_takes_effect_immediately(controller, desired_store, qtbot):
    host, _, _ = controller
    plugin_id = "files.image_factory"
    host.center.select_plugin(plugin_id)
    host.center.enabled.click()
    assert host._writing
    host._project()
    assert not host.center.enabled.isEnabled()
    qtbot.waitUntil(lambda: not host._writing)
    row = next(info for info in host._infos if info.plugin_id == plugin_id)
    assert not row.desired_enabled and not row.runtime_loaded and not row.needs_restart
    assert not row.can_launch
    assert host.center.items.count() == len(BUILTIN_IDS)
    assert desired_store.load()[plugin_id] is False
    assert "生效" in host.center.notice.text()


def test_launch_freezes_selection_suppresses_duplicates_and_records_recent(controller, qtbot):
    host, selected, _ = controller
    opened = _native_test_openers(host)
    plugin_id = "files.decode_benchmark"
    host.launch(plugin_id)
    host.launch(plugin_id)
    selected[:] = ["/other.mov"]
    qtbot.waitUntil(lambda: bool(host.preferences.plugin_recent))
    assert opened == [("decode_benchmark", ("/media/A #100% 中文.mov",))]
    assert host.preferences.plugin_recent == [plugin_id]
    assert "已打开" in host.center.notice.text()


def test_native_launch_preserves_multiselection(controller, qtbot):
    host, selected, _ = controller
    opened = _native_test_openers(host)
    selected.append("/second.mov")
    host.launch("files.decode_benchmark")
    qtbot.waitUntil(lambda: bool(opened), timeout=8000)
    assert opened[0][1] == ("/media/A #100% 中文.mov", "/second.mov")


@pytest.mark.parametrize("reason", ["取消", "服务关闭", "权限拒绝", "源文件丢失"])
def test_native_open_failure_never_records_usage(controller, qtbot, reason):
    host, _, _ = controller

    def refuse(paths):
        raise ValueError(reason)

    host._native_opener = lambda key: refuse
    host.launch("files.decode_benchmark")
    qtbot.waitUntil(lambda: not host._busy)
    assert host.preferences.plugin_recent == []
    assert reason in host.center.notice.text()


def test_pin_roundtrip(controller):
    host, _, _ = controller
    host._pin("files.image_factory")
    assert host.preferences.plugin_pins == ["files.image_factory"]
    host._pin("files.image_factory")
    assert host.preferences.plugin_pins == []


def test_shutdown_discards_late_launch_completion(controller):
    host, _, _ = controller
    host.shutdown()
    host._launch_finished("image_factory", True, "late")
    assert host.preferences.plugin_recent == []


def test_rejected_dispatch_clears_native_busy_state(controller, qtbot):
    from src.shared.plugins.services import rejected

    host, _, _ = controller
    service = host._native_commands["decode_benchmark"]
    service.dispatch = lambda prepared, opener: rejected(
        prepared.command_id, error_code="INVOCATION_CANCELLED", message="调用已取消")
    host.launch("files.decode_benchmark")
    qtbot.waitUntil(lambda: not host._busy)
    assert host.preferences.plugin_recent == []
    assert host.center.notice.text() == "调用已取消"


def test_native_panel_plugin_toolbar_and_context_actions(qtbot):
    from src.app.ui.native_files_panel import NativeFilesPanel

    panel = NativeFilesPanel()
    qtbot.addWidget(panel)
    with qtbot.waitSignal(panel.plugins_requested):
        panel.plugins_button.click()
    menu = panel._build_context_menu(panel.icon_view, panel.icon_view.rect().center())
    assert any("使用插件" in action.text() for action in menu.actions())
    panel.shutdown()


@pytest.fixture
def f1_sources(tmp_path):
    """Discover installed and development plugins alongside packaged built-ins."""
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "topos.plugin.json").write_text(json.dumps({
        "plugin_id": "files.installed_tool", "display_name": "已安装工具",
        "version": "1.2.0",
        "capabilities": [{"capability_id": "launch", "kind": "command"}]}),
        encoding="utf-8")
    LocalPackageInstaller(tmp_path / "managed", host_facts=current_host_facts()).install(package)
    dev = tmp_path / "devsrc"
    (dev / "files.dev_tool").mkdir(parents=True)
    (dev / "files.dev_tool" / "topos.plugin.json").write_text(json.dumps({
        "plugin_id": "files.dev_tool", "display_name": "开发中工具",
        "version": "0.1.0",
        "capabilities": [{"capability_id": "launch", "kind": "command"}]}),
        encoding="utf-8")
    return PluginSourceConfig.for_host(
        builtin_ids=BUILTIN_IDS, install_root=tmp_path / "managed", dev_roots=[dev])


def test_controller_merges_three_sources_into_center(make_host, f1_sources):
    host, _ = make_host(sources=f1_sources, extra_count=2)
    by_id = {info.plugin_id: info for info in host._infos}
    builtin = by_id["files.format_factory"]
    assert builtin.source == "builtin" and builtin.install_state == "installed"
    assert builtin.command_key == "format_factory"
    installed = by_id["files.installed_tool"]
    assert installed.source == "installed"
    assert installed.version == "1.2.0"
    assert not installed.can_launch
    assert "尚未接入" in installed.launch_unavailable_reason
    assert not installed.desired_enabled
    dev = by_id["files.dev_tool"]
    assert dev.source == "local_dev" and dev.install_state == "discovered"
    assert not dev.desired_enabled
    host.center.show()
    host.center.select_plugin("files.dev_tool")
    assert "开发源" in host.center.origin.text()
    assert "未验证" in host.center.origin.text()
    host.center.select_plugin("files.format_factory")
    assert "内建" in host.center.origin.text()


def test_controller_duplicate_identity_shows_diagnostic(make_host, tmp_path, f1_sources):
    package = tmp_path / "dupe-pkg"
    package.mkdir()
    (package / "topos.plugin.json").write_text(json.dumps({
        "plugin_id": "files.format_factory", "display_name": "撞名安装包",
        "version": "9.9.9",
        "capabilities": [{"capability_id": "launch", "kind": "command"}]}),
        encoding="utf-8")
    LocalPackageInstaller(f1_sources.install_root, host_facts=current_host_facts()).install(package)
    host, _ = make_host(sources=f1_sources, extra_count=2)
    info = next(item for item in host._infos if item.plugin_id == "files.format_factory")
    assert info.source == "builtin"
    assert any("PLUGIN_ID_DUPLICATE" in note for note in info.diagnostics)


def test_inventory_only_enable_persists_desired(make_host, desired_store, f1_sources, qtbot):
    host, _ = make_host(sources=f1_sources, extra_count=2)
    plugin_id = "files.installed_tool"
    row = next(info for info in host._infos if info.plugin_id == plugin_id)
    assert not row.desired_enabled
    host._set_enabled(plugin_id, True)
    assert desired_store.load()[plugin_id] is True
    qtbot.waitUntil(lambda: next(
        info for info in host._infos if info.plugin_id == plugin_id).desired_enabled,
        timeout=8000)
    assert not next(info for info in host._infos if info.plugin_id == plugin_id).can_launch


def test_inventory_does_not_match_old_short_id_preferences(make_host, f1_sources):
    host, _ = make_host(sources=f1_sources, extra_count=2, pins=["format_factory"])
    host.center.show()
    host.center.select_plugin("files.format_factory")
    assert "固定到启动器" in host.center.pin.text()


def test_desired_state_store_roundtrip(tmp_path):
    state_file = tmp_path / "nested" / "desired.json"
    store = DesiredStateStore(state_file)
    assert store.load() == {}
    store.save({"files.a": True, "files.b": False})
    assert DesiredStateStore(state_file).load() == {"files.a": True, "files.b": False}
    store.save({})
    assert store.load() == {}


def test_shortcut_contribution_is_released_on_disable(make_host, qtbot):
    plugin_id = "files.format_factory"
    host, _ = make_host(shortcuts={plugin_id: "Ctrl+Alt+F"})
    assert host.host_session.shortcut_count(plugin_id) == 1
    host._set_enabled(plugin_id, False)
    qtbot.waitUntil(lambda: not host._writing, timeout=8000)
    assert host.host_session.shortcut_count(plugin_id) == 0
    actions = [action for action in host.panel.findChildren(QAction)
               if action.shortcut().toString() == "Ctrl+Alt+F"]
    assert not actions or not actions[0].isEnabled()


def test_launch_is_owned_job_and_retire_waits_for_confirmation(make_host, qtbot):
    host, _ = make_host(selected_paths=lambda: ["/m/a.mov"])
    opened = _native_test_openers(host)
    plugin_id = "files.decode_benchmark"
    started, release = threading.Event(), threading.Event()
    command = host._native_commands["decode_benchmark"]
    real_revalidate = command.revalidate

    def held_revalidate(prepared):
        started.set()
        assert release.wait(5)
        return real_revalidate(prepared)

    command.revalidate = held_revalidate
    try:
        host.launch(plugin_id)
        qtbot.waitUntil(started.is_set)
        jobs = host.host_session.lifecycle.jobs
        job_id = host._launch_jobs[plugin_id]
        assert jobs.owner(job_id) == plugin_id
        report = host.host_session.retire_plugin(plugin_id)
        assert not report["fully_stopped"]
        assert job_id in report["jobs_still_terminating"]
        assert jobs.state(job_id) == "cancel_requested"
        host.launch(plugin_id)
        release.set()
        qtbot.waitUntil(lambda: jobs.state(job_id) == "cancelled")
        assert opened == []
        assert host.host_session.retire_plugin(plugin_id)["fully_stopped"]
    finally:
        release.set()


def test_retire_async_advances_without_gui_sleep(make_host, qtbot):
    host, _ = make_host()
    plugin_id = "files.format_factory"
    host.host_session.lifecycle.activate(
        plugin_id, lambda contributions: contributions.register("menu", "m", lambda: None))
    host._retire_plugin_async(plugin_id)
    qtbot.waitUntil(lambda: host.host_session.lifecycle.is_stopped(plugin_id),
                    timeout=5000)
    assert host._retire_polls.get(plugin_id, 0) <= 50


def test_image_outputs_return_via_same_index_entry(make_host, monkeypatch):
    host, _ = make_host()
    rescans = []
    monkeypatch.setattr(host.transport, "start_scan", lambda: rescans.append(True))
    host._image_outputs_to_library([{"source": "/m/a.png", "output": "/m/a.jpg"}])
    assert rescans == [True]
    assert "回库" in host.panel.meta_status.text()


def test_shutdown_timer_native_singleton(make_host, qtbot):
    host, _ = make_host()
    plugin_id = "files.shutdown_timer"
    host.launch(plugin_id)
    qtbot.waitUntil(lambda: plugin_id not in host._busy, timeout=8000)
    window = host._shutdown_timer_window
    assert window.isVisible()
    assert window._service.state == "idle"
    host.launch(plugin_id)
    qtbot.waitUntil(lambda: plugin_id not in host._busy, timeout=8000)
    assert host._shutdown_timer_window is window
    host.host_session.windows.close_all(plugin_id)
    assert host.host_session.windows.windows_for(plugin_id) == ()
    assert window._service.state == "idle"


def test_decode_benchmark_native_singleton(make_host, qtbot):
    host, _ = make_host()
    plugin_id = "files.decode_benchmark"
    host.launch(plugin_id)
    qtbot.waitUntil(lambda: plugin_id not in host._busy, timeout=10000)
    window = host._decode_benchmark_window
    assert window.isVisible()
    host.launch(plugin_id)
    qtbot.waitUntil(lambda: plugin_id not in host._busy, timeout=8000)
    assert host._decode_benchmark_window is window
    host.host_session.windows.close_all(plugin_id)
    assert host.host_session.windows.windows_for(plugin_id) == ()
    host._decode_benchmark_window = None


def test_code_editor_native_launch_and_availability(make_host, qtbot):
    host, _ = make_host()
    plugin_id = "files.code_editor"
    info = next(item for item in host._infos if item.plugin_id == plugin_id)
    assert info.can_launch and info.launch_path == "native://code_editor"
    host.launch(plugin_id)
    qtbot.waitUntil(lambda: plugin_id not in host._busy, timeout=8000)
    window = host._code_editor_window
    assert window.isVisible()
    host.launch(plugin_id)
    qtbot.waitUntil(lambda: plugin_id not in host._busy, timeout=8000)
    assert host._code_editor_window is window
    host._set_enabled(plugin_id, False)
    qtbot.waitUntil(lambda: not host._writing, timeout=8000)
    updated = next(item for item in host._infos if item.plugin_id == plugin_id)
    assert not updated.desired_enabled and not updated.can_launch


def test_image_editor_native_launch_requires_one_image(make_host, tmp_path, qtbot):
    from PIL import Image

    media = tmp_path / "photo.png"
    Image.new("RGB", (48, 32), (200, 40, 40)).save(media, format="PNG")
    selected = [str(media)]
    host, _ = make_host(selected_paths=lambda: list(selected))
    plugin_id = "files.image_editor"
    host.launch(plugin_id)
    qtbot.waitUntil(lambda: plugin_id not in host._busy, timeout=8000)
    window = host._image_editor_window
    assert window.isVisible()
    assert window._service.source_path == str(media)
    host._image_editor_window = None
    host.host_session.windows.close_window(plugin_id, "main")
    selected.clear()
    host.launch(plugin_id)
    qtbot.waitUntil(lambda: "选择一张图片" in host.panel.meta_status.text(), timeout=8000)
