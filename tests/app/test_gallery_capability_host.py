"""G01 Gallery 能力宿主测试：安装／刷新／门控／revision 守卫／关闭。

用真实 PresetsPanel（根目录指向 tmp）＋ SimpleNamespace 窗口事实驱动，
验证门槛：无素材 Capture 禁用；仅 JPEG 且 grade 不可加载时 reference 可
用、apply 禁用；只读 album 写动作禁用；chrome more-menu 打开时同步门控。
"""
import json
import os
import zipfile
from types import SimpleNamespace

import pytest

from src.app.controllers.gallery_capability_host import (
    GalleryCapabilityHost,
    install_gallery_capability_host,
)


def _make_zip_topos(path):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("project.json", json.dumps({"layers": []}))


@pytest.fixture
def panel(tmp_path, monkeypatch, qtbot):
    monkeypatch.chdir(tmp_path)
    from src.features.color.ui import presets_panel
    monkeypatch.setattr(
        presets_panel, "_resolve_presets_root",
        lambda: str(tmp_path / "Presets"))
    widget = presets_panel.PresetsPanel()
    qtbot.addWidget(widget)
    return widget


def _seed(folder, name, *, real_zip=True, thumb=True, meta=None):
    topos = os.path.join(folder, f"{name}.topos")
    if real_zip:
        _make_zip_topos(topos)
    else:
        with open(topos, "wb") as fh:
            fh.write(b"not a zip")
    if thumb:
        with open(os.path.join(folder, f"{name}.thumb.jpg"), "wb") as fh:
            fh.write(b"thumb")
    if meta is not None:
        from src.features.color.core.preset_meta import save_preset_meta
        save_preset_meta(topos, meta)


def _window(panel, *, media="/tmp/grade.mov", video=True, engine=object()):
    return SimpleNamespace(
        presets_panel=panel,
        current_file_path=media,
        current_file_is_video=video,
        engine=engine,
    )


class TestInstall:
    def test_install_binds_panel_and_helpers(self, panel):
        window = _window(panel)
        host = install_gallery_capability_host(window)
        assert host is not None and host.install() is False  # 已装面板复装失败
        assert window._gallery_capability_host is host
        assert panel._gallery_capability_host is host

    def test_install_without_panel_returns_none(self):
        host = install_gallery_capability_host(SimpleNamespace())
        assert host is None

    def test_module_helpers_getattr_guarded(self, panel):
        from src.app.controllers.gallery_capability_host import (
            bump_gallery_context, refresh_gallery_capabilities,
        )
        refresh_gallery_capabilities(SimpleNamespace())   # 无宿主：no-op
        bump_gallery_context(SimpleNamespace())
        window = _window(panel)
        install_gallery_capability_host(window)
        refresh_gallery_capabilities(window)
        bump_gallery_context(window)
        assert window._gallery_capability_host.last_revision >= 1


class TestThresholds:
    def test_no_media_disables_capture(self, panel):
        window = _window(panel, media=None)
        host = install_gallery_capability_host(window)
        caps = host.last_capabilities
        assert caps["capture.still"].supported is False
        assert host.last_context.has_media is False

    def test_media_engine_writable_album_enable_capture(self, panel):
        window = _window(panel)
        host = install_gallery_capability_host(window)
        assert host.last_capabilities["capture.still"].supported is True
        assert host.last_context.album.album_id == "legacy:User"

    def test_jpeg_only_unloadable_grade_reference_yes_apply_no(self, panel):
        folder = panel.get_current_folder()
        _seed(folder, "Legacy", real_zip=False, thumb=True)  # grade 坏＋JPEG
        panel.scan_presets()
        panel.list_widget.item(0).setSelected(True)
        window = _window(panel)
        host = install_gallery_capability_host(window)
        caps = host.last_capabilities
        assert caps["reference.load"].supported is True
        assert caps["match.color"].supported is True
        assert caps["apply.grade"].supported is False
        assert caps["apply.categories"].supported is False
        assert host.last_context.selection_grade_loadable is False

    def test_loadable_grade_enables_apply(self, panel):
        folder = panel.get_current_folder()
        _seed(folder, "Good", real_zip=True, thumb=True)
        panel.scan_presets()
        panel.list_widget.item(0).setSelected(True)
        window = _window(panel)
        host = install_gallery_capability_host(window)
        assert host.last_capabilities["apply.grade"].supported is True
        assert host.last_context.selection_grade_loadable is True

    def test_readonly_album_blocks_write_actions(self, panel, tmp_path):
        if os.geteuid() == 0:
            pytest.skip("root ignores directory permission bits")
        folder = panel.get_current_folder()
        _seed(folder, "Still", real_zip=True, thumb=True)
        panel.scan_presets()
        panel.list_widget.item(0).setSelected(True)
        folder_path = tmp_path / "Presets" / "User"
        folder_path.chmod(0o555)
        try:
            window = _window(panel)
            host = install_gallery_capability_host(window)
            caps = host.last_capabilities
            assert caps["capture.still"].supported is False
            assert caps["rename.bundle"].supported is False
            assert caps["delete.bundle"].supported is False
            assert caps["reference.load"].supported is True
        finally:
            folder_path.chmod(0o755)


class TestMenuGating:
    def _capture_action(self, panel):
        chrome = panel._gallery
        for action in chrome.more_menu.actions():
            if action.text().rstrip("…").strip() == "Capture Still":
                return action
        return None

    def test_more_menu_capture_disabled_without_media(self, panel):
        window = _window(panel, media=None)
        install_gallery_capability_host(window)
        chrome = panel._gallery
        chrome.more_menu.aboutToShow.emit()   # chrome 重建 → 宿主门控
        action = self._capture_action(panel)
        assert action is not None
        assert action.isEnabled() is False
        assert action.toolTip()

    def test_more_menu_capture_enabled_with_media(self, panel):
        window = _window(panel)
        install_gallery_capability_host(window)
        panel._gallery.more_menu.aboutToShow.emit()
        assert self._capture_action(panel).isEnabled() is True

    def test_panel_context_menu_capability_applied_to_action(self, panel):
        from PyQt6.QtGui import QAction
        window = _window(panel, media=None)
        install_gallery_capability_host(window)
        action = QAction(panel)
        panel._apply_action_capability("capture.still", action)
        assert action.isEnabled() is False
        assert action.toolTip()
        # 未知 key／宿主未安装时不触碰
        other = QAction(panel)
        panel._apply_action_capability("nonexistent.key", other)
        assert other.isEnabled() is True

    def test_menu_gate_refreshes_media_state_live(self, panel):
        """菜单打开时同步重评——媒体状态变化无需额外信号线。"""
        window = _window(panel, media=None)
        install_gallery_capability_host(window)
        panel._gallery.more_menu.aboutToShow.emit()
        assert self._capture_action(panel).isEnabled() is False
        window.current_file_path = "/tmp/late.mov"   # 菜单再次打开前才变
        panel._gallery.more_menu.aboutToShow.emit()
        assert self._capture_action(panel).isEnabled() is True

    def test_panel_without_host_keeps_legacy_enabled_behavior(self, panel):
        from PyQt6.QtGui import QAction
        action = QAction(panel)
        panel._apply_action_capability("capture.still", action)
        assert action.isEnabled() is True
        assert panel.gallery_action_caps() is None


class TestRevisionGuardAndShutdown:
    def test_mid_refresh_bump_discards_stale_evaluation(self, panel):
        window = _window(panel, media=None)
        host = GalleryCapabilityHost(window)
        host.install()
        before_rev = host.last_revision
        original = host._gather_context

        def _gather_with_mid_change():
            context = original()
            host.bump_context_revision()   # 评估期间上下文又变
            return context

        host._gather_context = _gather_with_mid_change
        host.refresh()
        assert host.last_revision == before_rev       # 陈旧结果被丢弃
        assert host._revision == before_rev + 1

    def test_folder_change_bumps_and_refreshes(self, panel, qtbot):
        window = _window(panel)
        host = install_gallery_capability_host(window)
        rev = host._revision
        # 模拟 folder_combo 切换（直接调槽避免 UI 索引细节）
        host._on_album_changed()
        assert host._revision == rev + 1
        assert host.last_revision == rev + 1

    def test_selection_change_refreshes_capabilities(self, panel, qtbot):
        folder = panel.get_current_folder()
        _seed(folder, "Good", real_zip=True, thumb=True)
        panel.scan_presets()
        window = _window(panel)
        host = install_gallery_capability_host(window)
        assert host.last_context.selection is None
        panel.list_widget.item(0).setSelected(True)
        assert host.last_context.selection is not None
        assert host.last_context.selection.display_name == "Good"

    def test_shutdown_stops_refresh_and_clears_panel_ref(self, panel):
        window = _window(panel)
        host = install_gallery_capability_host(window)
        caps_before = host.last_capabilities
        host.mark_shutdown()
        assert host.shutdown is True
        assert panel._gallery_capability_host is None
        window.current_file_path = None
        host.refresh()
        assert host.last_capabilities == caps_before   # no-op
        # more-menu 门控同样 no-op：菜单保持 chrome 重建的原状
        panel._gallery.more_menu.aboutToShow.emit()

    def test_diagnostics_snapshot(self, panel):
        window = _window(panel)
        host = install_gallery_capability_host(window)
        diag = host.diagnostics()
        assert diag["host_shutdown"] is False
        assert diag["album_id"] == "legacy:User"
        assert diag["has_media"] is True
        assert "capture.still" in diag["capabilities"]
        host.mark_shutdown()
        assert host.diagnostics()["host_shutdown"] is True
